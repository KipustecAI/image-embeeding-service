# Phase 05: Match Detection + Report Publishing

This phase wires up the two match trigger points and the outgoing report stream:

1. **Inline match** — after a new evidence is ingested and fully committed, search the user's blacklist subset in Qdrant for matches against the new evidence vectors. Each match → publish an `image:blacklist_match` event.
2. **Reverse search match** — the async job from Phase 04 (`_run_reverse_search`) already calls `_publish_blacklist_match`. This phase defines exactly what that event looks like and how it's sent.
3. **Formalize the DTO** — promote the placeholder in [`docs/requirements/REPORT_GENERATION_STREAMS.md §3`](../requirements/REPORT_GENERATION_STREAMS.md) (type 1E) from rough sketch to real contract.

## Scope

- New helper `src/application/helpers/blacklist_match_events.py` — pure function `build_blacklist_match_event(...)` that produces the exact DTO. Mirrors the `weapon_report_events.py` pattern.
- Extend [`embedding_results_consumer._process_embeddings_result`](../../src/streams/embedding_results_consumer.py) with an inline-match step: after the DB commit, for each new evidence vector, search the `source_type="blacklist"` subset of Qdrant and publish matches.
- The reverse-search path from Phase 04 calls the same event builder + publisher.
- Update `docs/requirements/REPORT_GENERATION_STREAMS.md §3` with the real DTO, example payloads, trigger semantics, and rename it from placeholder-style to a real contract section.
- **Fire-and-forget publish** semantics — a failed `image:blacklist_match` publish must not break ingest (same policy as weapons).

## Files modified

| File | Action |
|---|---|
| `src/application/helpers/blacklist_match_events.py` | **Create** — event builder |
| [src/streams/embedding_results_consumer.py](../../src/streams/embedding_results_consumer.py) | Add inline-match block after DB commit |
| `src/services/blacklist_match_service.py` | **Create** — shared publish logic called from both consumer and reverse-search paths |
| [src/infrastructure/config.py](../../src/infrastructure/config.py) | Reuse existing `stream_reports_image_blacklist_match` env var (already wired in the weapons Phase 4) |
| [docs/requirements/REPORT_GENERATION_STREAMS.md](../requirements/REPORT_GENERATION_STREAMS.md) | §3 promoted from placeholder to contract |
| `tests/test_blacklist_match_events.py` | **Create** — pure unit tests |

## The `image:blacklist_match` DTO

Matches the shape of `WeaponsDetectedEvent` for easy reuse of report-generation's base consumer pattern. Self-contained — no receiver-side DB lookup needed.

```typescript
// To be added to report-generation/src/type1/dto/image-blacklist-match.event.ts
export interface ImageBlacklistMatchEvent {
  // ── Identity / multi-tenant ──
  user_id: string;              // Tenant owner of both blacklist entry and evidence

  // ── The blacklist side ──
  blacklist_entry_id: string;
  blacklist_entry_name: string;
  blacklist_entry_category: string | null;  // From entry.category, see Phase 01
  blacklist_entry_version: number;          // For idempotency — receiver can dedup
                                            // on (evidence_id, entry_id, entry_version)
  blacklist_reference_id: string;           // Which specific reference image matched
  blacklist_reference_url: string;          // URL to the reference image
  blacklist_model_version: string;

  // ── The evidence side ──
  evidence_id: string;
  evidence_camera_id: string | null;
  evidence_device_id: string | null;
  evidence_app_id: number | null;
  evidence_infraction_code: string | null;
  evidence_category: string | null;
  matched_image_url: string;     // The specific evidence image that matched
  matched_image_index: number;
  matched_qdrant_point_id: string;

  // ── Match detail ──
  similarity_score: number;      // 0..1 cosine similarity
  threshold_used: number;        // The threshold at match time — lets receiver explain why
  trigger: "inline" | "reverse_search";  // Which code path fired this match

  // ── Timing ──
  matched_at: string;            // ISO-8601 UTC
}
```

Key design choices:

- **`blacklist_entry_version` included** so the report receiver can build a dedup key `(evidence_id, entry_id, entry_version)`. If an entry is re-indexed (version bumped) and the same evidence matches again, the receiver can decide whether to generate a new report or suppress as duplicate — policy on their side.
- **Both IDs echoed** (`blacklist_entry_id` + `blacklist_reference_id`) so the receiver's PDF can attribute the match to the specific reference image, not just the entry.
- **`trigger` field** distinguishes an inline match (new evidence matched existing blacklist) from a reverse-search match (new blacklist entry matched historical evidence). Receiver may want to phrase the alert differently — "new weapon in your stored evidence" vs "historical evidence matches your new blacklist entry".
- **`similarity_score` + `threshold_used`** both included — similarity alone is ambiguous ("is 0.87 high or low?"); paired with threshold, the receiver can render `"Match confidence: 92% (threshold 85%)"`.

### Example payload — inline match

A new evidence arrives, one of its frames matches an existing blacklist entry:

```json
{
  "user_id": "3996d660-99c2-4c9e-bda6-4a5c2be7906e",

  "blacklist_entry_id": "a1b2c3d4-...",
  "blacklist_entry_name": "Placa del sospechoso - caso 2025-042",
  "blacklist_entry_category": "vehicle",
  "blacklist_entry_version": 1,
  "blacklist_reference_id": "e5f6a7b8-...",
  "blacklist_reference_url": "https://minio.lookia.mx/blacklist/user-3996/.../ref_1.jpg",
  "blacklist_model_version": "clip-vit-b-32",

  "evidence_id": "dd946e14-17b2-4d6d-a94a-94793fce2347",
  "evidence_camera_id": "0d3f9a4b-3381-4e9e-b8a7-6db8ad0fc5e3",
  "evidence_device_id": "1d7d50b3-b97e-4725-b044-e2de4624d2e5",
  "evidence_app_id": 1,
  "evidence_infraction_code": "SMVV8UGE_116_875_20260410100151",
  "evidence_category": "vehicle",
  "matched_image_url": "https://minio.lookia.mx/lucam-assets/embeddings/.../frame_3.jpg",
  "matched_image_index": 3,
  "matched_qdrant_point_id": "326b5f83-6d88-4c34-b82d-c46ccaa104b1",

  "similarity_score": 0.893,
  "threshold_used": 0.85,
  "trigger": "inline",

  "matched_at": "2026-04-18T14:22:03.501Z"
}
```

### Example payload — reverse-search match

Same shape, `trigger: "reverse_search"`. The `evidence_*` fields reference an evidence that was ingested before the blacklist entry existed.

## Event builder

New file `src/application/helpers/blacklist_match_events.py`:

```python
"""Builder for image:blacklist_match stream events.

Produces the ImageBlacklistMatchEvent payload documented in
docs/requirements/REPORT_GENERATION_STREAMS.md §3. Pure function,
unit-testable without Redis, DB, or Qdrant.
"""

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

IMAGE_BLACKLIST_MATCH_EVENT_TYPE = "image.blacklist_match"


def build_blacklist_match_event(
    *,
    user_id: str,
    # Blacklist side
    blacklist_entry_id: str,
    blacklist_entry_name: str,
    blacklist_entry_category: str | None,
    blacklist_entry_version: int,
    blacklist_reference_id: str,
    blacklist_reference_url: str,
    blacklist_model_version: str,
    # Evidence side
    evidence_id: str,
    evidence_camera_id: str | None,
    evidence_device_id: str | None,
    evidence_app_id: int | None,
    evidence_infraction_code: str | None,
    evidence_category: str | None,
    matched_image_url: str,
    matched_image_index: int,
    matched_qdrant_point_id: str,
    # Match detail
    similarity_score: float,
    threshold_used: float,
    trigger: Literal["inline", "reverse_search"],
    # Optional testing hook
    matched_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the ImageBlacklistMatchEvent payload.

    The returned shape matches docs/requirements/REPORT_GENERATION_STREAMS.md §3
    exactly. See that doc for field semantics. If you change field names here,
    coordinate with the report-generation team.
    """
    if matched_at is None:
        matched_at = datetime.utcnow()

    return {
        "user_id": user_id,
        "blacklist_entry_id": blacklist_entry_id,
        "blacklist_entry_name": blacklist_entry_name,
        "blacklist_entry_category": blacklist_entry_category,
        "blacklist_entry_version": blacklist_entry_version,
        "blacklist_reference_id": blacklist_reference_id,
        "blacklist_reference_url": blacklist_reference_url,
        "blacklist_model_version": blacklist_model_version,
        "evidence_id": evidence_id,
        "evidence_camera_id": evidence_camera_id,
        "evidence_device_id": evidence_device_id,
        "evidence_app_id": evidence_app_id,
        "evidence_infraction_code": evidence_infraction_code,
        "evidence_category": evidence_category,
        "matched_image_url": matched_image_url,
        "matched_image_index": matched_image_index,
        "matched_qdrant_point_id": matched_qdrant_point_id,
        "similarity_score": similarity_score,
        "threshold_used": threshold_used,
        "trigger": trigger,
        "matched_at": matched_at.isoformat() + "Z",
    }
```

Same pattern as `weapon_report_events.py`. Fully typed, fully testable.

## The shared publish path

New file `src/services/blacklist_match_service.py`:

```python
"""Publish image:blacklist_match events. Called by both:
- Inline match from embedding_results_consumer (new evidence → existing blacklist)
- Reverse search from blacklist_reverse_search (new blacklist → existing evidence)

Fire-and-forget: failures log at ERROR level but do not raise. Report
generation is non-critical; ingest and the reverse-search job must not
fail on a publish error.
"""

async def publish_blacklist_match(
    *,
    user_id: str,
    entry: BlacklistImageEntry,
    reference: BlacklistImageReference,
    evidence_id: str,
    evidence_metadata: dict,    # Pulled from the Qdrant point's payload
    matched_image_url: str,
    matched_image_index: int,
    matched_qdrant_point_id: str,
    similarity_score: float,
    threshold_used: float,
    trigger: str,               # "inline" | "reverse_search"
) -> None:
    event = build_blacklist_match_event(
        user_id=user_id,
        blacklist_entry_id=str(entry.id),
        blacklist_entry_name=entry.name,
        blacklist_entry_category=entry.category,
        blacklist_entry_version=entry.blacklist_version,
        blacklist_reference_id=str(reference.id),
        blacklist_reference_url=reference.image_url,
        blacklist_model_version="clip-vit-b-32",
        evidence_id=evidence_id,
        evidence_camera_id=evidence_metadata.get("camera_id"),
        evidence_device_id=evidence_metadata.get("device_id"),
        evidence_app_id=evidence_metadata.get("app_id"),
        evidence_infraction_code=None,  # Not stored in Qdrant payload;
                                         # fetch from DB if needed, else None
        evidence_category=evidence_metadata.get("category"),
        matched_image_url=matched_image_url,
        matched_image_index=matched_image_index,
        matched_qdrant_point_id=matched_qdrant_point_id,
        similarity_score=similarity_score,
        threshold_used=threshold_used,
        trigger=trigger,
    )

    if _stream_producer is None:
        logger.warning(
            "Blacklist match produced but no stream_producer configured; "
            "dropping. evidence=%s entry=%s",
            evidence_id,
            entry.id,
        )
        return

    try:
        _stream_producer.publish(
            stream=settings.stream_reports_image_blacklist_match,
            event_type=IMAGE_BLACKLIST_MATCH_EVENT_TYPE,
            payload=event,
        )
        logger.info(
            "Published image.blacklist_match: evidence=%s entry=%s "
            "score=%.3f trigger=%s",
            evidence_id, entry.id, similarity_score, trigger,
        )
    except Exception as pub_err:
        logger.error(
            "Failed to publish image.blacklist_match for evidence=%s entry=%s: %s",
            evidence_id, entry.id, pub_err, exc_info=True,
        )
```

### Open question on `evidence_infraction_code`

The Qdrant point payload carries `user_id`, `camera_id`, `device_id`, `app_id` — but **not** `infraction_code` (checked the consumer). For an inline match, we have the payload in hand and can pass it directly. For a reverse-search match, the Qdrant search result only gives us the payload; `infraction_code` would need a DB lookup via `evidence_id`.

Options:
- **v1: send `null`.** Infraction code is nice-to-have, not essential for a match alert.
- **v1+: add `infraction_code` to the Qdrant payload** — one line in the embedding consumer, done. Worth it for the 10 bytes of metadata.

**Decision: add `infraction_code` to the Qdrant payload as part of this phase.** It's a trivial consumer-side change and removes the DB roundtrip from the match path. Mark as a sub-task in the implementation list.

## Inline match in the embedding consumer

In [`_process_embeddings_result`](../../src/streams/embedding_results_consumer.py), after the existing weapons-report publish block, add:

```python
# Inline blacklist match — search user's blacklist subset for matches
# against the newly-ingested evidence vectors.
# See docs/image-blacklist/05_MATCH_AND_REPORT.md.
if user_id and _vector_repo:
    try:
        await _inline_blacklist_match(
            user_id=user_id,
            evidence_id=evidence_id,
            embeddings=qdrant_embeddings,  # list[ImageEmbedding] we just stored
        )
    except Exception as match_err:
        # Blacklist match is non-critical — log and continue.
        logger.error(
            f"Inline blacklist match failed for evidence {evidence_id}: {match_err}",
            exc_info=True,
        )
```

`_inline_blacklist_match` does:

1. For each newly-stored evidence `ImageEmbedding`, run `vector_repo.search_similar` with filter `build_blacklist_only_filter({"user_id": user_id})` and threshold from settings.
2. For each match returned, look up the blacklist entry and reference in the DB.
3. Call `publish_blacklist_match(trigger="inline", ...)`.

**Cost:** one Qdrant query per new evidence image. At 9 frames per evidence and 10ms per query, ~90ms added to ingest. Acceptable. If blacklist adoption grows and ingest latency suffers, we can skip the inline match when the user has zero active blacklist entries (the repository has `count_active_by_user` exactly for this reason).

### Optimization: skip when no active blacklist

```python
async def _inline_blacklist_match(
    user_id: str,
    evidence_id: str,
    embeddings: list[ImageEmbedding],
) -> None:
    # Fast exit if user has no active blacklist — don't pay the Qdrant cost
    async with get_session() as session:
        repo = BlacklistImageRepository(session)
        if await repo.count_active_by_user(user_id) == 0:
            return

    # ... rest of the matching logic ...
```

A single indexed SQL query (~1ms) guards ~9 Qdrant queries (~90ms). Worth the branch.

## Reverse-search match

In [Phase 04's `_run_reverse_search`](04_EMBEDDING_FLOW.md), the `_publish_blacklist_match` call becomes:

```python
for match in matches:
    # match is a domain SearchResult with .metadata (Qdrant point payload)
    await publish_blacklist_match(
        user_id=user_id,
        entry=entry,           # loaded once at the top of _run_reverse_search
        reference=reference,   # same
        evidence_id=str(match.evidence_id),
        evidence_metadata=match.metadata,
        matched_image_url=match.image_url,
        matched_image_index=match.metadata.get("image_index", 0),
        matched_qdrant_point_id=match.id,
        similarity_score=match.similarity_score,
        threshold_used=threshold,
        trigger="reverse_search",
    )
```

Same event shape, different `trigger` value. Receiver treats them uniformly.

## Duplicate matches — when the same pair fires twice

Reverse search can fire matches that also get hit by a later inline match, or vice versa. Examples:

- User adds blacklist entry X → reverse search fires `match(evidence=E1, entry=X)` events.
- A new evidence E2 arrives → inline match fires `match(evidence=E2, entry=X)` — new event, no duplication.
- User edits entry X → version bumps → admin runs `POST /entries/X/backfill` → reverse search fires `match(evidence=E1, entry=X)` again.

The third case is the interesting one. The events are legitimately different (new version) but conceptually duplicate. Handling:

- **Producer side (us): publish every time.** Always emit.
- **Receiver side (them): dedup on `(evidence_id, entry_id, entry_version)`.** The version field on the event is there exactly for this. If version is the same, skip report generation.
- **If entry_version changed, the receiver can decide** — new version = fresh report, or suppress if the match was already reviewed.

Documented in `REPORT_GENERATION_STREAMS.md §3` under "Dedup responsibility".

## REPORT_GENERATION_STREAMS.md §3 update

The current §3 is a "do not build yet, rough sketch" placeholder. This phase replaces it with a real contract section mirroring §2 (weapons) in structure:

```
§3 — Sub-type 1E: image:blacklist_match
  §3.1 Stream
  §3.2 Envelope
  §3.3 Payload (full DTO + field-by-field notes)
  §3.4 Example payloads — inline + reverse-search
  §3.5 Trigger semantics — when we publish
  §3.6 Dedup responsibility (receiver-side via (evidence_id, entry_id, version))
  §3.7 Delivery guarantees (fire-and-forget)
  §3.8 Event ordering (no cross-evidence guarantees)
```

Ship this update in the same commit as the match-publishing code so the report-generation team can start building against the real DTO.

## Tests

### Unit — `tests/test_blacklist_match_events.py`

~10 tests mirroring `test_weapon_report_events.py`:
- Event type constant matches the contract
- Full-event shape end-to-end with realistic data
- Inline vs reverse_search trigger values
- `None` pass-through for multi-tenant fields
- `matched_at` defaults to now, has trailing Z
- JSON-serializability
- Entry category and evidence category propagate independently
- `blacklist_entry_version` is an int, not a string

### Integration (manual)

1. Seed a blacklist entry with a reference image (via API, Phase 06) → watch the entry transition CREATED → PROCESSING → INDEXED
2. XADD a synthetic evidence that's visually similar to the reference → inline match fires → observe `image:blacklist_match` event on the stream
3. Inspect receiver-side log: the report-generation consumer should pick it up (once they implement their side)

## Rollout order reminder

1. Phases 01–04 must all be deployed first.
2. This phase touches the hot path of `embedding_results_consumer` — roll carefully. Inline-match is gated on "user has active blacklist entries", so users who haven't adopted blacklist pay ~1ms (the SQL count query) per ingest. Production-safe.
3. Once this ships, the report-generation team needs their consumer + PDF template before end-users see anything. Coordinate the activation date.
