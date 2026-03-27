#!/usr/bin/env python3
"""
Comprehensive test script for Image Embedding Service.

This script tests all functionalities of the embedding service including:
- Health checks
- Evidence embedding
- Image similarity search
- Batch processing
- Statistics retrieval
"""

import asyncio
import json
import sys
import time
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import uuid4
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

# Configuration
MAIN_API_URL = "http://localhost:8000"
EMBEDDING_API_URL = "http://localhost:8001"
MAIN_API_KEY = "vsk_dev_your_dev_api_key"  # Replace with your DEV/ROOT API key

# Sample image URLs for testing
SAMPLE_IMAGES = [
    "https://lookia.mx/api/minio/lucam-assets/evidences/026c0347-a3ab-4b28-8a1b-2d15cebe4d32/images/1755812894716_20250821-173727_001139.jpg",
    "https://picsum.photos/640/480",  # Random image service
    "https://via.placeholder.com/640x480",  # Placeholder image
]

console = Console()


class EmbeddingServiceTester:
    """Test client for Image Embedding Service."""
    
    def __init__(self, embedding_api_url: str, main_api_url: str, api_key: str):
        self.embedding_api_url = embedding_api_url
        self.main_api_url = main_api_url
        self.api_key = api_key
        self.headers = {"X-API-Key": api_key}
        
    async def test_health(self) -> bool:
        """Test health check endpoint."""
        console.print("\n[bold blue]Testing: Health Check[/bold blue]")
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # Test embedding service health
                response = await client.get(f"{self.embedding_api_url}/health")
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Embedding Service is healthy[/green]")
                    
                    # Display component status
                    components = data.get("components", {})
                    table = Table(title="Component Status")
                    table.add_column("Component", style="cyan")
                    table.add_column("Status", style="green")
                    
                    for comp, status in components.items():
                        status_icon = "✓" if status else "✗"
                        table.add_row(comp, status_icon)
                    
                    console.print(table)
                    
                    # Display vector DB stats
                    vdb_stats = data.get("vector_db_stats", {})
                    if vdb_stats:
                        console.print(f"  Vector DB: {vdb_stats.get('collection')} - "
                                    f"{vdb_stats.get('points', 0)} points")
                    
                    return True
                else:
                    console.print(f"[red]✗ Health check failed: {response.status_code}[/red]")
                    return False
                    
            except Exception as e:
                console.print(f"[red]✗ Health check error: {e}[/red]")
                return False
    
    async def test_manual_evidence_embedding(self) -> bool:
        """Test manual evidence embedding."""
        console.print("\n[bold blue]Testing: Manual Evidence Embedding[/bold blue]")
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # Generate test data
                evidence_id = str(uuid4())
                camera_id = str(uuid4())
                image_url = SAMPLE_IMAGES[0]
                
                console.print(f"  Evidence ID: {evidence_id[:8]}...")
                console.print(f"  Camera ID: {camera_id[:8]}...")
                console.print(f"  Image URL: {image_url[:50]}...")
                
                # Call embedding endpoint
                response = await client.post(
                    f"{self.embedding_api_url}/api/v1/embed/evidence",
                    json={
                        "evidence_id": evidence_id,
                        "image_url": image_url,
                        "camera_id": camera_id
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Evidence embedded successfully![/green]")
                    console.print(f"  Embedding ID: {data.get('embedding_id')}")
                    console.print(f"  Vector Dimension: {data.get('vector_dimension')}")
                    return True
                else:
                    console.print(f"[red]✗ Embedding failed: {response.status_code}[/red]")
                    console.print(f"  Response: {response.text}")
                    return False
                    
            except Exception as e:
                console.print(f"[red]✗ Embedding error: {e}[/red]")
                return False
    
    async def test_manual_search(self) -> Dict[str, Any]:
        """Test manual image search."""
        console.print("\n[bold blue]Testing: Manual Image Search[/bold blue]")
        
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                # First, embed some test evidences
                console.print("  [dim]Preparing test evidences...[/dim]")
                
                # Embed multiple evidences
                embedded_ids = []
                for i in range(3):
                    evidence_id = str(uuid4())
                    camera_id = str(uuid4())
                    
                    response = await client.post(
                        f"{self.embedding_api_url}/api/v1/embed/evidence",
                        json={
                            "evidence_id": evidence_id,
                            "image_url": SAMPLE_IMAGES[i % len(SAMPLE_IMAGES)],
                            "camera_id": camera_id
                        }
                    )
                    
                    if response.status_code == 200:
                        embedded_ids.append(evidence_id)
                        console.print(f"    Embedded evidence {i+1}/{3}")
                
                # Now perform search
                search_id = str(uuid4())
                user_id = str(uuid4())
                search_image = SAMPLE_IMAGES[0]  # Search with first image
                
                console.print(f"\n  Search ID: {search_id[:8]}...")
                console.print(f"  User ID: {user_id[:8]}...")
                console.print(f"  Searching with: {search_image[:50]}...")
                
                response = await client.post(
                    f"{self.embedding_api_url}/api/v1/search/manual",
                    json={
                        "search_id": search_id,
                        "user_id": user_id,
                        "image_url": search_image,
                        "threshold": 0.5,  # Lower threshold for test
                        "max_results": 10
                    }
                )
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Search completed successfully![/green]")
                    console.print(f"  Total Matches: {data.get('total_matches', 0)}")
                    console.print(f"  Search Time: {data.get('search_time_ms', 0):.2f}ms")
                    
                    # Display results
                    results = data.get('results', [])
                    if results:
                        console.print("\n  Top Results:")
                        for i, result in enumerate(results[:5], 1):
                            console.print(f"    {i}. Evidence: {result['evidence_id'][:8]}... "
                                        f"Score: {result['similarity_score']:.3f}")
                    
                    return data
                else:
                    console.print(f"[red]✗ Search failed: {response.status_code}[/red]")
                    console.print(f"  Response: {response.text}")
                    return {}
                    
            except Exception as e:
                console.print(f"[red]✗ Search error: {e}[/red]")
                return {}
    
    async def test_batch_processing(self) -> bool:
        """Test batch processing endpoints."""
        console.print("\n[bold blue]Testing: Batch Processing[/bold blue]")
        
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                # Test evidence batch processing
                console.print("  Testing evidence batch processing...")
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console
                ) as progress:
                    task = progress.add_task("Processing evidences...", total=None)
                    
                    response = await client.post(
                        f"{self.embedding_api_url}/api/v1/process/evidences"
                    )
                    
                    progress.stop()
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Evidence batch processing completed[/green]")
                    console.print(f"    Total: {data.get('total_processed', 0)}")
                    console.print(f"    Successful: {data.get('successful', 0)}")
                    console.print(f"    Failed: {data.get('failed', 0)}")
                    console.print(f"    Time: {data.get('processing_time_ms', 0):.2f}ms")
                else:
                    console.print(f"[yellow]⚠ Evidence batch processing: {response.status_code}[/yellow]")
                
                # Test search batch processing
                console.print("\n  Testing search batch processing...")
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console
                ) as progress:
                    task = progress.add_task("Processing searches...", total=None)
                    
                    response = await client.post(
                        f"{self.embedding_api_url}/api/v1/process/searches"
                    )
                    
                    progress.stop()
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Search batch processing completed[/green]")
                    console.print(f"    Total: {data.get('total_processed', 0)}")
                    console.print(f"    Successful: {data.get('successful', 0)}")
                    console.print(f"    Failed: {data.get('failed', 0)}")
                    return True
                else:
                    console.print(f"[yellow]⚠ Search batch processing: {response.status_code}[/yellow]")
                    return False
                    
            except Exception as e:
                console.print(f"[red]✗ Batch processing error: {e}[/red]")
                return False
    
    async def test_statistics(self) -> bool:
        """Test statistics endpoint."""
        console.print("\n[bold blue]Testing: Statistics[/bold blue]")
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self.embedding_api_url}/api/v1/stats")
                
                if response.status_code == 200:
                    data = response.json()
                    console.print("[green]✓ Statistics retrieved successfully[/green]")
                    
                    # Display vector database stats
                    vdb = data.get("vector_database", {})
                    if vdb:
                        table = Table(title="Vector Database Statistics")
                        table.add_column("Metric", style="cyan")
                        table.add_column("Value", style="white")
                        
                        table.add_row("Collection", str(vdb.get("collection_name", "N/A")))
                        table.add_row("Points Count", str(vdb.get("points_count", 0)))
                        table.add_row("Vector Size", str(vdb.get("vector_size", 0)))
                        table.add_row("Distance Metric", str(vdb.get("distance_metric", "N/A")))
                        table.add_row("Status", str(vdb.get("status", "N/A")))
                        
                        console.print(table)
                    
                    # Display configuration
                    config = data.get("configuration", {})
                    if config:
                        console.print("\n  Configuration:")
                        console.print(f"    Model: {config.get('model')}")
                        console.print(f"    Device: {config.get('device')}")
                        console.print(f"    Vector Size: {config.get('vector_size')}")
                        console.print(f"    Evidence Batch Size: {config.get('evidence_batch_size')}")
                        console.print(f"    Search Batch Size: {config.get('search_batch_size')}")
                    
                    return True
                else:
                    console.print(f"[red]✗ Statistics failed: {response.status_code}[/red]")
                    return False
                    
            except Exception as e:
                console.print(f"[red]✗ Statistics error: {e}[/red]")
                return False
    
    async def test_end_to_end_flow(self) -> bool:
        """Test complete end-to-end flow through main API."""
        console.print("\n[bold blue]Testing: End-to-End Flow (Main API → Embedding Service)[/bold blue]")
        
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                # Step 1: Create image search through main API
                console.print("\n  1. Creating image search via Main API...")
                
                search_response = await client.post(
                    f"{self.main_api_url}/api/v1/image-search/upload",
                    headers=self.headers,
                    json={
                        "image_url": SAMPLE_IMAGES[0],
                        "metadata": {
                            "test": True,
                            "source": "embedding_service_test",
                            "threshold": 0.7
                        }
                    }
                )
                
                if search_response.status_code != 200:
                    console.print(f"[red]✗ Failed to create search: {search_response.status_code}[/red]")
                    return False
                
                search_data = search_response.json()
                search_id = search_data["id"]
                console.print(f"[green]✓ Created search: {search_id}[/green]")
                
                # Step 2: Trigger processing in embedding service
                console.print("\n  2. Triggering processing in Embedding Service...")
                
                process_response = await client.post(
                    f"{self.embedding_api_url}/api/v1/process/searches"
                )
                
                if process_response.status_code == 200:
                    process_data = process_response.json()
                    console.print(f"[green]✓ Processed {process_data['successful']} searches[/green]")
                
                # Step 3: Check results via main API
                console.print("\n  3. Checking results via Main API...")
                
                # Wait a bit for processing
                await asyncio.sleep(2)
                
                results_response = await client.get(
                    f"{self.main_api_url}/api/v1/image-search/{search_id}",
                    headers=self.headers
                )
                
                if results_response.status_code == 200:
                    results_data = results_response.json()
                    
                    if results_data["search_status"] == 3:  # COMPLETED
                        console.print("[green]✓ Search completed successfully![/green]")
                        console.print(f"    Status: COMPLETED")
                        console.print(f"    Total Matches: {results_data.get('total_matches', 0)}")
                        
                        # Try to get cached results
                        cache_response = await client.get(
                            f"{self.main_api_url}/api/v1/image-search/{search_id}/results",
                            headers=self.headers
                        )
                        
                        if cache_response.status_code == 200:
                            cache_data = cache_response.json()
                            console.print(f"[green]✓ Results cached successfully[/green]")
                            console.print(f"    Cached: {cache_data.get('cached', False)}")
                            console.print(f"    TTL: {cache_data.get('ttl', 0)} seconds")
                        
                        return True
                    else:
                        status_map = {1: "TO_WORK", 2: "IN_PROGRESS", 3: "COMPLETED", 4: "FAILED"}
                        status = status_map.get(results_data["search_status"], "UNKNOWN")
                        console.print(f"[yellow]⚠ Search status: {status}[/yellow]")
                        return False
                else:
                    console.print(f"[red]✗ Failed to get results: {results_response.status_code}[/red]")
                    return False
                    
            except Exception as e:
                console.print(f"[red]✗ End-to-end test error: {e}[/red]")
                return False
    
    async def run_all_tests(self):
        """Run all tests in sequence."""
        console.print(Panel.fit(
            "[bold green]Image Embedding Service Test Suite[/bold green]\n" +
            f"Embedding API: {self.embedding_api_url}\n" +
            f"Main API: {self.main_api_url}",
            title="Configuration"
        ))
        
        start_time = time.time()
        results = {
            "health": False,
            "manual_embedding": False,
            "manual_search": False,
            "batch_processing": False,
            "statistics": False,
            "end_to_end": False
        }
        
        try:
            # Run tests
            results["health"] = await self.test_health()
            
            if results["health"]:
                results["manual_embedding"] = await self.test_manual_evidence_embedding()
                search_result = await self.test_manual_search()
                results["manual_search"] = bool(search_result)
                results["batch_processing"] = await self.test_batch_processing()
                results["statistics"] = await self.test_statistics()
                results["end_to_end"] = await self.test_end_to_end_flow()
            
            # Display summary
            elapsed = time.time() - start_time
            
            console.print("\n" + "=" * 60)
            console.print("[bold]Test Summary[/bold]")
            console.print("=" * 60)
            
            table = Table()
            table.add_column("Test", style="cyan")
            table.add_column("Result", justify="center")
            
            for test, passed in results.items():
                status = "[green]✓ PASSED[/green]" if passed else "[red]✗ FAILED[/red]"
                table.add_row(test.replace("_", " ").title(), status)
            
            console.print(table)
            
            total = len(results)
            passed = sum(1 for v in results.values() if v)
            
            console.print(f"\nTotal: {passed}/{total} tests passed")
            console.print(f"Time: {elapsed:.2f} seconds")
            
            if passed == total:
                console.print("\n[bold green]✓ All tests passed successfully![/bold green]")
            else:
                console.print(f"\n[bold yellow]⚠ {total - passed} test(s) failed[/bold yellow]")
            
        except Exception as e:
            console.print(f"\n[bold red]✗ Test suite failed with error:[/bold red]")
            console.print(f"  {str(e)}")
            raise


async def main():
    """Main entry point."""
    # Check command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--help":
            console.print("Usage: python test_embedding_service.py [API_KEY]")
            console.print("\nTests the Image Embedding Service functionality")
            console.print("\nOptional: Pass API key as argument, otherwise uses default")
            return
        
        # Use provided API key
        api_key = sys.argv[1]
    else:
        api_key = MAIN_API_KEY
    
    tester = EmbeddingServiceTester(
        embedding_api_url=EMBEDDING_API_URL,
        main_api_url=MAIN_API_URL,
        api_key=api_key
    )
    
    await tester.run_all_tests()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Test interrupted by user[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        sys.exit(1)