# Step 2: Database Models

## Objective

Create SQLAlchemy models that track the full processing lifecycle locally, mirroring the deepface `detection_requests` + `face_embeddings` pattern.

## Status Constants

**New file:** `src/db/models/constants.py`

```python
class EmbeddingRequestStatus:
    TO_WORK = 1          # Created, waiting for processing
    WORKING = 2          # Picked up by worker
    EMBEDDED = 3         # CLIP vectors stored in Qdrant
    DONE = 4             # Notified Video Server successfully
    ERROR = 5            # Failed (see error_message)

class SearchRequestStatus:
    TO_WORK = 1
    WORKING = 2
    COMPLETED = 3
    ERROR = 4

class SimilarityStatus:
    NO_MATCHES = 1
    MATCHES_FOUND = 2
```

## Model: embedding_requests

**New file:** `src/db/models/embedding_request.py`

Tracks each evidence that needs embedding. One row per evidence (which may have multiple images).

```python
class EmbeddingRequest(Base):
    __tablename__ = "embedding_requests"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evidence_id   = Column(String(255), nullable=False, index=True)     # Video Server evidence UUID
    camera_id     = Column(String(255), nullable=False, index=True)
    status        = Column(Integer, nullable=False, default=1, index=True)
    image_urls    = Column(JSONB, default=[])                           # List of crop URLs

    # Worker tracking
    worker_id               = Column(String(100))
    error_message           = Column(Text)
    retry_count             = Column(Integer, default=0)
    processing_started_at   = Column(DateTime)
    processing_completed_at = Column(DateTime)

    # Stream metadata
    stream_message_id       = Column(String(100))                       # Redis Stream message ID

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    embeddings = relationship("EvidenceEmbedding", back_populates="request", cascade="all, delete-orphan")
```

**Indexes:**
- `status` — safety net queries: `WHERE status = 1`
- `evidence_id` — dedup check: `WHERE evidence_id = ?`
- `camera_id` — analytics
- `created_at` — FIFO ordering

## Model: evidence_embeddings

**New file:** `src/db/models/evidence_embedding.py`

One row per image embedded. An evidence with 3 crop images produces 3 rows.

```python
class EvidenceEmbeddingRecord(Base):
    __tablename__ = "evidence_embeddings"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id      = Column(UUID(as_uuid=True), ForeignKey("embedding_requests.id"), nullable=False)
    qdrant_point_id = Column(String(255), unique=True, index=True)      # Reference to Qdrant
    image_index     = Column(Integer, default=0)                        # Which image in the evidence
    image_url       = Column(Text, nullable=False)
    json_data       = Column(JSONB)                                     # Extra metadata stored in Qdrant

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    request = relationship("EmbeddingRequest", back_populates="embeddings")
```

## Model: search_requests

**New file:** `src/db/models/search_request.py`

Tracks each similarity search lifecycle locally.

```python
class SearchRequest(Base):
    __tablename__ = "search_requests"

    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    search_id         = Column(String(255), nullable=False, index=True)  # Video Server search UUID
    user_id           = Column(String(255), nullable=False, index=True)
    image_url         = Column(Text, nullable=False)
    status            = Column(Integer, nullable=False, default=1, index=True)
    similarity_status = Column(Integer, default=1)                       # NO_MATCHES initially

    # Search parameters
    threshold   = Column(Float, default=0.75)
    max_results = Column(Integer, default=50)
    metadata    = Column(JSONB)                                          # camera_id filters, date range

    # Results
    total_matches = Column(Integer, default=0)
    results_key   = Column(String(255))                                  # Redis cache key

    # Worker tracking
    worker_id               = Column(String(100))
    error_message           = Column(Text)
    retry_count             = Column(Integer, default=0)
    processing_started_at   = Column(DateTime)
    processing_completed_at = Column(DateTime)

    # Stream metadata
    stream_message_id = Column(String(100))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

## Repository Layer

**New file:** `src/db/repositories/embedding_request_repo.py`

Follows deepface's `DetectionRequestRepository` pattern with FOR UPDATE SKIP LOCKED:

```python
class EmbeddingRequestRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_pending_requests(self, limit: int = 20) -> List[EmbeddingRequest]:
        """Get TO_WORK requests with row-level locking to prevent double-pickup."""
        query = (
            select(EmbeddingRequest)
            .where(
                and_(
                    EmbeddingRequest.status == EmbeddingRequestStatus.TO_WORK,
                    EmbeddingRequest.retry_count < 3,
                )
            )
            .order_by(EmbeddingRequest.created_at.asc())   # FIFO
            .limit(limit)
            .with_for_update(skip_locked=True)              # Prevents double-pickup
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def check_duplicate(self, evidence_id: str) -> bool:
        """Check if evidence already has a processing request."""
        query = select(EmbeddingRequest).where(
            EmbeddingRequest.evidence_id == evidence_id
        ).limit(1)
        result = await self.session.execute(query)
        return result.scalar() is not None

    async def create_request(self, evidence_id, camera_id, image_urls, stream_msg_id=None):
        """Create new embedding request at status=1."""
        request = EmbeddingRequest(
            evidence_id=evidence_id,
            camera_id=camera_id,
            image_urls=image_urls,
            stream_message_id=stream_msg_id,
        )
        self.session.add(request)
        await self.session.flush()  # Get ID without committing
        return request

    async def get_stale_working(self, stale_minutes: int = 10) -> List[EmbeddingRequest]:
        """Find requests stuck in WORKING for too long."""
        cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
        query = (
            select(EmbeddingRequest)
            .where(
                and_(
                    EmbeddingRequest.status == EmbeddingRequestStatus.WORKING,
                    EmbeddingRequest.processing_started_at < cutoff,
                )
            )
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())
```

**New file:** `src/db/repositories/search_request_repo.py`

Same pattern for search requests (parallel structure, same FOR UPDATE SKIP LOCKED).

## Migration

```bash
# Generate migration
alembic revision --autogenerate -m "add embedding_requests, evidence_embeddings, search_requests"

# Apply
alembic upgrade head
```

## ER Diagram

```
┌─────────────────────────┐       ┌─────────────────────────┐
│   embedding_requests     │       │    search_requests       │
├─────────────────────────┤       ├─────────────────────────┤
│ id (PK)                  │       │ id (PK)                  │
│ evidence_id (idx)        │       │ search_id (idx)          │
│ camera_id (idx)          │       │ user_id (idx)            │
│ status (idx)             │       │ image_url                │
│ image_urls (JSONB)       │       │ status (idx)             │
│ worker_id                │       │ similarity_status        │
│ error_message            │       │ threshold                │
│ retry_count              │       │ max_results              │
│ stream_message_id        │       │ metadata (JSONB)         │
│ processing_started_at    │       │ total_matches            │
│ processing_completed_at  │       │ results_key              │
│ created_at (idx)         │       │ worker_id                │
│ updated_at               │       │ error_message            │
└──────────┬──────────────┘       │ retry_count              │
           │ 1:N                   │ stream_message_id        │
           ▼                       │ processing_started_at    │
┌─────────────────────────┐       │ processing_completed_at  │
│  evidence_embeddings     │       │ created_at (idx)         │
├─────────────────────────┤       │ updated_at               │
│ id (PK)                  │       └─────────────────────────┘
│ request_id (FK)          │
│ qdrant_point_id (unique) │
│ image_index              │
│ image_url                │
│ json_data (JSONB)        │
│ created_at               │
└─────────────────────────┘
```
