"""Image diversity filter using Bhattacharyya histogram distance.

Filters near-duplicate crop images before processing.
Ported from deepface-restapi src/services/diversity_filter.py.
"""

import logging
from typing import List, Optional

import cv2
import httpx
import numpy as np

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
        """Download images and return only visually distinct ones."""
        if not image_urls:
            return []

        selected_urls: List[str] = []
        selected_histograms: List[np.ndarray] = []
        rejected_urls: List[str] = []

        for url in image_urls:
            if len(selected_urls) >= self.max_images:
                break

            try:
                image = await self._download_image(url)
                if image is None:
                    continue

                if not self._passes_quality_check(image):
                    continue

                histogram = self._compute_histogram(image)

                # First image always selected
                if not selected_histograms:
                    selected_urls.append(url)
                    selected_histograms.append(histogram)
                    continue

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
        h, w = image.shape[:2]
        if min(h, w) < self.min_dimension:
            return False
        aspect = max(h, w) / min(h, w) if min(h, w) > 0 else 999
        if aspect > self.max_aspect_ratio:
            return False
        return True

    def _compute_histogram(self, image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        resized = cv2.resize(gray, (224, 224))
        hist = cv2.calcHist([resized], [0], None, [self.histogram_bins], [0, 256])
        cv2.normalize(hist, hist)
        return hist

    def _is_unique(self, histogram: np.ndarray, selected: List[np.ndarray]) -> bool:
        if self.compare_all:
            for ref_hist in selected:
                distance = cv2.compareHist(histogram, ref_hist, cv2.HISTCMP_BHATTACHARYYA)
                if distance < self.threshold:
                    return False
            return True
        else:
            last = selected[-1]
            distance = cv2.compareHist(histogram, last, cv2.HISTCMP_BHATTACHARYYA)
            return distance >= self.threshold

    async def _download_image(self, url: str) -> Optional[np.ndarray]:
        try:
            if url.startswith("file://"):
                path = url[7:]  # Strip file:// prefix
                image = cv2.imread(path, cv2.IMREAD_COLOR)
                return image
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
                nparr = np.frombuffer(response.content, np.uint8)
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
        from ..infrastructure.config import get_settings

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
