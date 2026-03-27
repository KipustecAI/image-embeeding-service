"""
Image Embedding Service Configuration
"""

from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Configuration settings for Image Embedding Service."""
    
    # Application Settings
    app_name: str = Field("Image Embedding Service", validation_alias="APP_NAME")
    environment: str = Field("development", validation_alias="ENVIRONMENT")
    debug: bool = Field(False, validation_alias="DEBUG")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    
    # Service Security
    service_api_key: str = Field(..., validation_alias="EMBEDDING_SERVICE_API_KEY")

    # Database
    database_url: str = Field(
        "postgresql+asyncpg://embed_user:embed_pass@localhost:5433/embedding_service",
        validation_alias="DATABASE_URL",
    )

    # Qdrant Vector Database
    qdrant_host: str = Field("localhost", validation_alias="QDRANT_HOST")
    qdrant_port: int = Field(6333, validation_alias="QDRANT_PORT")
    qdrant_api_key: Optional[str] = Field(None, validation_alias="QDRANT_API_KEY")
    qdrant_collection_name: str = Field("evidence_embeddings", validation_alias="QDRANT_COLLECTION_NAME")
    qdrant_vector_size: int = Field(512, validation_alias="QDRANT_VECTOR_SIZE")  # CLIP ViT-B-32 dimension
    
    # Redis Configuration
    redis_host: str = Field("localhost", validation_alias="REDIS_HOST")
    redis_port: int = Field(6379, validation_alias="REDIS_PORT")
    redis_password: Optional[str] = Field(None, validation_alias="REDIS_PASSWORD")
    redis_database: int = Field(5, validation_alias="REDIS_DATABASE")

    # Redis Streams
    redis_streams_db: int = Field(3, validation_alias="REDIS_STREAMS_DB")
    stream_evidence_embed: str = Field("evidence:embed", validation_alias="STREAM_EVIDENCE_EMBED")
    stream_evidence_search: str = Field("evidence:search", validation_alias="STREAM_EVIDENCE_SEARCH")
    stream_consumer_group: str = Field("embed-workers", validation_alias="STREAM_CONSUMER_GROUP")
    stream_search_group: str = Field("search-workers", validation_alias="STREAM_SEARCH_GROUP")
    stream_consumer_block_ms: int = Field(5000, validation_alias="STREAM_CONSUMER_BLOCK_MS")
    stream_consumer_batch_size: int = Field(10, validation_alias="STREAM_CONSUMER_BATCH_SIZE")
    stream_reclaim_idle_ms: int = Field(3_600_000, validation_alias="STREAM_RECLAIM_IDLE_MS")
    stream_dead_letter_max_retries: int = Field(3, validation_alias="STREAM_DEAD_LETTER_MAX_RETRIES")
    stream_consumer_concurrency: int = Field(1, validation_alias="STREAM_CONSUMER_CONCURRENCY")

    # Batch Trigger
    batch_trigger_size: int = Field(20, validation_alias="BATCH_TRIGGER_SIZE")
    batch_trigger_max_wait: float = Field(5.0, validation_alias="BATCH_TRIGGER_MAX_WAIT")
    batch_trigger_search_size: int = Field(10, validation_alias="BATCH_TRIGGER_SEARCH_SIZE")
    batch_trigger_search_wait: float = Field(3.0, validation_alias="BATCH_TRIGGER_SEARCH_WAIT")

    # Recalculation
    recalculation_enabled: bool = Field(True, validation_alias="RECALCULATION_ENABLED")
    recalculation_hours_old: int = Field(2, validation_alias="RECALCULATION_HOURS_OLD")
    recalculation_batch_size: int = Field(20, validation_alias="RECALCULATION_BATCH_SIZE")

    # Safety Nets
    stale_working_minutes: int = Field(10, validation_alias="STALE_WORKING_MINUTES")
    max_retries: int = Field(3, validation_alias="MAX_RETRIES")
    cleanup_days: int = Field(30, validation_alias="CLEANUP_DAYS")

    # Diversity Filter
    diversity_filter_threshold: float = Field(0.10, validation_alias="DIVERSITY_FILTER_THRESHOLD")
    diversity_filter_histogram_bins: int = Field(64, validation_alias="DIVERSITY_FILTER_HISTOGRAM_BINS")
    diversity_filter_compare_all: bool = Field(False, validation_alias="DIVERSITY_FILTER_COMPARE_ALL")
    diversity_filter_min_dimension: int = Field(50, validation_alias="DIVERSITY_FILTER_MIN_DIMENSION")
    diversity_filter_max_aspect_ratio: float = Field(5.0, validation_alias="DIVERSITY_FILTER_MAX_ASPECT_RATIO")
    diversity_filter_max_images: int = Field(10, validation_alias="DIVERSITY_FILTER_MAX_IMAGES")
    diversity_filter_min_images: int = Field(1, validation_alias="DIVERSITY_FILTER_MIN_IMAGES")
    
    # CLIP Model Configuration
    clip_model_name: str = Field("ViT-B-32", validation_alias="CLIP_MODEL_NAME")
    clip_device: str = Field("cpu", validation_alias="CLIP_DEVICE")  # "cpu" or "cuda"
    clip_batch_size: int = Field(32, validation_alias="CLIP_BATCH_SIZE")
    
    # Image Processing
    image_download_timeout: int = Field(30, validation_alias="IMAGE_DOWNLOAD_TIMEOUT")
    max_image_size: int = Field(10485760, validation_alias="MAX_IMAGE_SIZE")  # 10MB
    supported_formats: str = Field("jpg,jpeg,png,webp", validation_alias="SUPPORTED_FORMATS")
    
    # Search Configuration
    default_similarity_threshold: float = Field(0.75, validation_alias="DEFAULT_SIMILARITY_THRESHOLD")
    max_search_results: int = Field(100, validation_alias="MAX_SEARCH_RESULTS")
    
    # Worker Configuration
    worker_concurrency: int = Field(4, validation_alias="WORKER_CONCURRENCY")
    worker_max_retries: int = Field(3, validation_alias="WORKER_MAX_RETRIES")
    worker_retry_delay: int = Field(60, validation_alias="WORKER_RETRY_DELAY")
    
    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = ["development", "staging", "production"]
        if v not in allowed:
            raise ValueError(f"Environment must be one of: {allowed}")
        return v
    
    @field_validator("clip_device")
    @classmethod
    def validate_device(cls, v: str) -> str:
        allowed = ["cpu", "cuda"]
        if v not in allowed:
            raise ValueError(f"Device must be one of: {allowed}")
        return v
    
    @field_validator("supported_formats")
    @classmethod
    def parse_formats(cls, v: str) -> list[str]:
        """Parse comma-separated formats into list."""
        return [fmt.strip().lower() for fmt in v.split(",")]
    
    @field_validator("default_similarity_threshold")
    @classmethod
    def validate_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Similarity threshold must be between 0 and 1")
        return v
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore"
    }


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()