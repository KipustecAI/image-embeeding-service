"""Download ZIP and extract specific images by name."""

import io
import logging
import zipfile

import httpx

logger = logging.getLogger(__name__)


class ZipProcessor:
    async def download_and_extract(
        self,
        zip_url: str,
        image_names: list[str],
    ) -> dict[str, bytes]:
        """Download ZIP, extract only the named images, return {name: bytes}."""
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(zip_url)
            response.raise_for_status()

        image_map: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in image_names:
                # Match by filename (ignoring directory prefix in ZIP)
                matching = [n for n in zf.namelist() if n.endswith(name)]
                if matching:
                    image_map[name] = zf.read(matching[0])
                else:
                    logger.warning(f"Image '{name}' not found in ZIP from {zip_url}")

        return image_map
