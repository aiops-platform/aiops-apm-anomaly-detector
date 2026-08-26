"""UC-7.4 seed：写 2 个 monitor_target（http_metrics + http_logs，domain=demo）+ demo 域配置。

幂等：重复运行不重复插入（monitor_target 按 (tenant_id, target_id) 判重；domain_config upsert）。
用法：``python seed.py``（容器内已设 ``APM_STORAGE_BACKEND=mysql``）。
"""

from __future__ import annotations

import asyncio

from aiops_apm.models.config import DetectorSpec, DomainConfig, SuppressorSpec
from aiops_apm.settings import Settings
from aiops_apm.storage import build_storage

TENANT = "default"
DOMAIN = "demo"

TARGETS = [
    {
        "service": "demo-app",
        "signal_type": "metric",
        "source_type": "http_metrics",
        "domain": DOMAIN,
        "schedule": {"interval_sec": 60},
        "source_config": {
            "url": "http://mock-source:9100/metrics",
            "metric_path": "$.metrics[*]",
        },
    },
    {
        "service": "demo-app",
        "signal_type": "log",
        "source_type": "http_logs",
        "domain": DOMAIN,
        "schedule": {"interval_sec": 60},
        "source_config": {
            "url": "http://mock-source:9100/logs",
            "log_path": "$.logs[*]",
        },
    },
]

DOMAIN_CONFIG = DomainConfig(
    detectors=[
        DetectorSpec(signal="cpu_usage", plugin="static_threshold", params={"threshold": 0.9}, severity="high"),
        DetectorSpec(
            signal={"metric": "heap_usage"}, plugin="simple_compare", params={"ratio": 1.5}, severity="warning"
        ),
    ],
    suppressors=[
        SuppressorSpec(name="maintenance_window", params={"duration_minutes": 30}),
    ],
)


async def seed() -> None:
    settings = Settings()
    storage = await build_storage(settings)
    try:
        for t in TARGETS:
            # 幂等：已存在同 target 的 service/signal_type/domain 则跳过
            existing = await storage.monitor_targets.list(TENANT, service=t["service"], signal_type=t["signal_type"])
            if any(x["domain"] == DOMAIN for x in existing):
                print(f"[seed] skip existing target {t['signal_type']}@{t['service']}")
                continue
            row = await storage.monitor_targets.create(TENANT, t)
            print(f"[seed] created target {row['target_id']} ({t['signal_type']})")

        version = await storage.domain_configs.upsert(TENANT, DOMAIN, DOMAIN_CONFIG)
        print(f"[seed] domain_config upserted domain={DOMAIN} version={version}")
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(seed())
