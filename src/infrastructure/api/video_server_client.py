"""Video Server API client implementation."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx

from ...domain.entities import Evidence, ImageSearch
from ...domain.repositories import EvidenceRepository, ImageSearchRepository
from ..config import Settings

logger = logging.getLogger(__name__)


class VideoServerClient(EvidenceRepository, ImageSearchRepository):
    """Client for Video Server API implementing repository interfaces."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = str(settings.video_server_base_url).rstrip("/")
        self.api_key = settings.video_server_api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),  # 60s read timeout, 10s connect timeout
            headers={"X-API-Key": self.api_key},
        )

    # EvidenceRepository implementation

    async def get_unembedded_evidences(self, limit: int = 50) -> list[Evidence]:
        """Get evidences with status=3 (FOUND) that need embedding."""
        try:
            url = f"{self.base_url}/api/v1/evidences/internal/evidences/for-embedding"
            response = await self.client.get(url, params={"limit": limit})
            response.raise_for_status()

            data = response.json()
            evidences = []

            for item in data.get("evidences", []):
                evidence = Evidence(
                    id=UUID(item["id"]),
                    camera_id=UUID(item["camera_id"]),
                    status=item["status"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                    json_data=item.get("json_data"),  # Store the full json_data
                    updated_at=datetime.fromisoformat(item["updated_at"])
                    if item.get("updated_at")
                    else None,
                    processed_at=datetime.fromisoformat(item["processed_at"])
                    if item.get("processed_at")
                    else None,
                )
                evidences.append(evidence)

            logger.info(f"Retrieved {len(evidences)} unembedded evidences")
            return evidences

        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting evidences: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to get unembedded evidences: {e}")
            return []

    async def mark_evidence_as_embedded(self, evidence_id: UUID, embedding_ids: list[str]) -> bool:
        """Update evidence status to 4 (EMBEDDED) with embedding IDs."""
        try:
            url = f"{self.base_url}/api/v1/evidences/internal/evidences/{evidence_id}/embedded"
            response = await self.client.patch(
                url,
                json={"embedding_ids": embedding_ids},  # Send list of IDs
            )
            response.raise_for_status()

            logger.info(
                f"Marked evidence {evidence_id} as embedded with {len(embedding_ids)} embeddings"
            )
            return True

        except httpx.HTTPError as e:
            logger.error(f"HTTP error marking evidence as embedded: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to mark evidence {evidence_id} as embedded: {e}")
            return False

    # ImageSearchRepository implementation

    async def get_pending_searches(self, limit: int = 10) -> list[ImageSearch]:
        """Get image searches with status=1 (TO_WORK)."""
        try:
            url = f"{self.base_url}/api/v1/internal/image-search/pending"
            response = await self.client.get(url, params={"limit": limit})
            response.raise_for_status()

            data = response.json()
            searches = []

            for item in data.get("searches", []):
                search = ImageSearch(
                    id=UUID(item["id"]),
                    user_id=UUID(item["user_id"]),
                    image_url=item["image_url"],
                    search_status=item["search_status"],
                    similarity_status=item["similarity_status"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                    updated_at=datetime.fromisoformat(item["updated_at"])
                    if item.get("updated_at")
                    else None,
                    processed_at=datetime.fromisoformat(item["processed_at"])
                    if item.get("processed_at")
                    else None,
                    metadata=item.get("metadata"),
                    results_key=item.get("results_key"),
                    total_matches=item.get("total_matches", 0),
                )
                searches.append(search)

            logger.info(f"Retrieved {len(searches)} pending searches")
            return searches

        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting pending searches: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to get pending searches: {e}")
            return []

    async def update_search_status(
        self,
        search_id: UUID,
        search_status: int,
        similarity_status: int | None = None,
        total_matches: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Update search processing status."""
        try:
            url = f"{self.base_url}/api/v1/internal/image-search/{search_id}/status"

            payload = {"search_status": search_status}
            if similarity_status is not None:
                payload["similarity_status"] = similarity_status
            if total_matches is not None:
                payload["total_matches"] = total_matches
            if metadata is not None:
                payload["metadata"] = metadata
                logger.debug(f"Sending metadata with {len(metadata)} keys: {list(metadata.keys())}")
            if search_status == 3:  # COMPLETED
                payload["processed_at"] = datetime.utcnow().isoformat()

            logger.info(f"Updating search {search_id} with payload keys: {list(payload.keys())}")
            response = await self.client.patch(url, json=payload)
            response.raise_for_status()

            logger.info(f"Updated search {search_id} status to {search_status}")
            return True

        except httpx.HTTPError as e:
            logger.error(f"HTTP error updating search status: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to update search {search_id} status: {e}")
            return False

    async def store_search_results(
        self, search_id: UUID, results: dict[str, Any], ttl: int = 3600
    ) -> bool:
        """Store search results in Redis cache via API."""
        try:
            url = f"{self.base_url}/api/v1/internal/redis/image-search/{search_id}"

            # Results should be a dictionary with the complete Redis data structure
            # It already contains search_id, search_image_url, results, etc.
            if isinstance(results, dict):
                payload = results  # Already in correct format
            else:
                # Fallback if results is not a dict (shouldn't happen)
                payload = {
                    "search_id": str(search_id),
                    "search_image_url": "",
                    "total_matches": 0,
                    "results": [],
                    "processed_at": datetime.utcnow().isoformat(),
                }

            response = await self.client.post(url, json=payload)
            response.raise_for_status()

            total_matches = payload.get("total_matches", 0)
            logger.info(f"Stored {total_matches} results for search {search_id}")
            return True

        except httpx.HTTPError as e:
            logger.error(f"HTTP error storing search results: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to store results for search {search_id}: {e}")
            return False

    async def cleanup(self) -> None:
        """Clean up HTTP client."""
        await self.client.aclose()
