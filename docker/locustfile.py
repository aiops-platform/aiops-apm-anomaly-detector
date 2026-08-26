"""UC-7.5 压测脚本（locust）：读 GET /v1/problems + 写 POST /v1/alerts/run + /metrics。

本机无 locust → 写出待补跑。用法：
    locust -f docker/locustfile.py --host http://127.0.0.1:8000
  （web UI :8089；或 ``locust --headless -u 20 -r 2 -t 60s``）
"""

from __future__ import annotations

from locust import HttpUser, between, task


class AlertUser(HttpUser):
    """混合读/写：问题查询（读）+ 手动触发轮次（写）+ 指标拉取。"""

    wait_time = between(0.5, 2.0)

    @task(3)
    def list_problems(self) -> None:
        self.client.get("/v1/problems", params={"limit": 20}, name="/v1/problems")

    @task(1)
    def run_domain_round(self) -> None:
        # 写路径：触发一轮 demo 域检测（带 X-Tenant-Id）
        self.client.post("/v1/alerts/run", params={"domain": "demo"}, headers={"X-Tenant-Id": "default"}, name="POST /v1/alerts/run")

    @task(2)
    def metrics(self) -> None:
        self.client.get("/metrics", name="/metrics")

    @task(1)
    def audit_rounds(self) -> None:
        self.client.get("/v1/audit/rounds", params={"limit": 50}, name="/v1/audit/rounds")
