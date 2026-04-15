# Phase 3: Consumer Enrichment

One file touched: [src/streams/embedding_results_consumer.py](../../src/streams/embedding_results_consumer.py). The `_process_embeddings_result` function ([lines 89-216](../../src/streams/embedding_results_consumer.py#L89-L216)) gains a parsing block for `weapon_analysis` and a per-image injection step inside the existing loop.

## Contract

**Input (enriched payload, fields added to the existing contract):**
```json
{
  "weapon_analysis": {
    "images": [
      {
        "image_name": "20260410-100150_023389.jpg",
        "image_index": 0,
        "detections": [
          {"class_name": "handgun", "class_id": 0, "confidence": 0.873,
           "bbox": {"x1": 412, "y1": 188, "x2": 596, "y2": 402}}
        ]
      }
    ],
    "summary": {
      "images_analyzed": 9,
      "images_with_detections": 2,
      "total_detections": 3,
      "classes_detected": ["handgun", "knife"],
      "max_confidence": 0.873,
      "has_weapon": true
    }
  }
}
```

**Input (legacy / routing bypass):** `weapon_analysis` absent entirely. Every existing field is untouched.

**Output per Qdrant point (new payload keys):**
- `weapon_analyzed: bool` — same across all points of the same evidence
- `has_weapon: bool` — per-image (true iff THIS image had at least one detection)
- `weapon_classes: list[str]` — per-image (sorted deduplicated)

**Output per `embedding_requests` row (new columns):**
- `weapon_analyzed`, `has_weapon`, `weapon_classes`, `weapon_max_confidence`, `weapon_summary` — all evidence-level

**Output per `evidence_embeddings` row (new column):**
- `weapon_detections: list[dict] | None` — per-image bboxes (or `None` if not analyzed)

## Full enrichment logic

The new block sits at the very top of `_process_embeddings_result`, right after the payload fields are unpacked. It builds a lookup map indexed by `image_name`:

```python
async def _process_embeddings_result(payload: dict, message_id: str):
    """Receive pre-computed vectors → download ZIP → upload images → store in Qdrant + DB."""
    evidence_id = payload.get("evidence_id", "")
    camera_id = payload.get("camera_id", "")
    zip_url = payload.get("zip_url")
    user_id = payload.get("user_id")
    device_id = payload.get("device_id")
    app_id = payload.get("app_id")
    infraction_code = payload.get("infraction_code")
    embeddings_data = payload.get("embeddings", [])

    # === NEW: parse optional weapon_analysis enrichment ===
    weapon_analysis = payload.get("weapon_analysis") or {}
    weapon_analyzed = bool(weapon_analysis)
    weapon_summary = weapon_analysis.get("summary") or {}
    # image_name → list of detections (may be empty if analyzed but clean)
    detections_by_name: dict[str, list[dict]] = {
        img["image_name"]: img.get("detections", []) or []
        for img in weapon_analysis.get("images", [])
        if "image_name" in img
    }
    # === END NEW ===

    if not evidence_id or not embeddings_data:
        logger.warning(f"Skipping result with missing data: evidence_id={evidence_id}")
        return

    # ... existing dedup check unchanged ...
```

Inside the existing `for emb in embeddings_data` loop (currently at [line 132](../../src/streams/embedding_results_consumer.py#L132)), add per-image enrichment when building the `ImageEmbedding` and `EvidenceEmbeddingRecord`:

```python
        for emb in embeddings_data:
            point_id = str(uuid4())
            vector = np.array(emb["vector"], dtype=np.float32)

            image_name = emb.get("image_name", "")
            image_url = uploaded_urls.get(image_name, emb.get("image_url", ""))
            all_image_urls.append(image_url)

            # === NEW: per-image weapon fields ===
            per_image_detections = detections_by_name.get(image_name, [])
            per_image_has_weapon = weapon_analyzed and len(per_image_detections) > 0
            per_image_classes = sorted({
                d["class_name"] for d in per_image_detections if "class_name" in d
            })
            # === END NEW ===

            embedding = ImageEmbedding.from_evidence(
                evidence_id=evidence_id,
                vector=vector,
                image_url=image_url,
                camera_id=camera_id,
                additional_metadata={
                    "image_index": emb.get("image_index", 0),
                    "user_id": user_id,
                    "device_id": device_id,
                    "app_id": app_id,
                    # Weapons enrichment (Phase 3)
                    "weapon_analyzed": weapon_analyzed,
                    "has_weapon": per_image_has_weapon,
                    "weapon_classes": per_image_classes,
                },
            )
            embedding.id = point_id
            qdrant_embeddings.append(embedding)

            db_records.append(
                EvidenceEmbeddingRecord(
                    qdrant_point_id=point_id,
                    image_index=emb.get("image_index", 0),
                    image_url=image_url,
                    json_data=embedding.metadata,
                    # Weapons enrichment (Phase 3)
                    weapon_detections=per_image_detections if weapon_analyzed else None,
                )
            )
```

After the existing bulk Qdrant upsert, extend the `repo.create_request(...)` call to pass the evidence-level weapon summary:

```python
        # 4. Create DB request + embedding records in one transaction
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            request = await repo.create_request(
                evidence_id=evidence_id,
                camera_id=camera_id,
                image_urls=all_image_urls,
                stream_msg_id=message_id,
                user_id=user_id,
                device_id=device_id,
                app_id=app_id,
                infraction_code=infraction_code,
                # Weapons enrichment (Phase 3)
                weapon_analyzed=weapon_analyzed,
                has_weapon=bool(weapon_summary.get("has_weapon", False)),
                weapon_classes=list(weapon_summary.get("classes_detected", []) or []),
                weapon_max_confidence=weapon_summary.get("max_confidence"),
                weapon_summary=weapon_summary or None,
            )
            # ... rest of the transaction unchanged ...
```

The existing log line at [line 192-196](../../src/streams/embedding_results_consumer.py#L192-L196) gains a weapon suffix:

```python
        logger.info(
            f"Stored {len(qdrant_embeddings)} vectors for evidence {evidence_id} "
            f"(input={payload.get('input_count')}, filtered={payload.get('filtered_count')}, "
            f"uploaded={len(uploaded_urls)}, "
            f"weapon_analyzed={weapon_analyzed}, "
            f"has_weapon={bool(weapon_summary.get('has_weapon', False))}, "
            f"detections={weapon_summary.get('total_detections', 0)})"
        )
```

Ops can now grep for `weapon_analyzed=True` in logs to observe the rollout coverage.

## Edge cases

| Case | Behavior | Rationale |
|---|---|---|
| `weapon_analysis` absent | `weapon_analyzed=False`, all detection fields empty/None, exact legacy path | Backwards compatibility |
| `weapon_analysis={}` (empty dict) | `weapon_analyzed=False` — empty dict is falsy | Defensive: treat `{}` as "no enrichment" rather than "analyzed with nothing" |
| `weapon_analysis.images=[]` (analyzed no images) | `weapon_analyzed=True`, but every per-image lookup returns `[]` → `has_weapon=False` everywhere | Edge case when the weapons model rejects all frames |
| `weapon_analysis.images[N].image_name` not in `embeddings[]` | Silently skipped — that image isn't in our embedding set | compute-weapons may analyze more frames than compute-embedding kept after diversity filter |
| `weapon_analysis.summary` missing but `images[]` present | `weapon_summary={}` — all evidence-level flags default to false. Per-image data still stored. | Defensive reconstruction is tempting but dangerous: we'd duplicate logic that lives in the weapons service. Prefer `summary` as source of truth. |
| `detections[N].class_name` missing | Silently skipped in the `weapon_classes` set comprehension | Malformed detection, unclear what class was meant |
| `summary.classes_detected` is `None` | `weapon_classes` column gets `[]` — matches column default | `or []` normalization |
| Duplicate image_name in `weapon_analysis.images` | Last one wins in `detections_by_name` | Unlikely but possible if compute-weapons has a bug; explicit dedup would obscure the upstream bug |

## Error path untouched

[_process_compute_error](../../src/streams/embedding_results_consumer.py#L218-L238) handles `compute.error` events. Weapons failures should never reach this handler because compute-weapons runs **after** compute-embedding — if the weapons model crashes, we still want to store the embeddings. The compute-weapons service should either:
1. Silently drop the `weapon_analysis` block (falls through to our legacy path — safe), or
2. Publish its own error event on a different stream

Either way, our error path stays identical. No changes needed.

## What we do NOT do here

- **No schema validation.** If `weapon_analysis.summary.has_weapon` is a string `"true"` instead of a bool, `bool("true")` is `True`, which is fine. If it's `"false"`, `bool("false")` is still `True` — which is **wrong**. We're trusting the compute-weapons service to send booleans as booleans. If experience proves otherwise, add a `parse_bool` helper.
- **No partial replay.** If `weapon_analysis` changes shape upstream and we need to re-process old messages, that's a backfill job — out of scope (see README).
- **No per-class histograms in logs.** Tempting, but logs aren't the right place. If ops needs class distribution, that's a SQL query against `embedding_requests.weapon_classes`.

## Verification

See [05_DOCS_AND_VERIFICATION.md](05_DOCS_AND_VERIFICATION.md) for the end-to-end test plan. Minimum smoke test:

1. XADD a message WITH `weapon_analysis`
2. Assert `embedding_requests.has_weapon=true`
3. Assert `evidence_embeddings.weapon_detections` is non-null
4. Query Qdrant point by ID and assert payload has `has_weapon=true`
5. XADD a message WITHOUT `weapon_analysis`
6. Assert `embedding_requests.weapon_analyzed=false`
7. Assert `evidence_embeddings.weapon_detections IS NULL`
8. Assert Qdrant point has `weapon_analyzed=false, has_weapon=false, weapon_classes=[]`
