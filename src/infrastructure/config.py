"""
Image Embedding Service Configuration
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration settings for Image Embedding Service."""

    # Application Settings
    app_name: str = Field("Image Embedding Service", validation_alias="APP_NAME")
    environment: str = Field("development", validation_alias="ENVIRONMENT")
    debug: bool = Field(False, validation_alias="DEBUG")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")

    # Database
    database_url: str = Field(
        "postgresql+asyncpg://embed_user:embed_pass@localhost:5433/embedding_service",
        validation_alias="DATABASE_URL",
    )

    # Qdrant Vector Database
    qdrant_host: str = Field("localhost", validation_alias="QDRANT_HOST")
    qdrant_port: int = Field(6333, validation_alias="QDRANT_PORT")
    qdrant_api_key: str | None = Field(None, validation_alias="QDRANT_API_KEY")
    qdrant_collection_name: str = Field(
        "evidence_embeddings", validation_alias="QDRANT_COLLECTION_NAME"
    )
    qdrant_vector_size: int = Field(
        512, validation_alias="QDRANT_VECTOR_SIZE"
    )  # CLIP ViT-B-32 dimension

    # Redis Configuration
    redis_host: str = Field("localhost", validation_alias="REDIS_HOST")
    redis_port: int = Field(6379, validation_alias="REDIS_PORT")
    redis_password: str | None = Field(None, validation_alias="REDIS_PASSWORD")
    # Redis Streams
    redis_streams_db: int = Field(3, validation_alias="REDIS_STREAMS_DB")
    # Publishing to GPU (input streams)
    stream_evidence_search: str = Field(
        "evidence:search", validation_alias="STREAM_EVIDENCE_SEARCH"
    )
    # Publishing to report-generation (see docs/requirements/REPORT_GENERATION_STREAMS.md)
    stream_reports_weapons_detected: str = Field(
        "weapons:detected", validation_alias="STREAM_REPORTS_WEAPONS_DETECTED"
    )
    stream_reports_image_blacklist_match: str = Field(
        "image:blacklist_match", validation_alias="STREAM_REPORTS_IMAGE_BLACKLIST_MATCH"
    )
    # Consuming from GPU (output streams)
    stream_embeddings_results: str = Field(
        "embeddings:results", validation_alias="STREAM_EMBEDDINGS_RESULTS"
    )
    stream_search_results: str = Field("search:results", validation_alias="STREAM_SEARCH_RESULTS")
    stream_backend_group: str = Field("backend-workers", validation_alias="STREAM_BACKEND_GROUP")
    stream_consumer_block_ms: int = Field(5000, validation_alias="STREAM_CONSUMER_BLOCK_MS")
    stream_consumer_batch_size: int = Field(10, validation_alias="STREAM_CONSUMER_BATCH_SIZE")
    stream_reclaim_idle_ms: int = Field(3_600_000, validation_alias="STREAM_RECLAIM_IDLE_MS")
    stream_dead_letter_max_retries: int = Field(
        3, validation_alias="STREAM_DEAD_LETTER_MAX_RETRIES"
    )
    stream_consumer_concurrency: int = Field(1, validation_alias="STREAM_CONSUMER_CONCURRENCY")

    # Recalculation
    recalculation_enabled: bool = Field(True, validation_alias="RECALCULATION_ENABLED")
    recalculation_hours_old: int = Field(2, validation_alias="RECALCULATION_HOURS_OLD")
    recalculation_batch_size: int = Field(20, validation_alias="RECALCULATION_BATCH_SIZE")

    # Safety Nets
    stale_working_minutes: int = Field(10, validation_alias="STALE_WORKING_MINUTES")
    max_retries: int = Field(3, validation_alias="MAX_RETRIES")
    cleanup_days: int = Field(30, validation_alias="CLEANUP_DAYS")

    # Storage Service (for uploading filtered images)
    storage_service_url: str = Field(
        "http://storage-service:8006",
        validation_alias="STORAGE_SERVICE_URL",
    )

    # Search Configuration
    default_similarity_threshold: float = Field(
        0.75, validation_alias="DEFAULT_SIMILARITY_THRESHOLD"
    )
    max_search_results: int = Field(100, validation_alias="MAX_SEARCH_RESULTS")

    # Image Blacklist — Phase 04 (see docs/image-blacklist/04_EMBEDDING_FLOW.md)
    # Higher than the search default (0.75) because false positives here are
    # more harmful — a stray match would trigger a downstream alert.
    blacklist_match_threshold: float = Field(0.85, validation_alias="BLACKLIST_MATCH_THRESHOLD")
    blacklist_reverse_search_batch_size: int = Field(
        1000, validation_alias="BLACKLIST_REVERSE_SEARCH_BATCH_SIZE"
    )

    # ── lookia-dw publisher streams ──────────────────────────────────────
    # Wire-format authority: docs/requirements/LOOKIA_DW_STREAMS.md
    # All 7 streams publish flat-hash {event_type, payload} with PII hashing
    # on the blacklist_image_entry stream (name → name_hash).
    dw_stream_image_search_request: str = Field(
        "image_search_request:raw",
        validation_alias="DW_STREAM_IMAGE_SEARCH_REQUEST",
    )
    dw_stream_image_search_match: str = Field(
        "image_search_match:raw",
        validation_alias="DW_STREAM_IMAGE_SEARCH_MATCH",
    )
    dw_stream_blacklist_image_entry: str = Field(
        "blacklist_image_entry:raw",
        validation_alias="DW_STREAM_BLACKLIST_IMAGE_ENTRY",
    )
    dw_stream_blacklist_image_reference: str = Field(
        "blacklist_image_reference:raw",
        validation_alias="DW_STREAM_BLACKLIST_IMAGE_REFERENCE",
    )
    dw_stream_blacklist_image_embedding: str = Field(
        "blacklist_image_embedding:raw",
        validation_alias="DW_STREAM_BLACKLIST_IMAGE_EMBEDDING",
    )
    dw_stream_image_embedding_request: str = Field(
        "image_embedding_request:raw",
        validation_alias="DW_STREAM_IMAGE_EMBEDDING_REQUEST",
    )
    dw_stream_image_embedding: str = Field(
        "image_embedding:raw",
        validation_alias="DW_STREAM_IMAGE_EMBEDDING",
    )
    # MAXLEN per stream. Defaults from the renegotiated contract sizing —
    # observed prod volume drove the embed streams to 500k (was 100k).
    # Bump `DW_MAXLEN_IMAGE_EMBEDDING` to 2_000_000 before a backfill push.
    dw_maxlen_image_search_request: int = Field(
        10_000, validation_alias="DW_MAXLEN_IMAGE_SEARCH_REQUEST"
    )
    dw_maxlen_image_search_match: int = Field(
        10_000, validation_alias="DW_MAXLEN_IMAGE_SEARCH_MATCH"
    )
    dw_maxlen_blacklist_image_entry: int = Field(
        5_000, validation_alias="DW_MAXLEN_BLACKLIST_IMAGE_ENTRY"
    )
    dw_maxlen_blacklist_image_reference: int = Field(
        10_000, validation_alias="DW_MAXLEN_BLACKLIST_IMAGE_REFERENCE"
    )
    dw_maxlen_blacklist_image_embedding: int = Field(
        10_000, validation_alias="DW_MAXLEN_BLACKLIST_IMAGE_EMBEDDING"
    )
    dw_maxlen_image_embedding_request: int = Field(
        500_000, validation_alias="DW_MAXLEN_IMAGE_EMBEDDING_REQUEST"
    )
    dw_maxlen_image_embedding: int = Field(500_000, validation_alias="DW_MAXLEN_IMAGE_EMBEDDING")

    # ── On-demand image-index (ADDITIVE, ISOLATED, gated-OFF) ────────────────
    # Full spec: docs/image-index/00_DESIGN.md §2. Feature stays dark until this
    # flag flips True after the Phase 5 live smoke. All Redis on redis_streams_db (DB 3).
    image_index_enabled: bool = Field(False, validation_alias="IMAGE_INDEX_ENABLED")

    # Streams (locked / playbook defaults)
    stream_image_index_submit: str = Field(
        "image:index:submit", validation_alias="STREAM_IMAGE_INDEX_SUBMIT"
    )  # coordinator → us
    stream_image_index: str = Field(
        "image:index", validation_alias="STREAM_IMAGE_INDEX"
    )  # us → compute (dispatch)
    stream_image_index_results: str = Field(
        "image:index:results", validation_alias="STREAM_IMAGE_INDEX_RESULTS"
    )  # compute → us
    stream_image_batch_raw: str = Field(
        "image_batch:raw", validation_alias="STREAM_IMAGE_BATCH_RAW"
    )  # us → coordinator (lifecycle)

    # Dedicated consumer groups (isolated from the live backend-workers group)
    image_index_submit_group: str = Field(
        "image-index-submit", validation_alias="IMAGE_INDEX_SUBMIT_GROUP"
    )
    image_index_results_group: str = Field(
        "image-index-results", validation_alias="IMAGE_INDEX_RESULTS_GROUP"
    )

    # Dedicated Qdrant collection (own ensure/latch; NOT in the live initialize())
    qdrant_collection_image_index: str = Field(
        "image_index_embeddings", validation_alias="QDRANT_COLLECTION_IMAGE_INDEX"
    )

    # Knobs
    image_index_n_cap: int = Field(
        100, validation_alias="IMAGE_INDEX_N_CAP"
    )  # N_CAP (companion §1)
    image_index_batch_maxlen: int = Field(
        20_000, validation_alias="IMAGE_INDEX_BATCH_MAXLEN"
    )  # image_batch:raw trim
    image_index_max_compute_seconds: int = Field(
        900, validation_alias="IMAGE_INDEX_MAX_COMPUTE_SECONDS"
    )  # reaper cutoff
    image_index_reaper_interval_seconds: int = Field(
        60, validation_alias="IMAGE_INDEX_REAPER_INTERVAL_SECONDS"
    )  # reaper cadence

    # ── Image-index SEARCH + blacklist cross-ref (ADDITIVE, ISOLATED, gated-OFF) ──
    # Full spec: docs/image-index/02_SEARCH_DESIGN.md §2. AND-gated with
    # image_index_enabled at wiring time (the deps only wire inside that block).
    # Threshold reuses the existing blacklist_match_threshold (0.85).
    # image_index_search_enabled defaults TRUE: search-by-image (Cap A) + the
    # blacklist cross-reference REST (Cap B) are prod-standard, not an opt-in
    # toggle — no env var needed. The auto-hook below stays default-False until
    # report-generation signs off on the additive image:blacklist_match fields.
    image_index_search_enabled: bool = Field(  # Capability A (async search-by-image)
        True, validation_alias="IMAGE_INDEX_SEARCH_ENABLED"
    )
    image_index_blacklist_xref_enabled: bool = Field(  # Capability B REST endpoint (v1)
        False, validation_alias="IMAGE_INDEX_BLACKLIST_XREF_ENABLED"
    )
    image_index_blacklist_autocheck_enabled: bool = Field(  # Capability B auto-hook (v1.1)
        False, validation_alias="IMAGE_INDEX_BLACKLIST_AUTOCHECK_ENABLED"
    )
    image_index_xref_limit: int = Field(  # per-search top_k
        50, validation_alias="IMAGE_INDEX_XREF_LIMIT"
    )
    image_index_external_ids_cap: int = Field(  # MatchAny bound (A + B)
        200, validation_alias="IMAGE_INDEX_EXTERNAL_IDS_CAP"
    )

    @property
    def image_index_collection(self) -> str:
        """Read-only alias for `qdrant_collection_image_index` (§2 canonical name)."""
        return self.qdrant_collection_image_index

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = ["development", "staging", "production"]
        if v not in allowed:
            raise ValueError(f"Environment must be one of: {allowed}")
        return v

    @field_validator("default_similarity_threshold")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Similarity threshold must be between 0 and 1")
        return v

    model_config = {
        "env_file": (".env", ".env.dev"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
