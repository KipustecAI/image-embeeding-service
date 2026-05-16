# Repository Structure

## Two Repos

```
lucam/
├── embedding-compute/           ← GPU service (separate repo)
└── image-embeeding-service/     ← CPU backend (this repo)
```

---

## embedding-compute/ (GPU)

Stateless. No DB, no Qdrant. Only CLIP + Redis Streams.

```
embedding-compute/
├── src/
│   ├── main.py                      # Entry point: load CLIP, start consumers, signal shutdown
│   ├── config.py                    # CLIP + Redis + diversity filter settings
│   ├── streams/
│   │   ├── consumer.py              # Generic StreamConsumer (shared pattern)
│   │   ├── producer.py              # Publish results to output streams
│   │   ├── evidence_handler.py      # evidence:embed → download + filter + CLIP → embeddings:results
│   │   └── search_handler.py        # evidence:search → download + CLIP → search:results
│   ├── services/
│   │   ├── clip_embedder.py         # CLIP ViT-B-32 via sentence-transformers
│   │   └── diversity_filter.py      # Bhattacharyya histogram filter
│   └── utils/
│       └── image_downloader.py      # Async image download, supports file:// URLs
├── Dockerfile                       # CUDA base image + torch + sentence-transformers
├── docker-compose.yml               # Just Redis (for local dev)
├── requirements.txt                 # torch, sentence-transformers, redis, opencv, Pillow, httpx
├── .env
├── .env.dev
└── CLAUDE.md
```

~4GB Docker image (dominated by torch + CUDA).

See [02_COMPUTE_SERVICE.md](02_COMPUTE_SERVICE.md) for runtime behavior.

---

## image-embeeding-service/ (CPU Backend — this repo)

Pipeline orchestration + storage + REST API. No CLIP, no torch. Lightweight.

```
image-embeeding-service/
├── src/
│   ├── main.py                              # FastAPI app + lifespan (port 8001)
│   ├── api/
│   │   ├── dependencies.py                  # UserContext from gateway headers (X-User-Id, X-User-Role)
│   │   └── v1/
│   │       ├── routers/
│   │       │   └── blacklist_image.py       # /api/v1/blacklist/image-entries surface (8 endpoints)
│   │       └── schemas/
│   │           └── blacklist_image.py       # Pydantic request/response models
│   ├── application/
│   │   ├── helpers/
│   │   │   ├── blacklist_match_events.py    # build_blacklist_match_event() — image:blacklist_match DTO
│   │   │   ├── category_serializer.py       # entities[] → category (db str + qdrant list)
│   │   │   ├── source_type_filter.py        # build_evidence_only_filter / build_blacklist_only_filter
│   │   │   ├── weapon_filters.py            # all/only/exclude/analyzed_clean → Qdrant filter
│   │   │   └── weapon_report_events.py      # build_weapons_detected_event() — weapons:detected DTO
│   │   └── use_cases/
│   │       └── manage_blacklist_image.py    # Blacklist CRUD business logic (multi-tenant + version bump)
│   ├── db/
│   │   ├── base.py                          # SQLAlchemy declarative base
│   │   ├── models/
│   │   │   ├── constants.py                 # Status enums (Embedding/Search/Blacklist*)
│   │   │   ├── embedding_request.py         # Evidence pipeline tracking + weapons + category fields
│   │   │   ├── evidence_embedding.py        # One row per vector in Qdrant + weapon_detections JSONB
│   │   │   ├── search_request.py            # Search lifecycle + qdrant_query_point_id
│   │   │   ├── search_match.py              # Per-match results
│   │   │   └── blacklist_image.py           # 3 tables: entries / references / embeddings
│   │   └── repositories/
│   │       ├── embedding_request_repo.py
│   │       ├── search_request_repo.py
│   │       └── blacklist_image_repo.py
│   ├── infrastructure/
│   │   ├── config.py                        # Pydantic BaseSettings — DB/Qdrant/Redis/streams/thresholds
│   │   ├── database.py                      # SQLAlchemy async engine + get_session()
│   │   ├── entity_taxonomy.py               # STOP-GAP YOLO/COCO-80 id→label map for categories
│   │   └── vector_db/
│   │       └── qdrant_repository.py         # Qdrant client + _EVIDENCE_PAYLOAD_INDICES list
│   ├── streams/
│   │   ├── consumer.py                      # Generic StreamConsumer (XREADGROUP + DLQ)
│   │   ├── producer.py                      # StreamProducer (XADD)
│   │   ├── embedding_results_consumer.py    # embeddings:results → Qdrant + DB + weapons publish + inline blacklist match
│   │   └── search_results_consumer.py       # search:results → purpose dispatch (search vs blacklist_embed) + compute.error routing
│   ├── services/
│   │   ├── blacklist_embed_service.py       # purpose=blacklist_embed → store Qdrant + DB + schedule reverse search
│   │   ├── blacklist_match_service.py       # publish_blacklist_match() — shared inline + reverse-search publisher
│   │   ├── blacklist_reverse_search.py      # APScheduler one-shot reverse search after new reference
│   │   ├── safety_nets.py                   # Stale recovery, recalculation, cleanup
│   │   ├── storage_uploader.py              # Upload ZIP-extracted frames to storage service
│   │   └── zip_processor.py                 # Download + extract evidence ZIPs from ETL
│   └── domain/
│       └── entities/
│           ├── embedding.py                 # ImageEmbedding domain entity
│           └── search_result.py             # SearchResult — Qdrant query result wrapper
├── alembic/
│   ├── env.py
│   └── versions/                            # 10 migrations: ETL fields, search matches, vector_data
│                                            #   removal, qdrant_query_point_id, weapon fields,
│                                            #   weapon_analysis_error, category, blacklist tables
├── scripts/
│   ├── test_local_pipeline.sh               # E2E test: embedding + search + mock modes
│   └── generate_similarity_report.py        # Heatmap visualization
├── docs/
│   ├── README.md                            # Wiki schema
│   ├── index.md                             # Wiki content catalog
│   ├── log.md                               # Chronological event log
│   ├── llmwiki.md                           # LLM wiki pattern (seed)
│   ├── API_REFERENCE.md                     # Canonical REST API reference
│   ├── BLACKLIST_API.md                     # Standalone blacklist consumer contract
│   ├── CURL_EXAMPLES.md                     # Curl cookbook
│   ├── new_arq_v2/                          # Current architecture (this directory)
│   ├── requirements/                        # Inter-team contracts (compute, report-gen)
│   ├── weapons/                             # Weapons-enrichment phase plans + producer CONTRACT
│   ├── image-blacklist/                     # Blacklist phase plans (00..07) + README index
│   ├── new_arq/                             # v1 architecture (raw, superseded)
│   └── legacy/                              # Pre-v2 docs (raw, reference only)
├── tests/
│   ├── test_db.py                           # DB model + repository tests (needs postgres)
│   ├── test_blacklist_image_repo.py         # Blacklist repository tests
│   ├── test_blacklist_image_schemas.py      # Blacklist Pydantic validation
│   ├── test_manage_blacklist_use_case.py    # Use-case scoping/ownership tests
│   ├── test_blacklist_match_events.py       # Event builder tests
│   ├── test_blacklist_reverse_search.py     # APScheduler reverse-search tests
│   ├── test_search_results_blacklist_dispatch.py  # purpose-dispatch tests
│   ├── test_source_type_filter.py           # Strict filter helper tests
│   ├── test_weapon_filters.py               # Weapons filter mode tests
│   ├── test_weapon_report_events.py         # Weapons event builder tests
│   ├── test_entity_taxonomy.py              # Entity-id resolver tests
│   ├── test_category_serializer.py          # entities → category serializer tests
│   └── test_integration.py                  # End-to-end (needs full stack)
├── docker-compose.yml                       # PostgreSQL + Redis + Qdrant
├── Dockerfile
├── Makefile                                 # install, run-api, run-worker, docker-up, migrate, test
├── requirements.txt
├── pyproject.toml                           # ruff + pytest config
├── .env                                     # Production defaults (remote hosts)
├── .env.dev                                 # Local dev overrides (localhost, no password)
├── .env.docker                              # Docker environment
├── .env.example                             # Documented template
└── CLAUDE.md
```

~200MB Docker image. **No torch, no CUDA, no sentence-transformers.**

### Qdrant Collections

Single collection for both evidence and blacklist vectors, discriminated by the `source_type` payload field. See [docs/image-blacklist/03_QDRANT.md](../image-blacklist/03_QDRANT.md) for the rationale.

| Collection | Purpose |
|------------|---------|
| `evidence_embeddings` | Evidence + blacklist vectors (512-dim CLIP ViT-B-32, cosine). Payload indices: `source_type`, `camera_id`, `evidence_id`, `user_id`, `device_id`, `app_id` (multi-tenant), `weapon_analyzed`, `has_weapon`, `weapon_classes` (weapons), `category` (entity-id keyword[]), `blacklist_entry_id` (blacklist scoping). Source of truth at [`src/infrastructure/vector_db/qdrant_repository.py`](../../src/infrastructure/vector_db/qdrant_repository.py) — `_EVIDENCE_PAYLOAD_INDICES`. |
| `search_queries` | Stored query vectors for GPU-free recalculation. One point per search, payload: `search_id`. |

### Database Tables

| Table | Purpose |
|-------|---------|
| `embedding_requests` | Tracks each evidence through embedding. Carries ETL fields (`user_id`, `device_id`, `app_id`, `infraction_code`), weapons fields (`weapon_analyzed`, `has_weapon`, `weapon_classes`, `weapon_max_confidence`, `weapon_summary`, `weapon_analysis_error`), and `category` (stringified entity ids). |
| `evidence_embeddings` | One row per vector stored in Qdrant (FK → embedding_requests). Optional `weapon_detections JSONB`. |
| `search_requests` | Tracks search lifecycle. `qdrant_query_point_id` links to stored query vector. |
| `search_matches` | Individual match results per search (FK → search_requests). |
| `blacklist_image_entries` | One row per blacklist profile (name, category, threshold override, version, status, active). |
| `blacklist_image_references` | One row per reference image attached to an entry (image_url, status, error_message). Unique `(entry_id, image_url)`. |
| `blacklist_image_embeddings` | Join row from reference → Qdrant point. Carries `qdrant_point_id` + `model_version`. |

### Legacy Code (present but not on the active path)

These files exist from the v1 monolith but are not wired into the current lifespan:

| File | Status |
|------|--------|
| `src/services/batch_trigger.py` | Not used — consumers store directly. |
| `src/workers/` | Legacy ARQ workers — replaced by APScheduler in main.py. |
| `src/infrastructure/scheduler/arq_scheduler.py` | Legacy ARQ scheduler — replaced by APScheduler. |
| `src/infrastructure/api/video_server_client.py` | Legacy HTTP client to the old Video Server API. |
| `src/infrastructure/embedding/clip_embedder.py` | Legacy CLIP wrapper — CLIP runs in the compute service now. |
| `src/services/diversity_filter.py` | Legacy — moved to compute service. |
| `src/streams/evidence_consumer.py` | Legacy — replaced by `embedding_results_consumer.py`. |
| `src/streams/search_consumer.py` | Legacy — replaced by `search_results_consumer.py`. |

**Note:** `src/application/` and `src/domain/` are **not** legacy anymore. `src/application/helpers/` and `src/application/use_cases/` carry active business logic for category/weapons/blacklist features; `src/domain/entities/` is referenced by the consumers and Qdrant repository.
