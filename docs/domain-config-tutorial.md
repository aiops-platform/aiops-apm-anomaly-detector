# domain_config 的 config 到底是什么？—— 一次讲透 + 最小 demo 贯穿

> 写给：刚接手、被 `domain_config` 表里那一大坨 JSON 劝退的人。
>
> 读完这篇，你能回答三个问题：
> 1. `domain_config` 是什么？它里面的 `config`（一个 JSON）到底存了什么？
> 2. 怎么配置它（不需要看代码也能改）？
> 3. 配完之后它是怎么起作用的（每一轮检测里谁在读它）？
>
> 全篇用一个最简单的 demo（监控「订单服务」的 CPU）从头讲到尾；**log 检测怎么配单独在第 6 节**（建 log 端点 + 配日志检测器 + 完整 JSON）。
>
> 配套阅读：字段级参考手册见 [`domain-config-guide.md`](domain-config-guide.md)，全链路操作手册见 [`operational-guide.md`](operational-guide.md)。

---

## 0. 30 秒速览（先看这个）

- 一个「业务域」（domain）就相当于**一套检测规则的名字**，比如 `application`、`infra`、`demo`。
- `domain_config` 是**一张表**，一个租户 + 一个域 = 一行。
- 每行有一个 `config` 列，里面是一整个 JSON——它就是这一个域**完整的检测规则**。
- 这个 JSON 只有 **4 块**：`detectors`（怎么判异常）、`suppressors`（哪些先掐掉）、`correlation`（怎么把指标和日志关联起来）、`verify`（要持续几轮才算真问题）。
- 每一轮检测开始，系统把这行的 JSON 读出来、解析成规则，然后串行跑 **L0 → L1 → L2 → L3** 四个步骤，**每一步各用各的一块**。

就是这么简单：**「一个域的检测规则，用 JSON 装在这张表的一行里」**。

---

## 1. 一个贯穿全文的 demo 背景

你的任务是：**监控「订单服务」（service=`order-svc`）的 CPU**。一旦 `cpu_usage` 这个指标超过 `0.9`，就要出一个「高严重度」的问题单。

这篇文档后面所有例子，都围绕这一件事。配置就配一次，然后跟着跑一轮看它怎么生效。

---

## 2. 先搞清三个词：租户 / 监控端点 / 域

在系统里，这三个东西各管一件事：

| 词 | 存的表 | 回答的问题 |
|---|---|---|
| 租户 `tenant_id` | 所有表都带这一列 | 「这套数据是谁的」（默认 `default`） |
| 监控端点 `monitor_target` | `monitor_target` 表 | 「监控谁、从哪采、多久采一次」 |
| 域规则 `domain_config` | `domain_config` 表 | 「采回来的数据，**怎么判异常、怎么抑制、怎么验证**」 |

用体检中心类比：

- `monitor_target` = 你预约体检的那个人（监控谁、去哪抽血）。
- `domain_config` = 体检的**标准套餐**（查哪几项、超过多少算异常、异常了要不要复检几次才下结论）。
- 一个人（监控端点）指定自己用哪套套餐（`domain` 字段填套餐名）。

**`config` 就是这个套餐的完整内容。**

---

## 3. `domain_config` 表长什么样

先看建表语句（`src/aiops_apm/migrations/V1__init_tables.sql`）：

```sql
CREATE TABLE domain_config (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64) NOT NULL DEFAULT 'default',   -- 租户
    domain     VARCHAR(32) NOT NULL,                      -- 域 id，如 application
    config     JSON        NOT NULL,                      -- 域检测规则（就是本文主角）
    enabled    TINYINT(1)  NOT NULL DEFAULT 1,            -- 是否启用
    version    INT         NOT NULL DEFAULT 1,            -- 版本号，每次改 +1
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_domain (tenant_id, domain)       -- 一个租户一个域只能有一行
) ENGINE=InnoDB;
```

要点：

- 一个 (租户, 域) 只允许一行（`UNIQUE`）。
- **检测规则没有拆成一列列字段，而是整包放在 `config` 这个 JSON 列里。** 为什么？因为规则结构是「插件名 + 参数」这种可扩展形态，字段是定死的、JSON 是灵活的。改规则 = 改这一行 JSON，不用改表结构。

---

## 4. `config` 这个大 JSON，拆开看只有 4 块

用我们的 demo 配置（后面第 9 节就是配它）：

```json
{
  "detectors": [
    {"signal": "cpu_usage", "plugin": "static_threshold", "params": {"threshold": 0.9}, "severity": "high"}
  ],
  "suppressors": [],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify": {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

4 块各管一件事，正好对应检测漏斗的 4 个步骤：

| 块 | 对应漏斗步骤 | 大白话 | 在 demo 里的意思 |
|---|---|---|---|
| `detectors` | **L1 检测** | 「怎么判异常」——一条条规则：什么信号、用什么插件、什么参数、给什么严重度 | `cpu_usage` 超过 0.9 → 出 1 条 high 异常 |
| `suppressors` | **L0 抑制** | 「哪些信号先掐掉，不参与判定」——启用了哪些抑制插件 | 先留空，什么都不掐 |
| `correlation` | **L2 关联** | 「指标和日志、变更在多少秒内，算同一个问题」 | 5 分钟内 CPU 高 + 同服务日志报错 → 算「同源关联」 |
| `verify` | **L3 验证** | 「持续几轮才算真问题 + 误报率闸门」 | 累计出现 **2 轮**（中间断轮不清零），才落问题单 |

> 记忆口诀：**detect 判异常，suppress 先掐掉，correlate 找关联，verify 验真假。** 正好 L1 → L0 → L2 → L3。

---

## 5. 每一块的细节

### 5.1 `detectors` —— 怎么判异常（L1 用）

`detectors` 是一个数组，每个元素是一条检测规则，4 个字段：

```json
{
  "signal":   "cpu_usage",          // 匹配哪类信号（重要，下面专门讲）
  "plugin":   "static_threshold",   // 用哪个检测插件（算法）
  "params":   {"threshold": 0.9},   // 插件的参数
  "severity": "high"                // 命中后给的严重度（会覆盖插件默认值）
}
```

系统内置的检测插件就 3 个（`src/aiops_apm/detectors/`）：

| 插件名 | 参数 | 作用 |
|---|---|---|
| `static_threshold` | `threshold`（必填）；可选 `operator`：`gt`/`gte`/`lt`/`lte`/`range` | 指标值越过阈值 → 异常（默认 `>` 大于） |
| `simple_compare` | `baseline` 或 `ratio`（至少给一个） | `值 > 基线 × 倍数` → 异常（环比突增） |
| `signature_aggregate` | `min_count`、`n_frames` | 同一段日志堆栈签名出现够多次 → 1 条日志异常 |

**关键点 ①：`signal` 不是插件参数，它是「信号筛选器」。**
它回答「这批采回来的信号里，哪些交给这个插件判」。两种写法：

- **字符串**（最常用）：`"cpu_usage"` = 命中指标名为 `cpu_usage` 的信号；`"ERROR"` = 命中日志 level 为 `ERROR` 的信号。
- **结构化 dict**（更精确）：想限定服务等，写 `{"signal_type": "metric", "metric": "cpu_usage", "service": "order-svc"}` 或 `{"signal_type": "log", "level": "ERROR", "service": "order-svc"}`。

**关键点 ②：metric 和 log 共用同一份 config。**
指标和日志信号都进同一个池子，靠 `detectors` 里每条规则的 `signal` 自动分流。所以你可以在这一个 JSON 里既配指标检测器、又配日志检测器，互不干扰。

### 5.2 `suppressors` —— 先掐掉哪些（L0 用）

```json
"suppressors": [ {"name": "maintenance_window"}, {"name": "blacklist"} ]
```

**最容易误解的地方**：这里只是「启用哪些抑制插件」的**开关列表**，抑制的**具体内容不在这个 JSON 里**。

- 「哪个服务在哪个时间段是维护窗口」存在 `maintenance_window` 表。
- 「哪条黑名单」存在 `suppress_blacklist` 表。
- 每轮检测时，系统把这两张表的内容读进上下文，喂给抑制插件用。

所以这里只需要列「我要开哪个抑制器」：内置两个——`maintenance_window`（维护窗口）和 `blacklist`（黑名单）。demo 里暂时都关掉，写空数组 `[]`。

### 5.3 `correlation` —— 怎么关联（L2 用）

```json
"correlation": {"metric_log_window_sec": 300, "change_window_sec": 300}
```

- `metric_log_window_sec`：同一服务，一条指标异常和一条日志异常，判定时间相差 ≤ 这个秒数 → 算「指标+日志同源」。
- `change_window_sec`：一次部署变更（`change_record`）和异常时间相差 ≤ 这个秒数 → 算「变更相关」。

demo 保持默认 300（5 分钟）就行。

### 5.4 `verify` —— 持续几轮才算真（L3 用）

```json
"verify": {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
```

- `persistence_rounds`：同一个异常**累计出现 N 轮**，第 N 轮才真正开问题单。默认 2——注意是**累计**：第 1 轮出现、第 2 轮断、第 3 轮再出现，也算累计 2 次，第 3 轮开单（**中间断轮不清零**）。这是**演示时最容易踩的坑**（跑一轮不开单是正常的）。
- `false_positive_threshold` + `min_samples`：误报率闸门。样本数不足或误报率低于阈值，才算「误报」从而降级处理；否则降级成 warning 仍开单（降级不丢弃）。

---

## 6. 专门讲讲 log 怎么配置（完整示例）

前面 1–5 节一直在配 metric（CPU）。这一节专门讲 **log 检测**：从「建 log 监控端点把日志采进来」到「在 config 里配日志检测器」，再到「metric 和 log 怎么在 L2 关联」，最后给一个完整走一遍。还是用订单服务（`order-svc`）做例子。

### 6.1 日志信号长什么样（先知道要匹配什么）

采集器把每一条日志变成 `LogSignal`（`src/aiops_apm/models/signal.py`），关键字段：

| 字段 | 说明 |
|---|---|
| `service` | 属于哪个服务（如 `order-svc`） |
| `level` | 日志级别（`INFO`/`WARN`/`ERROR`…）——**L1 匹配主要靠它** |
| `message` | 日志正文 |
| `stack_trace` | 堆栈（异常时才有） |
| `timestamp` | 日志时间 |
| `trace_id` | 可选，业务全链路 id（落单后当证据） |
| `signature` | 采集器预计算的堆栈签名（`signature_aggregate` 聚合用） |

一条日志信号长这样：

```python
LogSignal(service="order-svc", level="ERROR",
          message="java.lang.NullPointerException: null",
          stack_trace="java.lang.NullPointerException\n\tat com.acme.OrderService.fulfill(...)",
          timestamp=now, trace_id="trace-abc123")
```

### 6.2 第一步：先把日志采进来（建 log 监控端点）

建监控端点时：`signal_type="log"` + `source_type="http"/"elk"` → 分派到 `http_logs` 采集器；`source_type="mock"` 走 mock 采集器（不产信号，仅链路验证）。

`source_config` 里 `field_mapping` 用这些键：`service` / `level` / `message` / `stack_trace` / `timestamp` / `trace_id`；`signature_frames` 控制堆栈签名取前几帧（默认 3）。

```bash
curl -X POST http://127.0.0.1:7070/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{
    "service": "order-svc",
    "signal_type": "log",
    "source_type": "elk",
    "domain": "application",
    "source_config": {
      "url": "http://log-source:9200/app-log/_search",
      "rows_path": "hits.hits",
      "field_mapping": {
        "service":     "_source.service",
        "level":       "_source.level",
        "message":     "_source.message",
        "stack_trace": "_source.stack_trace",
        "timestamp":   "_source.timestamp"
      },
      "signature_frames": 3,
      "timezone": "Asia/Shanghai"
    },
    "schedule": {"interval_sec": 60},
    "enabled": true
  }'
# → 201 {"target_id":"MT-0002"}
```

说明：响应每行形状类似 `{"_source":{"service":"order-svc","level":"ERROR","message":"...","timestamp":"..."}}`，`field_mapping` 用点路径逐行映射。若源返回的是**本地墙钟时间**（无时区后缀，如 Spring 应用日志），配 `timezone`（IANA 名，如 `Asia/Shanghai`）让系统按该时区解释再转 UTC，否则时间会偏 8 小时、水位线错乱、每轮重复采集。

### 6.3 第二步：在 config 里配日志检测器

日志检测器固定用 `signature_aggregate` 插件：按堆栈签名把日志分组，同一签名日志条数 ≥ `min_count` → 出 1 条日志异常（`count` = 聚合到的条数）。

`signal` 两种写法（都在 `detectors` 里）：

- **按 level 匹配（字符串）**：`"ERROR"` → 命中所有 level 为 `ERROR` 的日志。
- **按 level + 服务匹配（dict，更精确）**：`{"signal_type": "log", "level": "ERROR", "service": "order-svc"}`。

完整 JSON（metric + log 检测器混排在同一份 config，靠 `signal` 分流）：

```json
{
  "detectors": [
    {"signal": "cpu_usage",  "plugin": "static_threshold",     "params": {"threshold": 0.9},               "severity": "high"},
    {"signal": "error_rate", "plugin": "simple_compare",       "params": {"ratio": 1.5, "baseline": 0.02}, "severity": "high"},
    {"signal": "ERROR",      "plugin": "signature_aggregate",  "params": {"min_count": 5, "n_frames": 3},   "severity": "warning"}
  ],
  "suppressors": [],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify":      {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

参数含义：

- `min_count`（默认 5）：同一签名日志**至少几条**才判异常。设 5 = 同一堆栈报错出现 5 次才出异常，防单条偶发噪音。
- `n_frames`（默认 3）：堆栈取**前几帧**做签名。帧数越大越精确，越小越容易把相近异常聚成一条。
- `severity`：命中后给的严重度（覆盖插件默认值）。

> 想只盯某个服务？把 `signal` 换成 `{"signal_type":"log","level":"ERROR","service":"order-svc"}` 即可（L1 `filter_signals` 结构化 matcher，`src/aiops_apm/pipeline/filter_signals.py`）。

### 6.4 metric 和 log 怎么关联（L2）

`correlation.metric_log_window_sec`（默认 300 秒）决定：**同一服务**在窗口内同时有「指标异常」和「日志异常」→ 算同源（`related=true`）。同源且两者都是 `high` → L3 组合升 `critical`。

例子：`cpu_usage=0.95`（high）出现后 5 分钟内，同服务 `order-svc` 又冒出 5 条 ERROR 日志 → L2 同源 → L3 组合升 **critical**。这就是「指标 + 日志要共域、共服务」的原因——拆成两个域，L2 跨类型关联就断了。

### 6.5 log 检测走一遍（和 metric demo 一样，两轮开单）

1. 建 log 监控端点（见 6.2）→ `MT-0002`
2. 配 ERROR 检测器（见 6.3）
3. 手动触发，连跑两轮：

```bash
curl -X POST http://127.0.0.1:7070/v1/monitors/MT-0002/run   # 第 1 轮：L1 命中但 L3 持续性不够 → 不开单
curl -X POST http://127.0.0.1:7070/v1/monitors/MT-0002/run   # 第 2 轮：累计 2 轮 → 落单（断轮不清零）
curl "http://127.0.0.1:7070/v1/problems?state=pending&service=order-svc"
```

日志问题单里能看到 `log_anomalies`：`level=ERROR`、`signature`、`count`（聚合到的日志条数）。

想先不依赖真实日志源验证逻辑？用代码跑（mock 采集器经 API 建端点不产信号，所以用内存 store + 手工构造 `LogSignal`）：

```python
from aiops_apm.models.signal import LogSignal

signals = [
    LogSignal(service="order-svc", level="ERROR", message="OOMError",
              signature="java.lang.OOMError", timestamp=now)
    for _ in range(5)          # 5 条同签名日志 → min_count=5 命中
]
```

然后走和第 10 节一样的 `build_context` + `run_domain`（把 `signals` 换成上面的 log 信号，config 里 detector 用 `signal="ERROR"` + `plugin="signature_aggregate"`）。

### 6.6 纯日志域：L0–L3 完整配置（只采日志、不采指标）

如果你的域**只采日志、不采指标**（`monitor_target` 全部是 `signal_type="log"`，指标端点一个都不建），那信号池里永远只有 `LogSignal`。此时：

- 指标检测器（`static_threshold`/`simple_compare`）**只吃 `MetricSignal`**，纯日志域里写了也不会命中任何信号——所以可以完全不写。
- L0/L1/L3 照常工作；L2 里 `metric_log_window_sec` 那一半失效，`change_window_sec` 仍生效。

一份完整的纯日志 `domain_config`：

```json
{
  "detectors": [
    {"signal": "ERROR", "plugin": "signature_aggregate", "params": {"min_count": 5, "n_frames": 3}, "severity": "high"},
    {"signal": {"signal_type": "log", "level": "WARN", "service": "order-svc"},
     "plugin": "signature_aggregate", "params": {"min_count": 20}, "severity": "warning"}
  ],
  "suppressors": [
    {"name": "blacklist"},
    {"name": "maintenance_window"}
  ],
  "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
  "verify": {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
}
```

逐层看纯日志域里每一块怎么用：

**L0 抑制（`suppressors`）——对日志一样生效**
- `blacklist`：按 `service + level` 抑制（`suppressors/blacklist.py` 对 `LogSignal` 用 `entry["signal"] == signal.level` 匹配）。例：某服务 ERROR 日志一直刷屏，在黑名单表加一条 `{"service":"order-svc","signal":"ERROR","reason":"noisy"}`，这条 level 的信号在 L0 直接被掐掉，不参与后续判定。
- `maintenance_window`：按 `service + 时间窗口` 抑制。发布窗口内该服务的日志不判。
- 注意：这里只列「开哪个插件」，具体条目在维护窗口表 / 黑名单表（见 5.2）。

**L1 检测（`detectors`）——只配日志检测器**
- 只用 `signature_aggregate`。上例两件事：所有 `ERROR` 日志 5 条聚合出 1 条 high；`order-svc` 的 `WARN` 日志 20 条聚合出 1 条 warning（限定服务用 dict 写法，见 6.3）。

**L2 关联（`correlation`）——metric 那半没用，change 那半有用**
- `metric_log_window_sec`：没有指标异常 → `_within_window` 恒返回 False，该参数**不生效**，本轮 reason 是 `log_only`。留着默认即可，不读。
- `change_window_sec`：**仍然生效**——日志异常前后 N 秒内有部署变更（`change_record`）→ `change_related=true`，开单时带上变更关联信息。纯日志域主要就看它。

**L3 验证（`verify`）——不变**
- `persistence_rounds=2`：同一日志异常（同 `anomaly_key`）**累计出现 2 轮**才开单（中间断轮不清零）——纯日志域也一样，跑一轮不开单是正常的。
- `false_positive_threshold` + `min_samples`：误报率闸门，对日志异常同样适用。
- 注意：**纯日志域不会触发「组合升 critical」**。那个判定（`l3_verify.py` `calibrate_severity`）要求同一 service 同时有 high 指标异常 + high 日志异常；没有指标永远走不到，严重度就取你配的 `severity` 里最高的。

> 提醒：这套纯日志 config 只在「该域一个指标端点都不建」时成立。若之后往同域加了 metric 端点，`cpu_usage` 等指标信号会进来，但 config 里没有对应的指标 detector——此时指标**只会被采集、不会被判定**。想要混合检测，回到 6.3 的混排写法。

### 6.7 log 配置的常见坑

| 坑 | 说明 |
|---|---|
| 配了但一直不触发 | 看 `min_count`：设太高（如 100）日志量少时永远够不到；或 `field_mapping` 漏映射 `level`/`message`，信号里 level 为 null，字符串 `"ERROR"` 匹配不上 |
| 字符串 `"ERROR"` 匹配到所有服务的 ERROR 日志 | 想限定服务，用 dict 写法 `{"signal_type":"log","level":"ERROR","service":"order-svc"}` |
| metric 和 log 关联不上 | 必须是**同一 service + 同一 domain**，且时间差 ≤ `metric_log_window_sec` |
| 时间不对（水位线反复重采） | 源返回本地墙钟时间时没配 `source_config.timezone`，被当 UTC 处理，偏 8 小时 |

---

## 7. 这张 JSON 存在哪、怎么写进去、怎么改

### 7.1 存在哪

- 生产：MySQL `domain_config.config`（JSON 列）。
- 本地演示 / 单测：内存版存储 `InMemoryDomainConfigStore`（不碰数据库）。

### 7.2 三种写入方式（任选一种）

1. **YAML seed（首次初始化，自动发生）**
   表是空的时候，系统启动会读 `src/aiops_apm/config/domains.yaml` 自动灌一个 `application` 域。打开那个文件，就是一份 YAML 写的 config：
   ```yaml
   domains:
     - id: application
       enabled: true
       detectors:
         - { signal: cpu_usage, plugin: static_threshold, params: { threshold: 0.9 }, severity: high }
       verify: { persistence_rounds: 2, false_positive_threshold: 0.6, min_samples: 20 }
   ```

2. **REST API（日常运维改）**
   - 读：`GET  /v1/config/{domain}`（带 `X-Tenant-Id` 请求头，默认租户 `default`）
   - 写：`PUT  /v1/config/{domain}`（需要 admin；body 就是第 4 节那个 JSON）
   - 写入时会做**参数校验**（`validator.py`）：插件名不存在、必填参数缺失、参数类型不对 → 返回 400 `CONFIG_ERROR`。
   - 改完**立刻生效，不用重启**。

3. **代码直接写 store（测试 / 脚本）**
   ```python
   await storage.domain_configs.upsert("default", "application", cfg)
   ```

---

## 8. 关键链路：每一轮检测，它是怎么被用起来的

这是整篇最核心的一张图，看懂了就全通了：

```
           每轮检测（Scheduler 或手动 POST /run 触发）
                          │
                          ▼
    build_context（pipeline/context.py）
      ① DomainConfigLoader.load(tenant_id)        ← 从 domain_config 表读出这个租户所有域
      ② DomainConfig.model_validate(row["config"])← 把 config 这个 JSON 解析成规则对象
      ③ 塞进本轮上下文 ctx.domain_config
                          │
                          ▼
    run_domain（pipeline/runner.py）串行跑漏斗
      L0 l0_suppress  ──读──>  ctx.domain_config.suppressors
      L1 l1_detect    ──读──>  ctx.domain_config.detectors
      L2 l2_correlate ──读──>  ctx.domain_config.correlation
      L3 l3_verify    ──读──>  ctx.domain_config.verify
                          │
                          ▼
                problem_record（问题单落库）
```

一句话：**config 就是「规则仓库」，每次检测把它读出来，L0–L3 各取所需。**

---

## 9. 最小 demo 走一遍（贯穿全篇）

回到第 1 节的任务：监控订单服务 CPU，`cpu_usage` 当前值 `0.95`，超阈值 `0.9`。

> 操作面是 REST API。以下 `<port>` 用 `.env` 里的 `APM_PORT`（示例 7070）。

**第 1 步 — 看规则在不在**
```bash
curl http://127.0.0.1:7070/v1/config/application
```
返回里能看到 seed 好的规则，`cpu_usage` 那条 `threshold: 0.9` 已经在了。

**第 2 步 — 改成我们 demo 要的最简规则（可选）**
```bash
curl -X PUT http://127.0.0.1:7070/v1/config/application \
  -H "Content-Type: application/json" \
  -d '{
    "detectors": [
      {"signal": "cpu_usage", "plugin": "static_threshold", "params": {"threshold": 0.9}, "severity": "high"}
    ],
    "suppressors": [],
    "correlation": {"metric_log_window_sec": 300, "change_window_sec": 300},
    "verify": {"persistence_rounds": 2, "false_positive_threshold": 0.6, "min_samples": 20}
  }'
# → {"domain": "application", "version": 2}   ← version +1，说明生效了
```

**第 3 步 — 建监控端点（用哪套规则由 `domain` 字段决定）**
```bash
curl -X POST http://127.0.0.1:7070/v1/monitors \
  -H "Content-Type: application/json" -H "X-Tenant-Id: default" \
  -d '{"service":"order-svc","signal_type":"metric","source_type":"mock",
       "domain":"application",
       "source_config":{"url":"http://8.8.8.8/metrics"},
       "schedule":{"interval_sec":60},"enabled":true}'
# → 201 {"target_id":"MT-0001"}
```

**第 4 步 — 手动触发第 1 轮**
```bash
curl -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run
```
结果：采集到 `cpu_usage=0.95` → L1 命中（0.95 > 0.9）→ 出 1 条异常。
但 L3 看 `persistence_rounds=2`：这是第 1 次出现（累计 1 次）→ **不开单**。`anomaly_count=1`、`record_created=0`。

**第 5 步 — 再触发第 2 轮**
```bash
curl -X POST http://127.0.0.1:7070/v1/monitors/MT-0001/run
```
第 2 次出现（累计 2 次，中间断轮不清零）→ 满足持续性 → **落库一条问题单**（severity=high，因为第 2 步配的就是 high）。

**第 6 步 — 查单**
```bash
curl "http://127.0.0.1:7070/v1/problems?state=pending"
# → items 里出现一条 PR-xxxx：service=order-svc, severity=high, cpu_usage=0.95
```

**第 7 步 — 改规则看效果（验证「改配置即生效」）**
```bash
curl -X PUT http://127.0.0.1:7070/v1/config/application \
  -H "Content-Type: application/json" \
  -d '{"detectors":[{"signal":"cpu_usage","plugin":"static_threshold",
                     "params":{"threshold":0.99},"severity":"high"}],
       "verify":{"persistence_rounds":2}}'
```
阈值改成 0.99 后再跑两轮：`0.95` 不再命中 → 不再新开单。**同一份配置，改个数字，行为立刻变。**

> 嫌 2 轮太久？把 `verify` 改成 `{"persistence_rounds": 1}`，一轮就开单。

✅ 到这里，你已经完整走过了 **配规则 → 读规则 → 每轮被 L1/L3 消费 → 出单 → 改规则生效** 的最小闭环。

---

## 10. 最小代码 demo（同一套逻辑，不依赖 MySQL）

下面这段和上面 curl 走的是**同一条链路**（store → build_context 自动 load → run_domain）。写法直接取自项目测试 `tests/test_pipeline.py`，是真实可跑的：

```python
import asyncio
from datetime import datetime, timezone

from aiops_apm.models.config import DetectorSpec, DomainConfig, VerifySpec
from aiops_apm.models.signal import MetricSignal
from aiops_apm.pipeline.context import build_context
from aiops_apm.pipeline.runner import run_domain
from aiops_apm.plugins.registry import PluginRegistry
from aiops_apm.settings import Settings
from aiops_apm.storage import build_storage


async def main():
    # 内存版存储：不碰 MySQL，最简演示环境
    storage = await build_storage(Settings(_env_file=None, storage_backend="memory"))
    registry = PluginRegistry().load()          # 从 entry_points 加载内置插件

    # ① 把规则写进 store（等价于生产里 PUT /v1/config/application）
    cfg = DomainConfig(
        detectors=[DetectorSpec(
            signal="cpu_usage", plugin="static_threshold",
            params={"threshold": 0.9}, severity="high",
        )],
        verify=VerifySpec(persistence_rounds=2),   # 累计出现 2 轮才开单（断轮不清零）
    )
    await storage.domain_configs.upsert("default", "application", cfg)

    # ② 组装一轮检测上下文：不传 domain_config，让 build_context 从 store 自动 load
    now = datetime.now(timezone.utc)
    signals = [MetricSignal(service="order-svc", metric="cpu_usage", value=0.95, timestamp=now)]

    # ③ 第 1 轮：异常 1 条，但持续性未满足 → 不开单
    ctx1 = await build_context(tenant_id="default", domain="application",
                               registry=registry, storage=storage, now=now, signals=signals)
    r1 = await run_domain(ctx1)
    print("round1: anomaly_count =", r1.anomaly_count, "| records =", r1.records)

    # ④ 第 2 轮：同一 storage 记住上一轮状态 → 开单
    ctx2 = await build_context(tenant_id="default", domain="application",
                               registry=registry, storage=storage, now=now, signals=signals)
    r2 = await run_domain(ctx2)
    print("round2: anomaly_count =", r2.anomaly_count, "| records =", r2.records)

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
```

预期输出：
```
round1: anomaly_count = 1 | records = []
round2: anomaly_count = 1 | records = [<problem_record ...>]
```

> 想改阈值看效果？把 `params={"threshold": 0.99}`，重跑，两条 round 的 `records` 都是空。

---

## 11. 常见坑（对照着排查）

| 现象 | 原因 / 解法 |
|---|---|
| 触发一次没开单 | **正常**。L3 持续性默认 `persistence_rounds=2`，要累计出现 2 轮（中间断轮不清零）。演示可临时改 `verify.persistence_rounds=1` |
| `PUT /v1/config/{domain}` 报 400 CONFIG_ERROR | 参数校验没过：`static_threshold` 缺 `threshold`、`simple_compare` 缺 `baseline`/`ratio`、插件名拼错等 |
| 以为 `suppressors` 里要写维护窗口/黑名单内容 | 不用。那里只列插件名，具体内容在 `maintenance_window` / `suppress_blacklist` 表，每轮单独读 |
| metric 和 log 想用完全不同的规则 | 它们**共用同一份 config**，靠每条 detector 的 `signal` 区分。实在要拆就新建一个域（如 `application-logs`），监控端点 `domain` 填那个 |
| `signal` 写了个没见过的名字 | 字符串 `signal` 只匹配 metric 名或 log level；要限定服务/标签用 dict 写法 |
| 域删不掉（DELETE 报 400） | 还有 enabled 的监控端点引用着这个域。先删/停那些端点再删规则 |
| log 检测器一直不触发 | 见第 6 节：`min_count` 是否过高、`field_mapping` 是否映射了 `level`/`message`；字符串 `signal` 只按 level 匹配，限定服务用 dict 写法 |

---

## 12. 参考

- 字段级参考手册（偏原理）：[`docs/domain-config-guide.md`](domain-config-guide.md)
- 全链路操作手册（完整手动跑通步骤）：[`docs/operational-guide.md`](operational-guide.md)
- 真源代码：
  - 结构定义：`src/aiops_apm/models/config.py`（`DetectorSpec` / `SuppressorSpec` / `CorrelationSpec` / `VerifySpec` / `DomainConfig`）
  - 加载器（空表 seed / last-known-good 回退）：`src/aiops_apm/config/loader.py`
  - 写入校验：`src/aiops_apm/config/validator.py`
  - 表读写：`src/aiops_apm/storage/domain_config.py`
  - 每轮装载与消费：`src/aiops_apm/pipeline/context.py`、`src/aiops_apm/pipeline/runner.py`
  - seed 示例：`src/aiops_apm/config/domains.yaml`
