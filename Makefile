VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(PY) -m pip
RUFF     := $(VENV)/bin/ruff
MYPY     := $(VENV)/bin/mypy
PYTEST   := $(VENV)/bin/pytest
UVICORN  := $(VENV)/bin/uvicorn

.PHONY: install lint test dev migrate docker-up docker-down loadtest

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

# ---- M7 交付：Docker 一键演示 + 压测（本机无 docker/locust → 待补跑）----
docker-up:
	docker compose -f docker/docker-compose.yml up --build -d mysql mock-source apm-alert prometheus

docker-down:
	docker compose -f docker/docker-compose.yml down

loadtest:
	locust -f docker/locustfile.py --host http://127.0.0.1:8000 --headless -u 20 -r 2 -t 60s
