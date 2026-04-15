# ETL Integration: New Fields + ZIP Pipeline + Storage Upload

## Context

The ETL service publishes a richer payload to `evidence:embed` than what we currently handle. We need to:
1. Accept the full ETL payload (with `zip_url` instead of `image_urls`)
2. Store new metadata fields (`user_id`, `device_id`, `app_id`, `infraction_code`)
3. Upload diversity-filtered images to the storage service and store permanent URLs
4. Add `user_id` to Qdrant for multi-tenant search isolation

## Current vs Target

### Stream payload: evidence:embed

| Field | Current | Target (ETL contract) |
|-------|---------|----------------------|
| `evidence_id` | Yes | Yes |
| `camera_id` | Yes | Yes |
| `image_urls` | ~~Yes~~ (removed) | **No** — replaced by `zip_url` |
| `zip_url` | No | **Yes** |
| `user_id` | No | **Yes** |
| `device_id` | No | **Yes** |
| `app_id` | No | **Yes** |
| `infraction_code` | No | **Yes** |
| `config_id` | No | Available (optional) |
| `event_type` (int) | No | Available (optional) |
| `entities` | No | Available (optional) |
| Event type string | `evidence.ready.embed` | `evidence.created.embed` |

### Stream payload: embeddings:results (compute → backend)

Current:
```json
{
  "evidence_id": "...",
  "camera_id": "...",
  "embeddings": [{"image_url": "https://...", "vector": [...], "image_index": 0}],
  "input_count": 3, "filtered_count": 2, "embedded_count": 2
}
```

Target:
```json
{
  "evidence_id": "...",
  "camera_id": "...",
  "user_id": "...",
  "device_id": "...",
  "app_id": 1,
  "infraction_code": "...",
  "zip_url": "https://minio.lookia.mx/lucam-assets/infractions/.../evidence.zip",
  "embeddings": [
    {"image_name": "frame_001.jpg", "vector": [...], "image_index": 0},
    {"image_name": "frame_003.jpg", "vector": [...], "image_index": 1}
  ],
  "input_count": 5, "filtered_count": 2, "embedded_count": 2
}
```

Key changes:
- `image_url` → `image_name` (filename inside the ZIP, not a URL)
- New fields passed through: `user_id`, `device_id`, `app_id`, `infraction_code`, `zip_url`
- Backend is responsible for uploading images and generating permanent URLs

---

## End-to-End Flow

```
ETL Service
     │
     └─ XADD evidence:embed
        {evidence_id, user_id, camera_id, device_id, app_id,
         infraction_code, zip_url, ...}
              │
              ▼
embedding-compute (GPU)
     1. Download ZIP from zip_url
     2. Extract image files (jpg, png)
     3. Diversity filter → selects N images by filename
     4. CLIP inference on selected images → N vectors
     5. XADD embeddings:results
        {evidence_id, user_id, camera_id, device_id, app_id,
         infraction_code, zip_url,
         embeddings: [{image_name: "frame_001.jpg", vector: [...], image_index: 0}, ...]}
              │
              ▼
image-embeeding-service (Backend)
     1. Receive embeddings:results
     2. Download ZIP from zip_url
     3. Extract ONLY the images listed in embeddings[].image_name
     4. Upload each image to storage service:
        POST http://storage-service:8006/api/v1/upload/file
          Headers: X-User-Id: <user_id or "embedding-service">
                   X-User-Role: dev
          file=<image bytes>
          folder=embeddings/{camera_id}/{evidence_id}
        → response: {public_url: "http://minio:9000/lucam-assets/embeddings/.../frame_001.jpg"}
     5. For each embedding:
        - Upsert vector to Qdrant evidence_embeddings with payload:
          {evidence_id, user_id, camera_id, device_id, app_id,
           image_url: <public_url>, image_index, source_type: "evidence"}
        - Create evidence_embedding DB record
     6. Create/update embedding_request row with:
        - user_id, device_id, app_id, infraction_code
        - image_urls = [<public_url>, <public_url>, ...]  (the uploaded images)
     7. XACK
```

---

## Changes by Service

### 1. embedding-compute (GPU) — ../embedding-compute/

#### `src/streams/evidence_handler.py`

- Accept `zip_url` field from payload
- Download ZIP, extract images to temp directory
- Run diversity filter on extracted images
- Run CLIP on filtered images
- Publish `image_name` (filename inside ZIP) instead of `image_url`
- Pass through new fields: `user_id`, `device_id`, `app_id`, `infraction_code`, `zip_url`
- Register handler for `evidence.created.embed` (in addition to `evidence.ready.embed` for backwards compat)

```python
async def _process_evidence(payload, message_id):
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    zip_url = payload.get("zip_url")
    if not evidence_id or not zip_url:
        logger.warning(f"Skipping: missing evidence_id or zip_url")
        return

    # Download ZIP → extract images → diversity filter → CLIP
    extracted_images = await download_and_extract_zip(zip_url)
    filtered = diversity_filter(extracted_images)
    vectors = clip_inference(filtered)
    embeddings = [{"image_name": img.filename, "vector": v, "image_index": i}
                  for i, (img, v) in enumerate(zip(filtered, vectors))]

    producer.publish(output_stream, {
        "evidence_id": evidence_id,
        "camera_id": camera_id,
        "user_id": payload.get("user_id"),
        "device_id": payload.get("device_id"),
        "app_id": payload.get("app_id"),
        "infraction_code": payload.get("infraction_code"),
        "zip_url": zip_url,
        "embeddings": embeddings,
        ...
    })
```

#### `src/utils/zip_handler.py` (NEW)

```python
async def download_and_extract_zip(zip_url: str) -> list[ExtractedImage]:
    """Download ZIP, extract image files, return list of (filename, PIL.Image)."""
```

### 2. image-embeeding-service (Backend) — this repo

#### Alembic migration

Add columns to `embedding_requests`:

```python
op.add_column('embedding_requests', sa.Column('user_id', sa.String(255), nullable=True))
op.add_column('embedding_requests', sa.Column('device_id', sa.String(255), nullable=True))
op.add_column('embedding_requests', sa.Column('app_id', sa.Integer(), nullable=True))
op.add_column('embedding_requests', sa.Column('infraction_code', sa.String(255), nullable=True))
```

#### `src/db/models/embedding_request.py`

```python
user_id = Column(String(255), nullable=True, index=True)
device_id = Column(String(255), nullable=True)
app_id = Column(Integer, nullable=True)
infraction_code = Column(String(255), nullable=True, index=True)
```

#### `src/infrastructure/vector_db/qdrant_repository.py`

Add payload indices on `evidence_embeddings` collection initialization:

```python
# New indices for multi-tenant filtering
self.client.create_payload_index(
    collection_name=self.collection_name,
    field_name="user_id",
    field_schema="keyword",
)
self.client.create_payload_index(
    collection_name=self.collection_name,
    field_name="device_id",
    field_schema="keyword",
)
self.client.create_payload_index(
    collection_name=self.collection_name,
    field_name="app_id",
    field_schema="integer",
)
```

#### `src/services/storage_uploader.py` (NEW)

Uploads images to the storage service via Docker network DNS (direct, no gateway).

```python
import httpx

class StorageUploader:
    def __init__(self, base_url: str = "http://storage-service:8006"):
        self.base_url = base_url
        self.upload_url = f"{base_url}/api/v1/upload/file"

    async def upload_image(
        self, image_bytes: bytes, filename: str, folder: str, user_id: str = "embedding-service"
    ) -> str | None:
        """Upload image to storage service, return public_url."""
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.upload_url,
                headers={
                    "X-User-Id": user_id,
                    "X-User-Role": "dev",
                },
                files={"file": (filename, image_bytes, "image/jpeg")},
                data={"folder": folder, "storage": "minio"},
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    return data["public_url"]
        return None
```

The storage service normally lives behind the API Gateway (which injects user context headers), but this service talks to it **directly** on the Docker network, so we must inject `X-User-Id` and `X-User-Role` ourselves.

#### `src/services/zip_processor.py` (NEW)

Downloads ZIP and extracts specific images by name.

```python
import httpx
import zipfile
import io

class ZipProcessor:
    async def download_and_extract(
        self, zip_url: str, image_names: list[str]
    ) -> dict[str, bytes]:
        """Download ZIP, extract only the named images, return {name: bytes}."""
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(zip_url)
            response.raise_for_status()

        image_map = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in image_names:
                # Match by filename (ignoring directory prefix in ZIP)
                matching = [n for n in zf.namelist() if n.endswith(name)]
                if matching:
                    image_map[name] = zf.read(matching[0])

        return image_map
```

#### `src/streams/embedding_results_consumer.py`

Update `_process_embeddings_result` to handle the new flow:

```python
async def _process_embeddings_result(payload, message_id):
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    zip_url = payload.get("zip_url")
    user_id = payload.get("user_id")
    device_id = payload.get("device_id")
    app_id = payload.get("app_id")
    infraction_code = payload.get("infraction_code")
    embeddings_data = payload.get("embeddings", [])

    uploaded_urls = []

    if zip_url:
        # New flow: download ZIP, extract filtered images, upload to storage
        image_names = [e["image_name"] for e in embeddings_data if "image_name" in e]
        image_map = await zip_processor.download_and_extract(zip_url, image_names)

        folder = f"embeddings/{camera_id}/{evidence_id}"
        for name, img_bytes in image_map.items():
            public_url = await storage_uploader.upload_image(img_bytes, name, folder)
            if public_url:
                uploaded_urls.append((name, public_url))

    # Map image_name → public_url
    url_map = dict(uploaded_urls)

    for emb in embeddings_data:
        image_name = emb.get("image_name", "")
        image_url = url_map.get(image_name, "")

        # Upsert to Qdrant with new metadata fields
        point_id = str(uuid4())
        await vector_repo.store_embedding(ImageEmbedding(
            id=point_id,
            vector=np.array(emb["vector"]),
            metadata={
                "evidence_id": evidence_id,
                "camera_id": camera_id,
                "user_id": user_id,
                "device_id": device_id,
                "app_id": app_id,
                "image_url": image_url,
                "image_index": emb.get("image_index", 0),
                "source_type": "evidence",
            },
        ))

    # Create DB record with new fields
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if not await repo.check_duplicate(evidence_id):
            await repo.create_request(
                evidence_id=evidence_id,
                camera_id=camera_id,
                user_id=user_id,
                device_id=device_id,
                app_id=app_id,
                infraction_code=infraction_code,
                image_urls=[url for _, url in uploaded_urls],
                stream_msg_id=message_id,
            )
```

#### `src/main.py` — Search API multi-tenant enforcement

```python
@app.post("/api/v1/search", status_code=202)
async def create_search(body: SearchCreateRequest, ctx: UserContext = Depends(get_user_context)):
    metadata = body.metadata or {}

    # Multi-tenant: non-admin users can only search their own evidence
    if ctx.role not in ("admin", "root", "dev"):
        metadata["user_id"] = ctx.user_id

    # Publish to GPU with enforced metadata
    stream_producer.publish(
        stream=settings.stream_evidence_search,
        event_type="search.created",
        payload={..., "metadata": metadata},
    )
```

#### `src/infrastructure/config.py`

Add storage service URL:

```python
storage_service_url: str = Field(
    "http://storage-service:8006",
    validation_alias="STORAGE_SERVICE_URL",
)
```

#### `.env` / `.env.dev` / `.env.example`

```
STORAGE_SERVICE_URL=http://storage-service:8006  # Docker network DNS (direct, no gateway)
# For local dev (if storage service runs on host):
# STORAGE_SERVICE_URL=http://localhost:8006
```

---

## Qdrant Payload (per vector point)

```json
{
  "evidence_id": "e2aa7d67-0377-4905-a923-2bc2b4fa9bc7",
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",
  "camera_id": "54c398ab-ea0d-4085-875b-af816eb00b03",
  "device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "app_id": 1,
  "image_url": "http://minio:9000/lucam-assets/embeddings/54c398ab.../e2aa7d67.../frame_001.jpg",
  "image_index": 0,
  "source_type": "evidence"
}
```

Payload indices: `evidence_id`, `camera_id`, `user_id`, `device_id`, `app_id`, `source_type`

---

## Multi-Tenant Search Access Control

| Caller role | Behavior |
|-------------|----------|
| `user` | API auto-injects `user_id` from `X-User-Id` header → only sees own evidence |
| `admin`, `root`, `dev` | No forced filter → sees all evidence. Can optionally filter by `user_id` |
| No `user_id` filter | Qdrant returns ALL evidence (for admins) |
| `user_id` + `camera_id` | Both filters applied (intersection) |

---

---

## Files to Create/Modify

### Backend (this repo)

| File | Action |
|------|--------|
| `alembic/versions/xxx_add_etl_fields.py` | **Create** — migration for user_id, device_id, app_id, infraction_code |
| `src/db/models/embedding_request.py` | **Modify** — add new columns |
| `src/db/repositories/embedding_request_repo.py` | **Modify** — accept new fields in create_request |
| `src/services/storage_uploader.py` | **Create** — upload images to storage service |
| `src/services/zip_processor.py` | **Create** — download ZIP + extract specific images |
| `src/infrastructure/config.py` | **Modify** — add STORAGE_SERVICE_URL |
| `src/infrastructure/vector_db/qdrant_repository.py` | **Modify** — add payload indices |
| `src/streams/embedding_results_consumer.py` | **Modify** — handle new fields + ZIP upload flow |
| `src/main.py` | **Modify** — multi-tenant enforcement in search endpoint |
| `.env`, `.env.dev`, `.env.example` | **Modify** — add STORAGE_SERVICE_URL |

### Compute (../embedding-compute/)

| File | Action |
|------|--------|
| `src/streams/evidence_handler.py` | **Modify** — handle zip_url, pass through new fields, publish image_name |
| `src/utils/zip_handler.py` | **Create** — download + extract ZIP |
| `src/config.py` | **Modify** — add zip download settings if needed |

---

## Verification

1. Run migration: `make migrate`
2. Start storage service (docker)
3. Start compute + backend
4. Publish test event with `zip_url`:
   ```bash
   redis-cli -n 3 XADD evidence:embed '*' \
     event_type evidence.created.embed \
     payload '{"evidence_id":"test-001","user_id":"user-001","camera_id":"cam-001","device_id":"dev-001","app_id":1,"infraction_code":"INF-001","zip_url":"https://minio.lookia.mx/lucam-assets/infractions/.../evidence.zip"}'
   ```
5. Verify:
   - Compute extracts ZIP, filters, publishes embeddings with `image_name`
   - Backend downloads ZIP, uploads filtered images to storage
   - `embedding_requests` row has `user_id`, `device_id`, `app_id`, `infraction_code`
   - `image_urls` contains permanent MinIO URLs
   - Qdrant points have `user_id`, `device_id`, `app_id` in payload
6. Search as regular user → only sees own evidence
7. Search as admin → sees all evidence
