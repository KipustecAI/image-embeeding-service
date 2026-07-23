# Image-Index Search + Blacklist Cross-Reference — Validated Design

Post-audit design for **two additive, isolated, gated-OFF capabilities** layered on the shipped
on-demand image-index feature ([`00_DESIGN.md`](00_DESIGN.md) — the submit → compute → land pipeline
that fills the dedicated `image_index_embeddings` Qdrant collection):

- **Capability A — async search-by-image over a list of `external_ids`.** A caller posts a query
  image + a set of `external_ids`; we embed the query through the **existing evidence compute
  round-trip** (we are NO-CLIP), search the dedicated `image_index_embeddings` collection scoped by
  tenant + `external_id`, and expose matches through the same async submit→poll shape as the live
  evidence `/search`. State is recovered by **reusing** `search_requests` / `search_matches` with a
  discriminator column.
- **Capability B — GPU-free blacklist cross-reference.** Given a blacklist entry + a set of
  `external_ids`, fetch the entry's **already-stored** CLIP vectors (no compute round-trip) and
  reverse-search `image_index_embeddings` scoped by tenant + `external_id`. Ships as a synchronous
  REST endpoint (v1) with a separately-flagged auto-on-land hook (v1.1).

Both are **byte-identical non-regressions** of the live evidence `/search` and blacklist
reverse-search paths. Every seam the six draft dimensions disagreed on is resolved to a single answer
here; **§12 is the fix log**. Cross-refs: [`../apis/IMAGE_INDEX_API.md`](../apis/IMAGE_INDEX_API.md)
(REST read legs; the deferred-until-now `POST /search` note), [`../apis/IMAGE_INDEX_SUBMIT.md`](../apis/IMAGE_INDEX_SUBMIT.md)
(coordinator submit + tenant model), [`../requirements/IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md)
(frozen 512-D CLIP ViT-B-32 / L2-normalized compute envelope), [`../image-blacklist/README.md`](../image-blacklist/README.md)
+ [`../image-blacklist/05_MATCH_AND_REPORT.md`](../image-blacklist/05_MATCH_AND_REPORT.md)
(the reverse-search + `image:blacklist_match` report contract we reuse for B).

> **Compute-contract impact: ZERO.** Capability A rides the pre-existing `evidence:search →
> search:results` round-trip; the discriminator + query `external_ids` ride **inside the `metadata`
> object** that compute already echoes byte-identical (the same channel `weapons_filter` / `category`
> already use — verified `search_results_consumer` reads `payload.get("metadata")` and rebuilds
> filters from it). Capability B never dispatches to any compute stream. The frozen
> `vector_b64`/`f32le_b64` encoding is **not** touched — it is the batch `image:index:results` leg
> only; the single query vector rides the existing `search:results` `list[float]` envelope.

---

## 1. Canonical names — frozen before any phase builds

The single largest audit finding was that the six drafts used **three** discriminator column names
(`search_type` / `target` / `target_type`), **two** metadata keys, and **two** storage decisions.
The image-index result branch fires **only** on an exact key match; a divergence sends an image-index
reply through the **unchanged evidence block**, which searches `evidence_embeddings` with a filter
allow-list that carries **neither `user_id` nor `external_id`** — a silent cross-tenant leak, not a
benign miss. Therefore **one name set, used identically** in the POST handler, the stream consumer,
the results consumer, the model/migration, the recalc guard, and the repo filter:

| Concept | **Frozen name** | Where |
|---|---|---|
| Discriminator column on `search_requests` | **`search_type`** VARCHAR(32) NOT NULL server_default `'evidence'` | model + migration |
| Discriminator value | **`'image_index'`** (vs `'evidence'`) | constant `SearchType.IMAGE_INDEX` |
| Metadata key carrying the discriminator on the compute round-trip | **`metadata["search_type"]`** | dispatch + consumer branch |
| Query `external_ids` list column | **`external_ids`** JSONB nullable | model + migration |
| Per-match run tag column on `search_matches` | **`external_id`** VARCHAR(255) nullable, indexed | model + migration |
| Tenant key (everywhere) | **`ctx.owner_id`** (NOT `ctx.user_id`) | §7 |
| Repo search method | **`ImageIndexVectorRepository.search_similar(...)`** | §5 |
| Shared vector-fetch primitive | **`get_point_vector(point_id)`** on both repos | §5 |

`SearchType` is a new constant class in `src/db/models/constants.py` (colocated with the existing
status classes), imported by **both** the dispatch and the consumer so the literal is never inlined
twice:

```python
class SearchType:
    EVIDENCE = "evidence"
    IMAGE_INDEX = "image_index"
```

> **`purpose` is observability only.** The dispatch may set `purpose="image_index_search"` for logs,
> but routing keys on `metadata["search_type"]` (guaranteed echoed inside the metadata object), never
> on `purpose` (which compute's `compute.error` envelope does **not** carry). Do **not** branch on
> `purpose`.

---

## 2. Configuration — `src/infrastructure/config.py` (append)

Three new flags (all default **False**) + one knob + one cap, alongside the existing
`image_index_enabled` (`config.py:156`). Threshold reuses the existing `blacklist_match_threshold`
(0.85, `config.py:93`) — no new threshold knob.

```python
# ── Image-index SEARCH + blacklist cross-ref (ADDITIVE, ISOLATED, gated-OFF) ──
image_index_search_enabled: bool = Field(               # Capability A (async search-by-image)
    False, validation_alias="IMAGE_INDEX_SEARCH_ENABLED")
image_index_blacklist_xref_enabled: bool = Field(       # Capability B REST endpoint (v1)
    False, validation_alias="IMAGE_INDEX_BLACKLIST_XREF_ENABLED")
image_index_blacklist_autocheck_enabled: bool = Field(  # Capability B auto-on-land hook (v1.1)
    False, validation_alias="IMAGE_INDEX_BLACKLIST_AUTOCHECK_ENABLED")

image_index_xref_limit: int = Field(50, validation_alias="IMAGE_INDEX_XREF_LIMIT")            # per-search top_k
image_index_external_ids_cap: int = Field(200, validation_alias="IMAGE_INDEX_EXTERNAL_IDS_CAP")  # MatchAny bound (A + B)
```

**Why separate flags, all AND-gated with `image_index_enabled`.** Indexing (submit→compute→land) and
searching-over-the-index are independently shippable: flip `image_index_enabled`, let batches land,
*then* flip `image_index_search_enabled`. Operationally A and B are AND-gated with
`image_index_enabled` **at wiring time** — `image_index_repo` (the dedicated
`ImageIndexVectorRepository`) is only constructed inside the gated lifespan block (`main.py:212-219`);
if it is `None`, A and B have no collection to hit and their routes 503 / their consumer branch
marks the row ERROR. The `external_ids` cap (default 200, mirroring the read-leg `?all=200` bound)
applies to **both** the A search body and the B cross-reference body — one shared bound on the
`MatchAny` filter.

---

## 3. Cross-collection SAME-SPACE validity (the load-bearing premise — VERIFIED)

Both capabilities compare cosine scores **across** collections (`evidence_embeddings` for blacklist
vectors, `image_index_embeddings` for indexed images, plus the Capability-A query vector). This is
valid, and the audit verified every link:

1. **One encoder, all four vector sources.** The image-index batch handler uses the *same*
   `CLIPEmbedder` singleton ([`IMAGE_INDEX_COMPUTE.md`](../requirements/IMAGE_INDEX_COMPUTE.md) §4–5:
   "512-D CLIP ViT-B-32, L2-normalized"). Blacklist reference embeds **and** the Capability-A query
   image both ride the **same** `evidence:search → search:results` embed handler, so query, evidence,
   and blacklist vectors are byte-identically produced; image-index shares the singleton. **The
   Capability-A query vector sharing the singleton is a verified premise, not an assumption.**
2. **Same dimensionality (512).** Both repos read the single `settings.qdrant_vector_size` knob
   (`config.py`, default 512) — they cannot diverge within a deployment.
3. **Same metric (COSINE).** `evidence_embeddings` (`qdrant_repository.py:90`), `search_queries`
   (`:109`), and `image_index_embeddings` (`image_index_vector_repository.py:96-98`) are **all**
   `Distance.COSINE`. Qdrant normalizes both stored points and the incoming query vector internally
   at compare time, so cross-collection cosine is invariant to caller pre-normalization (this is why
   `upsert_items` documents "NO re-normalization").

**Consequence:** a blacklist vector fetched from `evidence_embeddings` and searched against
`image_index_embeddings` yields the same cosine it would against any other CLIP point. **Two guards
make this durable, not merely assumed:**

- **Model-version stamp (ships NOW, not as a follow-up).** Blacklist points already stamp
  `model_version="clip-vit-b-32"` in their payload (`blacklist_embed_service.py:41,103`), but
  `ImageIndexVectorRepository.upsert_items` today writes **no** version key. This asymmetry means a
  future compute model bump on one path but not the other would silently decalibrate B with zero
  error signal — and once un-versioned points accumulate, a guard is un-addable without a full
  re-index. **Fix: add one additive payload key `model_version` (reuse the same
  `"clip-vit-b-32"` constant) to `upsert_items` in this change set.** Capability B compares the
  blacklist point's `model_version` against the indexed point's and **logs-and-skips on mismatch**.
- **Threshold portability is *comparability*, not a preserved operating point.** Same-space
  guarantees scores are mathematically comparable; it does **not** guarantee that
  `blacklist_match_threshold=0.85` — calibrated blacklist-image-vs-full-evidence-frames — has the
  same precision/recall on **dw-offline detection crops** (what `image_index_embeddings` holds) or on
  arbitrary Capability-A query images. **Default to 0.85, expose the per-entry `match_threshold`
  override + a request-level `threshold` param as required knobs, and gate prod-enable of B behind an
  empirical threshold sweep against real indexed crops.** Do not present cross-collection threshold
  reuse as free.
- **Degenerate-vector guard.** Cosine is undefined for a zero/NaN vector. A failed embed that leaked
  into a stored point (or a zero query vector) would produce garbage rather than an empty result.
  `search_similar` and the xref core **skip-and-log** when a fetched/query vector has ~zero norm or
  contains NaN (mirroring the existing `_vector_repo is None` skip pattern), instead of issuing the
  Qdrant search.

---

## 4. Data model + migration

**One additive migration**, `down_revision = "f3a8d5c9e1b7"` (the current head —
`create_image_index_tables`, confirmed via `alembic heads`). It touches **both** live tables with
backfill-safe additive columns; the entire non-regression story for the write path is
`search_type server_default 'evidence'`.

```python
revision = "a9c1e4f7b2d8"
down_revision = "f3a8d5c9e1b7"

def upgrade() -> None:
    # ── search_requests: discriminator + query external_ids ──
    op.add_column("search_requests",
        sa.Column("search_type", sa.String(32), nullable=False, server_default="evidence"))
    op.add_column("search_requests",
        sa.Column("external_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint("ck_search_requests_search_type", "search_requests",
        "search_type IN ('evidence','image_index')")     # NOT VALID + VALIDATE if the table is large
    op.create_index("ix_search_requests_search_type", "search_requests", ["search_type"])

    # ── search_matches: per-match run tag (queryable / frontend grouping) ──
    op.add_column("search_matches",
        sa.Column("external_id", sa.String(255), nullable=True))
    op.create_index("ix_search_matches_external_id", "search_matches", ["external_id"])

def downgrade() -> None:
    op.drop_index("ix_search_matches_external_id", "search_matches")
    op.drop_column("search_matches", "external_id")
    op.drop_index("ix_search_requests_search_type", "search_requests")
    op.drop_constraint("ck_search_requests_search_type", "search_requests")
    op.drop_column("search_requests", "external_ids")
    op.drop_column("search_requests", "search_type")
```

**Why these exact columns and no more:**

- **`search_type`** — VARCHAR(32) + CheckConstraint, mirroring the TEXT+Check convention the newer
  image-index tables chose over the integer status-machines. `server_default='evidence'` backfills
  every existing row → `'evidence'`, and `create_request()` never sets it for evidence callers, so
  the live `POST /api/v1/search` (`main.py:448`) and the stream-created path
  (`search_consumer.py:_process_search_created`) stay byte-identical. On a large `search_requests`
  table, add the CHECK as `NOT VALID` then `VALIDATE CONSTRAINT` in a second step to avoid a blocking
  `ACCESS EXCLUSIVE` scan (`add_column` with a constant default is already metadata-only on PG11+).
- **`external_ids` JSONB, nullable** — the Capability-A query list; NULL for every evidence row.
  A **real column, not `search_metadata`** — the recalc branch and the frontend facet both read it as
  a first-class field, and (verified) `create_request` today passes `SearchRequest(metadata=…)` which
  resolves to the inherited `Base.metadata` and is **never persisted** — so the "bury it in
  `search_metadata`" variant is a non-functional trap. **This variant is deleted from the plan.**
- **`external_id` on `search_matches`** — nullable + indexed so the frontend groups image-index
  matches by run. The remaining per-match tags ride the **existing `match_metadata` JSONB**; no other
  new column is needed.

**Query image URL — NO new column.** `search_requests.image_url` (Text NOT NULL, `search_request.py:19`)
already stores the query image for the evidence flow and is semantically identical for image-index;
the read endpoints return it unchanged.

**`search_matches` write mapping (locked):**

| `search_matches` column | image-index source | Note |
|---|---|---|
| `evidence_id` (String, **NOT NULL**) | **`item_ref or qdrant_point_id`** | **Mandatory fallback** — `item_ref` is nullable in the point payload (only set when the submit supplied `item_id`); `evidence_id` is `nullable=False` (`search_match.py:20`), so a null insert throws inside the landing txn and marks the *whole* search ERROR. The point-id is always present. |
| `image_url` (Text) | `source_url` | indexed image URL from the Qdrant payload |
| `similarity_score` (Float) | Qdrant cosine `score` | |
| `camera_id` (String, nullable) | `null` | not applicable |
| `external_id` (String, nullable, **new**) | `external_id` | indexed; frontend grouping |
| `match_metadata` (JSONB) | `{batch_id, item_index, item_ref, qdrant_point_id, search_type:"image_index"}` | discriminators + the raw `item_ref` even when it fell back |

**Model + constants changes:**
- `src/db/models/search_request.py` — add `search_type = Column(String(32), nullable=False, server_default="evidence", index=True)` and `external_ids = Column(JSONB, nullable=True)`, plus the CheckConstraint in `__table_args__`.
- `src/db/models/search_match.py` — add `external_id = Column(String(255), nullable=True, index=True)`; extend `to_dict()` to emit `external_id` and surface `batch_id`/`item_index` from `match_metadata` (absent → omitted for evidence rows).
- `src/db/models/constants.py` — add the `SearchType` class (§1).

---

## 5. Qdrant layer — `ImageIndexVectorRepository.search_similar` + shared `get_point_vector`

`src/infrastructure/vector_db/image_index_vector_repository.py` today has only `initialize` /
`_ensure_collection` / `upsert_items` / `delete_by_batch`. Add two read primitives; the live
`QdrantVectorRepository` gains only one read-only method. **Every qdrant-client call runs under
`asyncio.to_thread`** per the module's shared-event-loop contract (module docstring) — required
because these run on the shared results-consumer loop where a blocking `retrieve`/`search` stalls the
live embed/search handlers.

### 5.1 `search_similar` — fixed, closed, tenant-scoped filter

```python
async def search_similar(self, query_vector, *, user_id: str,
                         external_ids: list[str] | None = None,
                         batch_id: str | None = None,
                         top_k: int = 50, threshold: float = 0.75) -> list[dict]:
    if self.client is None:
        logger.error("search_similar called before initialize()"); return []
    vec = query_vector.tolist() if hasattr(query_vector, "tolist") else list(query_vector)
    if not _is_finite_nonzero(vec):          # degenerate-vector guard (§3)
        logger.warning("search_similar skipped: zero/NaN query vector (user=%s)", user_id); return []

    must = [FieldCondition(key="user_id", match=MatchValue(value=str(user_id)))]   # IDOR guard
    if external_ids:
        must.append(FieldCondition(key="external_id", match=MatchAny(any=[str(e) for e in external_ids])))
    if batch_id:
        must.append(FieldCondition(key="batch_id", match=MatchValue(value=str(batch_id))))
    query_filter = Filter(must=must)

    def _sync():
        return self.client.search(collection_name=self.collection_name, query_vector=vec,
            limit=top_k, score_threshold=threshold, query_filter=query_filter, with_payload=True)
    try:
        hits = await asyncio.to_thread(_sync)
    except Exception as e:                    # match live repo: log-and-empty, never raise
        logger.error("image-index search failed (user=%s): %s", user_id, e); return []
    return [self._hit_to_match(h) for h in hits]
```

**Design choices vs. the live `search_similar`:**
- The filter is **fixed and closed** — `user_id` (scalar → `MatchValue`) always, `external_id`
  (list → `MatchAny`) and `batch_id` (scalar) optional. No free-form `filter_conditions` dict, so
  there is **no metadata allow-listing / injection surface** (the live path must allow-list keys;
  we don't). Requires adding `MatchAny` to the existing `qdrant_client.models` import.
- **`user_id` in `must` is the IDOR enforcement point at the vector layer** — a foreign/cross-tenant
  `external_id` produces zero intersection → `[]` (the locked "returns nothing", enforced in the
  filter itself, not just the API). `user_id`/`external_id`/`batch_id` are already keyword payload
  indices on this collection (`_IMAGE_INDEX_PAYLOAD_INDICES`), so the filter is index-served — no
  full scan.
- Returns **`list[dict]`, not `SearchResult`** — `SearchResult` forces `evidence_id`/`camera_id`
  UUIDs the image-index domain lacks and would reject `item_ref` strings.

```python
@staticmethod
def _hit_to_match(hit) -> dict:
    p = hit.payload or {}
    point_id = str(hit.id)
    return {
        "external_id": p.get("external_id"), "batch_id": p.get("batch_id"),
        "item_index": p.get("item_index"), "item_ref": p.get("item_ref"),
        "image_id": p.get("item_ref") or point_id,   # NOT NULL fallback (§4)
        "source_url": p.get("source_url"), "score": hit.score,
        "qdrant_point_id": point_id, "user_id": p.get("user_id"),
        "model_version": p.get("model_version"),      # for the B cross-collection guard (§3)
    }
```

Every field is a pure payload read (the keys `upsert_items` writes) — **no DB join**.

### 5.2 Shared `get_point_vector(point_id)`

Added to **both** repos so each capability fetches from the correct collection with no re-embedding.
On `ImageIndexVectorRepository` it reads `image_index_embeddings`; on `QdrantVectorRepository` it
reads `evidence_embeddings` (where blacklist vectors live with `source_type="blacklist"`, keyed by
`BlacklistImageEmbedding.qdrant_point_id`). It **generalizes the already-shipping
`retrieve_query_vector`** (`qdrant_repository.py:425-438`, `retrieve(..., with_vectors=True) →
results[0].vector`) — but is **`asyncio.to_thread`-wrapped** even on `QdrantVectorRepository` (its
sibling `retrieve_query_vector` is not; **do not "consistency-fix" the wrap away** — it runs on the
shared consumer loop).

```python
async def get_point_vector(self, point_id: str) -> list[float] | None:
    if self.client is None:
        logger.error("get_point_vector called before initialize()"); return None
    def _sync():
        return self.client.retrieve(collection_name=self.collection_name,
                                    ids=[point_id], with_vectors=True)
    try:
        res = await asyncio.to_thread(_sync)
        return res[0].vector if res else None
    except Exception as e:
        logger.error("get_point_vector failed (%s): %s", point_id, e); return None
```

Returns `None` on a missing/orphaned point — **every caller must treat `None` as skip-and-log**
(never pass it into `search_similar`).

---

## 6. Capability A — async search-by-image

Additive, gated-OFF sibling of the live evidence `POST /api/v1/search`. **Compute is unchanged.**

### 6.1 REST surface — on the existing gated `image_index` router

All three routes mount on `src/api/v1/routers/image_index.py` (prefix `/api/v1/image-index`) under a
new `require_image_index_search_enabled` dependency (503 when `image_index_search_enabled` is off or
`image_index_repo is None`), in the same style as the read legs' `require_image_index_enabled`. **Not
on `main.py`** — this keeps the evidence read endpoints byte-identical and puts image-index reads
behind the 503 gate + strict tenant scope. Gateway path `/api/v1/embedding/image-index/search*`
rides the already-registered `image-index/*` forwarding rule ([`IMAGE_INDEX_API.md`](../apis/IMAGE_INDEX_API.md) §1).

**`POST /api/v1/image-index/search` → 202** — body model in `src/api/v1/schemas/image_index.py`:

```python
class ImageIndexSearchCreate(BaseModel):
    image_url: str
    external_ids: list[str] = Field(..., min_length=1, max_length=200)  # image_index_external_ids_cap
    threshold: float = 0.75
    max_results: int = 50
    metadata: dict | None = None
```

Handler (mirrors `create_search`, **but** scopes by `ctx.owner_id` and inserts at `WORKING`):

```python
@router.post("/search", status_code=202, dependencies=[Depends(require_image_index_search_enabled)])
async def create_image_index_search(body, ctx=Depends(get_user_context)):
    owner_id = _require_user(ctx)                    # 401 if missing X-User-Id
    search_id = str(uuid4())
    metadata = dict(body.metadata or {})
    metadata["search_type"]  = SearchType.IMAGE_INDEX   # discriminator survives the round-trip
    metadata["external_ids"] = list(body.external_ids)
    metadata["user_id"]      = owner_id                 # tenant scope (always)
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        req = await repo.create_request(
            search_id=search_id, user_id=owner_id, image_url=body.image_url,
            threshold=body.threshold, max_results=body.max_results,
            search_type=SearchType.IMAGE_INDEX, external_ids=list(body.external_ids),
            status=SearchRequestStatus.WORKING, processing_started_at=datetime.utcnow())
        session.expunge(req)                            # commit-before-publish (like create_search)
    stream_producer.publish(stream=settings.stream_evidence_search, event_type="search.created",
        payload={"search_id": search_id, "user_id": owner_id, "image_url": body.image_url,
                 "threshold": body.threshold, "max_results": body.max_results, "metadata": metadata})
    return {"search_id": search_id, "status": "pending",
            "message": f"Search submitted, poll /api/v1/image-index/search/{search_id}"}
```

**`GET /api/v1/image-index/search/{search_id}`** — tenant + type scoped (strict IDOR):

```python
req = await repo.get_by_search_id_scoped(search_id, user_id=owner_id, search_type=SearchType.IMAGE_INDEX)
if req is None: raise HTTPException(404, "Search not found")   # row-miss ≡ tenant-miss ≡ wrong-type
return {search_id, status(STATUS_NAMES), similarity_status(SIMILARITY_NAMES), total_matches,
        threshold, max_results, external_ids: req.external_ids, image_url: req.image_url,
        created_at, completed_at, error}
```

**`GET /api/v1/image-index/search/{search_id}/matches`** (paginated, `limit` 1–100, `offset` ≥0) —
same tenant+type gate, then `repo.get_matches(req.id, …)` / `repo.count_matches(req.id)`. Each match
emits `{image_id (=evidence_id), source_url (=image_url), score (=similarity_score), external_id,
batch_id, item_index}`.

`get_by_search_id_scoped(search_id, *, user_id, search_type)` is a **new** scoped repo read; the live
unscoped `get_by_search_id` stays **untouched**, so the evidence path is byte-identical. `STATUS_NAMES`
/ `SIMILARITY_NAMES` are lifted into a shared constant so both surfaces agree.

> **The pre-existing evidence read-IDOR gap** (`main.py get_search`/`get_search_matches` compare no
> `user_id`) is **real but out of scope here** — mounting A's reads on the gated router with the
> scoped repo method means image-index rows are never reachable through `/api/v1/search/{id}`.
> Hardening the shared evidence reads is a **separate, independently-reviewed** change (it alters live
> evidence read behavior); do not bundle it into this additive feature.

### 6.2 `create_request` extension (additive kwargs only)

`SearchRequestRepository.create_request` gains optional kwargs — every existing evidence caller stays
byte-identical:

```python
async def create_request(self, search_id, user_id, image_url, threshold=0.75, max_results=50,
                         metadata=None, stream_msg_id=None,
                         search_type=SearchType.EVIDENCE, external_ids=None,
                         status=SearchRequestStatus.TO_WORK, processing_started_at=None):
    request = SearchRequest(search_id=search_id, user_id=user_id, image_url=image_url,
        threshold=threshold, max_results=max_results, search_metadata=metadata,
        stream_message_id=stream_msg_id, search_type=search_type, external_ids=external_ids,
        status=status, processing_started_at=processing_started_at)
    ...
```

> Note the existing `metadata=metadata` → `SearchRequest(metadata=…)` shadow bug is left as-is
> (fixing it is an evidence-path change, out of scope); image-index does **not** rely on
> `search_metadata` — it uses the real `search_type` / `external_ids` columns.

### 6.3 Query embedding — reuse the evidence dispatch/consumer path

- **Dispatch:** the POST handler publishes to `settings.stream_evidence_search` event
  `"search.created"` with `metadata.search_type` + `metadata.external_ids` + `metadata.user_id`
  (= owner_id) — the same publish `create_search` does, plus the discriminator keys. **Zero compute
  change.**
- **Shared stream consumer dedup (must-fix).** `search_consumer._process_search_created` also
  consumes `"search.created"` and creates a row. For image-index the POST commits the row **before**
  publishing, so the consumer's `check_duplicate(search_id)` finds it and no-ops. **Defensively**,
  `_process_search_created` is made discriminator-aware — it reads `metadata["search_type"]` /
  `metadata["external_ids"]` and passes them (plus `status=WORKING`, `processing_started_at=now` for
  image-index) into `create_request` — so even in a lost-POST-commit race the row is correctly typed
  and can never be mis-created as `'evidence'` (which the recalc guard would then re-search against
  `evidence_embeddings`, corrupting it).
- **Results:** compute replies on `settings.stream_search_results` event `"search.vector.computed"`,
  consumed by `search_results_consumer._process_search_result`. Add **one branch**, immediately after
  the existing `purpose == "blacklist_embed"` branch and **before** the evidence block:

```python
meta = payload.get("metadata") or {}
if meta.get("search_type") == SearchType.IMAGE_INDEX:      # NEW — discriminator in metadata
    await _process_image_index_search_result(payload, message_id); return
# ── existing evidence block, BYTE-IDENTICAL ──
```

`_process_image_index_search_result(payload, message_id)` — a near-clone of the evidence block
pointed at the dedicated collection, **entirely wrapped in try/except that marks the row ERROR and
returns cleanly (never re-raises)** so it cannot regress the shared live consumer:

1. Guard: if `_image_index_vector_repo is None` (flag off/degraded) → mark the row ERROR, return.
   **Never** fall through to the evidence search.
2. `query_vector = np.array(payload["vector"], np.float32)` (plain `list[float]` — the frozen
   base64 encoding does **not** apply to the single query vector); degenerate-vector guard (§3).
3. `matches = await _image_index_vector_repo.search_similar(query_vector, user_id=meta["user_id"],
   external_ids=meta["external_ids"], top_k=max_results, threshold=threshold)`. No
   `build_evidence_only_filter` (that is the live-collection blacklist guard, irrelevant here).
4. `store_query_vector(point_id, query_vector.tolist(), search_id)` on the **live**
   `QdrantVectorRepository` (reuses `SEARCH_QUERIES_COLLECTION`, `Distance.COSINE` — storing the
   normalized vector, idempotent on re-search) → recalc for free; write `qdrant_query_point_id`.
5. One txn: load the row by `search_id`, set `status=COMPLETED`, `similarity_status`, `total_matches`,
   `processing_completed_at`, clear prior matches on re-land, insert `SearchMatch` rows via the §4
   mapping (`evidence_id = item_ref or qdrant_point_id`).

- **Wiring:** `set_search_image_index_vector_repo(image_index_repo)` in the gated
  `if settings.image_index_enabled:` lifespan block, passing the same repo already built there. Flag
  off → no route to dispatch, and the branch is inert.

### 6.4 compute.error, stuck-search reaper, recalculation

- **`compute.error` — zero change.** The envelope carries `entity_type="search"` + `entity_id=<search_id>`
  and no `metadata`. `_process_compute_error` tries a blacklist-ref UUID lookup, then falls through to
  `get_by_search_id(entity_id)` and marks the row ERROR — an image-index row is a normal
  `search_requests` row with a fresh uuid4, so it terminalizes via the fall-through unchanged.
  (It also fires an evidence-shaped `image_search.failed` DW event — harmless for v1; gate on
  `search_type` later if DW correctness matters.)
- **Stuck-search reaper (must-fix — insert at WORKING).** Image-index rows are inserted at
  **`status=WORKING`, `processing_started_at=now`** (not the evidence path's `TO_WORK`), because
  `recover_stale_working` (`safety_nets.py:37-74`) sweeps **only** `WORKING` rows, and there is **no
  worker that re-dispatches `TO_WORK` searches** (the API dispatches once at POST) — a `TO_WORK`
  image-index row with a lost compute reply would hang forever. Add a small **`search_type`-keyed
  terminalize branch** to `recover_stale_working`: for `search_type=='image_index'` rows past
  `image_index_max_compute_seconds`, mark **ERROR directly** (a plain reset→`TO_WORK` is a silent
  no-op that also escapes the next `WORKING`-only sweep). Evidence rows keep the existing reset logic
  untouched (~5 additive lines, no repo change — `get_stale_working` already returns all `WORKING`
  rows). Note the intentional lifecycle divergence so no shared report assumes all `search_requests`
  start at `TO_WORK`.
- **Recalculation (must-fix — required guard + branch).**
  `SearchRequestRepository.get_for_recalculation` (`search_request_repo.py:83`) is the **single
  choke-point** for both recalc callers (`safety_nets.recalculate_searches` and
  `POST /api/v1/recalculate/searches`). Add `SearchRequest.search_type == SearchType.EVIDENCE` to its
  WHERE. **This MUST land in the same migration/PR** — without it a landed image-index row
  (COMPLETED + MATCHES_FOUND + `qdrant_query_point_id`) satisfies the recalc predicate and is
  re-searched against `evidence_embeddings` (which carries no `user_id` filter), silently overwriting
  its matches with **cross-tenant evidence** data. Behavior-preserving for evidence (all legacy rows
  backfill to `'evidence'`). Add a **separate** `get_for_image_index_recalculation` +
  recalc-branch: `retrieve_query_vector(row.qdrant_query_point_id)` (collection-agnostic, reused
  as-is) → `image_index_repo.search_similar(vec, user_id=row.user_id, external_ids=row.external_ids,
  …)` → rewrite matches. This delivers "recalculation-against-new-index for free": images landing
  later under the same `external_ids` fold in on the next pass.

---

## 7. Capability B — GPU-free blacklist cross-reference

**Intent:** "Does this blacklisted image appear in these indexed runs?" — take a blacklist entry's
**already-stored** CLIP vectors and reverse-search `image_index_embeddings` scoped by `external_id` +
tenant. **No compute round-trip** — the sharp line vs. Capability A. This is exactly the
[`blacklist_reverse_search`](../image-blacklist/05_MATCH_AND_REPORT.md) pattern, re-pointed at a
different collection. **Synchronous** (200 with matches inline), because it is GPU-free — there is
nothing to wait on, so no 202/poll.

### 7.1 Trigger model — RECOMMENDATION: **BOTH, staged (REST in v1, auto-hook in v1.1)**

| | **REST endpoint (v1)** | **Auto-hook on landed batch (v1.1)** |
|---|---|---|
| Input | `{entry_id}` + `external_ids[]` (the locked "entry + external_ids" shape) | whole tenant active blacklist × just-landed `batch_id` (no `external_ids`) |
| Output | synchronous JSON `matches[]` | fire-and-forget `image:blacklist_match` events |
| Cost site | on-demand, cold — never the hot path | inside the ~281 KB results-consumer land path |
| Blast radius | new read-only route | mutates the isolated landing hot path |

**REST is the v1 deliverable and primary surface** — the locked Capability-B definition is *literally*
"given a blacklist entry **+ a set of external_ids**", which is a request shape, not an event trigger
(the auto-hook has no `external_ids` input). It is GPU-free + bounded, so it returns inline; lowest
blast radius (read-only, never touches `land_computed`). **The auto-hook is a gated v1.1 follow-up**
behind its own `image_index_blacklist_autocheck_enabled` flag, because it (a) adds a blacklist read +
N vector searches to the isolated land path and (b) introduces a new passive-monitoring behavior the
report-generation team must be ready for. It reuses the **same** match core with `batch_id` scoping —
zero logic divergence.

### 7.2 Match core — one function, two callers

New `src/services/blacklist_image_index_xref.py`. Reuses `BlacklistImageRepository`
(`get_entry`/`list_embeddings`/`get_active_qdrant_point_ids`/`count_active_by_user`) and the injected
`set_*` DI pattern from `blacklist_reverse_search.py`.

```python
_evidence_repo = None       # QdrantVectorRepository — blacklist vectors live in evidence_embeddings
_image_index_repo = None    # ImageIndexVectorRepository — dedicated collection

async def cross_reference_entry(*, user_id: str, entry_id: UUID,
        external_ids: list[str] | None = None, batch_id: str | None = None,
        threshold: float | None = None, limit: int | None = None) -> list[dict]:
    settings = get_settings()
    thr = threshold if threshold is not None else settings.blacklist_match_threshold
    lim = limit or settings.image_index_xref_limit
    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        entry = await repo.get_entry(entry_id)
        if entry is None or entry.user_id != user_id:        # IDOR — tenant miss ⇒ nothing
            return []
        if threshold is None and entry.match_threshold is not None:
            thr = entry.match_threshold                      # per-entry override
        embeddings = await repo.list_embeddings(entry_id)    # qdrant_point_id per reference
    best: dict[str, dict] = {}                               # indexed point_id → best match
    for emb in embeddings:
        vec = await _evidence_repo.get_point_vector(emb.qdrant_point_id)   # NO compute
        if vec is None:
            logger.warning("xref: orphaned blacklist point %s (entry %s)", emb.qdrant_point_id, entry_id)
            continue
        hits = await _image_index_repo.search_similar(vec, user_id=user_id,
                   external_ids=external_ids, batch_id=batch_id, top_k=lim, threshold=thr)
        for h in hits:
            prev = best.get(h["qdrant_point_id"])
            if prev is None or h["score"] > prev["similarity_score"]:
                best[h["qdrant_point_id"]] = {
                    "blacklist_entry_id": str(entry_id), "blacklist_reference_id": str(emb.reference_id),
                    "external_id": h["external_id"], "batch_id": h["batch_id"], "item_index": h["item_index"],
                    "image_id": h["image_id"],          # item_ref or qdrant_point_id (§4)
                    "source_url": h["source_url"], "qdrant_point_id": h["qdrant_point_id"],
                    "similarity_score": h["score"], "threshold_used": thr}
    return sorted(best.values(), key=lambda m: m["similarity_score"], reverse=True)
```

Aggregation by **indexed `point_id`** (max score + winning `reference_id`) means a multi-reference
entry never returns the same indexed image twice. An all-orphaned entry logs WARNING per point rather
than returning a silent empty.

### 7.3 REST endpoint (v1)

On the **blacklist router** (`src/api/v1/routers/blacklist_image.py`, `prefix="/api/v1/blacklist"`)
as an entry sub-resource, reusing that router's `get_user_context` + wiring, **additionally** gated on
`require_image_index_search_enabled` (so it 503s when the target collection is off/unavailable):

```
POST /api/v1/blacklist/image-entries/{entry_id}/cross-reference
  deps: Depends(get_user_context), Depends(require_image_index_search_enabled)
  body: { "external_ids": ["run-42", ...],   # required, 1..image_index_external_ids_cap
          "threshold": 0.85,                  # optional override
          "max_results": 50 }                 # optional
  200:  { "entry_id", "external_ids", "threshold_used", "match_count", "matches": [ XrefMatch, ... ] }
```

**Status codes:** `401` missing `X-User-Id`; `404` entry not under tenant (IDOR — `get_entry`
tenant-miss ⇒ router 404 for the *entry*, never 403/existence-disclosure); `422` bad body; `503`
feature off or repo unavailable. A foreign/other-tenant `external_id` is **not** an error —
`search_similar` ANDs `user_id`, so it contributes no hits (IDOR-by-emptiness). Document the
404-vs-503-vs-200-empty distinction for the frontend ("no such entry" vs "feature off" vs "no matches
in those runs"). `external_ids` is capped at `image_index_external_ids_cap` (same 200 bound as A).

**`XrefMatch`** is a new synchronous DTO (image-index-shaped, returned to the caller — not the report
event): `{blacklist_entry_id, blacklist_reference_id, external_id, batch_id, item_index, image_id,
source_url, qdrant_point_id, similarity_score, threshold_used}`.

### 7.4 Auto-hook (v1.1) + relation to `image:blacklist_match`

Behind `image_index_blacklist_autocheck_enabled`. Insertion point: `_process_computed`
(`image_index_results_consumer.py`) **after** the terminal `_publish_lifecycle`, fast-exited on
`BlacklistImageRepository.count_active_by_user(user_id) == 0` (the same optimization the evidence
inline path uses). It calls `cross_reference_entry(..., batch_id=<just-landed>, external_ids=None)`
per active entry, wrapped fire-and-forget (log-and-continue, never raise — a match-scan failure must
never block batch completion).

It **reuses the existing `image:blacklist_match` event**, extended additively so report-generation's
consumer keeps working (`src/application/helpers/blacklist_match_events.py`):
- Widen `BlacklistMatchTrigger` to include `"image_index_xref"`.
- Add three **optional** fields (default null → evidence callers byte-identical): `match_target`
  (`"evidence"|"image_index"`, default `"evidence"`), `external_id`, `batch_id`.
- **Field mapping:** `evidence_id ← item_ref or qdrant_point_id` (so report-gen's
  `(evidence_id, entry_id, entry_version)` dedup works unchanged), `matched_image_url ← source_url`,
  `matched_image_index ← item_index`, `matched_qdrant_point_id ← image_index point_id`.
  `evidence_camera_id / device_id / app_id / infraction_code` are **null** (the image-index payload
  carries only `{user_id, external_id, batch_id, item_index, item_ref, source_url, model_version}`).

Published through the **same** `publish_blacklist_match` on the **same** stream/event. **This is a
cross-service wire-contract change** — the `build_blacklist_match_event` builder takes all-required
kwargs (the xref caller passes explicit `None` for every evidence-side field), and report-generation
is an external consumer. **Confirm with report-generation** that their consumer tolerates the new
trigger value + null evidence fields **before** emitting any `image_index_xref` event; consider a
distinct `event_type` if null-tolerance can't be guaranteed. **v1's REST path is event-silent**
(sync JSON only), which sidesteps this entirely — a good reason to keep v1 event-free.

### 7.5 Wiring

Inside the gated `if settings.image_index_enabled:` lifespan block:
`set_xref_evidence_repo(vector_repo)` (live `QdrantVectorRepository` — blacklist vectors) +
`set_xref_image_index_repo(image_index_repo)`. Both already singletons there. Fully additive — no
live path touched.

---

## 8. Gating + non-regression proof

Every shared touch is additive, defaulted, and dark behind a flag.

1. **Write path.** `create_request` gains only optional kwargs; `search_type` DB-defaults to
   `'evidence'`, `external_ids` to NULL, `status` to `TO_WORK`. Both live producers (`main.py:448`,
   `_process_search_created`) are untouched for evidence. Existing rows backfill via `server_default`.
2. **Search-result consumer** (`_process_search_result`, started **unconditionally** at
   `main.py`, NOT under the flag). The new branch is a **pure additive early-return** keyed on
   `metadata.search_type == "image_index"` **AND** `_image_index_vector_repo is not None`, with the
   entire new handler wrapped in its own try/except that marks only the image-index row ERROR. When
   the discriminator is absent, the evidence + `blacklist_embed` blocks are **byte-identical** — the
   exact precedent set by the shipped `purpose == "blacklist_embed"` guard. A golden test asserts
   evidence processing is byte-identical with the discriminator absent.
3. **Recalc cron.** `get_for_recalculation` gains `search_type == 'evidence'` (one edit covers both
   callers) — behavior-preserving for every existing row, **required** isolation, shipped **with** the
   migration. Image-index recalc is a separate query + branch.
4. **Blacklist reverse-search** (`_run_reverse_search`) still targets `evidence_embeddings` via
   `search_similar` + `build_evidence_only_filter`. B is a **new, additively-wired** path (own flags,
   own repo call) — it does not modify `blacklist_reverse_search.py` or the existing
   `publish_blacklist_match` call; the trigger union widens additively.
5. **Qdrant init untouched.** The dedicated collection's `_ensure_collection` is still never called
   from `QdrantVectorRepository.initialize()` (the documented isolation boundary). Adding
   `search_similar`/`get_point_vector` adds no `initialize()` coupling.
6. **conftest isolation.** `tests/conftest.py` today truncates only the image-index result/batch
   tables and refuses to touch live evidence/search/blacklist tables. Capability A now writes
   `search_type='image_index'` rows into the shared `search_requests`/`search_matches`. Add a
   **row-scoped autouse fixture** (DELETE, never TRUNCATE) that removes only
   `search_requests WHERE search_type='image_index'` and `search_matches WHERE external_id IS NOT
   NULL`, preserving the "never touch live tenant rows" contract while making A's redelivery tests
   deterministic.

---

## 9. IDOR invariants

- **Every A/B search is created + scoped with `ctx.owner_id`** (the trusted `X-User-Id` gateway
  header resolved to the tenant owner — **not** `ctx.user_id`, which for a guest is the guest id and
  would miss the parent-owned indexed data). This matches the payload `user_id` written at
  `upsert_items` and the existing image-index read legs. The `image_index_embeddings` filter is
  `{user_id: owner, external_id: MatchAny(external_ids)}`, both keyword-indexed — a foreign/other-
  tenant `external_id` matches zero points → empty result. No cross-tenant leakage even with a guessed
  `external_id`.
- **Capability B** additionally requires the blacklist entry to belong to the tenant
  (`entry.user_id == owner`) before any `get_point_vector` — a foreign `entry_id` yields nothing to
  search with (404 for the entry).
- **A's reads are on the gated router** with `get_by_search_id_scoped(search_id, owner_id,
  'image_index')` — 404 on row-miss ≡ tenant-miss ≡ wrong-type (no existence disclosure), 401 on
  missing header. The evidence reads on `main.py` stay unchanged; the pre-existing evidence read-IDOR
  gap is tracked as a **separate** hardening PR (§6.1).

---

## 10. Reuse map

| Concern | REUSE (unchanged) | NEW (additive) |
|---|---|---|
| Async submit→poll shape | `main.py` `POST /search`→202; GET status; GET matches (handler shapes) | `POST/GET/GET /api/v1/image-index/search*` on the gated router |
| Search-state tables | `SearchRequest` / `SearchMatch`; `SearchRequestRepository` (`create_request`, `get_matches`, `count_matches`, `get_for_recalculation`) | `search_type` + `external_ids` columns; `search_matches.external_id`; `get_by_search_id_scoped`, `get_for_image_index_recalculation` |
| Query-image embed | `stream_evidence_search` `search.created` → `search:results` `search.vector.computed`; **compute echoes `metadata` byte-identical** | `metadata.search_type` + `metadata.external_ids` ride the echoed metadata — no stream, no compute change |
| Result dispatch | `_process_search_result` `purpose`-dispatch pattern | one branch → `_process_image_index_search_result` (guarded, try/except, byte-identical when absent) |
| Qdrant filtered search | `qdrant_repository.search_similar` (MatchAny/MatchValue/score_threshold pattern) | `ImageIndexVectorRepository.search_similar` (fixed closed filter, `to_thread`) |
| Query-vector recalc | `store_query_vector`/`retrieve_query_vector` (`search_queries`) + `qdrant_query_point_id` + hourly `recalculate_searches` | recalc branch on `search_type` → image-index collection |
| Blacklist reverse-search infra (B) | `blacklist_reverse_search` skeleton + `publish_blacklist_match` + `BlacklistImageRepository` | `blacklist_image_index_xref.cross_reference_entry` pointed at `image_index_embeddings` |
| Stored-vector fetch | `retrieve_query_vector` pattern (`retrieve(with_vectors=True)`) | shared `get_point_vector` on **both** repos (`to_thread`-wrapped) |
| Tenant auth / IDOR | `get_user_context` + `ctx.owner_id`; `require_image_index_enabled` 503 gate | `require_image_index_search_enabled`; `user_id`-in-Qdrant-filter |
| Report event (B v1.1) | `image:blacklist_match` stream/event + `build_blacklist_match_event` | `"image_index_xref"` trigger + optional `match_target`/`external_id`/`batch_id` (defaults byte-identical) |

**Untouched:** `evidence_embeddings` / `search_queries` collections, `search_similar` (live),
blacklist points, the evidence/blacklist consumers' existing branches, `land_computed`
terminalization, all live-table schemas.

---

## 11. Phased delegate-build plan

Each phase: implement → parallel adversarial audit → must-fixes → integrator re-runs gates.

**P1 — Qdrant primitives (pure additive, no live-path wiring change).**
`ImageIndexVectorRepository.search_similar` + `get_point_vector` on both repos + the `model_version`
stamp in `upsert_items`. Ship-able independently with unit tests against a fake client. *Audit focus:*
cross-collection cosine (same `vector_size`/`Distance.COSINE`), `to_thread` on every client call,
list→`MatchAny`/scalar→`MatchValue`, `user_id` always in filter, `None`/empty/degenerate-vector
handling, no import of the live collection name. **Compute dependency: none.**

**P2 — Capability A async search (depends on P1).** Migration (`search_type` + `external_ids` +
`search_matches.external_id`) **with the `get_for_recalculation` `search_type=='evidence'` guard in
the same PR**; `create_request` kwargs; the three gated routes + `get_by_search_id_scoped`;
`_process_search_created` discriminator-awareness; the `_process_search_result` branch +
`_process_image_index_search_result`; WORKING-at-insert + `recover_stale_working` terminalize branch;
image-index recalc branch; lifespan wiring. *Audit focus:* evidence search byte-identical (diff the
untouched branch), IDOR (other-tenant `external_id` → empty), `search_type` backfill, recalc
misroute, `evidence_id` NOT-NULL fallback, golden round-trip test that `metadata.search_type`
survives and routes to the image-index handler. **Compute dependency:** relies on the already-true
`metadata` echo; add a contract assertion test + the flag-off ERROR fallback.

**P3 — Capability B REST (depends on P1; parallel with P2).** `blacklist_image_index_xref` core +
`POST …/cross-reference` on the blacklist router + `XrefMatch` DTO + `require_image_index_search_enabled`.
Event-silent. *Audit focus:* zero compute round-trips (grep for any `stream_producer.publish` to a
compute stream), same-space + `model_version` guard, tenant scope on both the entry read and the index
search, entry-not-found → 404, orphaned-point WARNING.

**P3b — B auto-hook (optional, own flag `image_index_blacklist_autocheck_enabled`).** Gated call in
`_process_computed` after the terminal lifecycle publish, `count_active_by_user==0` fast-exit,
fire-and-forget. Requires report-generation sign-off on the additive `image:blacklist_match` fields +
trigger value.

**P4 — Live e2e + flag flip.** Extend the `image-index-e2e` skill with a search leg (index batch →
`POST /image-index/search` with the batch's `external_id` → poll matches) and an xref leg (create
blacklist entry → `POST …/cross-reference`). Flip flags in staging, confirm live evidence `/search` +
blacklist reverse-search metrics unchanged, then prod.

Dependency order: **P1 → {P2 ∥ P3} → P3b → P4.**

---

## 12. Post-audit fix log

INPUT-A (6 merged draft dimensions) + INPUT-B (5 adversarial lenses). Every must-fix and the
reasonable should-fixes are folded above; this is the traceability record.

| # | Finding (lens) | Resolution |
|---|---|---|
| M1 | Discriminator named 3 ways (`search_type`/`target`/`target_type`), 2 metadata keys — a mismatch falls image-index replies through the **un-tenant-scoped** evidence block (cross-tenant leak) | **One frozen name set** (§1): column `search_type`, value `'image_index'`, metadata key `metadata["search_type"]`, constant `SearchType`, imported by both dispatch + consumer. Golden test that the branch never reaches the evidence block. |
| M2 | `search_matches.evidence_id` is NOT NULL but `item_ref` is nullable → INSERT throws, whole search ERRORs | **Mandatory fallback `evidence_id = item_ref or qdrant_point_id`** (§4, §5) in the consumer landing path, the recalc write, and the B event mapping. |
| M3 | `get_for_recalculation` has no target filter → landed image-index rows re-searched against `evidence_embeddings` (cross-collection **and** cross-tenant overwrite) | **`search_type=='evidence'` added in `get_for_recalculation`** (single choke-point) + a separate image-index recalc branch; **ships in the migration PR** (§6.4, §8). |
| M4 | Storage of `external_ids`/per-match tags specified 2 incompatible ways (real columns vs bury in `search_metadata`); `create_request` `metadata=` is a confirmed shadow-drop bug | **Real columns** (`search_type`, `external_ids`, `search_matches.external_id`); the `search_metadata` variant **deleted** (§4). |
| M5 | Shared read endpoints IDOR change specified as either "new scoped router" or "harden main.py in place" (a live-contract change) | **New scoped endpoints on the gated router** (`get_by_search_id_scoped`); main.py reads byte-identical; the pre-existing evidence IDOR gap is a **separate PR** (§6.1, §9). |
| M6 | Shared `_process_search_created` also creates the row, not discriminator-aware → race could mis-type as `'evidence'` | POST commits-before-publish + `check_duplicate` no-op; **`_process_search_created` made discriminator-aware** as defense (§6.3). |
| M7 | Stuck-search reaper: inserting at `TO_WORK` (mirror `create_search`) hangs forever (no TO_WORK re-dispatch worker; reset→TO_WORK is a no-op) | **Insert image-index rows at `WORKING` + `processing_started_at=now`; `search_type`-keyed terminalize-to-ERROR branch** in `recover_stale_working` (§6.4). |
| M8 | New branch on the always-live shared consumer could regress evidence/blacklist if it raises | **Pure additive early-return keyed on discriminator AND repo-wired; whole handler in try/except that marks ERROR and returns, never re-raises**; flag-off/`None`-repo → ERROR, never fall through (§6.3, §8). |
| S1 | Cross-collection SAME-space stated as assumption | **Verified premise** (§3): one CLIP singleton (query + evidence + blacklist share the `evidence:search` embed handler; image-index shares the singleton), same 512-D knob, all COSINE. |
| S2 | No model-version drift detection; image-index points un-versioned (asymmetric with blacklist) | **Stamp `model_version` in `upsert_items` NOW**; B compares + logs/skips on mismatch (§3). |
| S3 | Threshold "transfers directly with no recalibration" over-claims | **Softened to comparability, not preserved operating point**; per-entry + request threshold knobs required; prod-enable behind an empirical sweep (§3). |
| S4 | Degenerate (zero/NaN) vector → undefined cosine | **Skip-and-log guard** in `search_similar` + xref core (§3, §5). |
| S5 | Tenant key inconsistently `user_id` vs `owner_id` → guest searches miss their own data | **`ctx.owner_id` end-to-end** (§1, §9). |
| S6 | `get_point_vector` on `QdrantVectorRepository` shown un-`to_thread`-wrapped (blocks the shared loop) | **`to_thread`-wrapped on both repos**; note the un-wrapped `retrieve_query_vector` sibling must not be "consistency-fixed" (§5). |
| S7 | `external_ids` cap inconsistent (200 in A, uncapped in B) | **One `image_index_external_ids_cap=200`** applied to both bodies (§2, §6.1, §7.3). |
| S8 | B reuses `publish_blacklist_match` → cross-service wire change to report-generation | **Additive optional fields + defaults byte-identical; v1 REST is event-silent; report-gen sign-off required before v1.1** (§7.4). |
| S9 | conftest would leak image-index rows into shared tables | **Row-scoped DELETE autouse fixture** (never TRUNCATE live tables) (§8). |
| N1 | `compute.error` fires evidence-shaped `image_search.failed` DW event for image-index rows | Accepted for v1 (harmless); gate on `search_type` later (§6.4). |
| N2 | CHECK constraint validation lock on a large `search_requests` | `NOT VALID` + `VALIDATE CONSTRAINT` in two steps at scale (§4). |
| N3 | Frozen `vector_b64` mistaken as applying to the query vector | Query rides the `search:results` `list[float]` envelope; base64 is the **batch** leg only — zero interaction (§6.3). |

---

## 13. Residual open questions (non-blocking)

1. **Per-`external_id` roll-up on the A status object** — the locked spec tags each *match* with
   `external_id` but doesn't require an aggregate; recommend flat `total_matches` like evidence, add
   counts-per-`external_id` only if the frontend asks.
2. **`external_ids` cap of 200** — confirm no coordinator needs a larger list (would require chunking
   the `MatchAny`).
3. **DW lifecycle events for image-index searches** — mirror the evidence path's
   `image_search.created/completed`, or stay DW-silent in v1? (Recommend DW-silent for v1; the
   `compute.error` fall-through `image_search.failed` is the only DW leak, and it is harmless.)
4. **Auto-hook (v1.1) event volume + scoping** — per-match vs rolled-up `image:blacklist_match`; whole
   active blacklist vs category-scoped to the batch's class. Confirm with report-generation before
   building v1.1.
5. **B multi-reference rollup** — max-score-per-indexed-point (chosen) vs one row/event per matching
   reference so the operator sees which reference hit.
6. **Explicit `query_image_url` alias in the read response** — design reuses `image_url`; add an alias
   only if the frontend wants it (no new column either way).
