"""UC-7.4 模拟第三方源：stdlib HTTP 服务，单调递增的 CPU/内存指标 + 日志。

仅依赖标准库（``http.server``），供 mock-source 容器 / 本地 ``python docker/mock_source.py`` 运行。
``GET /metrics`` 返回指标信号 JSON；``GET /logs`` 返回日志信号 JSON —— 与
``source_type="mock"`` 采集器的返回值形状一致（见 ``collectors/mock.py``）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler 命名约定
        ts = datetime.now(timezone.utc).isoformat()
        if self.path.startswith("/metrics"):
            # 单调递增的 CPU 使用率，模拟某服务持续飙高（第 20 秒后进入异常区间）
            elapsed = time.time() - _BOOT
            cpu = 0.95 if elapsed > 20 else 0.30 + (elapsed % 10) / 100
            self._send(
                {
                    "service": "demo-app",
                    "timestamp": ts,
                    "metrics": [
                        {"metric": "cpu_usage", "value": round(cpu, 4), "labels": {"instance": "pod-1"}},
                        {"metric": "heap_usage", "value": round(512 + elapsed * 4, 2), "labels": {}},
                    ],
                }
            )
        elif self.path.startswith("/logs"):
            self._send(
                {
                    "service": "demo-app",
                    "timestamp": ts,
                    "logs": [
                        {"signature": "OutOfMemoryError: heap space", "level": "error", "count": 3},
                    ],
                }
            )
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 -- 精简容器日志
        print(f"[mock-source] {fmt % args}")


_BOOT = time.time()


def main() -> None:
    server = HTTPServer(("0.0.0.0", 9100), Handler)
    print("[mock-source] serving on :9100  GET /metrics  GET /logs")
    server.serve_forever()


if __name__ == "__main__":
    main()
