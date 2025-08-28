.PHONY: help install run-api run-worker run-all docker-up docker-down test clean

help:
	@echo "Available commands:"
	@echo "  make install       - Install dependencies"
	@echo "  make run-api      - Run API server locally"
	@echo "  make run-worker   - Run scheduler worker locally"
	@echo "  make run-all      - Run both API and worker (requires tmux)"
	@echo "  make docker-up    - Start services with Docker Compose"
	@echo "  make docker-down  - Stop Docker services"
	@echo "  make test         - Run tests"
	@echo "  make clean        - Clean cache and temp files"

install:
	pip install -r requirements.txt

run-api:
	python -m src.main

run-worker:
	python worker.py

run-all:
	@if command -v tmux > /dev/null; then \
		tmux new-session -d -s embedding-service; \
		tmux send-keys -t embedding-service "python -m src.main" C-m; \
		tmux split-window -t embedding-service -v; \
		tmux send-keys -t embedding-service "python worker.py" C-m; \
		tmux attach -t embedding-service; \
	else \
		echo "tmux not found. Please install tmux or run services separately."; \
	fi

docker-up:
	docker-compose up -d
	@echo "Services started. API at http://localhost:8001"
	@echo "Qdrant dashboard at http://localhost:6333/dashboard"

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

test:
	pytest tests/ -v

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov