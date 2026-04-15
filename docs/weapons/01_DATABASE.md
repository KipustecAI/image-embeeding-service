# Phase 1: Database — Migration, Models, Repository

Additive changes only. No drops, no type changes. Safe to roll forward and backward.

## Files modified

| File | Action |
|---|---|
| [alembic/versions/xxx_add_weapon_analysis_fields.py](../../alembic/versions/) | **Create** new migration |
| [src/db/models/embedding_request.py](../../src/db/models/embedding_request.py) | Add 5 columns |
| [src/db/models/evidence_embedding.py](../../src/db/models/evidence_embedding.py) | Add 1 column |
| [src/db/repositories/embedding_request_repo.py](../../src/db/repositories/embedding_request_repo.py) | Extend `create_request` signature |

## Model changes

### `EmbeddingRequest` — evidence-level flags (for fast SQL + reports)

```python
# src/db/models/embedding_request.py — additions inside the class body

from sqlalchemy import Boolean, Float  # add Boolean, Float to existing imports

class EmbeddingRequest(Base):
    # ... existing columns ...

    # Weapons enrichment (Phase 1)
    weapon_analyzed = Column(Boolean, nullable=False, default=False, index=True)
    has_weapon = Column(Boolean, nullable=False, default=False, index=True)
    weapon_classes = Column(JSONB, nullable=False, default=list)
    weapon_max_confidence = Column(Float, nullable=True)
    weapon_summary = Column(JSONB, nullable=True)
```

Column rationale:

| Column | Type | Nullable | Default | Purpose |
|---|---|---|---|---|
| `weapon_analyzed` | `BOOLEAN` | `NOT NULL` | `FALSE` | Did compute-weapons run on this evidence? Indexed for `WHERE weapon_analyzed = true` |
| `has_weapon` | `BOOLEAN` | `NOT NULL` | `FALSE` | Did it find anything? Indexed — the hottest report query |
| `weapon_classes` | `JSONB` | `NOT NULL` | `[]` | List of class names detected. GIN index optional — only add if reports grow slow |
| `weapon_max_confidence` | `FLOAT` | `NULL` | — | Peak confidence across all detections for this evidence; enables sort/threshold queries |
| `weapon_summary` | `JSONB` | `NULL` | — | Raw summary block preserved verbatim — cheap insurance against summary-shape evolution upstream |

### `EvidenceEmbeddingRecord` — per-image bbox detail

```python
# src/db/models/evidence_embedding.py — single column addition

class EvidenceEmbeddingRecord(Base):
    # ... existing columns ...

    # Per-image weapon detections (Phase 1)
    weapon_detections = Column(JSONB, nullable=True)
```

Value shape:
```json
[
  {
    "class_name": "handgun",
    "class_id": 0,
    "confidence": 0.873,
    "bbox": { "x1": 412, "y1": 188, "x2": 596, "y2": 402 }
  }
]
```

`NULL` means "not analyzed"; `[]` means "analyzed, clean"; non-empty array means "detections present". The consumer writes the appropriate value based on `weapon_analysis` presence in the payload. See Phase 3.

## Alembic migration

File: `alembic/versions/<timestamp>_add_weapon_analysis_fields.py`

Generate with:
```bash
make migrate-create msg="add weapon analysis fields"
```

Replace the generated body with:

```python
"""add weapon analysis fields

Revision ID: <auto>
Revises: <previous head>
Create Date: <auto>
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "<auto>"
down_revision = "<previous head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # embedding_requests: evidence-level weapon flags + summary
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_analyzed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "has_weapon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "embedding_requests",
        sa.Column("weapon_max_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "embedding_requests",
        sa.Column(
            "weapon_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_embedding_requests_weapon_analyzed",
        "embedding_requests",
        ["weapon_analyzed"],
    )
    op.create_index(
        "ix_embedding_requests_has_weapon",
        "embedding_requests",
        ["has_weapon"],
    )

    # evidence_embeddings: per-image detection detail
    op.add_column(
        "evidence_embeddings",
        sa.Column(
            "weapon_detections",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("evidence_embeddings", "weapon_detections")

    op.drop_index("ix_embedding_requests_has_weapon", table_name="embedding_requests")
    op.drop_index("ix_embedding_requests_weapon_analyzed", table_name="embedding_requests")

    op.drop_column("embedding_requests", "weapon_summary")
    op.drop_column("embedding_requests", "weapon_max_confidence")
    op.drop_column("embedding_requests", "weapon_classes")
    op.drop_column("embedding_requests", "has_weapon")
    op.drop_column("embedding_requests", "weapon_analyzed")
```

Notes on the migration body:

- **`server_default=sa.false()`** is critical. Without a server-side default, adding a `NOT NULL` column to a non-empty table fails. With it, existing rows backfill to `false` at migration time with no application intervention.
- **`server_default=sa.text("'[]'::jsonb")`** does the same for the JSONB array column. The `default=list` in the model is a separate Python-side default for new rows created through the ORM.
- The indices are plain B-tree on boolean columns — Postgres handles these efficiently for low-cardinality filters like `WHERE has_weapon = true`.
- No GIN index on `weapon_classes` in the initial migration. If reports grow slow on class-level SQL queries, add it in a follow-up:
  ```python
  op.create_index(
      "ix_embedding_requests_weapon_classes_gin",
      "embedding_requests",
      ["weapon_classes"],
      postgresql_using="gin",
  )
  ```
- Concurrency: all `op.add_column` calls are online for Postgres 11+. Index creation uses default (non-concurrent) in Alembic, which locks the table briefly. Since `embedding_requests` holds at most ~weeks of data and this service isn't write-heavy, brief locks are fine. If that changes, switch to `CREATE INDEX CONCURRENTLY` via `op.execute`.

## Repository signature change

Extend [embedding_request_repo.py:45-69](../../src/db/repositories/embedding_request_repo.py#L45-L69):

```python
async def create_request(
    self,
    evidence_id: str,
    camera_id: str,
    image_urls: list,
    stream_msg_id: str | None = None,
    user_id: str | None = None,
    device_id: str | None = None,
    app_id: int | None = None,
    infraction_code: str | None = None,
    # Weapons enrichment (Phase 1)
    weapon_analyzed: bool = False,
    has_weapon: bool = False,
    weapon_classes: list[str] | None = None,
    weapon_max_confidence: float | None = None,
    weapon_summary: dict | None = None,
) -> EmbeddingRequest:
    """Create new embedding request at status=1."""
    request = EmbeddingRequest(
        evidence_id=evidence_id,
        camera_id=camera_id,
        image_urls=image_urls,
        stream_message_id=stream_msg_id,
        user_id=user_id,
        device_id=device_id,
        app_id=app_id,
        infraction_code=infraction_code,
        weapon_analyzed=weapon_analyzed,
        has_weapon=has_weapon,
        weapon_classes=weapon_classes or [],
        weapon_max_confidence=weapon_max_confidence,
        weapon_summary=weapon_summary,
    )
    self.session.add(request)
    await self.session.flush()
    return request
```

Key points:

- **All new kwargs default to "unanalyzed safe" values.** A caller that doesn't know about weapons (e.g. the error path in `_process_compute_error`) continues to work unchanged.
- `weapon_classes` normalizes `None` → `[]` inside the function. Callers can pass either; the DB always sees a list (matches the `NOT NULL DEFAULT '[]'` column).
- No changes to `create_request`'s return type or the rest of the repository — read methods, status transitions, and retry logic are untouched.

## Rollout order

1. **Merge + deploy the migration first.** `make migrate` on the target environment. Verify via `psql`:
   ```sql
   \d embedding_requests
   \d evidence_embeddings
   ```
   New columns must appear before step 2.
2. **Deploy Phase 2 (Qdrant indices).**
3. **Deploy Phase 3 (consumer).** At this point the consumer starts writing weapon fields.
4. **Deploy Phase 4 (search API).**

If you deploy the consumer before the migration, `INSERT` will fail with "column does not exist" and messages will go to the dead letter stream. Not a data-loss event (they'll be replayed after rollback + migration), but annoying. Don't skip step 1.

## Verification (manual)

After `make migrate`:

```sql
-- New columns present with expected defaults
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'embedding_requests'
  AND column_name IN ('weapon_analyzed', 'has_weapon', 'weapon_classes', 'weapon_max_confidence', 'weapon_summary')
ORDER BY column_name;

-- New indices present
SELECT indexname FROM pg_indexes
WHERE tablename = 'embedding_requests'
  AND indexname LIKE 'ix_embedding_requests_%weapon%';

-- Existing rows backfilled
SELECT COUNT(*) FROM embedding_requests WHERE weapon_analyzed = false;
-- Should equal total row count.

-- evidence_embeddings has the new column
SELECT column_name FROM information_schema.columns
WHERE table_name = 'evidence_embeddings' AND column_name = 'weapon_detections';
```

Downgrade test on a throwaway DB:
```bash
alembic downgrade -1
# columns + indices should all be gone
alembic upgrade head
# back to the new state
```
