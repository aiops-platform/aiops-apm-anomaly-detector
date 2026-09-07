# domain_config 配置说明（config JSON 结构 + metric/log 共域原理）

> 说明文档：回答两个问题——① `domain_config` 表里 `config` 这一列的大 JSON 是什么、每块供漏斗哪个步骤调用；② 监控 log 与监控应用指标时，配置是不是同一份。
> 真源代码：`src/aiops_apm/models/config.py`（结构）、`src/aiops_apm/pipeline/runner.py`（编排）、`src/aiops_apm/pipeline/{l0_suppress,l1_detect,l2_correlate,l3_verify}.py`（各层消费）、`src/aiops_apm/pipeline/context.py`（装载）、`src/aiops_apm/config/domains.yaml`（seed 例子）。
> 配套阅读：可视化配置页计划见 `docs/plans/UI-domain-rules-config-plan.md`。

---

## 1. `domain_config.config` 是什么

一个 **domain（域）** 的完整检测规则，存 MySQL `domain_config` 表的 `config` JSON 列，由 `build_context`（`pipeline/context.py:96` `DomainConfig.model_validate(r["config"])`）解析进 `DetectionContext.domain_config`，然后 `run_domain`（`pipeline/runner.py:28`）串行调用 L0→L1→L2→L3 时各自读取。

结构共 4 块，恰好对应漏斗的 4 个步骤：

```jsonc
{
  "detectors":    [ {"signal":"cpu_usage","plugin":"static_threshold","params":{"threshold":0.9},"severity":"high"}, ... ],
  "suppressors":  [ {"name":"maintenance_window"}, {"name":"blacklist"} ],
  "correlation":  {"metric_log_window_sec":300, "change_window_sec":300},
  "verify":       {"persistence_rounds":2, "false_positive_threshold":0.6, "min_samples":20}
}
```

## 2. 各块消费方（每块对应一个 L 步骤）

| config 键 | 漏斗步骤 | 谁读 | 具体怎么用 |
|---|---|---|---|
| `suppressors` | **L0 抑制** | `l0_suppress.py:17` | 遍历每个 `{name, params}` → `registry.get("suppressor", name)` → `batch_check(ctx.signals, ctx, params)`。命中就从 `ctx.signals` 剔除、记入 `ctx.suppressed`（审计） |
| `detectors` | **L1 检测** | `l1_detect.py:22-36` | 每项 `{signal, plugin, params, severity}`：`filter_signals(ctx.signals, signal)` 匹配 → `registry.get("detector", plugin).detect(matched, params)` → 产出 anomaly，且 `spec.severity` **权威覆盖** detector 自带 severity |
| `correlation` | **L2 关联** | `l2_correlate.py:61,72,81` | `metric_log_window_sec` 判断指标+日志同源（`_within_window`）；`change_window_sec` 判断变更关联（`_change_within_window`） |
| `verify` | **L3 验证** | `l3_verify.py:35,44,62` | `persistence_rounds`：同一 anomaly_key **累计出现** N 轮才开单（中间断轮不清零）；`false_positive_threshold`+`min_samples`：样本不足或 fpr 低于阈值才算误报，否则降级仍开单 |

## 3. 两个容易混的点

1. **`detectors[].signal` 不是插件参数**——它是**信号匹配器**（`l1_detect.py:24` 的 `filter_signals`）：普通字符串 = 信号名，也可以是一整个 dict（结构化 matcher，如 `{"signal_type":"log","level":"ERROR","service":"svc-a"}`）。可视化配置页的 signal 字段因此要支持 `{...}` JSON 输入。
2. **`suppressors` 只是「启用哪些抑制器插件」**——抑制的具体内容（哪些维护窗口、哪些黑名单条目）**不在这份 JSON 里**，而在 `dynamic_config` 表（`context.py` 里 `load_maintenance_windows`/`load_blacklist` 读入 `ctx.maintenance_windows`/`ctx.blacklist`，`l0_suppress.py:21` 把 `ctx` 传给插件的 `batch_check`）。黑名单条目在页面有独立管理（blacklist 路由）。

---

## 4. 监控 log 和监控应用指标，配置是同一份吗

**是同一份（按 domain 分，不按 metric/log 分）**。同一个 domain 里，metric 信号和 log 信号共享同一份 `domain_config`，靠每个 detector 的 `signal` 匹配字段区分两类信号。

- `signal_type`（metric/log/change）是 **monitor_target** 上的字段，只决定采集器产出哪种信号（`models/signal.py`：`MetricSignal`/`LogSignal`/`ChangeSignal`）。
- 采集进来的信号进合并信号集后，统一由该 target 的 `domain` 对应的那份 `domain_config` 处理。
- L1 分发时 `filter_signals` 对字符串信号名的匹配规则（`filter_signals.py:17-23`）：
  - `MetricSignal` → 按 `metric` 名匹配（如 `cpu_usage`）
  - `LogSignal` → 按 `level` 匹配（如 `ERROR`）

所以**同一份 config 可以混排 metric 和 log 检测器**，各匹配各的（seed `config/domains.yaml` 就是这么写的）：

```jsonc
{
  "detectors": [
    // metric 检测器
    {"signal": "cpu_usage",  "plugin": "static_threshold",    "params": {"threshold": 0.9}, "severity": "high"},
    {"signal": "error_rate", "plugin": "simple_compare",      "params": {"ratio": 1.5, "baseline": 0.02}, "severity": "high"},
    // log 检测器 —— 同一个 config，靠 signal=ERROR 命中日志
    {"signal": "ERROR",      "plugin": "signature_aggregate", "params": {"min_count": 5, "n_frames": 3}, "severity": "warning"}
  ],
  "suppressors": [...],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify":      {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

## 5. 两种精细匹配方式

1. **字符串**：`signal: cpu_usage` 只命中 metric，`signal: ERROR` 只命中 log（同名字段两种语义，靠信号类型自动分流）。
2. **结构化 matcher（dict）**：显式写 `{"signal_type":"metric","metric":"cpu_usage"}` 或 `{"signal_type":"log","level":"ERROR","service":"svc-a"}`，可加 `labels`/`service` 过滤（`filter_signals.py:24-40`）。

## 6. 为什么必须共域（不能拆两套）

L2 同源关联（`l2_correlate.py`）是按 **service** 把 metric anomaly 和 log anomaly 放在一起判断是否同源的——如果 metric 和 log 拆到两个 domain，各自跑各自的漏斗，**L2 跨类型关联就断了**，也就出不了「组合升 critical」的场景。所以设计意图就是：同一业务域的指标+日志，配在**同一份** domain_config 里。

若确实想让某类信号用完全不同的规则集，可以新建一个 domain（如 `application-logs`），对应 target 填那个 domain——但正常不需要。
