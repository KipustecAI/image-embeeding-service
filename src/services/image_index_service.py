"""Service layer for the on-demand image-index feature.

Home of the SINGLE shared terminal-status helper (with the accounted-guard) and
the lifecycle payload builders. This is the one place the terminal rule and the
4-key count vocabulary are defined; the submit consumer, results consumer, and
reaper all route through here so the three surfaces can never drift
(docs/image-index/00_DESIGN.md §4 / §8).

Phase-1 scope: `terminal_status`, `counts_from_batch`, and the three payload
builders are IMPLEMENTED and unit-tested (pure). Phase-2 wired submit/create.
Phase-3 (this) fills `land_computed` + `mark_error_from_compute_error` against
the FROZEN compute wire (docs/requirements/IMAGE_INDEX_COMPUTE.md, 2026-07-22).

⚠️ COMPUTE-ERROR SHAPE DIVERGENCE — read before copying the live consumers.
`mark_error_from_compute_error` reads ``payload["batch_id"]`` +
``payload["error_message"]`` — OUR compute contract's `compute.error` shape
(companion §3). This is NOT the ``entity_id`` / ``entity_type`` shape the LIVE
`embedding_results_consumer` / `search_results_consumer` use. A maintainer who
copies a live consumer and reads ``payload["entity_id"]`` here would silently
no-op every batch-level compute error and hang the no-HTTP coordinator. The
dedicated stream + dedicated group make this collision-free; the divergence is
intentional and load-bearing.
"""

import base64
import logging
from datetime import datetime
from uuid import UUID

import numpy as np

from ..db.models.constants import ImageIndexBatchStatus, ImageIndexResultStatus
from ..db.models.image_index import ImageIndexBatch
from ..db.repositories.image_index_repo import ImageIndexRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session

logger = logging.getLogger(__name__)

# The one count vocabulary across DB columns / lifecycle / REST.
COUNT_KEYS = ("submitted", "embedded", "filtered", "failed")

# Compute wire constants (docs/requirements/IMAGE_INDEX_COMPUTE.md §2, FROZEN).
# vector_encoding is a REAL field on every embedded result: assert it and
# dead-letter an unknown encoding rather than silently misparsing the buffer.
VECTOR_ENCODING_F32LE_B64 = "f32le_b64"
# numpy dtype of the raw buffer: 512 × float32 LITTLE-ENDIAN.
_VECTOR_DTYPE = "<f4"
EMBEDDING_DIM = 512


class ImageIndexService:
    """Terminal rule + lifecycle payloads + (Phase 2/3) orchestration."""

    # ── The single shared terminal helper (pure, load-bearing) ────────────

    @staticmethod
    def terminal_status(counts: dict[str, int]) -> str | None:
        """Derive the terminal batch status from ABSOLUTE counts, or None.

        Contract (00_DESIGN §4/§8):
          1) ``counts`` MUST already be recomputed absolutely (GROUP BY),
             never incremented — this helper only reads them.
          2) ACCOUNTED-GUARD: if embedded + filtered + failed < submitted,
             the batch is NOT fully accounted → return ``None`` (stay
             'computing'; the reaper is the backstop). ``filtered`` is a clean
             disposition and counts toward accounting, never a downgrade.
          3) else 'completed' iff failed == 0 else 'completed_with_errors'.
          4) 'error' is NEVER returned here — it is only set on a batch-level
             compute.error / reaper timeout, not derivable from item counts.
        """
        submitted = int(counts.get("submitted", 0))
        embedded = int(counts.get("embedded", 0))
        filtered = int(counts.get("filtered", 0))
        failed = int(counts.get("failed", 0))

        accounted = embedded + filtered + failed
        if accounted < submitted:
            # Unaccounted — a result is still outstanding (or was dropped).
            return None
        if failed == 0:
            return ImageIndexBatchStatus.COMPLETED
        return ImageIndexBatchStatus.COMPLETED_WITH_ERRORS

    # ── Count + payload builders (pure) ───────────────────────────────────

    @staticmethod
    def counts_from_batch(batch: "ImageIndexBatch") -> dict[str, int]:
        """Read the 4-key folded counts off the denormalized batch columns."""
        return {
            "submitted": int(batch.submitted_count or 0),
            "embedded": int(batch.embedded_count or 0),
            "filtered": int(batch.filtered_count or 0),
            "failed": int(batch.failed_count or 0),
        }

    @staticmethod
    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None else None

    @classmethod
    def _base_lifecycle_payload(cls, batch: "ImageIndexBatch") -> dict:
        """The common lifecycle envelope (00_DESIGN §6.2)."""
        return {
            "batch_id": str(batch.id),
            "client_batch_ref": batch.client_batch_ref,
            "external_id": batch.external_id,
            "user_id": batch.user_id,
            "status": batch.status,
            "counts": cls.counts_from_batch(batch),
            "source_ref": batch.source_ref,
            "created_at": cls._iso(batch.created_at),
            "completed_at": cls._iso(batch.completed_at),
        }

    @classmethod
    def build_batch_created_payload(cls, batch: "ImageIndexBatch") -> dict:
        """`image_batch.created` — fresh mint, right after dispatch (status 'computing')."""
        return cls._base_lifecycle_payload(batch)

    @classmethod
    def build_batch_completed_payload(cls, batch: "ImageIndexBatch") -> dict:
        """`image_batch.completed` — terminal; completed | completed_with_errors."""
        return cls._base_lifecycle_payload(batch)

    @classmethod
    def build_batch_failed_payload(cls, batch: "ImageIndexBatch") -> dict:
        """`image_batch.failed` — terminal, batch-level only. Carries error_message."""
        payload = cls._base_lifecycle_payload(batch)
        payload["error_message"] = batch.error_message
        return payload

    # ── Submit-intake validation (pure; TIER-2 guards) ────────────────────

    ERROR_MESSAGE_MAX = 500

    @staticmethod
    def _is_blank(value) -> bool:
        """True for None, non-str, or whitespace-only strings."""
        return value is None or not (isinstance(value, str) and value.strip())

    @classmethod
    def _validate_submit(cls, payload: dict, *, n_cap: int) -> str | None:
        """TIER-2 guards (bindable-but-rejected). Returns an error string or None.

        Assumes TIER-1 already passed (payload is a dict; user_id +
        client_batch_ref present). These are the failures that DO get an ERROR
        batch + `image_batch.failed` — never a silent drop (00_DESIGN §5.1/§6.1):
        missing external_id, empty items, over N_CAP, missing per-item item_id,
        non-http(s) image_url.
        """
        if cls._is_blank(payload.get("external_id")):
            return "missing external_id"
        items = payload.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return "batch has no items"
        if len(items) > n_cap:
            return f"batch exceeds N_CAP={n_cap} (got {len(items)})"
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                return f"item {i} is not an object"
            if cls._is_blank(item.get("item_id")):
                return f"item {i} missing item_id"
            url = item.get("image_url")
            if cls._is_blank(url) or not str(url).strip().lower().startswith(
                ("http://", "https://")
            ):
                return f"item {i} has invalid image_url"
        return None

    # ── Orchestration (Phase 2) ───────────────────────────────────────────

    async def submit_batch_created(self, payload: dict):
        """Atomic idempotent mint from a submit payload → ``(batch, created, error)``.

        Runs the TIER-2 guards first (no DB touched on rejection), then
        ``create_or_get_batch`` (INSERT ... ON CONFLICT (client_batch_ref) DO
        NOTHING RETURNING id). ``created`` derives from the ATOMIC outcome, so
        two concurrent redeliveries yield exactly one batch and the consumer
        dispatches exactly once. The batch is minted at status ``pending``; the
        consumer flips it to ``computing`` only after a successful dispatch.

        Returns a DETACHED snapshot (expunged) safe to read after the session
        closes. On a TIER-2 rejection returns ``(None, False, error)``.
        """
        settings = get_settings()
        error = self._validate_submit(payload, n_cap=settings.image_index_n_cap)
        if error is not None:
            return None, False, error

        items = payload["items"]
        async with get_session() as session:
            repo = ImageIndexRepository(session)
            batch, created = await repo.create_or_get_batch(
                user_id=payload["user_id"],
                client_batch_ref=payload["client_batch_ref"],
                external_id=payload.get("external_id"),
                submitted_count=len(items),
                source_ref=payload.get("source_ref"),
                batch_metadata=payload.get("metadata"),
                status=ImageIndexBatchStatus.PENDING,
            )
            await session.flush()
            session.expunge(batch)
        return batch, created, None

    async def create_error_batch(self, payload: dict, error_message: str):
        """Persist an ERROR batch for a bindable-but-rejected submit (idempotent).

        Mints (or re-binds) the batch keyed on ``client_batch_ref`` at status
        ``error`` with ``error_message`` set — the no-HTTP coordinator MUST get
        a loud terminal, never a silent drop. An over-cap batch stores
        ``submitted_count = N`` with zero result rows; the reconciliation
        invariant exempts ``status=='error'`` batches (00_DESIGN §8, N2).
        Returns a detached snapshot for the ``image_batch.failed`` payload.
        """
        items = payload.get("items")
        submitted = len(items) if isinstance(items, list) else 0
        async with get_session() as session:
            repo = ImageIndexRepository(session)
            batch, _created = await repo.create_or_get_batch(
                user_id=payload.get("user_id"),
                client_batch_ref=payload.get("client_batch_ref"),
                external_id=payload.get("external_id"),
                submitted_count=submitted,
                source_ref=payload.get("source_ref"),
                batch_metadata=payload.get("metadata"),
                status=ImageIndexBatchStatus.ERROR,
            )
            # Idempotent: ensure error status + message even when re-binding an
            # existing row (rejection is deterministic — the same
            # client_batch_ref always fails the same guard).
            batch.status = ImageIndexBatchStatus.ERROR
            batch.error_message = (error_message or "")[: self.ERROR_MESSAGE_MAX]
            batch.completed_at = datetime.utcnow()
            await session.flush()
            session.expunge(batch)
        return batch

    async def mark_computing(self, batch_id) -> None:
        """Flip a freshly-dispatched batch to ``computing`` (single actor).

        Called by the submit consumer immediately after a successful dispatch,
        so ``computing`` always means "dispatched" and the reaper predicate
        (status IN (pending, computing)) is meaningful (00_DESIGN §5.1, M8).
        """
        async with get_session() as session:
            repo = ImageIndexRepository(session)
            await repo.set_status(batch_id, ImageIndexBatchStatus.COMPUTING)

    @staticmethod
    def _decode_vector_b64(vector_b64, *, batch_uuid, item_index) -> list[float]:
        """Decode a base64 f32le buffer → list[float] for Qdrant.

        ``np.frombuffer(base64.b64decode(v), dtype="<f4").tolist()`` — the
        FROZEN decode (companion §2). Bit-exact float32; no re-normalization.

        Contract-boundary validation: a null buffer, a non-multiple-of-4 buffer,
        or a wrong-dimension vector is a **dead-letter** (raise → message unacked
        → PEL/DLQ), surfaced HERE with a labeled ``ValueError`` naming the batch +
        item, rather than opaquely later at the Qdrant upsert or as a bare numpy
        error. Safe either way (a bad vector never lands searchable), but this
        makes the on-call failure legible.
        """
        if vector_b64 is None:
            raise ValueError(
                f"land_computed: embedded item has null vector_b64 "
                f"(batch={batch_uuid} item_index={item_index}) — dead-letter"
            )
        raw = base64.b64decode(vector_b64)
        if len(raw) % 4 != 0:
            raise ValueError(
                f"land_computed: vector_b64 buffer is not a whole number of "
                f"float32 ({len(raw)} bytes; batch={batch_uuid} "
                f"item_index={item_index}) — dead-letter"
            )
        vector = np.frombuffer(raw, dtype=_VECTOR_DTYPE).tolist()
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"land_computed: decoded vector has dim {len(vector)}, expected "
                f"{EMBEDDING_DIM} (batch={batch_uuid} item_index={item_index}) "
                f"— dead-letter"
            )
        return vector

    @staticmethod
    def _coerce_batch_uuid(batch_id_raw) -> UUID | None:
        """Parse the wire ``batch_id`` (a bare uuid string) → UUID, or None."""
        try:
            return UUID(str(batch_id_raw))
        except (ValueError, TypeError, AttributeError):
            return None

    async def land_computed(self, session, payload: dict, *, vector_repo):
        """Idempotently land an ``image.index.computed`` results payload.

        The results consumer owns the ``session`` (opened + committed around this
        call); we never open or commit one here — we mutate + flush and RETURN the
        ``image_batch.completed`` payload for the consumer to publish AFTER commit.

        Steps (00_DESIGN §5.2, invariants §8 #2/#3/#4):
          1. For each ``results[]`` item → ``upsert_result`` by (batch_id,
             item_index) — a redelivery overwrites the SAME row in place.
          2. ``status=="embedded"`` → ASSERT ``vector_encoding=="f32le_b64"``
             (unknown encoding RAISES → dead-letter, never a silent misparse),
             decode ``vector_b64``, and collect into ONE batched
             ``vector_repo.upsert_items(...)`` with the deterministic point-id.
             Failures / ``no_result`` store the disposition + error_message, no
             vector. The reference row stores the same deterministic
             ``qdrant_point_id`` (recomputed, no Qdrant round-trip). ``filtered``
             never fires in v1 (dedup disabled).
          3. Recompute counts ABSOLUTELY (GROUP BY) — never ``+= n``.
          4. Terminal via the shared ``terminal_status``: None → leave
             'computing' + log-loud the shortfall (the reaper is the backstop);
             else set completed | completed_with_errors.
          5. Return the completed payload (or None: a never-minted batch_id, or a
             not-yet-accounted batch → ACK no-op, no lifecycle publish).
        """
        batch_uuid = self._coerce_batch_uuid(payload.get("batch_id"))
        if batch_uuid is None:
            logger.error(
                "land_computed: unparseable batch_id %r — ACK no-op",
                payload.get("batch_id"),
            )
            return None

        # A batch we never minted (results leg never mints) → ACK no-op.
        batch = await session.get(ImageIndexBatch, batch_uuid)
        if batch is None:
            logger.warning(
                "land_computed: batch_id %s never minted — ACK no-op", batch_uuid
            )
            return None

        from ..infrastructure.vector_db.image_index_vector_repository import (
            image_index_point_id,
        )

        repo = ImageIndexRepository(session)
        results = payload.get("results") or []
        points: list[dict] = []

        # ── 1/2: per-item upsert + collect embedded vectors ──────────────────
        for item in results:
            item_index = int(item["item_index"])
            item_ref = item.get("item_id") or ""
            status = item.get("status")
            source_url = item.get("source_url")  # not in the v1 results wire → None
            qdrant_point_id = None

            if status == ImageIndexResultStatus.EMBEDDED:
                encoding = item.get("vector_encoding")
                if encoding != VECTOR_ENCODING_F32LE_B64:
                    # DEAD-LETTER: raising leaves the message unacked → PEL/DLQ.
                    # Never silently misparse an unknown encoding.
                    raise ValueError(
                        f"land_computed: unknown vector_encoding {encoding!r} "
                        f"(batch={batch_uuid} item_index={item_index}); expected "
                        f"{VECTOR_ENCODING_F32LE_B64!r} — dead-letter"
                    )
                vector = self._decode_vector_b64(
                    item.get("vector_b64"),
                    batch_uuid=batch_uuid,
                    item_index=item_index,
                )
                # Deterministic point-id — recomputed, no Qdrant round-trip; the
                # reference row stores the SAME value (redelivery overwrites).
                qdrant_point_id = image_index_point_id(str(batch_uuid), item_index)
                points.append(
                    {
                        "batch_id": str(batch_uuid),
                        "item_index": item_index,
                        "vector": vector,
                        "user_id": batch.user_id,
                        "external_id": batch.external_id,
                        "item_ref": item_ref,
                        "source_url": source_url,
                    }
                )

            await repo.upsert_result(
                batch_id=batch_uuid,
                item_index=item_index,
                item_ref=item_ref,
                status=status,
                source_url=source_url,
                qdrant_point_id=qdrant_point_id,
                duplicate_of_index=item.get("duplicate_of_index"),
                error_message=item.get("error_message"),
            )

        # ── Batched Qdrant upsert (ONE call) — before we terminalize ─────────
        # A Qdrant failure RAISES so the message is NOT acked (PEL → XCLAIM
        # retry); the re-land is idempotent (deterministic point-ids + upsert).
        if points:
            ok = await vector_repo.upsert_items(points)
            if not ok:
                raise RuntimeError(
                    f"land_computed: Qdrant upsert_items failed for batch "
                    f"{batch_uuid} ({len(points)} points) — not acking"
                )

        # ── 3: absolute recompute (GROUP BY, never += n) ─────────────────────
        counts = await repo.recompute_counts(batch_uuid)
        # Mirror the freshly-recomputed counters onto the ORM object so the
        # lifecycle payload is authoritative (recompute_counts also writes them
        # in real code; this keeps the payload correct + self-contained).
        batch.submitted_count = counts.get("submitted", 0)
        batch.embedded_count = counts.get("embedded", 0)
        batch.filtered_count = counts.get("filtered", 0)
        batch.failed_count = counts.get("failed", 0)

        # ── 4: terminal-from-counts via the single shared helper ─────────────
        terminal = self.terminal_status(counts)
        if terminal is None:
            # Unaccounted — a result is still outstanding. Do NOT terminalize;
            # log LOUD (invariant §8 #3) and let the reaper be the backstop.
            logger.warning(
                "land_computed shortfall: batch %s accounted "
                "embedded=%d+filtered=%d+failed=%d < submitted=%d — staying "
                "'computing' (reaper backstop)",
                batch_uuid,
                counts.get("embedded", 0),
                counts.get("filtered", 0),
                counts.get("failed", 0),
                counts.get("submitted", 0),
            )
            return None

        # Terminalize (ORM mutation + flush idiom, matching create_error_batch /
        # mark_failed). The consumer commits; we only build the payload.
        now = datetime.utcnow()
        batch.status = terminal
        batch.completed_at = now
        batch.updated_at = now
        return self.build_batch_completed_payload(batch)

    async def mark_error_from_compute_error(self, session, payload: dict):
        """Terminalize a batch as 'error' from a ``compute.error`` payload.

        Reads ``payload["batch_id"]`` + ``payload["error_message"]`` — OUR
        contract shape (companion §3), NOT the live entity_id/entity_type shape
        (see the module docstring). Last-writer-wins: even an already-terminal
        batch is overwritten to 'error'. The consumer owns + commits the session;
        we mutate + flush and RETURN the ``image_batch.failed`` payload.

        A never-minted / unparseable ``batch_id`` → return None (ACK no-op).
        """
        batch_uuid = self._coerce_batch_uuid(payload.get("batch_id"))
        if batch_uuid is None:
            logger.error(
                "mark_error_from_compute_error: unparseable batch_id %r — ACK no-op",
                payload.get("batch_id"),
            )
            return None

        batch = await session.get(ImageIndexBatch, batch_uuid)
        if batch is None:
            logger.warning(
                "mark_error_from_compute_error: batch_id %s never minted — ACK no-op",
                batch_uuid,
            )
            return None

        error_message = payload.get("error_message")
        now = datetime.utcnow()
        batch.status = ImageIndexBatchStatus.ERROR
        batch.error_message = (error_message or "")[: self.ERROR_MESSAGE_MAX]
        batch.completed_at = now
        batch.updated_at = now
        await session.flush()
        return self.build_batch_failed_payload(batch)
