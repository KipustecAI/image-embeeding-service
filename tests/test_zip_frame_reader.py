"""Unit tests for src/services/zip_frame_reader.py (frames desde ZIP).

No network: a real in-memory ZIP (built with ``zipfile``) is served by an
``httpx.MockTransport`` that honours ``Range`` like storage.lookia.mx (206 +
Content-Range), and can be told to ignore Range (200), answer 416 / 0 bytes
once (stale Cloudflare object) or presign through a fake storage-service.
"""

import io
import json
import re
import zipfile

import httpx
import pytest

from src.services.zip_frame_reader import (
    TAIL_BYTES,
    FrameEntryNotFound,
    FrameTimeout,
    FrameUnreadable,
    PresignError,
    ZipIndexCache,
    check_allowed_host,
    content_type_for,
    is_public_storage_url,
    object_name_from_url,
    presign_get_url,
    read_frame,
    read_zip_entry,
)

PUBLIC_URL = "https://storage.lookia.mx/lucam-assets/public/evidences/ev1.zip"
PRIVATE_URL = "https://storage.lookia.mx/lucam-assets/evidences/2026/ev1.zip"
SIGNED_URL = (
    "https://storage.lookia.mx/lucam-assets/evidences/2026/ev1.zip"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
)
STORAGE = "http://storage-service:8006"

ROOT = b"\xff\xd8root-frame" * 50
LOW = b"\xff\xd8low-res-frame" * 40
CROPPED = b"\xff\xd8cropped-frame" * 30


def build_zip(entries: list[tuple[str, bytes, int]], **kwargs) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, compression in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = compression
            with zf.open(info, "w", **kwargs) as fh:
                fh.write(data)
    return buf.getvalue()


EVIDENCE_ZIP = build_zip(
    [
        ("__MACOSX/._20261021-001529_457445.jpg", b"resource-fork", zipfile.ZIP_STORED),
        ("20261021-001529_457445.jpg", ROOT, zipfile.ZIP_STORED),
        ("low_res/20261021-001529_457445.jpg", LOW, zipfile.ZIP_DEFLATED),
        ("cropped/20261021-001529_457445.jpg", CROPPED, zipfile.ZIP_STORED),
        ("track.json", b'{"a": 1}', zipfile.ZIP_DEFLATED),
        ("frame.png", b"\x89PNG-bytes", zipfile.ZIP_STORED),
    ]
)


class FakeStore:
    """Serves ``objects`` (url-without-query → bytes) with Range support."""

    def __init__(self, objects: dict[str, bytes], *, ignore_range: bool = False):
        self.objects = objects
        self.ignore_range = ignore_range
        self.requests: list[httpx.Request] = []
        self.stale_responses: list[int] = []  # statuses to answer first (416 or 200-empty)
        self.presign_calls: list[dict] = []
        self.presign_reply: tuple[int, dict] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/internal/presigned-url":
            body = json.loads(request.content)
            self.presign_calls.append({"headers": dict(request.headers), "json": body})
            if self.presign_reply is not None:
                status, payload = self.presign_reply
                return httpx.Response(status, json=payload)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "url": f"https://storage.lookia.mx/lucam-assets/{body['object_name']}"
                    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=sig",
                },
            )
        if self.stale_responses:
            status = self.stale_responses.pop(0)
            return httpx.Response(status, content=b"")
        key = str(request.url.copy_with(query=None))
        data = self.objects.get(key)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if not rng or self.ignore_range:
            return httpx.Response(200, content=data)
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng)
        assert m, rng
        total = len(data)
        if m.group(1) == "":
            start, end = max(0, total - int(m.group(2))), total - 1
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
        if start >= total:
            return httpx.Response(416)
        return httpx.Response(
            206,
            content=data[start : end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{total}"},
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def get_urls(self) -> list[str]:
        return [str(r.url) for r in self.requests if r.method == "GET"]


# ── Entry reads ──────────────────────────────────────────────────────────────


async def test_reads_stored_entry_by_exact_zip_entry_with_ranges():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        frame = await read_zip_entry(
            client,
            PUBLIC_URL,
            zip_entry="cropped/20261021-001529_457445.jpg",
            image_name="20261021-001529_457445.jpg",
        )
    assert frame.data == CROPPED
    assert frame.entry_name == "cropped/20261021-001529_457445.jpg"
    assert frame.content_type == "image/jpeg"
    ranges = [r.headers.get("range") for r in store.requests]
    assert ranges[0] == f"bytes=-{TAIL_BYTES}"
    assert all(r is not None for r in ranges)  # never the whole ZIP


async def test_reads_deflated_entry():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        frame = await read_zip_entry(
            client,
            PUBLIC_URL,
            zip_entry="low_res/20261021-001529_457445.jpg",
            image_name="20261021-001529_457445.jpg",
        )
    assert frame.data == LOW


async def test_duplicate_basenames_resolved_by_zip_entry():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        got = {}
        for entry in (
            "20261021-001529_457445.jpg",
            "low_res/20261021-001529_457445.jpg",
            "cropped/20261021-001529_457445.jpg",
        ):
            frame = await read_zip_entry(client, PUBLIC_URL, zip_entry=entry, image_name=None)
            got[entry] = frame.data
    assert got == {
        "20261021-001529_457445.jpg": ROOT,
        "low_res/20261021-001529_457445.jpg": LOW,
        "cropped/20261021-001529_457445.jpg": CROPPED,
    }


async def test_basename_fallback_skips_macosx_and_takes_first_match():
    # No zip_entry (older compute): basename == image_name; the __MACOSX twin
    # ("._<name>") and the AppleDouble dir are never candidates.
    zip_bytes = build_zip(
        [
            ("__MACOSX/a.jpg", b"apple-double", zipfile.ZIP_STORED),
            ("a.jpg", ROOT, zipfile.ZIP_STORED),
            ("cropped/a.jpg", CROPPED, zipfile.ZIP_STORED),
        ]
    )
    store = FakeStore({PUBLIC_URL: zip_bytes})
    async with store.client() as client:
        frame = await read_zip_entry(client, PUBLIC_URL, zip_entry=None, image_name="a.jpg")
    assert frame.data == ROOT
    assert frame.entry_name == "a.jpg"


async def test_basename_fallback_finds_nested_entry():
    zip_bytes = build_zip([("cropped/only.jpg", CROPPED, zipfile.ZIP_STORED)])
    store = FakeStore({PUBLIC_URL: zip_bytes})
    async with store.client() as client:
        frame = await read_zip_entry(client, PUBLIC_URL, zip_entry=None, image_name="only.jpg")
    assert frame.data == CROPPED


async def test_macosx_entry_never_served_even_by_exact_name():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        with pytest.raises(FrameEntryNotFound):
            await read_zip_entry(
                client,
                PUBLIC_URL,
                zip_entry="__MACOSX/._20261021-001529_457445.jpg",
                image_name=None,
            )


async def test_missing_entry_raises_entry_not_found():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        with pytest.raises(FrameEntryNotFound):
            await read_zip_entry(client, PUBLIC_URL, zip_entry=None, image_name="nope.jpg")
        # A present-but-unknown zip_entry does NOT fall back to the basename.
        with pytest.raises(FrameEntryNotFound):
            await read_zip_entry(
                client,
                PUBLIC_URL,
                zip_entry="other/20261021-001529_457445.jpg",
                image_name="20261021-001529_457445.jpg",
            )
    assert issubclass(FrameEntryNotFound, FrameUnreadable)


async def test_200_instead_of_206_uses_full_body():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP}, ignore_range=True)
    async with store.client() as client:
        frame = await read_zip_entry(
            client, PUBLIC_URL, zip_entry="low_res/20261021-001529_457445.jpg", image_name=None
        )
    assert frame.data == LOW
    assert len(store.requests) == 1  # the 200 body already is the ZIP


async def test_zip64_local_headers_are_read():
    zip_bytes = build_zip([("cropped/z.jpg", CROPPED, zipfile.ZIP_DEFLATED)], force_zip64=True)
    store = FakeStore({PUBLIC_URL: zip_bytes})
    async with store.client() as client:
        frame = await read_zip_entry(client, PUBLIC_URL, zip_entry="cropped/z.jpg", image_name=None)
    assert frame.data == CROPPED


async def test_central_directory_bigger_than_tail_is_fetched_by_range():
    entries = [(f"pad/{'x' * 60}_{i:05d}.jpg", b"p", zipfile.ZIP_STORED) for i in range(1500)]
    entries.append(("cropped/target.jpg", CROPPED, zipfile.ZIP_STORED))
    zip_bytes = build_zip(entries)
    store = FakeStore({PUBLIC_URL: zip_bytes})
    async with store.client() as client:
        frame = await read_zip_entry(
            client, PUBLIC_URL, zip_entry="cropped/target.jpg", image_name=None
        )
    assert frame.data == CROPPED
    assert all(r.headers.get("range") for r in store.requests)  # no full download


async def test_png_content_type():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        frame = await read_zip_entry(client, PUBLIC_URL, zip_entry="frame.png", image_name=None)
    assert frame.content_type == "image/png"
    assert content_type_for("x.WEBP") == "image/webp"
    assert content_type_for("x.bmp") == "image/bmp"
    assert content_type_for("x") == "image/jpeg"


# ── Stale empty object (416 / 0 bytes) ───────────────────────────────────────


@pytest.mark.parametrize("stale_status", [416, 200])
async def test_stale_empty_object_retries_once_with_nc_on_unsigned_url(stale_status):
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    store.stale_responses = [stale_status]
    async with store.client() as client:
        frame = await read_zip_entry(
            client, PUBLIC_URL, zip_entry="cropped/20261021-001529_457445.jpg", image_name=None
        )
    assert frame.data == CROPPED
    urls = store.get_urls()
    assert urls[0] == PUBLIC_URL
    assert re.fullmatch(re.escape(PUBLIC_URL) + r"\?nc=[0-9a-f]{32}", urls[1])
    # Every later read of that attempt uses the same cache-busted URL.
    assert all(u == urls[1] for u in urls[1:])


async def test_stale_empty_object_twice_is_unreadable():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    store.stale_responses = [416, 416]
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_zip_entry(client, PUBLIC_URL, zip_entry="frame.png", image_name=None)
    assert len(store.get_urls()) == 2  # exactly one retry


async def test_no_nc_retry_on_presigned_url():
    store = FakeStore({PRIVATE_URL: EVIDENCE_ZIP})
    store.stale_responses = [416]
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_zip_entry(client, SIGNED_URL, zip_entry="frame.png", image_name=None)
    urls = store.get_urls()
    assert urls == [SIGNED_URL]
    assert "nc=" not in urls[0]


async def test_storage_error_is_unreadable_and_timeout_is_typed():
    store = FakeStore({})
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_zip_entry(client, PUBLIC_URL, zip_entry="frame.png", image_name=None)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(FrameTimeout):
            await read_zip_entry(client, PUBLIC_URL, zip_entry="frame.png", image_name=None)


async def test_bad_zip_is_unreadable():
    store = FakeStore({PUBLIC_URL: b"not a zip at all" * 10})
    async with store.client() as client:
        with pytest.raises(FrameUnreadable) as exc_info:
            await read_zip_entry(client, PUBLIC_URL, zip_entry="frame.png", image_name=None)
    assert not isinstance(exc_info.value, FrameEntryNotFound)


# ── Index cache ──────────────────────────────────────────────────────────────


async def test_index_cache_skips_tail_and_drops_on_failure():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    cache = ZipIndexCache(max_entries=4)
    async with store.client() as client:
        await read_zip_entry(
            client, PUBLIC_URL, zip_entry="frame.png", image_name=None, index_cache=cache
        )
        assert len(cache) == 1
        store.requests.clear()
        frame = await read_zip_entry(
            client, PUBLIC_URL, zip_entry="track.json", image_name=None, index_cache=cache
        )
        assert frame.data == b'{"a": 1}'
        assert len(store.requests) == 1  # only the entry range, no tail
        with pytest.raises(FrameEntryNotFound):
            await read_zip_entry(
                client, PUBLIC_URL, zip_entry="nope.jpg", image_name=None, index_cache=cache
            )
    assert len(cache) == 0  # a failure never stays cached


def test_index_cache_is_lru_bounded():
    from src.services.zip_frame_reader import ZipEntryInfo, ZipIndex

    entry = ZipEntryInfo("a.jpg", 0, 1, 1, 0, 0, 0)
    cache = ZipIndexCache(max_entries=2)
    for key in ("a", "b"):
        cache.put(key, ZipIndex(10, (entry,)))
    cache.get("a")
    cache.put("c", ZipIndex(10, (entry,)))
    assert cache.get("b") is None and cache.get("a") and cache.get("c")
    cache.put("empty", ZipIndex(0, ()))
    assert cache.get("empty") is None


# ── Presign ──────────────────────────────────────────────────────────────────


def test_public_detection_and_object_name_derivation():
    assert is_public_storage_url(PUBLIC_URL)
    assert not is_public_storage_url(PRIVATE_URL)
    assert object_name_from_url(PRIVATE_URL) == "evidences/2026/ev1.zip"
    assert object_name_from_url(SIGNED_URL) == "evidences/2026/ev1.zip"
    assert object_name_from_url("https://storage.lookia.mx/lucam-assets/a%20b.zip") == "a b.zip"
    assert object_name_from_url("https://storage.lookia.mx/lucam-assets//x/y.zip") == "x/y.zip"
    assert object_name_from_url("https://other.example/bucket/x.zip") is None
    assert object_name_from_url("https://storage.lookia.mx/lucam-assets/") is None


async def test_presign_request_shape():
    store = FakeStore({})
    async with store.client() as client:
        url = await presign_get_url(client, PRIVATE_URL, storage_service_url=STORAGE + "/")
    assert url.startswith("https://storage.lookia.mx/lucam-assets/evidences/2026/ev1.zip?")
    (call,) = store.presign_calls
    req = store.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"{STORAGE}/internal/presigned-url"
    assert call["headers"]["x-user-id"] == "image-embeeding-service"
    assert call["headers"]["x-user-role"] == "service"
    assert call["json"] == {
        "bucket_name": "lucam-assets",
        "object_name": "evidences/2026/ev1.zip",
        "method": "GET",
        "expiry_hours": 1,
    }


@pytest.mark.parametrize(
    "reply",
    [(500, {"detail": "boom"}), (200, {"success": False}), (200, {"success": True, "url": ""})],
)
async def test_presign_failure_raises(reply):
    store = FakeStore({})
    store.presign_reply = reply
    async with store.client() as client:
        with pytest.raises(PresignError):
            await presign_get_url(client, PRIVATE_URL, storage_service_url=STORAGE)
    assert issubclass(PresignError, FrameUnreadable)


async def test_read_frame_presigns_private_and_never_appends_to_signed_url():
    store = FakeStore({PRIVATE_URL: EVIDENCE_ZIP})
    cache = ZipIndexCache()
    async with store.client() as client:
        frame = await read_frame(
            client,
            PRIVATE_URL,
            zip_entry="cropped/20261021-001529_457445.jpg",
            image_name=None,
            storage_service_url=STORAGE,
            index_cache=cache,
            timeout_s=5,
        )
    assert frame.data == CROPPED
    assert len(store.presign_calls) == 1
    gets = store.get_urls()
    assert gets and all(u.endswith("X-Amz-Signature=sig") for u in gets)
    assert cache.get(PRIVATE_URL) is not None  # keyed by the STORED url


async def test_read_frame_public_url_skips_presign():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        frame = await read_frame(
            client,
            PUBLIC_URL,
            zip_entry=None,
            image_name="20261021-001529_457445.jpg",
            storage_service_url=STORAGE,
        )
    assert frame.data == ROOT
    assert store.presign_calls == []


async def test_read_frame_deadline_maps_to_timeout():
    import asyncio

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=EVIDENCE_ZIP)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(FrameTimeout):
            await read_frame(
                client,
                PUBLIC_URL,
                zip_entry="frame.png",
                image_name=None,
                storage_service_url=STORAGE,
                timeout_s=0.05,
            )


# ── Must-fixes from the audit ────────────────────────────────────────────────


def _shrink_declared_size(zip_bytes: bytes, new_size: int) -> bytes:
    """Lie about the uncompressed size in both the local and central headers."""
    buf = bytearray(zip_bytes)
    local = buf.index(b"PK\x03\x04")
    buf[local + 22 : local + 26] = new_size.to_bytes(4, "little")
    central = buf.index(b"PK\x01\x02")
    buf[central + 24 : central + 28] = new_size.to_bytes(4, "little")
    return bytes(buf)


async def test_deflate_bomb_is_rejected_without_inflating_past_declared_size():
    bomb = _shrink_declared_size(
        build_zip([("bomb.jpg", b"\0" * 20_000_000, zipfile.ZIP_DEFLATED)]), 10
    )
    store = FakeStore({PUBLIC_URL: bomb})
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_zip_entry(client, PUBLIC_URL, zip_entry="bomb.jpg", image_name="bomb.jpg")


def test_public_detection_is_a_path_prefix_not_a_substring():
    assert is_public_storage_url(PUBLIC_URL)
    assert not is_public_storage_url("https://storage.lookia.mx/x/lucam-assets/public/a.zip")


def test_check_allowed_host():
    allowed = frozenset({"storage.lookia.mx"})
    check_allowed_host(PUBLIC_URL, allowed)
    check_allowed_host("https://evil.example/x.zip", None)  # None = no check
    with pytest.raises(FrameUnreadable):
        check_allowed_host("https://evil.example/lucam-assets/public/x.zip", allowed)


async def test_read_frame_refuses_foreign_zip_host_without_any_request():
    store = FakeStore({})
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_frame(
                client,
                "http://169.254.169.254/lucam-assets/public/x.zip",
                zip_entry="a.jpg",
                image_name="a.jpg",
                storage_service_url=STORAGE,
                allowed_hosts=frozenset({"storage.lookia.mx", "storage-service"}),
            )
    assert store.requests == []


async def test_read_frame_refuses_presigned_url_on_foreign_host():
    store = FakeStore({PRIVATE_URL: EVIDENCE_ZIP})
    store.presign_reply = (
        200,
        {"success": True, "url": "https://evil.example/x.zip?X-Amz-Signature=s"},
    )
    async with store.client() as client:
        with pytest.raises(FrameUnreadable):
            await read_frame(
                client,
                PRIVATE_URL,
                zip_entry="cropped/20261021-001529_457445.jpg",
                image_name=None,
                storage_service_url=STORAGE,
                allowed_hosts=frozenset({"storage.lookia.mx", "storage-service"}),
            )
    assert store.get_urls() == []
