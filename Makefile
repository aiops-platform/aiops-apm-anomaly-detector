VENV     := .venv
PY       := $(VENV)/bin/python
PIP      := $(PY) -m pip
RUFF     := $(VENV)/bin/ruff
MYPY     := $(VENV)/bin/mypy
PYTEST   := $(VENV)/bin/pytest
UVICORN  := $(VENV)/bin/uvicorn

# 建 venv 用的解释器：可显式覆盖（make install PYTHON=python3.12），否则按新→旧挑选 >=3.10 的解释器。
# 注意别退回裸 python3：macOS 自带的 python3 是 3.9，既低于 requires-python>=3.10，其内置 pip(<21.3)
# 也不支持 PEP 660，`pip install -e` 会误报 "editable mode currently requires a setuptools-based build"。
PYTHON   ?= $(shell for p in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do \
              command -v $$p >/dev/null 2>&1 && \
              $$p -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null && \
              { echo $$p; break; }; \
            done)
PY_VER   := $(shell $(PYTHON) -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)

.PHONY: install lint test test-pg dev migrate seed-testbed docker-up docker-down loadtest

install:
	@test -n "$(PYTHON)" || { \
	  echo "错误：未找到 Python >= 3.10（pyproject.toml requires-python）。"; \
	  echo "      请先安装 Python 3.10+，或指定解释器：make install PYTHON=/path/to/python3.12"; \
	  exit 1; }
	@echo "==> 使用解释器 $(PYTHON) (Python $(PY_VER))"
	@if [ -f $(VENV)/pyvenv.cfg ] && ! grep -q "^version = $(PY_VER)" $(VENV)/pyvenv.cfg; then \
	  echo "==> $(VENV) 是旧版本 Python 创建的，重建中…"; rm -rf $(VENV); \
	fi
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -e ".[dev]"

lint:
	$(RUFF) check .
	$(MYPY) src

test:
	$(PYTEST) -q

# 真库集成道（tests/test_pg_integration.py）。不设 APM_TEST_PG_DSN 时该文件整体 skip，
# 所以 `make test` 不需要 PG。这里显式跑一遍，覆盖那些字符串断言抓不到的 SQL 语义
# （整数除法、jsonb 路径、时区折算、search_path 是否覆盖每条连接）。
# 用法：make test-pg APM_TEST_PG_DSN=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow
test-pg:
	@test -n "$(APM_TEST_PG_DSN)" || { \
	  echo "错误：需指定 APM_TEST_PG_DSN，例："; \
	  echo "  make test-pg APM_TEST_PG_DSN=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow"; \
	  exit 1; }
	APM_TEST_PG_DSN="$(APM_TEST_PG_DSN)" $(PYTEST) -q tests/test_pg_integration.py

dev:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(UVICORN) aiops_apm._app:create_app --factory --reload --host 0.0.0.0 --port "$${APM_PORT:-8000}"

migrate:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(PY) -m aiops_apm.migrations.runner

# 测试床三服务（order/warranty/gateway）的日志监控端点 seed（幂等）。
# 注意：采集逻辑待补，现在跑起来每轮采 0 条（原因见 docker/seed_testbed_logs.py 的 docstring）。
seed-testbed:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(PY) docker/seed_testbed_logs.py

# ---- M7 交付：Docker 一键演示 + 压测（本机无 docker/locust → 待补跑）----
docker-up:
	docker compose -f docker/docker-compose.yml up --build -d postgres mock-source apm-alert prometheus

docker-down:
	docker compose -f docker/docker-compose.yml down

loadtest:
	locust -f docker/locustfile.py --host http://127.0.0.1:8000 --headless -u 20 -r 2 -t 60s
