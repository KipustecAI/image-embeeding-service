#!/usr/bin/env python3
"""Test status update workflow."""

import asyncio
import httpx
from uuid import uuid4
from datetime import datetime

MAIN_API = "http://localhost:8000"
EMBEDDING_API = "http://localhost:8001"
API_KEY = "vsk_live_uYgoQg66IJfpK4mgkWXNDDusqbiI7KWieOliw0hXJrI"

# Use an existing evidence image
IMAGE_URL = "https://lookia.mx/api/minio/lucam-assets/evidences/eb293f36-a0b6-40c1-b0d1-63c9287ee11e/images/1756244434140_20250817-225047_000190.jpg"

async def test_status_update():
    """Test that status updates work correctly."""
    
    async with httpx.AsyncClient(timeout=30) as client:
        print("1. Creating new search...")
        
        # Create search
        response = await client.post(
            f"{MAIN_API}/api/v1/image-search/upload",
            headers={"X-API-Key": API_KEY},
            json={
                "image_url": IMAGE_URL,
                "metadata": {
                    "test": "status_update",
                    "timestamp": datetime.now().isoformat()
                }
            }
        )
        
        if response.status_code not in [200, 201]:
            print(f"❌ Failed to create search: Status {response.status_code}")
            print(f"   Response: {response.text}")
            return
        
        search_id = response.json()["id"]
        print(f"✅ Created search: {search_id}")
        print(f"   Initial status: {response.json()['search_status']}")
        
        # Wait a bit
        await asyncio.sleep(1)
        
        print("\n2. Processing search...")
        
        # Trigger processing
        response = await client.post(f"{EMBEDDING_API}/api/v1/process/searches")
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Processed {data['total_processed']} searches")
            print(f"   Successful: {data['successful']}")
            print(f"   Failed: {data['failed']}")
        else:
            print(f"❌ Processing failed: {response.text}")
            return
        
        # Wait for processing to complete
        await asyncio.sleep(1)
        
        print("\n3. Checking final status...")
        
        # Check final status
        response = await client.get(
            f"{MAIN_API}/api/v1/image-search/{search_id}",
            headers={"X-API-Key": API_KEY}
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Final status:")
            print(f"   search_status: {data['search_status']} (expected: 3)")
            print(f"   similarity_status: {data['similarity_status']} (expected: 1 or 2)")
            print(f"   total_matches: {data['total_matches']}")
            
            # Check metadata
            metadata = data.get('search_metadata', {})
            if 'results' in metadata:
                print(f"   metadata.results: {len(metadata['results'])} items")
            else:
                print(f"   metadata.results: NOT FOUND")
            
            # Check Redis
            if data.get('results'):
                print(f"   Redis cache: {len(data['results'].get('results', []))} results")
            
            # Verify success
            if data['search_status'] == 3:
                print("\n✅ SUCCESS: Status was updated correctly!")
            else:
                print(f"\n❌ FAILED: Status is {data['search_status']}, expected 3")
                
        else:
            print(f"❌ Failed to get search: {response.text}")

if __name__ == "__main__":
    asyncio.run(test_status_update())