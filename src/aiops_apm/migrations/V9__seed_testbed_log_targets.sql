-- V9：测试床三服务的日志监控端点（首次初始化数据）。
--
-- 给 minikube `order` 命名空间里的 order-service / warranty-service / gateway-service
-- 各建一个日志监控端点，由 scheduler 按 schedule.interval_sec 定时触发采集 → L0–L3 → 开单。
--
-- 这是**首次在迁移里放业务数据**（V1–V8 全是 DDL）。理由：用户要求这三个端点随
-- `make migrate` 一并就位，不需要额外记得跑 seed 脚本。
-- 迁移是一次性的、不可变的——之后要改这三个端点的配置，**不要改本文件**，走
-- `docker/seed_testbed_logs.py`（可重复执行、会刷新 source_config）或管理 API。
--
-- ES 地址来自 GUC：SQL 是静态文件读不到环境变量，由 MigrationRunner 用
-- set_config('aiops.testbed_es_url', ...) 注入（值取自 APM_TESTBED_ES_URL）。
-- 本机 port-forward 是 localhost:19200；**容器里 localhost 指向容器自己**，
-- 需传 host.containers.internal:19200。GUC 未设时 COALESCE 回落到同样的默认值，
-- 保证手工用 psql 跑这个文件也能得到可用配置。
--
-- 幂等：ON CONFLICT DO NOTHING。已存在同 target_id 的行就跳过——迁移不该覆盖现网数据。
-- target_id 必须是 MT-NNNN 形态：MonitorTargetStore._parse_suffix 解析失败会返回 0，
-- 下一个新建端点会拿到 MT-0001 而撞唯一键。

INSERT INTO monitor_target
    (tenant_id, target_id, service, signal_type, source_type, domain, source_config, schedule, enabled)
SELECT
    'default',
    v.target_id,
    v.service,
    'log',
    'elk',
    'application',
    jsonb_build_object(
        -- 时间窗与服务过滤放 POST body 的 ES 查询 DSL：ES 的日期 range 只认 body，
        -- 写进 URL 参数会 400；ES 侧 term 过滤需要 .keyword 后缀。
        'url', COALESCE(current_setting('aiops.testbed_es_url', true), 'http://localhost:19200/app-logs/_search'),
        'method', 'POST',
        'rows_path', 'hits.hits',
        'time_field', '@timestamp',
        'service_field', 'app.service.keyword',
        -- 路径带 _source. 前缀：采集器不剥壳（M3 起的约定，见 tests/test_collectors.py）
        -- stack_trace 必须映射：signature() 有堆栈时取「异常首行|顶部N帧」，缺了它回退到
        -- message[:120] —— Spring 的 "Servlet.service() for servlet [dispatcherServlet]..."
        -- 前缀对**所有**异常都一样，真正的异常类型在 120 字符之外被截掉，于是不同类型的
        -- error 全塌成同一个签名、归成同一条记录（与「不同类型 error 各自成单」正好相反）。
        'field_mapping', jsonb_build_object(
            'service', '_source.app.service',
            'level', '_source.app.level',
            'message', '_source.app.message',
            'timestamp', '_source.@timestamp',
            'trace_id', '_source.app.traceId',
            'stack_trace', '_source.app.stack_trace'
        )
    ),
    jsonb_build_object('interval_sec', 60),
    1
FROM (VALUES
    ('MT-0001', 'order-service'),
    ('MT-0002', 'warranty-service'),
    ('MT-0003', 'gateway-service')
) AS v(target_id, service)
ON CONFLICT (tenant_id, target_id) DO NOTHING;
