# 自定义签名级抑制器指南（signature_blacklist）

> 目的：内置 `blacklist` 抑制器对日志**只按 `level` 匹配**（`blacklist.py:24` 拿 `entry["signal"] == signal.level`），
> 无法针对「某一条异常签名」抑制。本指南给出一份**签名级**抑制器插件 `signature_blacklist`，
> 匹配 `LogSignal.signature` / `LogSignal.message` 文本，命中即 L0 剔除——解决
> 「`MissingServletRequestParameterException` 已知问题，只滤这一条、不误伤同 service 其他 ERROR 日志」。
>
> 设计特点：**config 驱动，不依赖表**（规则全在 `domain_config.suppressors[].params`），改配置即热生效，
> 与内置 blacklist（表驱动，读 `ctx.blacklist`）互补。走标准插件注册流程（entry_points + `build()` 工厂），不动 M1 冻结契约。

---

## 1. 插件源码

落位文件：`src/aiops_apm/suppressors/signature_blacklist.py`（复制以下全部内容）：

```python
"""内置抑制器：签名黑名单（signature_blacklist）。

按 ``LogSignal`` 的堆栈签名 / message 文本抑制，弥补内置 ``blacklist`` 只匹配
``level`` 的粒度不足（内置黑名单对日志只能按 ERROR/WARNING 整级滤掉）。

规则完全来自 ``domain_config.suppressors[].params``（config 驱动），不依赖表——
与内置 blacklist（表驱动，``ctx.blacklist``）互补。改 domain_config 即热生效。

params 结构：:

    {
        "match": "contains" | "exact" | "regex",   # 默认 contains
        "signatures": ["MissingServletRequestParameterException", ...],
        "services": ["sip-aiops-management"],        # 可选，留空 = 全部 service
        "levels": ["ERROR"],                         # 可选，留空 = 全部 level
        "case_sensitive": false                      # 可选，默认忽略大小写
    }

匹配对象：``LogSignal.signature``（采集器预计算），缺省回退 ``signal.message``。
MetricSignal 一律放行（不抑制指标）。
"""

from __future__ import annotations

import re
from typing import Any

from aiops_apm.models.signal import LogSignal
from aiops_apm.plugins.base import Suppressor


class SignatureBlacklistSuppressor(Suppressor):
    """签名黑名单抑制器：LogSignal.signature/message 命中规则 → 抑制；MetricSignal 一律放行。"""

    name = "signature_blacklist"

    def _matches(self, haystack: str, params: dict) -> bool:
        mode = params.get("match", "contains")
        signatures = params.get("signatures") or []
        if not signatures:
            return False
        if mode == "exact":
            return haystack in signatures
        flags = 0 if params.get("case_sensitive", False) else re.IGNORECASE
        if mode == "regex":
            pattern = "|".join(signatures)  # 每个元素是一段正则，用 | 组合
        else:  # contains（默认）：任一子串命中即抑制
            pattern = "|".join(re.escape(sig) for sig in signatures)
        return re.search(pattern, haystack, flags) is not None

    def _reason_for(self, signal: Any, ctx: Any, params: dict) -> str | None:
        if not isinstance(signal, LogSignal):
            return None
        services = params.get("services") or []
        if services and signal.service not in services:
            return None
        levels = params.get("levels") or []
        if levels and signal.level not in levels:
            return None
        haystack = signal.signature or signal.message
        if not haystack:
            return None
        return "signature_blacklist" if self._matches(haystack, params) else None

    async def check(self, signal: Any, ctx: Any, params: dict) -> str | None:
        return self._reason_for(signal, ctx, params)

    async def batch_check(self, signals: list[Any], ctx: Any, params: dict) -> list[tuple]:
        return [(s, self._reason_for(s, ctx, params)) for s in signals]


def build(*, http: Any = None, pool: Any = None, settings: Any = None) -> SignatureBlacklistSuppressor:
    return SignatureBlacklistSuppressor()
```

**要点：**
- 只抑制 `LogSignal`（`_reason_for` 第 2 行 isinstance 判断），Metric/Change 信号原样放行。
- 匹配对象优先 `signal.signature`（`http_logs` 采集器 / `signature.py` 预计算的堆栈签名），
  无 signature 时回退 `signal.message`（`signature.py:21` 同款回退逻辑）。
- `exact` 模式不做正则转义、区分大小写（签名精确相等）；`contains`/`regex` 默认忽略大小写。
- `params` 从 `domain_config.suppressors[].params` 来（`l0_suppress.py:21` 透传 `sc.params`），
  本插件不读 `ctx`，`ctx` 仅做接口占位。

## 2. 测试源码

落位文件：`tests/test_signature_blacklist.py`（复制以下全部内容，风格对齐 `tests/test_suppressors.py`）：

```python
"""自定义签名黑名单抑制器（signature_blacklist）测试。

规则来自 domain_config.suppressors[].params，不依赖 ctx——用空 Ctx 即可。
"""

from datetime import datetime

import pytest

from aiops_apm.models.signal import LogSignal, MetricSignal
from aiops_apm.suppressors.signature_blacklist import SignatureBlacklistSuppressor

NOON = datetime(2026, 8, 26, 12, 0, 0)


def ls(*, service="sip-aiops-management", level="ERROR", message="boom", signature=None) -> LogSignal:
    return LogSignal(service=service, level=level, message=message, signature=signature, timestamp=NOON)


class Ctx:
    """占位 ctx：本插件不读 ctx，规则全在 params。"""


PARAMS = {
    "signatures": ["MissingServletRequestParameterException"],
    "services": ["sip-aiops-management"],
    "levels": ["ERROR"],
}


@pytest.mark.asyncio
async def test_signature_hit_suppresses():
    sig = "MissingServletRequestParameterException: Required request parameter 'size'|com.x.Ctrl.handle(Controller.java:42)"
    assert await SignatureBlacklistSuppressor().check(ls(signature=sig), Ctx(), PARAMS) == "signature_blacklist"


@pytest.mark.asyncio
async def test_signature_miss_passes():
    sig = "NullPointerException: boom|com.x.Ctrl.run(Controller.java:42)"
    assert await SignatureBlacklistSuppressor().check(ls(signature=sig), Ctx(), PARAMS) is None


@pytest.mark.asyncio
async def test_message_fallback_when_no_signature():
    reason = await SignatureBlacklistSuppressor().check(
        ls(signature=None, message="Caused by: MissingServletRequestParameterException"), Ctx(), PARAMS
    )
    assert reason == "signature_blacklist"


@pytest.mark.asyncio
async def test_service_filter_passes_other_service():
    sig = "MissingServletRequestParameterException: x"
    assert await SignatureBlacklistSuppressor().check(ls(service="svc-b", signature=sig), Ctx(), PARAMS) is None


@pytest.mark.asyncio
async def test_level_filter_passes_other_level():
    sig = "MissingServletRequestParameterException: x"
    assert await SignatureBlacklistSuppressor().check(ls(level="WARNING", signature=sig), Ctx(), PARAMS) is None


@pytest.mark.asyncio
async def test_metric_signal_always_passes():
    m = MetricSignal(service="sip-aiops-management", metric="cpu_usage", value=0.95, timestamp=NOON)
    assert await SignatureBlacklistSuppressor().check(m, Ctx(), PARAMS) is None


@pytest.mark.asyncio
async def test_exact_mode():
    params = {"match": "exact", "signatures": ["only this exact sig"]}
    assert await SignatureBlacklistSuppressor().check(ls(signature="only this exact sig"), Ctx(), params) == "signature_blacklist"
    assert await SignatureBlacklistSuppressor().check(ls(signature="only this exact sig "), Ctx(), params) is None


@pytest.mark.asyncio
async def test_case_insensitive_by_default():
    params = {"signatures": ["missing"]}  # 小写，命中大写开头签名 → 忽略大小写命中
    sig = "MissingServletRequestParameterException: x"
    assert await SignatureBlacklistSuppressor().check(ls(signature=sig), Ctx(), params) == "signature_blacklist"


@pytest.mark.asyncio
async def test_batch_check():
    hit = ls(signature="MissingServletRequestParameterException: a")
    miss = ls(signature="NullPointerException: b")
    results = await SignatureBlacklistSuppressor().batch_check([hit, miss], Ctx(), PARAMS)
    assert results[0] == (hit, "signature_blacklist")
    assert results[1] == (miss, None)
```

## 3. 注册（entry_points）

在 `pyproject.toml` 的 `[project.entry-points."aiops_apm.suppressors"]` 加一行：

```toml
[project.entry-points."aiops_apm.suppressors"]
maintenance_window  = "aiops_apm.suppressors.maintenance_window:build"
blacklist           = "aiops_apm.suppressors.blacklist:build"
signature_blacklist = "aiops_apm.suppressors.signature_blacklist:build"   # 新增
```

**生效步骤（重要）**：entry_points 元数据在安装时生成，新增后必须重装才进 registry：

```bash
cd /Users/h.a.hu/accenture/accenture_aiops_platform/acc-aiops-platform-zjb/aiops-apm-anomaly-detector
.venv/bin/pip install -e .          # 重新生成 entry_points 元数据
curl -X POST http://localhost:8000/v1/plugins/reload   # 热重载 registry（无需重启服务）
curl http://localhost:8000/v1/plugins | grep signature_blacklist   # 确认可见
```

> 若跳过 `pip install -e .`，`POST /v1/plugins/reload` 也找不到新插件——**必须先重装**。
> 之后 `PUT /v1/config/{domain}` 才不会被 `validator` 以「suppressor plugin not found」400 拦下。

## 4. 启用配置（对应你的场景）

`PUT /v1/config/sales`（`X-Tenant-Id: default`），`suppressors` 加 `signature_blacklist` 一项：

```jsonc
{
  "detectors": [
    {"signal": "ERROR", "plugin": "signature_aggregate", "params": {"min_count": 5, "n_frames": 3}, "severity": "warning"}
  ],
  "suppressors": [
    {"name": "maintenance_window"},
    {"name": "blacklist"},                       // 如需保留表驱动的整级黑名单（可选）
    {
      "name": "signature_blacklist",             // 方案 B：签名级抑制
      "params": {
        "match": "contains",
        "signatures": ["MissingServletRequestParameterException"],
        "services": ["sip-aiops-management"],
        "levels": ["ERROR"]
      }
    }
  ],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify": {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

**行为**：`sip-aiops-management` 服务、`ERROR` 级、签名含 `MissingServletRequestParameterException`
的日志在 L0 被剔除 → 走不到 L1 → 该签名的 `signature_aggregate` 异常不再开出 → 不再新增/累计
`problem_record`。命中原因记入 `ctx.suppressed`，可查 `GET /v1/audit/suppressed`。

## 5. 验证

```bash
# 定向跑新增测试
.venv/bin/pytest tests/test_signature_blacklist.py
# 全量回归（确认没破坏 433 用例）
make lint && make test
```

## 6. 与内置 blacklist 对比

| 维度 | 内置 `blacklist` | 自定义 `signature_blacklist` |
|---|---|---|
| 规则来源 | `suppress_blacklist` 表（`ctx.blacklist`，API CRUD） | `domain_config.suppressors[].params`（config JSON） |
| 日志匹配粒度 | 只匹配 `signal.level`（整级抑制） | `signature`/`message` 子串 / 精确 / 正则 |
| 可过滤字段 | `service` + `level` | `service` + `level` + **签名文本** |
| Metric | 按 `metric` 名匹配 | 不处理（放行） |
| 改规则生效 | `PUT /v1/blacklist/{id}` | `PUT /v1/config/{domain}`（都无需重启） |

## 7. 注意事项

1. **validator 不会拦自定义插件**：`config/validator.py:105` 对 registry 里解析到但无内置 schema 的
   插件跳过结构校验——所以 `signature_blacklist` 的 params 怎么写都能过 400 校验，
   参数拼错（如 `"signatures"` 拼成 `"signature"`）不会报错、只会「永远不命中」。排查时先看 `signatures` 键。
2. **签名文本来源**：L0 匹配的 `LogSignal.signature` 是**采集器预计算**的堆栈签名（首行异常 + 顶部 N 帧，
   见 `signature.py`），不是你 `problem_record` 里 `log_anomalies[].signature` 的原样——用 contains
   子串 `MissingServletRequestParameterException` 最稳（首行异常类型必然包含）。
3. **若之前已配了 `blacklist` 的 `signal: "ERROR"` 条目**：它会在 L0 把整个 service 的 ERROR 先滤掉，
   `signature_blacklist` 就永远看不到这些信号了。要只滤这一条，把那条表级 ERROR 黑名单 `PUT /v1/blacklist/{id} {"enabled": false}` 停掉。
4. **只影响 L0**：被抑制的信号不产 anomaly、不产 problem_record，且 `occurrence_count` 不会累加——
   与维护窗口/内置黑名单一致。
