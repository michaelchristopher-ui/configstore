.PHONY: help sync etcd-up etcd-down etcd-status ui test test-unit cover lint fmt typecheck check clean

ETCD_URL ?= http://127.0.0.1:2379

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install the project with its dev and UI dependencies
	uv sync --extra dev --extra ui

etcd-up: ## Start the local single-node etcd
	docker compose up -d --wait etcd

etcd-down: ## Stop it, keeping its volume
	docker compose stop etcd

etcd-status: ## Cluster health, database size, and the current revision
	uv run configstore status --etcd $(ETCD_URL)

ui: ## Run the Streamlit console on :8502
	uv run streamlit run ui/app.py --server.port 8502

test: ## Run everything, including the tests that need a live etcd
	CONFIGSTORE_ETCD_URL=$(ETCD_URL) uv run pytest

test-unit: ## Run only the tests that need no etcd
	uv run pytest -m 'not etcd'

cover: ## Run the suite and write an HTML coverage report
	CONFIGSTORE_ETCD_URL=$(ETCD_URL) uv run pytest --cov=configstore --cov-report=html --cov-report=term

lint: ## Lint
	uv run ruff check .

fmt: ## Format and autofix
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Type check in strict mode
	uv run mypy src tests

check: lint typecheck test ## Everything CI runs

clean: ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
