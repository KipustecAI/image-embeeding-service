# Step 7: FastAPI Endpoint + Lifespan Migration

## Objective

Update the FastAPI application to wire all new components in the lifespan and update endpoints to use local DB state.

## Lifespan: Startup Sequence

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool, scheduler, embed_consumer, search_consumer

    logger.info("Starting Image Embedding Service...")

    # 1. Database — verify connection
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("PostgreSQL connected")

    # 2. Redis — create ARQ pool
    arq_pool = await create_pool(get_redis_settings())
    logger.info("ARQ pool created")

    # 3. Scheduler — safety nets + periodic tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(embedding_safety_net, IntervalTrigger(seconds=60),
                      id="embedding_safety_net", misfire_grace_time=30)
    scheduler.add_job(search_safety_net, IntervalTrigger(seconds=120),
                      id="search_safety_net", misfire_grace_time=60)
    scheduler.add_job(recover_stale_working, IntervalTrigger(minutes=5),
                      id="stale_recovery", misfire_grace_time=60)
    scheduler.add_job(recalculate_searches, IntervalTrigger(hours=1),
                      id="recalculate_searches", misfire_grace_time=300)
    scheduler.add_job(cleanup_old_requests, IntervalTrigger(hours=24),
                      id="cleanup_old", misfire_grace_time=600)
    scheduler.start()
    logger.info("Scheduler started with safety nets")

    # 4. Batch Triggers — event-driven dispatch
    embedding_trigger = create_batch_trigger(
        name="embedding",
        batch_size=settings.batch_trigger_size,
        max_wait_seconds=settings.batch_trigger_max_wait,
        process_callback=dispatch_embedding_batch,
    )
    await embedding_trigger.start()

    search_trigger = create_batch_trigger(
        name="search",
        batch_size=settings.batch_trigger_search_size,
        max_wait_seconds=settings.batch_trigger_search_wait,
        process_callback=dispatch_search_batch,
    )
    await search_trigger.start()
    logger.info("Batch triggers started")

    # 5. Stream Consumers — push-based ingestion
    set_event_loop(asyncio.get_running_loop())

    embed_consumer = create_evidence_embed_consumer()
    embed_consumer.start()

    search_consumer = create_evidence_search_consumer()
    search_consumer.start()
    logger.info("Stream consumers started")

    logger.info("Image Embedding Service started successfully")

    yield

    # ── SHUTDOWN ──
    logger.info("Shutting down...")

    if embed_consumer:
        embed_consumer.stop()
    if search_consumer:
        search_consumer.stop()

    embedding_trigger_ref = get_batch_trigger("embedding")
    if embedding_trigger_ref:
        await embedding_trigger_ref.stop()
    search_trigger_ref = get_batch_trigger("search")
    if search_trigger_ref:
        await search_trigger_ref.stop()

    if scheduler.running:
        scheduler.shutdown(wait=True)

    if arq_pool:
        await arq_pool.close()

    await engine.dispose()

    logger.info("Shutdown complete")
```

## Updated Endpoints

### Health Check — includes new components

```python
@app.get("/health")
async def health_check():
    components = {
        "database": False,
        "qdrant": False,
        "scheduler": scheduler.running if scheduler else False,
    }

    # Check DB
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["database"] = True
    except Exception:
        pass

    # Check Qdrant (via existing vector repo)
    try:
        stats = await vector_repo.get_collection_stats()
        components["qdrant"] = bool(stats)
    except Exception:
        pass

    # Batch trigger health
    embed_trigger = get_batch_trigger("embedding")
    search_trigger = get_batch_trigger("search")

    return {
        "status": "healthy" if all(components.values()) else "degraded",
        "components": components,
        "triggers": {
            "embedding": embed_trigger.health() if embed_trigger else None,
            "search": search_trigger.health() if search_trigger else None,
        },
        "qdrant_stats": stats if components["qdrant"] else None,
    }
```

### Stats — add DB pipeline metrics

```python
@app.get("/api/v1/stats")
async def get_statistics():
    async with get_session() as session:
        # Pipeline counts
        embed_counts = {}
        for status_name, status_val in [
            ("to_work", 1), ("working", 2), ("embedded", 3), ("done", 4), ("error", 5)
        ]:
            result = await session.execute(
                select(func.count()).where(EmbeddingRequest.status == status_val)
            )
            embed_counts[status_name] = result.scalar()

        search_counts = {}
        for status_name, status_val in [
            ("to_work", 1), ("working", 2), ("completed", 3), ("error", 4)
        ]:
            result = await session.execute(
                select(func.count()).where(SearchRequest.status == status_val)
            )
            search_counts[status_name] = result.scalar()

    qdrant_stats = await vector_repo.get_collection_stats()

    return {
        "service": "Image Embedding Service",
        "pipeline": {
            "embedding_requests": embed_counts,
            "search_requests": search_counts,
        },
        "vector_database": qdrant_stats,
        "configuration": {
            "model": settings.clip_model_name,
            "device": settings.clip_device,
            "vector_size": settings.qdrant_vector_size,
        },
    }
```

### Internal Trigger Endpoint

```python
@app.post("/api/v1/internal/trigger/{trigger_name}")
async def notify_trigger(trigger_name: str, count: int = 1):
    """Called by ARQ worker (separate process) to notify in-memory triggers."""
    trigger = get_batch_trigger(trigger_name)
    if not trigger:
        raise HTTPException(404, f"Trigger '{trigger_name}' not found")
    await trigger.notify(count=count)
    return {"notified": True, "count": count}
```

### Manual Endpoints — still work, now write to DB too

Existing manual endpoints (`/api/v1/embed/evidence`, `/api/v1/search/manual`) continue to work for ad-hoc usage. They bypass the stream/trigger path and go directly to the use cases, but can optionally create a DB record for tracking.

## Endpoints That Can Be Removed (after migration)

These endpoints won't be needed once streams are the primary input:

- `POST /api/v1/process/evidences` — replaced by stream + trigger
- `POST /api/v1/process/searches` — replaced by stream + trigger

Keep them temporarily for backward compatibility, but they should just dispatch to the same ARQ jobs.

## Backward Compatibility

During the migration period, BOTH paths work:

1. **New path:** Stream → DB row → BatchTrigger → ARQ worker
2. **Old path:** API endpoint → direct use case execution

The old polling-based cron jobs in `arq_scheduler.py` can be removed once streams are confirmed working in production. Keep the safety nets as permanent fallbacks.
