# Phase 04: Embedding Flow — Reuse `evidence:search` With a Purpose Discriminator

When a user attaches a reference image to a blacklist entry, we need to run that image through the GPU's CLIP model and store the resulting vector in Qdrant with `source_type="blacklist"`. Rather than build a new stream pair (`evidence:blacklist:embed` / `evidence:blacklist:results`) like deepface-restapi did for faces, we **reuse the existing `evidence:search` → `search:results` pipeline** and add a `purpose` field to the envelope that tells the results consumer what to do with the returned vector.

Two paths from the same stream:

```
                                         ┌──► purpose="search" (today)
                                         │      → run Qdrant search
                                         │      → store SearchMatch rows
POST /api/v1/search                      │      → publish blacklist-match events (Phase 05)
or                          evidence:search   search:results
POST /api/v1/blacklist/     ────────────►GPU──►─────────────► search_results_consumer
     entries/{id}/refs                   │
                                         └──► purpose="blacklist_embed" (new)
                                                → store in Qdrant w/ source_type="blacklist"
                                                → upsert BlacklistImageEmbedding row
                                                → kick off async reverse search
```

## Scope

- Add `purpose: "search" | "blacklist_embed"` field to the `evidence:search` envelope payload. Default `"search"` — legacy paths unchanged.
- Extend `search_results_consumer._process_search_result` to branch on `purpose`:
  - `"search"` (default) → run existing Qdrant search + store matches logic.
  - `"blacklist_embed"` → store the returned vector as a blacklist point, upsert the `BlacklistImageEmbedding` row, enqueue an async reverse-search job.
- **Async reverse search** runs as an APScheduler one-shot job (created via `scheduler.add_job(..., trigger="date", run_date=now)`) so the API returns 202 immediately and the reverse search happens in the background.
- **No new streams.** No GPU-side code changes — the GPU already passes unknown fields through.

## Files modified

| File | Change |
|---|---|
| [src/streams/producer.py](../../src/streams/producer.py) | No change — `publish()` already takes an arbitrary `payload` dict |
| [src/streams/search_results_consumer.py](../../src/streams/search_results_consumer.py) | Branch on `purpose` inside `_process_search_result` |
| `src/services/blacklist_embed_service.py` | **Create** — owns the blacklist-embed success path: store in Qdrant, upsert DB row, schedule reverse search |
| `src/services/blacklist_reverse_search.py` | **Create** — async reverse-search job with progress tracking |
| [src/infrastructure/config.py](../../src/infrastructure/config.py) | Add `BLACKLIST_MATCH_THRESHOLD`, `BLACKLIST_REVERSE_SEARCH_BATCH_SIZE` |
| [src/main.py](../../src/main.py) | Expose the APScheduler to the blacklist service so it can schedule reverse-search jobs |

## The `purpose` field — stream envelope update

The existing `evidence:search` payload today:

```json
{
  "search_id": "uuid",
  "user_id": "uuid",
  "image_url": "https://.../query.jpg",
  "threshold": 0.75,
  "max_results": 50,
  "metadata": { "camera_id": "..." }
}
```

The extended payload:

```json
{
  "search_id": "uuid",               // For purpose="search": the SearchRequest ID.
                                      // For purpose="blacklist_embed": the BlacklistImageReference ID.
  "user_id": "uuid",
  "image_url": "https://.../ref.jpg",
  "threshold": 0.75,                 // Ignored when purpose="blacklist_embed"
  "max_results": 50,                 // Ignored when purpose="blacklist_embed"
  "metadata": {...},
  "purpose": "blacklist_embed",      // NEW, optional, default "search"
  "blacklist_entry_id": "uuid"       // Present only when purpose="blacklist_embed"
}
```

Two minor cheats worth spelling out:

1. **`search_id` overloading.** For blacklist embeds, we pass the `BlacklistImageReference.id` here. The GPU doesn't care — it just treats the ID as an opaque echo value. The consumer decides what table to look it up in based on `purpose`. Cleaner than adding a parallel `reference_id` field that would be ignored in the search case.
2. **`threshold` and `max_results` pass through but are ignored** when `purpose="blacklist_embed"`. Could strip them, but the GPU ignores fields it doesn't need anyway. Omitting them from the payload builder is a minor readability win.

Backwards compatibility: legacy callers that don't set `purpose` get `"search"` by default. Zero risk to the existing search flow.

## Consumer dispatch

In [src/streams/search_results_consumer.py](../../src/streams/search_results_consumer.py), `_process_search_result` gets a top-level branch:

```python
async def _process_search_result(payload: dict, message_id: str):
    search_id = payload.get("search_id", "")
    user_id = payload.get("user_id", "")
    vector = payload.get("vector")
    purpose = payload.get("purpose", "search")

    if not search_id or vector is None:
        logger.warning(f"Skipping search result with missing data: {search_id}")
        return

    if purpose == "blacklist_embed":
        await _process_blacklist_embed_result(payload, message_id)
        return

    # purpose == "search" — existing logic unchanged
    ...


async def _process_blacklist_embed_result(payload: dict, message_id: str):
    """Store blacklist reference vector in Qdrant + DB, schedule reverse search."""
    reference_id = payload["search_id"]           # Overloaded — see §"The purpose field"
    entry_id = payload["blacklist_entry_id"]
    user_id = payload["user_id"]
    vector = payload["vector"]

    from ..services.blacklist_embed_service import store_blacklist_embedding
    await store_blacklist_embedding(
        entry_id=UUID(entry_id),
        reference_id=UUID(reference_id),
        user_id=user_id,
        vector=vector,
        stream_msg_id=message_id,
    )
```

The existing search path is untouched — its code runs when `purpose != "blacklist_embed"`.

## Error routing — `compute.error` does NOT carry `purpose`

Per the upstream compute contract (commit `639d753`, [CONTRACT.md §3.4](../../../image-embedding-compute/docs/CONTRACT.md)), the `compute.error` envelope published to `search:results` on GPU-side failure **deliberately omits `purpose` and `blacklist_entry_id`**. Compute's reasoning: the failure mode is structurally identical regardless of intent. See [../requirements/IMAGE_COMPUTE_STREAMS.md](../requirements/IMAGE_COMPUTE_STREAMS.md) §3 for the negotiation trail.

That means our `_handle_compute_error` path can't branch on `purpose` — it only has `entity_id` (which carries either a `SearchRequest.search_id` or a `BlacklistImageReference.id`, depending on the purpose of the original request). We resolve which one by looking it up:

```python
async def _process_compute_error(payload: dict):
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    # The producer ONLY emits entity_type="search" for the evidence:search flow
    # (both purposes share the same stream). Dispatch by looking up the id —
    # NOT by envelope fields, because compute.error doesn't carry `purpose`.
    if entity_type != "search":
        return

    async with get_session() as session:
        # Try blacklist first (shorter table, indexed lookup on id PK)
        bl_repo = BlacklistImageRepository(session)
        try:
            ref_uuid = UUID(entity_id)
        except ValueError:
            ref_uuid = None

        reference = await bl_repo.get_reference(ref_uuid) if ref_uuid else None

        if reference is not None:
            await bl_repo.update_reference_status(
                reference.id,
                status=BlacklistReferenceStatus.ERROR,
                error_message=f"Compute error: {error}"[:500],
            )
            logger.error(
                f"Blacklist embed compute error: ref={reference.id} err={error}"
            )
            return

        # Fall back to search request path (existing behavior)
        sr_repo = SearchRequestRepository(session)
        request = await sr_repo.get_by_search_id(entity_id)
        if request:
            request.status = SearchRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"[:500]
        # ... (existing else-branch preserved)
```

Two properties worth calling out:

1. **Lookup order matters.** `blacklist_image_references.id` is a UUID PK — a lookup miss is cheap (indexed scan, no rows returned). The fallback to `SearchRequestRepository.get_by_search_id` preserves today's behavior for non-blacklist errors.
2. **Collision impossible in practice.** Both tables use UUID PKs; a UUID minted for a `BlacklistImageReference` cannot collide with one minted for a `SearchRequest`. If some future contract hands us a non-UUID `entity_id`, the `UUID(entity_id)` parse guard drops us straight to the search path.

If compute ever decides to carry `purpose` on `compute.error` (we'd accept it gracefully), this lookup becomes redundant — but the fallback logic is cheap to leave in place as defense-in-depth.

## `BlacklistEmbedService` — the success path

New file `src/services/blacklist_embed_service.py`:

```python
"""Handle a completed blacklist reference embedding.

Called from search_results_consumer when purpose="blacklist_embed".
Stores the vector in Qdrant, upserts the BlacklistImageEmbedding row,
marks the reference as PROCESSED, and schedules the reverse-search job.
"""

async def store_blacklist_embedding(
    *,
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
    stream_msg_id: str,
) -> None:
    vector_repo = _vector_repo   # injected at startup (same pattern as other services)
    point_id = str(uuid4())

    # 1. Load the entry + reference to build the Qdrant payload
    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        entry = await repo.get_entry(entry_id)
        references = await repo.list_references(entry_id)
        reference = next((r for r in references if r.id == reference_id), None)
        if not entry or not reference:
            logger.error(
                f"Blacklist embed missing entry={entry_id} ref={reference_id}; dropping"
            )
            return

    # 2. Build Qdrant payload — see docs/image-blacklist/03_QDRANT.md
    payload = {
        "source_type": "blacklist",
        "blacklist_entry_id": str(entry_id),
        "blacklist_reference_id": str(reference_id),
        "user_id": user_id,
        "model_version": "clip-vit-b-32",
        "image_url": reference.image_url,
    }
    if entry.category:
        payload["category"] = entry.category

    # 3. Upsert to Qdrant
    await vector_repo.store_raw_point(
        point_id=point_id,
        vector=vector,
        payload=payload,
    )

    # 4. DB: embedding row + reference status + entry status
    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        await repo.create_embedding(
            entry_id=entry_id,
            reference_id=reference_id,
            qdrant_point_id=point_id,
            model_version="clip-vit-b-32",
        )
        await repo.update_reference_status(reference_id, status=3)  # PROCESSED

        # Entry becomes INDEXED once at least one reference is PROCESSED
        await repo.update_entry_status(entry_id, status=3)  # INDEXED

    logger.info(
        f"Stored blacklist embedding: entry={entry_id}, ref={reference_id}, "
        f"point={point_id}"
    )

    # 5. Schedule async reverse search (see blacklist_reverse_search.py)
    from .blacklist_reverse_search import schedule_reverse_search
    schedule_reverse_search(
        entry_id=entry_id,
        reference_id=reference_id,
        user_id=user_id,
        vector=vector,
    )
```

`store_raw_point` is a small new method on `QdrantVectorRepository` that accepts `(point_id, vector, payload)` directly — simpler than the existing `store_embedding(ImageEmbedding)` method, which is scoped to the `ImageEmbedding` domain entity. We don't want to conflate a blacklist point with an evidence `ImageEmbedding` — distinct domain objects, distinct methods.

Alternatively: add a `BlacklistEmbedding` domain entity in `src/domain/entities/` and a symmetric method. More formal, more code. The direct `store_raw_point` approach is lighter and we can promote to an entity later if it becomes muddled.

## Async reverse search

New file `src/services/blacklist_reverse_search.py`:

```python
"""Reverse search: when a new blacklist reference is embedded, find all
existing evidence that matches it, publish image:blacklist_match events.

Scheduled as an APScheduler one-shot job via `schedule_reverse_search`.
Progress tracked in a blacklist_reverse_search_jobs table (optional, v1
might skip and rely on logs).

Runs in pages of BLACKLIST_REVERSE_SEARCH_BATCH_SIZE to avoid loading
millions of evidence points into memory at once.
"""

def schedule_reverse_search(
    *,
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
) -> None:
    """Fire-and-forget: schedule a run_date=now APScheduler job.

    The scheduler runs it in a worker thread so the consumer returns
    immediately. If the backend restarts mid-job, the job is lost —
    deliberate tradeoff to keep v1 simple. Ops can re-trigger via the
    POST /backfill endpoint (Phase 06).
    """
    from datetime import datetime
    scheduler = _get_scheduler()   # wired in main.py lifespan
    scheduler.add_job(
        _run_reverse_search,
        trigger="date",
        run_date=datetime.utcnow(),
        args=(entry_id, reference_id, user_id, vector),
        id=f"reverse_search_{reference_id}",
        replace_existing=True,
    )


async def _run_reverse_search(
    entry_id: UUID,
    reference_id: UUID,
    user_id: str,
    vector: list[float],
) -> None:
    """Search all historical evidence against the new blacklist vector,
    publish image:blacklist_match for each match."""
    threshold = settings.blacklist_match_threshold
    batch_size = settings.blacklist_reverse_search_batch_size

    vector_repo = _vector_repo
    filter_conditions = build_evidence_only_filter({"user_id": user_id})

    # Qdrant's scroll+search handles batching under the hood for search_similar.
    # For reverse search we just set a generous limit — if it's too big, we
    # loop with offset (not supported directly by search_similar, so we may
    # need a paginated variant).
    matches = await vector_repo.search_similar(
        query_vector=np.array(vector, dtype=np.float32),
        limit=batch_size,
        threshold=threshold,
        filter_conditions=filter_conditions,
    )

    logger.info(
        f"Reverse search complete: entry={entry_id} ref={reference_id} "
        f"matches={len(matches)}"
    )

    # Publish image:blacklist_match for each match (Phase 05 covers the DTO
    # and publishing logic)
    for match in matches:
        await _publish_blacklist_match(
            entry_id=entry_id,
            match=match,
            reason="reverse_search",  # vs "inline_match" from Phase 05
        )
```

### Why APScheduler instead of ARQ or raw asyncio

- **ARQ requires a separate worker process.** We have `src/workers/` but adding another queue adds operational surface. v1 prefers in-process.
- **Raw `asyncio.create_task`** works but fire-and-forgets without any observability — if the coroutine dies, we get a silent failure.
- **APScheduler** is already in the process, has a job registry (we can query "is this job running?"), and supports replace-existing for idempotency. One-shot `trigger="date"` jobs are exactly the right primitive.

## Evidence limit — what if there are millions of evidence points?

At current volume (~thousands): a single `search_similar` call with `limit=batch_size=1000` returns all matches above threshold in one shot. Fine.

At scale (10M+ evidence): Qdrant's HNSW search is already O(log N) for the graph traversal, but the post-threshold match list could be huge if the threshold is loose. Two mitigations:

1. **Tighten the threshold.** The whole point of the global `BLACKLIST_MATCH_THRESHOLD` is to keep match counts manageable. If a reverse search returns 10,000 matches, the threshold is wrong — fix it live via env var.
2. **Batch via a paginated scroll.** If we ever need to process millions of matches, add a `search_similar_paginated(offset=0, batch=1000)` variant to the vector repo, loop until matches drop below threshold. v1 doesn't need it.

## Configuration

New env vars in [src/infrastructure/config.py](../../src/infrastructure/config.py):

```python
# Image blacklist — Phase 04
blacklist_match_threshold: float = Field(
    0.85,  # Conservative default — CLIP semantic matches are noisier than face identity matches
    validation_alias="BLACKLIST_MATCH_THRESHOLD",
)
blacklist_reverse_search_batch_size: int = Field(
    1000,
    validation_alias="BLACKLIST_REVERSE_SEARCH_BATCH_SIZE",
)
```

Threshold default of **0.85** is higher than the evidence-search default of 0.75 because false positives are more harmful here (they'd trigger WhatsApp alerts). Product should tune after observing real data.

## Failure semantics

| Failure | Behavior |
|---|---|
| `purpose="blacklist_embed"` result arrives but the entry was deleted | Log error, drop the message. Reference is orphaned — could leave a zombie row but `ON DELETE CASCADE` prevents that at the DB level. |
| Qdrant upsert fails | Log error, do NOT mark reference as PROCESSED. Retry on consumer's next XREADGROUP delivery (existing DLQ mechanism). |
| DB upsert fails after Qdrant succeeded | Qdrant has the orphan point; DB has no record. Reconciliation job (future, out of scope) would detect and clean up. Alternative: store DB row first, then Qdrant, rollback DB on Qdrant failure. Chose Qdrant-first because the point_id is the join key — simpler to reason about. |
| GPU-side CLIP inference fails (`compute.error` envelope) | Consumer can't branch on `purpose` (upstream omits it on errors). Dispatch by looking up `entity_id` in `blacklist_image_references` first, fall through to `search_requests`. On match: set `BlacklistReferenceStatus.ERROR`. See §"Error routing" above. |
| Reverse search job fails mid-run | Partial matches published, rest lost. Re-triggerable via `POST /api/v1/blacklist/entries/{id}/backfill` (Phase 06). |
| Backend restart mid-reverse-search | Job lost. Same re-trigger mechanism. |

All failure modes are tracked in the reference status (1=TO_PROCESS / 2=PROCESSING / 3=PROCESSED / 4=ERROR) — ops can query stuck references via SQL.

## Tests

- Unit tests for the consumer dispatch: verify `purpose` branching calls the right handler.
- DB test: call `store_blacklist_embedding` with a mocked `_vector_repo`, assert DB rows and statuses land correctly.
- APScheduler test: schedule a reverse search, assert the job is registered with the expected ID.
- Threshold test: env var default + override works via `get_settings()`.

## Verification

Manual steps after deploying this phase *and* Phase 03:

1. Publish a synthetic `evidence:search` message with `purpose="blacklist_embed"` (pre-existing GPU-side embed logic runs, publishes to `search:results`).
2. Observe the consumer log line: `"Stored blacklist embedding: entry=... ref=... point=..."`
3. Query the Qdrant point by ID — payload should show `source_type="blacklist"`, `blacklist_entry_id` set.
4. SQL: `SELECT status FROM blacklist_image_references WHERE id = '...'` returns 3 (PROCESSED).
5. APScheduler registry: `scheduler.get_jobs()` briefly shows the `reverse_search_<ref_id>` job (it runs immediately and disappears).

Once these check, Phase 05 wires up the match-publishing to report-generation.
