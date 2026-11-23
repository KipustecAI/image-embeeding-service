# Helmet enrichment — consumer contract

> Backend side (phase 1) built 2026-11-23, not deployed. The producer contract is a **proposal** in
> `compute-image-helmets/docs/CONTRATO_EMBEDDINGS_ENRIQUECIDO.md`; section numbers below (§) refer to it.

## Flow

`image-embedding-compute` → `embeddings:results:helmet_analysis` → `compute-image-helmets` →
`embeddings:results` (optionally through weapons) → this service. We keep consuming **only**
`embeddings:results`, event `embeddings.computed`, one message per evidence. Dedup by `evidence_id` is unchanged.

## What we read and store

All columns are on `embedding_requests`, nullable, migration `c5e9a2d7f1b3`.

| Column | Source |
|---|---|
| `helmet_analyzed` | `true` if `helmet_analysis` is a non-empty object; NULL on rows older than the feature and on ERROR rows |
| `helmet_verdict` | `summary.verdict` (`ALARMA`, `REVISAR`, `CON_CASCO`, `SIN_EVIDENCIA`; not validated) |
| `helmet_alarm` | `summary.alarm` |
| `helmet_frames_sin_casco` | `summary.frames_sin_casco` |
| `helmet_max_sin_casco` | `summary.max_sin_casco` (NULL when there are no detections) |
| `helmet_analysis` | the full block |
| `helmet_analysis_error` | `helmet_analysis_error.message`, or the value itself if it is a string (§6.3) |

| `helmet_analyzed` | `helmet_analysis_error` | Meaning |
|---|---|---|
| false | NULL | Not attempted |
| true | NULL | Analyzed: read `helmet_verdict` |
| false | set | Attempted and failed; vectors are stored as usual |

Parser: [`src/application/helpers/helmet_analysis.py`](../../src/application/helpers/helmet_analysis.py).
It never raises: summary values of the wrong type become NULL.

To draw a box on a frame, join `helmet_analysis.images[].zip_entry` with `evidence_embeddings.zip_entry`.
Never join on `image_index`. Crops without a vector exist only in the JSONB.

## Not done

Search filter `helmet_filter`, report event, Qdrant flags, DW fields. DW fields need negotiation with lookia-dw.

## Open points with the producers

1. `helmet_analysis_error` is an object in §4.2 and a string in §6.3. We accept both; please fix one.
2. A message with empty `embeddings[]` is skipped here, so its `helmet_analysis` is lost. Confirm the
   producer never sends a helmet request without at least one vector.
3. If storage fails at ingest, the ERROR row does not carry the helmet fields (same as weapons).
4. When weapons runs after helmets, `trace_event` is overwritten by weapons. We only log it.
5. Deploy order: this migration and code first, then producer phases 2 and 3.
