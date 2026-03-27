.PHONY: help install run-api run-worker run-all docker-up docker-down test clean migrate

help:
	@echo "Available commands:"
	@echo "  make install          - Install dependencies"
	@echo "  make run-api          - Run API server locally"
	@echo "  make run-worker       - Run ARQ worker locally"
	@echo "  make run-all          - Run both API and worker (requires tmux)"
	@echo "  make docker-up        - Start services with Docker Compose"
	@echo "  make docker-down      - Stop Docker services"
	@echo "  make migrate          - Run database migrations"
	@echo "  make migrate-create   - Create new migration (msg=description)"
	@echo "  make test             - Run tests"
	@echo "  make clean            - Clean cache and temp files"

install:
	pip install -r requirements.txt

run-api:
	python -m src.main

run-worker:
	arq src.workers.main.WorkerSettings

run-all:
	@if command -v tmux > /dev/null; then \
		tmux new-session -d -s embedding-service; \
		tmux send-keys -t embedding-service "make run-api" C-m; \
		tmux split-window -t embedding-service -v; \
		tmux send-keys -t embedding-service "make run-worker" C-m; \
		tmux attach -t embedding-service; \
	else \
		echo "tmux not found. Please install tmux or run services separately."; \
	fi

docker-up:
	docker-compose up -d
	@echo "Services started:"
	@echo "  PostgreSQL at localhost:5433"
	@echo "  Redis at localhost:6379"
	@echo "  Qdrant dashboard at http://localhost:6333/dashboard"

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

# Database migrations
migrate:
	alembic upgrade head

migrate-create:
	alembic revision --autogenerate -m "$(msg)"

migrate-downgrade:
	alembic downgrade -1

migrate-history:
	alembic history

# Testing
test:
	pytest tests/ -v

test-single:
	pytest tests/ -v -k "$(t)"

# Code quality
lint:
	ruff check src/ tests/

format:
	ruff check --fix src/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage htmlcov
