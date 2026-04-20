# Phase 06: REST CRUD API

HTTP surface for managing blacklist entries, attaching reference images, triggering backfills, and reading status.

## Files modified

| File | Action |
|---|---|
| `src/api/v1/routers/blacklist_image.py` | **Create** — endpoints below |
| `src/api/v1/schemas/blacklist_image.py` | **Create** — Pydantic request/response models |
| [src/main.py](../../src/main.py) | Register the router |
| `src/application/use_cases/manage_blacklist_image.py` | **Create** — business logic isolated from HTTP layer |
| [docs/API_REFERENCE.md](../API_REFERENCE.md) | Document new endpoints |
| [docs/CURL_EXAMPLES.md](../CURL_EXAMPLES.md) | Add blacklist examples |

## Endpoint summary

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/blacklist/image-entries` | Yes | Create a new blacklist entry |
| GET | `/api/v1/blacklist/image-entries` | Yes | List entries (multi-tenant filtered) |
| GET | `/api/v1/blacklist/image-entries/{id}` | Yes | Get entry detail with references + counts |
| PATCH | `/api/v1/blacklist/image-entries/{id}` | Yes | Update name/category/description/active/match_threshold |
| DELETE | `/api/v1/blacklist/image-entries/{id}` | Yes | Delete entry + all refs + all Qdrant points |
| POST | `/api/v1/blacklist/image-entries/{id}/references` | Yes | Attach a reference image (triggers embed via `evidence:search`) |
| DELETE | `/api/v1/blacklist/image-entries/{id}/references/{ref_id}` | Yes | Remove a reference + its Qdrant point |
| POST | `/api/v1/blacklist/image-entries/{id}/backfill` | Yes | Trigger a reverse-search against all historical evidence |
| GET | `/api/v1/blacklist/image-entries/{id}/matches` | Yes | Paginated recent matches for this entry (SQL-side, if we decide to store them) |

**Auth:** gateway-header based, matching the existing search API pattern. `X-User-Id` identifies the tenant; `X-User-Role` gates admin-only operations.

**Multi-tenant:**
- `user` role: can only CRUD their own entries. All endpoints filter by `ctx.user_id`.
- `admin`, `root`, `dev`: can see all entries; GET `?user_id=<uuid>` filters to a specific tenant.

## Request/response shapes

### POST `/api/v1/blacklist/image-entries`

```json
// Request
{
  "name": "Placa del sospechoso — caso 2025-042",
  "category": "vehicle",                     // Optional
  "description": "Vehículo vinculado a investigación abierta",  // Optional
  "match_threshold": 0.88,                   // Optional override of global default
  "json_data": { "case_id": "CRIM-2025-042" }  // Optional free-form
}

// Response — 201 Created
{
  "id": "a1b2c3d4-...",
  "name": "Placa del sospechoso — caso 2025-042",
  "category": "vehicle",
  "description": "Vehículo vinculado a investigación abierta",
  "status": 1,                               // CREATED
  "active": true,
  "blacklist_version": 1,
  "match_threshold": 0.88,
  "references_count": 0,
  "embeddings_count": 0,
  "user_id": "3996d660-...",
  "created_at": "2026-04-18T14:22:03Z",
  "updated_at": "2026-04-18T14:22:03Z"
}
```

**Status at create: 1 (CREATED)** — no references yet. Moves to 2 (PROCESSING) on first reference, 3 (INDEXED) once any reference completes embedding.

**`name` validation:** length 1–255, no other constraints. Caller's responsibility to normalize.

**`match_threshold`:** if null, matching uses the global `BLACKLIST_MATCH_THRESHOLD`. If non-null, must be 0.0–1.0. **Note: v1 only reads this column during match; it does not rebuild or invalidate existing matches if the threshold is later changed.** Re-running matches with a new threshold is a future backfill-on-demand feature.

### GET `/api/v1/blacklist/image-entries`

```
Query params:
  ?active=true          (default) Only active entries
  ?active=false         Inactive only
  ?active=all           Both
  ?category=vehicle     Filter by category
  ?user_id=<uuid>       ADMIN ONLY — filter to a specific tenant
  ?limit=20             (default, max 100)
  ?offset=0
```

Response:
```json
{
  "total": 42,
  "limit": 20,
  "offset": 0,
  "entries": [
    { ... same shape as POST response ... }
  ]
}
```

### GET `/api/v1/blacklist/image-entries/{id}`

Returns the entry plus its references (list, each with its own status):

```json
{
  "id": "...",
  "name": "...",
  // ... same fields as POST response ...
  "references": [
    {
      "id": "e5f6a7b8-...",
      "image_url": "https://minio.lookia.mx/blacklist/...",
      "image_type": "reference",
      "status": 3,         // PROCESSED
      "error_message": null,
      "created_at": "..."
    }
  ],
  "embeddings_count": 1     // Total Qdrant points for this entry across all references
}
```

### PATCH `/api/v1/blacklist/image-entries/{id}`

Partial update. Any of `name`, `category`, `description`, `active`, `match_threshold` may be sent. `user_id` is immutable (no cross-tenant reassignment).

**Version bump rule:** if the update is materially matching-relevant (i.e. the entry would produce different matches), bump `blacklist_version`. v1 definition of "matching-relevant":

- `match_threshold` changes → bump
- `active` changes false → true → bump (reactivation)
- Other fields → no bump

Reasoning: bumping version is the signal for receivers to not dedup matches. We want a threshold change to produce fresh reports; we don't want a typo fix in the name to do so.

The Phase 05 event carries `blacklist_entry_version`. Receivers dedup on `(evidence_id, entry_id, entry_version)`.

### DELETE `/api/v1/blacklist/image-entries/{id}`

Cascades to references and embeddings via FK. Must also clean up Qdrant points.

Flow:
1. Fetch the list of `qdrant_point_id`s from `blacklist_image_embeddings WHERE entry_id = ?`
2. Call `vector_repo.delete_points(point_ids)` (**new small method**)
3. `DELETE FROM blacklist_image_entries WHERE id = ?` — CASCADE cleans the rest of SQL

**Soft delete vs hard delete:** v1 is hard delete. If product wants to preserve history (e.g. "this entry matched X evidences before we deleted it"), use `active=false` via PATCH — that keeps the entry around for audit. Hard delete is for cleanup of erroneous entries.

**Error handling:** if Qdrant delete fails mid-operation, we leave the DB row intact and log the error. Better to have an orphaned blacklist entry in SQL (which the admin can retry) than orphaned Qdrant points that keep matching against evidence (which silently generates zombie reports).

### POST `/api/v1/blacklist/image-entries/{id}/references`

```json
// Request
{
  "image_url": "https://minio.lookia.mx/uploaded-via-ui/...",
  "image_type": "reference"      // Optional, default "reference"
}

// Response — 202 Accepted
{
  "id": "e5f6a7b8-...",
  "entry_id": "a1b2c3d4-...",
  "image_url": "https://...",
  "status": 1,                   // TO_PROCESS — embedding will be triggered
  "created_at": "..."
}
```

**Behavior:**
1. Insert `BlacklistImageReference` row with status=1 (TO_PROCESS)
2. Bump the entry to status=2 (PROCESSING) if it was 1 (CREATED)
3. Publish to `evidence:search` with `purpose="blacklist_embed"`, `search_id=<reference.id>`, `blacklist_entry_id=<entry.id>`, `image_url`, `user_id`
4. Return 202 — embedding happens async via the GPU, results land via Phase 04

**Validation:**
- `image_url` must be a valid http/https URL (Pydantic `HttpUrl`). Local `file://` URLs rejected — blacklist references must be publicly reachable by the GPU.
- Unique `(entry_id, image_url)` constraint in the DB raises a 409 Conflict on duplicate.

### DELETE `/api/v1/blacklist/image-entries/{id}/references/{ref_id}`

1. Look up the reference, extract its `qdrant_point_id`s via the embedding table
2. `vector_repo.delete_points(point_ids)`
3. `DELETE FROM blacklist_image_references WHERE id = ?` — CASCADE removes embeddings

If the reference's status is 2 (PROCESSING) when delete is called, we honor the request but log that the in-flight embedding will land as an orphan (no matching DB row to attach to). The Phase 04 consumer handles orphans by logging and dropping — we spelled that out in [04_EMBEDDING_FLOW.md §"Failure semantics"](04_EMBEDDING_FLOW.md).

### POST `/api/v1/blacklist/image-entries/{id}/backfill`

Triggers a full reverse-search for this entry against historical evidence.

```
Query params:
  ?since=2026-04-01        (optional) Only match evidence created after this date
  ?limit=10000             (optional) Cap total matches published (default: no cap)
```

Response — 202 Accepted:
```json
{
  "message": "Backfill scheduled",
  "entry_id": "a1b2c3d4-...",
  "references_count": 3,
  "estimated_matches_upper_bound": 3000,  // References × batch_size upper bound
  "job_id": "reverse_search_a1b2c3d4"
}
```

Under the hood:
1. Load all `BlacklistImageEmbedding` rows for this entry → their vectors + reference IDs
2. For each, schedule an APScheduler one-shot job (same `_run_reverse_search` logic from Phase 04)
3. If a previous backfill for this entry is still in progress, reject with 409 Conflict

**Use case:** ops adds a new entry but the initial auto-reverse-search failed (backend crashed), or the entry's threshold was changed and we want to re-match with the new threshold.

**Admin-only?** v1 says yes — non-admin users trigger implicit reverse search automatically (via Phase 04 scheduling on first reference embed), so they don't need this endpoint. Admins need it for operational recovery.

### GET `/api/v1/blacklist/image-entries/{id}/matches`

**Scope question:** do we want to store matches on our side in a `blacklist_image_matches` table, or is the report-generation service the source of truth?

**v1 decision: don't store matches on our side.** The report-generation service is already going to store them (that's the whole point). Asking this endpoint about "matches for entry X" is better answered by querying their DB.

So we **don't ship this endpoint in v1.** If ops needs it, they use the report-generation service's API. If that becomes impractical (cross-service queries are awkward), add a `blacklist_image_matches` table in a follow-up migration.

**Alternative:** expose a thin read-only view that counts matches via a background-updated counter on the entry (`total_matches` column). That's a "future" item — not shipping.

## Use case layer

`src/application/use_cases/manage_blacklist_image.py` houses the non-HTTP business logic so the router is thin:

```python
class ManageBlacklistImageUseCase:
    def __init__(
        self,
        repo: BlacklistImageRepository,
        vector_repo: VectorRepository,
        stream_producer: StreamProducer,
    ): ...

    async def create_entry(self, user_id: str, request: CreateEntryRequest) -> Entry: ...

    async def add_reference(
        self, entry_id: UUID, user_id: str, request: AddReferenceRequest
    ) -> Reference: ...
    # ^^ Also publishes the evidence:search event with purpose=blacklist_embed

    async def delete_entry(self, entry_id: UUID, user_id: str) -> None: ...

    async def update_entry(
        self, entry_id: UUID, user_id: str, patch: PatchEntryRequest
    ) -> Entry: ...
    # ^^ Handles version-bump logic

    async def trigger_backfill(
        self, entry_id: UUID, user_id: str, since: datetime | None, limit: int | None
    ) -> BackfillResponse: ...
```

The router imports this use case, constructs request objects, calls the method, returns the response model. Same clean-architecture pattern as the search flow.

## Pydantic schemas

`src/api/v1/schemas/blacklist_image.py`:

```python
class CreateEntryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    category: str | None = None
    description: str | None = None
    match_threshold: float | None = Field(None, ge=0.0, le=1.0)
    json_data: dict | None = None


class PatchEntryRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    category: str | None = None
    description: str | None = None
    active: bool | None = None
    match_threshold: float | None = Field(None, ge=0.0, le=1.0)
    json_data: dict | None = None


class EntryResponse(BaseModel):
    id: str
    name: str
    category: str | None
    description: str | None
    status: int
    active: bool
    blacklist_version: int
    match_threshold: float | None
    references_count: int
    embeddings_count: int
    user_id: str
    created_at: datetime
    updated_at: datetime


class ReferenceResponse(BaseModel):
    id: str
    entry_id: str
    image_url: str
    image_type: str
    status: int
    error_message: str | None
    created_at: datetime


class AddReferenceRequest(BaseModel):
    image_url: HttpUrl    # Pydantic-validated
    image_type: str = "reference"
```

## Error responses

| Case | Status | Body |
|---|---|---|
| Unauthenticated | 401 | `{"detail": "Missing X-User-Id"}` |
| Non-admin trying to access another user's entry | 403 | `{"detail": "Entry not accessible"}` |
| Entry not found | 404 | `{"detail": "Entry not found"}` |
| Duplicate `(entry_id, image_url)` on reference create | 409 | `{"detail": "Reference already exists for this entry"}` |
| Backfill already in progress for entry | 409 | `{"detail": "Backfill already running"}` |
| Pydantic validation failure | 422 | Pydantic default |
| Qdrant unavailable during delete | 503 | `{"detail": "Storage backend unavailable; retry"}` |

## Tests

Integration-level tests in `tests/test_blacklist_image_api.py`:

- Create entry, add reference, observe `evidence:search` XADD (inspect Redis directly)
- List entries, assert multi-tenant isolation (user A can't see user B's entries)
- Admin list with `?user_id=<B>` returns B's entries
- PATCH to change threshold bumps version
- PATCH to change name does NOT bump version
- PATCH to toggle `active` from false → true bumps version (re-activation = re-match later)
- DELETE entry removes SQL rows AND Qdrant points (check both sides)
- DELETE reference removes exactly that reference's Qdrant points
- POST backfill schedules an APScheduler job with the expected ID
- POST backfill while a job is already running returns 409
- POST reference with file:// URL returns 422

## Rollout order reminder

1. Phases 01–05 must be deployed first.
2. This phase just adds the HTTP surface; the underlying machinery is already live from 01–05.
3. Announce the endpoints to frontend/UI team in parallel — they can start building the management UI against 06 while ops tunes the threshold from 05.
