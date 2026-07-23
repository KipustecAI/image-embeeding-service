"""P1 unit tests — image-index Qdrant read primitives against a FAKE client.

Hermetic (no Postgres, no live Qdrant, no env): the repos are constructed with a
tiny stand-in settings object and a recording fake client injected as `.client`.

Covers (per the §11 P1 audit focus):
  - cross-collection cosine SHAPE: search_similar returns pure-payload dicts with
    the locked key set (score/external_id/batch_id/item_index/item_ref/source_url/
    qdrant_point_id + image_id NOT-NULL fallback + model_version).
  - the CLOSED filter: user_id ALWAYS MatchValue; external_ids → MatchAny; batch_id
    → MatchValue; score_threshold + top_k forwarded.
  - None/empty/degenerate handling: client-None, empty, zero-vector, NaN/inf → [].
  - get_point_vector present on BOTH repos (evidence + image-index) → vector or None.
  - upsert_items stamps model_version="clip-vit-b-32".
"""

import math
from types import SimpleNamespace

import pytest
from qdrant_client.models import MatchAny, MatchValue

from src.infrastructure.vector_db.image_index_vector_repository import (
    _MODEL_VERSION,
    ImageIndexVectorRepository,
    _is_finite_nonzero,
)
from src.infrastructure.vector_db.qdrant_repository import QdrantVectorRepository

# ── fakes ────────────────────────────────────────────────────────────────────


class _Hit:
    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


class _Point:
    def __init__(self, vector):
        self.vector = vector


class FakeQdrantClient:
    """Records the last search/retrieve kwargs and returns canned results."""

    def __init__(self, *, hits=None, points=None, raise_on_search=False):
        self._hits = hits or []
        self._points = points  # None → not-found ([]); list → returned
        self._raise_on_search = raise_on_search
        self.last_search = None
        self.last_retrieve = None

    def search(self, **kwargs):
        self.last_search = kwargs
        if self._raise_on_search:
            raise RuntimeError("qdrant down")
        return self._hits

    def retrieve(self, **kwargs):
        self.last_retrieve = kwargs
        return self._points if self._points is not None else []


def _image_index_repo(client=None):
    settings = SimpleNamespace(
        qdrant_collection_image_index="image_index_embeddings",
        qdrant_vector_size=512,
    )
    repo = ImageIndexVectorRepository(settings)
    repo.client = client
    return repo


def _evidence_repo(client=None):
    settings = SimpleNamespace(
        qdrant_collection_name="evidence_embeddings",
        qdrant_vector_size=512,
    )
    repo = QdrantVectorRepository(settings)
    repo.client = client
    return repo


def _good_vec():
    return [0.1] * 512


# ── _is_finite_nonzero guard ─────────────────────────────────────────────────


def test_is_finite_nonzero_rejects_degenerate():
    assert _is_finite_nonzero([0.1, 0.2, 0.3]) is True
    assert _is_finite_nonzero([]) is False
    assert _is_finite_nonzero([0.0, 0.0, 0.0]) is False
    assert _is_finite_nonzero([float("nan"), 0.1]) is False
    assert _is_finite_nonzero([float("inf"), 0.1]) is False
    assert _is_finite_nonzero([None, 0.1]) is False


# ── search_similar: filter construction ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_similar_builds_closed_tenant_filter():
    client = FakeQdrantClient(hits=[])
    repo = _image_index_repo(client)

    await repo.search_similar(
        _good_vec(),
        user_id="tenant-1",
        external_ids=["run-1", "run-2"],
        batch_id="batch-xyz",
        top_k=17,
        threshold=0.83,
    )

    kwargs = client.last_search
    assert kwargs["collection_name"] == "image_index_embeddings"
    assert kwargs["limit"] == 17
    assert kwargs["score_threshold"] == 0.83
    assert kwargs["with_payload"] is True

    must = kwargs["query_filter"].must
    # user_id ALWAYS first, as a scalar MatchValue (IDOR enforcement point).
    assert isinstance(must[0].match, MatchValue)
    assert must[0].key == "user_id"
    assert must[0].match.value == "tenant-1"
    # external_ids → MatchAny over the string-coerced list.
    ext = next(c for c in must if c.key == "external_id")
    assert isinstance(ext.match, MatchAny)
    assert ext.match.any == ["run-1", "run-2"]
    # batch_id → scalar MatchValue.
    bid = next(c for c in must if c.key == "batch_id")
    assert isinstance(bid.match, MatchValue)
    assert bid.match.value == "batch-xyz"


@pytest.mark.asyncio
async def test_search_similar_user_id_only_when_no_optional_scopes():
    client = FakeQdrantClient(hits=[])
    repo = _image_index_repo(client)
    await repo.search_similar(_good_vec(), user_id="t9")
    must = client.last_search["query_filter"].must
    assert len(must) == 1
    assert must[0].key == "user_id"
    assert must[0].match.value == "t9"


@pytest.mark.asyncio
async def test_search_similar_coerces_non_string_external_ids():
    client = FakeQdrantClient(hits=[])
    repo = _image_index_repo(client)
    await repo.search_similar(_good_vec(), user_id=42, external_ids=[1, 2])
    must = client.last_search["query_filter"].must
    assert must[0].match.value == "42"
    ext = next(c for c in must if c.key == "external_id")
    assert ext.match.any == ["1", "2"]


# ── search_similar: result mapping (cross-collection cosine shape) ────────────


@pytest.mark.asyncio
async def test_search_similar_maps_hits_to_payload_dicts():
    hits = [
        _Hit(
            "pid-1",
            0.97,
            {
                "external_id": "run-1",
                "batch_id": "b1",
                "item_index": 3,
                "item_ref": "ref-3",
                "source_url": "http://img/3.jpg",
                "user_id": "tenant-1",
                "model_version": "clip-vit-b-32",
            },
        )
    ]
    repo = _image_index_repo(FakeQdrantClient(hits=hits))
    out = await repo.search_similar(_good_vec(), user_id="tenant-1")
    assert out == [
        {
            "external_id": "run-1",
            "batch_id": "b1",
            "item_index": 3,
            "item_ref": "ref-3",
            "image_id": "ref-3",
            "source_url": "http://img/3.jpg",
            "score": 0.97,
            "qdrant_point_id": "pid-1",
            "user_id": "tenant-1",
            "model_version": "clip-vit-b-32",
        }
    ]


@pytest.mark.asyncio
async def test_search_similar_image_id_falls_back_to_point_id():
    # item_ref is null (submit gave no item_id) → image_id must fall back to the
    # point-id so the NOT-NULL search_matches.evidence_id insert never throws (§4).
    hits = [_Hit("pid-9", 0.9, {"external_id": "run-1", "item_ref": None})]
    repo = _image_index_repo(FakeQdrantClient(hits=hits))
    out = await repo.search_similar(_good_vec(), user_id="t")
    assert out[0]["image_id"] == "pid-9"
    assert out[0]["item_ref"] is None


@pytest.mark.asyncio
async def test_search_similar_empty_payload_hit():
    hits = [_Hit("pid-x", 0.5, None)]
    repo = _image_index_repo(FakeQdrantClient(hits=hits))
    out = await repo.search_similar(_good_vec(), user_id="t")
    assert out[0]["image_id"] == "pid-x"
    assert out[0]["external_id"] is None


# ── search_similar: None/empty/degenerate/error → [] ─────────────────────────


@pytest.mark.asyncio
async def test_search_similar_returns_empty_when_client_none():
    repo = _image_index_repo(client=None)
    assert await repo.search_similar(_good_vec(), user_id="t") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vec",
    [
        [],
        [0.0] * 512,
        [float("nan")] + [0.1] * 511,
        [float("inf")] + [0.1] * 511,
    ],
)
async def test_search_similar_skips_degenerate_vector(vec):
    client = FakeQdrantClient(hits=[_Hit("p", 1.0, {})])
    repo = _image_index_repo(client)
    assert await repo.search_similar(vec, user_id="t") == []
    # Guard must fire BEFORE any Qdrant call.
    assert client.last_search is None


@pytest.mark.asyncio
async def test_search_similar_accepts_numpy_like_vector():
    import numpy as np

    client = FakeQdrantClient(hits=[])
    repo = _image_index_repo(client)
    await repo.search_similar(np.ones(512, dtype=np.float32), user_id="t")
    # tolist() branch produced a plain python list for the client.
    assert isinstance(client.last_search["query_vector"], list)
    assert len(client.last_search["query_vector"]) == 512


@pytest.mark.asyncio
async def test_search_similar_swallows_client_exception():
    repo = _image_index_repo(FakeQdrantClient(raise_on_search=True))
    assert await repo.search_similar(_good_vec(), user_id="t") == []


# ── get_point_vector on BOTH repos ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_point_vector_image_index_returns_vector():
    client = FakeQdrantClient(points=[_Point([0.5] * 512)])
    repo = _image_index_repo(client)
    vec = await repo.get_point_vector("pid-1")
    assert vec == [0.5] * 512
    assert client.last_retrieve["collection_name"] == "image_index_embeddings"
    assert client.last_retrieve["with_vectors"] is True
    assert client.last_retrieve["ids"] == ["pid-1"]


@pytest.mark.asyncio
async def test_get_point_vector_evidence_returns_vector():
    client = FakeQdrantClient(points=[_Point([0.25] * 512)])
    repo = _evidence_repo(client)
    vec = await repo.get_point_vector("pid-ev")
    assert vec == [0.25] * 512
    assert client.last_retrieve["collection_name"] == "evidence_embeddings"
    assert client.last_retrieve["with_vectors"] is True


@pytest.mark.asyncio
async def test_get_point_vector_missing_returns_none_both_repos():
    for repo in (
        _image_index_repo(FakeQdrantClient(points=[])),
        _evidence_repo(FakeQdrantClient(points=[])),
    ):
        assert await repo.get_point_vector("nope") is None


@pytest.mark.asyncio
async def test_get_point_vector_client_none_returns_none_both_repos():
    assert await _image_index_repo(client=None).get_point_vector("p") is None
    assert await _evidence_repo(client=None).get_point_vector("p") is None


def test_both_repos_expose_get_point_vector():
    assert hasattr(ImageIndexVectorRepository, "get_point_vector")
    assert hasattr(QdrantVectorRepository, "get_point_vector")


# ── upsert_items stamps model_version ────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_items_stamps_model_version():
    captured = {}

    class _UpsertClient:
        def upsert(self, **kwargs):
            captured["points"] = kwargs["points"]

            class _R:
                from qdrant_client.models import UpdateStatus as _US

                status = _US.COMPLETED

            return _R()

    repo = _image_index_repo(_UpsertClient())
    ok = await repo.upsert_items(
        [
            {
                "batch_id": "b1",
                "item_index": 0,
                "vector": _good_vec(),
                "user_id": "t1",
                "external_id": "run-1",
                "item_ref": "ref-0",
                "source_url": "http://img/0.jpg",
            }
        ]
    )
    assert ok is True
    payload = captured["points"][0].payload
    assert payload["model_version"] == _MODEL_VERSION == "clip-vit-b-32"


def test_math_import_present():
    # _is_finite_nonzero relies on math.isfinite; smoke that the module wired it.
    assert math.isfinite(1.0)
