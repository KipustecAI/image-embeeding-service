"""FRAME_STORAGE_MODE on the embeddings:results consumer + additive event keys.

Lookia Platform#frames-desde-zip, contracts A (ingest) and B (events):

* zip_ref (default): NO ZIP download, NO storage upload; image_url is the frame
  URL ``{FRAMES_PUBLIC_BASE_URL}/api/v1/frames/{qdrant_point_id}``; zip_url /
  image_name / zip_entry land on the DB row AND the Qdrant payload.
* upload: today's download + upload path, no frame fields.
* image_count on the request row; the five outbound events gain nullable keys.

Pure unit tests: no DB, no Qdrant, no storage, no Redis. Every collaborator of
the consumer is a fake injected with monkeypatch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import numpy as np
import pytest

from src.application.helpers import frame_refs
from src.application.helpers.blacklist_match_events import build_blacklist_match_event
from src.application.helpers.frame_refs import (
    frame_url,
    search_match_frame_fields,
    strip_cache_buster,
)
from src.application.helpers.weapon_report_events import build_weapons_detected_event
from src.db.models.search_match import SearchMatch
from src.services import blacklist_match_service as bms
from src.services import dw_publisher_service as dw
from src.streams import embedding_results_consumer as mod
from src.streams import search_results_consumer as src_mod

BASE = "https://api-dev.lookia.mx"
ZIP_URL = "https://storage.lookia.mx/lucam-assets/public/evidences/ev-1.zip"
VEC = np.zeros(512, dtype=np.float32).tolist()


# ── Fakes ────────────────────────────────────────────────────────────────────


class _ExplodingZip:
    """zip_ref must never touch the ZIP processor."""

    def __init__(self):
        self.calls = 0

    async def download_and_extract(self, zip_url, image_names):
        self.calls += 1
        raise AssertionError("ZIP downloaded in zip_ref mode")


class _CountingZip:
    def __init__(self):
        self.calls = 0

    async def download_and_extract(self, zip_url, image_names):
        self.calls += 1
        return dict.fromkeys(image_names, b"JPEG")


class _Uploader:
    def __init__(self, explode: bool = False):
        self.explode = explode
        self.calls = 0

    async def upload_image(self, img_bytes, name, folder, user_id="embedding-service"):
        self.calls += 1
        if self.explode:
            raise AssertionError("storage upload in zip_ref mode")
        return f"https://storage.test/lucam-assets/{folder}/{name}"


class _VectorRepo:
    def __init__(self):
        self.stored = []

    async def store_embeddings_batch(self, embeddings):
        self.stored.extend(embeddings)
        return True


class _Repo:
    """EmbeddingRequestRepository stand-in: never a duplicate, records the row."""

    created: list = []

    def __init__(self, session):
        pass

    async def check_duplicate(self, evidence_id):
        return False

    async def create_request(self, **kwargs):
        row = SimpleNamespace(id=uuid4(), status=None, processing_completed_at=None, **kwargs)
        _Repo.created.append(row)
        return row


@pytest.fixture
def harness(monkeypatch):
    """Wire the consumer to fakes; return the captured side effects."""
    session = MagicMock()
    session.flush = AsyncMock()
    added: list = []
    session.add_all.side_effect = added.extend

    @asynccontextmanager
    async def _fake_session():
        yield session

    _Repo.created = []
    published_embeddings: list = []
    published_requests: list = []
    vector_repo = _VectorRepo()
    zip_proc = _ExplodingZip()
    uploader = _Uploader(explode=True)

    monkeypatch.setattr(mod, "get_session", _fake_session)
    monkeypatch.setattr(mod, "EmbeddingRequestRepository", _Repo)
    monkeypatch.setattr(mod, "_vector_repo", vector_repo)
    monkeypatch.setattr(mod, "_zip_processor", zip_proc)
    monkeypatch.setattr(mod, "_storage_uploader", uploader)
    monkeypatch.setattr(mod, "_stream_producer", None)
    monkeypatch.setattr(mod, "publish_image_embedding", published_embeddings.append)
    monkeypatch.setattr(
        mod,
        "publish_image_embedding_request",
        lambda event_type, row: published_requests.append((event_type, row)),
    )
    monkeypatch.setattr(mod.settings, "frame_storage_mode", "zip_ref")
    monkeypatch.setattr(mod.settings, "frames_public_base_url", BASE + "/")

    return SimpleNamespace(
        session=session,
        added=added,
        vector_repo=vector_repo,
        zip=zip_proc,
        uploader=uploader,
        embeddings=published_embeddings,
        requests=published_requests,
        monkeypatch=monkeypatch,
    )


def _payload(**overrides) -> dict:
    payload = {
        "evidence_id": "ev-1",
        "camera_id": "cam-1",
        "zip_url": ZIP_URL,
        "embeddings": [
            {
                "image_name": "20261021-001529_457445.jpg",
                "zip_entry": "cropped/20261021-001529_457445.jpg",
                "image_index": 0,
                "vector": VEC,
            },
            {"image_name": "b.jpg", "zip_entry": "b.jpg", "image_index": 1, "vector": VEC},
        ],
    }
    payload.update(overrides)
    return payload


# ── Contract A: ingest ───────────────────────────────────────────────────────


async def test_zip_ref_makes_no_zip_or_storage_call_and_stores_frame_url(harness):
    await mod._process_embeddings_result(_payload(), "1-0")

    assert harness.zip.calls == 0
    assert harness.uploader.calls == 0

    stored = harness.vector_repo.stored
    assert len(stored) == 2
    for emb in stored:
        assert emb.image_url == f"{BASE}/api/v1/frames/{emb.id}"
        # Qdrant payload carries the same image_url + the frame reference.
        assert emb.metadata["image_url"] == emb.image_url
        assert emb.metadata["zip_url"] == ZIP_URL
    assert stored[0].metadata["zip_entry"] == "cropped/20261021-001529_457445.jpg"
    assert stored[0].metadata["image_name"] == "20261021-001529_457445.jpg"

    records = harness.added
    assert [r.qdrant_point_id for r in records] == [e.id for e in stored]
    for rec, emb in zip(records, stored, strict=True):
        assert rec.image_url == emb.image_url
        assert rec.zip_url == ZIP_URL
        # The point id the frame URL embeds is a canonical UUID string.
        assert str(UUID(rec.qdrant_point_id)) == rec.qdrant_point_id
    assert records[0].zip_entry == "cropped/20261021-001529_457445.jpg"  # RAW, not encoded
    assert records[1].image_name == "b.jpg"

    request = _Repo.created[0]
    assert request.image_urls == [e.image_url for e in stored]
    assert all(u for u in request.image_urls)  # never empty in zip_ref

    # DW events fire for every row.
    assert len(harness.embeddings) == 2
    assert [t for t, _ in harness.requests] == [
        "image_embedding_request.created",
        "image_embedding_request.completed",
    ]


async def test_image_count_is_number_of_embeddings(harness):
    await mod._process_embeddings_result(_payload(), "1-0")
    assert _Repo.created[0].image_count == 2


async def test_zip_entry_absent_is_stored_null(harness):
    payload = _payload(
        embeddings=[{"image_name": "a.jpg", "image_index": 0, "vector": VEC}],
    )
    await mod._process_embeddings_result(payload, "1-0")

    (rec,) = harness.added
    (emb,) = harness.vector_repo.stored
    assert rec.zip_entry is None
    assert rec.image_name == "a.jpg"
    assert emb.metadata["zip_entry"] is None
    assert emb.image_url == f"{BASE}/api/v1/frames/{emb.id}"


async def test_cache_buster_is_never_stored(harness):
    payload = _payload(zip_url=ZIP_URL + "?nc=deadbeef")
    await mod._process_embeddings_result(payload, "1-0")
    assert {r.zip_url for r in harness.added} == {ZIP_URL}


async def test_empty_base_url_gives_relative_frame_url(harness):
    harness.monkeypatch.setattr(mod.settings, "frames_public_base_url", "")
    await mod._process_embeddings_result(_payload(), "1-0")
    for emb in harness.vector_repo.stored:
        assert emb.image_url == f"/api/v1/frames/{emb.id}"


async def test_zip_ref_without_zip_url_keeps_legacy_direct_url(harness):
    payload = _payload(
        zip_url=None,
        embeddings=[{"image_url": "https://x/legacy.jpg", "image_index": 0, "vector": VEC}],
    )
    await mod._process_embeddings_result(payload, "1-0")
    (rec,) = harness.added
    assert rec.image_url == "https://x/legacy.jpg"
    assert rec.zip_url is None and rec.zip_entry is None and rec.image_name is None


async def test_upload_mode_keeps_download_and_upload(harness):
    harness.monkeypatch.setattr(mod.settings, "frame_storage_mode", "upload")
    zip_proc = _CountingZip()
    uploader = _Uploader()
    harness.monkeypatch.setattr(mod, "_zip_processor", zip_proc)
    harness.monkeypatch.setattr(mod, "_storage_uploader", uploader)

    await mod._process_embeddings_result(_payload(), "1-0")

    assert zip_proc.calls == 1
    assert uploader.calls == 2
    folder = "public/embeddings/cam-1/ev-1"
    urls = [e.image_url for e in harness.vector_repo.stored]
    assert urls == [
        f"https://storage.test/lucam-assets/{folder}/20261021-001529_457445.jpg",
        f"https://storage.test/lucam-assets/{folder}/b.jpg",
    ]
    for emb in harness.vector_repo.stored:
        # Upload-mode Qdrant payload is byte-identical to before: no frame keys.
        assert not {"zip_url", "image_name", "zip_entry"} & set(emb.metadata)
    for rec in harness.added:
        assert rec.zip_url is None and rec.image_name is None and rec.zip_entry is None
    assert _Repo.created[0].image_count == 2


async def test_weapons_event_images_carry_zip_reference(harness):
    producer = MagicMock()
    harness.monkeypatch.setattr(mod, "_stream_producer", producer)
    payload = _payload(
        weapon_analysis={
            "summary": {"has_weapon": True, "total_detections": 1},
            "images": [
                {
                    "image_name": "20261021-001529_457445.jpg",
                    "detections": [{"class_name": "arma_fuego", "confidence": 0.8}],
                }
            ],
        }
    )
    await mod._process_embeddings_result(payload, "1-0")

    event = producer.publish.call_args.kwargs["payload"]
    (img,) = event["images"]
    assert img["image_name"] == "20261021-001529_457445.jpg"
    assert img["zip_url"] == ZIP_URL
    assert img["zip_entry"] == "cropped/20261021-001529_457445.jpg"
    assert img["image_url"].startswith(f"{BASE}/api/v1/frames/")


# ── frame_refs helpers ───────────────────────────────────────────────────────


def test_frame_url_strips_trailing_slash_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(frame_refs, "_warned_relative", False)
    assert frame_url("https://h/", "p") == "https://h/api/v1/frames/p"
    caplog.set_level("WARNING", logger="src.application.helpers.frame_refs")
    assert frame_url("", "p") == "/api/v1/frames/p"
    assert frame_url(None, "q") == "/api/v1/frames/q"
    assert sum("FRAMES_PUBLIC_BASE_URL" in r.message for r in caplog.records) == 1


def test_strip_cache_buster():
    assert strip_cache_buster(ZIP_URL) == ZIP_URL
    assert strip_cache_buster(ZIP_URL + "?nc=abc") == ZIP_URL
    assert strip_cache_buster(ZIP_URL + "?a=1&nc=abc") == ZIP_URL + "?a=1"
    signed = ZIP_URL + "?X-Amz-Credential=c&X-Amz-Signature=s&nc=1"
    assert strip_cache_buster(signed) == signed  # never touch a presigned URL
    assert strip_cache_buster(None) is None


def test_search_match_frame_fields():
    assert search_match_frame_fields(
        {"qdrant_point_id": "p-1", "image_name": "a.jpg", "zip_entry": "low_res/a.jpg"}
    ) == {"qdrant_point_id": "p-1", "image_name": "a.jpg", "zip_entry": "low_res/a.jpg"}
    # Old point: only the id the Qdrant search injects.
    assert search_match_frame_fields({"qdrant_point_id": "p-2"}) == {
        "qdrant_point_id": "p-2",
        "image_name": None,
        "zip_entry": None,
    }
    assert search_match_frame_fields(None) == {
        "qdrant_point_id": None,
        "image_name": None,
        "zip_entry": None,
    }


# ── Contract B: outbound events (new keys present + nullable) ────────────────


@pytest.fixture
def dw_producer():
    original = dw._stream_producer
    p = MagicMock()
    dw.set_dw_publisher_producer(p)
    yield p
    dw._stream_producer = original


def _embedding_row(**extra):
    return SimpleNamespace(
        id=uuid4(),
        request_id=uuid4(),
        qdrant_point_id="11111111-2222-3333-4444-555555555555",
        image_index=0,
        image_url=f"{BASE}/api/v1/frames/11111111-2222-3333-4444-555555555555",
        weapon_detections=None,
        json_data={},
        created_at=datetime(2026, 10, 21),
        **extra,
    )


def test_image_embedding_upserted_carries_zip_reference(dw_producer):
    dw.publish_image_embedding(
        _embedding_row(zip_url=ZIP_URL, image_name="a.jpg", zip_entry="cropped/a.jpg")
    )
    p = dw_producer.publish.call_args.kwargs["payload"]
    assert p["zip_url"] == ZIP_URL
    assert p["image_name"] == "a.jpg"
    assert p["zip_entry"] == "cropped/a.jpg"
    assert p["qdrant_point_id"] == "11111111-2222-3333-4444-555555555555"
    assert p["image_url"].endswith("/api/v1/frames/11111111-2222-3333-4444-555555555555")


def test_image_embedding_upserted_new_keys_null_for_old_rows(dw_producer):
    dw.publish_image_embedding(_embedding_row())
    p = dw_producer.publish.call_args.kwargs["payload"]
    assert p["zip_url"] is None and p["image_name"] is None and p["zip_entry"] is None


def _request_row(**extra):
    fields = {
        "id": uuid4(),
        "evidence_id": "ev-1",
        "camera_id": "cam-1",
        "user_id": None,
        "device_id": None,
        "app_id": None,
        "infraction_code": None,
        "category": None,
        "status": 4,
        "image_urls": [],
        "worker_id": None,
        "error_message": None,
        "retry_count": 0,
        "processing_started_at": None,
        "processing_completed_at": None,
        "stream_message_id": None,
        "weapon_analyzed": False,
        "has_weapon": False,
        "weapon_classes": [],
        "weapon_max_confidence": None,
        "weapon_summary": None,
        "weapon_analysis_error": None,
        "created_at": datetime(2026, 10, 21),
        "updated_at": datetime(2026, 10, 21),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_image_embedding_request_carries_image_count(dw_producer):
    dw.publish_image_embedding_request(
        "image_embedding_request.completed", _request_row(image_count=9)
    )
    assert dw_producer.publish.call_args.kwargs["payload"]["image_count"] == 9


def test_image_embedding_request_image_count_null_for_old_rows(dw_producer):
    dw.publish_image_embedding_request("image_embedding_request.completed", _request_row())
    p = dw_producer.publish.call_args.kwargs["payload"]
    assert "image_count" in p and p["image_count"] is None


def test_image_search_matched_matches_carry_frame_fields(dw_producer):
    new = SimpleNamespace(
        id=uuid4(),
        evidence_id="ev-1",
        camera_id="cam-1",
        similarity_score=0.9,
        image_url=f"{BASE}/api/v1/frames/p-new",
        match_metadata={"qdrant_point_id": "p-new"},
        qdrant_point_id="p-new",
        image_name="a.jpg",
        zip_entry="cropped/a.jpg",
    )
    # Row written before the columns existed: point id from match_metadata,
    # frame fields null.
    old = SimpleNamespace(
        id=uuid4(),
        evidence_id="ev-0",
        camera_id="cam-1",
        similarity_score=0.8,
        image_url="https://storage.test/old.jpg",
        match_metadata={"qdrant_point_id": "p-old"},
    )
    dw.publish_image_search_matches(
        search_request_id=uuid4(),
        user_id="u",
        image_url="https://x/q.jpg",
        total_matches=2,
        matches=[new, old],
    )
    m_new, m_old = dw_producer.publish.call_args.kwargs["payload"]["matches"]
    assert (m_new["qdrant_point_id"], m_new["image_name"], m_new["zip_entry"]) == (
        "p-new",
        "a.jpg",
        "cropped/a.jpg",
    )
    assert (m_old["qdrant_point_id"], m_old["image_name"], m_old["zip_entry"]) == (
        "p-old",
        None,
        None,
    )


def test_weapons_event_images_new_keys_nullable():
    event = build_weapons_detected_event(
        evidence_id="ev-1",
        camera_id="cam-1",
        user_id=None,
        device_id=None,
        app_id=None,
        infraction_code=None,
        weapon_summary={},
        images_with_detections=[
            {"image_name": "a.jpg", "image_index": 0, "image_url": "u", "detections": []}
        ],
    )
    (img,) = event["images"]
    assert "zip_url" in img and img["zip_url"] is None
    assert "zip_entry" in img and img["zip_entry"] is None


_BL_KWARGS = {
    "user_id": "u",
    "blacklist_entry_id": "e",
    "blacklist_entry_name": "n",
    "blacklist_entry_category": None,
    "blacklist_entry_version": 1,
    "blacklist_reference_id": "r",
    "blacklist_reference_url": "https://x/r.jpg",
    "blacklist_model_version": "clip-vit-b-32",
    "evidence_id": "ev-1",
    "evidence_camera_id": None,
    "evidence_device_id": None,
    "evidence_app_id": None,
    "evidence_infraction_code": None,
    "evidence_category": None,
    "matched_image_url": f"{BASE}/api/v1/frames/p-1",
    "matched_image_index": 0,
    "matched_qdrant_point_id": "p-1",
    "similarity_score": 0.9,
    "threshold_used": 0.85,
    "trigger": "inline",
}


def test_blacklist_event_new_keys_present_and_nullable():
    ev = build_blacklist_match_event(**_BL_KWARGS)
    assert ev["matched_point_id"] == "p-1"
    assert ev["zip_url"] is None and ev["zip_entry"] is None

    ev = build_blacklist_match_event(**_BL_KWARGS, zip_url=ZIP_URL, zip_entry="cropped/a b.jpg")
    assert ev["zip_url"] == ZIP_URL
    assert ev["zip_entry"] == "cropped/a b.jpg"  # RAW, not URL-encoded


async def test_publish_blacklist_match_reads_zip_reference_from_point_payload(monkeypatch):
    producer = MagicMock()
    monkeypatch.setattr(bms, "_stream_producer", producer)
    entry = SimpleNamespace(id=uuid4(), name="n", category=None, blacklist_version=1)
    reference = SimpleNamespace(id=uuid4(), image_url="https://x/r.jpg")

    await bms.publish_blacklist_match(
        user_id="u",
        entry=entry,
        reference=reference,
        evidence_id="ev-1",
        evidence_metadata={"zip_url": ZIP_URL, "zip_entry": "low_res/a.jpg"},
        matched_image_url=f"{BASE}/api/v1/frames/p-1",
        matched_image_index=0,
        matched_qdrant_point_id="p-1",
        similarity_score=0.9,
        threshold_used=0.85,
        trigger="inline",
    )
    ev = producer.publish.call_args.kwargs["payload"]
    assert ev["matched_point_id"] == "p-1"
    assert ev["zip_url"] == ZIP_URL
    assert ev["zip_entry"] == "low_res/a.jpg"
    assert ev["matched_image_url"] == f"{BASE}/api/v1/frames/p-1"


# ── search_matches rows carry the frame columns ──────────────────────────────


async def test_evidence_search_writes_frame_columns_on_matches(monkeypatch):
    added: list = []
    session = MagicMock()
    session.flush = AsyncMock()
    session.add.side_effect = added.append

    @asynccontextmanager
    async def _fake_session():
        yield session

    request = SimpleNamespace(id=uuid4(), matches=[], user_id="u", image_url="https://x/q.jpg")

    class _SearchRepo:
        def __init__(self, s):
            pass

        async def get_by_search_id(self, search_id):
            return request

    new_point = SimpleNamespace(
        evidence_id="ev-1",
        camera_id="cam-1",
        similarity_score=0.9,
        image_url=f"{BASE}/api/v1/frames/p-new",
        metadata={"qdrant_point_id": "p-new", "image_name": "a.jpg", "zip_entry": "cropped/a.jpg"},
    )
    old_point = SimpleNamespace(
        evidence_id="ev-0",
        camera_id=None,
        similarity_score=0.8,
        image_url="https://storage.test/old.jpg",
        metadata={"qdrant_point_id": "p-old"},
    )
    vector_repo = MagicMock()
    vector_repo.search_similar = AsyncMock(return_value=[new_point, old_point])
    vector_repo.store_query_vector = AsyncMock(return_value=True)

    published: list = []
    monkeypatch.setattr(src_mod, "get_session", _fake_session)
    monkeypatch.setattr(src_mod, "SearchRequestRepository", _SearchRepo)
    monkeypatch.setattr(src_mod, "_vector_repo", vector_repo)
    monkeypatch.setattr(src_mod, "publish_image_search_request", lambda *a, **k: None)
    monkeypatch.setattr(src_mod, "publish_image_search_matches", lambda **k: published.append(k))

    await src_mod._process_search_result(
        {"search_id": "s-1", "user_id": "u", "vector": [0.1] * 512}, "1-0"
    )

    rows = [r for r in added if isinstance(r, SearchMatch)]
    assert [(r.qdrant_point_id, r.image_name, r.zip_entry) for r in rows] == [
        ("p-new", "a.jpg", "cropped/a.jpg"),
        ("p-old", None, None),
    ]
    assert published and published[0]["matches"] == rows


async def test_malformed_zip_url_is_stored_as_received_not_failed(harness):
    bad = "http://[bad/x.zip?nc=1"
    await mod._process_embeddings_result(_payload(zip_url=bad), "1-0")
    assert {r.zip_url for r in harness.added} == {bad}
