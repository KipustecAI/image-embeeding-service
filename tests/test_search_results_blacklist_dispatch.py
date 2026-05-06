"""Unit tests for search_results_consumer's blacklist-embed dispatch path.

The integration-level success path (Qdrant + DB writes) is exercised in
verification. Here we lock the input-validation contract that runs
before any side effects so we know malformed payloads are dropped with
a useful log line rather than crashing the consumer or, worse, silently
mutating state with a None entry_id.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.streams import search_results_consumer as srcons


async def test_missing_blacklist_entry_id_drops_message(caplog):
    """Dropped silently except for the error log — the upstream contract
    requires entry_id to be present whenever purpose=blacklist_embed."""
    caplog.set_level(logging.ERROR, logger="src.streams.search_results_consumer")
    payload = {
        "search_id": str(uuid4()),
        "user_id": "u-1",
        "vector": [0.1, 0.2],
        # blacklist_entry_id intentionally absent
    }

    with patch(
        "src.services.blacklist_embed_service.store_blacklist_embedding",
        new=AsyncMock(),
    ) as mock_store:
        await srcons._process_blacklist_embed_result(payload, "msg-id-1")
        mock_store.assert_not_called()

    assert any("missing blacklist_entry_id" in rec.message for rec in caplog.records)


async def test_malformed_uuids_drop_message(caplog):
    """Defensive parse: both ids must be UUIDs. Anything else means the
    upstream shape changed and we should fail loudly, not silently miss."""
    caplog.set_level(logging.ERROR, logger="src.streams.search_results_consumer")
    payload = {
        "search_id": "not-a-uuid",
        "blacklist_entry_id": str(uuid4()),
        "user_id": "u-1",
        "vector": [0.1, 0.2],
    }

    with patch(
        "src.services.blacklist_embed_service.store_blacklist_embedding",
        new=AsyncMock(),
    ) as mock_store:
        await srcons._process_blacklist_embed_result(payload, "msg-id-2")
        mock_store.assert_not_called()

    assert any("malformed UUIDs" in rec.message for rec in caplog.records)


async def test_well_formed_payload_calls_embed_service():
    """Happy path: parsed UUIDs hand off to the embed service with the
    correct kwargs. Service implementation tested elsewhere."""
    entry_uuid = uuid4()
    ref_uuid = uuid4()
    payload = {
        "search_id": str(ref_uuid),
        "blacklist_entry_id": str(entry_uuid),
        "user_id": "tenant-7",
        "vector": [0.1, 0.2, 0.3],
    }

    with patch(
        "src.services.blacklist_embed_service.store_blacklist_embedding",
        new=AsyncMock(),
    ) as mock_store:
        await srcons._process_blacklist_embed_result(payload, "msg-id-3")
        mock_store.assert_called_once()
        kwargs = mock_store.call_args.kwargs
        assert kwargs["entry_id"] == entry_uuid
        assert kwargs["reference_id"] == ref_uuid
        assert kwargs["user_id"] == "tenant-7"
        assert kwargs["vector"] == [0.1, 0.2, 0.3]
        assert kwargs["stream_msg_id"] == "msg-id-3"


async def test_embed_service_exception_is_logged_not_raised(caplog):
    """If store_blacklist_embedding throws, the consumer must log + swallow
    so the next XREADGROUP delivery retries (DLQ semantics). A propagated
    raise would PEL-block the message permanently."""
    caplog.set_level(logging.ERROR, logger="src.streams.search_results_consumer")
    payload = {
        "search_id": str(uuid4()),
        "blacklist_entry_id": str(uuid4()),
        "user_id": "u-1",
        "vector": [0.1, 0.2],
    }

    with patch(
        "src.services.blacklist_embed_service.store_blacklist_embedding",
        new=AsyncMock(side_effect=RuntimeError("qdrant exploded")),
    ):
        await srcons._process_blacklist_embed_result(payload, "msg-id-4")

    assert any("Failed to store blacklist embedding" in rec.message for rec in caplog.records)


async def test_default_purpose_does_not_dispatch_to_blacklist():
    """Regression guard for the legacy contract: callers that omit
    ``purpose`` must NOT trigger the blacklist branch. Tests just the
    dispatch decision — the search-path side effects (DB writes) live in
    integration coverage."""
    payload = {
        "search_id": str(uuid4()),
        "user_id": "u-1",
        "vector": [0.1, 0.2],
        # no `purpose` field — must default to "search"
    }
    with patch.object(srcons, "_process_blacklist_embed_result", new=AsyncMock()) as bl_handler:
        # The search path hits SQLAlchemy after the dispatch and will
        # raise on the test's mocked session — that's fine. Our contract
        # is "blacklist branch was not taken before the search path ran",
        # which is observable regardless of what happens downstream.
        try:
            await srcons._process_search_result(payload, "msg-id-5")
        except Exception:
            pass
        bl_handler.assert_not_called()


async def test_explicit_purpose_search_does_not_dispatch_to_blacklist():
    """Same contract, but with the field explicitly set rather than
    relying on the default — guards against a regression where the
    default-resolver works but explicit "search" gets misrouted."""
    payload = {
        "search_id": str(uuid4()),
        "user_id": "u-1",
        "vector": [0.1, 0.2],
        "purpose": "search",
    }
    with patch.object(srcons, "_process_blacklist_embed_result", new=AsyncMock()) as bl_handler:
        try:
            await srcons._process_search_result(payload, "msg-id-6")
        except Exception:
            pass
        bl_handler.assert_not_called()
