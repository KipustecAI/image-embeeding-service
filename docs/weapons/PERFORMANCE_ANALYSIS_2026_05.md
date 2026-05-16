# Weapons-notification performance — investigation 2026-05-15/16

**Trigger:** ops reported *"the stream input consumer for trigger the notifications is taking too much time"*.

**Outcome:** the service is healthy (p99 processing = 54 ms). The reported symptom has **two separate causes**, neither one a code problem on our side:

1. **Consumer queue wait** — messages sit in `embeddings:results` for ~65 s (p50) waiting for a worker. Throughput / scaling, not per-message slowness.
2. **Upstream coverage gap** — zero `weapon_analysis` blocks reached us on May 15–16 (0 / 8,305 rows). Notifications can't fire because there's nothing weapons-related to publish.

This page captures **the proof, the queries, and a triage decision tree** so the next time someone files this report, the next person doesn't redo the work.

---

## TL;DR — if someone reports "weapons notifications are slow"

Run these three checks in order. Each is ~10 seconds.

```
1. Did weapons analysis even reach us?    → Q5 (daily coverage)
                                              0% coverage  → upstream issue, escalate
                                            >50% coverage  → continue to #2
2. Is the consumer's per-message time slow? → Q7 (latency percentiles)
                                              p99 > 1000 ms → real code regression, profile
                                              p99 < 100 ms  → service is fine, continue to #3
3. Is the Redis consumer-group lagging?    → XINFO GROUPS
                                              lag > 1000   → scaling / throughput issue
                                              lag ~= 0     → reporter is confused or wrong
```

This is the decision tree. Everything below is the *evidence behind it* — concrete queries, real numbers from May 2026, and the wire diagram.

---

## The latency anatomy

```
Redis input id                                                Redis output id
  │                                                              │
  ├──── ~65s queue wait (p50) ───────┬──── ~30ms ──┬── ~70ms ────┤
  │   (message sits in Redis stream  │ processing  │ XADD +      │
  │    waiting for a free worker)    │ (our code)  │ network RTT │
  ▼                                  ▼             ▼             ▼
  XADD by compute             consumer picks   DB commit       XADD to
  embeddings:results          up (created_at)  (completed_at)  weapons:detected
```

The **only** part of this we own is `processing` — and it's already at 30 ms median. Everything else is queue depth / network.

---

## Diagnostic recipe

These are the queries we ran to produce the May 2026 numbers. Production credentials live in `.env`; do not commit them. The `PGURL` var below uses the Neon Postgres connection string.

### Q1 — 24h overview (is there any weapons traffic?)

```bash
PGURL='postgresql://USER:PASS@HOST/image-embedding-db?sslmode=require'
psql "$PGURL" -X -c "
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE weapon_analyzed) AS analyzed,
  COUNT(*) FILTER (WHERE has_weapon) AS with_weapons,
  COUNT(*) FILTER (WHERE weapon_analyzed AND NOT has_weapon) AS clean,
  COUNT(*) FILTER (WHERE status = 5) AS errors,
  MIN(created_at) AS oldest,
  MAX(created_at) AS newest
FROM embedding_requests
WHERE created_at > NOW() - INTERVAL '24 hours';
"
```

**Interpret:** `analyzed = 0` is the smoking gun for "upstream stopped enriching". Don't waste time profiling the consumer — talk to the routing / weapons-compute team.

### Q5 — coverage trend (when did it last work?)

```bash
psql "$PGURL" -X -c "
SELECT
  date_trunc('day', created_at) AS day,
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE weapon_analyzed) AS analyzed,
  COUNT(*) FILTER (WHERE has_weapon) AS with_weapons,
  COUNT(*) FILTER (WHERE weapon_analysis_error IS NOT NULL) AS analysis_errors
FROM embedding_requests
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1 DESC;
"
```

**Interpret:** the day where the `analyzed` column flips to 0 is when upstream coverage dropped. Take that date upstream — the routing or weapons-compute team can correlate against their deploy log.

### Q7 — per-phase latency on a known-good day (proves the service)

Replace the date range with a day where Q5 showed weapons traffic.

```bash
psql "$PGURL" -X -c "
WITH w AS (
  SELECT
    (split_part(stream_message_id, '-', 1))::BIGINT AS input_ms,
    (EXTRACT(EPOCH FROM created_at) * 1000)::BIGINT AS pickup_ms,
    (EXTRACT(EPOCH FROM processing_completed_at) * 1000)::BIGINT AS commit_ms
  FROM embedding_requests
  WHERE weapon_analyzed = TRUE
    AND created_at >= '2026-05-14'::timestamp
    AND created_at <  '2026-05-15'::timestamp
    AND stream_message_id IS NOT NULL
    AND processing_completed_at IS NOT NULL
)
SELECT 'queue_wait (input→pickup)' AS phase,
       COUNT(*), MIN(pickup_ms - input_ms),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pickup_ms - input_ms)::BIGINT,
       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY pickup_ms - input_ms)::BIGINT,
       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY pickup_ms - input_ms)::BIGINT,
       MAX(pickup_ms - input_ms),
       AVG(pickup_ms - input_ms)::BIGINT
FROM w
UNION ALL
SELECT 'processing (pickup→commit)',
       COUNT(*), MIN(commit_ms - pickup_ms),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY commit_ms - pickup_ms)::BIGINT,
       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY commit_ms - pickup_ms)::BIGINT,
       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY commit_ms - pickup_ms)::BIGINT,
       MAX(commit_ms - pickup_ms),
       AVG(commit_ms - pickup_ms)::BIGINT
FROM w
UNION ALL
SELECT 'total (input→commit)',
       COUNT(*), MIN(commit_ms - input_ms),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY commit_ms - input_ms)::BIGINT,
       PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY commit_ms - input_ms)::BIGINT,
       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY commit_ms - input_ms)::BIGINT,
       MAX(commit_ms - input_ms),
       AVG(commit_ms - input_ms)::BIGINT
FROM w;
"
```

**Interpret:** the **processing** row is the one that matters. If p99 is < 100 ms, our service is fine; the apparent slowness is elsewhere. The **queue_wait** row tells you whether the consumer is keeping up at all.

### Redis consumer-group lag (is the consumer keeping up?)

```bash
# From any machine with the prod Redis credentials in env:
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" -n 3 \
  XINFO GROUPS embeddings:results
```

**Interpret:** find the `backend-workers` group. If `lag > 1000`, the consumer is throughput-bound. If `pending` is big, in-flight messages are stuck (consumers crashed before XACK). If `lag ≈ 0`, the consumer is current — any "slow" report is a misread.

### Redis input → output stream cross-check (end-to-end proof for a specific event)

For a specific evidence_id, find both stream entries and compute the delta from the Redis IDs alone. This is the script at [`scripts/measure_stream_latency.py`](../../scripts/measure_stream_latency.py), or this inline Python for a single id:

```python
import os, json, redis
r = redis.Redis(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']),
                password=os.environ['REDIS_PASSWORD'], db=3, decode_responses=True)

target = 'evidence-uuid-here'

# Find on weapons:detected
for sid, fields in r.xrange('weapons:detected', count=10000):
    p = json.loads(fields.get('payload', '{}'))
    if p.get('evidence_id') == target:
        out_id = sid
        print(f'output_id={sid}')
        break

# Find on embeddings:results (or get from DB stream_message_id column)
# Compute delta = int(out_id.split('-')[0]) - int(in_id.split('-')[0])
```

A single Redis `XADD` round-trip to the remote Redis (`31.220.104.212`) adds ~70–80 ms on top of the DB commit time — that's network RTT, not code time.

---

## What we measured in May 2026

### Q1 result (last 24h, queried 2026-05-16)

| total | analyzed | with_weapons | clean | errors |
|--:|--:|--:|--:|--:|
| 8,258 | **0** | **0** | 0 | 393 |

8K rows processed, 0 had weapons analysis at all. This was the smoking gun.

### Q5 result (daily breakdown, last 30 days)

| Day | Total | Analyzed | with_weapons |
|---|--:|--:|--:|
| 2026-05-16 | 2,557 | **0** | 0 |
| **2026-05-15** | **5,748** | **0** | 0 |
| 2026-05-14 | 179 | 179 | 2 |
| 2026-05-13 | 65 | 64 | 0 |
| 2026-05-11 | 18 | 18 | 0 |
| 2026-05-07 | 3 | 0 | 0 |
| 2026-05-03 | 2,315 | **0** | 0 |
| 2026-05-02 | 9,886 | **0** | 0 |
| 2026-05-01 | 9,302 | **0** | 0 |
| 2026-04-30 | 340 | 0 | 0 |
| 2026-04-28 | 348 | 1 | 1 |
| 2026-04-27 | 1,534 | 11 | 7 |
| 2026-04-26 | 86 | 86 | 68 |
| 2026-04-23 | 124 | 124 | 24 |
| 2026-04-20 | 75 | 69 | 57 |
| 2026-04-16 | 23 | 15 | 15 |

The pattern is **bursty coverage** — not a single drop date. Big-traffic days (May 1, 2, 15) all had 0% coverage; small days bounced between 0% and 100%. This rules out *"image-weapons-compute crashed and stayed down"* — it's more like a routing flag / sample-rate config on the producer side, possibly tenant- or category-scoped.

**Last weapons-analyzed event reached us: 2026-05-14 20:47:27 UTC.**

`weapon_analysis_error = 0 forever` — upstream is silently skipping, not erroring loudly.

### Q7 result (May 14, the last day weapons worked, n=179)

| Phase | min | **p50** | p90 | p99 | max | avg |
|---|--:|--:|--:|--:|--:|--:|
| Queue wait (input→pickup) | 877 ms | **65,883 ms** | 86,719 ms | 111,176 ms | 115,565 ms | 53,543 ms |
| **Processing (pickup→commit)** | **28 ms** | **31 ms** | **46 ms** | **54 ms** | **66 ms** | **35 ms** |
| Total (input→commit) | 909 ms | 65,928 ms | 86,751 ms | 111,218 ms | 115,616 ms | 53,578 ms |

**Processing is uniformly fast: 28–66 ms across 179 events.** Queue wait is 65s median. Service is fine; queue management is the optimization target if total latency matters.

### Per-event proof — the 2 weapon-positive events on May 14

| evidence_id | queue_wait | **processing** | total (DB) | Redis input→output | classes | confidence |
|---|--:|--:|--:|--:|---|--:|
| 38e0022d…1be347c00 | 4,639 ms | **50 ms** | 4,690 ms | 4,757 ms | `arma_fuego` | 0.7707 |
| b5a6f42c…30d8e218  | 7,498 ms | **60 ms** | 7,558 ms | 7,642 ms | `arma_fuego` | 0.7367 |

Both alerts: **publish-to-Redis happened within 5–8 seconds of the input arriving**. The DB measurement (input id → DB commit) and the Redis measurement (input id → output id) agree within ~70 ms — that's one network RTT, exactly the expected overhead.

---

## What we definitively ruled out

- ❌ **Consumer code is slow.** Processing is 28–66 ms across 179 events. There's no per-message bottleneck.
- ❌ **Consumer is stuck.** Zero rows in `embedding_requests` with `processing_completed_at IS NULL` and a non-error status.
- ❌ **DB is slow.** Commits land within milliseconds of pickup.
- ❌ **Qdrant is slow.** Same — the processing column includes the Qdrant write.
- ❌ **The event publisher is buggy.** It only fires when `weapon_analyzed AND report_images_with_detections` — both correct semantics. On May 14 it fired correctly twice.

## The actual issues, ranked

| # | Issue | Owner | Severity |
|---|---|---|---|
| 1 | **Upstream stopped sending weapon_analysis** on May 15-16 (0/8,305 coverage) | Routing + image-weapons-compute team | High — no alerts firing |
| 2 | **Consumer is throughput-bound** — lag grows to 7K msgs during traffic spikes | This service (scale workers / parallelize uploads) | Medium — slow alerts when coverage is on |
| 3 | **No alerting on coverage drop** — we noticed because of a user report, not a metric | Observability | Medium — repeats this story next time |

---

## Cross-references

- [docs/weapons/RUNTIME.md](RUNTIME.md) — how the trigger works (the "what" — this page is the "why is it slow")
- [docs/weapons/CONTRACT.md](CONTRACT.md) — upstream producer contract; the `weapon_analysis` block they send
- [scripts/measure_stream_latency.py](../../scripts/measure_stream_latency.py) — generic Redis stream-id latency probe (use for non-weapons flows too)
- [docs/requirements/REPORT_GENERATION_STREAMS.md §2](../requirements/REPORT_GENERATION_STREAMS.md) — downstream contract; receiver of `weapons:detected`
- [docs/new_arq_v2/03_BACKEND_SERVICE.md](../new_arq_v2/03_BACKEND_SERVICE.md) — backend service responsibilities including report-event publishing
