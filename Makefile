VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(PY) -m pip
RUFF     := $(VENV)/bin/ruff
MYPY     := $(VENV)/bin/mypy
PYTEST   := $(VENV)/bin/pytest
UVICORN  := $(VENV)/bin/uvicorn

.PHONY: install lint test dev migrate

install:
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

lint:
	$(RUFF) check .
	$(MYPY) src

test:
	$(PYTEST) -q

dev:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(UVICORN) aiops_apm._app:create_app --factory --reload --host 0.0.0.0 --port "$${APM_PORT:-8000}"

migrate:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(PY) -m aiops_apm.migrations.runner
