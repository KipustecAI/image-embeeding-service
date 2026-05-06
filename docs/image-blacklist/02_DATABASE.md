# Phase 02: Database — Blacklist Schema

Three new tables modeled on the deepface-restapi face-blacklist spine. Additive migration, no changes to existing tables, no backfill needed (existing evidence is unaffected — blacklist is a pure addition).

## Files modified

| File | Action |
|---|---|
| `alembic/versions/xxx_create_blacklist_image_tables.py` | **Create** migration |
| `src/db/models/blacklist_image.py` | **Create** three model classes |
| `src/db/models/__init__.py` | Re-export new models |
| `src/db/repositories/blacklist_image_repo.py` | **Create** repository class with CRUD + lookups |
| `src/db/repositories/__init__.py` | Re-export new repository |

## Tables

### `blacklist_image_entries` — the profile

The unit on the blacklist. One entry per "thing we're watching for" (a vehicle, a scene, an object, an infraction pattern).

```python
class BlacklistImageEntry(Base):
    __tablename__ = "blacklist_image_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    category = Column(Text, nullable=True, index=True)  # Optional, see Phase 01
    description = Column(Text, nullable=True)  # Free-form notes
    status = Column(
        Integer, nullable=False, default=1, index=True
    )  # 1=CREATED, 2=PROCESSING, 3=INDEXED, 4=UPDATING, 5=ERROR
    active = Column(Boolean, nullable=False, default=True, index=True)
    is_archived = Column(Boolean, nullable=False, default=False)

    # Multi-tenant — strictly per-tenant in v1, no cross-user blacklists
    user_id = Column(String(255), nullable=False, index=True)

    # Version — bumped on edits to trigger re-matching of historical evidence
    blacklist_version = Column(Integer, nullable=False, default=1)

    # Per-entry threshold override — nullable. v1 always falls back to the
    # global BLACKLIST_MATCH_THRESHOLD. See docs/image-blacklist/05_MATCH_AND_REPORT.md
    # for the extension hook rationale.
    match_threshold = Column(Float, nullable=True)

    # Free-form metadata — notes, tags, external IDs from case management systems, etc.
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    references = relationship(
        "BlacklistImageReference",
        back_populates="entry",
        cascade="all, delete-orphan",
    )
    embeddings = relationship(
        "BlacklistImageEmbedding",
        back_populates="entry",
        cascade="all, delete-orphan",
    )
```

Status values (mirror deepface):

```python
# src/db/models/constants.py — add new class, don't touch existing

class BlacklistEntryStatus:
    CREATED = 1       # Profile created, no references yet
    PROCESSING = 2    # References being embedded
    INDEXED = 3       # At least one reference stored in Qdrant, ready for matching
    UPDATING = 4      # Adding/removing references, version bumping
    ERROR = 5         # Processing failed
```

Notes:
- `user_id` is **NOT NULL** — v1 has no cross-tenant blacklists.
- `blacklist_version` increments when the entry is edited (name change, reference added/removed). Future re-matching logic can filter `WHERE blacklist_version > last_matched_version`.
- `match_threshold` is nullable — v1 always uses the global default. The column exists so Phase 05's code has something to read when we later wire the override, without a second migration.
- No `FaceEmbedding` equivalent FK on this side — matches are tracked on the evidence side (via the existing `evidence_embeddings.json_data`) and as `BlacklistMatch` rows elsewhere if product ever asks for match-history queries. Out of scope for v1.

### `blacklist_image_references` — reference photos

The source images for a blacklist entry. One entry can have many references.

```python
class BlacklistImageReference(Base):
    __tablename__ = "blacklist_image_references"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url = Column(Text, nullable=False)
    image_type = Column(String(50), default="reference")  # Reserved for future: reference/crop/variant
    status = Column(
        Integer, nullable=False, default=1
    )  # 1=TO_PROCESS, 2=PROCESSING, 3=PROCESSED, 4=ERROR

    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Dimensions / thumbnail / quality — captured during embedding if needed
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    entry = relationship("BlacklistImageEntry", back_populates="references")
    embeddings = relationship(
        "BlacklistImageEmbedding",
        back_populates="reference",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # A reference image can only appear once per entry. See docs §"Dedup".
        UniqueConstraint("entry_id", "image_url", name="uq_entry_image"),
    )
```

Status:
```python
class BlacklistReferenceStatus:
    TO_PROCESS = 1
    PROCESSING = 2
    PROCESSED = 3
    ERROR = 4
```

Notes:
- `ON DELETE CASCADE` — if the entry is deleted, all references go with it.
- Unique constraint on `(entry_id, image_url)` prevents double-insertion to the same entry. **Not unique across entries** — the same image can legitimately be a reference for multiple entries (e.g., "wanted vehicle" and "stolen property evidence" both referencing the same crime scene photo).
- `error_message` + `retry_count` mirror the existing [EmbeddingRequest](../../src/db/models/embedding_request.py) pattern so ops can investigate failures without reading logs.

### `blacklist_image_embeddings` — Qdrant references

One row per vector stored in Qdrant. Joins back from `qdrant_point_id` to the SQL side.

```python
class BlacklistImageEmbedding(Base):
    __tablename__ = "blacklist_image_embeddings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reference_id = Column(
        UUID(as_uuid=True),
        ForeignKey("blacklist_image_references.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    qdrant_point_id = Column(String(255), unique=True, nullable=False, index=True)
    model_version = Column(String(50), nullable=False)  # e.g. "clip-vit-b-32"

    # Per-embedding metadata: crop region if the reference was cropped,
    # quality metrics, etc. Not used by filter logic — purely for traceability.
    json_data = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    entry = relationship("BlacklistImageEntry", back_populates="embeddings")
    reference = relationship("BlacklistImageReference", back_populates="embeddings")
```

Notes:
- `qdrant_point_id` is **UNIQUE** — catches any accidental re-embedding of the same reference.
- No `confidence` column (unlike the face version) — CLIP doesn't produce a face-detection confidence score at embed time. If future work adds quality scoring, it lands in `json_data`.
- `model_version` captures the CLIP variant — future work to upgrade the model needs to know which embeddings need re-running.

## Alembic migration

```python
"""Create blacklist image tables (entries, references, embeddings).

Revision ID: <auto>
Revises: <category migration id from Phase 01>
Create Date: 2026-04-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "<auto>"
down_revision = "<category migration id>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Entry — the "profile"
    op.create_table(
        "blacklist_image_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "blacklist_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("match_threshold", sa.Float(), nullable=True),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_blacklist_image_entries_name", "blacklist_image_entries", ["name"]
    )
    op.create_index(
        "ix_blacklist_image_entries_category", "blacklist_image_entries", ["category"]
    )
    op.create_index(
        "ix_blacklist_image_entries_status", "blacklist_image_entries", ["status"]
    )
    op.create_index(
        "ix_blacklist_image_entries_active", "blacklist_image_entries", ["active"]
    )
    op.create_index(
        "ix_blacklist_image_entries_user_id", "blacklist_image_entries", ["user_id"]
    )
    op.create_index(
        "ix_blacklist_image_entries_created_at",
        "blacklist_image_entries",
        ["created_at"],
    )

    # Reference images
    op.create_table(
        "blacklist_image_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("image_type", sa.String(50), server_default="reference"),
        sa.Column("status", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("entry_id", "image_url", name="uq_entry_image"),
    )
    op.create_index(
        "ix_blacklist_image_references_entry_id",
        "blacklist_image_references",
        ["entry_id"],
    )

    # Embeddings — Qdrant references
    op.create_table(
        "blacklist_image_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blacklist_image_references.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("qdrant_point_id", sa.String(255), nullable=False, unique=True),
        sa.Column("model_version", sa.String(50), nullable=False),
        sa.Column("json_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_blacklist_image_embeddings_entry_id",
        "blacklist_image_embeddings",
        ["entry_id"],
    )
    op.create_index(
        "ix_blacklist_image_embeddings_reference_id",
        "blacklist_image_embeddings",
        ["reference_id"],
    )


def downgrade() -> None:
    op.drop_table("blacklist_image_embeddings")
    op.drop_table("blacklist_image_references")
    op.drop_table("blacklist_image_entries")
```

Notes:
- **No GIN index on `json_data`.** Add only if a real query pattern emerges.
- **Server-side defaults** on `status`, `active`, `blacklist_version`, `retry_count` so inserting with minimal SQL works even outside the ORM.
- **CASCADE on delete** propagates down the FK chain — one `DELETE FROM blacklist_image_entries WHERE id = ?` takes the references and embeddings with it.

## Repository

`src/db/repositories/blacklist_image_repo.py`:

```python
class BlacklistImageRepository:
    """CRUD + lookup operations for the blacklist image tables."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # Entry ops

    async def create_entry(
        self,
        name: str,
        user_id: str,
        *,
        category: str | None = None,
        description: str | None = None,
        match_threshold: float | None = None,
        json_data: dict | None = None,
    ) -> BlacklistImageEntry: ...

    async def get_entry(self, entry_id: UUID) -> BlacklistImageEntry | None: ...

    async def list_entries(
        self,
        user_id: str | None = None,
        *,
        active_only: bool = True,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BlacklistImageEntry]: ...

    async def update_entry_status(
        self, entry_id: UUID, status: int
    ) -> None: ...

    async def bump_version(self, entry_id: UUID) -> int: ...

    async def deactivate_entry(self, entry_id: UUID) -> None: ...

    async def delete_entry(self, entry_id: UUID) -> None: ...

    # Reference ops

    async def add_reference(
        self,
        entry_id: UUID,
        image_url: str,
        *,
        image_type: str = "reference",
    ) -> BlacklistImageReference: ...

    async def list_references(
        self, entry_id: UUID
    ) -> list[BlacklistImageReference]: ...

    async def update_reference_status(
        self, reference_id: UUID, status: int, error: str | None = None
    ) -> None: ...

    async def delete_reference(self, reference_id: UUID) -> None: ...

    # Embedding ops

    async def create_embedding(
        self,
        entry_id: UUID,
        reference_id: UUID,
        qdrant_point_id: str,
        model_version: str,
        json_data: dict | None = None,
    ) -> BlacklistImageEmbedding: ...

    async def list_embeddings(
        self, entry_id: UUID
    ) -> list[BlacklistImageEmbedding]: ...

    async def delete_embeddings_for_reference(
        self, reference_id: UUID
    ) -> list[str]:
        """Delete embedding rows for a reference, return their qdrant_point_ids
        so the caller can remove them from Qdrant."""
        ...

    # Lookup / stats

    async def count_active_by_user(self, user_id: str) -> int: ...

    async def get_active_qdrant_point_ids(
        self, user_id: str
    ) -> list[str]: ...
```

Each method is a thin wrapper over SQLAlchemy — no business logic here. Business logic lives in the use cases (Phase 05).

## Tests

New file `tests/test_blacklist_image_repo.py`:

```python
@pytest.mark.asyncio
async def test_create_entry_with_defaults(session):
    # Minimal required args only — name + user_id
    ...

@pytest.mark.asyncio
async def test_create_entry_with_category_and_threshold(session):
    ...

@pytest.mark.asyncio
async def test_list_entries_multitenant_isolation(session):
    # User A's entries don't leak into User B's list
    ...

@pytest.mark.asyncio
async def test_list_entries_active_only_default(session):
    # Inactive entries are filtered out by default
    ...

@pytest.mark.asyncio
async def test_add_reference_enforces_entry_image_unique(session):
    # Adding the same image_url twice to the same entry raises IntegrityError
    ...

@pytest.mark.asyncio
async def test_add_reference_same_image_different_entries(session):
    # Same image_url on different entries is allowed
    ...

@pytest.mark.asyncio
async def test_cascade_delete(session):
    # Deleting an entry removes its references AND embeddings
    ...

@pytest.mark.asyncio
async def test_delete_embeddings_for_reference_returns_point_ids(session):
    # Caller needs the qdrant_point_ids to clean up Qdrant after the DB delete
    ...

@pytest.mark.asyncio
async def test_bump_version_increments(session):
    ...
```

## Verification

After `make migrate`:

```sql
\d blacklist_image_entries
\d blacklist_image_references
\d blacklist_image_embeddings

-- Unique constraint enforced
INSERT INTO blacklist_image_entries (id, name, user_id) VALUES ('...', 'test', 'u1');
INSERT INTO blacklist_image_references (id, entry_id, image_url) VALUES ('ref1', '...', 'http://x/1.jpg');
INSERT INTO blacklist_image_references (id, entry_id, image_url) VALUES ('ref2', '...', 'http://x/1.jpg');
-- ERROR: duplicate key value violates unique constraint "uq_entry_image"

-- Cascade delete
DELETE FROM blacklist_image_entries WHERE id = '...';
SELECT COUNT(*) FROM blacklist_image_references WHERE entry_id = '...';
-- 0
```

Once these three tables exist and the repository tests pass, Phase 03 can start wiring the Qdrant side.

## Rollout order reminder

1. Phase 01 must be **fully deployed** before running this migration — otherwise FK references on `category` column in queries will fail if any code is using it.
2. Apply this migration.
3. Only then deploy Phase 03 (which starts writing to Qdrant with `source_type="blacklist"`).
