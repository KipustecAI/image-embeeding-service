"""Public frame endpoint: GET + HEAD /api/v1/frames/{point_id} (FZ-04).

Frames desde ZIP (Lookia Platform#frames-desde-zip): with FRAME_STORAGE_MODE=zip_ref
the ingest stores ``zip_url`` / ``zip_entry`` / ``image_name`` per vector instead of
uploading each frame, and ``image_url`` points here. This router resolves one
frame on demand: DB lookup by ``evidence_embeddings.qdrant_point_id`` → presign
(private) or direct (public) → HTTP Range read of the one entry
(``src/services/zip_frame_reader.py``).

Contract invariants enforced here:

* **Public.** No ``get_user_context`` / X-User-* / X-API-Key dependency: the
  gateway strips those headers on this route. We validate ``point_id`` is a UUID
  ourselves; malformed, unknown, or an old row without ``zip_url`` → **404**.
* **Caches.** Parsed central directory per stored zip_url (in-process LRU,
  FRAMES_INDEX_CACHE_SIZE); frame bytes per point_id (in-process LRU bounded by
  total bytes, FRAMES_BYTES_CACHE_MAX_MB); optional Redis byte cache only when
  FRAMES_BYTES_CACHE_REDIS_URL is set. Failures, empty bodies and 0-byte
  entries are NEVER cached.
* **Concurrency + deadline.** A global ``asyncio.Semaphore``
  (FRAMES_MAX_CONCURRENT_FETCHES) around storage reads; FRAMES_FETCH_TIMEOUT_S
  covers waiting for the semaphore + presign + reads. Timeout → **504**; storage
  error / bad ZIP / missing entry / presign failure → **502**. Every error
  response carries ``Cache-Control: no-store``.
* **Success.** 200 with Content-Type from the entry extension,
  ``Cache-Control: public, max-age=31536000, immutable`` and ``ETag: "<point_id>"``
  (a point's frame never changes). ``If-None-Match`` match → **304** with no
  storage read (the DB lookup still runs so an unknown id stays 404).
* **HEAD** (documented choice): same lookup AND frame resolution as GET (the
  bytes then sit in the cache for the GET that usually follows), so a HEAD 200
  means the frame is really readable and Content-Length is exact. Body empty.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ....db.models.evidence_embedding import EvidenceEmbeddingRecord
from ....infrastructure.config import get_settings
from ....infrastructure.database import get_session
from ....services.zip_frame_reader import (
    FrameReadError,
    FrameTimeout,
    ZipIndexCache,
    content_type_for,
    read_frame,
)

logger = logging.getLogger(__name__)

SUCCESS_CACHE_CONTROL = "public, max-age=31536000, immutable"
ERROR_CACHE_CONTROL = "no-store"
# Redis byte cache: key prefix + TTL (only used when FRAMES_BYTES_CACHE_REDIS_URL is set).
REDIS_KEY_PREFIX = "frames:bytes:v1:"
REDIS_TTL_S = 7 * 24 * 3600
_REDIS_SEP = b"\n"


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrameRef:
    """What the DB says about one point's frame."""

    point_id: str
    zip_url: str | None
    zip_entry: str | None
    image_name: str | None


@dataclass(frozen=True)
class CachedFrame:
    data: bytes
    content_type: str


class FrameBytesCache:
    """In-process LRU of frame bytes per point_id, bounded by TOTAL bytes.

    Empty frames and frames bigger than the whole budget are never stored.
    """

    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self.total_bytes = 0
        self._data: OrderedDict[str, CachedFrame] = OrderedDict()

    def get(self, key: str) -> CachedFrame | None:
        frame = self._data.get(key)
        if frame is not None:
            self._data.move_to_end(key)
        return frame

    def put(self, key: str, frame: CachedFrame) -> None:
        size = len(frame.data)
        if size == 0 or size > self.max_bytes:
            return
        old = self._data.pop(key, None)
        if old is not None:
            self.total_bytes -= len(old.data)
        self._data[key] = frame
        self.total_bytes += size
        while self.total_bytes > self.max_bytes and self._data:
            _, evicted = self._data.popitem(last=False)
            self.total_bytes -= len(evicted.data)

    def __len__(self) -> int:
        return len(self._data)


# ── Service (client + caches + semaphore) ───────────────────────────────────


class FrameService:
    """Owns the shared httpx client, the caches and the semaphore.

    One process-wide instance (see ``get_frame_service``); tests build their own
    with an ``httpx.MockTransport`` client and override the dependency.
    """

    def __init__(
        self,
        *,
        storage_service_url: str,
        allowed_hosts: frozenset[str] | None = None,
        timeout_s: float = 25.0,
        max_concurrent: int = 8,
        index_cache_size: int = 512,
        bytes_cache_max_mb: int = 128,
        client: httpx.AsyncClient | None = None,
        redis=None,
    ):
        self.storage_service_url = storage_service_url
        self.allowed_hosts = allowed_hosts
        self.timeout_s = float(timeout_s)
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self.index_cache = ZipIndexCache(index_cache_size)
        self.bytes_cache = FrameBytesCache(int(bytes_cache_max_mb) * 1024 * 1024)
        self.redis = redis
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        # Created lazily on first use (inside the running loop), closed at shutdown.
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s), follow_redirects=False
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass

    # Redis is optional and never fatal: any error is a cache miss.
    async def _redis_get(self, point_id: str) -> CachedFrame | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(REDIS_KEY_PREFIX + point_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Frame Redis cache GET failed: %s", exc)
            return None
        if not raw or _REDIS_SEP not in raw:
            return None
        content_type, data = raw.split(_REDIS_SEP, 1)
        if not data:
            return None
        return CachedFrame(data=data, content_type=content_type.decode("ascii", "replace"))

    async def _redis_put(self, point_id: str, frame: CachedFrame) -> None:
        if self.redis is None or not frame.data:
            return
        try:
            value = frame.content_type.encode("ascii") + _REDIS_SEP + frame.data
            await self.redis.set(REDIS_KEY_PREFIX + point_id, value, ex=REDIS_TTL_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Frame Redis cache SET failed: %s", exc)

    async def get_frame(self, ref: FrameRef) -> CachedFrame:
        """Bytes of ``ref``'s frame: memory → Redis → storage (under semaphore +
        deadline). Raises ``FrameTimeout`` / ``FrameReadError``; nothing failed
        is cached."""
        cached = self.bytes_cache.get(ref.point_id)
        if cached is not None:
            return cached
        cached = await self._redis_get(ref.point_id)
        if cached is not None:
            self.bytes_cache.put(ref.point_id, cached)
            return cached

        try:
            # The deadline also covers the wait for a semaphore slot.
            async with asyncio.timeout(self.timeout_s):
                async with self.semaphore:
                    frame = await read_frame(
                        self.client,
                        ref.zip_url or "",
                        zip_entry=ref.zip_entry,
                        image_name=ref.image_name,
                        storage_service_url=self.storage_service_url,
                        index_cache=self.index_cache,
                        allowed_hosts=self.allowed_hosts,
                    )
        except TimeoutError as exc:
            raise FrameTimeout(f"frame read exceeded {self.timeout_s}s") from exc

        if not frame.data:
            raise FrameReadError("empty frame")
        result = CachedFrame(
            data=frame.data,
            content_type=frame.content_type or content_type_for(frame.entry_name),
        )
        self.bytes_cache.put(ref.point_id, result)
        await self._redis_put(ref.point_id, result)
        return result


_service: FrameService | None = None


def _build_redis(url: str):
    if not url:
        return None
    try:
        import redis.asyncio as redis_asyncio

        return redis_asyncio.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    except Exception as exc:  # noqa: BLE001 — optional cache, never fatal
        logger.warning("Frame Redis cache disabled (%s)", exc)
        return None


def get_frame_service() -> FrameService:
    """Process-wide FrameService, built lazily from settings (overridable in tests)."""
    global _service
    if _service is None:
        s = get_settings()
        _service = FrameService(
            storage_service_url=s.storage_service_url,
            allowed_hosts=s.frames_allowed_host_set,
            timeout_s=s.frames_fetch_timeout_s,
            max_concurrent=s.frames_max_concurrent_fetches,
            index_cache_size=s.frames_index_cache_size,
            bytes_cache_max_mb=s.frames_bytes_cache_max_mb,
            redis=_build_redis(s.frames_bytes_cache_redis_url),
        )
    return _service


async def close_frame_service() -> None:
    """Close the shared httpx client (and Redis) — called from the app shutdown."""
    global _service
    if _service is not None:
        await _service.aclose()
        _service = None


# ── DB lookup ────────────────────────────────────────────────────────────────

FrameLookup = Callable[[str], Awaitable[FrameRef | None]]


async def lookup_frame_ref(point_id: str) -> FrameRef | None:
    """Row of ``evidence_embeddings`` by qdrant_point_id (unique index)."""
    async with get_session() as session:
        row = (
            await session.execute(
                select(
                    EvidenceEmbeddingRecord.zip_url,
                    EvidenceEmbeddingRecord.zip_entry,
                    EvidenceEmbeddingRecord.image_name,
                ).where(EvidenceEmbeddingRecord.qdrant_point_id == point_id)
            )
        ).first()
    if row is None:
        return None
    return FrameRef(
        point_id=point_id, zip_url=row.zip_url, zip_entry=row.zip_entry, image_name=row.image_name
    )


def get_frame_lookup() -> FrameLookup:
    """Overridable in tests so the route runs without a database."""
    return lookup_frame_ref


# ── Route ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1/frames", tags=["frames"])


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers={"Cache-Control": ERROR_CACHE_CONTROL},
    )


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


@router.api_route("/{point_id}", methods=["GET", "HEAD"])
async def get_frame(
    point_id: str,
    request: Request,
    lookup: FrameLookup = Depends(get_frame_lookup),
    service: FrameService = Depends(get_frame_service),
) -> Response:
    """Serve one frame from its evidence ZIP. Public: no auth dependency."""
    try:
        canonical = str(UUID(point_id))
    except (ValueError, TypeError, AttributeError):
        return _error(404, "Frame not found")

    try:
        ref = await lookup(canonical)
    except Exception:
        logger.exception("Frame lookup failed for %s", canonical)
        return _error(502, "Frame lookup failed")
    if ref is None or not ref.zip_url:
        return _error(404, "Frame not found")

    etag = f'"{canonical}"'
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304, headers={"ETag": etag, "Cache-Control": SUCCESS_CACHE_CONTROL}
        )

    try:
        frame = await service.get_frame(ref)
    except FrameTimeout as exc:
        logger.warning("Frame %s timed out: %s", canonical, exc)
        return _error(504, "Frame read timed out")
    except FrameReadError as exc:
        logger.warning("Frame %s unreadable: %s", canonical, exc)
        return _error(502, "Frame unreadable")
    except Exception:
        logger.exception("Frame %s failed", canonical)
        return _error(502, "Frame unreadable")

    headers = {
        "ETag": etag,
        "Cache-Control": SUCCESS_CACHE_CONTROL,
        "Content-Length": str(len(frame.data)),
        "X-Content-Type-Options": "nosniff",
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=frame.content_type, headers=headers)
    return Response(
        content=frame.data, status_code=200, media_type=frame.content_type, headers=headers
    )
