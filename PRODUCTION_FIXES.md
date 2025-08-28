# Production Timeout Fixes for Image Embedding Service

## Problem Summary
The worker is timing out after ~347 seconds while processing evidence embeddings, causing the following issues:
- ARQ worker timeout (300 seconds) is being exceeded
- Evidence is being processed multiple times due to retries
- HTTP requests are hanging when marking evidence as embedded

## Root Causes
1. **ARQ Worker Timeout Too Short**: Set to 300 seconds (5 minutes) but tasks take longer
2. **Evidence Check Interval Too Frequent**: Set to 1 second instead of 600 seconds
3. **Batch Size Too Large**: Processing 50 evidences in one batch can take too long
4. **HTTP Client Timeout Too Short**: 30 seconds may not be enough for API calls under load

## Fixes Applied

### 1. Increased ARQ Worker Timeout
**File**: `src/infrastructure/scheduler/arq_scheduler.py`
```python
"job_timeout": 600,  # Increased from 300 to 600 seconds (10 minutes)
```

### 2. Increased HTTP Client Timeout
**File**: `src/infrastructure/api/video_server_client.py`
```python
self.client = httpx.AsyncClient(
    timeout=httpx.Timeout(60.0, connect=10.0),  # Increased from 30s to 60s
    headers={"X-API-Key": self.api_key}
)
```

## Production Environment Variables to Update

Update your production `.env` file with these optimized values:

```env
# CRITICAL: Fix the check interval (currently set to 1 second!)
EVIDENCE_CHECK_INTERVAL=600  # Check every 10 minutes, not every second

# Reduce batch size to prevent timeouts
EVIDENCE_BATCH_SIZE=10  # Reduced from 50 to 10

# Keep other settings as is
IMAGE_SEARCH_CHECK_INTERVAL=30
IMAGE_SEARCH_BATCH_SIZE=10

# Optional: Adjust worker concurrency if needed
WORKER_CONCURRENCY=2  # Reduce from 4 if still having issues
```

## Deployment Steps

1. **Update the code**:
```bash
cd /root/projects/microservices/image-embedding-service
git pull  # or copy the updated files
```

2. **Update environment variables**:
```bash
# Edit the .env file
nano .env

# Change these values:
EVIDENCE_CHECK_INTERVAL=600  # CRITICAL: Not 1!
EVIDENCE_BATCH_SIZE=10       # Reduce batch size
```

3. **Restart the service**:
```bash
# If using Docker Compose
docker-compose down
docker-compose up -d --build

# If using systemd
systemctl restart image-embedding-worker

# If running directly
pkill -f "python worker.py"
python worker.py &
```

## Monitoring

After deployment, monitor for:

1. **Check the logs**:
```bash
# Docker logs
docker logs -f image-embedding-worker --tail 100

# Or direct logs
tail -f worker.log
```

2. **Look for successful completions**:
- "Successfully embedded image X/Y"
- "Marked evidence XXX as embedded"
- No TimeoutError messages

3. **Verify processing frequency**:
- Evidence should be checked every 10 minutes, not every second
- Each batch should complete within 10 minutes

## Additional Recommendations

1. **Consider splitting large evidence batches**:
   - If an evidence has many images (>10), process them in smaller chunks
   - Add progress tracking for long-running tasks

2. **Add connection pooling**:
   - Use httpx connection limits to prevent overwhelming the API server
   - Consider adding retry logic with exponential backoff

3. **Monitor resource usage**:
   - Check CPU and memory usage during processing
   - Ensure Qdrant has enough resources for vector operations

4. **Add health checks**:
   - Implement a health endpoint that verifies all connections
   - Set up monitoring alerts for timeout errors

## Troubleshooting

If timeouts persist after these changes:

1. **Check network connectivity**:
```bash
curl -X GET http://31.220.104.212:8000/health
```

2. **Verify API responsiveness**:
```bash
time curl -X GET http://31.220.104.212:8000/api/v1/evidences/internal/evidences/for-embedding?limit=1 \
  -H "X-API-Key: your-api-key"
```

3. **Check Qdrant performance**:
```bash
curl http://qdrant:6333/collections/evidence_embeddings
```

4. **Consider further reducing batch sizes**:
   - Set `EVIDENCE_BATCH_SIZE=5` if issues persist
   - Process evidences one at a time if necessary

## Long-term Solutions

1. **Implement async batch processing**: Process multiple evidences concurrently
2. **Add task queuing**: Use separate queues for different priority tasks
3. **Implement circuit breakers**: Automatically pause processing if errors exceed threshold
4. **Add distributed locking**: Prevent multiple workers from processing same evidence

## Related Files
- `/src/infrastructure/scheduler/arq_scheduler.py` - Worker configuration
- `/src/infrastructure/api/video_server_client.py` - API client
- `/src/infrastructure/config.py` - Configuration settings
- `/.env` - Environment variables