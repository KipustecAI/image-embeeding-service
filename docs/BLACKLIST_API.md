# Image Blacklist API — Frontend Contract

**Audience:** frontend / mobile clients building the blacklist management UI.

**Status:** **available in dev** as of Phase 06. Production deploy follows the rollout sequence below — coordinate with the backend team before pointing prod traffic at it.

This document is the single contract for consumers. The internal phase planning lives under [image-blacklist/](image-blacklist/); skip those unless you're contributing to backend implementation.

---

## What this feature is

Users (or admins on their behalf) register CLIP-embedded "blacklist" reference images on their tenant — license plates, vehicles, scenes, anything visually identifiable. Whenever a piece of evidence is ingested that looks like a registered reference (cosine similarity above a configurable threshold), the system fires a match event to the report-generation service so end users get an alert.

Two trigger paths produce alerts:

1. **New evidence matches an existing blacklist entry** (live alerting on ingest).
2. **A newly added blacklist entry matches historical evidence** (catch-up reverse search).

This API surface lets clients manage the blacklist entries and their reference images. Match events themselves don't surface here — they go to `image:blacklist_match` (consumed by report-generation, see [requirements/REPORT_GENERATION_STREAMS.md §3](requirements/REPORT_GENERATION_STREAMS.md)).

---

## Authentication

All endpoints require gateway-set headers. Same scheme as the rest of the search API ([API_REFERENCE.md](API_REFERENCE.md)):

| Header | Required | Meaning |
|---|---|---|
| `X-User-Id` | yes | Tenant id (UUID). Owns the blacklist entries. |
| `X-User-Role` | yes | `user` (default) \| `admin` \| `root` \| `dev` |
| `X-User-Created-By` | no | Parent user id for guest accounts |
| `X-Request-Id` | recommended | For correlation in logs |

**Multi-tenant rules:**

- `user` role: can only manage **their own** entries. Foreign-tenant entries return 404 (not 403 — we don't leak their existence).
- `admin`, `root`, `dev`: can manage any tenant's entries. Use `?user_id=<uuid>` on list endpoints to scope to a specific tenant.

---

## Endpoint summary

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/blacklist/image-entries` | yes | Create a new blacklist entry |
| GET | `/api/v1/blacklist/image-entries` | yes | List entries (paginated, multi-tenant filtered) |
| GET | `/api/v1/blacklist/image-entries/{id}` | yes | Entry detail with references |
| PATCH | `/api/v1/blacklist/image-entries/{id}` | yes | Partial update of entry fields |
| DELETE | `/api/v1/blacklist/image-entries/{id}` | yes | Hard-delete entry + all refs + Qdrant points |
| POST | `/api/v1/blacklist/image-entries/{id}/references` | yes | Attach a reference image (triggers async embed) |
| DELETE | `/api/v1/blacklist/image-entries/{id}/references/{ref_id}` | yes | Remove a reference + its Qdrant point |
| POST | `/api/v1/blacklist/image-entries/{id}/backfill` | yes | Re-run reverse search across all references |
| POST | `/api/v1/blacklist/image-entries/{id}/cross-reference` | yes | **GPU-free** — does this entry appear in a set of indexed runs? Returns matches inline. Full spec: [IMAGE_INDEX_API.md §6](apis/IMAGE_INDEX_API.md). |

> **Gateway base path (frontend):** through the API gateway the public prefix is
> **`https://api.lookia.mx/api/v1/images/blacklist`**, which the gateway rewrites to the internal
> `/api/v1/blacklist/image-entries` shown above. **So the frontend replaces the leading
> `/api/v1/blacklist/image-entries` with `/api/v1/images/blacklist`** — e.g. create an entry with
> `POST https://api.lookia.mx/api/v1/images/blacklist`, add a reference with
> `POST …/api/v1/images/blacklist/{id}/references`, cross-reference with
> `POST …/api/v1/images/blacklist/{id}/cross-reference`. The gateway injects `X-User-Id` from your
> API key (send `X-API-Key`, not the `X-User-*` headers). Cloudflare needs a real `User-Agent`.

---

## Data model — what's an "entry"?

A **blacklist entry** is the user-visible profile (e.g. *"Suspect plate, case 2025-042"*). It carries a name, optional category and description, an active flag, and a status that reflects backend processing.

Each entry has zero or more **references** — actual image URLs that get CLIP-embedded and stored in Qdrant. References have their own status because embedding is asynchronous.

```
BlacklistEntry (1) ──< (N) BlacklistReference (1) ──< (N) BlacklistEmbedding
   "Suspect plate"         image_url + status            qdrant_point_id
```

### Status codes

**Entry status (`status` field):**

| Value | Name | Meaning |
|---|---|---|
| 1 | CREATED | Profile created, no references yet |
| 2 | PROCESSING | At least one reference is being embedded |
| 3 | INDEXED | At least one reference is stored in Qdrant — entry is matchable |
| 4 | UPDATING | References being added/removed, version bumping |
| 5 | ERROR | Processing failed (see reference-level errors for detail) |

**Reference status (`status` field on each reference):**

| Value | Name | Meaning |
|---|---|---|
| 1 | TO_PROCESS | Inserted, awaiting GPU embed |
| 2 | PROCESSING | GPU is embedding |
| 3 | PROCESSED | Vector stored in Qdrant, matchable |
| 4 | ERROR | Embed failed (see `error_message`) |

---

## Endpoint details

### POST /api/v1/blacklist/image-entries

Create a new entry. References are added separately.

**Request:**
```json
{
  "name": "Placa del sospechoso — caso 2025-042",
  "category": "vehicle",
  "description": "Vehículo vinculado a investigación abierta",
  "match_threshold": 0.88,
  "json_data": { "case_id": "CRIM-2025-042" }
}
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string (1–255) | yes | | Free-form display name |
| `category` | string \| null | no | null | Optional tag for grouping |
| `description` | string \| null | no | null | Free-form text |
| `match_threshold` | float (0.0–1.0) \| null | no | null | Per-entry override of the global blacklist threshold (env: `BLACKLIST_MATCH_THRESHOLD`, default 0.85). v1 only reads this column at match time — changing it does NOT re-evaluate existing matches. Use `/backfill` for that. |
| `json_data` | object \| null | no | null | Free-form extra metadata; stored as JSONB |

**Response — 201 Created:**
```json
{
  "id": "a1b2c3d4-...",
  "name": "Placa del sospechoso — caso 2025-042",
  "category": "vehicle",
  "description": "Vehículo vinculado a investigación abierta",
  "status": 1,
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

The new entry starts at status `1 (CREATED)`. It transitions to `2 (PROCESSING)` when the first reference is added, then to `3 (INDEXED)` once any reference completes embedding.

### GET /api/v1/blacklist/image-entries

List entries visible to the caller, paginated.

**Query params:**

| Name | Default | Notes |
|---|---|---|
| `active` | `true` | `true` (only active) \| `false` (only inactive) \| `all` (both) |
| `category` | none | Exact match filter |
| `user_id` | none | **Admin only.** Scopes to a specific tenant. Ignored for non-admin callers. |
| `limit` | 20 | Max 100 |
| `offset` | 0 | |

**Response — 200 OK:**
```json
{
  "total": 42,
  "limit": 20,
  "offset": 0,
  "entries": [
    {
      "id": "...",
      "name": "...",
      "category": "vehicle",
      "status": 3,
      "active": true,
      "blacklist_version": 1,
      "match_threshold": null,
      "references_count": 3,
      "embeddings_count": 3,
      "user_id": "...",
      "created_at": "...",
      "updated_at": "..."
    }
  ]
}
```

`references_count` and `embeddings_count` are populated via two bulk SQL queries (not per-row), so latency is constant in page size.

### GET /api/v1/blacklist/image-entries/{id}

Entry detail with the full reference list inline.

**Response — 200 OK:**
```json
{
  "id": "...",
  "name": "...",
  "category": "vehicle",
  "description": "...",
  "status": 3,
  "active": true,
  "blacklist_version": 1,
  "match_threshold": 0.88,
  "references_count": 1,
  "embeddings_count": 1,
  "user_id": "...",
  "created_at": "...",
  "updated_at": "...",
  "references": [
    {
      "id": "e5f6a7b8-...",
      "entry_id": "a1b2c3d4-...",
      "image_url": "https://minio.lookia.mx/blacklist/...",
      "image_type": "reference",
      "status": 3,
      "error_message": null,
      "created_at": "..."
    }
  ]
}
```

**Errors:**
- 404 if the id doesn't exist or isn't visible to the caller.

### PATCH /api/v1/blacklist/image-entries/{id}

Partial update. Send only the fields you want to change. `user_id` is immutable — cross-tenant reassignment is forbidden by design.

**Request — any subset of:**
```json
{
  "name": "New name",
  "category": null,
  "description": "Updated case detail",
  "active": false,
  "match_threshold": 0.92,
  "json_data": { "ticket": "OPS-1234" }
}
```

**Response — 200 OK:** the updated entry (same shape as POST response).

**Version-bump rules:** the `blacklist_version` field bumps when a change is "matching-relevant" — i.e. when the same evidence might produce a different match outcome. v1 rules:

| Change | Bumps version? |
|---|---|
| `match_threshold` changes | yes |
| `active` toggles `false → true` (re-activation) | yes |
| Anything else (name, description, category, json_data) | no |

The version field is included on every match event (`image:blacklist_match`) so the report receiver can dedup on `(evidence_id, entry_id, version)`. A version bump means matches re-evaluate as fresh; a typo fix on the name doesn't.

### DELETE /api/v1/blacklist/image-entries/{id}

Hard-delete the entry, all its references, and all its Qdrant points. Cascades through SQL via FK; Qdrant cleanup is best-effort.

**Response — 204 No Content** on success.

**Errors:**
- 404 if the id doesn't exist or isn't visible to the caller.
- If Qdrant cleanup fails after the SQL row is gone, the response is still 204 — the orphan points are logged ERROR-level for ops to clean up.

**Soft delete:** v1 doesn't expose one. If you want to keep the entry for audit but stop matching, PATCH `active=false` instead.

### POST /api/v1/blacklist/image-entries/{id}/references

Attach a reference image to an entry. The actual embedding happens asynchronously through the GPU compute service — the API returns 202 immediately.

**Request:**
```json
{
  "image_url": "https://minio.lookia.mx/uploaded-via-ui/...",
  "image_type": "reference"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `image_url` | URL | yes | Must be `http://` or `https://` and reachable by the GPU. Local `file://` URLs are rejected (422). |
| `image_type` | string | no | Default `"reference"`. Free-form tag for distinguishing variants (e.g. `"enhanced"`). |

**Response — 202 Accepted:**
```json
{
  "id": "e5f6a7b8-...",
  "entry_id": "a1b2c3d4-...",
  "image_url": "https://...",
  "status": 1,
  "created_at": "..."
}
```

Reference starts at status `1 (TO_PROCESS)`. Poll `GET /image-entries/{id}` to observe the transition to `2 (PROCESSING)` then `3 (PROCESSED)` (or `4 (ERROR)` with `error_message`).

**Errors:**
- 404 if the entry doesn't exist or isn't visible.
- 409 if the same `image_url` was already attached to this entry.
- 422 on URL validation failure.

**Side effects after 202:**
1. Reference inserted at status TO_PROCESS.
2. Entry status lifts CREATED → PROCESSING (if applicable).
3. Backend publishes to the `evidence:search` stream with `purpose="blacklist_embed"` so the GPU embeds the image.
4. When the GPU's result lands, the backend stores the vector in Qdrant + DB and **schedules a reverse-search job** — historical evidence is matched against this new vector. Matches go to `image:blacklist_match` for report-generation to consume.

### DELETE /api/v1/blacklist/image-entries/{id}/references/{reference_id}

Remove one reference from an entry. Cascades the embedding row in SQL and deletes the Qdrant point.

**Response — 204 No Content** on success.

**Errors:**
- 404 if entry or reference doesn't exist (or isn't visible).

**In-flight references:** if the reference is at status `2 (PROCESSING)` when you delete it, the in-flight embed will land as an orphan and be dropped silently by the consumer. The 204 still applies — the SQL row is gone.

### POST /api/v1/blacklist/image-entries/{id}/backfill

Re-run reverse search for every PROCESSED reference under an entry. Use cases: ops recovery after an incident, or post-`match_threshold`-change re-evaluation.

**Response — 202 Accepted:**
```json
{
  "message": "Backfill scheduled",
  "entry_id": "a1b2c3d4-...",
  "references_count": 3,
  "job_ids": ["reverse_search_e5f6...-a", "reverse_search_e5f6...-b"]
}
```

Each reference fires its own APScheduler job. `job_ids` is the list of jobs that were scheduled (only PROCESSED references contribute — pending/error references are skipped silently).

**Errors:**
- 404 if the entry doesn't exist.

---

### POST /api/v1/blacklist/image-entries/{id}/cross-reference  ✅ live

**"Does this blacklisted entry appear in a set of on-demand *indexed* runs?"** A **GPU-free** reverse
search: we take the entry's already-stored reference vector(s) and search the `image_index_embeddings`
collection (the [on-demand image-index](apis/IMAGE_INDEX_API.md) feature) scoped to a list of
`external_ids` (runs) + your tenant. Because nothing is re-embedded, it returns **inline (200, no poll)**.

This is distinct from `/backfill` (which reverse-searches the **live evidence** collection): cross-reference
targets the **indexed on-demand runs** and is scoped to the `external_ids` you pass.

**Prerequisite:** the entry must be `INDEXED` (status `3`) — its reference image(s) embedded.

- Body: `{ "external_ids": ["run-42", …] (1–200), "threshold"?: float, "max_results"?: int }`
- Returns: `{ entry_id, external_ids, threshold_used, match_count, matches: [...] }` — each match carries
  `external_id` / `batch_id` / `item_index` / `image_id` (= the crop's `evidence_id`) / `source_url` /
  `similarity_score`.
- **Full request/response spec + a verified worked example:** [IMAGE_INDEX_API.md §6](apis/IMAGE_INDEX_API.md).
- Gated by `IMAGE_INDEX_SEARCH_ENABLED` (prod-default true) → `503` when off. Tenant-scoped: a foreign
  `entry_id` → `404`; a run you don't own → simply absent from the results.

Verified live in prod (2026-07-23): a blacklisted image was found in its run at `similarity_score`
`0.9999998`, with **zero compute round-trips**.

---

## Error response shape

All error responses follow FastAPI's default:
```json
{ "detail": "Entry not found" }
```

| Status | When |
|---|---|
| 401 | Missing `X-User-Id` |
| 404 | Entry / reference not found, or not visible to the caller |
| 409 | Duplicate `(entry_id, image_url)` on reference create |
| 422 | Pydantic validation failure (bad URL, name out of range, etc.) |
| 5xx | Backend / Qdrant / DB unreachable — retry with exponential backoff |

---

## Workflow examples

### Creating a blacklist and watching it become matchable

```bash
# 1. Create the entry
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"name": "Sospechoso 042", "category": "vehicle"}'
# → 201 with entry { "id": "a1b2c3d4-...", "status": 1 }

# 2. Attach a reference image
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries/a1b2c3d4-.../references \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://minio.lookia.mx/uploads/ref_1.jpg"}'
# → 202 with reference { "id": "e5f6...", "status": 1 }

# 3. Poll for status (10–30s on the GPU's typical roundtrip)
curl http://localhost:8001/api/v1/blacklist/image-entries/a1b2c3d4-... \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user"
# → entry.status = 3 (INDEXED), references[0].status = 3 (PROCESSED)

# 4. Reverse search runs automatically. For matches to fire on FUTURE
#    evidence, no further action — the inline-match path on ingest
#    handles it. For historical evidence, the reverse-search job kicked
#    off when the reference completed embedding (step 3) already
#    published any matches to image:blacklist_match.
```

### Re-evaluating against a new threshold

```bash
# Tighten the threshold for an entry
curl -X PATCH http://localhost:8001/api/v1/blacklist/image-entries/a1b2c3d4-... \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user" \
  -H "Content-Type: application/json" \
  -d '{"match_threshold": 0.92}'
# → 200 with entry.blacklist_version bumped to 2

# Re-run reverse search at the new threshold
curl -X POST http://localhost:8001/api/v1/blacklist/image-entries/a1b2c3d4-.../backfill \
  -H "X-User-Id: 550e8400-..." \
  -H "X-User-Role: user"
# → 202 with the list of scheduled job_ids
```

The downstream report-generation service receives fresh match events tagged with `blacklist_entry_version: 2` — its dedup key on `(evidence_id, entry_id, version)` recognizes them as new and surfaces them as fresh alerts.

---

## What's NOT exposed in v1

- **Listing matches.** Matches live in the report-generation service's domain. If you need "all matches for entry X", query that service. We don't store match rows on this side.
- **Soft delete.** Use PATCH `active=false`.
- **Bulk operations** (delete N entries / N references). Loop on the client side.
- **Search by reference image content** (e.g. "find blacklist entries similar to this image"). Different feature.
- **Re-embed** (re-run the GPU on an existing reference image). Delete + add the reference again.

---

## Cross-references

- Backend implementation phases: [image-blacklist/](image-blacklist/)
- Match-event contract for the report-generation team: [requirements/REPORT_GENERATION_STREAMS.md §3](requirements/REPORT_GENERATION_STREAMS.md)
- Main search/category API: [API_REFERENCE.md](API_REFERENCE.md)
- Curl examples for ingest / search: [CURL_EXAMPLES.md](CURL_EXAMPLES.md)
