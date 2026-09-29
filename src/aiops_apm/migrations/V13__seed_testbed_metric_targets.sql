-- V13：测试床 order-service 的**指标**监控端点（CPU 风险线 + 限流症状线）。
--
-- 给 minikube `order` 命名空间的 order-service 建两条 `metric + prometheus` 端点，由自带
-- scheduler 定时拉 Prometheus → L0–L3 → 开单 → 出现在 Diagnosis Center 等人点 Analyze。
--
-- **与 V9（日志端点）的分工**：日志走 ES、按堆栈签名聚合；指标走 Prometheus、"持续"
-- 写在查询里。两条线一风险一症状，各管一件事：
--   风险线 cpu_usage             —— CPU 占 limit 的比例（还有多少余量）
--   症状线 cpu_throttled_percent —— 被 CFS 限流的周期占比（**有人已经在等**）
--
-- ⚠️ 与 V9 同一个约定：**迁移是一次性的、不可变的**。之后要改这两条端点的配置，改
-- `docker/seed_testbed_metrics.py` 后重跑 `make seed-testbed-metrics`（幂等、会刷新
-- source_config），**不要改本文件**。（本文件是它的首次初始化快照，两处内容一致。）
--
-- Prometheus 地址来自 GUC：SQL 是静态文件读不到环境变量，由 MigrationRunner 用
-- set_config('aiops.testbed_prom_url', ...) 注入（值取 APM_TESTBED_PROM_URL）。本机
-- port-forward 是 localhost:19090；**容器里 localhost 指向容器自己**。GUC 未设时
-- COALESCE 回落到同样的默认值，保证手工用 psql 跑这个文件也能用。
--
-- 幂等：ON CONFLICT (tenant_id, target_id) DO NOTHING（迁移不覆盖现网数据）。
-- target_id 必须是 MT-NNNN 形态：MonitorTargetStore._parse_suffix 解析失败会返回 0，
-- 下一个新建端点会拿到 MT-0001 而撞唯一键。这里用 MT-0004/0005，与 seed 脚本同一组号。
--
-- 判据为什么写在查询里：采集器是 instant query + **一行一信号**（FieldMapper.map_metric），
-- 每轮只拿得到一个标量 ⇒ 让那个标量本身就回答"持续了吗"：
--   min_over_time((比值)[3m:30s]) 取窗口内每 30s 一次采样的**最小值**，
--   最小值都超门限 ⟺ 整整 3 分钟都超。比"连续 N 轮超限"精确，也不受漏采集影响。
-- 几个配套写法各有原因（详见 docker/seed_testbed_metrics.py 的长注释）：
--   · sum by (pod)    服务级求和会把"单副本跑飞"平均掉（3 副本里 1 个打满 = 33%）
--   · >= bool         输出 0/1 而不是把行过滤掉 ⇒ 每轮每 pod 恒定一行，于是
--                     signals_count 成了**采集健康度**指标，也绕开"空结果=静默成功"
--   · and on(pod) count_over_time(...) >= 4   样本不够就不判
--   · label_replace×2 sum() 会丢掉标签（含 __name__），映射会静默落成 "unknown"，
--                     而检测器按信号名匹配 ⇒ 永远不命中
--   · container!=""   ⚠️ **不能写成 container!="POD"**：cAdvisor 的 pod 级聚合序列其
--                     container 标签是**空串**，!="POD" 会把它留下 ⇒ 分子分母各算两遍，
--                     实测一个 limit=1 核的容器算出来是 2 核。
--
-- 症状线门限 = 5（百分数）。曾抬到 50 想让两条线同时越线，**实测证伪**（限流比 40 秒
-- 就到 99%，与门限无关）——"一次故障一张单"改在去重侧解决
--（storage/records.py 的 write_or_append 超集并入）。
INSERT INTO monitor_target
    (tenant_id, target_id, service, signal_type, source_type, domain, source_config, schedule, enabled)
SELECT
    'default',
    v.target_id,
    'order-service',
    'metric',
    'prometheus',
    'application',
    jsonb_build_object(
        -- 幂等判据的一部分：同一服务下有两条 metric 端点，
        -- 只按 (service, signal_type) 分不开（见 seed 脚本的 seed()）
        'label', v.label,
        'url', COALESCE(current_setting('aiops.testbed_prom_url', true), 'http://localhost:19090/api/v1/query'),
        'method', 'GET',
        'rows_path', 'data.result',
        'params', jsonb_build_object('query', v.query),
        'field_mapping', jsonb_build_object(
            'service', 'metric.service',
            'metric', 'metric.metric',
            'value', 'value[1]',
            'timestamp', 'value[0]',
            -- 让 per-pod 标签传下去：Prometheus 把标签放在 metric 对象里
            'labels', 'metric'
        )
    ),
    jsonb_build_object('interval_sec', 60),
    1
FROM (VALUES
    ('MT-0004', 'cpu_risk', '(label_replace(label_replace((min_over_time((sum by (pod) (rate(container_cpu_usage_seconds_total{pod=~"order-service.*",container!="",container!="POD"}[1m])) / sum by (pod) (container_spec_cpu_quota{pod=~"order-service.*",container!="",container!="POD"} / container_spec_cpu_period{pod=~"order-service.*",container!="",container!="POD"}))[3m:30s]) >= bool 0.8) and on(pod) (count_over_time((sum by (pod) (rate(container_cpu_usage_seconds_total{pod=~"order-service.*",container!="",container!="POD"}[1m])) / sum by (pod) (container_spec_cpu_quota{pod=~"order-service.*",container!="",container!="POD"} / container_spec_cpu_period{pod=~"order-service.*",container!="",container!="POD"}))[3m:30s]) >= 4),"metric", "cpu_usage", "pod", ".*"), "service", "order-service", "pod", ".*"))'),
    ('MT-0005', 'cpu_throttle', '(label_replace(label_replace((min_over_time((100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m])))[3m:30s]) >= bool 5) and on(pod) (count_over_time((100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{pod=~"order-service.*",container!="",container!="POD"}[1m])))[3m:30s]) >= 4),"metric", "cpu_throttled_percent", "pod", ".*"), "service", "order-service", "pod", ".*"))')
) AS v(target_id, label, query)
ON CONFLICT (tenant_id, target_id) DO NOTHING;

-- 域检测器：症状线要一条**自己的**（名字必须与 cpu_usage 不同，否则两条线的
-- anomaly_key 会撞，记录里分不出"是 CPU 满还是被限流"）。
-- 新库理论上能靠 `config/domains.yaml` 的 seed 拿到，但那只在**空表**时生效，
-- 而 V11 已经把这一行写进去了 ⇒ 不补这一刀，新库并不开箱可用。
-- NOT EXISTS 守卫让它可重复执行（迁移只跑一次，但手工 psql 重放时不该重复追加）。
UPDATE domain_config
   SET config = jsonb_set(
         config,
         '{detectors}',
         (config -> 'detectors') || '[{"signal": "cpu_throttled_percent", "plugin": "static_threshold",
                                        "params": {"threshold": 0.9}, "severity": "high"}]'::jsonb
       ),
       version = version + 1,
       updated_at = CURRENT_TIMESTAMP(3)
 WHERE tenant_id = 'default'
   AND domain = 'application'
   AND NOT EXISTS (
         SELECT 1
           FROM jsonb_array_elements(config -> 'detectors') AS d
          WHERE d ->> 'signal' = 'cpu_throttled_percent'
       );
