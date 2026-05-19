"""Upload images to the storage service (MinIO via Docker network DNS)."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# httpx timeout exceptions and other transient network errors carry an empty
# str(), so we must log type(e).__name__ to make these visible in operations.
_RETRIABLE_EXC = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class StorageUploader:
    def __init__(
        self,
        base_url: str = "http://storage-service:8006",
        timeout_seconds: float = 60.0,
        max_attempts: int = 3,
    ):
        self.base_url = base_url
        self.upload_url = f"{base_url}/api/v1/upload/file"
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    async def upload_image(
        self,
        image_bytes: bytes,
        filename: str,
        folder: str,
        user_id: str = "embedding-service",
    ) -> str | None:
        """Upload image to storage service, return public_url or None on failure."""
        headers = {
            "X-User-Id": user_id,
            "X-User-Role": "dev",
        }
        last_exc_label: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
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
                    return None
                if 500 <= response.status_code < 600 and attempt < self.max_attempts:
                    logger.warning(
                        f"Storage upload {response.status_code} for {filename} "
                        f"(attempt {attempt}/{self.max_attempts}); retrying"
                    )
                    await asyncio.sleep(0.5 * attempt)
                    continue
                logger.warning(
                    f"Storage upload failed ({response.status_code}) for {filename}: "
                    f"{response.text[:200]}"
                )
                return None
            except _RETRIABLE_EXC as e:
                last_exc_label = f"{type(e).__name__}: {e}"
                if attempt < self.max_attempts:
                    logger.warning(
                        f"Storage upload transient error for {filename} "
                        f"(attempt {attempt}/{self.max_attempts}): {last_exc_label}"
                    )
                    await asyncio.sleep(0.5 * attempt)
                    continue
            except Exception as e:
                logger.error(
                    f"Storage upload error for {filename}: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                return None
        logger.error(
            f"Storage upload exhausted retries for {filename}: {last_exc_label}"
        )
        return None
