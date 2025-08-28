#!/usr/bin/env python3
"""Test scheduler configuration for recalculation task."""

from src.infrastructure.config import get_settings
from src.infrastructure.scheduler.arq_scheduler import create_scheduler_settings

def test_scheduler_config():
    """Test and display scheduler configuration."""
    
    settings = get_settings()
    scheduler_config = create_scheduler_settings()
    
    print("=" * 60)
    print("Scheduler Configuration Test")
    print("=" * 60)
    
    print("\n📋 Recalculation Settings:")
    print(f"  - Enabled: {settings.recalculation_enabled}")
    print(f"  - Interval: {settings.recalculation_interval} seconds ({settings.recalculation_interval // 60} minutes)")
    print(f"  - Hours Old: {settings.recalculation_hours_old} hours")
    print(f"  - Batch Size: {settings.recalculation_batch_size} searches")
    
    print("\n⏰ Cron Jobs:")
    for idx, job in enumerate(scheduler_config["cron_jobs"], 1):
        func_name = job.coroutine_func.__name__ if hasattr(job, 'coroutine_func') else str(job)
        print(f"  {idx}. {func_name}")
        
        # Print schedule details if available
        if hasattr(job, 'minute'):
            print(f"     - Minutes: {job.minute}")
        if hasattr(job, 'hour'):
            print(f"     - Hours: {job.hour}")
        if hasattr(job, 'run_at_startup'):
            print(f"     - Run at startup: {job.run_at_startup}")
    
    print("\n🔧 Worker Settings:")
    print(f"  - Max Jobs: {scheduler_config['max_jobs']}")
    print(f"  - Job Timeout: {scheduler_config['job_timeout']} seconds")
    print(f"  - Redis DB: {settings.redis_database}")
    
    print("\n✅ Recalculation Task:")
    if settings.recalculation_enabled:
        interval_minutes = settings.recalculation_interval // 60
        if interval_minutes >= 60:
            hours = interval_minutes // 60
            print(f"  Will run every {hours} hour(s) at minute 15")
            print(f"  Example: 00:15, {hours:02d}:15, {hours*2:02d}:15, ...")
        else:
            print(f"  Will run every {interval_minutes} minutes")
            print(f"  Example: :00, :{interval_minutes:02d}, :{interval_minutes*2:02d}, ...")
            
        print(f"\n  When triggered, will:")
        print(f"  1. Fetch up to {settings.recalculation_batch_size} searches")
        print(f"  2. Only process searches older than {settings.recalculation_hours_old} hours")
        print(f"  3. Only recalculate COMPLETED searches with MATCHES_FOUND")
    else:
        print("  ❌ Recalculation is DISABLED")
        print("  Set RECALCULATION_ENABLED=true to enable automatic recalculation")
    
    print("\n" + "=" * 60)
    print("Configuration test complete!")
    print("=" * 60)

if __name__ == "__main__":
    test_scheduler_config()