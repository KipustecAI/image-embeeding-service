"""Read ONE frame out of a remote evidence ZIP with HTTP Range requests.

Frames desde ZIP (Lookia Platform#frames-desde-zip): in FRAME_STORAGE_MODE=zip_ref
the ingest no longer uploads frames; GET /api/v1/frames/{point_id} serves each one
from the evidence ZIP instead. Reading the whole ZIP (~2.5 MB) per frame is
wasteful, so (ported from compute-image-redlight ``zip_download.py`` and
lookia-dw-pass-clip ``cameras/frames.py``):

  1. GET the tail (~66 KB) → End Of Central Directory + central directory
     (ZIP64 included; ``zipfile`` parses it over a sparse in-memory view).
     A central directory bigger than the tail is fetched with one more Range.
  2. GET a Range over the one entry's local header + data; inflate if deflated
     (the edge writes them stored, most likely), check size + CRC-32.

Fallbacks:
  * The store answers 200 instead of 206 → the body IS the ZIP: use it.
  * 416 or a 0-byte body = stale empty object cached by Cloudflare (#1697/#1698):
    retry ONCE with ``?nc=<token>`` — ONLY for unsigned URLs (a query on a
    presigned URL invalidates the signature).

Private objects: only ``/lucam-assets/public/`` is anonymously readable. Any other
lucam-assets key is presigned server-side through storage-service
(``POST /internal/presigned-url``), ported from image-embedding-compute
``store_download.py``. NEVER append anything to a presigned URL.

Pure and injectable: every function takes an ``httpx.AsyncClient`` (tests pass one
with ``httpx.MockTransport``); no settings are read here.

Errors are typed so the endpoint maps them without guessing:
  * ``FrameTimeout``       → 504 (deadline or httpx timeout)
  * ``FrameUnreadable``    → 502 (storage error, empty/bad ZIP, presign failure)
  * ``FrameEntryNotFound`` → 502 (subclass of FrameUnreadable)
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import uuid
import zipfile
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

STORAGE_BUCKET = "lucam-assets"
PUBLIC_PATH_MARKER = f"/{STORAGE_BUCKET}/public/"
PRESIGN_CALLER_ID = "image-embeeding-service"

# EOCD (22) + max comment (65535) + ZIP64 locator (20) + ZIP64 EOCD (56): the
# whole end-of-archive search window of ``zipfile`` fits in one tail request.
TAIL_BYTES = 66 * 1024
# The local header's extra field can be longer than the central directory's.
LOCAL_HEADER_SLACK = 1024
# Guards against serving something that is not a frame (e.g. a clip).
MAX_ENTRY_BYTES = 32 * 1024 * 1024
# Guards the full-body fallback and a central directory that misses the tail.
MAX_FULL_ZIP_BYTES = 64 * 1024 * 1024
MAX_CENTRAL_DIR_BYTES = 16 * 1024 * 1024

_LOCAL_HEADER = struct.Struct("<4sHHHHHIIIHH")  # 30 bytes
_LOCAL_SIGNATURE = b"PK\x03\x04"

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


# ── Errors ───────────────────────────────────────────────────────────────────


class FrameReadError(Exception):
    """Base of every frame-read failure."""


class FrameTimeout(FrameReadError):
    """The overall deadline or an httpx timeout expired (→ 504)."""


class FrameUnreadable(FrameReadError):
    """Storage error, empty/bad ZIP or presign failure (→ 502)."""


class FrameEntryNotFound(FrameUnreadable):
    """The ZIP is readable but has no matching entry (→ 502)."""


class PresignError(FrameUnreadable):
    """storage-service could not presign the object."""


class _EmptyObject(FrameUnreadable):
    """416 or 0-byte body: stale empty object, retried once with ?nc= (unsigned only)."""


class _Missing(OSError):
    """The sparse view does not hold that byte range yet."""

    def __init__(self, pos: int):
        super().__init__(f"range at {pos} not read")
        self.pos = pos


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ZipEntryInfo:
    """What the central directory says about one entry (enough to Range-read it)."""

    name: str
    header_offset: int
    compress_size: int
    file_size: int
    compress_type: int
    flag_bits: int
    crc: int


@dataclass(frozen=True)
class ZipIndex:
    """Parsed central directory of one ZIP."""

    total_size: int
    entries: tuple[ZipEntryInfo, ...]


@dataclass(frozen=True)
class FrameData:
    data: bytes
    entry_name: str
    content_type: str


class ZipIndexCache:
    """In-process LRU of parsed central directories, keyed by the STORED zip_url
    (never by a presigned URL, which changes on every presign). Only successful
    parses are put here; a failed read drops the key."""

    def __init__(self, max_entries: int = 512):
        self.max_entries = max(0, int(max_entries))
        self._data: OrderedDict[str, ZipIndex] = OrderedDict()

    def get(self, key: str) -> ZipIndex | None:
        index = self._data.get(key)
        if index is not None:
            self._data.move_to_end(key)
        return index

    def put(self, key: str, index: ZipIndex) -> None:
        if self.max_entries == 0 or not index.entries:
            return
        self._data[key] = index
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def discard(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


# ── URL helpers (public detection, presign) ─────────────────────────────────


def content_type_for(name: str) -> str:
    """Content-Type from the entry extension; image/jpeg by default."""
    lowered = (name or "").lower()
    dot = lowered.rfind(".")
    return _CONTENT_TYPES.get(lowered[dot:], "image/jpeg") if dot >= 0 else "image/jpeg"


def is_public_storage_url(url: str) -> bool:
    """True for keys under ``/lucam-assets/public/`` (anonymously readable)."""
    return (urlparse(url).path or "").startswith(PUBLIC_PATH_MARKER)


def is_presigned_url(url: str) -> bool:
    """True when the URL carries an S3 signature (nothing may be appended to it)."""
    query = urlparse(url).query or ""
    return "X-Amz-Signature" in query or "X-Amz-Credential" in query


def object_name_from_url(url: str, bucket: str = STORAGE_BUCKET) -> str | None:
    """Key relative to the bucket: no host, no leading ``/``, no bucket, no query.

    ``https://storage.lookia.mx/lucam-assets/a/b.zip?x=1`` → ``a/b.zip``.
    None when the path is not ``/<bucket>/<key>``.
    """
    path = unquote(urlparse(url).path or "")
    prefix = f"/{bucket}/"
    if not path.startswith(prefix):
        return None
    key = path[len(prefix) :].lstrip("/")
    return key or None


async def presign_get_url(
    client: httpx.AsyncClient,
    zip_url: str,
    *,
    storage_service_url: str,
    expiry_hours: int = 1,
) -> str:
    """Presigned GET for a private lucam-assets object via storage-service.

    ``POST {storage}/internal/presigned-url`` with ``X-User-Id: image-embeeding-service``
    and ``X-User-Role: service``. Raises ``PresignError`` (or ``FrameTimeout``).
    """
    object_name = object_name_from_url(zip_url)
    if object_name is None:
        raise PresignError(f"cannot derive object_name from {zip_url!r}")
    if not storage_service_url:
        raise PresignError("STORAGE_SERVICE_URL is not set")
    endpoint = f"{storage_service_url.rstrip('/')}/internal/presigned-url"
    body = {
        "bucket_name": STORAGE_BUCKET,
        "object_name": object_name,
        "method": "GET",
        "expiry_hours": max(1, min(int(expiry_hours), 24)),
    }
    headers = {"X-User-Id": PRESIGN_CALLER_ID, "X-User-Role": "service"}
    try:
        resp = await client.post(endpoint, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise FrameTimeout(f"presign timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise PresignError(f"presign request failed ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise PresignError(f"presign HTTP {resp.status_code}")
    try:
        payload = resp.json() or {}
    except ValueError as exc:
        raise PresignError("presign returned non-JSON") from exc
    url = payload.get("url") if isinstance(payload, dict) else None
    if not payload.get("success") or not isinstance(url, str) or not url:
        raise PresignError("presign returned no url")
    return url


def check_allowed_host(url: str, allowed_hosts: frozenset[str] | None) -> None:
    """Raise ``FrameUnreadable`` when ``url`` is not on an allowed host.

    The frame route is public, so the stored ``zip_url`` (and the presigned URL
    storage-service returns) must never send our server anywhere else.
    ``None`` = no check (unit tests of the pure reader).
    """
    if allowed_hosts is None:
        return
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed_hosts:
        raise FrameUnreadable(f"host {host!r} is not an allowed storage host")


async def resolve_fetch_url(
    client: httpx.AsyncClient,
    zip_url: str,
    *,
    storage_service_url: str,
    allowed_hosts: frozenset[str] | None = None,
) -> str:
    """URL to read the ZIP from: the stored URL when public, else a presigned one.

    URLs outside lucam-assets (no ``/lucam-assets/`` path) are read as-is.
    """
    check_allowed_host(zip_url, allowed_hosts)
    if is_public_storage_url(zip_url) or object_name_from_url(zip_url) is None:
        return zip_url
    url = await presign_get_url(client, zip_url, storage_service_url=storage_service_url)
    check_allowed_host(url, allowed_hosts)
    return url


# ── Entry match ──────────────────────────────────────────────────────────────


def _is_candidate(name: str) -> bool:
    return not (name.startswith("__MACOSX/") or "/__MACOSX/" in name or name.endswith("/"))


def select_entry(
    entries: tuple[ZipEntryInfo, ...] | list[ZipEntryInfo],
    *,
    zip_entry: str | None,
    image_name: str | None,
) -> ZipEntryInfo:
    """Exact ``zip_entry`` when present; else the first entry (central-directory
    order, like the old ZipProcessor) whose basename == ``image_name``.
    ``__MACOSX/`` and directories are always skipped. A present-but-unmatched
    zip_entry does NOT fall back to the basename (it could pick a sibling in
    ``low_res/`` or ``cropped/``)."""
    candidates = [e for e in entries if _is_candidate(e.name)]
    if zip_entry:
        for entry in candidates:
            if entry.name == zip_entry:
                return entry
        raise FrameEntryNotFound(f"entry {zip_entry!r} not in ZIP")
    if image_name:
        base = image_name.rsplit("/", 1)[-1]
        for entry in candidates:
            if entry.name.rsplit("/", 1)[-1] == base:
                return entry
        raise FrameEntryNotFound(f"no entry named {image_name!r} in ZIP")
    raise FrameEntryNotFound("neither zip_entry nor image_name given")


# ── ZIP parsing ──────────────────────────────────────────────────────────────


class _Sparse(io.RawIOBase):
    """File of ``size`` bytes of which only some ranges are known."""

    def __init__(self, size: int):
        self.size, self.pos, self.parts = size, 0, []

    def add(self, start: int, data: bytes) -> None:
        self.parts.append((start, data))

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence] + offset
        return self.pos

    def readinto(self, b) -> int:
        want = min(len(b), max(0, self.size - self.pos))
        n = 0
        while n < want:
            hit = next(((s, d) for s, d in self.parts if s <= self.pos < s + len(d)), None)
            if hit is None:
                raise _Missing(self.pos)
            s, d = hit
            chunk = d[self.pos - s : self.pos - s + want - n]
            b[n : n + len(chunk)] = chunk
            n += len(chunk)
            self.pos += len(chunk)
        return n


def _index_from_zipfile(fp, total_size: int) -> ZipIndex:
    with zipfile.ZipFile(fp) as zf:
        entries = tuple(
            ZipEntryInfo(
                name=zi.filename,
                header_offset=zi.header_offset,
                compress_size=zi.compress_size,
                file_size=zi.file_size,
                compress_type=zi.compress_type,
                flag_bits=zi.flag_bits,
                crc=zi.CRC,
            )
            for zi in zf.infolist()
        )
    return ZipIndex(total_size=total_size, entries=entries)


def parse_index(data: bytes) -> ZipIndex:
    """Central directory of a complete ZIP held in memory."""
    if not data:
        raise _EmptyObject("empty ZIP body")
    try:
        return _index_from_zipfile(io.BytesIO(data), len(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError) as exc:
        raise FrameUnreadable(f"bad ZIP ({type(exc).__name__}: {exc})") from exc


def _decode_entry(local: bytes, entry: ZipEntryInfo) -> bytes | int:
    """Bytes of ``entry`` from ``local`` (starting at its local header).

    Returns an int (total bytes needed from the header start) when ``local`` is
    too short, so the caller can fetch the missing range.
    """
    if len(local) < _LOCAL_HEADER.size:
        return _LOCAL_HEADER.size + LOCAL_HEADER_SLACK + entry.compress_size
    fields = _LOCAL_HEADER.unpack_from(local, 0)
    if fields[0] != _LOCAL_SIGNATURE:
        raise FrameUnreadable(f"bad local header for {entry.name!r}")
    name_len, extra_len = fields[9], fields[10]
    data_start = _LOCAL_HEADER.size + name_len + extra_len
    needed = data_start + entry.compress_size
    if len(local) < needed:
        return needed
    raw = local[data_start:needed]
    if entry.flag_bits & 0x1:
        raise FrameUnreadable(f"entry {entry.name!r} is encrypted")
    if entry.compress_type == zipfile.ZIP_STORED:
        data = bytes(raw)
    elif entry.compress_type == zipfile.ZIP_DEFLATED:
        try:
            # Inflate at most the declared size + 1 byte and never flush(): a
            # stream that inflates past file_size is a bomb, not a frame.
            inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            data = inflater.decompress(raw, entry.file_size + 1)
        except zlib.error as exc:
            raise FrameUnreadable(f"cannot inflate {entry.name!r}: {exc}") from exc
        if inflater.unconsumed_tail or not inflater.eof or len(data) != entry.file_size:
            raise FrameUnreadable(f"deflate stream of {entry.name!r} does not match its size")
    else:
        raise FrameUnreadable(f"unsupported compression {entry.compress_type} for {entry.name!r}")
    if len(data) != entry.file_size or zlib.crc32(data) != entry.crc:
        raise FrameUnreadable(f"size/CRC mismatch for {entry.name!r}")
    return data


def _entry_from_full(body: bytes, entry: ZipEntryInfo) -> bytes:
    result = _decode_entry(body[entry.header_offset :], entry)
    if isinstance(result, int):
        raise FrameUnreadable(f"ZIP truncated inside {entry.name!r}")
    return result


# ── HTTP ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Reply:
    status: int
    content_range: str | None
    content: bytes


def _content_range(reply: _Reply) -> tuple[int, int] | None:
    """(start, total) from ``Content-Range: bytes <start>-<end>/<total>``."""
    try:
        unit_range, total = (reply.content_range or "").split("/", 1)
        start = int(unit_range.strip().split(" ", 1)[1].split("-", 1)[0])
        return start, int(total)
    except (IndexError, ValueError):
        return None


async def _range_get(client: httpx.AsyncClient, url: str, range_header: str | None) -> _Reply:
    """GET (optionally ranged), streamed under MAX_FULL_ZIP_BYTES, mapping
    failures to typed errors: 416 / 0-byte → ``_EmptyObject``; other non-2xx →
    ``FrameUnreadable``; httpx timeout → ``FrameTimeout``."""
    headers = {"Range": range_header} if range_header else None
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                raise _EmptyObject("store answered 416")
            if resp.status_code not in (200, 206):
                raise FrameUnreadable(f"store answered HTTP {resp.status_code}")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_FULL_ZIP_BYTES:
                    raise FrameUnreadable("ZIP larger than the read limit")
                chunks.append(chunk)
            reply = _Reply(resp.status_code, resp.headers.get("content-range"), b"".join(chunks))
    except httpx.TimeoutException as exc:
        raise FrameTimeout(f"storage read timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise FrameUnreadable(f"storage read failed ({type(exc).__name__})") from exc
    if not reply.content:
        raise _EmptyObject(f"store answered HTTP {reply.status} with 0 bytes")
    return reply


async def _fetch_index(client: httpx.AsyncClient, url: str) -> tuple[ZipIndex, bytes | None]:
    """(index, full body if the store sent the whole ZIP, else None)."""
    tail = await _range_get(client, url, f"bytes=-{TAIL_BYTES}")
    if tail.status == 200:  # store ignored Range: the body is the ZIP
        return parse_index(tail.content), tail.content
    cr = _content_range(tail)
    if cr is None:
        raise FrameUnreadable("206 without a usable Content-Range")
    total = cr[1]
    if total <= 0:
        raise _EmptyObject("store reports a 0-byte object")
    sparse = _Sparse(total)
    sparse.add(cr[0], tail.content)
    known_start = cr[0]
    for _ in range(3):
        try:
            return _index_from_zipfile(sparse, total), None
        except _Missing as missing:
            # Central directory starts before the tail: fetch [pos, known_start).
            if missing.pos >= known_start or known_start - missing.pos > MAX_CENTRAL_DIR_BYTES:
                break
            part = await _range_get(client, url, f"bytes={missing.pos}-{known_start - 1}")
            if part.status == 200:
                return parse_index(part.content), part.content
            sparse.add(missing.pos, part.content)
            known_start = missing.pos
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError) as exc:
            logger.info("Ranged central-directory parse failed (%s); reading the whole ZIP", exc)
            break
    full = await _range_get(client, url, None)
    return parse_index(full.content), full.content


async def _fetch_entry(
    client: httpx.AsyncClient, url: str, index: ZipIndex, entry: ZipEntryInfo
) -> bytes:
    if entry.file_size > MAX_ENTRY_BYTES or entry.compress_size > MAX_ENTRY_BYTES:
        raise FrameUnreadable(f"entry {entry.name!r} larger than the frame limit")
    start = entry.header_offset
    want = (
        _LOCAL_HEADER.size
        + len(entry.name.encode("utf-8"))
        + entry.compress_size
        + LOCAL_HEADER_SLACK
    )
    local = b""
    for _ in range(2):
        end = min(index.total_size, start + want) - 1
        resp = await _range_get(client, url, f"bytes={start + len(local)}-{end}")
        if resp.status == 200:  # store ignored Range: the body is the ZIP
            return _entry_from_full(resp.content, entry)
        cr = _content_range(resp)
        if cr is None or cr[0] != start + len(local):
            raise FrameUnreadable("206 with an unexpected Content-Range")
        local += resp.content
        result = _decode_entry(local, entry)
        if not isinstance(result, int):
            return result
        want = result
    raise FrameUnreadable(f"could not read the local data of {entry.name!r}")


def _with_cache_buster(url: str) -> str | None:
    """``url?nc=<token>`` for unsigned URLs; None when the URL has a query
    (presigned or otherwise — appending would break or change it)."""
    if urlparse(url).query or is_presigned_url(url):
        return None
    return f"{url}?nc={uuid.uuid4().hex}"


async def _read_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    zip_entry: str | None,
    image_name: str | None,
    index: ZipIndex | None,
) -> tuple[bytes, ZipEntryInfo, ZipIndex]:
    full: bytes | None = None
    if index is None:
        index, full = await _fetch_index(client, url)
    entry = select_entry(index.entries, zip_entry=zip_entry, image_name=image_name)
    if full is not None:
        if entry.file_size > MAX_ENTRY_BYTES:
            raise FrameUnreadable(f"entry {entry.name!r} larger than the frame limit")
        data = _entry_from_full(full, entry)
    else:
        data = await _fetch_entry(client, url, index, entry)
    if not data:
        raise FrameUnreadable(f"entry {entry.name!r} is empty")
    return data, entry, index


async def read_zip_entry(
    client: httpx.AsyncClient,
    fetch_url: str,
    *,
    zip_entry: str | None,
    image_name: str | None,
    index_cache: ZipIndexCache | None = None,
    cache_key: str | None = None,
) -> FrameData:
    """Read one entry of the ZIP at ``fetch_url`` (already public or presigned).

    ``cache_key`` (the STORED zip_url) keys ``index_cache``; it defaults to
    ``fetch_url``. Failures and empty entries are never cached.
    """
    key = cache_key or fetch_url
    cached = index_cache.get(key) if index_cache is not None else None
    try:
        try:
            data, entry, index = await _read_once(
                client, fetch_url, zip_entry=zip_entry, image_name=image_name, index=cached
            )
        except _EmptyObject as exc:
            fresh = _with_cache_buster(fetch_url)
            if fresh is None:
                raise FrameUnreadable(f"empty object ({exc}); no retry on a signed URL") from exc
            logger.warning("Empty ZIP object (%s); re-reading once past the cache", exc)
            try:
                data, entry, index = await _read_once(
                    client, fresh, zip_entry=zip_entry, image_name=image_name, index=None
                )
            except _EmptyObject as again:
                raise FrameUnreadable(f"empty object also past the cache ({again})") from again
    except FrameReadError:
        if index_cache is not None:
            index_cache.discard(key)
        raise
    if index_cache is not None and cached is None:
        index_cache.put(key, index)
    return FrameData(data=data, entry_name=entry.name, content_type=content_type_for(entry.name))


async def read_frame(
    client: httpx.AsyncClient,
    zip_url: str,
    *,
    zip_entry: str | None,
    image_name: str | None,
    storage_service_url: str,
    index_cache: ZipIndexCache | None = None,
    timeout_s: float | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> FrameData:
    """Presign when private, then read one entry — all under ``timeout_s``.

    Raises ``FrameTimeout`` / ``FrameUnreadable`` / ``FrameEntryNotFound``.
    """

    async def _go() -> FrameData:
        fetch_url = await resolve_fetch_url(
            client,
            zip_url,
            storage_service_url=storage_service_url,
            allowed_hosts=allowed_hosts,
        )
        return await read_zip_entry(
            client,
            fetch_url,
            zip_entry=zip_entry,
            image_name=image_name,
            index_cache=index_cache,
            cache_key=zip_url,
        )

    if timeout_s is None:
        return await _go()
    try:
        async with asyncio.timeout(timeout_s):
            return await _go()
    except TimeoutError as exc:
        raise FrameTimeout(f"frame read exceeded {timeout_s}s") from exc


__all__ = [
    "FrameData",
    "FrameEntryNotFound",
    "FrameReadError",
    "FrameTimeout",
    "FrameUnreadable",
    "PresignError",
    "ZipEntryInfo",
    "ZipIndex",
    "ZipIndexCache",
    "content_type_for",
    "is_presigned_url",
    "is_public_storage_url",
    "object_name_from_url",
    "parse_index",
    "presign_get_url",
    "read_frame",
    "read_zip_entry",
    "resolve_fetch_url",
    "select_entry",
]
