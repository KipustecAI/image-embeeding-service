# On-Demand Image Index — Validated Design

Post-audit design for the additive / isolated / gated-OFF batch-index feature. Every seam that the
six draft dimensions disagreed on has been resolved to a single answer here (§11 is the fix log).
The compute wire (`image:index` / `image:index:results`) is **not** re-specified — it lives in the
companion [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md); this
doc points at it where an item crosses that boundary.

> **Status of the compute wire:** the companion is still **v1-DRAFT**. Phases 1–2 build against a
> stable submit/lifecycle envelope we own; the **land logic (Phase 3) and any live dispatch are
> gated on the compute v1-FREEZE**. Our terminal/reconciliation logic holds whether or not compute
> ever emits `filtered` (it stays 0).

---

## 1. Wire topology

All Redis on **DB 3**, two-field hash `{event_type, payload:<json string>}`, published via the
existing `StreamProducer.publish(stream=…, event_type=…, payload=…)` (`src/streams/producer.py`).
`StreamProducer.publish` takes `stream` as a **required positional** with no default — the etl
`EventPublisher` `split('.')[0]` landmine (routing `image_batch.created` to a stream literally named
`image_batch`) is **structurally impossible** here. Every lifecycle publish still passes `stream=`
explicitly for readability.

```
SUBMIT    coordinator ──XADD──► image:index:submit      event image.index.submit      (we consume)
DISPATCH  us          ──XADD──► image:index             event image.index.compute     (compute consumes)  [companion §1]
RESULTS   compute     ──XADD──► image:index:results     event image.index.computed     (we consume)        [companion §2]
ERROR     compute     ──XADD──► image:index:results     event compute.error            (we consume)        [companion §3]
LIFECYCLE us          ──XADD──► image_batch:raw         event image_batch.{created,completed,failed}        (coordinator consumes)
QUERY     frontend    ──HTTP──► GET /api/v1/image-index/results/{batch_id | by-external-id/{external_id}}
```

| Direction | Stream | event_type | We | File |
|---|---|---|---|---|
| coordinator → us | `image:index:submit` | `image.index.submit` | consume | `image_index_submit_consumer.py` |
| us → compute | `image:index` | `image.index.compute` | produce (fresh mint only) | `image_index_submit_consumer.py` |
| us → coordinator | `image_batch:raw` | `image_batch.created` | produce | submit consumer |
| compute → us | `image:index:results` | `image.index.computed` | consume | `image_index_results_consumer.py` |
| compute → us | `image:index:results` | `compute.error` | consume | `image_index_results_consumer.py` |
| us → coordinator | `image_batch:raw` | `image_batch.completed` / `image_batch.failed` | produce | results consumer + reaper |
| auto DLQ | `image:index:submit:dead`, `image:index:results:dead` | verbatim + audit fields | `StreamConsumer._dead_letter` (reused) | `consumer.py` |

**Lifecycle event_type is `image_batch.*` (domain-prefixed), not bare `batch.*`.** This is the
string the dw-offline handler registers on; a mismatch means the message is unhandled, `batch_id`
is never bound, and the no-HTTP coordinator hangs — the exact failure this feature exists to
prevent. Confirm the strings with the dw-offline agent before freeze (open item #2).

---

## 2. Canonical Settings block — `src/infrastructure/config.py`

**One attribute + one env alias per value.** Every dimension references these exact names; a
Phase-1 unit test asserts each attribute the consumers/service/reaper read actually exists.
Appended to `Settings` (mirrors the `recalculation_enabled` flag template + the existing `stream_*`
fields). All Redis on the existing `redis_streams_db` (DB 3).

```python
# ── On-demand image-index (ADDITIVE, ISOLATED, gated-OFF) ──
image_index_enabled: bool = Field(False, validation_alias="IMAGE_INDEX_ENABLED")

# Streams (locked / playbook defaults) — VALUES agree across all dimensions; names are canonical here.
stream_image_index_submit: str = Field("image:index:submit", validation_alias="STREAM_IMAGE_INDEX_SUBMIT")     # coordinator → us
stream_image_index:        str = Field("image:index",         validation_alias="STREAM_IMAGE_INDEX")            # us → compute (dispatch)
stream_image_index_results:str = Field("image:index:results", validation_alias="STREAM_IMAGE_INDEX_RESULTS")   # compute → us
stream_image_batch_raw:    str = Field("image_batch:raw",     validation_alias="STREAM_IMAGE_BATCH_RAW")        # us → coordinator (lifecycle)

# Dedicated consumer groups (isolated from the live backend-workers group)
image_index_submit_group:  str = Field("image-index-submit",  validation_alias="IMAGE_INDEX_SUBMIT_GROUP")
image_index_results_group: str = Field("image-index-results", validation_alias="IMAGE_INDEX_RESULTS_GROUP")

# Dedicated Qdrant collection (own ensure/latch; NOT in the live initialize())
qdrant_collection_image_index: str = Field("image_index_embeddings", validation_alias="QDRANT_COLLECTION_IMAGE_INDEX")

# Knobs
image_index_n_cap:                  int = Field(100,    validation_alias="IMAGE_INDEX_N_CAP")                    # N_CAP (companion §1)
image_index_batch_maxlen:           int = Field(20_000, validation_alias="IMAGE_INDEX_BATCH_MAXLEN")            # image_batch:raw trim
image_index_max_compute_seconds:    int = Field(900,    validation_alias="IMAGE_INDEX_MAX_COMPUTE_SECONDS")    # reaper cutoff
image_index_reaper_interval_seconds:int = Field(60,     validation_alias="IMAGE_INDEX_REAPER_INTERVAL_SECONDS")# reaper cadence (USED by add_job, not hardcoded)
```

Vector size reuses the existing `qdrant_vector_size` (512) — no new dimension knob. Search-back
defaults (`image_index_search_threshold`, `image_index_search_limit`) are **deferred to v1.1** along
with the search endpoint (§7).

**Difference from the template:** `recalculation_enabled` defaults **True** (a live feature);
`image_index_enabled` defaults **False** — it is the bring-up switch, flipped to the standard
prod-on state only after the Phase 5 live smoke. Checked **once per surface at wiring time**
(`src/main.py` lifespan), never in a hot per-message path.

---

## 3. Data model + migration

Two dedicated tables in a new file `src/db/models/image_index.py` (both models colocated), one
additive migration off the current head `e7f2c9a1b3d6`. Copy-template: `blacklist_image.py` +
`e7f2c9a1b3d6_create_blacklist_image_tables.py`. No ALTER of any live table.

> **Landmine:** `metadata` is a **reserved attribute** on SQLAlchemy's `DeclarativeBase`
> (`Base.metadata` is the `MetaData` registry). The physical column stays `metadata` (per locked
> spec) but is mapped through an aliased Python attribute: `batch_metadata = Column("metadata", JSONB)`.
> Declaring `metadata = Column(...)` raises `InvalidRequestError` at import time.

### `t_image_index_batches` — the job

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK (`default=uuid.uuid4`) | the `batch_id` we mint and echo to compute |
| `external_id` | String(255), nullable, **indexed** | coordinator run id (**non-unique**; groups re-runs) |
| `client_batch_ref` | String(255), nullable, **UNIQUE** | idempotency anchor; Postgres allows multiple NULLs so a ref-less submit is never blocked |
| `user_id` | String(255), not null, **indexed** | tenant (Redis-trusted) |
| `status` | String(32), not null, default `pending`, **indexed** | `pending → computing → {completed \| completed_with_errors \| error}` (CheckConstraint) |
| `submitted_count` | Integer, not null, default 0 | reconciliation base |
| `embedded_count` | Integer, not null, default 0 | absolute (GROUP BY) |
| `filtered_count` | Integer, not null, default 0 | absolute; clean disposition |
| `failed_count` | Integer, not null, default 0 | absolute; **folds** download_failed + decode_failed + no_result |
| `source_ref` | Text, nullable | provenance (`run_id/class_name`) |
| `batch_metadata` | JSONB (`Column("metadata", …)`), nullable | free-form passthrough |
| `error_message` | Text, nullable | set only on batch-level `error` |
| `created_at` | DateTime, `default=utcnow`, **indexed** | |
| `updated_at` | DateTime, `default/onupdate=utcnow` | |

`__table_args__`: `CheckConstraint("status IN ('pending','computing','completed','completed_with_errors','error')", name="ck_image_index_batches_status")`.
`results` relationship → `cascade="all, delete-orphan"`.

**Count vocabulary is the 4-key folded shape** `{submitted, embedded, filtered, failed}` — the same
shape the lifecycle payload publishes and the REST surface returns (§6, §7). There is exactly one
count vocabulary in the whole feature.

### `t_image_index_results` — per-item disposition + Qdrant ref

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `batch_id` | UUID FK → `t_image_index_batches.id` `ondelete=CASCADE`, not null, **indexed** | |
| `item_ref` | String(255), not null | caller's `item_id`, echoed by compute (join key) |
| `source_url` | Text, nullable | the dispatched image URL |
| `item_index` | Integer, not null | 0-based submit position; deterministic point-id basis |
| `status` | String(32), not null, **indexed** | `embedded \| download_failed \| decode_failed \| filtered \| no_result` (CheckConstraint) |
| `qdrant_point_id` | String(255), nullable, UNIQUE | set iff `status=='embedded'`; deterministic (§4) |
| `duplicate_of_index` | Integer, nullable | kept-unique's `item_index` when `status=='filtered'` |
| `error_message` | Text, nullable | short reason on a failed disposition |
| `created_at` / `updated_at` | DateTime | |

`__table_args__`:
- `UniqueConstraint("batch_id", "item_index", name="uq_image_index_result_item")` — the **land-idempotency key**; a redelivered result re-upserts the same row rather than double-counting.
- `UniqueConstraint("qdrant_point_id", name="uq_image_index_result_point")` — belt-and-suspenders. (The point-id is deterministic `uuid5(batch_id,item_index)` and `item_index` is already unique per batch, so this can only fire on the same redelivery the composite key already collapses. Kept, but it is **not** the primary idempotency guard.)
- `CheckConstraint("status IN ('embedded','download_failed','decode_failed','filtered','no_result')", name="ck_image_index_results_status")`.

**`filtered` / `duplicate_of_index` are forward-compatible, not load-bearing.** Compute open-item
#3 leaves diversity-dedup optional; if compute never emits `filtered`, these stay null/0 and the
terminal rule is unaffected. **Do not build logic that assumes `filtered` ever fires** until compute
confirms — `filtered_count == 0` is the expected v1 state.

### Status constants — `src/db/models/constants.py` (append)

Two new classes, colocated like `BlacklistEntryStatus`. **Stored as TEXT** (CheckConstraint) so the
vocabulary lines up 1:1 with the coordinator lifecycle — unlike the integer status-machines the
live evidence/search tables use.

```python
class ImageIndexBatchStatus:
    PENDING = "pending"; COMPUTING = "computing"; COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"; ERROR = "error"

class ImageIndexResultStatus:   # canonical name (NOT ImageIndexItemStatus)
    EMBEDDED = "embedded"; DOWNLOAD_FAILED = "download_failed"; DECODE_FAILED = "decode_failed"
    FILTERED = "filtered"; NO_RESULT = "no_result"
    FAILED_SET = frozenset({DOWNLOAD_FAILED, DECODE_FAILED, NO_RESULT})   # → folds into failed_count
```

Register both models in `src/db/models/__init__.py` (import + `__all__`) so `alembic/env.py` autogen
sees them.

### Migration — `alembic/versions/<rev>_create_image_index_tables.py`

Pure additive `create_table` ×2 (`down_revision = "e7f2c9a1b3d6"` — confirmed single head via
`alembic heads`). `server_default` on status/counters so the DDL is backfill-safe; ORM `default=…`
set too so raw-SQL and ORM inserts stay consistent. Explicit named indexes on
`external_id`/`user_id`/`status`/`created_at` (batches) and `batch_id`/`status`/`created_at`
(results). `downgrade()` drops only the two tables. **The migration is unconditional — the flag
gates code paths, never DDL.**

> **CI hygiene (load-bearing):** this repo has **no `tests/conftest.py`** today, and the existing
> suite commits to real Postgres, so rows already leak across tests. Phase 1 adds
> `tests/conftest.py` with an autouse fixture that runs `TRUNCATE … RESTART IDENTITY CASCADE` over
> at minimum the two new tables, **guarded to only-existing tables** (query `information_schema`, or
> ensure `alembic upgrade head` ran in fixture setup) so it doesn't error when migrations aren't
> applied. This is a hard Phase-1 deliverable, not deferred — the redelivery/idempotency tests are
> only trustworthy once it exists.

---

## 4. Dedicated Qdrant collection — `image_index_embeddings`

**One standalone repo class in a new file** `src/infrastructure/vector_db/image_index_vector_repository.py`,
with its **own `_ensured` latch** and an ensure method that is **never called from the live
`QdrantVectorRepository.initialize()`**. This is the real isolation requirement: `initialize()`
(`qdrant_repository.py:62-118`) **raises on any failure** and is `await`ed **unguarded** in the
lifespan (`main.py:96-97`) — adding the new collection there would let a create/index failure wedge
the live evidence/search/blacklist path (playbook §6). A separate file/class structurally prevents a
future edit from wiring the ensure into `initialize()`.

**The repo shares the live repo's `QdrantClient`** (`self.client` injected from the already-built
`QdrantVectorRepository`) rather than constructing a second client — no duplicated construction
logic, no doubled socket count. The isolation guarantee comes from the separate ensure + latch, not
from a separate client.

**Every synchronous qdrant-client call runs under `asyncio.to_thread`** — including the write path.
All consumers (live + new) bridge handlers onto the single shared event loop via
`run_coroutine_threadsafe`; a 100-item `upsert(wait=True)` on the loop thread would stall the live
embed/search result handlers. Wrap `_ensure_collection`, `upsert_items`, `get_point_vector`, and
`delete_by_batch`.

```python
IMAGE_INDEX_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lookia.image-index.v1")   # FROZEN — re-keys every point if changed

def image_index_point_id(batch_id: str, item_index: int) -> str:
    """Deterministic → a redelivered result overwrites the SAME point in place."""
    return str(uuid.uuid5(IMAGE_INDEX_NS, f"{batch_id}_{item_index}"))

# ids-only, face-style payload: the 512-D vector IS the payload. Keyword indices ONLY on the filter
# fields; item_index/item_ref/source_url are stored-but-unindexed (returned, never filtered).
_IMAGE_INDEX_PAYLOAD_INDICES = [("user_id", "keyword"), ("external_id", "keyword"), ("batch_id", "keyword")]
```

`ImageIndexVectorRepository` surface (v1):
- `initialize(client)` — accept the shared client, run `_ensure_collection()` (collection_exists-guarded, idempotent, never recreate/delete; `create_payload_index` failures swallowed as benign-conflict, matching the live repo policy).
- `upsert_items(points: list[dict]) -> bool` — batch-upsert embedded items. Each point-id is deterministic; `distance=COSINE`; **no re-normalization** (Qdrant normalizes internally, compute already returns a CLIP vector — same as the live `search_similar`). Payload = `{user_id, external_id, batch_id, item_index, item_ref, source_url}`.
- `delete_by_batch(batch_id) -> bool` — filter-delete a whole batch's points; provided for optional re-run cleanup, **not auto-invoked in v1** (external_id scoping surfaces the newest run).

**Deferred to v1.1** (see §7): `search`, `get_point_vector`, `exclude_point_id`. v1 stores
searchable vectors; the query-time similarity endpoint is not part of v1.

Wiring — gated, isolated, in `main.py` lifespan **after** the live `await vector_repo.initialize()`:

```python
image_index_repo = None
if settings.image_index_enabled:
    try:
        image_index_repo = ImageIndexVectorRepository(settings)
        await image_index_repo.initialize(vector_repo.client)   # SHARE the live client
        # inject the DEDICATED repo (not vector_repo) into the results consumer
        set_results_vector_repo(image_index_repo)
        logger.info("Image-index collection ready (%s)", settings.qdrant_collection_image_index)
    except Exception:
        logger.exception("Image-index Qdrant init failed — feature stays degraded, live path intact")
        image_index_repo = None
```

The try/except is the isolation latch at the wiring level: the live `vector_repo` is already up, and
no exception here can propagate into the live path.

---

## 5. Streams, consumers, and reaper

Three new in-process daemons/jobs, all gated on `IMAGE_INDEX_ENABLED`, reusing
`src/streams/consumer.py::StreamConsumer` (daemon, `XREADGROUP >`, ACK-on-success,
`XPENDING`/`XCLAIM` reclaim, `{stream}:dead` DLQ) and `StreamProducer` **verbatim**. The
sync→async bridge is a **module-global `_event_loop` + `asyncio.run_coroutine_threadsafe(coro,
loop).result(timeout)`** — mirroring `evidence_consumer.py` / `search_results_consumer.py`, **not**
etl's `run_coro` seam (our `StreamConsumer` has no such hook). `StreamProducer.publish` is a
synchronous Redis call invoked inline from the async handler, exactly as
`embedding_results_consumer.py:339` does.

### 5.1 Submit-intake consumer — `image_index_submit_consumer.py`

Consumes `image:index:submit` (event `image.index.submit`), group `image-index-submit`. Handler
timeout 120s.

**TIER 1 — unbindable → LOG LOUD + `return` (ACK-drop, NEVER raise):** payload not a dict; `user_id`
missing/blank/not a UUID (nothing to bind a tenant to); `client_batch_ref` missing/blank (the
idempotency anchor).

**Atomic idempotent mint** — `service.submit_batch_created(payload)` → `(batch, created, error)`.
`create_or_get_batch` is **`INSERT … ON CONFLICT (client_batch_ref) DO NOTHING RETURNING id`**:
`created=True` iff a row was returned; otherwise `SELECT` the existing row with `created=False`
(equivalently, catch `IntegrityError` and re-`SELECT`). **`created` derives from the atomic outcome,
never from a check-then-insert** — so two concurrent redeliveries (XCLAIM / >1 replica) yield exactly
one batch and exactly one dispatch. The service runs the TIER-2 guards (empty `items` / over
`image_index_n_cap` / non-`http(s)` `image_url` / missing per-item `item_id` / missing `external_id`).

**TIER 2 — bindable-but-rejected (`error is not None`) → persist ERROR batch + `return` (ACK):**
`service.create_error_batch(...)` (idempotent), then publish `image_batch.failed` to the lifecycle
stream. The no-HTTP coordinator MUST get a loud terminal, never a silent drop. **No dispatch.** (An
over-cap ERROR batch stores `submitted_count = N` with zero result rows — the reconciliation
invariant §8 explicitly **exempts** `status=='error'` batches.)

**Success → dispatch + set `computing` + publish, on a fresh mint only (`created is True`):**
1. Persist + commit (session `__aexit__`).
2. `_producer.publish(stream=settings.stream_image_index, event_type="image.index.compute", payload={batch_id, user_id, items:[{item_id, image_url, item_index}], metadata})` — the dispatch (companion §1).
3. **Set `status='computing'`** on the batch immediately after a successful dispatch — this is the single actor that writes `computing`, so it is never a dead state and the reaper predicate (`status IN (pending, computing)`) is meaningful.
4. `_producer.publish(stream=settings.stream_image_batch_raw, event_type="image_batch.created", payload=<lifecycle>, maxlen=settings.image_index_batch_maxlen)`.

**Ordering:** commit → dispatch → `computing` → `image_batch.created`, so the coordinator never sees
`image_batch.created` for a batch that failed to dispatch. A **duplicate/redelivered** submit
(`created is False`) does **not** re-dispatch and does **not** mint a second batch; it re-publishes
`image_batch.created` (the only channel by which dw-offline learns `batch_id`, so re-announce in case
a prior lifecycle publish was lost) and ACKs.

**Raise vs. ACK:** only genuine transient DB/Redis failures (session error, `_producer` missing,
`publish` raising) propagate → not ACKed → PEL → XCLAIM retry → DLQ after
`dead_letter_max_retries`. Because the mint is idempotent, a retry re-binds the same batch.

> **Pre-mint DLQ gap (documented, not auto-recovered):** if a submit fails hard *before* the batch
> row is minted (Postgres down during create-or-get), it lands in `image:index:submit:dead` with no
> batch row — the reaper (which sweeps `t_image_index_batches`) never sees it, so the coordinator
> gets neither `image_batch.created` nor `image_batch.failed`. **The reaper's guarantee starts only
> after a batch is minted.** Pre-mint failures are surfaced by a **metric/alert on
> `image:index:submit:dead` (and `image:index:results:dead`) depth**, not by auto-recovery. Ship the
> alert with Phase 2/3.

### 5.2 Results consumer — `image_index_results_consumer.py`

Consumes `image:index:results`, group `image-index-results`, **two** event types on the one stream:
`image.index.computed` (timeout 300s) and `compute.error` (timeout 60s).

`_process_computed(payload)` → `service.land_computed(session, payload, vector_repo=image_index_repo)`,
which does all the heavy work **idempotently**:
1. For each `results[]` item → **upsert the reference row by `(batch_id, item_index)`** (`INSERT … ON CONFLICT (batch_id, item_index) DO UPDATE`; a redelivery overwrites in place).
2. If `status=='embedded'` and `vector` present → collect into a **single batch** `upsert_items(...)` call on the dedicated `image_index_embeddings` collection with the deterministic point-id (redelivery overwrites the same point; the indexed image becomes searchable — face-style). Failures/`filtered`/`no_result` store the disposition + `error_message`, no vector. The reference row stores the same deterministic `qdrant_point_id` (recomputable via `image_index_point_id()`, no round-trip).
3. **Recompute counts ABSOLUTELY** via `SELECT status, COUNT(*) … WHERE batch_id=:id GROUP BY status`, written into the batch columns. **Never `+= n`** — redelivery-safe.
4. **Reconciliation check + terminal-from-counts** via the single shared helper (§8): if `embedded+filtered+failed < submitted` → **do NOT terminalize** (leave `computing`, log-loud the shortfall; the reaper is the backstop). Else `completed` iff `failed==0`, else `completed_with_errors`.
5. Build the `image_batch.completed` payload while ORM attrs are loaded, commit, then publish to `image_batch:raw` **before ACK**.

`None` return ⇒ results for a `batch_id` we never minted → **ACK no-op** (we never mint in the
results leg).

`_process_compute_error(payload)` — **key divergence from the live flow:** our compute contract
(companion §3) keys the batch-level error on **`batch_id` + `error_message`**, NOT the
`entity_id`/`entity_type` shape the live `embedding_results_consumer`/`search_results_consumer` use.
Because this is a dedicated stream + dedicated group there is no collision — read
`payload["batch_id"]` / `payload["error_message"]` per our own contract. **This must be called out
in the module docstring** or a future maintainer copying the live consumer will read
`payload["entity_id"]` and silently no-op every batch-level error. `service.mark_error_from_compute_error(...)`
→ terminalize `error` (last-writer-wins) → publish `image_batch.failed`. Unknown/never-minted
`batch_id` → ACK no-op.

Same raise/ACK rule as the submit leg: a hard publish/DB failure raises → not ACKed → PEL retry;
`land_computed` idempotency makes the re-land a no-op that re-publishes the terminal event.

### 5.3 Reaper — `image_index_reaper.py`

A batch whose compute crashed / result was lost stays `pending`/`computing` forever, and the
coordinator is no-HTTP → it hangs. Reuse the **existing `AsyncIOScheduler`** in the `main.py`
lifespan (same pattern as `recover_stale_working`), not etl's standalone `asyncio.Task`.

```python
async def reap_stuck_image_index_batches():
    settings = get_settings()
    if not settings.image_index_enabled:   # defensive; job only added when enabled
        return
    cutoff = datetime.utcnow() - timedelta(seconds=settings.image_index_max_compute_seconds)
    failed_payloads = []
    async with get_session() as session:
        repo = ImageIndexBatchRepository(session)
        for batch in await repo.list_stuck_active(cutoff):   # status IN (pending, computing) AND created_at < cutoff
            await repo.mark_failed(batch, "compute timeout")
            failed_payloads.append(ImageIndexService.build_batch_failed_payload(batch))
        # terminal DB mark committed on __aexit__ FIRST — unblocks the stuck state
    for p in failed_payloads:                                # best-effort publish AFTER commit
        try:
            _producer.publish(stream=settings.stream_image_batch_raw,
                              event_type="image_batch.failed", payload=p,
                              maxlen=settings.image_index_batch_maxlen)
        except Exception:
            logger.exception("Reaper publish image_batch.failed failed (batch already marked error) id=%s", p.get("batch_id"))
```

Registered **only when the flag is on**, using the **configured interval** (not a literal):

```python
scheduler.add_job(reap_stuck_image_index_batches,
                  IntervalTrigger(seconds=settings.image_index_reaper_interval_seconds),
                  id="image_index_reaper", misfire_grace_time=30)
```

The DB mark is committed before the publish and a marked batch is no longer stuck, so a failed
publish is not retried by a later sweep — **acceptable for a coarse 60s safety net**; a transient
Redis outage spanning the sweep loses those notifications (the REST reconcile-by-`external_id` is the
coordinator's backstop). **Multi-replica caveat:** `list_stuck_active` is a plain SELECT
(SQLite-portable for CI); switch to `FOR UPDATE SKIP LOCKED` before running >1 API replica
(single-replica today, open item #5).

### 5.4 Cold-start wiring + ordering — `main.py` lifespan (single gated block)

`StreamConsumer._ensure_group` creates groups at `id="$"` (`consumer.py:97`), so a group only sees
messages XADD'd **after** it exists. **Register + start the results consumer BEFORE the submit
consumer**: a fresh-mint submit dispatches to `image:index`; compute replies to
`image:index:results`. If the submit leg started before the results group existed, a fast compute
reply could land before the group and be invisible.

```python
# 5. On-demand image-index (ADDITIVE, gated) — the SINGLE gating chokepoint for all runtime surfaces.
if settings.image_index_enabled:
    # results FIRST so its group exists before any dispatch can produce a reply
    set_results_event_loop(loop); set_results_producer(stream_producer); set_results_vector_repo(image_index_repo)
    image_index_results_consumer = create_image_index_results_consumer(); image_index_results_consumer.start()

    set_submit_event_loop(loop); set_submit_producer(stream_producer)
    image_index_submit_consumer = create_image_index_submit_consumer(); image_index_submit_consumer.start()

    set_reaper_producer(stream_producer)
    scheduler.add_job(reap_stuck_image_index_batches,
                      IntervalTrigger(seconds=settings.image_index_reaper_interval_seconds),
                      id="image_index_reaper", misfire_grace_time=30)
    logger.info("Image-index on-demand flow ENABLED (results→submit consumers + reaper)")
# shutdown: image_index_results_consumer.stop() / image_index_submit_consumer.stop(), flag/None-guarded
```

External legs are covered by the flag + bilateral bring-up: (a) dw-offline must not XADD to
`image:index:submit` until our submit group exists (flag flipped only after we're live); (b) we must
not dispatch to `image:index` until compute's group exists (compute signals live first — companion §4).

---

## 6. Coordinator submit + lifecycle contract (dw-offline-facing)

This is the envelope we hand coordinators. It owns the inbound **submit** (`image:index:submit`) and
the outbound **lifecycle** (`image_batch:raw`); the compute wire is the companion doc.

### 6.1 SUBMIT — coordinator publishes `image:index:submit`

Envelope `{event_type: "image.index.submit", payload: <json string>}`.

| payload field | Req | Notes |
|---|---|---|
| `user_id` | **Yes** | Tenant. Redis-trusted (no gateway). Missing/blank → logged + DROPPED. Must equal the tenant the frontend's gateway key resolves to, or the REST read 404s. |
| `client_batch_ref` | **Yes** | Per-submit **UNIQUE** idempotency anchor; echoed on every lifecycle event so you bind our `batch_id` ↔ your submit. |
| `external_id` | **Yes** | Your run id (**NON-unique**; groups re-runs; the recover-by key). |
| `items` | **Yes** | `1..N_CAP` objects `{item_id, image_url}`. |
| `source_ref` | opt | free-form origin (e.g. `run_id/class_name`); preserved on the batch. |
| `metadata` | opt | free-form JSON; preserved (`JSONB`). |

**`items[]`:** `item_id` (**Yes** — your stable per-item ref, e.g. dw `evidence_id`; compute echoes
it verbatim; stored as `item_ref`), `image_url` (**Yes** — durable public http(s); compute fetches
directly; presigned/expiring → `download_failed`). We mint `item_index` (0-based) ourselves at
dispatch; the caller does not send it.

> **Naming note:** the submit uses `items[]/item_id` (not face/plates' `images[]/image_id`) so the
> join-key name is identical end-to-end with the compute envelope (companion §1). Call this out to
> integrators copying a face/plates snippet.

**Cap `N_CAP = 100`** images/batch. Over-cap (with `user_id` + `client_batch_ref` present) → persist
an ERROR batch + `image_batch.failed` (never a silent drop); split larger runs coordinator-side under
one `external_id`.

**Two-tier rejection** (§5.1): malformed/untrusted & unbindable → log + ACK-drop;
bindable-but-rejected → persist ERROR batch + `image_batch.failed`.

### 6.2 CONFIRM — coordinator consumes `image_batch:raw`

Attach your own consumer group (e.g. `dwoff-image-index-acks`). Events
`{event_type, payload:<json string>}`, event_type **`image_batch.created` / `image_batch.completed`
/ `image_batch.failed`**. Every event carries `batch_id`, `client_batch_ref`, `external_id`,
`user_id`, `status`, `source_ref`, `counts`, `created_at`, `completed_at`.

**`counts` — the single 4-key folded vocabulary** (identical to the DB columns and the REST
response), absolute (recomputed by GROUP BY, never incremented):

```
{submitted, embedded, filtered, failed}   where failed = download_failed + decode_failed + no_result
```

Reconciliation (computed batches only): `submitted == embedded + filtered + failed`.

**Status machine** `pending → computing → {completed | completed_with_errors | error}`. `completed`
iff `failed == 0` (and all items accounted); `completed_with_errors` iff `failed > 0`; `error` only
for a **batch-level** failure (bad/over-cap submit, a `compute.error` on `batch_id`, a reaper-swept
timeout). Per-item failures ride **inside** a `completed`/`completed_with_errors` batch — they are
**not** a `image_batch.failed`. **`completed ≠ done`.**

```jsonc
// image_batch.created  (fresh mint, right after dispatch; status already 'computing')
{ "batch_id":"49c7861d-…", "client_batch_ref":"dwoff-run42-image-vehiculo-b0",
  "external_id":"obj_…run42", "user_id":"<tenant>", "status":"computing",
  "source_ref":"obj_…run42/Vehículo",
  "counts":{"submitted":2,"embedded":0,"filtered":0,"failed":0},
  "created_at":"2026-07-17T…", "completed_at":null }

// image_batch.completed  (terminal; here with one per-item failure → completed_with_errors)
{ "batch_id":"49c7861d-…", "client_batch_ref":"dwoff-run42-image-vehiculo-b0",
  "external_id":"obj_…run42", "user_id":"<tenant>", "status":"completed_with_errors",
  "counts":{"submitted":2,"embedded":1,"filtered":0,"failed":1},
  "completed_at":"2026-07-17T…" }

// image_batch.failed  (terminal; batch-level only)
{ "batch_id":"49c7861d-…", "client_batch_ref":"dwoff-run42-image-vehiculo-b0",
  "external_id":"obj_…run42", "user_id":"<tenant>", "status":"error",
  "error_message":"batch exceeds N_CAP=100 (got 137)",
  "counts":{"submitted":137,"embedded":0,"filtered":0,"failed":0} }
```

**Reliability:** best-effort publish + your consumer-group cursor replay (`image_batch:raw` is
`MAXLEN`-trimmed at `image_index_batch_maxlen`, default 20 000). A periodic reconcile-by-`external_id`
via the REST read is a safe backstop; the reaper terminalizes stuck batches so a lost compute reply
still becomes `error` + `image_batch.failed` — the coordinator never hangs.

### 6.3 dw-offline wiring — the 4th enrichment target `target="image"`

dw-offline already runs `face` / `plates` / `analysis` as Redis-only enrichment targets. `image`
(a.k.a. `clip`) is the **4th**, class-agnostic, mirroring the `analysis` path 1:1:

1. `POST /offline/enrichment {run_id, class_name, target:"image"}` → `202 {enrichment_id, total_crops, total_batches}`.
2. Gather that class's crops (`evidence.crop_url` — permanent public objects; `item_id = evidence_id`).
3. Split into ≤100-crop batches; XADD each to `image:index:submit` with `user_id` = run owner, `external_id = run_id`, `client_batch_ref = dwoff-{run_id}-image-{class}-b{idx}`, `source_ref = {run_id}/{class_name}`, `items[] = {item_id: evidence_id, image_url: crop_url}`.
4. Consume `image_batch:raw`: bind `batch_id` on `image_batch.created`, mark terminal on `.completed`/`.failed`, roll `counts` into the enrichment `summary`.
5. Recover by `external_id` (= `run_id`): `GET /api/v1/image-index/results/by-external-id/{run_id}?all=true`.

**Enrichment `summary` for `target:"image"` uses the same 4-key folded set** `{submitted, embedded,
filtered, failed}` — confirm dw-offline adds this roll-up when the `image` target row lands (open
item #2). `failed` is a normal per-crop disposition, not a job failure; `completed_with_errors`
still completes.

---

## 7. REST query surface (face-style, read-only)

Leg 3 of the playbook. A thin router `src/api/v1/routers/image_index.py` + schemas
`src/api/v1/schemas/image_index.py` + a read-only repo `src/db/repositories/image_index_repo.py`,
cloning the `blacklist_image.py` skeleton and the `search_request_repo.py` async-select idioms.
Reuses `get_user_context` (`src/api/dependencies.py`) verbatim.

**Router gating — mounted unconditionally, gated per-route by a 503 dependency.** Mount once at
module level (like `blacklist_image_router` at `main.py:206`), **not** inside the lifespan — mutating
the route table during ASGI startup is fragile and the three draft dimensions each mounted it
differently (double-registration risk). A shared dependency `require_image_index_enabled` returns
**503** when `image_index_enabled` is off **or** `image_index_repo is None` after an init failure.
This is a truer no-op-when-off than a missing mount (routes 503 rather than the gateway/app 404-ing).

### Endpoints

**`GET /api/v1/image-index/results/{batch_id}`** — query params `include_items` (bool, default
false; false = counts-only), `limit` (int, default 100, 1–500), `offset` (int, default 0).

**`GET /api/v1/image-index/results/by-external-id/{external_id}`** — `external_id` is non-unique.
Default returns the most-recent run; `?all=true` returns every run newest-first in a
`{external_id, count, batches:[…]}` envelope. Query params `all`, plus `include_items`/`limit`/
`offset`. **`?all=true` is bounded** — `list_batches_by_external_id` caps at `le=200` (matching the
bounded `get_items` paging); a >100-crop class fans out to many batches under one `external_id`, so
an uncapped list would be unbounded on a large backfill.

### IDOR — strict tenant scoping (no admin bypass)

**Resolved to strict:** the admin-sees-all convention (`scope=None`) is **dropped** — matching the
face-index sibling, the (deferred) search endpoint, and playbook §7.7. **Every** repo method that
takes `user_id` puts it in the WHERE; a tenant miss (unknown id **or** another tenant) collapses to
`None` → the router raises **`404`** (never `403`, never `200`-empty — no existence disclosure).
`get_items` is scoped transitively (its `batch_pk` came from an already-tenant-checked batch).
Missing `X-User-Id` → `401`.

```python
async def get_batch(self, batch_id, *, user_id):
    q = select(ImageIndexBatch).where(ImageIndexBatch.id == batch_id,
                                      ImageIndexBatch.user_id == user_id)   # user_id ALWAYS in WHERE
    return (await self.session.execute(q.limit(1))).scalar()
```

### Response (single batch, face-style)

```jsonc
{
  "batch_id":"a3c97b5a-…", "external_id":"run-42", "client_batch_ref":"dwoff-run42-b7",
  "status":"completed",                                  // pending|computing|completed|completed_with_errors|error
  "counts":{"submitted":3,"embedded":2,"filtered":0,"failed":1},   // 4-key folded, read from denormalized columns
  "source_ref":"run-42/vehicle", "created_at":"…", "completed_at":"…",
  "error_message":null,                                  // set iff status=="error"
  "items":[                                              // present only when include_items=true
    {"item_ref":"crop-000","source_url":"https://…/a.jpg","item_index":0,
     "status":"embedded","qdrant_point_id":"16d1a741-…","duplicate_of_index":null,"error_message":null},
    {"item_ref":"crop-002","source_url":"https://…/c.jpg","item_index":2,
     "status":"download_failed","qdrant_point_id":null,"duplicate_of_index":null,"error_message":"http 404"}
  ]
}
```

List envelope (`?all=true`): `{external_id, count, batches:[<single batch, items=[]>, …]}`.
**`counts` is read from the denormalized batch columns** (recomputed-on-land by the results
consumer) — the query surface **never recomputes**. There is **no `matched` field** — this flow has
no inline blacklist matching (images become searchable, not matched).

| Code | When |
|---|---|
| `401` | missing `X-User-Id` |
| `404` | no batch for `{batch_id}`/`{external_id}` **under this tenant** (row-missing and tenant-miss indistinguishable, by design) |
| `422` | bad `limit`/`offset`/`all` |
| `503` | `IMAGE_INDEX_ENABLED=false` or repo unavailable (`require_image_index_enabled`) |

### Gateway registration (prerequisite)

The public REST leg is blocked at the gateway until the route prefix is registered:
`/api/v1/embedding/image-index/*` → `/api/v1/image-index/*` (the existing embedding-service
rewrite), a clean sibling of `/api/v1/embedding/{search,stats,pipeline,recalculate}` so it passes
`test_route_config_no_tiene_prefijos_redundantes`. Until it lands the frontend gets a **gateway**
404 (`{"detail":"Route not found: …"}`), distinct from our tenant-miss 404. The Redis submit/lifecycle
legs do not depend on the gateway, so the no-HTTP coordinator is unblocked regardless (open item #3).

### Deferred to v1.1 — the search endpoint

`POST /api/v1/image-index/search` (search-by-point + search-by-vector, `get_point_vector` with its
own IDOR guard, `exclude_point_id`) is **deferred**. The face/plates/etl playbook this mirrors is a
3-leg contract (submit → lifecycle → results-query) — **none** of them ship a query-time
similarity-search endpoint, and the locked "searchable" decision is satisfied merely by **storing**
the vectors in the dedicated collection. Deferring removes a whole new IDOR surface from the v1 blast
radius. If a search seam is genuinely needed sooner, add only mode B (caller-supplied vector) — keep
`get_point_vector`/`exclude_point_id`/`delete_by_batch` out until a concrete consumer exists.

---

## 8. Invariant map (playbook §7)

| # | Invariant | Concrete design point |
|---|---|---|
| 1 | **Idempotent submit** | `create_or_get_batch` = `INSERT … ON CONFLICT (client_batch_ref) DO NOTHING RETURNING id`; `created` derives from the **atomic** outcome. Dispatch + `image_batch.created` fire **only when `created is True`**. Covered by a concurrent-redelivery test (exactly one batch + one dispatch). |
| 2 | **Idempotent land** | `upsert_result(batch_id, item_index)` = `ON CONFLICT (batch_id, item_index) DO UPDATE`; Qdrant point-id = deterministic `uuid5(NS, "{batch_id}_{item_index}")` → redelivery overwrites in place. |
| 3 | **Absolute counts + reconciliation** | `recompute_counts` = `GROUP BY status` written into the columns — **never `+= n`**. After recompute, `land_computed` **log-loud when `embedded+filtered+failed != submitted`** (a dropped/duplicated compute result is surfaced, not silently absorbed) and, per #4, does not terminalize while unaccounted. One 4-key vocabulary across columns / lifecycle / REST — no reconstruction needed. |
| 4 | **Terminal from counts / `completed ≠ done`** | **One shared helper `ImageIndexService.terminal_status(counts)`**: (1) recompute absolute; (2) **accounted-guard** — if `embedded+filtered+failed < submitted`, return non-terminal (stay `computing`, let the reaper sweep); (3) else `completed` iff `failed==0` else `completed_with_errors`; `error` only on batch-level `compute.error` / reaper timeout. `filtered` is clean, never a downgrade. **Error batches are exempt** from the reconciliation invariant (no result rows written). |
| 5 | **Cold-start ordering** | `results_consumer.start()` **before** `submit_consumer.start()` in the single gated block — the results group must exist before any dispatch can produce a reply. |
| 6 | **Downstream service auth** | v1 makes **no** downstream HTTP call from either consumer (the 512-D vector rides back in the results payload; Qdrant is the in-process shared client; Postgres is direct). Recorded as a forward-looking guard comment in `image_index_results_consumer.py`: a future enrichment needing storage-service must send service-to-service headers + the internal URL, not a raw client call. |
| 7 | **IDOR** | Every REST read resolves the tenant from `get_user_context().owner_id` (gateway `X-User-Id`) and ANDs `user_id` in the WHERE. Tenant miss → `404`, missing header → `401`. **No admin bypass.** |
| 8 | **CI conftest truncate** | New `tests/conftest.py` autouse `TRUNCATE … RESTART IDENTITY CASCADE` over the two new tables, guarded to only-existing tables. Hard Phase-1 deliverable. |
| 9 | **Gating** | One flag `IMAGE_INDEX_ENABLED` (default False), one wiring chokepoint (the gated lifespan block), read once at wiring time. Router mounted unconditionally + 503-gated. A Phase-1 test greps that every new symbol is reachable only from the gated block (or the 503-gated router). |

---

## 9. Isolation proof — the complete touch-set

| New artifact | File | Touches a live path? |
|---|---|---|
| Canonical Settings block | `src/infrastructure/config.py` (append fields) | No — new fields, defaults keep behavior identical when unset |
| Status constants | `src/db/models/constants.py` (append 2 classes) | No |
| Models | `src/db/models/image_index.py` (new) + `__init__.py` (append 2 exports) | No — separate `__tablename__`, own `Base` subclasses, no FK into live tables |
| Migration | `alembic/versions/<rev>_create_image_index_tables.py` (new) | No — additive `create_table` ×2; `downgrade` drops only the two |
| Dedicated collection | `src/infrastructure/vector_db/image_index_vector_repository.py` (new) | **No — the critical isolation point.** Own ensure + own `_ensured` latch, **not** called from `initialize()`; shares the live client but never `evidence_embeddings` |
| Read repo | `src/db/repositories/image_index_repo.py` (new) | No — queries only the two new tables |
| Service | `src/services/image_index_service.py` (new) | No |
| Submit consumer | `src/streams/image_index_submit_consumer.py` (new) | No — own stream/group |
| Results consumer | `src/streams/image_index_results_consumer.py` (new) | No — own stream/group |
| Reaper | `src/streams/image_index_reaper.py` (new, gated APScheduler job) | No — sweeps only `t_image_index_batches` |
| REST surface | `src/api/v1/routers/image_index.py` + `src/api/v1/schemas/image_index.py` (new) | No — read-only, `get_user_context` reuse |
| Wiring | `src/main.py` (one gated lifespan block + one module-level 503-gated `include_router`) | Additive; guarded |

**Reuse (not reinvented):** `StreamConsumer`, `StreamProducer`, `get_user_context`, the lifespan
`AsyncIOScheduler`, the shared `QdrantClient`, and the `blacklist_image.py` / `e7f2c9a1b3d6`
templates. **Untouched:** `evidence_embeddings` / `search_queries` collections, `search_similar`,
blacklist points, the evidence/search consumers, all live tables.

---

## 10. Residual risks (accepted for v1)

- **Reaper best-effort publish:** a batch marked `error` whose `image_batch.failed` publish fails during a transient Redis blip is not retried (no longer stuck) — the coordinator's backstop is REST reconcile-by-`external_id`. Acceptable for a 60s coarse net at single replica.
- **Multi-replica reaper:** plain SELECT → two replicas would double-publish `image_batch.failed`. Switch to `FOR UPDATE SKIP LOCKED` before scaling past one API replica.
- **Pre-mint DLQ:** submits that die before the batch row exists are surfaced by DLQ-depth alerting, not auto-recovery (§5.1).
- **Redelivery correctness hinges on `land_computed` idempotency** (upsert by `(batch_id, item_index)` + absolute GROUP BY + deterministic `uuid5` point-ids). A drift to incremental counting or random point-ids breaks XCLAIM/DLQ safety — covered by a Phase-1 redelivery test.
- **`filtered` is vestigial until compute confirms diversity-dedup** (companion open-item #3). Columns are forward-compatible; no logic assumes it fires.

---

## 11. Post-audit fix log

The 6 draft dimensions were merged and run through a 5-lens adversarial audit (contract-alignment,
invariant-coverage, reuse-vs-reinvent, isolation-safety, simplicity-completeness). Every must-fix and
the reasonable should-fixes are folded into the design above; this is the traceability record.

| # | Finding (lens) | Resolution in this doc |
|---|---|---|
| M1 | Lifecycle event_type inconsistent (`image_batch.*` vs `batch.*`) — would hang the coordinator | **`image_batch.*`** everywhere (§1, §6). Confirm strings with dw-offline before freeze. |
| M2 | Terminal-status rule stated 3 incompatible ways; also a partial-batch (unaccounted) hole | **One shared helper** with an **accounted-guard** (§4/§8). `filtered` is clean. |
| M3 | Count vocabulary diverged (6-key wire vs 4-column DB vs 4-key REST) | **One 4-key folded shape** `{submitted, embedded, filtered, failed}` across all three (§3, §6, §7). |
| M4 | Config field-name + env-alias collisions across dimensions | **One canonical Settings block** (§2) + a Phase-1 attribute-existence test. |
| M5 | Dedicated Qdrant repo specified 2 incompatible ways; wiring injected the wrong repo | **One standalone `ImageIndexVectorRepository`** (own ensure/latch, shared client), injected as the **dedicated** repo — not `vector_repo` (§4, §5.4). One write method `upsert_items`. |
| M6 | Router mounted 3 different ways (double-registration / lifespan fragility) | **Unconditional module-level mount + `require_image_index_enabled` 503 gate** (§7). |
| M7 | Idempotent-submit atomicity hand-waved (create-or-get race) | **`INSERT … ON CONFLICT DO NOTHING RETURNING id`**; `created` from the atomic outcome (§5.1, §8). |
| M8 | `pending→computing` transition undefined (dead state) | Submit handler **sets `computing`** after a successful dispatch (§5.1). |
| S1 | Write path not off the event loop (starves live consumers) | **`asyncio.to_thread`** wraps every image-index qdrant call, including writes (§4). |
| S2 | Admin IDOR bypass (`scope=None`) inconsistent + a cross-tenant read path | **Dropped** — strict tenant scoping (§7). |
| S3 | Search endpoint over-engineered vs the 3-leg playbook | **Deferred to v1.1** (§7); v1 stores searchable vectors only. |
| S4 | Unbounded `?all=true` | Bounded `le=200` + paging (§7). |
| S5 | `filtered`/`duplicate_of_index` built ahead of an unconfirmed contract | **Forward-compatible columns, no logic assumes it fires** (§3). |
| S6 | Submit-side DLQ has no reconciliation net (pre-mint failures hang) | **DLQ-depth alert** + documented reaper-starts-after-mint boundary (§5.1). |
| S7 | Reconciliation assertion never actually checked | **Log-loud on `Σ dispositions != submitted`** in `land_computed`; accounted-guard blocks premature terminal (§4, §8). |
| S8 | Reaper interval hardcoded / multi-replica SELECT | **Configured `image_index_reaper_interval_seconds`**; `FOR UPDATE SKIP LOCKED` flagged for scale-out (§5.3). |
| N1 | Constant-class / model-file name drift | **`ImageIndexResultStatus`** + single **`src/db/models/image_index.py`** (§3). |
| N2 | Over-cap error batch violates the reconciliation invariant | Invariant **exempts `status=='error'`** batches (§5.1, §8). |
| N3 | `UNIQUE(qdrant_point_id)` redundant | Kept as belt-and-suspenders; the composite key is the real guard (§3). |
| — | `stream=` landmine | Verified **structurally impossible** in this repo (`publish` has no stream default). Residual risk = a wrong stream constant, closed by M4. |
