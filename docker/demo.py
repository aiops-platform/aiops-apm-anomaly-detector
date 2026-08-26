"""UC-7.4 端到端演示：起 app → 手动 run → 查 problems / audit / metrics。

依赖 requests（或 httpx），用法：``docker compose exec apm-alert python demo.py``
打印验证摘要：问题单数、轮次审计数、/metrics 关键指标。
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8000"


def _get(client: httpx.Client, path: str) -> dict:
    resp = client.get(BASE + path)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    with httpx.Client(timeout=30.0) as client:
        print(f"[demo] GET /health -> {_get(client, '/health')}")

        # 手动触发一轮 demo 域采集
        resp = client.post(BASE + "/v1/alerts/run", params={"domain": "demo"})
        resp.raise_for_status()
        run = resp.json()
        print(f"[demo] POST /v1/alerts/run -> {run}")

        problems = _get(client, "/v1/problems")
        print(f"[demo] problems count={len(problems['items'])} first={problems['items'][0] if problems['items'] else None}")

        rounds = _get(client, "/v1/audit/rounds?domain=demo")
        print(f"[demo] audit rounds count={len(rounds['items'])}")

        suppressed = _get(client, "/v1/audit/suppressed")
        print(f"[demo] audit suppressed count={len(suppressed['items'])}")

        metrics = _get(client, "/metrics")
        body = str(metrics)
        for name in ("aiops_round_total", "aiops_records_created", "aiops_false_positive_rate"):
            print(f"[demo] metrics contains {name} = {name in body}")

    print("[demo] done; 浏览器可看 http://localhost:9090 (Prometheus) / http://localhost:8000/metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
