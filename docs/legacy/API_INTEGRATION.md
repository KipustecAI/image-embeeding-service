# API Integration Guide for Video Server

## Current Qdrant Payload Structure

Currently, when we store evidence embeddings in Qdrant, we store the following metadata:

```json
{
  "evidence_id": "uuid-string",
  "camera_id": "uuid-string", 
  "image_index": 0,
  "total_images": 3,
  "image_url": "https://example.com/image.jpg",
  "created_at": "2025-08-28T17:00:00Z"
}
```

## Future Enhancements (TODO)

We can extend the metadata to include:
- `text_description`: Text caption/description from OCR or AI analysis
- `object_types`: List of detected objects ["person", "vehicle", etc.]
- `location`: GPS coordinates {"lat": float, "lon": float}
- `scene_type`: Indoor/outdoor/street/parking/etc.
- `confidence_scores`: Object detection confidence scores

## Triggering Recalculation from Core Video Server

The core video server can trigger image search recalculation using the following endpoints:

### 1. Force Recalculate All Searches

```bash
# From the core video server, call the embedding service endpoint
curl -X POST http://localhost:8001/api/v1/recalculate/searches?force_all=true
```

### 2. Recalculate Specific Searches

```bash
# Recalculate by specific IDs
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?search_ids=uuid1&search_ids=uuid2"

# Recalculate with time filter (older than 2 hours)
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?hours_old=2&limit=20"
```

### 3. Recalculate Single Search

```bash
curl -X POST http://localhost:8001/api/v1/recalculate/search/{search_id}
```

## Creating Image Search with Filters from Video Server

When the video server creates a new image search, it can pass metadata filters that will be used during the search:

### Endpoint: Create Image Search

```http
POST http://localhost:8000/api/v1/image-search/upload
```

### Request Body with Filters

```json
{
  "image_url": "https://example.com/search_image.jpg",
  "metadata": {
    "text_description": "red car near building",
    "camera_id": "550e8400-e29b-41d4-a716-446655440000",
    "object_type": "vehicle",
    "date_from": "2025-08-01T00:00:00Z",
    "date_to": "2025-08-31T23:59:59Z"
  }
}
```

### Currently Supported Filters

The embedding service is configured to extract and use these filters from the metadata:
- `text_description`: Filter by text/caption (when available in Qdrant payload)
- `camera_id`: Filter results from specific camera
- `object_type`: Filter by detected object type
- `date_from` and `date_to`: Filter by date range

### Example: Complete Flow from Video Server

```python
import httpx
import asyncio

async def create_filtered_search():
    # 1. Create image search with filters
    async with httpx.AsyncClient() as client:
        # Create search request with metadata filters
        response = await client.post(
            "http://localhost:8000/api/v1/image-search/upload",
            headers={"Authorization": "Bearer <user-token>"},
            json={
                "image_url": "https://example.com/search_image.jpg",
                "metadata": {
                    "camera_id": "550e8400-e29b-41d4-a716-446655440000",
                    "text_description": "red vehicle",
                    "date_from": "2025-08-01"
                }
            }
        )
        
        search_id = response.json()["id"]
        
        # 2. Wait for processing (or check status)
        await asyncio.sleep(5)
        
        # 3. Get results
        response = await client.get(
            f"http://localhost:8000/api/v1/image-search/{search_id}",
            headers={"Authorization": "Bearer <user-token>"}
        )
        
        results = response.json()
        print(f"Found {results['total_matches']} matches with filters")
        
        # 4. Later, trigger recalculation if needed
        response = await client.post(
            f"http://localhost:8001/api/v1/recalculate/search/{search_id}"
        )
```

## Triggering from Video Server Backend

You can add a service method in the video server to trigger recalculation:

```python
# In video_server/src/core/image_search/image_search_service.py

async def trigger_search_recalculation(
    self,
    search_ids: Optional[List[UUID]] = None,
    force_all: bool = False,
    hours_old: Optional[int] = None
) -> dict:
    """Trigger recalculation of image searches in embedding service."""
    
    async with httpx.AsyncClient() as client:
        params = {}
        
        if force_all:
            params["force_all"] = True
        elif search_ids:
            params["search_ids"] = [str(id) for id in search_ids]
        else:
            params["limit"] = 10
            if hours_old:
                params["hours_old"] = hours_old
        
        response = await client.post(
            f"{EMBEDDING_SERVICE_URL}/api/v1/recalculate/searches",
            params=params
        )
        
        return response.json()
```

## Integration with Scheduled Tasks

You can also add a scheduled task in the video server to trigger recalculation periodically:

```python
# In video_server worker or scheduler

@repeat(every=3600)  # Every hour
async def recalculate_old_searches():
    """Trigger recalculation for searches older than 2 hours."""
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://embedding-service:8001/api/v1/recalculate/searches",
            params={"hours_old": 2, "limit": 50}
        )
```

## Response Format

All recalculation endpoints return:

```json
{
  "success": true,
  "message": "Recalculated 5 searches",
  "total_processed": 5,
  "successful": 5,
  "failed": 0,
  "mode": "batch",
  "search_ids": ["uuid1", "uuid2", ...]  // When specific IDs requested
}
```

## Notes

1. **Authentication**: The embedding service endpoints are public. Add authentication if needed.
2. **Rate Limiting**: Consider adding rate limits for recalculation endpoints.
3. **Async Processing**: All recalculations are processed asynchronously.
4. **Filters**: Filters only work if the corresponding metadata exists in Qdrant payloads.
5. **Performance**: Recalculation is expensive - use time filters and limits wisely.