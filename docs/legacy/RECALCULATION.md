# Image Search Recalculation

## Overview

The Image Embedding Service supports automatic and manual recalculation of completed image searches when new evidence is added to the vector database. This ensures that users get updated results without having to re-submit their searches.

## How It Works

### Automatic Recalculation (Scheduled)

When enabled, the service will periodically check for completed searches that may benefit from recalculation with newly added evidence.

**Eligibility Criteria:**
- `search_status = 3` (COMPLETED)
- `similarity_status = 2` (MATCHES_FOUND)
- `processed_at` older than configured hours (default: 2 hours)

**Configuration (Environment Variables):**
```bash
# Enable/disable automatic recalculation
RECALCULATION_ENABLED=false  # Set to true to enable

# How often to run recalculation (seconds)
RECALCULATION_INTERVAL=3600  # 1 hour

# Only recalculate searches older than X hours
RECALCULATION_HOURS_OLD=2

# Batch size per recalculation run
RECALCULATION_BATCH_SIZE=20
```

**Schedule:**
- If interval >= 60 minutes: Runs at minute 15 of specified hours (e.g., 00:15, 01:15)
- If interval < 60 minutes: Runs at specified minute intervals

### Manual Recalculation (API Endpoints)

#### 1. Recalculate Multiple Searches

```bash
POST /api/v1/recalculate/searches
```

**Query Parameters:**
- `search_ids` (optional): List of specific search IDs to recalculate
- `limit` (optional): Maximum number of searches to recalculate (default: 10)
- `hours_old` (optional): Only recalculate searches older than X hours
- `force_all` (optional): Recalculate ALL eligible searches (use carefully!)

**Examples:**

a) **Recalculate a batch (default 10):**
```bash
curl -X POST http://localhost:8001/api/v1/recalculate/searches
```

b) **Recalculate specific searches:**
```bash
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?search_ids=id1&search_ids=id2"
```

c) **Recalculate searches older than 2 hours:**
```bash
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?hours_old=2&limit=20"
```

d) **Force recalculate ALL (use carefully!):**
```bash
curl -X POST "http://localhost:8001/api/v1/recalculate/searches?force_all=true"
```

#### 2. Recalculate Single Search

```bash
POST /api/v1/recalculate/search/{search_id}

curl -X POST http://localhost:8001/api/v1/recalculate/search/abc123-def456
```

## Testing

### Test Scripts

1. **Test Manual Recalculation:**
```bash
python test_recalculation.py
```

2. **Test Force All:**
```bash
python test_recalculation.py --force-all
```

3. **Test Scheduler Configuration:**
```bash
python test_scheduler_config.py
```

### Verify Scheduler is Running

Check logs for recalculation task execution:
```bash
docker logs image-embedding-service 2>&1 | grep "recalculate"
```

## Implementation Details

### Workflow

1. **Fetch Eligible Searches**: Query for completed searches with matches
2. **Apply Time Filter**: Only process searches older than threshold
3. **Execute Search**: Re-run similarity search with current vector database
4. **Update Results**: Store new results in database metadata
5. **Update Redis**: Cache updated results if configured

### Performance Considerations

- Recalculation is more expensive than initial search (no caching)
- Each search requires embedding generation and vector comparison
- Batch size should balance throughput and system load
- Consider scheduling during off-peak hours

### Status Management

During recalculation:
- Search maintains `search_status = 3` (COMPLETED)
- `similarity_status` updates based on new results
- `processed_at` updates to current timestamp
- `total_matches` reflects new count
- Previous results are replaced entirely

## ARQ Scheduler Integration

The recalculation task is integrated with the ARQ scheduler and runs automatically when enabled:

```python
# src/infrastructure/scheduler/arq_scheduler.py

async def recalculate_searches(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Task to recalculate completed searches with new evidence.
    Runs periodically based on configuration.
    """
    # Check if enabled
    if not scheduler.settings.recalculation_enabled:
        return {"skipped": True, "reason": "Recalculation disabled"}
    
    # Fetch and process eligible searches
    # Returns metrics about processing
```

The task is scheduled based on the `RECALCULATION_INTERVAL`:
- For hourly intervals: Runs at minute 15 (e.g., 00:15, 01:15)
- For sub-hourly intervals: Runs at regular minute intervals

## Current Status (December 2024)

The recalculation feature is **fully operational** with the following configuration:

### Active Scheduler Configuration
```
✅ Recalculation Task:
  - Enabled: True
  - Interval: 3600 seconds (60 minutes)
  - Hours Old: 2 hours
  - Batch Size: 20 searches
  - Schedule: Runs every hour at minute 15 (00:15, 01:15, 02:15, ...)
```

### All Active Cron Jobs
1. **process_evidence_embeddings** - Every 10 minutes
   - Processes new evidence images for embedding
   - Runs at startup: Yes

2. **process_image_searches** - Every minute
   - Processes pending user search requests
   - Runs at startup: Yes

3. **update_vector_statistics** - Every hour at minute 0
   - Updates Qdrant vector database statistics
   - Runs at startup: Yes

4. **recalculate_searches** - Every hour at minute 15
   - Recalculates completed searches with new evidence
   - Runs at startup: No (to avoid unnecessary load)

## Monitoring

### Metrics to Track

1. **Recalculation Rate**: Number of searches recalculated per hour
2. **Match Changes**: How often recalculation finds new matches
3. **Processing Time**: Average time per recalculation
4. **Queue Depth**: Number of searches pending recalculation

### Log Messages

```
INFO: Starting search recalculation task
INFO: Recalculating 5 searches
INFO: Recalculation task completed: 4/5 successful, 1 failed in 8.5s
DEBUG: Search recalculation is disabled (when RECALCULATION_ENABLED=false)
```

### Verification Commands

```bash
# Check current scheduler configuration
docker exec -it <container-id> python test_scheduler_config.py

# Monitor recalculation logs in real-time
docker logs -f <container-id> 2>&1 | grep -i recalculate

# Manually trigger recalculation
docker exec -it <container-id> python test_recalculation.py

# Check if recalculation is running
docker exec -it <container-id> ps aux | grep recalculate
```

## Best Practices

1. **Start Conservatively**: Begin with small batch sizes and longer intervals
2. **Monitor Load**: Watch CPU/memory usage during recalculation periods
3. **Time Windows**: Set appropriate `hours_old` to avoid unnecessary recalculation
4. **Manual Triggers**: Use manual recalculation for critical searches
5. **Alerting**: Set up alerts for failed recalculations

## Troubleshooting

### Common Issues

**Issue**: Recalculation not running
- Check `RECALCULATION_ENABLED=true`
- Verify ARQ worker is running
- Check Redis connectivity
- Review scheduler logs

**Issue**: Too many recalculations
- Increase `RECALCULATION_HOURS_OLD`
- Decrease `RECALCULATION_BATCH_SIZE`
- Increase `RECALCULATION_INTERVAL`

**Issue**: Searches not updating
- Verify search eligibility (status values)
- Check API connectivity
- Review error logs

## API Reference

See full API documentation at `http://localhost:8001/docs` when service is running.

## Future Enhancements

- [ ] Differential recalculation (only check new evidence since last run)
- [ ] Priority-based recalculation queue
- [ ] Notification system for significant result changes
- [ ] Recalculation history tracking
- [ ] Cost optimization with result similarity thresholds
- [ ] WebSocket notifications for real-time updates