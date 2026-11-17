#!/usr/bin/env python
"""Manual re-send of the image blacklist to lookia-dw (Frontend#dw-online #4936 (a)).

The service already re-sends the whole blacklist every day at
BLACKLIST_DW_RECONCILE_HOUR_UTC and retries every failed send each sweep. This
script is the on-demand version, for devops-2 (e.g. after a DW table reset):

    python -m scripts.resend_blacklist_to_dw            # show what is pending, send nothing
    python -m scripts.resend_blacklist_to_dw --apply    # mark every entry + reference
                                                        # pending, then send
    python -m scripts.resend_blacklist_to_dw --apply --facts
                                                        # also re-send embeddings,
                                                        # tombstones and live hits

Run it inside the API container (it uses the same DATABASE_URL / REDIS_STREAMS_URL).
Safe to repeat: the DW upsert keeps the newer ``updated_at`` and keys facts on ``id``.
Tombstones and hits still follow BLACKLIST_DW_TOMBSTONES_ENABLED /
BLACKLIST_MATCH_DW_ENABLED — the script never sends what the DW does not accept yet.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, or_, select

from src.db.models.blacklist_image import (
    BlacklistImageDeletion,
    BlacklistImageEmbedding,
    BlacklistImageEntry,
    BlacklistImageMatch,
    BlacklistImageReference,
)
from src.infrastructure.config import get_settings
from src.infrastructure.database import AsyncSessionLocal, engine
from src.services import blacklist_dw_delivery as delivery
from src.streams.producer import StreamProducer


async def _pending(session) -> dict[str, int]:
    def versioned(model):
        return or_(
            model.dw_published_updated_at.is_(None),
            model.dw_published_updated_at != model.updated_at,
        )

    counts = {}
    for name, model, cond in (
        ("entries", BlacklistImageEntry, versioned(BlacklistImageEntry)),
        ("references", BlacklistImageReference, versioned(BlacklistImageReference)),
        ("embeddings", BlacklistImageEmbedding, BlacklistImageEmbedding.dw_published_at.is_(None)),
        ("tombstones", BlacklistImageDeletion, BlacklistImageDeletion.dw_published_at.is_(None)),
        ("hits", BlacklistImageMatch, BlacklistImageMatch.dw_published_at.is_(None)),
    ):
        counts[name] = await session.scalar(select(func.count()).select_from(model).where(cond))
    return counts


async def main(apply: bool, facts: bool) -> None:
    settings = get_settings()
    print(
        f"flags: publish={settings.blacklist_dw_publish_enabled} "
        f"tombstones={settings.blacklist_dw_tombstones_enabled} "
        f"hits={settings.blacklist_match_dw_enabled}"
    )
    async with AsyncSessionLocal() as session:
        print("pending before:", await _pending(session))
        if not apply:
            print("dry run — pass --apply to re-send")
            return
        delivery.set_blacklist_dw_producer(StreamProducer(redis_url=settings.redis_streams_url))
        marked = await delivery.mark_all_for_reconcile(session)
        if facts:
            marked += await delivery.mark_facts_for_resend(session)
        print("marked for re-send:", marked)
        sent = 0
        while True:
            batch = await delivery.sweep_pending(session)
            sent += batch
            if batch == 0:
                break
        print("sent:", sent)
        print("pending after:", await _pending(session))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="mark pending and send")
    parser.add_argument(
        "--facts", action="store_true", help="also re-send embeddings, tombstones and hits"
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.facts))
