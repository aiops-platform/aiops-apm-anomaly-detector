"""指纹函数：去重与 L3 持续性的唯一真源。

- ``anomaly_key``：单个异常的稳定指纹（相同问题身份恒等，与 value/severity/时间无关）。
- ``group_key``：一组异常的「排序无关」去重键 ``tenant_id:domain:service:<hash>``。
- ``is_same_group``：两个 group_key 是否属于同一问题。

契约在 M1 冻结，之后禁止再改，只允许增加可选字段。
"""

import hashlib

from aiops_apm.models.anomaly import LogAnomaly, MetricAnomaly


def anomaly_key(a: MetricAnomaly | LogAnomaly) -> str:
    """对单个 anomaly 生成稳定指纹（sha256 前 16 位十六进制）。"""
    if isinstance(a, MetricAnomaly):
        raw = f"metric|{a.tenant_id}|{a.service}|{a.metric}|{sorted(a.labels.items())}"
    else:
        raw = f"log|{a.tenant_id}|{a.service}|{a.signature}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def group_key(tenant_id: str, domain: str, service: str, anomalies: list) -> str:
    """对一组 anomaly 生成排序无关的去重键（sha256 前 12 位）。"""
    keys = sorted(anomaly_key(a) for a in anomalies)
    raw = f"{tenant_id}|{domain}|{service}|{'|'.join(keys)}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{tenant_id}:{domain}:{service}:{h}"


def is_same_group(key_a: str, key_b: str) -> bool:
    """两个 group_key 是否属于同一问题。"""
    return key_a == key_b
