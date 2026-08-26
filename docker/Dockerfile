# aiops-apm-anomaly-detector 多阶段镜像（UC-7.4，本机无 docker → 写出待补跑）。
# 用法：cd docker && docker compose up --build
FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && pip wheel --no-cache-dir --no-deps -w /wheels .

FROM python:3.11-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    APM_STORAGE_BACKEND=mysql \
    APM_DB_HOST=mysql \
    APM_DB_PORT=3306 \
    APM_DB_USER=root \
    APM_DB_PASSWORD=root123 \
    APM_DB_NAME=aiops_apm_runtime
COPY --from=builder /wheels /wheels
COPY --from=builder /build/src /app/src
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
# seed 与可插拔规则示例随镜像分发（迁移脚本随包安装进 site-packages，runner 用 Path(__file__).parent 定位）
COPY docker/seed.py /app/seed.py
# 可插拔规则示例（custom_detector entry_points）
COPY docker/custom_detector /app/custom_detector
RUN pip install --no-cache-dir /app/custom_detector
EXPOSE 8000
CMD ["uvicorn", "aiops_apm.main:app", "--host", "0.0.0.0", "--port", "8000"]
