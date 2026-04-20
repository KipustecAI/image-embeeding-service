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
