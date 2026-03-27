# Questions — Resolved

All questions answered. Summary of decisions below.

---

## Stream Events

**Q1: Who publishes to Redis Streams?**
**RESOLVED:** The Video Server publishes events. We only consume. No integration work needed on our side.

**Q2: Stream names and Redis DB**
**RESOLVED:** Same Redis instance. Stream names: `evidence:embed` and `evidence:search`.

**Q3: Stream payload — who provides image_urls?**
**RESOLVED:** We receive the same payload as the face service (full image URLs included). Must implement a **diversity filter** (same as deepface's Bhattacharyya histogram filter) to skip near-duplicate images before processing.

---

## Database

**Q4: PostgreSQL instance — shared or dedicated?**
**RESOLVED:** Dedicated instance will be provided. Connection string will be given separately.

**Q5: Connection string**
**RESOLVED:** Same `postgresql+asyncpg://` driver. Connection string provided at deploy time.

**Q6: Retention policy**
**RESOLVED:** 30 days for completed rows via `CLEANUP_DAYS` config (default accepted).

---

## Processing Pipeline

**Q7: Do we still notify the Video Server?**
**RESOLVED: NO.** We are now the source of truth for embedding and search data. We expose endpoints for the Video Server to query when it needs our data. No callbacks.

**Q8: Recalculation trigger**
**RESOLVED:** Recalculation lives entirely on our side (like deepface's blacklist rematch). Must be a triggerable endpoint AND scheduled.

**Q9: Search results storage**
**RESOLVED:** Store search results in a **dedicated Redis DB with TTL** for fast access (results are ephemeral and change over time). Use a separate Redis DB on the remote Redis instance. Not in PostgreSQL — Redis is better for this use case.

---

## Infrastructure

**Q10: APScheduler vs ARQ cron**
**RESOLVED:** Use APScheduler (same as face server).

**Q11: Worker scaling**
**RESOLVED:** Plan for 1-to-N workers from the start. Use FOR UPDATE SKIP LOCKED.

**Q12: Migration strategy**
**RESOLVED: Big bang.** No backward compatibility. Full freedom to reshape endpoints and architecture.

**Q13: Internal API availability**
**RESOLVED:** Recalculation is entirely our responsibility. No dependency on Video Server internal endpoints for this. We expose a triggerable endpoint for recalculation.

**Q14: Video Server Redis access**
**RESOLVED:** We only consume from Redis Streams. No publishing needed.

---

## New Requirements from Answers

1. **Diversity filter** — port from deepface (`src/services/diversity_filter.py`). Bhattacharyya histogram distance, threshold 0.10, max 10 images per evidence. Runs before creating embedding DB rows.
2. **We are source of truth** — must expose query endpoints for the Video Server to fetch embedding/search status.
3. **Search results in Redis** — dedicated Redis DB with TTL, not PostgreSQL.
4. **Recalculation endpoint** — triggerable API endpoint + hourly schedule.
5. **No Video Server callbacks** — remove all `video_server_client.py` notification logic.
