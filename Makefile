VENV_DIR := .venv
ACTIVATE_SCRIPT := $(VENV_DIR)/bin/activate
PKG := db_sync_comparator

.PHONY: help venv install activate shell clean lint format typecheck test test-db check

help:
	@echo ""
	@echo "Makefile commands:"
	@echo ""
	@echo "  make venv       - Create a virtual environment in $(VENV_DIR) (uv)"
	@echo "  make install    - Create the venv and install dev dependencies"
	@echo "  make activate   - Print how to activate the venv manually"
	@echo "  make shell      - Spawn a new shell with the venv activated"
	@echo "  make lint       - ruff check"
	@echo "  make format     - ruff format"
	@echo "  make typecheck  - mypy"
	@echo "  make test       - pytest (DB-free unit tests)"
	@echo "  make test-db    - pytest -m fixture (end-to-end tests; needs PostgreSQL)"
	@echo "  make check      - lint + format --check + typecheck + test (what CI runs)"
	@echo "  make clean      - Remove the virtual environment and caches"
	@echo ""

venv:
	@echo "🔧 Creating virtual environment in $(VENV_DIR)..."
	uv venv $(VENV_DIR)

install: venv
	@echo "📦 Installing dev dependencies..."
	uv pip install -r requirements-dev.txt
	uv pip install -e .
	@echo
	@echo "✅ Installed. Spawning a shell inside the virtual environment..."
	@$(MAKE) shell

shell:
	@bash -c "source $(ACTIVATE_SCRIPT); exec bash"

activate:
	@echo "👉 To activate the virtual environment manually, run:"
	@echo "source $(ACTIVATE_SCRIPT)"

lint:
	ruff check $(PKG) tests

format:
	ruff format $(PKG) tests

typecheck:
	mypy $(PKG) tests

test:
	pytest

# End-to-end tests against a real PostgreSQL. Uses pytest-postgresql to spin a
# throwaway cluster locally; set DBSYNC_COMPARE_PG_EXTERNAL=1 (+ PG* env) to use
# an existing server instead.
test-db:
	pytest -m fixture

check:
	ruff check $(PKG) tests
	ruff format --check $(PKG) tests
	mypy $(PKG) tests
	pytest

clean:
	@echo "🧹 Removing virtual environment and caches..."
	rm -rf $(VENV_DIR) .ruff_cache .mypy_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "✅ Done."
