#!/usr/bin/env python3
"""Test recalculation functionality for image searches."""

import asyncio
import httpx
from datetime import datetime
import sys

EMBEDDING_API = "http://localhost:8001"

async def test_recalculation():
    """Test different recalculation modes."""
    
    async with httpx.AsyncClient(timeout=30) as client:
        print("=" * 60)
        print("Image Search Recalculation Test")
        print("=" * 60)
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        
        # Test 1: Get searches eligible for recalculation
        print("\n1. Checking searches eligible for recalculation...")
        response = await client.post(
            f"{EMBEDDING_API}/api/v1/recalculate/searches",
            params={"limit": 5, "force_all": False}
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Recalculation completed")
            print(f"   Total processed: {data['total_processed']}")
            print(f"   Successful: {data['successful']}")
            print(f"   Failed: {data['failed']}")
            print(f"   Mode: {data['mode']}")
        else:
            print(f"❌ Failed: {response.status_code}")
            print(f"   Response: {response.text}")
        
        # Test 2: Recalculate specific search by ID (if you have one)
        specific_id = input("\n2. Enter a specific search ID to recalculate (or press Enter to skip): ").strip()
        
        if specific_id:
            print(f"\nRecalculating specific search: {specific_id}")
            response = await client.post(
                f"{EMBEDDING_API}/api/v1/recalculate/search/{specific_id}"
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Single search recalculation completed")
                # Handle different response formats
                if 'successful' in data:
                    print(f"   Successful: {data['successful']}")
                    print(f"   Failed: {data.get('failed', 0)}")
                if 'total_processed' in data:
                    print(f"   Total processed: {data['total_processed']}")
                if 'mode' in data:
                    print(f"   Mode: {data['mode']}")
                if 'message' in data:
                    print(f"   Message: {data['message']}")
            else:
                print(f"❌ Failed: {response.status_code}")
                print(f"   Response: {response.text}")
        
        # Test 3: Recalculate with hours_old filter
        print("\n3. Testing recalculation with time filter (only searches older than 1 hour)...")
        response = await client.post(
            f"{EMBEDDING_API}/api/v1/recalculate/searches",
            params={"limit": 10, "hours_old": 1}
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Time-filtered recalculation completed")
            print(f"   Total processed: {data['total_processed']}")
            print(f"   Message: {data['message']}")
        else:
            print(f"❌ Failed: {response.status_code}")
            print(f"   Response: {response.text}")
        
        # Test 4: Multiple specific IDs
        print("\n4. Testing batch recalculation with specific IDs...")
        test_ids = input("Enter comma-separated search IDs (or press Enter to skip): ").strip()
        
        if test_ids:
            search_ids = [id.strip() for id in test_ids.split(",")]
            response = await client.post(
                f"{EMBEDDING_API}/api/v1/recalculate/searches",
                params={"search_ids": search_ids}
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Batch recalculation completed")
                if 'total_processed' in data:
                    print(f"   Total processed: {data['total_processed']}")
                if 'successful' in data:
                    print(f"   Successful: {data['successful']}")
                if 'failed' in data:
                    print(f"   Failed: {data['failed']}")
                if data.get('search_ids'):
                    print(f"   Processed IDs: {', '.join(data['search_ids'])}")
                if 'message' in data:
                    print(f"   Message: {data['message']}")
            else:
                print(f"❌ Failed: {response.status_code}")
                print(f"   Response: {response.text}")
        
        print("\n" + "=" * 60)
        print("Test completed!")
        print("=" * 60)

async def test_force_all():
    """Test force_all recalculation (use with caution!)."""
    
    confirm = input("\n⚠️  WARNING: This will recalculate ALL eligible searches. Continue? (yes/no): ")
    
    if confirm.lower() != "yes":
        print("Cancelled.")
        return
    
    async with httpx.AsyncClient(timeout=60) as client:
        print("\nRecalculating ALL eligible searches...")
        response = await client.post(
            f"{EMBEDDING_API}/api/v1/recalculate/searches",
            params={"force_all": True}
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Force-all recalculation completed")
            print("data", data)
            # print(f"✅ Force-all recalculation completed")
            # print(f"   Total processed: {data['total_processed']}")
            # print(f"   Successful: {data['successful']}")
            # print(f"   Failed: {data['failed']}")
        else:
            print(f"❌ Failed: {response.status_code}")
            print(f"   Response: {response.text}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--force-all":
        asyncio.run(test_force_all())
    else:
        asyncio.run(test_recalculation())
        print("\nTip: Use --force-all flag to test recalculating ALL searches")