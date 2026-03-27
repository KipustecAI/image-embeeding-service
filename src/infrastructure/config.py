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
    # Publishing to GPU (input streams)
    stream_evidence_search: str = Field("evidence:search", validation_alias="STREAM_EVIDENCE_SEARCH")
    # Consuming from GPU (output streams)
    stream_embeddings_results: str = Field("embeddings:results", validation_alias="STREAM_EMBEDDINGS_RESULTS")
    stream_search_results: str = Field("search:results", validation_alias="STREAM_SEARCH_RESULTS")
    stream_backend_group: str = Field("backend-workers", validation_alias="STREAM_BACKEND_GROUP")
    stream_consumer_block_ms: int = Field(5000, validation_alias="STREAM_CONSUMER_BLOCK_MS")
    stream_consumer_batch_size: int = Field(10, validation_alias="STREAM_CONSUMER_BATCH_SIZE")
    stream_reclaim_idle_ms: int = Field(3_600_000, validation_alias="STREAM_RECLAIM_IDLE_MS")
    stream_dead_letter_max_retries: int = Field(3, validation_alias="STREAM_DEAD_LETTER_MAX_RETRIES")
    stream_consumer_concurrency: int = Field(1, validation_alias="STREAM_CONSUMER_CONCURRENCY")

    # Recalculation
    recalculation_enabled: bool = Field(True, validation_alias="RECALCULATION_ENABLED")
    recalculation_hours_old: int = Field(2, validation_alias="RECALCULATION_HOURS_OLD")
    recalculation_batch_size: int = Field(20, validation_alias="RECALCULATION_BATCH_SIZE")

    # Safety Nets
    stale_working_minutes: int = Field(10, validation_alias="STALE_WORKING_MINUTES")
    max_retries: int = Field(3, validation_alias="MAX_RETRIES")
    cleanup_days: int = Field(30, validation_alias="CLEANUP_DAYS")
    
    # Search Configuration
    default_similarity_threshold: float = Field(0.75, validation_alias="DEFAULT_SIMILARITY_THRESHOLD")
    max_search_results: int = Field(100, validation_alias="MAX_SEARCH_RESULTS")

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


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()