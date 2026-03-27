# Step 9: Diversity Filter (from deepface)

## Objective

Port the deepface diversity filter to avoid processing near-duplicate crop images from the same evidence. Runs **before** creating DB rows, filtering the image list down to visually distinct images.

## Where It Runs

```
Stream event arrives with image_urls: [img1, img2, img3, ..., img15]
       │
       ▼
  Diversity Filter (this step)
       │
       ▼
  Filtered: [img1, img4, img7, img11]   ← only visually distinct images
       │
       ▼
  Create DB row with filtered image_urls
       │
       ▼
  BatchTrigger.notify()
```

## Algorithm: Bhattacharyya Histogram Distance

1. Convert each image to **grayscale**
2. Resize to **224x224** pixels (normalize size)
3. Compute **64-bin histogram** of pixel intensity values (normalized)
4. Compare against previously selected images using **Bhattacharyya distance** via `cv2.compareHist`
5. If distance >= threshold (0.10) → image is "unique enough" → **keep**
6. If distance < threshold → too similar → **skip**

**Distance scale:** 0 = identical, 1 = completely different.

## Parameters

| Parameter | Default | Env Var | Description |
|-----------|---------|---------|-------------|
| `diversity_filter_threshold` | **0.10** | `DIVERSITY_FILTER_THRESHOLD` | Min Bhattacharyya distance to consider "different" |
| `diversity_filter_histogram_bins` | **64** | `DIVERSITY_FILTER_HISTOGRAM_BINS` | Histogram bins for pixel intensity |
| `diversity_filter_compare_all` | **False** | `DIVERSITY_FILTER_COMPARE_ALL` | Compare against last selected only (False) or all selected (True) |
| `diversity_filter_min_dimension` | **50** | `DIVERSITY_FILTER_MIN_DIMENSION` | Min width/height in pixels |
| `diversity_filter_max_aspect_ratio` | **5.0** | `DIVERSITY_FILTER_MAX_ASPECT_RATIO` | Max width:height ratio |
| `diversity_filter_max_images` | **10** | `DIVERSITY_FILTER_MAX_IMAGES` | Max images to select per evidence |
| `diversity_filter_min_images` | **1** | `DIVERSITY_FILTER_MIN_IMAGES` | Min images to keep (even if all similar) |

## Implementation

**New file:** `src/services/diversity_filter.py`

```python
import cv2
import numpy as np
import logging
from typing import List, Tuple, Optional
from PIL import Image
import io
import httpx

logger = logging.getLogger(__name__)


class DiversityFilter:
    def __init__(
        self,
        threshold: float = 0.10,
        histogram_bins: int = 64,
        compare_all: bool = False,
        min_dimension: int = 50,
        max_aspect_ratio: float = 5.0,
        max_images: int = 10,
        min_images: int = 1,
    ):
        self.threshold = threshold
        self.histogram_bins = histogram_bins
        self.compare_all = compare_all
        self.min_dimension = min_dimension
        self.max_aspect_ratio = max_aspect_ratio
        self.max_images = max_images
        self.min_images = min_images

    async def filter_image_urls(self, image_urls: List[str]) -> List[str]:
        """Download images and filter for diversity.

        Returns a subset of image_urls containing only visually distinct images.
        """
        if not image_urls:
            return []

        selected_urls = []
        selected_histograms = []
        rejected_urls = []  # Track for min_images fallback

        for url in image_urls:
            if len(selected_urls) >= self.max_images:
                break

            try:
                image_data = await self._download_image(url)
                if image_data is None:
                    continue

                # Quality pre-filter
                if not self._passes_quality_check(image_data):
                    continue

                # Compute histogram
                histogram = self._compute_histogram(image_data)

                # First image always selected
                if not selected_histograms:
                    selected_urls.append(url)
                    selected_histograms.append(histogram)
                    continue

                # Diversity check
                if self._is_unique(histogram, selected_histograms):
                    selected_urls.append(url)
                    selected_histograms.append(histogram)
                else:
                    rejected_urls.append(url)

            except Exception as e:
                logger.warning(f"Diversity filter error for {url}: {e}")
                continue

        # Ensure minimum images
        while len(selected_urls) < self.min_images and rejected_urls:
            selected_urls.append(rejected_urls.pop(0))

        logger.info(
            f"Diversity filter: {len(image_urls)} input → {len(selected_urls)} selected "
            f"(threshold={self.threshold})"
        )
        return selected_urls

    def _passes_quality_check(self, image: np.ndarray) -> bool:
        """Check minimum dimension and aspect ratio."""
        h, w = image.shape[:2]

        if min(h, w) < self.min_dimension:
            return False

        aspect = max(h, w) / min(h, w) if min(h, w) > 0 else 999
        if aspect > self.max_aspect_ratio:
            return False

        return True

    def _compute_histogram(self, image: np.ndarray) -> np.ndarray:
        """Compute normalized grayscale histogram."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        resized = cv2.resize(gray, (224, 224))
        hist = cv2.calcHist([resized], [0], None, [self.histogram_bins], [0, 256])
        cv2.normalize(hist, hist)
        return hist

    def _is_unique(self, histogram: np.ndarray, selected: List[np.ndarray]) -> bool:
        """Check if histogram is different enough from selected images."""
        if self.compare_all:
            # Must be different from ALL selected
            for ref_hist in selected:
                distance = cv2.compareHist(histogram, ref_hist, cv2.HISTCMP_BHATTACHARYYA)
                if distance < self.threshold:
                    return False
            return True
        else:
            # Compare only against last selected
            last = selected[-1]
            distance = cv2.compareHist(histogram, last, cv2.HISTCMP_BHATTACHARYYA)
            return distance >= self.threshold

    async def _download_image(self, url: str) -> Optional[np.ndarray]:
        """Download image and convert to numpy array."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
                image_bytes = response.content
                nparr = np.frombuffer(image_bytes, np.uint8)
                image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                return image
        except Exception as e:
            logger.warning(f"Failed to download {url}: {e}")
            return None


# ── Singleton ──

_filter_instance: Optional[DiversityFilter] = None

def get_diversity_filter() -> DiversityFilter:
    global _filter_instance
    if _filter_instance is None:
        from src.infrastructure.config import get_settings
        settings = get_settings()
        _filter_instance = DiversityFilter(
            threshold=settings.diversity_filter_threshold,
            histogram_bins=settings.diversity_filter_histogram_bins,
            compare_all=settings.diversity_filter_compare_all,
            min_dimension=settings.diversity_filter_min_dimension,
            max_aspect_ratio=settings.diversity_filter_max_aspect_ratio,
            max_images=settings.diversity_filter_max_images,
            min_images=settings.diversity_filter_min_images,
        )
    return _filter_instance
```

## Integration in Stream Consumer

```python
# In evidence_consumer.py → _process_evidence_embed()

async def _process_evidence_embed(payload: dict, message_id: str):
    evidence_id = payload["evidence_id"]
    camera_id = payload["camera_id"]
    raw_image_urls = payload.get("image_urls", [])

    # Apply diversity filter BEFORE creating DB row
    diversity = get_diversity_filter()
    filtered_urls = await diversity.filter_image_urls(raw_image_urls)

    if not filtered_urls:
        logger.warning(f"No images passed diversity filter for evidence {evidence_id}")
        return

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)

        if await repo.check_duplicate(evidence_id):
            return

        request = await repo.create_request(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=filtered_urls,       # ← only diverse images
            stream_msg_id=message_id,
        )
        await session.commit()

    trigger = get_batch_trigger("embedding")
    if trigger:
        await trigger.notify(count=1)
```

## New Dependency

```
# Add to requirements.txt
opencv-python-headless>=4.8.0    # For histogram computation (no GUI needed)
```

## Example

```
Evidence with 15 crop images arrives:
  img01.jpg ← selected (first image always kept)
  img02.jpg ← distance 0.03 from img01 → REJECTED (too similar)
  img03.jpg ← distance 0.05 from img01 → REJECTED
  img04.jpg ← distance 0.14 from img01 → SELECTED
  img05.jpg ← distance 0.02 from img04 → REJECTED
  img06.jpg ← distance 0.08 from img04 → REJECTED
  img07.jpg ← distance 0.22 from img04 → SELECTED
  img08.jpg ← distance 0.04 from img07 → REJECTED
  img09.jpg ← distance 0.31 from img07 → SELECTED
  ...

Result: 4-6 diverse images out of 15 → faster processing, less storage
```
