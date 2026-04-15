# Phase 4: Search API — Filter Modes

Three coordinated changes:

1. **Request DTO** — add `weapons_filter` and `weapon_classes` fields
2. **Search use case** — translate the filter mode into a Qdrant `filter_conditions` dict
3. **Persistence of filters** — filters must survive the async GPU round-trip so the consumer can apply them when `search:results` arrives

## Files modified

| File | Change |
|---|---|
| [src/application/dto/embedding_dto.py](../../src/application/dto/embedding_dto.py) | Add fields to `ImageSearchRequest` |
| [src/main.py](../../src/main.py) | Add fields to the FastAPI Pydantic request model and propagate to the use case |
| [src/application/use_cases/search_similar_images.py](../../src/application/use_cases/search_similar_images.py) | Build `filter_conditions` from the new fields |
| [src/db/models/search_request.py](../../src/db/models/search_request.py) | **NEW** `search_filters JSONB` column (see "Persistence" below) |
| [src/streams/search_results_consumer.py](../../src/streams/search_results_consumer.py) | Read `search_filters` and forward to the Qdrant search call |
| `alembic/versions/xxx_add_search_filters.py` | New migration for the `search_filters` column (can be part of Phase 1's migration if you prefer one-big-migration) |

## Request shape

```json
POST /api/v1/search
{
  "image_url": "https://storage.example.com/query.jpg",
  "threshold": 0.75,
  "max_results": 50,
  "weapons_filter": "only",
  "weapon_classes": ["handgun"],
  "metadata": {
    "camera_id": "f39d4374-8a68-4b34-b3ef-5a0514e81d92"
  }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `weapons_filter` | `"all"` \| `"only"` \| `"exclude"` \| `"analyzed_clean"` | `"all"` | How to filter matches by weapon presence |
| `weapon_classes` | `list[str]` | `null` | Optional class subset. Only meaningful when `weapons_filter="only"`. Ignored otherwise with a warning log. |

## Filter mode → Qdrant conditions table

| `weapons_filter` | Qdrant `must` conditions added | Intent |
|---|---|---|
| `"all"` | — (nothing added) | Default; unchanged from today |
| `"only"` | `has_weapon=true` + optional `weapon_classes ∈ [...]` (as `MatchAny`) | Search constrained to weapon-bearing images |
| `"exclude"` | `has_weapon=false` | Broad safe search. Includes unanalyzed images (their `has_weapon=false`). |
| `"analyzed_clean"` | `weapon_analyzed=true` AND `has_weapon=false` | False-positive review queue. Only includes images explicitly checked and cleared. |

These conditions combine with the existing `user_id` / `camera_id` filters in the same `must` list — Qdrant intersects them.

## DTO change

```python
# src/application/dto/embedding_dto.py
from typing import Literal

class ImageSearchRequest(BaseModel):
    # ... existing fields ...

    # Weapons enrichment (Phase 4)
    weapons_filter: Literal["all", "only", "exclude", "analyzed_clean"] = "all"
    weapon_classes: list[str] | None = None
```

Validation rules:
- `weapon_classes` normalized: empty list → `None`
- If `weapon_classes` is passed with `weapons_filter != "only"`: log a warning and **ignore** (do not 400). Permissive APIs age better than strict ones.
- `weapons_filter` is validated by Pydantic against the `Literal` — a bad value returns 422 automatically.

## Use case change

In [search_similar_images.py](../../src/application/use_cases/search_similar_images.py), the `filter_conditions` dict is built once and passed to `vector_repo.search_similar`. Add:

```python
def _build_weapon_filter_conditions(
    request: ImageSearchRequest,
) -> dict:
    """Map weapons_filter enum → Qdrant filter_conditions additions."""
    conditions: dict = {}
    mode = request.weapons_filter

    if mode == "all":
        return conditions

    if mode == "only":
        conditions["has_weapon"] = True
        classes = request.weapon_classes
        if classes:
            # List value triggers MatchAny in qdrant_repository.search_similar.
            # See docs/weapons/02_QDRANT.md.
            conditions["weapon_classes"] = classes
        return conditions

    if mode == "exclude":
        conditions["has_weapon"] = False
        return conditions

    if mode == "analyzed_clean":
        conditions["weapon_analyzed"] = True
        conditions["has_weapon"] = False
        return conditions

    # Unreachable — Literal validation catches unknown values
    return conditions
```

Inside `execute()`, merge these conditions with any existing `user_id`/`camera_id` filters before calling `search_similar`:

```python
filter_conditions = self._build_base_filters(request, ctx)  # existing logic
filter_conditions.update(_build_weapon_filter_conditions(request))

matches = await self.vector_repo.search_similar(
    query_vector=vector,
    limit=request.max_results,
    threshold=request.threshold,
    filter_conditions=filter_conditions,
)
```

Also log the weapons-filter intent at info level so ops can observe filter usage without DB queries:

```python
if request.weapons_filter != "all":
    logger.info(
        f"Search {request.search_id}: weapons_filter={request.weapons_filter} "
        f"weapon_classes={request.weapon_classes}"
    )
```

## Persistence: why filters can't live only in memory

The current search flow is **asynchronous**: the API posts to `evidence:search`, the GPU publishes the query vector to `search:results`, and `search_results_consumer` runs the Qdrant search in a separate process from the one that received the POST. The filter intent must survive that round-trip.

Two options were considered:

### Option A (rejected) — Forward filters through the GPU stream

The API puts `weapons_filter` and `weapon_classes` into the `evidence:search` envelope; the GPU's compute service copies them into `search:results`; the backend consumer reads them back.

**Problems:**
- Couples the GPU service to a filter shape it doesn't care about. Every new filter requires GPU code changes.
- Burns stream bandwidth on data the GPU has no use for.
- Harder to debug (filter values pass through a service that doesn't use them).

### Option B (chosen) — Stash filters on the `SearchRequest` DB row

The API writes filters to a new `search_filters JSONB` column on `search_requests` at the same time it publishes to `evidence:search`. When `search_results_consumer` later runs, it reads the filters from the DB row (which it already loads by `search_id`) and passes them to `search_similar`.

**Advantages:**
- GPU service stays filter-agnostic
- Filters are queryable for analytics ("how many searches this month used `analyzed_clean`?")
- Clean DB migration path — the column can be backfilled with `'{}'::jsonb`

### Migration addition

Bundle with the Phase 1 migration or create a dedicated one:

```python
op.add_column(
    "search_requests",
    sa.Column(
        "search_filters",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
)
```

Model addition in [search_request.py](../../src/db/models/search_request.py):

```python
search_filters = Column(JSONB, nullable=False, default=dict)
```

Writer side (API handler at `POST /api/v1/search`):

```python
search_filters = {
    "weapons_filter": body.weapons_filter,
    "weapon_classes": body.weapon_classes,
}
# Pass this to the SearchRequest row creation
await search_repo.create(
    ...,
    search_filters=search_filters,
)
```

Reader side ([search_results_consumer.py](../../src/streams/search_results_consumer.py)):

```python
request = await search_repo.get_by_search_id(search_id)
filters = request.search_filters or {}

filter_conditions = _base_filters_from_request(request)  # existing user_id/camera_id logic
filter_conditions.update(
    _build_weapon_filter_conditions_from_dict(filters)
)

matches = await vector_repo.search_similar(
    query_vector=query_vector,
    limit=request.max_results,
    threshold=request.threshold,
    filter_conditions=filter_conditions,
)
```

`_build_weapon_filter_conditions_from_dict` is the same logic as the use case's helper, but reads from a plain dict instead of an `ImageSearchRequest` Pydantic model. Factor it into a tiny utility module (e.g. `src/application/helpers/weapon_filters.py`) shared between the use case and the consumer — single source of truth.

## Recalculation path

[POST /api/v1/recalculate/searches](../../src/main.py) and the hourly recalculation job re-run old searches against current Qdrant state. Since they reload the `SearchRequest` row by ID, they'll automatically pick up `search_filters` and pass them through. **No code changes needed** — the persistence design gives recalculation the right behavior for free.

Confirm this behavior in the integration test by:
1. Creating a search with `weapons_filter="only"`
2. Letting it complete
3. Triggering recalculation
4. Asserting the recalculated matches still honor the weapons filter

## Response format — unchanged

`GET /api/v1/search/{id}/matches` returns a `metadata` dict per match. Since the consumer (Phase 3) injects `has_weapon`, `weapon_classes`, and `weapon_analyzed` into the Qdrant payload, those fields appear in the `metadata` response automatically. Clients that want to render bboxes can query `evidence_embeddings.weapon_detections` by `qdrant_point_id`, or we add a dedicated endpoint in a follow-up.

Example response (no code change, new fields appear naturally):

```json
{
  "search_id": "...",
  "matches": [
    {
      "evidence_id": "dd946e14-...",
      "similarity_score": 0.91,
      "image_url": "https://minio.../frame_001.jpg",
      "metadata": {
        "camera_id": "0d3f9a4b-...",
        "user_id": "3996d660-...",
        "image_index": 0,
        "source_type": "evidence",
        "weapon_analyzed": true,
        "has_weapon": true,
        "weapon_classes": ["handgun"]
      }
    }
  ]
}
```

## Backwards compatibility

Callers that don't set `weapons_filter` get the default `"all"` — identical behavior to today. Callers that pass unexpected fields (e.g. an old client sending `weapons_filter: "yes"`) get a 422 from Pydantic — preferable to silently running the wrong query.

The one behavior change visible to existing callers: matches now include the three new `metadata` fields. If clients have strict deserialization, they need to accept unknown fields — but this is the same change we've made before for multi-tenant fields, and historically no one has complained.

## Verification

See [05_DOCS_AND_VERIFICATION.md](05_DOCS_AND_VERIFICATION.md) for the full test plan. Minimum search-API smoke test:

```bash
# Default — unchanged behavior
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: u1" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"...", "threshold":0.75}'

# Weapons only — any class
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: u1" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"...", "threshold":0.75, "weapons_filter":"only"}'

# Handguns only
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: u1" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"...", "threshold":0.75, "weapons_filter":"only", "weapon_classes":["handgun"]}'

# False-positive review queue
curl -X POST http://localhost:8001/api/v1/search \
  -H "X-User-Id: u1" -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"...", "threshold":0.75, "weapons_filter":"analyzed_clean"}'
```

All four should return 202 immediately. Polling `GET /api/v1/search/{id}/matches` should show matches respecting the filter after the GPU round-trip completes (~2 seconds).
