#!/usr/bin/env python
"""ARQ storage worker — stores vectors in Qdrant and executes searches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from arq import run_worker
from src.workers.main import WorkerSettings

if __name__ == "__main__":
    run_worker(WorkerSettings)
