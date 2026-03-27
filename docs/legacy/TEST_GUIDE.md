# Image Embedding Service - Test Guide

This directory contains test scripts to verify and measure the performance of the Image Embedding Service.

## Available Test Scripts

### 1. Quick Test (`quick_test.py`)
A simple, fast test to verify basic functionality.

**Purpose**: Quick health check and basic functionality verification
**Duration**: ~10 seconds

```bash
python docs/quick_test.py
```

**Tests:**
- Service health check
- Single embedding creation
- Similarity search
- Batch processing
- Statistics retrieval

**Use when:**
- After starting the service
- Quick validation of deployment
- Debugging basic issues

### 2. Comprehensive Test (`test_embedding_service.py`)
Full functional test suite covering all endpoints and workflows.

**Purpose**: Complete functional testing
**Duration**: ~2-3 minutes

```bash
# With default API key
python docs/test_embedding_service.py

# With custom API key
python docs/test_embedding_service.py vsk_dev_your_api_key_here
```

**Tests:**
- Health checks with component status
- Manual evidence embedding
- Manual image search with results
- Batch processing (evidences and searches)
- Statistics and metrics
- End-to-end flow through main API
- Cache validation

**Use when:**
- Validating new deployments
- After configuration changes
- Integration testing with main API
- Regression testing

### 3. Performance Test (`test_performance.py`)
Comprehensive performance testing and benchmarking.

**Purpose**: Performance measurement and stress testing
**Duration**: ~5-10 minutes

```bash
python docs/test_performance.py
```

**Tests:**
- Single embedding generation speed
- Vector search performance
- Batch processing throughput
- Concurrent request handling
- Vector database scaling (100, 500, 1000+ vectors)

**Metrics provided:**
- Response times (min, max, mean, median)
- Throughput (items/second)
- Success rates
- Scaling characteristics

**Use when:**
- Performance tuning
- Capacity planning
- Comparing different configurations
- Stress testing

## Prerequisites

### Required Python Packages
```bash
pip install httpx rich
```

### Service Requirements
1. Image Embedding Service running on port 8001
2. Main Video Server API running on port 8000 (for end-to-end tests)
3. Valid API key with DEV or ROOT role

### Using Docker
```bash
# Start all services
cd microservices/image_embedding_service
docker-compose up -d

# Wait for services to be ready
sleep 10

# Run quick test
python docs/quick_test.py
```

## Configuration

### Environment Variables
Set these before running tests:
```bash
export MAIN_API_URL="http://localhost:8000"
export EMBEDDING_API_URL="http://localhost:8001"
export API_KEY="vsk_dev_your_api_key"
```

### Modify Test Parameters
Edit the scripts to change:
- Number of test iterations
- Concurrent request count
- Image URLs
- Similarity thresholds
- Batch sizes

## Understanding Results

### Quick Test Output
```
✅ Service is healthy
✅ Embedding created successfully
✅ Search completed successfully
✅ Statistics retrieved
Result: 5/5 tests passed
```

### Performance Test Metrics
```
Embedding Performance (ms)
├── Min: 120.45      # Fastest response
├── Max: 450.23      # Slowest response
├── Mean: 225.67     # Average
├── Median: 210.34   # Middle value
├── Std Dev: 45.23   # Consistency
└── Success Rate: 98.5%
```

### Interpreting Performance

**Good Performance (CPU)**:
- Embedding: < 300ms average
- Search: < 100ms average
- Batch throughput: > 5 items/sec

**Good Performance (GPU)**:
- Embedding: < 50ms average
- Search: < 50ms average
- Batch throughput: > 20 items/sec

## Troubleshooting

### Common Issues

**Service not reachable:**
```bash
# Check if service is running
docker ps | grep embedding

# Check logs
docker-compose logs embedding-api
```

**Authentication errors:**
```bash
# Verify API key has DEV or ROOT role
# Check key in main API database or configuration
```

**Slow performance:**
```bash
# Check CPU/Memory usage
docker stats

# Review configuration
cat .env | grep BATCH_SIZE
cat .env | grep CLIP_DEVICE
```

**Vector DB errors:**
```bash
# Check Qdrant is running
curl http://localhost:6333/collections

# Reset collection if needed
docker-compose restart qdrant
```

## Continuous Testing

### Automated Testing
Add to CI/CD pipeline:
```yaml
- name: Start services
  run: docker-compose up -d
  
- name: Wait for services
  run: sleep 30
  
- name: Run quick tests
  run: python docs/quick_test.py
  
- name: Run performance baseline
  run: python docs/test_performance.py
```

### Monitoring
Monitor these metrics in production:
- Embedding generation time
- Search response time
- Queue length
- Vector DB size
- Cache hit rate

## Test Data

### Sample Images
The tests use various image sources:
- Picsum Photos (random images)
- Placeholder.com (test images)
- Your actual evidence images

### Adding Custom Test Images
Edit the `TEST_IMAGES` or `SAMPLE_IMAGES` arrays in the scripts:
```python
TEST_IMAGES = [
    "https://your-storage.com/image1.jpg",
    "https://your-storage.com/image2.jpg",
]
```

## Advanced Testing

### Load Testing
Modify `test_performance.py`:
```python
NUM_EMBEDDINGS = 1000  # Increase load
NUM_SEARCHES = 100
CONCURRENT_REQUESTS = 50
```

### Custom Scenarios
Create your own test scenarios:
```python
# Custom test function
async def test_specific_workflow():
    # Your test logic here
    pass
```

## Reporting

### Generate Test Report
```bash
# Run all tests and save output
python docs/test_embedding_service.py > test_report.txt 2>&1

# Generate performance report
python docs/test_performance.py > performance_report.txt 2>&1
```

### Metrics to Track
- Average response times over time
- Success rates by operation type
- Throughput trends
- Resource utilization

## Support

For issues or questions:
1. Check service logs: `docker-compose logs -f`
2. Verify configuration: `cat .env`
3. Test connectivity: `curl http://localhost:8001/health`
4. Review main documentation: `README.md`