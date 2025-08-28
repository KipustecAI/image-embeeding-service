#!/usr/bin/env python3
"""
Test script for the complete similarity workflow.
This script demonstrates the full process of:
1. Processing evidence images to create embeddings
2. Uploading a search image
3. Running similarity search
4. Checking the results
"""

import asyncio
import httpx
import json
import time
from uuid import uuid4
from datetime import datetime
import sys

# Configuration
MAIN_API = "http://localhost:8000"
EMBEDDING_API = "http://localhost:8001"
API_KEY = "vsk_live_uYgoQg66IJfpK4mgkWXNDDusqbiI7KWieOliw0hXJrI"

# Sample evidence from your database
SAMPLE_EVIDENCE = {
    "id": "eb293f36-a0b6-40c1-b0d1-63c9287ee11e",
    "camera_id": "bc5351e8-c25c-44e2-b93d-c6eab8372a0f",
    "status": 3,  # FOUND - ready for embedding
    "images": [
        "https://lookia.mx/api/minio/lucam-assets/evidences/eb293f36-a0b6-40c1-b0d1-63c9287ee11e/images/1756244434140_20250817-225047_000190.jpg",
        "https://lookia.mx/api/minio/lucam-assets/evidences/eb293f36-a0b6-40c1-b0d1-63c9287ee11e/images/1756244437226_20250817-225047_000184.jpg",
        "https://lookia.mx/api/minio/lucam-assets/evidences/eb293f36-a0b6-40c1-b0d1-63c9287ee11e/images/1756244440389_20250817-225051_000320.jpg"
    ]
}


async def check_services():
    """Check if both services are running."""
    print("🔍 Checking services...")
    
    async with httpx.AsyncClient(timeout=10) as client:
        # Check main API
        try:
            response = await client.get(f"{MAIN_API}/", headers={"X-API-Key": API_KEY})
            if response.status_code == 200:
                print("✅ Main API is running")
            else:
                print(f"❌ Main API returned {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Cannot connect to Main API: {e}")
            return False
        
        # Check embedding service
        try:
            response = await client.get(f"{EMBEDDING_API}/health")
            if response.status_code == 200:
                print("✅ Embedding Service is running")
                data = response.json()
                print(f"   Components: {data.get('components', {})}")
                if 'vector_db_stats' in data:
                    stats = data['vector_db_stats']
                    print(f"   Vector DB: {stats.get('points', 0)} points in collection")
            else:
                print(f"❌ Embedding Service returned {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Cannot connect to Embedding Service: {e}")
            return False
    
    return True


async def trigger_evidence_processing():
    """Manually trigger evidence processing."""
    print("\n📦 Triggering evidence processing...")
    
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(f"{EMBEDDING_API}/api/v1/process/evidences")
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Evidence processing completed")
                print(f"   Processed: {data.get('total_processed', 0)}")
                print(f"   Successful: {data.get('successful', 0)}")
                print(f"   Failed: {data.get('failed', 0)}")
                
                if data.get('embedded_ids'):
                    print(f"   Embedded IDs: {data['embedded_ids'][:3]}...")  # Show first 3
                
                return data.get('successful', 0) > 0
            else:
                print(f"❌ Failed to process evidences: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
                
        except Exception as e:
            print(f"❌ Error processing evidences: {e}")
            return False


async def upload_search_image(image_url: str):
    """Upload an image for similarity search."""
    print(f"\n🔍 Uploading search image...")
    print(f"   URL: {image_url}")
    
    user_id = str(uuid4())  # Generate a test user ID
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # Upload the image search request
            response = await client.post(
                f"{MAIN_API}/api/v1/image-search/upload",
                headers={"X-API-Key": API_KEY},
                json={
                    "user_id": user_id,
                    "image_url": image_url,
                    "metadata": {
                        "source": "test_script",
                        "test_id": str(uuid4()),
                        "timestamp": datetime.utcnow().isoformat(),
                        "threshold": 0.5,  # Lower threshold to find more matches
                        "max_results": 100
                    }
                }
            )
            
            if response.status_code in [200, 201]:
                data = response.json()
                search_id = data.get("id")
                print(f"✅ Search uploaded successfully")
                print(f"   Search ID: {search_id}")
                print(f"   Status: {data.get('search_status', 'N/A')}")
                return search_id
            else:
                print(f"❌ Failed to upload search: {response.status_code}")
                print(f"   Response: {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ Error uploading search: {e}")
            return None


async def trigger_search_processing():
    """Manually trigger search processing."""
    print("\n🔎 Triggering search processing...")
    
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(f"{EMBEDDING_API}/api/v1/process/searches")
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Search processing completed")
                print(f"   Processed: {data.get('total_processed', 0)}")
                print(f"   Successful: {data.get('successful', 0)}")
                print(f"   Failed: {data.get('failed', 0)}")
                
                return data.get('successful', 0) > 0
            else:
                print(f"❌ Failed to process searches: {response.status_code}")
                return False
                
        except Exception as e:
            print(f"❌ Error processing searches: {e}")
            return False


async def get_search_results(search_id: str):
    """Get the results of a similarity search."""
    print(f"\n📊 Getting search results for {search_id}...")
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            # First, check the search status
            response = await client.get(
                f"{MAIN_API}/api/v1/image-search/{search_id}",
                headers={"X-API-Key": API_KEY}
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"   Search Status: {data.get('search_status', 'N/A')}")
                print(f"   Similarity Status: {data.get('similarity_status', 'N/A')}")
                print(f"   Total Matches in DB: {data.get('total_matches', 0)}")
                
                # Check if metadata contains results
                if data.get('metadata'):
                    metadata = data['metadata']
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)
                    
                    if 'results' in metadata:
                        print(f"\n   📋 Results in metadata:")
                        for idx, result in enumerate(metadata['results'][:5], 1):  # Show top 5
                            print(f"      {idx}. Evidence: {result.get('evidence_id', 'N/A')}")
                            print(f"         Score: {result.get('similarity_score', 0):.4f}")
                            print(f"         Camera: {result.get('camera_id', 'N/A')}")
            
            # Now get results from Redis
            response = await client.get(
                f"{MAIN_API}/api/v1/image-search/{search_id}/results",
                headers={"X-API-Key": API_KEY}
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"\n✅ Results retrieved from Redis")
                print(f"   Total Matches: {data.get('total_matches', 0)}")
                print(f"   Cached: {data.get('cached', False)}")
                print(f"   TTL: {data.get('ttl', 0)} seconds")
                
                results = data.get('results', [])
                if results:
                    print(f"\n   🎯 Top similarity matches:")
                    for idx, result in enumerate(results[:10], 1):  # Show top 10
                        print(f"      {idx}. Evidence: {result.get('evidence_id', 'N/A')}")
                        print(f"         Score: {result.get('similarity_score', 0):.4f}")
                        print(f"         Image: {result.get('image_url', 'N/A')[:50]}...")
                        print(f"         Camera: {result.get('camera_id', 'N/A')}")
                else:
                    print("   ⚠️  No matches found")
                
                return results
            else:
                print(f"❌ Failed to get results: {response.status_code}")
                print(f"   Response: {response.text}")
                return []
                
        except Exception as e:
            print(f"❌ Error getting results: {e}")
            return []


async def check_vector_stats():
    """Check vector database statistics."""
    print("\n📈 Vector Database Statistics:")
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{EMBEDDING_API}/api/v1/stats")
            
            if response.status_code == 200:
                data = response.json()
                vdb = data.get("vector_database", {})
                config = data.get("configuration", {})
                
                print(f"   Collection: {vdb.get('collection_name', 'N/A')}")
                print(f"   Points: {vdb.get('points_count', 0)}")
                print(f"   Status: {vdb.get('status', 'N/A')}")
                print(f"   Model: {config.get('model', 'N/A')}")
                print(f"   Vector Size: {config.get('vector_size', 'N/A')}")
                
                return vdb.get('points_count', 0)
            else:
                print(f"❌ Failed to get stats: {response.status_code}")
                return 0
                
        except Exception as e:
            print(f"❌ Error getting stats: {e}")
            return 0


async def main():
    """Run the complete test workflow."""
    print("=" * 60)
    print("Image Embedding Service - Similarity Workflow Test")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Evidence ID: {SAMPLE_EVIDENCE['id']}")
    print(f"Evidence Images: {len(SAMPLE_EVIDENCE['images'])}")
    print("=" * 60)
    
    # Step 1: Check services
    if not await check_services():
        print("\n⚠️  Services are not ready. Please start them first.")
        sys.exit(1)
    
    # Step 2: Check initial vector stats
    initial_points = await check_vector_stats()
    
    # Step 3: Process evidences to create embeddings
    print("\n" + "=" * 60)
    print("PHASE 1: Evidence Embedding")
    print("=" * 60)
    
    if await trigger_evidence_processing():
        print("✅ Evidence embeddings created")
        await asyncio.sleep(2)  # Wait a bit for processing
        
        # Check new vector stats
        new_points = await check_vector_stats()
        print(f"   New embeddings added: {new_points - initial_points}")
    else:
        print("⚠️  No evidences were embedded")
    
    # Step 4: Upload a search image (use one of the evidence images)
    print("\n" + "=" * 60)
    print("PHASE 2: Image Search")
    print("=" * 60)
    
    test_image = SAMPLE_EVIDENCE['images'][0]  # Use first evidence image
    search_id = await upload_search_image(test_image)
    
    if not search_id:
        print("❌ Failed to create search request")
        sys.exit(1)
    
    # Step 5: Process the search
    await asyncio.sleep(2)  # Wait a bit
    if await trigger_search_processing():
        print("✅ Search processing completed")
    else:
        print("⚠️  Search processing may have failed")
    
    # Step 6: Get and display results
    print("\n" + "=" * 60)
    print("PHASE 3: Results Analysis")
    print("=" * 60)
    
    results = await get_search_results(search_id)
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    if results:
        print(f"✅ Found {len(results)} similar images!")
        print(f"   Best match score: {results[0].get('similarity_score', 0):.4f}")
        print("\n🎉 Similarity search is working correctly!")
    else:
        print("⚠️  No similar images found")
        print("\nPossible reasons:")
        print("- No evidence embeddings in vector DB")
        print("- Similarity threshold too high")
        print("- Evidence images not accessible")
        print("\nTry running the test again or check the logs.")
    
    print("\n" + "=" * 60)
    print("Test completed!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️  Test interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)