# Frames desde ZIP — serve evidence frames from the evidence ZIP

**Status:** built on branch `feat/frames-from-zip` (2026-10-21), not deployed. **Layer:** wiki (design + contract).
**Coordination:** Lookia Platform channel `#frames-desde-zip`. Message refs below (`#18xx`) point to that channel.

---

## 1. Problem

On every `embeddings:results` message, the service downloads the evidence ZIP again, extracts each frame that compute kept, and uploads it to storage-service under `public/embeddings/...`. That is about **65k frames per hour** copied into object storage. Each copy duplicates bytes that already exist inside the evidence ZIP.

On **2026-10-21** storage became **read-only** for new writes. The upload step therefore fails, and new vectors can no longer get a frame URL. Writes are not expected to reopen soon, and the copies were never needed: the evidence ZIPs are permanent.

**Fix:** stop copying. Store a *reference* to the frame (ZIP URL + entry path) per vector, and serve the frame on demand from the ZIP with HTTP Range reads.

---

## 2. Decisions (with channel refs)

| # | Decision | Ref |
|---|---|---|
| D1 | Evidence ZIPs are **permanent** in storage, so a reference into the ZIP is a stable frame address. | #1811 |
| D2 | Proposal accepted: no more per-frame uploads; store `zip_url` + `zip_entry` + `image_name` per vector and serve on demand. | #1818 |
| D3 | The frame route is **public** and addressed by the **Qdrant point UUID**: `GET /api/v1/frames/{point_id}`. The UUID is unguessable, and it lets `<img src>` work with no auth header. | #1826 |
| D4 | DW gets the reference as **additive, nullable** keys on the existing events, plus `image_count` on `image_embedding_request.*`. Nothing is removed or renamed. | #1828 |
| D5 | Gateway: the public path, upstream timeout and header handling of `/api/v1/frames/*` were agreed with api-gateway. The service side assumes no auth on this route (X-User-* / X-API-Key are stripped there) and an upstream timeout of at least `FRAMES_FETCH_TIMEOUT_S`. See the channel messages for the exact gateway values. | #1829, #1831 |
| D6 | Private ZIPs are read through a presigned GET from storage-service `POST /internal/presigned-url`. Presign and HTTP Range on storage were verified in the channel. `scripts/check_zip_range.py` repeats that check for any `zip_url`. | #1822, #1823 |
| D7 | Compute sends the exact entry path `zip_entry` per vector (compute PR #4), so `low_res/` and `cropped/` siblings with the same basename are not confused. | #1821 |

Design choices made during the build (see the `issues` of each increment):

- A given `zip_entry` that is not in the ZIP is an error (502). There is **no basename fallback**, because it could serve a sibling from `low_res/` or `cropped/`. Rows without `zip_entry` (compute before PR #4) match the **first** entry whose basename equals `image_name`, in central-directory order, the same rule as the old `ZipProcessor`.
- In `upload` mode, the new columns stay NULL and the Qdrant payload gets no frame keys. `image_count` is set in both modes.
- `If-None-Match` is checked **after** the DB lookup: an unknown id is always 404, never 304.
- HEAD resolves the frame fully (storage read on a cold cache), so a HEAD 200 means the frame is readable and `Content-Length` is exact.

---

## 3. Contract

### A. Ingest (`embeddings:results` consumer)

Per message, `zip_ref` applies when `FRAME_STORAGE_MODE=zip_ref` **and** the payload has a `zip_url`. Per vector, it also needs an `image_name` or a `zip_entry`. Otherwise the legacy path is used unchanged.

In `zip_ref` mode:

1. No ZIP download and no upload.
2. `point_id = str(uuid4())` (lowercase, hyphenated). The same id is used for the Qdrant point, the DB row and the frame URL.
3. `image_url = f"{FRAMES_PUBLIC_BASE_URL.rstrip('/')}/api/v1/frames/{point_id}"`. If the base is empty, the URL is relative (`/api/v1/frames/{id}`) and a warning is logged once.
4. Stored on `evidence_embeddings` (`zip_url`, `image_name`, `zip_entry`) **and** in the Qdrant payload. `zip_url` is stored without a `?nc=` cache-buster; presigned URLs are never changed. A missing `zip_entry` is stored as `null`.
5. `embedding_requests.image_count = len(embeddings)` (both modes).

`search_matches` rows written by the evidence paths (search consumer, recalculation, safety-net recalc) also store `qdrant_point_id`, `image_name` and `zip_entry` from the point payload. `SearchMatch.to_dict()` and the REST responses are unchanged.

### B. Outbound events (additive, nullable)

| Event | New keys |
|---|---|
| `image_embedding.upserted` | `zip_url`, `image_name`, `zip_entry` |
| `image_embedding_request.*` | `image_count` |
| `image_search.matched` → `matches[]` | `qdrant_point_id`, `image_name`, `zip_entry` |
| `weapons.detected` → `images[]` | `zip_url`, `zip_entry` |
| `image.blacklist_match` | `matched_point_id`, `zip_url`, `zip_entry` |

Old rows emit `null` for the new keys. The `image_url` / `matched_image_url` values become frame URLs for new rows on their own, because they are read from the stored `image_url`.

### C. Frame endpoint — `GET` / `HEAD /api/v1/frames/{point_id}`

Public, no auth. Full reference: [API_REFERENCE.md](../API_REFERENCE.md#get--head-apiv1framespoint_id--evidence-frame-public).

1. `point_id` must be a UUID (malformed → 404).
2. Lookup `evidence_embeddings` by `qdrant_point_id` (unique index). No row, or a row without `zip_url` → **404**.
3. `If-None-Match: "<point_id>"` → **304** with no storage read.
4. Frame bytes: in-process LRU → optional Redis cache → storage. The storage read is: presign if private → tail Range GET (66 KB, central directory) → one Range GET of the entry → inflate + size/CRC check. The parsed central directory is cached per stored `zip_url`.
5. A 416 or 0-byte reply (stale object) is retried **once** with `?nc=<hex>`, only on unsigned URLs.
6. **200** with `Content-Type` from the extension, `Cache-Control: public, max-age=31536000, immutable`, `ETag: "<point_id>"`, exact `Content-Length`, `X-Content-Type-Options: nosniff`.
7. Errors: timeout → **504**; storage error, bad ZIP, missing entry or presign failure → **502**. All errors carry `Cache-Control: no-store`. Failures are never cached.

---

## 4. Environment keys

All in `src/infrastructure/config.py` (Pydantic `BaseSettings`, `validation_alias`).

| Key | Default | Meaning |
|---|---|---|
| `FRAME_STORAGE_MODE` | `zip_ref` | `zip_ref` = store the reference, no download/upload. `upload` = today's behavior (download ZIP, upload frames). Any other value fails at startup. |
| `FRAMES_PUBLIC_BASE_URL` | `""` | Public base of the frame URL, e.g. the gateway host. Empty = relative URL + one warning. **Set it in every deploy.** |
| `FRAMES_FETCH_TIMEOUT_S` | `25` | Deadline of one frame read (semaphore wait + presign + reads). Over it → 504. Keep the gateway upstream timeout ≥ this. |
| `FRAMES_MAX_CONCURRENT_FETCHES` | `8` | Global cap on concurrent storage reads by the endpoint. |
| `FRAMES_INDEX_CACHE_SIZE` | `512` | Number of parsed ZIP central directories kept in memory. |
| `FRAMES_BYTES_CACHE_MAX_MB` | `128` | In-process frame-bytes LRU, bounded by total size. |
| `FRAMES_BYTES_CACHE_REDIS_URL` | `""` | Optional shared byte cache (7-day TTL, key `frames:bytes:v1:<point_id>`). Empty = off. |
| `STORAGE_SERVICE_URL` | `http://storage-service:8006` | Existing key; also used for the presign POST. |

---

## 5. Rollout

1. Apply migration `b2f6c8d1e4a7` (additive nullable columns only).
2. Gateway: confirm the public `/api/v1/frames/*` route (D5) is live.
3. Before the deploy, run the read-only Range check against one public and one private evidence ZIP:
   ```bash
   python scripts/check_zip_range.py <zip_url> --zip-entry <entry>
   ```
   It prints the status and `Content-Range` of the tail and entry Range GETs, the entry size, and `RESULT: PASS|FAIL`. It never writes anything.
4. Deploy with `FRAME_STORAGE_MODE=zip_ref` and `FRAMES_PUBLIC_BASE_URL` set.
5. Check: new `evidence_embeddings` rows have `zip_url` set and `image_url` = `.../api/v1/frames/<id>`; `GET` on one of those URLs returns 200; no new uploads in the logs (`frame_mode=zip_ref`).

## 6. Rollback

- **Code:** set `FRAME_STORAGE_MODE=upload`. Ingest goes back to download + upload, byte-identical to before. **This only works when storage accepts writes again**; while storage is read-only, `upload` mode fails the same way it did on 2026-10-21.
- The frame endpoint and the new columns can stay. Rows written in `zip_ref` mode keep working through the endpoint after a rollback.
- The migration is additive. Its downgrade drops only the new columns; do not run it while rows depend on them.

## 7. Old rows

- Rows written before this change have an `image_url` in `public/embeddings/...` and NULL `zip_url`. Their URLs keep working as before; nothing is migrated.
- `GET /api/v1/frames/{id}` for such a row returns **404** (no `zip_url`).
- Their events emit `null` for the new keys. `search_matches` rows written before the column existed fall back to `match_metadata['qdrant_point_id']` in the `image_search.matched` event.

---

## 8. Code map

| Piece | File |
|---|---|
| Settings | `src/infrastructure/config.py` |
| Migration | `alembic/versions/b2f6c8d1e4a7_add_frame_zip_reference_columns.py` |
| ZIP Range reader + presign | `src/services/zip_frame_reader.py` |
| Frame endpoint + caches | `src/api/v1/routers/frames.py` |
| Frame-URL helpers | `src/application/helpers/frame_refs.py` |
| Ingest switch | `src/streams/embedding_results_consumer.py` |
| Read-only Range check | `scripts/check_zip_range.py` |
| Tests | `tests/test_zip_frame_reader.py`, `tests/test_frames_router.py`, `tests/test_embedding_zip_ref_mode.py`, `tests/test_check_zip_range.py` |
