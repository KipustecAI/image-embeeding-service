"""Status constants for processing pipeline."""


class EmbeddingRequestStatus:
    TO_WORK = 1
    WORKING = 2
    EMBEDDED = 3
    DONE = 4
    ERROR = 5


class SearchRequestStatus:
    TO_WORK = 1
    WORKING = 2
    COMPLETED = 3
    ERROR = 4


class SimilarityStatus:
    NO_MATCHES = 1
    MATCHES_FOUND = 2


class SearchType:
    """Discriminator on ``search_requests`` (image-index SEARCH — 02_SEARCH_DESIGN §1).

    The single canonical name set, imported by BOTH the dispatch and the
    results consumer so the literal is never inlined twice. A mismatch would
    fall an image-index reply through the un-tenant-scoped evidence block — a
    cross-tenant leak, not a benign miss (must-fix M1).
    """

    EVIDENCE = "evidence"
    IMAGE_INDEX = "image_index"


class BlacklistEntryStatus:
    """Status of a blacklist image entry (the profile).

    See docs/image-blacklist/02_DATABASE.md for lifecycle transitions.
    """

    CREATED = 1  # Profile created, no references yet
    PROCESSING = 2  # At least one reference being embedded
    INDEXED = 3  # At least one reference stored in Qdrant, ready for matching
    UPDATING = 4  # Adding/removing references, version bumping
    ERROR = 5  # Processing failed


class BlacklistReferenceStatus:
    """Status of a single reference image attached to a blacklist entry."""

    TO_PROCESS = 1
    PROCESSING = 2
    PROCESSED = 3
    ERROR = 4


class ImageIndexBatchStatus:
    """Batch-level status for the on-demand image-index feature.

    Stored as TEXT (CheckConstraint) so the vocabulary lines up 1:1 with the
    coordinator lifecycle — unlike the integer status-machines above. See
    docs/image-index/00_DESIGN.md §3.

    Machine: pending → computing → {completed | completed_with_errors | error}
    """

    PENDING = "pending"
    COMPUTING = "computing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    ERROR = "error"


class ImageIndexResultStatus:
    """Per-item disposition for the on-demand image-index feature.

    ``FAILED_SET`` members fold into the single ``failed`` count key. ``FILTERED``
    is a clean disposition, never a downgrade. See docs/image-index/00_DESIGN.md §3.
    """

    EMBEDDED = "embedded"
    DOWNLOAD_FAILED = "download_failed"
    DECODE_FAILED = "decode_failed"
    FILTERED = "filtered"
    NO_RESULT = "no_result"

    # → folds into failed_count
    FAILED_SET = frozenset({DOWNLOAD_FAILED, DECODE_FAILED, NO_RESULT})
