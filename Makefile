.PHONY: help install run run-api run-worker run-all docker-up docker-down test clean migrate \
       lint lint-fix format format-check test-cov pre-commit-install pre-commit-run \
       build-image ci-local ci-lint ci-test ci-migrate ci-build ci-clean

help:
	@echo "=== Image Embedding Backend ==="
	@echo ""
	@echo "Startup order:"
	@echo "  1. make docker-up       - Start PostgreSQL + Redis + Qdrant"
	@echo "  2. make migrate         - Run database migrations"
	@echo "  3. make run-api         - Start API server (terminal 1)"
	@echo "  4. make run-worker      - Start ARQ storage worker (terminal 2)"
	@echo ""
	@echo "Development:"
	@echo "  make install            - Install dependencies"
	@echo "  make run-all            - Run API + worker in tmux"
	@echo "  make test               - Run integration tests"
	@echo "  make test-cov           - Run tests with coverage report"
	@echo "  make lint               - Ruff check"
	@echo "  make lint-fix           - Ruff check with auto-fix"
	@echo "  make format             - Ruff format"
	@echo "  make format-check       - Check formatting (no changes)"
	@echo "  make pre-commit-install - Install pre-commit hooks"
	@echo ""
	@echo "CI (Docker-based, mirrors GitHub Actions):"
	@echo "  make ci-local           - Full CI pipeline (lint + test + migrate + build)"
	@echo "  make ci-lint            - Ruff checks in Docker"
	@echo "  make ci-test            - Pytest with services in Docker"
	@echo "  make ci-migrate         - Alembic migration validation in Docker"
	@echo "  make ci-build           - Verify production image builds"
	@echo "  make ci-clean           - Remove CI containers and volumes"
	@echo ""
	@echo "Other:"
	@echo "  make docker-down        - Stop Docker services"
	@echo "  make migrate-create     - Create new migration (msg=description)"
	@echo "  make build-image        - Build production Docker image"
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

lint-fix:
	ruff check --fix src/ tests/

format:
	ruff format src/ tests/

format-check:
	ruff format --check src/ tests/

pre-commit-install:
	pre-commit install

pre-commit-run:
	pre-commit run --all-files

# --- Coverage ---

test-cov:
	pytest tests/ -v --tb=short --cov=src --cov-report=term-missing --cov-report=html

# --- Docker build ---

build-image:
	docker build -t lucam/image-embeeding-api:latest .

# --- CI (Docker-based, mirrors GitHub Actions) ---

ci-local: ci-lint ci-test ci-migrate ci-build
	@echo ""
	@echo "✓ Full CI pipeline passed"

ci-lint:
	@echo "=== CI: Lint ==="
	docker compose -f docker-compose.ci.yml run --rm ci-lint

ci-test:
	@echo "=== CI: Test ==="
	docker compose -f docker-compose.ci.yml run --rm ci-test

ci-migrate:
	@echo "=== CI: Migrations ==="
	docker compose -f docker-compose.ci.yml run --rm ci-migrate

ci-build:
	@echo "=== CI: Docker Build ==="
	docker build -t lucam/image-embeeding-api:ci-local .
	@echo "✓ Production image builds successfully"

ci-clean:
	docker compose -f docker-compose.ci.yml down -v --remove-orphans

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage htmlcov
