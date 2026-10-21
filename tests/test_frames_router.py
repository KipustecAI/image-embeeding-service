"""Route tests for the public frame endpoint GET/HEAD /api/v1/frames/{point_id}.

No live infra: the DB lookup and the FrameService are dependency-overridden.
The service gets an ``httpx.MockTransport`` client over ``FakeStore`` (Range-aware,
real in-memory ZIPs, fake storage-service presign) from the reader tests.

TestClient is constructed WITHOUT ``with`` on purpose — entering it would run the
app lifespan (Postgres/Qdrant/Redis startup).
"""

import asyncio
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

import src.api.v1.routers.frames as frames_mod
from src.api.v1.routers.frames import (
    CachedFrame,
    FrameBytesCache,
    FrameRef,
    FrameService,
)
from src.main import app
from tests.test_zip_frame_reader import (
    CROPPED,
    EVIDENCE_ZIP,
    PRIVATE_URL,
    PUBLIC_URL,
    ROOT,
    STORAGE,
    FakeStore,
    build_zip,
)

POINT = "16d1a741-1111-4222-8333-944455556666"
OLD_POINT = "26d1a741-1111-4222-8333-944455556666"
PRIVATE_POINT = "36d1a741-1111-4222-8333-944455556666"
PNG_POINT = "46d1a741-1111-4222-8333-944455556666"
BAD_POINT = "56d1a741-1111-4222-8333-944455556666"
BAD_ZIP_URL = "https://storage.lookia.mx/lucam-assets/public/evidences/bad.zip"

ROWS = {
    POINT: FrameRef(POINT, PUBLIC_URL, "cropped/20261021-001529_457445.jpg", "x.jpg"),
    OLD_POINT: FrameRef(OLD_POINT, None, None, None),
    PRIVATE_POINT: FrameRef(PRIVATE_POINT, PRIVATE_URL, None, "20261021-001529_457445.jpg"),
    PNG_POINT: FrameRef(PNG_POINT, PUBLIC_URL, "frame.png", "frame.png"),
    BAD_POINT: FrameRef(BAD_POINT, BAD_ZIP_URL, None, "a.jpg"),
}


class FakeLookup:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, point_id: str):
        self.calls.append(point_id)
        return ROWS.get(point_id)


def make_service(store: FakeStore, **kwargs) -> FrameService:
    kwargs.setdefault("timeout_s", 5.0)
    return FrameService(storage_service_url=STORAGE, client=store.client(), **kwargs)


@pytest.fixture
def store():
    return FakeStore({PUBLIC_URL: EVIDENCE_ZIP, PRIVATE_URL: EVIDENCE_ZIP, BAD_ZIP_URL: b"no zip"})


@pytest.fixture
def lookup():
    return FakeLookup()


@pytest.fixture
def wire(lookup):
    """Install a service + the fake lookup; returns a TestClient."""

    def _wire(service: FrameService) -> TestClient:
        app.dependency_overrides[frames_mod.get_frame_lookup] = lambda: lookup
        app.dependency_overrides[frames_mod.get_frame_service] = lambda: service
        return TestClient(app)

    yield _wire
    app.dependency_overrides.clear()


def storage_gets(store: FakeStore) -> list[str]:
    return store.get_urls()


# ── 404s ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "16d1a741-1111-4222-8333-94445555666Z"])
def test_malformed_id_is_404_without_lookup(wire, store, lookup, bad):
    r = wire(make_service(store)).get(f"/api/v1/frames/{bad}")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert lookup.calls == []
    assert store.requests == []


def test_unknown_id_is_404(wire, store):
    r = wire(make_service(store)).get("/api/v1/frames/99999999-1111-4222-8333-944455556666")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert store.requests == []


def test_old_row_without_zip_url_is_404(wire, store):
    r = wire(make_service(store)).get(f"/api/v1/frames/{OLD_POINT}")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert store.requests == []


def test_lookup_error_is_502_no_store(wire, store):
    async def boom(point_id):
        raise RuntimeError("db down")

    client = wire(make_service(store))
    app.dependency_overrides[frames_mod.get_frame_lookup] = lambda: boom
    r = client.get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 502
    assert r.headers["cache-control"] == "no-store"


# ── Happy path ───────────────────────────────────────────────────────────────


def test_happy_path_200_bytes_and_headers(wire, store, lookup):
    r = wire(make_service(store)).get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == CROPPED
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["etag"] == f'"{POINT}"'
    assert r.headers["content-length"] == str(len(CROPPED))
    # Public URL: no presign, ranged reads only (tail + entry).
    assert all("/internal/presigned-url" not in u for u in storage_gets(store))
    assert all(req.headers.get("range") for req in store.requests)
    assert lookup.calls == [POINT]


def test_uppercase_id_is_canonicalized(wire, store, lookup):
    r = wire(make_service(store)).get(f"/api/v1/frames/{POINT.upper()}")
    assert r.status_code == 200
    assert lookup.calls == [POINT]
    assert r.headers["etag"] == f'"{POINT}"'


def test_png_content_type(wire, store):
    r = wire(make_service(store)).get(f"/api/v1/frames/{PNG_POINT}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG-bytes"


def test_if_none_match_304_without_storage_read(wire, store):
    client = wire(make_service(store))
    r = client.get(f"/api/v1/frames/{POINT}", headers={"If-None-Match": f'"{POINT}"'})
    assert r.status_code == 304
    assert r.headers["etag"] == f'"{POINT}"'
    assert r.content == b""
    assert store.requests == []
    # Weak / list forms match too.
    r = client.get(f"/api/v1/frames/{POINT}", headers={"If-None-Match": f'"other", W/"{POINT}"'})
    assert r.status_code == 304
    assert store.requests == []


def test_if_none_match_mismatch_serves_200(wire, store):
    r = wire(make_service(store)).get(
        f"/api/v1/frames/{POINT}", headers={"If-None-Match": '"something-else"'}
    )
    assert r.status_code == 200
    assert r.content == CROPPED


def test_head_same_headers_no_body(wire, store):
    client = wire(make_service(store))
    r = client.head(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["etag"] == f'"{POINT}"'
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-length"] == str(len(CROPPED))
    # HEAD resolves the frame (documented choice), so the GET after it is a cache hit.
    reads = len(store.requests)
    assert reads > 0
    g = client.get(f"/api/v1/frames/{POINT}")
    assert g.content == CROPPED
    assert len(store.requests) == reads


def test_head_unknown_is_404(wire, store):
    r = wire(make_service(store)).head(f"/api/v1/frames/{OLD_POINT}")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"


# ── Caches ───────────────────────────────────────────────────────────────────


def test_bytes_cache_hit_avoids_second_storage_read(wire, store):
    client = wire(make_service(store))
    assert client.get(f"/api/v1/frames/{POINT}").status_code == 200
    reads = len(store.requests)
    r = client.get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == CROPPED
    assert len(store.requests) == reads


def test_index_cache_shared_across_points_of_same_zip(wire, store):
    client = wire(make_service(store))
    assert client.get(f"/api/v1/frames/{POINT}").status_code == 200
    before = len(store.requests)
    assert client.get(f"/api/v1/frames/{PNG_POINT}").status_code == 200
    # Same zip_url: central directory cached → only the entry range is read.
    assert len(store.requests) == before + 1


def test_failure_is_not_cached(wire, store):
    service = make_service(store)
    client = wire(service)
    store.objects.pop(PUBLIC_URL)
    r = client.get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 502
    assert r.headers["cache-control"] == "no-store"
    assert len(service.bytes_cache) == 0
    assert len(service.index_cache) == 0
    # Object appears → the next request reads storage again and succeeds.
    store.objects[PUBLIC_URL] = EVIDENCE_ZIP
    r = client.get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == CROPPED


def test_bytes_cache_is_bounded_by_total_bytes():
    cache = FrameBytesCache(max_bytes=10)
    cache.put("a", CachedFrame(b"12345", "image/jpeg"))
    cache.put("b", CachedFrame(b"12345", "image/jpeg"))
    cache.put("c", CachedFrame(b"123", "image/jpeg"))  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") is not None and cache.get("c") is not None
    assert cache.total_bytes == 8
    cache.put("big", CachedFrame(b"x" * 11, "image/jpeg"))  # bigger than budget
    cache.put("empty", CachedFrame(b"", "image/jpeg"))
    assert cache.get("big") is None and cache.get("empty") is None
    assert cache.total_bytes == 8


# ── Failures ─────────────────────────────────────────────────────────────────


def test_timeout_is_504_no_store(wire, store):
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return store.handler(request)

    service = FrameService(
        storage_service_url=STORAGE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        timeout_s=0.05,
    )
    r = wire(service).get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 504
    assert r.headers["cache-control"] == "no-store"
    assert len(service.bytes_cache) == 0


def test_bad_zip_is_502(wire, store):
    r = wire(make_service(store)).get(f"/api/v1/frames/{BAD_POINT}")
    assert r.status_code == 502
    assert r.headers["cache-control"] == "no-store"


def test_missing_entry_is_502(wire, store):
    store.objects[PUBLIC_URL] = build_zip([("other.jpg", b"x", zipfile.ZIP_STORED)])
    r = wire(make_service(store)).get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 502


def test_private_url_goes_through_presign(wire, store):
    r = wire(make_service(store)).get(f"/api/v1/frames/{PRIVATE_POINT}")
    assert r.status_code == 200
    assert r.content == ROOT  # basename fallback, __MACOSX skipped
    assert len(store.presign_calls) == 1
    call = store.presign_calls[0]
    assert call["json"]["object_name"] == "evidences/2026/ev1.zip"
    assert call["json"]["bucket_name"] == "lucam-assets"
    assert call["headers"]["x-user-id"] == "image-embeeding-service"
    assert call["headers"]["x-user-role"] == "service"
    # Every storage GET went to the signed URL, untouched.
    for url in storage_gets(store):
        assert "X-Amz-Signature=sig" in url
        assert "nc=" not in url


def test_presign_failure_is_502(wire, store):
    store.presign_reply = (500, {"success": False})
    r = wire(make_service(store)).get(f"/api/v1/frames/{PRIVATE_POINT}")
    assert r.status_code == 502
    assert r.headers["cache-control"] == "no-store"


# ── Public: no auth ──────────────────────────────────────────────────────────


def test_endpoint_needs_no_user_or_api_key_headers(wire, store):
    """No X-User-* / X-API-Key on the request, and no auth dependency on the route."""
    r = wire(make_service(store)).get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    route = next(rt for rt in app.routes if getattr(rt, "path", "") == "/api/v1/frames/{point_id}")
    calls = {d.call for d in route.dependant.dependencies}
    assert calls == {frames_mod.get_frame_lookup, frames_mod.get_frame_service}
    assert {"GET", "HEAD"} <= set(route.methods)


# ── Optional Redis byte cache ────────────────────────────────────────────────


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.data: dict[str, bytes] = {}
        self.fail = fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("NOPERM")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("NOPERM")
        self.data[key] = value

    async def aclose(self):
        pass


def test_redis_cache_written_and_read(wire, store):
    redis = FakeRedis()
    client = wire(make_service(store, redis=redis))
    assert client.get(f"/api/v1/frames/{POINT}").status_code == 200
    assert f"frames:bytes:v1:{POINT}" in redis.data
    # A fresh process (empty in-memory cache) is served from Redis, no storage read.
    store2 = FakeStore({})
    client2 = wire(make_service(store2, redis=redis))
    r = client2.get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == CROPPED
    assert r.headers["content-type"] == "image/jpeg"
    assert store2.requests == []


def test_redis_errors_are_a_cache_miss(wire, store):
    r = wire(make_service(store, redis=FakeRedis(fail=True))).get(f"/api/v1/frames/{POINT}")
    assert r.status_code == 200
    assert r.content == CROPPED


def test_default_service_has_no_redis(monkeypatch):
    monkeypatch.setattr(frames_mod, "_service", None)
    service = frames_mod.get_frame_service()
    try:
        assert service.redis is None
    finally:
        monkeypatch.setattr(frames_mod, "_service", None)
