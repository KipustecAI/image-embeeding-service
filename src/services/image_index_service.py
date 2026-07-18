"""Service layer for the on-demand image-index feature.

Home of the SINGLE shared terminal-status helper (with the accounted-guard) and
the lifecycle payload builders. This is the one place the terminal rule and the
4-key count vocabulary are defined; the submit consumer, results consumer, and
reaper all route through here so the three surfaces can never drift
(docs/image-index/00_DESIGN.md §4 / §8).

Phase-1 scope: `terminal_status`, `counts_from_batch`, and the three payload
builders are IMPLEMENTED and unit-tested (pure). The orchestration methods
(`submit_batch_created`, `create_error_batch`, `land_computed`,
`mark_error_from_compute_error`) carry the frozen SIGNATURES only —
submit/create are wired in Phase 2, land/mark in Phase 3 (compute freeze).
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ..db.models.constants import ImageIndexBatchStatus
from ..db.repositories.image_index_repo import ImageIndexRepository
from ..infrastructure.config import get_settings
from ..infrastructure.database import get_session

if TYPE_CHECKING:  # avoid a hard import cycle at module load
    from ..db.models.image_index import ImageIndexBatch

logger = logging.getLogger(__name__)

# The one count vocabulary across DB columns / lifecycle / REST.
COUNT_KEYS = ("submitted", "embedded", "filtered", "failed")


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

    async def land_computed(self, session, payload: dict, *, vector_repo):
        """Phase 3: idempotently land a compute results payload (upsert rows +
        batched Qdrant upsert + absolute recompute + terminal-from-counts).
        """
        raise NotImplementedError("Phase 3 — depends on compute v1-FREEZE")

    async def mark_error_from_compute_error(self, session, payload: dict):
        """Phase 3: terminalize a batch as 'error' from a compute.error payload
        keyed on batch_id + error_message (NOT the live entity_id/entity_type shape).
        """
        raise NotImplementedError("Phase 3 — depends on compute v1-FREEZE")
