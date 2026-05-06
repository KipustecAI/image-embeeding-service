# Phase 0: Context

## Problem statement

Right now the image embedding service has two detection paths:

1. **Weapons enrichment** ([docs/weapons/](../weapons/)) — YOLO-based object detection. Runs on every evidence. Detects specific object classes (handguns, knives, etc.) and feeds the `weapons:detected` report stream.
2. **Similarity search** (`POST /api/v1/search`) — user supplies a query image, gets back similar evidence based on CLIP cosine similarity. On-demand, one query at a time.

What's missing: a **standing list of "things to watch for"** that gets matched against incoming evidence automatically. Product has two immediate use cases — wanted vehicle plates captured in reference photos, and known-stolen objects from case files — that are semantic image matches, not object-detection problems. CLIP handles both naturally, but there's no infrastructure for "here's a reference image, tell me whenever anything similar comes in".

This plan adds that infrastructure.

## What face-blacklist already built, and what we can steal

The `deepface-restapi` service implements exactly this pattern for faces. The architecture is battle-tested and in production today. We steal as much as we can:

| Face-blacklist primitive | Reuse verbatim? | Notes |
|---|---|---|
| 3-table spine: `Person` → `Image` → `Embedding` | **Yes** | Rename to `BlacklistImageEntry` → `BlacklistImageReference` → `BlacklistImageEmbedding`. Status machines (1-5 on entry, 1-4 on image), multi-tenant `user_id`, JSONB `json_data` — all copied. |
| Status enum values and transitions | **Yes** | `CREATED (1) → PROCESSING (2) → INDEXED (3) → UPDATING (4) → ERROR (5)` on entries; `TO_PROCESS (1) → PROCESSING (2) → PROCESSED (3) → ERROR (4)` on references. |
| `blacklist_version INT` for re-matching after profile changes | **Yes** | Same pattern. When an admin edits an entry, bump version and re-run. |
| Single Qdrant collection with `type`/`source_type` discriminator | **Yes** | We already use `source_type` in our payload for other things. Add `"blacklist"` as a new value. |
| Reverse search when new blacklist image is added | **Yes** | But async (see Phase 04) — our evidence corpus is larger than theirs. |
| Inline match on new incoming detection | **Yes** | For us, this lives in `embedding_results_consumer` after the DB commit. |
| `face:blacklist_match` → report-generation stream | **Yes — as `image:blacklist_match`** | Already reserved as type 1E in [../requirements/REPORT_GENERATION_STREAMS.md](../requirements/REPORT_GENERATION_STREAMS.md). |
| Sync Redis client for publishing from the API router | **Yes** | Same pattern — XADD from the POST handler when a new blacklist reference is accepted. |

## What's genuinely different from faces

### 1. Semantic vs identity matching

Face embeddings (AdaFace) are **identity-specific**: two photos of the same person produce similar vectors regardless of lighting, angle, or expression, and two different people produce dissimilar vectors even if they share visual features. A blacklist match is a strong claim about identity.

CLIP embeddings are **semantic**: two photos of different red Honda Civics will match because "red sedan". Two photos of different handguns will match because "handgun". Blacklist matches are claims about *resemblance*, not identity.

**Practical implication:** threshold tuning matters more. A "wanted vehicle" blacklist needs a much tighter threshold than a "weapons reference" one, because the latter will fire on any weapon. This is why we leave hooks for per-entry and per-category threshold overrides even though v1 ships with a single global threshold — we already know the global default will need tuning once real categories appear.

### 2. No "person" abstraction

A blacklisted face is always a person — singular, identity-bound. A blacklisted image could be:
- A specific vehicle (identified by plate or paint pattern)
- A specific scene (a known stolen-goods storage location)
- A specific object (a unique work of art from a burglary report)
- A pattern of infraction (photos representative of a known fraud scheme)

The schema uses a generic `entry` as the unit, with an open `category` string to tag intent. No enum — product will want to add categories without schema changes.

### 3. Reverse search cost is higher

The deepface corpus is faces, which means per-evidence there are at most a few faces embedded. Our CLIP corpus is full-frame images — one evidence can have 9+ embeddings. At current volume (~thousands of evidence), full-history reverse search is still fine. At 10× or 100× the volume, it won't be, so the reverse search goes async from day one (Phase 04). The async-by-default design buys us runway without making the single-query case slow.

### 4. Weapons enrichment coexists, doesn't integrate

A single evidence could match a blacklist entry AND trigger the weapons detector. The two paths are orthogonal — they ride the same ingest but fire separate report events (`weapons.detected` and `image.blacklist_match`). report-generation's type-1 dispatcher handles each independently. **No effort to merge them into a single report.** If product later wants "combined alert" reports, that's a report-generation enrichment, not ours.

## The category infrastructure — why it's worth pulling forward

The user asked for `category` as a blacklist concept. It's actually useful well before blacklist ships:

- **Reports by category.** "Show me how many handguns I flagged this week, grouped by evidence category (vehicles, buildings, people)." Can't answer today.
- **Search by category.** A user with thousands of evidence wants to search "images similar to this one, restricted to vehicle-category evidence". Can't answer today.
- **Blacklist is simpler.** If `category` already exists as a Qdrant payload field and has a filter in the search API, blacklist just reuses it — the blacklist feature doesn't have to introduce category infrastructure, it just adds one more source-type value.

So Phase 01 ships the category infrastructure as a standalone deliverable: producer payload field, DB column, Qdrant payload index, search API filter. Independently useful, and ~80% of the work blacklist would have had to do anyway.

## Risks worth flagging

### False-positive explosion

CLIP is semantic. A "wanted red Honda Civic" blacklist will match *every* red Honda Civic in the corpus — not just the specific one. Before the first user activates a blacklist entry, someone needs to:
- Set expectations with product that reports will not be 1:1 with the specific referenced object.
- Crop reference images to the distinguishing region (plate, unique paint damage, custom decal) before embedding. **Not our job, but worth calling out to whoever's onboarding users.**
- Tune the global threshold conservatively — better to miss some matches than spam report-generation with hundreds of matches per day.

Mitigation: ship with a **conservative default threshold** (~0.85 cosine similarity), and a **per-entry threshold override** hook documented in Phase 05 code even though the override schema field isn't wired up in v1. The env var can be tuned live by ops without code changes.

### Reverse-search cost on backfill

Reverse search on full history is a single Qdrant query per new blacklist vector, with the query vector being the blacklist reference's CLIP embedding. Qdrant HNSW makes each query ~10-70ms. For a new entry with 5 reference images, that's 5 queries — cheap. For a bulk import of 500 entries × 5 images, that's 2,500 queries — ~30 seconds of compute on a single worker. Still manageable.

Where it breaks: if the user pastes a 10,000-image bulk import (not supported in v1 anyway). Asynchronous backfill means the API returns 202 immediately and the job runs in the background — the user's session isn't blocked. Progress is queryable.

### Multi-tenant bleed

Blacklist entries are scoped to `user_id`. But our Qdrant payload already has `user_id`, and the strict-filter helper in Phase 03 will enforce that reverse-search and inline-match queries are always scoped to the owning user. **However:** if an admin creates an entry with `user_id=null`, what does "null user_id" mean for matching? Options:

- **v1 choice: disallow null user_id on blacklist entries.** Every entry must be owned by a specific tenant. Matching is strictly per-tenant. No cross-tenant leakage possible.
- If product later wants "global blacklists" (an admin flags a suspect for everyone to be alerted on), that's a separate feature with distinct access-control requirements. Deferred.

### Dedup on reference images

Same image URL added twice — to the same entry, or to different entries — shouldn't produce two Qdrant points. v1 approach:

- Within a single entry: enforce uniqueness of `(entry_id, image_url)` via a unique constraint.
- Across entries: allow (same image can legitimately serve two entries), but log it.

This adds nothing to the failure modes documented in Phase 02. Just a migration constraint.

### Filter forgetfulness

Every user-facing similarity search must now add `source_type="evidence"` to its Qdrant filter — forgetting it would return blacklist images in search results, which is a multi-tenant leak (blacklist images from user A could appear in user B's search if user_id filter also got missed).

Mitigation (detailed in Phase 03): centralize the filter-building in a single helper function. The search use case, recalculation, inline-match, and reverse-search all call the same function. Code review only has to watch *one function* for correctness. We do the same thing today for multi-tenant user_id isolation — this just extends the pattern.

## Who does what

| Responsibility | Owner |
|---|---|
| Implement this plan | This service (image-embeeding-service) |
| Consume `image:blacklist_match` events | [report-generation](../../../../report-generation/) team — they already have the sub-type 1E placeholder from [../requirements/REPORT_GENERATION_STREAMS.md §3](../requirements/REPORT_GENERATION_STREAMS.md). Phase 05 of this plan formalizes the DTO so they can start implementing once we ship. |
| Add optional `category` field to the ETL producer payload | ETL / upstream producer team — lightweight change documented in Phase 01. |
| Tune the global `BLACKLIST_MATCH_THRESHOLD` after deploy | Ops, with product input. The default is a shipping value, not a committed production value. |
| Onboard end users on "what a blacklist is for" and "crop your reference images" | Product. Not an engineering concern. |
