.DEFAULT_GOAL := help
SHELL := /bin/bash
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
OER := $(BIN)/oer

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

$(VENV): ## Create the virtualenv
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

.PHONY: install
install: $(VENV) ## Install the package with all extras
	$(BIN)/pip install -e '.[splink,graph,llm,dev]'
	@echo
	@echo "Optional: the FollowTheMoney extra needs a system libicu."
	@echo "  macOS  : brew install icu4c pkg-config"
	@echo "  Debian : sudo apt-get install libicu-dev pkg-config"
	@echo "  then   : make install-ftm"

.PHONY: install-ftm
install-ftm: ## Install the FollowTheMoney extra (needs libicu)
	$(BIN)/pip install -e '.[ftm]'

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

.PHONY: lint
lint: ## Ruff + mypy
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests
	$(BIN)/mypy

.PHONY: format
format: ## Auto-fix formatting and lint
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

.PHONY: test
test: ## Run the test suite
	$(BIN)/pytest -q

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	$(BIN)/pytest --cov=ownership_er --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint test ## Lint and test

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

.PHONY: fixtures
fixtures: ## Regenerate the committed fixture corpus
	$(OER) make-fixtures --out fixtures --companies 1200 --people 400 --sanctioned 60

.PHONY: data
data: ## Download the real Companies House + OpenSanctions snapshots (~4GB)
	@echo "Downloading real snapshots. Expect ~4GB and 10-30 minutes."
	$(OER) fetch --source all

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

.PHONY: demo
demo: ## Full pipeline on the committed fixtures (~30s, no downloads)
	$(OER) run-all --fixtures --matcher rules

.PHONY: run
run: ## Full pipeline on real downloaded data
	$(OER) run-all --no-fixtures --matcher rules

.PHONY: normalize block match cluster analyse
normalize: ## Parse sources into the record schema
	$(OER) normalize --fixtures
block: ## Generate candidate pairs
	$(OER) block --truth fixtures/ground_truth.json
match: ## Score candidate pairs
	$(OER) match --matcher rules
cluster: ## Build canonical entities
	$(OER) cluster --matcher rules
analyse: ## Run the ownership analyses
	$(OER) analyse

.PHONY: evaluate
evaluate: ## Score every matcher against ground truth
	$(OER) evaluate --truth fixtures/ground_truth.json --matcher rules
	-$(OER) match --matcher splink && $(OER) cluster --matcher splink && \
	  $(OER) evaluate --truth fixtures/ground_truth.json --matcher splink

.PHONY: export
export: ## Export canonical entities as FollowTheMoney JSON
	$(OER) export-ftm --out outputs/entities.ftm.json

# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

.PHONY: neo4j-up
neo4j-up: ## Start Neo4j in Docker
	docker compose up -d neo4j
	@echo "Browser: http://localhost:7474  (neo4j / ownership-dev-password)"

.PHONY: neo4j-down
neo4j-down: ## Stop Neo4j
	docker compose down

.PHONY: load-graph
load-graph: ## Load the resolved graph into Neo4j
	$(OER) load-graph

.PHONY: load-graph-dry
load-graph-dry: ## Write the Cypher payload without a server
	$(OER) load-graph --dry-run

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Remove derived data and caches
	rm -rf data/warehouse data/interim data/processed outputs/eval outputs/analysis
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: clean-all
clean-all: clean ## Also remove downloaded raw data and the virtualenv
	rm -rf data/raw $(VENV)
