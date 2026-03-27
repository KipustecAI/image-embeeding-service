# Step 6: Safety Nets, Stale Recovery, and Cleanup

## Objective

Safety nets are scheduled background jobs that catch anything the primary path (Stream → BatchTrigger → ARQ) misses. Under normal operation they find 0 rows. If they find rows, they log a WARNING — something upstream may be broken.

## Architecture

```
PRIMARY PATH (fast, event-driven):
  Stream → DB row (st=1) → BatchTrigger → ARQ worker → st=3/4

SAFETY NETS (slow, polling fallback):
  Every 60s:  Find st=1 embedding rows → re-dispatch
  Every 120s: Find st=1 search rows → re-dispatch
  Every 5m:   Find stale st=2 rows (>10min) → reset to st=1
  Every 1h:   Recalculate completed searches against new evidence
  Every 24h:  Delete completed rows older than 30 days
```

## Embedding Safety Net

Catches `embedding_requests` stuck at status=1 (missed by BatchTrigger):

```python
async def embedding_safety_net():
    """Runs every 60 seconds. Normally finds 0 rows."""
    async with get_session() as session:
        repo = EmbeddingRequestRepository(session)
        pending = await repo.get_pending_requests(limit=20)

        if not pending:
            return

        logger.warning(
            f"Safety net found {len(pending)} missed embedding requests — "
            f"BatchTrigger may not be receiving notifications"
        )

        request_ids = [str(r.id) for r in pending]

        # Same atomic dispatch as BatchTrigger callback
        for r in pending:
            r.status = EmbeddingRequestStatus.WORKING
            r.processing_started_at = datetime.utcnow()
        await session.flush()

        await arq_pool.enqueue_job(
            "process_evidence_embeddings_batch", request_ids
        )
        await session.commit()
```

## Search Safety Net

Same pattern for `search_requests`:

```python
async def search_safety_net():
    """Runs every 120 seconds. Normally finds 0 rows."""
    async with get_session() as session:
        repo = SearchRequestRepository(session)
        pending = await repo.get_pending_requests(limit=10)

        if not pending:
            return

        logger.warning(
            f"Safety net found {len(pending)} missed search requests"
        )

        request_ids = [str(r.id) for r in pending]
        for r in pending:
            r.status = SearchRequestStatus.WORKING
            r.processing_started_at = datetime.utcnow()
        await session.flush()

        await arq_pool.enqueue_job(
            "process_image_searches_batch", request_ids
        )
        await session.commit()
```

## Stale WORKING Recovery

Catches rows stuck in status=2 (WORKING) because the worker crashed or timed out:

```python
async def recover_stale_working():
    """Runs every 5 minutes. Resets rows stuck in WORKING > 10 minutes."""
    async with get_session() as session:
        # Embedding requests
        embed_repo = EmbeddingRequestRepository(session)
        stale_embeds = await embed_repo.get_stale_working(stale_minutes=10)

        for r in stale_embeds:
            r.retry_count += 1
            if r.retry_count >= 3:
                r.status = EmbeddingRequestStatus.ERROR
                r.error_message = "Max retries exceeded (stale WORKING recovery)"
                logger.error(f"Embedding request {r.id} failed after 3 retries")
            else:
                r.status = EmbeddingRequestStatus.TO_WORK  # Reset to 1
                r.worker_id = None
                logger.warning(f"Reset stale embedding request {r.id} (retry {r.retry_count})")

        # Search requests (same pattern)
        search_repo = SearchRequestRepository(session)
        stale_searches = await search_repo.get_stale_working(stale_minutes=10)

        for r in stale_searches:
            r.retry_count += 1
            if r.retry_count >= 3:
                r.status = SearchRequestStatus.ERROR
                r.error_message = "Max retries exceeded (stale WORKING recovery)"
            else:
                r.status = SearchRequestStatus.TO_WORK
                r.worker_id = None

        await session.commit()

        total = len(stale_embeds) + len(stale_searches)
        if total > 0:
            logger.warning(f"Recovered {total} stale WORKING rows")
```

## Search Recalculation

Re-runs completed searches against the current Qdrant state (new evidence may have been added since the original search):

```python
async def recalculate_searches():
    """Runs every hour. Re-searches completed queries against new evidence."""
    if not settings.recalculation_enabled:
        return

    async with get_session() as session:
        cutoff = datetime.utcnow() - timedelta(hours=settings.recalculation_hours_old)
        query = (
            select(SearchRequest)
            .where(
                and_(
                    SearchRequest.status == SearchRequestStatus.COMPLETED,
                    SearchRequest.similarity_status == SimilarityStatus.MATCHES_FOUND,
                    SearchRequest.processing_completed_at < cutoff,
                )
            )
            .limit(settings.recalculation_batch_size)
        )
        result = await session.execute(query)
        searches = list(result.scalars().all())

    if not searches:
        return

    # Reset to TO_WORK for reprocessing
    async with get_session() as session:
        for s in searches:
            s.status = SearchRequestStatus.TO_WORK
            s.worker_id = None
        await session.commit()

    # Notify search BatchTrigger
    trigger = get_batch_trigger("search")
    if trigger:
        await trigger.notify(count=len(searches))

    logger.info(f"Queued {len(searches)} searches for recalculation")
```

## Old Row Cleanup

Prevents unbounded table growth:

```python
async def cleanup_old_requests():
    """Runs every 24 hours. Deletes completed rows > 30 days old."""
    cutoff = datetime.utcnow() - timedelta(days=30)

    async with get_session() as session:
        # Delete old completed embedding requests (cascades to evidence_embeddings)
        stmt = delete(EmbeddingRequest).where(
            and_(
                EmbeddingRequest.status.in_([
                    EmbeddingRequestStatus.DONE,
                    EmbeddingRequestStatus.ERROR,
                ]),
                EmbeddingRequest.created_at < cutoff,
            )
        )
        result = await session.execute(stmt)
        embed_count = result.rowcount

        # Delete old completed search requests
        stmt = delete(SearchRequest).where(
            and_(
                SearchRequest.status.in_([
                    SearchRequestStatus.COMPLETED,
                    SearchRequestStatus.ERROR,
                ]),
                SearchRequest.created_at < cutoff,
            )
        )
        result = await session.execute(stmt)
        search_count = result.rowcount

        await session.commit()

    if embed_count or search_count:
        logger.info(f"Cleanup: deleted {embed_count} embed + {search_count} search old rows")
```

## Scheduler Registration (APScheduler)

All safety nets registered in FastAPI lifespan (Step 7):

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler()

# Safety nets
scheduler.add_job(embedding_safety_net, IntervalTrigger(seconds=60),
                  id="embedding_safety_net", misfire_grace_time=30)
scheduler.add_job(search_safety_net, IntervalTrigger(seconds=120),
                  id="search_safety_net", misfire_grace_time=60)
scheduler.add_job(recover_stale_working, IntervalTrigger(minutes=5),
                  id="stale_recovery", misfire_grace_time=60)

# Periodic tasks
scheduler.add_job(recalculate_searches, IntervalTrigger(hours=1),
                  id="recalculate_searches", misfire_grace_time=300)
scheduler.add_job(cleanup_old_requests, IntervalTrigger(hours=24),
                  id="cleanup_old", misfire_grace_time=600)

scheduler.start()
```

## New Dependency

```
# Add to requirements.txt
apscheduler>=3.10.0
```

## Config Additions

```python
# Safety nets
stale_working_minutes: int = 10             # STALE_WORKING_MINUTES
max_retries: int = 3                        # MAX_RETRIES
cleanup_days: int = 30                      # CLEANUP_DAYS
```
