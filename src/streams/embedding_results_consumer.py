"""Consumer for embeddings:results stream — receives pre-computed vectors from GPU service.

Stores vectors directly in Qdrant + DB in the consumer (no ARQ queue needed).
The operation is fast (~70ms) so there's no benefit to deferring it.

New ZIP flow (ETL integration):
- GPU sends image_name (filename inside ZIP) instead of image_url
- Backend downloads ZIP, extracts filtered images, uploads to storage service
- Permanent MinIO URLs stored in DB and Qdrant
"""

import asyncio
import logging
from datetime import datetime
from uuid import uuid4

import numpy as np

from ..application.helpers.weapon_report_events import (
    WEAPONS_DETECTED_EVENT_TYPE,
    build_weapons_detected_event,
)
from ..db.models.constants import EmbeddingRequestStatus
from ..db.models.evidence_embedding import EvidenceEmbeddingRecord
from ..db.repositories import EmbeddingRequestRepository
from ..domain.entities import ImageEmbedding
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session
from ..services.storage_uploader import StorageUploader
from ..services.zip_processor import ZipProcessor
from .consumer import StreamConsumer
from .producer import StreamProducer

logger = logging.getLogger(__name__)
settings = get_settings()

_event_loop: asyncio.AbstractEventLoop | None = None
_vector_repo = None  # QdrantVectorRepository, set at startup
_zip_processor = ZipProcessor()
_storage_uploader: StorageUploader | None = None
_stream_producer: StreamProducer | None = None


def set_results_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def set_vector_repo(repo):
    global _vector_repo
    _vector_repo = repo


def set_storage_uploader(uploader: StorageUploader):
    global _storage_uploader
    _storage_uploader = uploader


def set_stream_producer(producer: StreamProducer):
    """Inject the producer used to publish weapons.detected / image.blacklist_match
    events to the report-generation service.

    See docs/requirements/REPORT_GENERATION_STREAMS.md for the contract.
    """
    global _stream_producer
    _stream_producer = producer


def create_embedding_results_consumer() -> StreamConsumer:
    """Factory: creates a consumer for embeddings:results from the GPU service."""
    consumer = StreamConsumer(
        stream=settings.stream_embeddings_results,
        group=settings.stream_backend_group,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_password=settings.redis_password or None,
        redis_db=settings.redis_streams_db,
        block_ms=settings.stream_consumer_block_ms,
        batch_size=settings.stream_consumer_batch_size,
        reclaim_idle_ms=settings.stream_reclaim_idle_ms,
        dead_letter_max_retries=settings.stream_dead_letter_max_retries,
        concurrency=settings.stream_consumer_concurrency,
    )
    consumer.register_handler("embeddings.computed", _handle_embeddings_computed)
    consumer.register_handler("compute.error", _handle_compute_error)
    return consumer


def _handle_embeddings_computed(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_embeddings_result(payload, message_id),
        _event_loop,
    )
    future.result(timeout=300)


def _handle_compute_error(event_type: str, payload: dict, message_id: str):
    future = asyncio.run_coroutine_threadsafe(
        _process_compute_error(payload),
        _event_loop,
    )
    future.result(timeout=30)


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

    # Optional weapons enrichment — see docs/weapons/03_CONSUMER.md
    weapon_analysis = payload.get("weapon_analysis") or {}
    weapon_analyzed = bool(weapon_analysis)
    weapon_summary = weapon_analysis.get("summary") or {}
    detections_by_name: dict[str, list[dict]] = {
        img["image_name"]: (img.get("detections") or [])
        for img in (weapon_analysis.get("images") or [])
        if "image_name" in img
    }

    # Root-level weapons failure signal (producer pass-through path). Capturing
    # this lets us distinguish "attempted and failed" from "never attempted".
    # See docs/weapons/CONTRACT.md §5 (tolerated root-level extensions).
    weapon_error_block = payload.get("weapon_analysis_error") or {}
    weapon_error_message = (
        weapon_error_block.get("message") if isinstance(weapon_error_block, dict) else None
    )
    trace_event = payload.get("trace_event")

    if not evidence_id or not embeddings_data:
        logger.warning(f"Skipping result with missing data: evidence_id={evidence_id}")
        return

    # Dedup check
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if await repo.check_duplicate(evidence_id):
            logger.info(f"Skipping duplicate evidence {evidence_id}")
            return

    try:
        # 1. If ZIP flow: download ZIP, extract filtered images, upload to storage
        uploaded_urls: dict[str, str] = {}  # image_name → public_url

        if zip_url and _storage_uploader:
            image_names = [e["image_name"] for e in embeddings_data if "image_name" in e]
            if image_names:
                image_map = await _zip_processor.download_and_extract(zip_url, image_names)
                folder = f"embeddings/{camera_id}/{evidence_id}"
                for name, img_bytes in image_map.items():
                    public_url = await _storage_uploader.upload_image(
                        img_bytes, name, folder, user_id=user_id or "embedding-service"
                    )
                    if public_url:
                        uploaded_urls[name] = public_url

        # 2. Build Qdrant embeddings + DB records
        qdrant_embeddings: list[ImageEmbedding] = []
        db_records: list[EvidenceEmbeddingRecord] = []
        all_image_urls: list[str] = []
        # Per-image data for the weapons.detected report event. Only frames
        # with at least one detection end up here — clean frames are omitted
        # per docs/requirements/REPORT_GENERATION_STREAMS.md §2.3.
        report_images_with_detections: list[dict] = []

        for emb in embeddings_data:
            point_id = str(uuid4())
            vector = np.array(emb["vector"], dtype=np.float32)

            # Resolve image URL: uploaded URL (ZIP flow) or direct URL (legacy)
            image_name = emb.get("image_name", "")
            image_url = uploaded_urls.get(image_name, emb.get("image_url", ""))
            all_image_urls.append(image_url)

            # Per-image weapon enrichment — see docs/weapons/03_CONSUMER.md
            per_image_detections = detections_by_name.get(image_name, [])
            per_image_has_weapon = weapon_analyzed and len(per_image_detections) > 0
            per_image_classes = sorted(
                {d["class_name"] for d in per_image_detections if d.get("class_name")}
            )

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
                    weapon_detections=per_image_detections if weapon_analyzed else None,
                )
            )

            if per_image_has_weapon:
                report_images_with_detections.append(
                    {
                        "image_name": image_name,
                        "image_index": emb.get("image_index", 0),
                        "image_url": image_url,
                        "detections": per_image_detections,
                    }
                )

        # 3. Store in Qdrant (single bulk upsert)
        if _vector_repo and qdrant_embeddings:
            await _vector_repo.store_embeddings_batch(qdrant_embeddings)

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
                weapon_analyzed=weapon_analyzed,
                has_weapon=bool(weapon_summary.get("has_weapon", False)),
                weapon_classes=list(weapon_summary.get("classes_detected") or []),
                weapon_max_confidence=weapon_summary.get("max_confidence"),
                weapon_summary=weapon_summary or None,
                weapon_analysis_error=weapon_error_message,
            )

            # Link DB records to the request
            for record in db_records:
                record.request_id = request.id
            session.add_all(db_records)

            # Mark as EMBEDDED directly
            request.status = EmbeddingRequestStatus.EMBEDDED
            request.processing_completed_at = datetime.utcnow()

        logger.info(
            f"Stored {len(qdrant_embeddings)} vectors for evidence {evidence_id} "
            f"(input={payload.get('input_count')}, filtered={payload.get('filtered_count')}, "
            f"uploaded={len(uploaded_urls)}, "
            f"weapon_analyzed={weapon_analyzed}, "
            f"has_weapon={bool(weapon_summary.get('has_weapon', False))}, "
            f"detections={weapon_summary.get('total_detections', 0)}, "
            f"trace_event={trace_event!r}, "
            f"weapon_error={weapon_error_message!r})"
        )

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
                logger.error(
                    f"Failed to publish weapons.detected for evidence {evidence_id}: {pub_err}",
                    exc_info=True,
                )

    except Exception as e:
        logger.error(f"Failed to store embeddings for {evidence_id}: {e}", exc_info=True)
        # Create error row for tracking
        async with get_session() as session:
            repo = EmbeddingRequestRepository(session)
            if not await repo.check_duplicate(evidence_id):
                request = await repo.create_request(
                    evidence_id=evidence_id,
                    camera_id=camera_id,
                    image_urls=[],
                    stream_msg_id=message_id,
                    user_id=user_id,
                    device_id=device_id,
                    app_id=app_id,
                    infraction_code=infraction_code,
                )
                request.status = EmbeddingRequestStatus.ERROR
                request.error_message = str(e)[:500]


async def _process_compute_error(payload: dict):
    """Mark the request as ERROR when the compute service fails."""
    entity_id = payload.get("entity_id", "")
    entity_type = payload.get("entity_type", "")
    error = payload.get("error", "Unknown compute error")

    if entity_type != "evidence":
        return

    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        if not await repo.check_duplicate(entity_id):
            request = await repo.create_request(
                evidence_id=entity_id,
                camera_id="unknown",
                image_urls=[],
            )
            request.status = EmbeddingRequestStatus.ERROR
            request.error_message = f"Compute error: {error}"

    logger.error(f"Compute error for evidence {entity_id}: {error}")
