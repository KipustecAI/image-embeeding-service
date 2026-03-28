"""Upload images to the storage service (MinIO via Docker network DNS)."""

import logging

import httpx

logger = logging.getLogger(__name__)


class StorageUploader:
    def __init__(self, base_url: str = "http://storage-service:8006"):
        self.base_url = base_url
        self.upload_url = f"{base_url}/api/v1/upload/file"

    async def upload_image(
        self,
        image_bytes: bytes,
        filename: str,
        folder: str,
        user_id: str = "embedding-service",
    ) -> str | None:
        """Upload image to storage service, return public_url or None on failure."""
        try:
            headers = {
                "X-User-Id": user_id,
                "X-User-Role": "dev",
            }
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.upload_url,
                    headers=headers,
                    files={"file": (filename, image_bytes, "image/jpeg")},
                    data={"folder": folder, "storage": "minio"},
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        return data["public_url"]
                    logger.warning(f"Storage upload not successful: {data}")
                else:
                    logger.warning(
                        f"Storage upload failed ({response.status_code}): {response.text[:200]}"
                    )
        except Exception as e:
            logger.error(f"Storage upload error for {filename}: {e}")
        return None
