# Weapons event flow — runtime synthesis

How a weapon detection becomes a downstream report alert, end-to-end. Synthesizes the architectural decision (no server-side annotation) with the trigger location and conditions, the data shapes, and the responsibilities of each service involved.

If you're asking *"where do we draw the bboxes?"* or *"how is the weapon notification triggered?"* — this is the page.

---

## The architectural decision

**This service does NOT render annotated images.** We upload the raw frames extracted from the ZIP and forward detection bboxes as structured JSON. The bbox-drawing happens on the [report-generation](../requirements/REPORT_GENERATION_STREAMS.md) side when it composes the alert PDF.

Why: rendering at ingest time would couple the consumer to image-processing dependencies (Pillow, etc.) for a non-critical path. Report-generation already owns the PDF pipeline; giving it both the raw frame URLs and the bbox metadata lets it render at its own cadence and keeps ingest fast.

The relevant alternative we rejected was *"draw bboxes onto the frame inline and upload the annotated version"*. We picked event-with-data instead. The decision is reflected in:

- No `PIL` / image-processing imports in any active code path (the only `from PIL` is in [`src/infrastructure/embedding/clip_embedder.py`](../../src/infrastructure/embedding/clip_embedder.py), which is legacy / not on the runtime graph).
- `evidence_embeddings.image_url` always points at a plain frame, never an annotated one.

---

## End-to-end data flow

```
ETL / Video Server
    │ (creates evidence, builds ZIP)
    │ XADD evidence:embed
    ▼
embedding-compute  (CLIP)
    │ ├─ optionally routed through image-weapons-compute (YOLO)
    │ │  which enriches the payload with weapon_analysis
    │ XADD embeddings:results
    ▼
this service — embedding_results_consumer._process_embeddings_result
    │
    ├─ 1. Download ZIP, extract listed frames
    ├─ 2. Upload each plain frame to storage-service → permanent image_url
    │     (services/storage_uploader.py — no annotation)
    ├─ 3. Per frame:
    │     ├─ Upsert vector into Qdrant payload with
    │     │     weapon_analyzed, has_weapon, weapon_classes (booleans + list)
    │     └─ Append to evidence_embeddings row with weapon_detections JSONB
    │        (per-frame bbox array — full detection data)
    ├─ 4. Create embedding_request row with evidence-level weapon fields
    │     (weapon_analyzed, has_weapon, weapon_classes, weapon_max_confidence,
    │      weapon_summary, weapon_analysis_error)
    ├─ 5. Mark request as EMBEDDED, commit DB transaction
    └─ 6. **Trigger:** publish weapons.detected to weapons:detected stream
                (only if at least one frame had detections — see "When it fires")
                                       │
                                       ▼
                            report-generation
                            (consumes, renders bbox-annotated images, sends PDF)
```

---

## Where the trigger lives

[`src/streams/embedding_results_consumer.py`](../../src/streams/embedding_results_consumer.py), inside `_process_embeddings_result`, right after the DB transaction commits. Conceptually:

```python
# Fire-and-forget: publish a weapons.detected event to the
# report-generation service. Report generation is non-critical —
# a publish failure must never break ingest. See
# docs/requirements/REPORT_GENERATION_STREAMS.md §2.7.
if weapon_analyzed and report_images_with_detections and _stream_producer is not None:
    try:
        event = build_weapons_detected_event(
            evidence_id=evidence_id,
            camera_id=camera_id,
            user_id=user_id,
            device_id=device_id,
            app_id=app_id,
            infraction_code=infraction_code,
            weapon_summary=weapon_summary,
            images_with_detections=report_images_with_detections,
        )
        _stream_producer.publish(
            stream=settings.stream_reports_weapons_detected,
            event_type=WEAPONS_DETECTED_EVENT_TYPE,
            payload=event,
        )
    except Exception as pub_err:
        logger.error(...)
```

Event builder: [`src/application/helpers/weapon_report_events.py`](../../src/application/helpers/weapon_report_events.py) — pure function, unit-tested in `tests/test_weapon_report_events.py`.

---

## When it fires (the three conditions)

All three must be true. Anything else: silent skip, no event.

| Condition | Meaning | If false |
|---|---|---|
| `weapon_analyzed` | The producer included a `weapon_analysis` block in the payload (i.e. the message went through `image-weapons-compute`). | No event — evidence wasn't analyzed for weapons at all. |
| `report_images_with_detections` non-empty | At least one frame on this evidence had ≥1 detection (clean-but-analyzed evidence does not produce a report). | No event — evidence was analyzed but came up clean. The DB still records the analyzed-clean state via `weapon_analyzed=true, has_weapon=false`; ops can review that subset with the `weapons_filter="analyzed_clean"` search mode. |
| `_stream_producer` injected | Lifespan wired the producer in startup. | No event + ERROR log. In normal operation always true. |

The per-frame filter that builds `report_images_with_detections` lives in the embedding loop (around `if per_image_has_weapon` inside `_process_embeddings_result`) — only frames with detections are kept in the list, so the event's `images[]` array is always non-empty when it fires.

---

## Failure semantics — fire and forget

The publish is wrapped in a try/except that logs but does not raise. Ingest commits regardless. Same policy as `image:blacklist_match` ([`blacklist_match_service.publish_blacklist_match`](../../src/services/blacklist_match_service.py)).

| Failure | Consequence | Recovery |
|---|---|---|
| Redis unreachable | One ERROR log line, no event published. | None on this side — the alert is lost. Receiver-side dedup is on `(evidence_id, user_id)` so the missed event surfaces as a missing alert, not a corrupted one. If patterns of misses appear, escalate to ops. |
| Producer not wired (startup bug) | One ERROR log per affected evidence + no event. | Restart fixes wiring; no replay. |
| Receiver consumer down | Event sits in `weapons:detected` stream until they catch up — standard Redis Stream semantics. | No action; their consumer group lag absorbs it. |

If the policy ever needs to change (e.g. weapons alerts become safety-critical and we want at-least-once delivery), the right move is to upgrade the publish to be transactional with the DB commit, not to add server-side retries on this side.

---

## Data shapes on the wire — pointers

| What | Where |
|---|---|
| `weapons.detected` event payload | [docs/requirements/REPORT_GENERATION_STREAMS.md §2.3](../requirements/REPORT_GENERATION_STREAMS.md) |
| Upstream `weapon_analysis` block we receive | [docs/weapons/CONTRACT.md](CONTRACT.md) (the `image-weapons-compute` producer contract) |
| Qdrant payload fields written per frame | [docs/weapons/02_QDRANT.md](02_QDRANT.md) |
| DB columns | [docs/weapons/01_DATABASE.md](01_DATABASE.md) + [`src/db/models/embedding_request.py`](../../src/db/models/embedding_request.py) + [`src/db/models/evidence_embedding.py`](../../src/db/models/evidence_embedding.py) |
| Consumer enrichment loop | [docs/weapons/03_CONSUMER.md](03_CONSUMER.md) |

---

## "But I see an annotated image somewhere"

If you see one in the running system — for example in a generated PDF or in a UI preview — it was **rendered by report-generation from our event**, not produced here. Their service downloads the `image_url` from our payload (the plain frame) and draws the bboxes from `images[].detections[]` (the array we forward verbatim).

If you need a sample annotated frame for QA without going through the full PDF pipeline, the simplest path is a small standalone script that pulls one `weapons:detected` event and renders with Pillow/cv2. Not in this codebase by design.

---

## Diagnosing "weapons notifications are slow"

If someone reports the notification path is slow, **don't start by profiling this consumer.** Run the three-step decision tree in [PERFORMANCE_ANALYSIS_2026_05.md](PERFORMANCE_ANALYSIS_2026_05.md):

1. **Coverage check** — is `weapon_analysis` even reaching us in recent payloads? (Most likely cause of "no alerts".)
2. **Per-message latency** — measured baseline 2026-05-14: p50 = 31 ms, p99 = 54 ms. If your numbers are within 2× of those, the service is fine.
3. **Consumer-group lag** (`XINFO GROUPS embeddings:results`) — if > 1000 messages, latency lives in Redis queue depth, not our code.

That doc carries the SQL queries, the Redis cross-check script, and the May 2026 incident write-up that produced the baseline.

## Cross-references

- [docs/weapons/README.md](README.md) — feature overview + phase index
- [docs/weapons/PERFORMANCE_ANALYSIS_2026_05.md](PERFORMANCE_ANALYSIS_2026_05.md) — diagnostic recipe + measured latency baseline
- [docs/weapons/CONTRACT.md](CONTRACT.md) — upstream producer contract
- [docs/requirements/REPORT_GENERATION_STREAMS.md §2](../requirements/REPORT_GENERATION_STREAMS.md) — downstream contract
- [docs/new_arq_v2/03_BACKEND_SERVICE.md](../new_arq_v2/03_BACKEND_SERVICE.md) — backend service responsibilities, including report-event publishing
