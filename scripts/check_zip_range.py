"""READ-ONLY check that one evidence ZIP can be served by HTTP Range (frames desde ZIP).

This is the "Range test in the PR" deliverable of Lookia Platform#frames-desde-zip
(docs/frames-from-zip/DESIGN.md). Given a stored ``zip_url`` (and optionally a
``zip_entry`` or ``image_name``) it does what GET /api/v1/frames/{point_id} does,
step by step, and prints what the store answered at each step:

1. Presign when the URL is a private ``lucam-assets`` key
   (``POST {STORAGE_SERVICE_URL}/internal/presigned-url``, same body + X-User-*
   headers as the service). Public (``/lucam-assets/public/``) and foreign URLs
   are read as-is.
2. ONE tail Range GET (``bytes=-67584``): status, Content-Range, bytes received.
   If the central directory starts before the tail, ONE more Range GET for it.
3. Pick the entry (exact ``zip_entry``, else first basename == ``image_name``,
   else the first image entry) and print its offset / sizes / compression.
4. ONE entry Range GET (local header + data + slack): status, Content-Range,
   bytes received, then decode + size/CRC check.

It NEVER writes anything: no file output, no DB, no Redis, no PUT/DELETE. The
only non-GET request is the presign POST, which only asks storage-service for a
signed GET URL. The signed URL is not printed (only its host + path).

Exit code: 0 when every step passed (206 on both Range GETs and the entry
decodes), 1 when any step failed, 2 on bad arguments.

Examples
--------
Public ZIP, entry by basename:
    python scripts/check_zip_range.py \\
        https://storage.lookia.mx/lucam-assets/public/evidences/ev1.zip \\
        --image-name 20261021-001529_457445.jpg

Private ZIP (presigned through storage-service), exact entry:
    STORAGE_SERVICE_URL=http://storage-service:8006 \\
    python scripts/check_zip_range.py \\
        https://storage.lookia.mx/lucam-assets/evidences/2026/ev1.zip \\
        --zip-entry cropped/20261021-001529_457445.jpg

Connection: STORAGE_SERVICE_URL from env or ``--storage-service-url``
(default ``http://storage-service:8006``, the service default).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Run from the repo root or from scripts/: make ``src`` importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.zip_frame_reader import (  # noqa: E402
    _LOCAL_HEADER,
    LOCAL_HEADER_SLACK,
    TAIL_BYTES,
    FrameReadError,
    ZipEntryInfo,
    ZipIndex,
    _decode_entry,
    _index_from_zipfile,
    _Missing,
    _Sparse,
    is_public_storage_url,
    object_name_from_url,
    presign_get_url,
    select_entry,
)

DEFAULT_STORAGE_SERVICE_URL = "http://storage-service:8006"
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_COMPRESSION_NAMES = {zipfile.ZIP_STORED: "stored", zipfile.ZIP_DEFLATED: "deflate"}


# ── Report ───────────────────────────────────────────────────────────────────


@dataclass
class Step:
    """One request (or local step) and what came back."""

    name: str
    ok: bool
    status: int | None = None
    content_range: str | None = None
    bytes_received: int | None = None
    detail: str = ""


@dataclass
class CheckReport:
    zip_url: str
    mode: str = ""  # "public" | "presigned" | "direct"
    total_size: int | None = None
    entry: ZipEntryInfo | None = None
    entry_bytes: int | None = None
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)


def redact_url(url: str) -> str:
    """Host + path only: never print a signature or any other query string."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """(start, end, total) from ``bytes <start>-<end>/<total>``; None if unusable."""
    try:
        unit, rest = (value or "").strip().split(" ", 1)
        span, total = rest.split("/", 1)
        start, end = span.split("-", 1)
        if unit != "bytes":
            return None
        return int(start), int(end), int(total)
    except ValueError:
        return None


def pick_entry(index: ZipIndex, *, zip_entry: str | None, image_name: str | None) -> ZipEntryInfo:
    """Same rule as the service; with neither given, the first image entry."""
    if zip_entry or image_name:
        return select_entry(index.entries, zip_entry=zip_entry, image_name=image_name)
    for entry in index.entries:
        name = entry.name
        if "__MACOSX/" in name or name.endswith("/"):
            continue
        if name.lower().endswith(_IMAGE_SUFFIXES):
            return entry
    raise FrameReadError("no image entry in ZIP (pass --zip-entry or --image-name)")


def entry_range_header(entry: ZipEntryInfo, total_size: int) -> str:
    """Range over the entry's local header + data + slack (what the service asks)."""
    want = (
        _LOCAL_HEADER.size
        + len(entry.name.encode("utf-8"))
        + entry.compress_size
        + LOCAL_HEADER_SLACK
    )
    end = min(total_size, entry.header_offset + want) - 1
    return f"bytes={entry.header_offset}-{end}"


def format_report(report: CheckReport) -> str:
    lines = [f"zip_url: {report.zip_url}", f"mode:    {report.mode or '-'}"]
    if report.total_size is not None:
        lines.append(f"size:    {report.total_size} bytes")
    for step in report.steps:
        mark = "OK  " if step.ok else "FAIL"
        parts = [f"[{mark}] {step.name}"]
        if step.status is not None:
            parts.append(f"status={step.status}")
        if step.content_range is not None:
            parts.append(f"content-range={step.content_range!r}")
        if step.bytes_received is not None:
            parts.append(f"bytes={step.bytes_received}")
        if step.detail:
            parts.append(step.detail)
        lines.append("  ".join(parts))
    if report.entry is not None:
        e = report.entry
        lines.append(
            f"entry:   {e.name}  offset={e.header_offset}  compress_size={e.compress_size}"
            f"  file_size={e.file_size}"
            f"  method={_COMPRESSION_NAMES.get(e.compress_type, e.compress_type)}"
        )
    if report.entry_bytes is not None:
        lines.append(f"decoded: {report.entry_bytes} bytes (size + CRC-32 verified)")
    lines.append(f"RESULT:  {'PASS' if report.ok else 'FAIL'}")
    return "\n".join(lines)


# ── Check ────────────────────────────────────────────────────────────────────


async def _get(client: httpx.AsyncClient, url: str, range_header: str) -> httpx.Response:
    return await client.get(url, headers={"Range": range_header})


async def run_check(
    client: httpx.AsyncClient,
    zip_url: str,
    *,
    zip_entry: str | None = None,
    image_name: str | None = None,
    storage_service_url: str = DEFAULT_STORAGE_SERVICE_URL,
) -> CheckReport:
    """Presign (if private) → tail Range GET → entry Range GET. GET + one presign POST only."""
    report = CheckReport(zip_url=zip_url)

    # 1) Fetch URL.
    if is_public_storage_url(zip_url):
        report.mode, fetch_url = "public", zip_url
    elif object_name_from_url(zip_url) is None:
        report.mode, fetch_url = "direct", zip_url
    else:
        report.mode = "presigned"
        try:
            fetch_url = await presign_get_url(
                client, zip_url, storage_service_url=storage_service_url
            )
        except FrameReadError as exc:
            report.steps.append(Step("presign", ok=False, detail=str(exc)))
            return report
        report.steps.append(Step("presign", ok=True, detail=f"-> {redact_url(fetch_url)}"))

    # 2) Tail Range GET.
    try:
        tail = await _get(client, fetch_url, f"bytes=-{TAIL_BYTES}")
    except httpx.HTTPError as exc:
        report.steps.append(Step("tail range GET", ok=False, detail=type(exc).__name__))
        return report
    cr = parse_content_range(tail.headers.get("content-range"))
    tail_ok = tail.status_code == 206 and cr is not None and len(tail.content) > 0
    detail = "" if tail_ok else "expected 206 with Content-Range and a non-empty body"
    report.steps.append(
        Step(
            "tail range GET",
            ok=tail_ok,
            status=tail.status_code,
            content_range=tail.headers.get("content-range"),
            bytes_received=len(tail.content),
            detail=detail,
        )
    )
    if not tail_ok or cr is None:
        return report
    start, _end, total = cr
    report.total_size = total

    # Central directory from the tail; at most ONE more Range GET if it starts earlier.
    sparse = _Sparse(total)
    sparse.add(start, tail.content)
    try:
        try:
            index = _index_from_zipfile(sparse, total)
        except _Missing as missing:
            cd = await _get(client, fetch_url, f"bytes={missing.pos}-{start - 1}")
            cd_ok = cd.status_code == 206 and len(cd.content) == start - missing.pos
            report.steps.append(
                Step(
                    "central-directory range GET",
                    ok=cd_ok,
                    status=cd.status_code,
                    content_range=cd.headers.get("content-range"),
                    bytes_received=len(cd.content),
                )
            )
            if not cd_ok:
                return report
            sparse.add(missing.pos, cd.content)
            index = _index_from_zipfile(sparse, total)
    except (_Missing, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError) as exc:
        report.steps.append(Step("parse central directory", ok=False, detail=repr(exc)))
        return report
    except httpx.HTTPError as exc:
        report.steps.append(
            Step("central-directory range GET", ok=False, detail=type(exc).__name__)
        )
        return report

    # 3) Entry.
    try:
        entry = pick_entry(index, zip_entry=zip_entry, image_name=image_name)
    except FrameReadError as exc:
        report.steps.append(
            Step("select entry", ok=False, detail=f"{exc} ({len(index.entries)} entries)")
        )
        return report
    report.entry = entry

    # 4) Entry Range GET + decode.
    try:
        resp = await _get(client, fetch_url, entry_range_header(entry, total))
    except httpx.HTTPError as exc:
        report.steps.append(Step("entry range GET", ok=False, detail=type(exc).__name__))
        return report
    ecr = parse_content_range(resp.headers.get("content-range"))
    entry_ok = resp.status_code == 206 and ecr is not None and ecr[0] == entry.header_offset
    report.steps.append(
        Step(
            "entry range GET",
            ok=entry_ok,
            status=resp.status_code,
            content_range=resp.headers.get("content-range"),
            bytes_received=len(resp.content),
            detail="" if entry_ok else "expected 206 starting at the entry offset",
        )
    )
    if not entry_ok:
        return report
    try:
        decoded = _decode_entry(resp.content, entry)
    except FrameReadError as exc:
        report.steps.append(Step("decode entry", ok=False, detail=str(exc)))
        return report
    if isinstance(decoded, int):
        # Long local extra field: the service would fetch one more range; report it.
        report.steps.append(
            Step(
                "decode entry",
                ok=False,
                detail=f"local extra field longer than the slack: needs {decoded} bytes",
            )
        )
        return report
    report.entry_bytes = len(decoded)
    report.steps.append(Step("decode entry", ok=True))
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="READ-ONLY Range check of one evidence ZIP (frames desde ZIP)."
    )
    parser.add_argument("zip_url", help="stored zip_url (as in evidence_embeddings.zip_url)")
    parser.add_argument("--zip-entry", default=None, help="exact entry path inside the ZIP")
    parser.add_argument("--image-name", default=None, help="basename to match when no zip-entry")
    parser.add_argument(
        "--storage-service-url",
        default=os.environ.get("STORAGE_SERVICE_URL", DEFAULT_STORAGE_SERVICE_URL),
        help="storage-service base for the presign POST (env STORAGE_SERVICE_URL)",
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="per-request timeout (s)")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout), follow_redirects=True
    ) as client:
        report = await run_check(
            client,
            args.zip_url,
            zip_entry=args.zip_entry,
            image_name=args.image_name,
            storage_service_url=args.storage_service_url,
        )
    print(format_report(report))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.zip_url.startswith(("http://", "https://")):
        print("zip_url must be an http(s) URL", file=sys.stderr)
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
