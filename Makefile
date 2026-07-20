UV = uv

.PHONY: help setup run stop test test-unit test-integration lint ruff mypy mypy-ci mypy-stop format lock sync migrate revision pipeline clean

ALEMBIC = $(UV) run alembic -c src/phd_searcher/database/alembic.ini

help:
	@echo "Usage:"
	@echo "  make setup            - Install deps (main + integration), enable git hooks"
	@echo "  make run              - Run the API via docker compose"
	@echo "  make stop             - Stop docker compose"
	@echo "  make test             - Unit + integration tests"
	@echo "  make test-unit        - Unit tests with coverage"
	@echo "  make test-integration - Integration tests (Docker required)"
	@echo "  make lint             - ruff + mypy"
	@echo "  make format           - ruff --fix + ruff format"
	@echo "  make lock             - Regenerate uv.lock (main + integration)"
	@echo "  make migrate          - Apply DB migrations (alembic upgrade head)"
	@echo "  make revision name=X  - Autogenerate a migration named X"
	@echo "  make pipeline args=X  - Run a pipeline stage in the API container, e.g. args=\"discovery --limit 20\""
	@echo "  make clean            - Remove virtualenvs"

setup:
	@if [ -s apt-packages.txt ]; then sudo apt-get update && sudo apt-get install -y $$(cat apt-packages.txt); fi
	$(UV) sync
	cd tests/integration && $(UV) sync
	@if [ -d .githooks ]; then chmod +x .githooks/* && git config core.hooksPath .githooks; fi

run:
	docker compose up -t 0 --build -d --wait

stop:
	docker compose down -t 0

test: test-unit test-integration

test-unit:
	PYTHONPATH=src $(UV) run pytest --cov=src --cov-report=term-missing tests/unit/

test-integration:
	cd tests/integration && $(UV) run pytest -v .

lint: ruff mypy

ruff:
	$(UV) run ruff check .

mypy:
	@# dmypy caches installed-package types; restart when the env moved.
	@if [ uv.lock -nt .dmypy.json ]; then $(UV) run dmypy stop >/dev/null 2>&1 || true; fi
	$(UV) run dmypy run -- src

mypy-ci:
	$(UV) run mypy src

mypy-stop:
	$(UV) run dmypy stop

format:
	$(UV) run ruff check . --fix && $(UV) run ruff format .

lock:
	$(UV) lock
	cd tests/integration && $(UV) lock

sync:
	$(UV) sync

migrate:
	PYTHONPATH=src $(ALEMBIC) upgrade head

revision:
	@test -n "$(name)" || { echo "usage: make revision name=\"describe change\""; exit 1; }
	PYTHONPATH=src $(ALEMBIC) revision --autogenerate -m "$(name)"

pipeline:
	@test -n "$(args)" || { echo "usage: make pipeline args=\"<stage> [--limit N] [--name X]\""; exit 1; }
	docker compose run --rm api python -m phd_searcher.pipeline.cli $(args)

clean:
	rm -rf .venv tests/integration/.venv
