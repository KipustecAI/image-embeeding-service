"""Integration tests: embed local images → search → verify matches.

Uses data/inputs/ as evidence images and data/request/ as search queries.
Requires: PostgreSQL, Qdrant, Redis, CLIP model, and local test images.

Run locally (NOT in CI — these are skipped automatically):
  source ~/anaconda3/etc/profile.d/conda.sh && conda activate clip_p11
  pytest tests/test_integration.py -v -s
"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

# Auto-skip entire module if CLIP dependencies are missing
np = pytest.importorskip("numpy", reason="numpy not available (CLIP dependency)")
pytest.importorskip(
    "sentence_transformers", reason="sentence-transformers not available — skip CLIP tests"
)

# ── Paths ──
PROJECT_DIR = Path(__file__).parent.parent
INPUT_DIR = PROJECT_DIR / "data" / "inputs"
REQUEST_DIR = PROJECT_DIR / "data" / "request"

# Test collection name (isolated from production)
TEST_COLLECTION = "test_evidence_embeddings"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def settings():
    from src.infrastructure.config import get_settings

    s = get_settings()
    return s


@pytest_asyncio.fixture(scope="session")
async def embedder(settings):
    from src.infrastructure.embedding.clip_embedder import CLIPEmbedder

    clip = CLIPEmbedder(settings)
    await clip.initialize()
    yield clip
    await clip.cleanup()


@pytest_asyncio.fixture(scope="session")
async def vector_repo(settings):
    from src.infrastructure.vector_db.qdrant_repository import QdrantVectorRepository

    repo = QdrantVectorRepository(settings)
    # Override collection name for test isolation
    repo.collection_name = TEST_COLLECTION
    await repo.initialize()
    yield repo
    # Cleanup: delete test collection
    try:
        repo.client.delete_collection(TEST_COLLECTION)
    except Exception:
        pass


@pytest_asyncio.fixture(scope="session")
async def db_session():
    from src.infrastructure.database import get_session

    async with get_session() as session:
        yield session


def get_input_images():
    """Get list of image file paths from data/inputs/."""
    if not INPUT_DIR.exists():
        return []
    return sorted(
        [
            str(p)
            for p in INPUT_DIR.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        ]
    )


def get_request_images():
    """Get list of image file paths from data/request/."""
    if not REQUEST_DIR.exists():
        return []
    return sorted(
        [
            str(p)
            for p in REQUEST_DIR.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
        ]
    )


# ── Tests ──


class TestCLIPEmbedder:
    """Test CLIP embedding generation with local images."""

    def test_input_images_exist(self):
        images = get_input_images()
        assert len(images) > 0, f"No images found in {INPUT_DIR}"

    def test_request_images_exist(self):
        images = get_request_images()
        assert len(images) > 0, f"No images found in {REQUEST_DIR}"

    @pytest.mark.asyncio
    async def test_generate_embedding_from_file(self, embedder):
        """Verify CLIP can generate a 512-dim embedding from a local image."""
        images = get_input_images()
        image_path = images[0]

        vector = await embedder.generate_embedding(f"file://{image_path}")
        assert vector is not None, f"Failed to embed {image_path}"
        assert isinstance(vector, np.ndarray)
        assert vector.shape == (512,), f"Expected 512-dim, got {vector.shape}"
        assert np.isfinite(vector).all(), "Vector contains NaN or Inf"

    @pytest.mark.asyncio
    async def test_generate_batch_embeddings(self, embedder):
        """Verify batch embedding for multiple images."""
        images = get_input_images()[:5]
        urls = [f"file://{p}" for p in images]

        vectors = await embedder.generate_embeddings_batch(urls)
        assert len(vectors) == len(urls)

        valid = [v for v in vectors if v is not None]
        assert len(valid) > 0, "No valid embeddings generated"

        for v in valid:
            assert v.shape == (512,)

    @pytest.mark.asyncio
    async def test_different_images_different_vectors(self, embedder):
        """Verify different images produce different embeddings."""
        images = get_input_images()
        if len(images) < 2:
            pytest.skip("Need at least 2 images")

        v1 = await embedder.generate_embedding(f"file://{images[0]}")
        v2 = await embedder.generate_embedding(f"file://{images[-1]}")

        assert v1 is not None and v2 is not None
        similarity = np.dot(v1, v2)
        # Different images should not be identical
        assert similarity < 0.999, f"Vectors too similar ({similarity:.4f})"

    @pytest.mark.asyncio
    async def test_same_image_consistent_vector(self, embedder):
        """Verify same image produces the same embedding."""
        images = get_input_images()
        url = f"file://{images[0]}"

        v1 = await embedder.generate_embedding(url)
        v2 = await embedder.generate_embedding(url)

        assert v1 is not None and v2 is not None
        similarity = np.dot(v1, v2)
        assert similarity > 0.999, f"Same image should produce same vector ({similarity:.4f})"


class TestQdrantStorage:
    """Test Qdrant vector storage and search."""

    @pytest.mark.asyncio
    async def test_store_and_retrieve_embedding(self, embedder, vector_repo):
        """Store an embedding in Qdrant and verify it exists."""
        from src.domain.entities import ImageEmbedding

        images = get_input_images()
        image_path = images[0]
        vector = await embedder.generate_embedding(f"file://{image_path}")
        assert vector is not None

        point_id = str(uuid4())
        embedding = ImageEmbedding.from_evidence(
            evidence_id=str(uuid4()),
            vector=vector,
            image_url=f"file://{image_path}",
            camera_id=str(uuid4()),
            additional_metadata={"image_index": 0, "total_images": 1},
        )
        embedding.id = point_id

        success = await vector_repo.store_embedding(embedding)
        assert success, "Failed to store embedding"

        exists = await vector_repo.embedding_exists(point_id)
        assert exists, "Embedding not found after storage"

    @pytest.mark.asyncio
    async def test_store_all_inputs_and_search(self, embedder, vector_repo):
        """Embed all input images, then search with request image — core pipeline test."""
        from src.domain.entities import ImageEmbedding

        input_images = get_input_images()
        request_images = get_request_images()
        assert len(input_images) > 0
        assert len(request_images) > 0

        evidence_id = str(uuid4())
        camera_id = str(uuid4())
        stored_ids = []

        # ── Phase 1: Embed all input images ──
        for idx, image_path in enumerate(input_images):
            vector = await embedder.generate_embedding(f"file://{image_path}")
            if vector is None:
                continue

            point_id = str(uuid4())
            embedding = ImageEmbedding.from_evidence(
                evidence_id=evidence_id,
                vector=vector,
                image_url=f"file://{image_path}",
                camera_id=camera_id,
                additional_metadata={
                    "image_index": idx,
                    "total_images": len(input_images),
                    "filename": os.path.basename(image_path),
                },
            )
            embedding.id = point_id

            success = await vector_repo.store_embedding(embedding)
            if success:
                stored_ids.append(point_id)

        assert len(stored_ids) > 0, "No embeddings stored"
        print(f"\n  Stored {len(stored_ids)}/{len(input_images)} embeddings")

        # ── Phase 2: Search with request image ──
        request_image = request_images[0]
        query_vector = await embedder.generate_embedding(f"file://{request_image}")
        assert query_vector is not None, "Failed to embed request image"

        results = await vector_repo.search_similar(
            query_vector=query_vector,
            limit=20,
            threshold=0.3,  # Low threshold to catch any matches
        )

        print(f"  Search returned {len(results)} results")
        for r in results[:5]:
            filename = r.metadata.get("filename", "?")
            print(f"    {filename}: {r.similarity_score:.4f}")

        # The request image (IMG_3416.JPG) is the same as one of the inputs
        # so we should find at least one very high match
        assert len(results) > 0, "No search results found"

        # Best match should be very high similarity (same image)
        best_score = results[0].similarity_score
        print(f"\n  Best match score: {best_score:.4f}")
        assert best_score > 0.8, (
            f"Expected high similarity for identical image, got {best_score:.4f}"
        )

    @pytest.mark.asyncio
    async def test_search_with_threshold_filtering(self, embedder, vector_repo):
        """Verify threshold filtering works — high threshold returns fewer results."""
        request_images = get_request_images()
        query_vector = await embedder.generate_embedding(f"file://{request_images[0]}")
        assert query_vector is not None

        results_low = await vector_repo.search_similar(
            query_vector=query_vector,
            limit=50,
            threshold=0.3,
        )
        results_high = await vector_repo.search_similar(
            query_vector=query_vector,
            limit=50,
            threshold=0.9,
        )

        print(f"\n  threshold=0.3: {len(results_low)} results")
        print(f"  threshold=0.9: {len(results_high)} results")

        assert len(results_low) >= len(results_high), (
            "Higher threshold should return fewer or equal results"
        )


class TestDiversityFilter:
    """Test the Bhattacharyya histogram diversity filter."""

    @pytest.mark.asyncio
    async def test_filter_reduces_similar_images(self):
        """Filter should reduce near-duplicate crops."""
        from src.services.diversity_filter import DiversityFilter

        images = get_input_images()
        if len(images) < 3:
            pytest.skip("Need at least 3 images")

        # Use file:// URLs
        urls = [f"file://{p}" for p in images]

        f = DiversityFilter(threshold=0.10, max_images=10, min_images=1)
        filtered = await f.filter_image_urls(urls)

        print(f"\n  Input: {len(urls)} images → Output: {len(filtered)} images")
        assert len(filtered) >= 1, "Should keep at least min_images"
        assert len(filtered) <= 10, "Should not exceed max_images"

    @pytest.mark.asyncio
    async def test_filter_min_images_guarantee(self):
        """Even if all images are similar, min_images should be kept."""
        from src.services.diversity_filter import DiversityFilter

        # Use larger images that pass quality check
        images = [p for p in get_input_images() if os.path.getsize(p) > 10000]
        if len(images) < 2:
            pytest.skip("Need at least 2 large images for this test")
        urls = [f"file://{p}" for p in images[:3]]

        # Very high threshold — almost nothing passes diversity check
        f = DiversityFilter(threshold=0.99, max_images=10, min_images=1)
        filtered = await f.filter_image_urls(urls)

        print(f"\n  threshold=0.99: {len(filtered)}/{len(urls)} kept")
        assert len(filtered) >= 1, "min_images guarantee violated"

    @pytest.mark.asyncio
    async def test_filter_quality_rejection(self):
        """Images below min_dimension should be rejected."""
        from src.services.diversity_filter import DiversityFilter

        images = get_input_images()
        urls = [f"file://{p}" for p in images]

        # Very high min dimension — should reject small crops
        f = DiversityFilter(
            threshold=0.10,
            min_dimension=2000,
            max_images=10,
            min_images=0,
        )
        filtered = await f.filter_image_urls(urls)

        print(f"\n  With min_dimension=2000: {len(filtered)}/{len(urls)} kept")
        # Small crops (~1600-1900 bytes) are likely < 2000px, large JPGs should pass
        assert len(filtered) < len(urls), "High min_dimension should reject some images"


# DB tests moved to tests/test_db.py (CI-safe, no CLIP dependency)
