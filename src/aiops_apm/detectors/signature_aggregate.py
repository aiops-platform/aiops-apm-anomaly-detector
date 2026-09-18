"""内置检测器：堆栈签名聚合（signature_aggregate）。

设计文档 §6.4：按 ``signature`` 分组日志，``count >= min_count`` 判定突增 → 1 条 LogAnomaly(count)。
优先用 M3 采集器预计算的 ``LogSignal.signature``，缺省回退 ``signature()`` 纯函数。
"""

from typing import Any

from aiops_apm.models.anomaly import LogAnomaly
from aiops_apm.models.signal import LogSignal
from aiops_apm.plugins.base import Detector
from aiops_apm.signature import signature


class SignatureAggregateDetector(Detector):
    """签名聚合检测器：同一堆栈签名出现 min_count 次 → 1 条 LogAnomaly(count=出现次数)。"""

    name = "signature_aggregate"

    async def detect(self, signals: list[Any], params: dict) -> list[Any]:
        min_count = params.get("min_count", 5)
        n_frames = params.get("n_frames", 3)
        # 分组键含 service：M9 前只用 signature，两个服务打出同一签名时会塌成**一条**
        # LogAnomaly，service 取到谁取决于 ctx.signals 的插入顺序（count 却是跨服务总数）。
        # 而 fingerprint.anomaly_key = log|tenant|service|signature 会把这个任意的 service
        # 焙进去重身份 → detection_state 持续性与 reconcile 的 miss 判定在服务间漂移。
        groups: dict[tuple[str, str], list[LogSignal]] = {}
        for s in signals:
            if not isinstance(s, LogSignal):
                continue
            sig = s.signature or signature(s, n_frames)
            groups.setdefault((sig, s.service), []).append(s)
        anomalies: list[Any] = []
        for (sig, service), logs in groups.items():
            if len(logs) < min_count:
                continue
            # 业务 trace/request id（可选，去重保留）：采集器带 trace_id 时透传到 problem_record 证据
            trace_ids = sorted({item.trace_id for item in logs if item.trace_id})
            anomalies.append(
                LogAnomaly(
                    kind="log",
                    tenant_id=logs[0].tenant_id,
                    service=service,
                    level=logs[0].level,
                    signature=sig,
                    pattern=logs[0].message[:120],
                    count=len(logs),
                    first_seen=min(item.timestamp for item in logs),
                    severity=params.get("severity", "warning"),
                    detected_at=max(item.timestamp for item in logs),
                    trace_ids=trace_ids,
                )
            )
        return anomalies


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> SignatureAggregateDetector:
    return SignatureAggregateDetector()
