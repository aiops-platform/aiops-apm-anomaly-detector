# 问题单去重身份失效 —— 分析与方案

> **状态：未实施。** 本文是一次排查的完整结论 + 两处改动方案，代码尚未改动。
> **读者**：接手「同一个故障反复开单 / 反复派工单」这类问题的人；改 `fingerprint.py`、
> `grouping.py`、`storage/records.py` 的去重路径之前先读。
> **日期**：2026-09-28 · 仓库：`aiops-apm-anomaly-detector`

---

## TL;DR

**症状**：Diagnosis Center 列表顶部出现两条同名「报价单模板缺失」，实为同一故障的 **3 张单**
（PR-20260928-0352 / 0354 / 0356）。同一根因在更早的 `Cannot invoke "String.trim()"` 上
已经**派出过 4 张 agentflow 工单**。

**根因**：`problem_record` 的去重靠 `group_key` 精确匹配，而 `group_key` 是**「这一轮、这一组
是哪几个异常」的集合哈希** —— 成员集合一变就换键，去重闸门恒不命中。

**已定位到三个「1」叠加**（缺一不可）：
1. `signature()` 对无堆栈日志回退 `message[:120]`，而正文带 per-request id ⇒ 每个请求一个新签名；
2. 活库 `min_count=1` ⇒ 每个这样的签名都自成一个 anomaly；
3. 活库 `persistence_rounds=1` ⇒ 它当轮就进 `group_key`。

**两处改动方案**：
- **方案一（身份锚）**：身份只取组内**带堆栈的成员**。实测重放 **3 张 → 1 张**。
- **方案二（escalated 吸收复发）**：`escalated` 单不再被复发另开一单、重派工单。
  走**不改 DDL** 的写入侧方案，避开活库撞唯一约束。

**必须先知道的**：方案一**不会让列表上那两张卡片消失**（见「交付后你会看到什么」）；
且它**修不掉三类键漂移**（见「已知残留」）。

---

# 第一部分：问题单去重身份失效

## 1.1 机制

`fingerprint.group_key`（`models/fingerprint.py:24`）：

```
group_key = sha256(tenant|domain|service| sorted(anomaly_key(a) for a in anomalies))[:12]
```

`anomaly_key` = `log|{tenant}|{service}|{signature}`（日志）/ `metric|…`（指标）。
去重靠 PG 生成列 + 唯一约束（`V1__init_tables.sql:47-50`）：

```sql
open_group_key = CASE WHEN state IN ('pending','in_progress') THEN group_key ELSE NULL END
CONSTRAINT uk_open_group_key UNIQUE (tenant_id, open_group_key)
```

`write_or_append` 走 `INSERT ... ON CONFLICT (tenant_id, open_group_key) DO UPDATE`
（`storage/records.py:366`）。**键一变，冲突不命中，就插一张新单。**

## 1.2 实测复现（活库）

用库里 `detection_state` 的 `anomaly_key` 重算三张单的 `group_key`，**三个都逐字复现**：

| 单 | 轮次 | 组内异常集合 | 集合大小 |
|---|---|---|---|
| PR-…0352 | 05:58:47 | {堆栈, 明文(91ca)} | 2 |
| PR-…0354 | 05:59:53 | {堆栈} | 1 |
| PR-…0356 | 06:01:00 | {堆栈, 明文(fdf9), 明文(f5ec)} | 3 |

集合大小 2 → 1 → 3，三个哈希都不同 ⇒ 三次都不命中 ⇒ 三次开新单。

**为什么集合会变**：
- 明文行 `报价单模板缺失 orderId=ORD-1 traceId=91ca5f3f-…` 无堆栈 ⇒ 签名 = 正文 ⇒
  正文里的 `traceId` 让**每个请求一个新签名**，只活一轮（实测这三个 key 在 `detection_state`
  里 `consecutive_rounds=1`、`last_seen == first_seen`）。
- 分组是**每轮的连通分量**（`grouping.py:66`）：同 signature **或** trace_ids 相交即并组，
  所以组里的成员取决于那一轮恰好发生了哪些请求。

## 1.3 放大器：两个运行库配置

| 配置 | `domains.yaml` / M1 默认 | 活库实际 | 影响 |
|---|---|---|---|
| `min_count` | 5 | **1**（2026-09-23 由 Domain Rules 改的） | 每个不同签名都成 anomaly，不再有"出现了 N 次才算"的过滤 |
| `persistence_rounds` | 2 | **1**（M9 决策 #5「即时开单」） | 当轮就进 `group_key`，原来那道闸门正好能挡掉"只活一轮"的正文行 |

两者都不是 bug，是**有意为之的产品决策**；但它们把 1.2 的机制从"偶尔重复"放大成"每个请求一张单"。

## 1.4 方案一：身份锚

```
identity(组) = 组内「带堆栈的日志异常」；若一个都没有 → 退回整组（= 现状行为）
group_key    = sha256(tenant|domain|service| sorted(anomaly_key for a in identity))[:12]
```

**依据**：堆栈签名由 `类名.方法(文件:行号)` 帧锚定，是 M1 契约刻意设计的稳定部分
（`signature.py` 的 docstring：帧保留类名.方法(文件:行号)）；正文行不是——它可能夹带
per-request 数据，且采集时来时不来。

**为什么收在 `fingerprint.group_key` 内部**，而不是让调用方各自挑子集：
`l3_verify.py:68`（fpr 读）与 `ProblemRecord.group_key`（落库 / fpr 写）必须算出**同一个键**
——`l3_verify` 的 docstring 专门警告过这条，两处各写一遍必然漂移。收进去，两个调用点自动一致，
`group_key` 的签名不变。

**「带堆栈」怎么判**：给 `LogAnomaly` **加可选字段** `has_stack: bool = False`，由
`signature_aggregate` 从底层 `LogSignal.stack_trace` 填（M1 契约「只允许增加可选字段」允许）。
*不*用 `"|at " in signature` 这类字符串猜测：非 Java 栈帧 / `Caused by:` 开头会漏判，
单行 `stack_trace` 也会漏；且该 detector 是**唯一**产出 `LogAnomaly` 的地方，手上就有真值。
第三方日志 detector 插件产的默认 `False` ⇒ 退回整组 ⇒ 行为与今天一致，不是回归
（本仓自带 `docker/custom_detector/p95_latency` 只产 `MetricAnomaly`，已核实）。

### 实现草图

```python
# src/aiops_apm/models/fingerprint.py
def anchor_anomalies(anomalies: list) -> list:
    """组内作为**去重身份**的成员：带堆栈的日志异常；一个都没有时退回全部。"""
    stacked = [a for a in anomalies if isinstance(a, LogAnomaly) and a.has_stack]
    return stacked or list(anomalies)

def group_key(tenant_id, domain, service, anomalies) -> str:
    keys = sorted(anomaly_key(a) for a in anchor_anomalies(anomalies))   # ← 唯一改动
    ...  # 其余不变

# src/aiops_apm/models/anomaly.py（LogAnomaly 内，紧随 trace_ids）
    has_stack: bool = False

# src/aiops_apm/detectors/signature_aggregate.py（LogAnomaly(...) 内）
    has_stack=any(s.stack_trace for s in logs),
```

⚠️ **`isinstance` 不是可选的**：`record.py:73` 传的是 `metric_anomalies + log_anomalies`，
`l3_verify.py:68` 的 `persisted` 也含 metric。裸写 `a.has_stack` 会在 `MetricAnomaly` 上抛
`AttributeError`，**带 metric 的组全军覆没**。

### ⚠️ 证据的诚实限定

上面以及后文的实测数字，验证的是**规则**，不是**接线**：`has_stack` 字段还不存在，
所以重放 / 影响面 / 23→10 的估算都是用「签名里有没有 `|at ` 帧」**模拟**锚，不是真读字段。
规则层面成立是确定的；字段接线要靠新增单测来证。

另：`signal_snapshot` 没有 `stack_trace` 列（`snapshots.py:98` 只存 `level/message/signature`）
⇒ `has_stack` **原理上无法回填**。"不回填"是被迫而非选择——这点应写进字段注释当不变量。

## 1.5 影响面（实测）

**键不变的 4 / 8 张在办单**：全堆栈组（0354 / 0396 / 0194）与无锚退回整组的（017-0001）——
锚 == 整组，哈希逐字不变。

**键会变的 4 张**：
- PR-0352、PR-0356 → 都收敛到 `…4452c7323196`（= PR-0354 的键）。
- PR-20260923-0004、PR-20260924-0001 → 键变；其异常若复发会另开 1 张新单，之后收敛。

**历史效果**：全部 23 张存量单按新锚规则重算 ⇒ **23 张 → 10 个键**，5 组本可合并。

**老行没有 `has_stack` 不构成问题**：`group_key` 只在 `write_or_append` 插新行时求值，
落库后不再重算；`reconcile.record_anomalies`（`reconcile.py:21-36`）只重建 `anomaly_key`
做 miss 判定，不碰 `group_key`。

**不需要迁移**：`open_group_key` 生成列 / 唯一约束 / `ON CONFLICT` 全部不动。
`has_stack` 随 `log_anomalies` JSONB 走（`_as_json` → `model_dump()` + `json.dumps`，
布尔原生可序列化）。

其它已核实：`group_key` 出现在公开 API 响应体（`router/problems.py:135`），但**没有任何下游
消费它**（已 grep 三个兄弟仓）；`fpr_table` 按 group_key 键控，活库 2 行且
`total=1 < min_samples=20` ⇒ 失配后仍 `fpr_ok=True`（**失败方向是不降级**）；
**要重启 APM 进程**才生效。

## 1.6 ⚠️ 交付后你会看到什么

**列表上那两张重复卡片不会消失。** `reconcile.py:64-67` 的关单条件是
`all(miss_rounds >= 3 for k in 该单的 anomaly_key)`，而堆栈那条异常每轮都活着 ⇒
`all(...)` 永远为假 ⇒ **0352 / 0356 会永远停在 `pending`**（旧键冻结：既不增长，也不再被匹配）。

净效果：**新重单不再产生**（下一轮算出的新键 = 0354 的键 → `ON CONFLICT` 追加，不会开第 4 张），
但诊断中心列表仍旧多两张同名卡片，直到人工处理。

**建议**：部署后对这两张走一次 `POST /v1/problems/{id}/ignore`（state → `closed` ⇒
`open_group_key` 变 NULL）。这是数据操作不是迁移，且正是最初想消掉的可见症状。

---

# 第二部分：escalated 单吸收复发

## 2.1 现状（活库实测，这就是问题本身）

同一个 `group_key`（同一个问题）下已有 6 张单，其中 **4 张派了工单**：

| 单 | 状态 | |
|---|---|---|
| PR-20260921-0003 | escalated | `INC-20260921-0001` |
| PR-20260921-0004 | escalated | `INC-20260922-0001` |
| PR-20260922-0001 | resolved | `agentflow:resolved:INC-20260922-0002` |
| PR-20260922-0002 | escalated | `INC-20260922-0004` |

09-22 04:54 结掉一张，11:39 复发**又派了一张新工单**（0004）。

## 2.2 目标规则

| 同键已有单的状态 | 行为 |
|---|---|
| `pending` / `in_progress` | 追加（现状，走 `ON CONFLICT`） |
| **`escalated`** | **追加到它，不开新单**（新）—— 加一条 `recurrence` 证据，状态**保持 escalated** |
| `resolved` / `closed` | 新开一单（不变） |

## 2.3 ⚠️ 为什么**不**改 `open_group_key` 生成列

最干净的做法本是把 `escalated` 加进生成列的 CASE。**但活库上会撞唯一约束**：
上表 3 张 escalated 同键，`CREATE UNIQUE (tenant_id, open_group_key)` 直接失败。
要改生成列就必须先合并那 3 张——那是动历史单，且它们背后有 3 张**未结的 agentflow 工单**，
谁关谁留得人来定。故改走写入侧。

## 2.4 设计（无 DDL、无迁移、不碰历史数据）

在 `write_or_append`（**两个 store 都要改，保持内存/PG 一致**）前加一步：

```
1. SELECT record_id, state FROM problem_record
   WHERE tenant_id=%s AND group_key=%s AND state IN ('pending','in_progress','escalated')
   ORDER BY (state = 'escalated'), detected_at DESC     -- 在办的优先，escalated 兜底
   LIMIT 1
2. 命中且 state=='escalated' →
     给 record.evidence 补一条 recurrence 证据（含上一张工单号）
     UPDATE 该行：evidence ||、occurrence_count+1、last_seen_at、severity（只升不降）、updated_at
     return（不开新单，state 不动）
3. 其余 → 原样的 INSERT ... ON CONFLICT (tenant_id, open_group_key) DO UPDATE
```

**并发安全性**：两个轮次同时命中同一张 escalated ⇒ 各自 UPDATE 追加自己的 evidence，安全；
都未命中 ⇒ 走原 `ON CONFLICT` 收敛。`resolved`/`closed` 不在查询里 ⇒ 复发照旧新开。

**新增常量**：`_DEDUP_STATES = ("pending","in_progress","escalated")`。必须与既有的
`_OPEN_STATES = ("pending","in_progress")` **分开命名**——后者是 reconcile 的自动关单范围
与前端徽标口径（`app.js:41-56`），**不能**跟着变大，否则 escalated 单会被 reconcile 自动关掉、
徽标口径也变。**这是本次最容易写错的地方。**

**证据条目**：`{type:"recurrence", round_id, detected_at, previous_state:"escalated", ticket_number}`。

⚠️ `ticket_number` 的取法**已经有 ≥2 份实现**，别再写第三份：`records.py:61` 的
`_holds_ticket`（匹配给定号）与 `router/problems.py:1224-1236`（按 `diagnose_decision` +
`decision=="escalate"` + 非空 `ticket_id` 取），前端 `app.js` 的 `problemTicketNumber` 还有一份。
三处编码的是同一条判据——「结构化真源在 `evidence[]`，`resolve_reason == "escalated:<号>"`
只是人眼副本、单号为空时退化成裸 `"escalated"`，不能当唯一判据」。
做法：在 `records.py` 紧邻 `_holds_ticket` 加一个**取号**函数，把这条判据收成一处。

**要同步改的契约文字**（否则文档与代码相反）：`storage/records.py:128/133/148` 三处 docstring
现在都写着「复发照常开新单，与 resolved/closed 一致」，其中 escalated 那条要改；
`V12__ticket_seq_and_state_vocab.sql:17-19` 关于生成列的注释也要加一句。

## 2.5 已知残留

活库那 3 张同键 escalated 仍在。规则生效后，复发会追加到**其中 `detected_at` 最新的一张**
（确定性），另两张继续冻结。

---

# 已知残留（锚修不掉的三类键漂移）

都属同一族：`group_key` 的身份仍不是"一个问题"，而是"这一轮的某个投影"。

1. **镜像情形——堆栈成员缺席的那一轮**：若某轮 ES 窗口 / `size=500` 截断 / 水位线推进只捞到
   正文行、没捞到堆栈行 ⇒ 组 `{明文}` ⇒ 无锚 ⇒ 退回整组 ⇒ `key(明文)`；相邻轮 `{栈,明文}`
   ⇒ `key(栈)`。**同一个 bug 只是换了角色**。本次事故本身就是"每轮落到组里的行会变"的证据。
2. **多个堆栈成员时仍会翻**：身份是堆栈成员的**集合**。活库 `min_count=1` ⇒ 每个不同的堆栈
   签名都成一个 anomaly ⇒ 堆栈集合实际是"这一窗口捞到了哪些堆栈行"。
3. **service 段会翻，锚够不着**：`representative_service(group)`（`grouping.py:131-139`）取
   **全组**（含 metric）排序后的第一个服务名，是键的**前缀**。跨服务事故逐轮"长"出新服务时
   （M9 的 traceId 跨 3 服务拓扑），前缀一变就是新键——与锚无关。
   `test_m9_group_key_stable_across_rounds` 喂的是固定服务集，抓不到。

**根治只有 overlap 匹配**（"与在办单共享任一 anomaly_key 即同一问题"），成本是
V13 迁移 + 并发语义重做 + close 时映射清理。

# 其它已核实但不动的

- **`l3_verify` 与 `emit` 的键**：评审证明锚是**单调**的——两处原先一致时可证仍然一致
  （`persisted ⊆ group` 且旧规则下相等 ⇒ `persisted == group` ⇒ 锚也相等），**不会引入新的分歧**。
  但残留情形的**后果**变了：退回整组时算出的可能是一个**别的单正在持有的稳定键**，
  l3 会读到那张单的 fpr 并据此**降级**本单严重度（写回仍落在本单键上，不再收敛）。
  需要 `persistence_rounds>1`（非活库配置）+ fpr 表有量才触发；且闸门只降级、不抑制。
  评审建议的根治：在 `run_domain` 里**算一次锚**并透传给 `l3_verify` 与 `emit`，
  而不是把锚埋在共享原语 `group_key` 里被两个不同列表各算一遍。
- **合并过度的风险**：本会话两处改动是一起落地的——`normalize_message` 让"只有 `key=value`
  取值不同"的异常首行塌成同一签名，锚又让同签名的组塌成同一张单。**两处叠加会把两个不同故障
  并成一张单，且共享 fpr 条目**（一个的误报会降级另一个）。已用"值里排除 `=`"限制了 `a==b`
  这类误伤，但这条风险应当在评审时**当成一件事看**。
- `metrics.py:87` 的 fpr Gauge 用 `{tenant}:{domain}:{service}:` 前缀匹配，而
  `router/problems.py:170` 传的是**拼接串** service ⇒ 跨服务记录的 Gauge 恒匹配不到（既有缺陷）。
- 文档漂移：`README.md:30`、`docs/archive/M1-contracts.md:92,112`、`CLAUDE.md:66` 仍把
  `group_key` 描述成"对传入的 anomalies 求哈希"——字面仍对，语义已偏。
- `is_same_group`（`fingerprint.py:32`）在 `src/` 里**没有调用方**，是文档级断言。
- 活库 3 张同键 escalated 单（背后 3 张未结工单）需要人工决定怎么收口。

---

# 验证方案（实施时用）

1. **单测**：`anchor_anomalies` 直测（有锚 / 无锚退回 / 混合 / 空列表）；
   `test_detectors.py` 断言 detector 填出 `has_stack`；`test_records.py` 加 escalated
   吸收复发的用例（两 store 都要覆盖）。另两条**必须**有，否则漏的是静默缺陷：
   - **组里混 metric 时锚不能崩**（`isinstance` 那条，见 1.4）。
   - **`l3_verify` 的键 == `emit` 落的键**（fpr 读写同键）。
2. **端到端回归**（`tests/test_pipeline.py`）：三轮 `{栈+明文(91ca)}` → `{栈}` →
   `{栈+明文(fdf9)+明文(f5ec)}`，用 `LogSignal(..., stack_trace=...)` + `signature=None`
   让 detector 自己算签名（走生产那条路），断言 **1 条、`occurrence_count==3`**。
3. **变异验证**：把 `anchor_anomalies` 改成恒返回全部成员 ⇒ 上述用例应红；**必须附变异
   真的生效**的证据（`grep`/`diff`），不能只写"测试红了"。
4. **活库重放**：内存存储重放活库三轮（不碰生产库），确认 1 张、occurrence=3。
5. **`make lint`**（= `ruff check .` **+ `mypy src`**；只跑 ruff 会漏类型错误）与 `make test`。
   基线 **621 passed / 29 skipped** 不得退化。
6. **`make test-pg APM_TEST_PG_DSN=postgresql://agentflow:agentflow@127.0.0.1:5432/agentflow`**
   ——把 29 条 skip 变真跑，覆盖 `write_or_append` 的真 SQL 路径。**安全**：建在独立 schema
   `aiops_apm_test`、跑完 drop（`tests/test_pg_integration.py:10-13, 87`），不碰
   `aiops_apm_runtime` 的活数据。

# 改动文件清单（实施时）

| 文件 | 改什么 |
|---|---|
| `src/aiops_apm/models/anomaly.py` | `LogAnomaly` 加 `has_stack: bool = False` + 不可回填的不变量注释 |
| `src/aiops_apm/detectors/signature_aggregate.py` | 填 `has_stack` |
| `src/aiops_apm/models/fingerprint.py` | 加 `anchor_anomalies`；`group_key` 改用它；订正 docstring |
| `src/aiops_apm/models/__init__.py` | 导出 `anchor_anomalies` |
| `src/aiops_apm/storage/records.py` | escalated 吸收复发（两 store）+ `_DEDUP_STATES` + 取号函数 + 三处 docstring |
| `docs/logs/M9.md` | 本次结论（已有 2026-09-28 那节，续写） |
