#!/usr/bin/env python
"""Worker script for running the ARQ scheduler."""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from arq import run_worker
from src.infrastructure.scheduler.arq_scheduler import WorkerSettings
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

if __name__ == "__main__":
    # Run the worker with loaded settings
    run_worker(WorkerSettings)