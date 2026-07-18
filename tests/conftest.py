"""Shared pytest fixtures.

Load-bearing (docs/image-index/00_DESIGN.md §3): this repo historically had NO
`tests/conftest.py`, so the existing suite commits to real Postgres and rows
leak across tests. The autouse fixture below TRUNCATEs the two image-index
tables (RESTART IDENTITY CASCADE) around every test so the
redelivery/idempotency tests are trustworthy.

Guarded two ways:
  1) Only truncates tables that actually EXIST (information_schema check), so it
     is a no-op when migrations haven't been applied.
  2) Best-effort on connection — if no test DB is reachable the fixture yields
     silently, so pure unit tests (e.g. the terminal_status truth table) still
     run without a database.

Scope is deliberately narrow: only the two NEW tables. Widening the truncate to
the pre-existing committed tables is a separate decision (00_DESIGN, out-of-scope).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.infrastructure.config import get_settings

# ONLY the two new tables — never the live evidence/search/blacklist tables.
_IMAGE_INDEX_TABLES = ("image_index_results", "image_index_batches")


async def _truncate_image_index_tables() -> None:
    """TRUNCATE the existing image-index tables. Best-effort; never raises."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                    ),
                    {"names": list(_IMAGE_INDEX_TABLES)},
                )
            ).scalars().all()
            if not existing:
                return
            # CASCADE handles the results→batches FK ordering regardless of order.
            table_list = ", ".join(existing)
            await conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
    except Exception:
        # No DB reachable / not migrated — pure unit tests still run.
        return
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_image_index_tables():
    """Autouse: clean the image-index tables before AND after each test."""
    await _truncate_image_index_tables()
    yield
    await _truncate_image_index_tables()
