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
    
    # Main Video Server API
    video_server_base_url: str = Field("http://localhost:8000", validation_alias="VIDEO_SERVER_BASE_URL")
    video_server_api_key: str = Field(..., validation_alias="VIDEO_SERVER_API_KEY")  # Must be DEV or ROOT role
    
    # Qdrant Vector Database
    qdrant_host: str = Field("localhost", validation_alias="QDRANT_HOST")
    qdrant_port: int = Field(6333, validation_alias="QDRANT_PORT")
    qdrant_api_key: Optional[str] = Field(None, validation_alias="QDRANT_API_KEY")
    qdrant_collection_name: str = Field("evidence_embeddings", validation_alias="QDRANT_COLLECTION_NAME")
    qdrant_vector_size: int = Field(512, validation_alias="QDRANT_VECTOR_SIZE")  # CLIP ViT-B-32 dimension
    
    # Redis Configuration (for scheduler)
    redis_host: str = Field("localhost", validation_alias="REDIS_HOST")
    redis_port: int = Field(6379, validation_alias="REDIS_PORT")
    redis_password: Optional[str] = Field(None, validation_alias="REDIS_PASSWORD")
    redis_database: int = Field(5, validation_alias="REDIS_DATABASE")  # Use DB 5 for embedding service
    
    # Scheduler Configuration (This is for the scheduler service to check for new evidence and image searches)
    scheduler_enabled: bool = Field(True, validation_alias="SCHEDULER_ENABLED")
    evidence_check_interval: int = Field(600, validation_alias="EVIDENCE_CHECK_INTERVAL")  # 10 minutes
    evidence_batch_size: int = Field(50, validation_alias="EVIDENCE_BATCH_SIZE")
    image_search_check_interval: int = Field(30, validation_alias="IMAGE_SEARCH_CHECK_INTERVAL")  # 30 seconds
    image_search_batch_size: int = Field(10, validation_alias="IMAGE_SEARCH_BATCH_SIZE")
    
    # Recalculation Configuration (This is for the scheduler service to recalculate image searches)
    recalculation_enabled: bool = Field(True, validation_alias="RECALCULATION_ENABLED")  # Disabled by default
    recalculation_interval: int = Field(3600, validation_alias="RECALCULATION_INTERVAL")  # 1 hour
    recalculation_hours_old: int = Field(2, validation_alias="RECALCULATION_HOURS_OLD")  # Only recalc if older than 2 hours
    recalculation_batch_size: int = Field(20, validation_alias="RECALCULATION_BATCH_SIZE")
    
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