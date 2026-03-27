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
