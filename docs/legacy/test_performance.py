#!/usr/bin/env python3
"""
Performance testing script for Image Embedding Service.

Tests performance metrics including:
- Embedding generation speed
- Vector search performance
- Batch processing throughput
- Concurrent request handling
"""

import asyncio
import time
import statistics
from typing import List, Dict, Any
from datetime import datetime
from uuid import uuid4
import random

import httpx
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, BarColumn, TimeRemainingColumn, TextColumn
from rich.panel import Panel
from rich import print as rprint

# Configuration
EMBEDDING_API_URL = "http://localhost:8001"
MAIN_API_URL = "http://localhost:8000"
API_KEY = "vsk_dev_your_dev_api_key"

# Test parameters
NUM_EMBEDDINGS = 100  # Number of embeddings to create
NUM_SEARCHES = 20     # Number of searches to perform
CONCURRENT_REQUESTS = 10  # Concurrent requests for stress testing

# Sample images (you can add more URLs)
TEST_IMAGES = [
    f"https://picsum.photos/640/480?random={i}" for i in range(50)
]

console = Console()


class PerformanceTester:
    """Performance tester for Image Embedding Service."""
    
    def __init__(self, api_url: str):
        self.api_url = api_url
        self.metrics = {
            "embedding_times": [],
            "search_times": [],
            "batch_times": [],
            "concurrent_times": []
        }
    
    async def test_single_embedding_speed(self, num_tests: int = 10) -> Dict[str, float]:
        """Test single embedding generation speed."""
        console.print("\n[bold blue]Testing: Single Embedding Speed[/bold blue]")
        
        times = []
        errors = 0
        
        async with httpx.AsyncClient(timeout=60) as client:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.0f}%",
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("Testing embeddings...", total=num_tests)
                
                for i in range(num_tests):
                    evidence_id = str(uuid4())
                    camera_id = str(uuid4())
                    image_url = random.choice(TEST_IMAGES)
                    
                    start_time = time.time()
                    try:
                        response = await client.post(
                            f"{self.api_url}/api/v1/embed/evidence",
                            json={
                                "evidence_id": evidence_id,
                                "image_url": image_url,
                                "camera_id": camera_id
                            }
                        )
                        
                        if response.status_code == 200:
                            elapsed = (time.time() - start_time) * 1000  # Convert to ms
                            times.append(elapsed)
                        else:
                            errors += 1
                    except:
                        errors += 1
                    
                    progress.update(task, advance=1)
        
        if times:
            metrics = {
                "min": min(times),
                "max": max(times),
                "mean": statistics.mean(times),
                "median": statistics.median(times),
                "stdev": statistics.stdev(times) if len(times) > 1 else 0,
                "success_rate": (len(times) / num_tests) * 100
            }
            
            self.metrics["embedding_times"] = times
            
            # Display results
            table = Table(title="Embedding Performance (ms)")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", justify="right")
            
            table.add_row("Min", f"{metrics['min']:.2f}")
            table.add_row("Max", f"{metrics['max']:.2f}")
            table.add_row("Mean", f"{metrics['mean']:.2f}")
            table.add_row("Median", f"{metrics['median']:.2f}")
            table.add_row("Std Dev", f"{metrics['stdev']:.2f}")
            table.add_row("Success Rate", f"{metrics['success_rate']:.1f}%")
            
            console.print(table)
            return metrics
        else:
            console.print("[red]✗ All embedding tests failed[/red]")
            return {}
    
    async def test_search_performance(self, num_tests: int = 10) -> Dict[str, float]:
        """Test search performance."""
        console.print("\n[bold blue]Testing: Search Performance[/bold blue]")
        
        # First, ensure we have some embeddings
        console.print("  [dim]Preparing test embeddings...[/dim]")
        async with httpx.AsyncClient(timeout=60) as client:
            for i in range(20):
                await client.post(
                    f"{self.api_url}/api/v1/embed/evidence",
                    json={
                        "evidence_id": str(uuid4()),
                        "image_url": TEST_IMAGES[i % len(TEST_IMAGES)],
                        "camera_id": str(uuid4())
                    }
                )
        
        times = []
        match_counts = []
        errors = 0
        
        async with httpx.AsyncClient(timeout=60) as client:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                "[progress.percentage]{task.percentage:>3.0f}%",
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("Testing searches...", total=num_tests)
                
                for i in range(num_tests):
                    search_id = str(uuid4())
                    user_id = str(uuid4())
                    image_url = random.choice(TEST_IMAGES)
                    
                    start_time = time.time()
                    try:
                        response = await client.post(
                            f"{self.api_url}/api/v1/search/manual",
                            json={
                                "search_id": search_id,
                                "user_id": user_id,
                                "image_url": image_url,
                                "threshold": 0.5,
                                "max_results": 50
                            }
                        )
                        
                        if response.status_code == 200:
                            elapsed = (time.time() - start_time) * 1000
                            times.append(elapsed)
                            data = response.json()
                            match_counts.append(data.get("total_matches", 0))
                        else:
                            errors += 1
                    except:
                        errors += 1
                    
                    progress.update(task, advance=1)
        
        if times:
            metrics = {
                "min": min(times),
                "max": max(times),
                "mean": statistics.mean(times),
                "median": statistics.median(times),
                "stdev": statistics.stdev(times) if len(times) > 1 else 0,
                "avg_matches": statistics.mean(match_counts) if match_counts else 0,
                "success_rate": (len(times) / num_tests) * 100
            }
            
            self.metrics["search_times"] = times
            
            # Display results
            table = Table(title="Search Performance (ms)")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", justify="right")
            
            table.add_row("Min", f"{metrics['min']:.2f}")
            table.add_row("Max", f"{metrics['max']:.2f}")
            table.add_row("Mean", f"{metrics['mean']:.2f}")
            table.add_row("Median", f"{metrics['median']:.2f}")
            table.add_row("Std Dev", f"{metrics['stdev']:.2f}")
            table.add_row("Avg Matches", f"{metrics['avg_matches']:.1f}")
            table.add_row("Success Rate", f"{metrics['success_rate']:.1f}%")
            
            console.print(table)
            return metrics
        else:
            console.print("[red]✗ All search tests failed[/red]")
            return {}
    
    async def test_batch_processing(self) -> Dict[str, Any]:
        """Test batch processing performance."""
        console.print("\n[bold blue]Testing: Batch Processing Performance[/bold blue]")
        
        results = {}
        
        async with httpx.AsyncClient(timeout=120) as client:
            # Test evidence batch
            console.print("  Testing evidence batch processing...")
            
            start_time = time.time()
            response = await client.post(f"{self.api_url}/api/v1/process/evidences")
            evidence_time = (time.time() - start_time) * 1000
            
            if response.status_code == 200:
                data = response.json()
                results["evidence_batch"] = {
                    "time_ms": evidence_time,
                    "total": data.get("total_processed", 0),
                    "successful": data.get("successful", 0),
                    "failed": data.get("failed", 0),
                    "throughput": (data.get("total_processed", 0) / evidence_time) * 1000 if evidence_time > 0 else 0
                }
                
                console.print(f"    Time: {evidence_time:.2f}ms")
                console.print(f"    Throughput: {results['evidence_batch']['throughput']:.2f} items/sec")
            
            # Test search batch
            console.print("\n  Testing search batch processing...")
            
            start_time = time.time()
            response = await client.post(f"{self.api_url}/api/v1/process/searches")
            search_time = (time.time() - start_time) * 1000
            
            if response.status_code == 200:
                data = response.json()
                results["search_batch"] = {
                    "time_ms": search_time,
                    "total": data.get("total_processed", 0),
                    "successful": data.get("successful", 0),
                    "failed": data.get("failed", 0),
                    "throughput": (data.get("total_processed", 0) / search_time) * 1000 if search_time > 0 else 0
                }
                
                console.print(f"    Time: {search_time:.2f}ms")
                console.print(f"    Throughput: {results['search_batch']['throughput']:.2f} items/sec")
        
        return results
    
    async def test_concurrent_requests(self, num_concurrent: int = 10) -> Dict[str, Any]:
        """Test concurrent request handling."""
        console.print(f"\n[bold blue]Testing: Concurrent Requests ({num_concurrent} simultaneous)[/bold blue]")
        
        async def make_embedding_request(client: httpx.AsyncClient) -> float:
            """Make a single embedding request."""
            start_time = time.time()
            try:
                response = await client.post(
                    f"{self.api_url}/api/v1/embed/evidence",
                    json={
                        "evidence_id": str(uuid4()),
                        "image_url": random.choice(TEST_IMAGES),
                        "camera_id": str(uuid4())
                    }
                )
                if response.status_code == 200:
                    return (time.time() - start_time) * 1000
            except:
                pass
            return -1
        
        async with httpx.AsyncClient(timeout=60) as client:
            # Launch concurrent requests
            console.print(f"  Launching {num_concurrent} concurrent requests...")
            
            start_time = time.time()
            tasks = [make_embedding_request(client) for _ in range(num_concurrent)]
            results = await asyncio.gather(*tasks)
            total_time = (time.time() - start_time) * 1000
            
            # Filter successful requests
            successful_times = [r for r in results if r > 0]
            
            if successful_times:
                metrics = {
                    "total_time": total_time,
                    "concurrent_requests": num_concurrent,
                    "successful": len(successful_times),
                    "failed": num_concurrent - len(successful_times),
                    "avg_response_time": statistics.mean(successful_times),
                    "requests_per_second": (len(successful_times) / total_time) * 1000
                }
                
                # Display results
                table = Table(title="Concurrent Request Performance")
                table.add_column("Metric", style="cyan")
                table.add_column("Value", justify="right")
                
                table.add_row("Total Time", f"{metrics['total_time']:.2f} ms")
                table.add_row("Concurrent Requests", str(metrics['concurrent_requests']))
                table.add_row("Successful", str(metrics['successful']))
                table.add_row("Failed", str(metrics['failed']))
                table.add_row("Avg Response Time", f"{metrics['avg_response_time']:.2f} ms")
                table.add_row("Requests/Second", f"{metrics['requests_per_second']:.2f}")
                
                console.print(table)
                return metrics
            else:
                console.print("[red]✗ All concurrent requests failed[/red]")
                return {}
    
    async def test_vector_db_scaling(self) -> Dict[str, Any]:
        """Test vector database performance with increasing data."""
        console.print("\n[bold blue]Testing: Vector Database Scaling[/bold blue]")
        
        # Get initial stats
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.api_url}/api/v1/stats")
            if response.status_code == 200:
                data = response.json()
                initial_points = data.get("vector_database", {}).get("points_count", 0)
                console.print(f"  Initial points in database: {initial_points}")
            else:
                initial_points = 0
        
        # Test search performance at different database sizes
        test_sizes = [100, 500, 1000]
        results = []
        
        async with httpx.AsyncClient(timeout=120) as client:
            for target_size in test_sizes:
                console.print(f"\n  Testing with ~{target_size} points...")
                
                # Add embeddings to reach target size
                current_size = initial_points + sum(r.get("added", 0) for r in results)
                to_add = max(0, target_size - current_size)
                
                if to_add > 0:
                    console.print(f"    Adding {to_add} embeddings...")
                    for i in range(to_add):
                        await client.post(
                            f"{self.api_url}/api/v1/embed/evidence",
                            json={
                                "evidence_id": str(uuid4()),
                                "image_url": TEST_IMAGES[i % len(TEST_IMAGES)],
                                "camera_id": str(uuid4())
                            }
                        )
                
                # Test search performance
                search_times = []
                for _ in range(5):
                    start_time = time.time()
                    response = await client.post(
                        f"{self.api_url}/api/v1/search/manual",
                        json={
                            "search_id": str(uuid4()),
                            "user_id": str(uuid4()),
                            "image_url": random.choice(TEST_IMAGES),
                            "threshold": 0.5,
                            "max_results": 50
                        }
                    )
                    if response.status_code == 200:
                        search_times.append((time.time() - start_time) * 1000)
                
                if search_times:
                    result = {
                        "target_size": target_size,
                        "added": to_add,
                        "avg_search_time": statistics.mean(search_times),
                        "min_search_time": min(search_times),
                        "max_search_time": max(search_times)
                    }
                    results.append(result)
                    console.print(f"    Avg search time: {result['avg_search_time']:.2f}ms")
        
        # Display scaling results
        if results:
            table = Table(title="Vector DB Scaling Performance")
            table.add_column("DB Size", style="cyan", justify="right")
            table.add_column("Avg Search (ms)", justify="right")
            table.add_column("Min (ms)", justify="right")
            table.add_column("Max (ms)", justify="right")
            
            for r in results:
                table.add_row(
                    str(r["target_size"]),
                    f"{r['avg_search_time']:.2f}",
                    f"{r['min_search_time']:.2f}",
                    f"{r['max_search_time']:.2f}"
                )
            
            console.print(table)
        
        return {"scaling_results": results}
    
    async def run_all_tests(self):
        """Run all performance tests."""
        console.print(Panel.fit(
            "[bold green]Image Embedding Service Performance Tests[/bold green]\n" +
            f"API URL: {self.api_url}\n" +
            f"Test Images: {len(TEST_IMAGES)}",
            title="Configuration"
        ))
        
        start_time = time.time()
        
        try:
            # Run performance tests
            embedding_metrics = await self.test_single_embedding_speed(NUM_EMBEDDINGS)
            search_metrics = await self.test_search_performance(NUM_SEARCHES)
            batch_metrics = await self.test_batch_processing()
            concurrent_metrics = await self.test_concurrent_requests(CONCURRENT_REQUESTS)
            scaling_metrics = await self.test_vector_db_scaling()
            
            # Generate summary report
            elapsed = time.time() - start_time
            
            console.print("\n" + "=" * 60)
            console.print("[bold]Performance Test Summary[/bold]")
            console.print("=" * 60)
            
            # Summary statistics
            summary = Table(title="Overall Performance Metrics")
            summary.add_column("Category", style="cyan")
            summary.add_column("Metric", style="white")
            summary.add_column("Value", justify="right")
            
            if embedding_metrics:
                summary.add_row(
                    "Embedding",
                    "Avg Time",
                    f"{embedding_metrics['mean']:.2f} ms"
                )
                summary.add_row(
                    "",
                    "Throughput",
                    f"{1000 / embedding_metrics['mean']:.1f} items/sec"
                )
            
            if search_metrics:
                summary.add_row(
                    "Search",
                    "Avg Time",
                    f"{search_metrics['mean']:.2f} ms"
                )
                summary.add_row(
                    "",
                    "Throughput",
                    f"{1000 / search_metrics['mean']:.1f} searches/sec"
                )
            
            if concurrent_metrics:
                summary.add_row(
                    "Concurrency",
                    "Requests/Sec",
                    f"{concurrent_metrics.get('requests_per_second', 0):.1f}"
                )
            
            console.print(summary)
            
            console.print(f"\nTotal test time: {elapsed:.2f} seconds")
            console.print("\n[bold green]✓ Performance tests completed![/bold green]")
            
        except Exception as e:
            console.print(f"\n[bold red]✗ Performance tests failed:[/bold red]")
            console.print(f"  {str(e)}")
            raise


async def main():
    """Main entry point."""
    tester = PerformanceTester(EMBEDDING_API_URL)
    await tester.run_all_tests()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Test interrupted by user[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/red]")
        import traceback
        traceback.print_exc()