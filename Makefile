.PHONY: help install run run-api run-worker run-all docker-up docker-down test clean migrate

help:
	@echo "=== Image Embedding Backend ==="
	@echo ""
	@echo "Startup order:"
	@echo "  1. make docker-up       - Start PostgreSQL + Redis + Qdrant"
	@echo "  2. make migrate         - Run database migrations"
	@echo "  3. make run-api         - Start API server (terminal 1)"
	@echo "  4. make run-worker      - Start ARQ storage worker (terminal 2)"
	@echo ""
	@echo "Commands:"
	@echo "  make install            - Install dependencies"
	@echo "  make run-all            - Run API + worker in tmux"
	@echo "  make test               - Run integration tests"
	@echo "  make test-pipeline      - Run full pipeline test with example payload"
	@echo "  make docker-down        - Stop Docker services"
	@echo "  make migrate-create     - Create new migration (msg=description)"
	@echo "  make clean              - Clean cache and temp files"

install:
	pip install -r requirements.txt

# --- Services ---

docker-up:
	docker compose up -d
	@echo ""
	@echo "Services started:"
	@echo "  PostgreSQL   localhost:5433"
	@echo "  Redis        localhost:6379"
	@echo "  Qdrant       http://localhost:6333/dashboard"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# --- Run ---

run: run-all

run-api:
	python -m src.main

run-worker:
	arq src.workers.main.WorkerSettings

run-all:
	@if command -v tmux > /dev/null; then \
		tmux new-session -d -s embedding-backend; \
		tmux send-keys -t embedding-backend "make run-api" C-m; \
		tmux split-window -t embedding-backend -v; \
		tmux send-keys -t embedding-backend "make run-worker" C-m; \
		tmux attach -t embedding-backend; \
	else \
		echo "tmux not found. Run in two terminals: make run-api + make run-worker"; \
	fi

# --- Database ---

migrate:
	alembic upgrade head

migrate-create:
	alembic revision --autogenerate -m "$(msg)"

migrate-downgrade:
	alembic downgrade -1

migrate-history:
	alembic history

# --- Testing ---

test:
	pytest tests/ -v

test-single:
	pytest tests/ -v -k "$(t)"

test-pipeline:
	./scripts/test_local_pipeline.sh

# --- Code quality ---

lint:
	ruff check src/ tests/

format:
	ruff check --fix src/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage htmlcov
