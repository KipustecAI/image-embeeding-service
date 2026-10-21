"""Guard: every frame the embeddings:results consumer uploads MUST land under ``public/``.

storage-service decides public vs private SOLELY by the ``folder`` sent on
upload. A bare ``embeddings/{camera}/{evidence}`` folder makes the object
private, so an anonymous <img> GET returns 403 — which is exactly what
happened: ~3.4M frames since 2026-09 were written private and had to be copied
into ``public/`` after the fact (scripts/backfill_public_assets.py). This test
is the guard against a repeat.

Pure unit test: no DB, no Qdrant, no storage. The pipeline is stopped right
after the uploads by a vector repo that raises.
"""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import numpy as np

from src.streams import embedding_results_consumer as mod


class _FakeZip:
    async def download_and_extract(self, zip_url, image_names):
        return dict.fromkeys(image_names, b"JPEG")


class _FakeUploader:
    def __init__(self):
        self.folders: list[str] = []

    async def upload_image(self, img_bytes, name, folder, user_id="embedding-service"):
        self.folders.append(folder)
        return f"https://storage.test/lucam-assets/{folder}/{name}"


class _StopAfterUpload:
    async def store_embeddings_batch(self, embeddings):
        raise RuntimeError("stop: uploads are all this test needs")


class _FakeRepo:
    # First call = the pre-flight dedup (not a duplicate); second = the error
    # path's re-check (pretend a row exists so it writes nothing).
    _answers = iter([False, True])

    def __init__(self, session):
        pass

    async def check_duplicate(self, evidence_id):
        return next(self._answers)


async def test_uploads_land_under_the_public_prefix(monkeypatch):
    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()

    _FakeRepo._answers = iter([False, True])
    uploader = _FakeUploader()
    monkeypatch.setattr(mod, "_zip_processor", _FakeZip())
    monkeypatch.setattr(mod, "_storage_uploader", uploader)
    monkeypatch.setattr(mod, "_vector_repo", _StopAfterUpload())
    monkeypatch.setattr(mod, "get_session", _fake_session)
    monkeypatch.setattr(mod, "EmbeddingRequestRepository", _FakeRepo)
    # The download+upload path is FRAME_STORAGE_MODE=upload; the default
    # (zip_ref) makes no upload at all — see test_embedding_zip_ref_mode.py.
    monkeypatch.setattr(mod.settings, "frame_storage_mode", "upload")

    vec = np.zeros(512, dtype=np.float32).tolist()
    payload = {
        "evidence_id": "ev-1",
        "camera_id": "cam-1",
        "zip_url": "https://storage.test/x.zip",
        "embeddings": [
            {"image_name": "a.jpg", "image_index": 0, "vector": vec},
            {"image_name": "b.jpg", "image_index": 1, "vector": vec},
        ],
    }

    await mod._process_embeddings_result(payload, "1-0")

    assert len(uploader.folders) == 2
    for folder in uploader.folders:
        assert folder.startswith("public/"), (
            f"upload folder {folder!r} is NOT under public/ — the object would be "
            f"private and the UI would get 403 on it"
        )
        assert folder == "public/embeddings/cam-1/ev-1"
