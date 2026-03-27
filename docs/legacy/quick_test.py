#!/usr/bin/env python3
"""
Quick test script for Image Embedding Service.
Simple tests to verify the service is working correctly.
"""

import asyncio
import httpx
from datetime import datetime
from uuid import uuid4
import sys

# Configuration
EMBEDDING_API = "http://localhost:8001"
MAIN_API = "http://localhost:8000"
API_KEY = "vsk_dev_your_dev_api_key"  # Replace with your API key

# Test image
TEST_IMAGE = "https://picsum.photos/640/480"


async def check_service_health():
    """Check if the embedding service is healthy."""
    print("🔍 Checking service health...")
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{EMBEDDING_API}/health")
            if response.status_code == 200:
                data = response.json()
                print("✅ Service is healthy")
                print(f"   Components: {data.get('components', {})}")
                return True
            else:
                print(f"❌ Service unhealthy: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Cannot connect to service: {e}")
            return False


async def test_embedding():
    """Test creating an embedding."""
    print("\n🔍 Testing embedding creation...")
    
    evidence_id = str(uuid4())
    camera_id = str(uuid4())
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{EMBEDDING_API}/api/v1/embed/evidence",
                json={
                    "evidence_id": evidence_id,
                    "image_url": TEST_IMAGE,
                    "camera_id": camera_id
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Embedding created successfully")
                print(f"   Evidence ID: {evidence_id[:8]}...")
                print(f"   Embedding ID: {data.get('embedding_id', 'N/A')}")
                print(f"   Vector dimension: {data.get('vector_dimension', 'N/A')}")
                return True
            else:
                print(f"❌ Failed to create embedding: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Error: {e}")
            return False


async def test_search():
    """Test similarity search."""
    print("\n🔍 Testing similarity search...")
    
    search_id = str(uuid4())
    user_id = str(uuid4())
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{EMBEDDING_API}/api/v1/search/manual",
                json={
                    "search_id": search_id,
                    "user_id": user_id,
                    "image_url": TEST_IMAGE,
                    "threshold": 0.5,
                    "max_results": 10
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Search completed successfully")
                print(f"   Search ID: {search_id[:8]}...")
                print(f"   Matches found: {data.get('total_matches', 0)}")
                print(f"   Search time: {data.get('search_time_ms', 0):.2f}ms")
                
                # Show top results
                results = data.get('results', [])
                if results:
                    print(f"   Top results:")
                    for i, r in enumerate(results[:3], 1):
                        print(f"     {i}. Score: {r['similarity_score']:.3f}")
                
                return True
            else:
                print(f"❌ Search failed: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Error: {e}")
            return False


async def test_batch_processing():
    """Test batch processing."""
    print("\n🔍 Testing batch processing...")
    
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            # Test evidence batch
            print("   Testing evidence batch...")
            response = await client.post(f"{EMBEDDING_API}/api/v1/process/evidences")
            
            if response.status_code == 200:
                data = response.json()
                print(f"   ✅ Evidences: {data.get('successful', 0)}/{data.get('total_processed', 0)} processed")
            else:
                print(f"   ❌ Evidence batch failed: {response.status_code}")
            
            # Test search batch
            print("   Testing search batch...")
            response = await client.post(f"{EMBEDDING_API}/api/v1/process/searches")
            
            if response.status_code == 200:
                data = response.json()
                print(f"   ✅ Searches: {data.get('successful', 0)}/{data.get('total_processed', 0)} processed")
                return True
            else:
                print(f"   ❌ Search batch failed: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ Error: {e}")
            return False


async def test_statistics():
    """Get service statistics."""
    print("\n🔍 Getting service statistics...")
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{EMBEDDING_API}/api/v1/stats")
            
            if response.status_code == 200:
                data = response.json()
                vdb = data.get("vector_database", {})
                config = data.get("configuration", {})
                
                print(f"✅ Statistics retrieved")
                print(f"   Vector DB:")
                print(f"     Collection: {vdb.get('collection_name', 'N/A')}")
                print(f"     Points: {vdb.get('points_count', 0)}")
                print(f"     Status: {vdb.get('status', 'N/A')}")
                print(f"   Configuration:")
                print(f"     Model: {config.get('model', 'N/A')}")
                print(f"     Device: {config.get('device', 'N/A')}")
                return True
            else:
                print(f"❌ Failed to get stats: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ Error: {e}")
            return False


async def main():
    """Run all quick tests."""
    print("=" * 50)
    print("Image Embedding Service - Quick Test")
    print("=" * 50)
    print(f"API: {EMBEDDING_API}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    # Run tests
    health_ok = await check_service_health()
    
    if not health_ok:
        print("\n⚠️  Service is not healthy. Please check if it's running.")
        print("   Run: docker-compose up -d")
        sys.exit(1)
    
    # Run functional tests
    embed_ok = await test_embedding()
    search_ok = await test_search()
    batch_ok = await test_batch_processing()
    stats_ok = await test_statistics()
    
    # Summary
    print("\n" + "=" * 50)
    print("Test Summary")
    print("=" * 50)
    
    tests = [
        ("Health Check", health_ok),
        ("Embedding", embed_ok),
        ("Search", search_ok),
        ("Batch Processing", batch_ok),
        ("Statistics", stats_ok)
    ]
    
    passed = sum(1 for _, ok in tests if ok)
    total = len(tests)
    
    for name, ok in tests:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{name:20} {status}")
    
    print("=" * 50)
    print(f"Result: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed!")
        sys.exit(0)
    else:
        print(f"⚠️  {total - passed} test(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️  Test interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)