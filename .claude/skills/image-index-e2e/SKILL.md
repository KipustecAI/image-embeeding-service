---
name: image-index-e2e
description: >-
  Run a live end-to-end test of the on-demand image-index feature on the deployed
  image-embeeding-service, exercising BOTH docs/apis contracts — the Redis submit leg
  (IMAGE_INDEX_SUBMIT.md, XADD image:index:submit like a no-HTTP coordinator) and the
  REST read leg (IMAGE_INDEX_API.md, poll by-external-id through the API gateway). Submits
  a small batch of public image URLs, waits for image-embedding-compute to embed + our
  service to land, polls to a terminal status, and verifies the counts/reconciliation
  invariants + per-item dispositions. Use after a deploy of the image-index feature, after
  changes to the submit/results consumers / reaper / Qdrant layer / REST surface, or to
  reproduce a stuck-batch. Requires an API key from the user. Mirrors deepface's
  face-index-e2e and plates' gateway-smoke.
---

# image-index E2E test

End-to-end validation of on-demand image indexing:
`XADD image:index:submit` → we mint + dispatch `image:index` → **image-embedding-compute**
embeds (512-D CLIP) → `image:index:results` → we land in the dedicated `image_index_embeddings`
Qdrant collection + Postgres → `image_batch:raw` lifecycle → poll REST by `external_id`.

Contracts under test: **`docs/apis/IMAGE_INDEX_SUBMIT.md`** (submit) + **`docs/apis/IMAGE_INDEX_API.md`** (read).
Compute envelope (frozen): `docs/requirements/IMAGE_INDEX_COMPUTE.md`. Design: `docs/image-index/00_DESIGN.md`.

> ⚠️ **Unlike face/plates, our SUBMIT is Redis-only — there is NO REST submit.** The coordinator
> (dw-offline) XADDs `image:index:submit`; only the READ side is REST. So this test needs BOTH the
> shared Redis (to submit) and the gateway API key (to read).

## 0. Inputs you need

- **Gateway base URL** — `https://api.lookia.mx` (deployed) — for the READ leg.
- **API key** — the user supplies it at call time (a secret). Used ONLY on the live `curl`/HTTP READ.
  **NEVER** write it to a file, commit, wiki, memory, or the agentmemory bank; never echo it.
- **Tenant `user_id` (UUID)** — submitted in the Redis payload. **It MUST equal the tenant the API key
  resolves to**, or the READ returns `404` (IDOR scoping, by design).
- **Redis** — read from `.env` (`REDIS_HOST/PORT/PASSWORD`, DB `REDIS_STREAMS_DB=3`). The shared remote
  Redis (`31.220.104.212:6379`) is where both our service and compute consume. Network egress →
  run the script with `dangerouslyDisableSandbox: true`.
- **Both sides deployed & the flag ON**: `ms-embedding-api` with **`IMAGE_INDEX_ENABLED=true`** (so the
  submit+results consumers + reaper run and the REST returns non-503), and `image-embedding-compute`
  with its `image-index-compute-workers` group live on `image:index`.

## 1. Preflight — are the consumer groups live? (never submit before this)

`StreamConsumer` groups are created at `id="$"`, so a pre-group `XADD` is **dropped**. Verify first:

```bash
python3 scripts/test_e2e_image_index.py --groups
```
Expects compute's **`image-index-compute-workers`** group present on `image:index`. If it's MISSING,
compute isn't live (or the flag/order is wrong) — do NOT submit; a dropped dispatch hangs the batch.
`✗ MISSING` → stop and check the deploy.

## 2. Fastest path — the script

Submit → poll → verify, using `data/image_urls.text` for the URLs:

```bash
IMAGE_INDEX_API_KEY='<key>' python3 scripts/test_e2e_image_index.py \
  --gateway https://api.lookia.mx --user-id <tenant-uuid>
# or pass URLs explicitly:  ... --user-id <uuid> https://.../a.png https://.../b.png
```
Run it with `dangerouslyDisableSandbox: true` (it reaches the remote Redis + gateway). The script:
reads Redis creds from `.env`, XADDs the submit, waits, polls `by-external-id?include_items=true` to a
terminal status, checks the invariants, and prints the `batch_id` to hand to compute.

## 2C. Search + blacklist cross-ref legs (Cap A / Cap B — `02_SEARCH_DESIGN.md`)

Once `IMAGE_INDEX_SEARCH_ENABLED=true` and some batches are indexed (§2), the runner also drives the
two new capabilities. Full plan + expected results: [`../../docs/image-index/03_TEST_PLAN.md`](../../docs/image-index/03_TEST_PLAN.md).

```bash
# Cap A — async search-by-image over external_ids (self-match ≈ 1.0), + live-Qdrant persistence check
IMAGE_INDEX_API_KEY=$KEY python3 scripts/test_e2e_image_index.py --search \
  --user-id <tenant> --query "$IMG_A" --external-ids "$E1,$E2" --verify-qdrant

# Cap B — GPU-free blacklist cross-reference (asserts ZERO embed-stream growth = no compute)
IMAGE_INDEX_API_KEY=$KEY python3 scripts/test_e2e_image_index.py --xref \
  --user-id <tenant> --entry-id <blacklist_entry_id> --external-ids "$E1,$E2"
```
- `--search` submits the query through the gateway, polls to terminal, checks the **self-match ≥ 0.9** and
  that every match is **scoped to the given `external_ids`**. `--verify-qdrant` counts the stored
  `image_index_embeddings` points + confirms the `model_version` stamp (read-only, `.env` `QDRANT_*`).
- `--xref` calls the sync cross-reference and **proves GPU-free** by asserting `image:index` +
  `evidence:search` stream lengths are unchanged across the call.
- Create the blacklist entry first (§7 of the test plan): `POST …/images/blacklist` → `…/{id}/references`
  → poll `INDEXED`, then pass the entry id to `--xref`.
- **DB persistence** for Cap A is already proven by the `GET /search/{id}/matches` read (it reads
  `search_matches` from Postgres); `--verify-qdrant` covers the vector side.

## 3. Manual flow (if you want to drive it by hand)

```bash
# SUBMIT (Redis) — via a python one-liner using .env creds; payload per IMAGE_INDEX_SUBMIT.md §1
#   {event_type:"image.index.submit", payload:{user_id, client_batch_ref, external_id, items:[{image_url,item_id}]}}
# READ (gateway) — per IMAGE_INDEX_API.md §4
GW=https://api.lookia.mx; KEY='<key>'; EXT='e2e-smoke'
curl -sS "$GW/api/v1/embedding/image-index/results/by-external-id/$EXT?include_items=true" \
  -H "X-API-Key: $KEY" | python3 -m json.tool
```
(Poll in a SEPARATE Bash call so time passes — the tool blocks foreground `sleep`.)

## 4. Success criteria (report a FAIL if any break)

- `status` reaches **`completed`** (or `completed_with_errors` if a URL 404s → that item is `download_failed`).
- **Reconciliation**: `counts.submitted == embedded + failed` (v1: `filtered` is ALWAYS 0 — dedup disabled).
- **One item row per submitted** (`len(items) == submitted`); `item_ref` echoes the submitted `item_id`.
- Each **`embedded`** item carries a `qdrant_point_id` (the vector landed in `image_index_embeddings`).
- `by-external-id` resolves the batch; `?all=true` lists re-runs (newest-first, bounded 200).
- Hand the printed **`batch_id`** to `image-embedding-compute` — they confirm their log line
  `Batch <id>: N submitted → M embedded → K failed`.

## 5. Common failures

| Symptom | Cause / fix |
|---|---|
| READ → **503** | `IMAGE_INDEX_ENABLED` is false on the deploy (or unset). The whole feature is gated off — set it `true` and redeploy/restart `ms-embedding-api`. |
| READ → **404 "Route not found"** (from the gateway) | gateway `ROUTE_CONFIG` not registered for `/api/v1/embedding/image-index/*` (open item). The Redis legs still work — verify via the `image_batch:raw` lifecycle / DB until the route lands. |
| READ → **404** (from us, `{"detail":"No runs..."}`) | tenant miss — the submit `user_id` ≠ the API key's tenant, OR nothing landed yet. Confirm the UUID matches the key. |
| Batch never leaves `pending`/`computing`, counts 0 | compute's group wasn't live when you submitted (dropped dispatch), OR the results consumer is raising. Check `--groups`; grab `ms-embedding-api` logs for `land_computed` / `Traceback` / `MissingGreenlet`. |
| Every item `download_failed` | the URLs aren't directly fetchable (compute does NOT follow redirects; a `302`/presigned-expired → `download_failed`). Use durable public `storage.lookia.mx/...` URLs. |
| `--groups` shows compute group MISSING | compute not deployed / not live — don't submit. |

## 6. Cleanup

Test batches are cheap: 2 tables (`t_image_index_batches`/`_results`) + a few `image_index_embeddings`
Qdrant points, all tagged by `user_id`/`external_id`. No delete endpoint in v1 — `delete_by_batch`
exists on the vector repo but is unwired; rows age out or can be purged by `external_id` later.

## 7. Report back

State plainly: preflight (groups live?), the submit (`external_id`, item count), the terminal `status`
+ `counts`, whether the invariants held, the `batch_id`, and any failure mapped to §5. **Never include
the API key** in the report.
