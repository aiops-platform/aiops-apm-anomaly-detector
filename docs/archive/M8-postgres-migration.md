# M8 归档：MySQL 版 DDL（已被 PostgreSQL 取代）

> **归档说明**：以下 DDL 摘自 `docs/apm-alert-module-design.md` §7.2/§7.3 的原始设计，
> 是 M2–M7 期间实际使用的 **MySQL** schema。M8（存储层 PostgreSQL 化）已把它整体改写为
> PG 方言，权威定义现在在 [`src/aiops_apm/migrations/V1..V8__*.sql`](../../src/aiops_apm/migrations/)。
>
> 保留此文件是为了记录**表结构与列的演进意图**（哪些列为什么存在），这些语义与方言无关、
> 依然有效；MySQL 专属的语法细节不必再参考。方言映射与 PG 专属陷阱见
> [`docs/logs/M8.md`](../logs/M8.md)。

---

## 7.2 DDL（aiops_apm_runtime 库，Python 直连）

```sql
CREATE DATABASE IF NOT EXISTS aiops_apm_runtime
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE aiops_apm_runtime;

CREATE TABLE IF NOT EXISTS problem_record (
    record_id        VARCHAR(32)   NOT NULL PRIMARY KEY COMMENT 'PR-YYYYMMDD-NNNN',
    group_key        VARCHAR(255)  NOT NULL COMMENT 'tenant_id:domain:service:anomaly_type 去重键',
    source           VARCHAR(64)   NOT NULL COMMENT '记录来源模块（固定 apm-alert）',
    tenant_id        VARCHAR(64)   NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain           VARCHAR(32)   NOT NULL,
    state            VARCHAR(16)   NOT NULL DEFAULT 'pending',
    service          VARCHAR(64)   NOT NULL,
    instance         VARCHAR(128)  DEFAULT NULL,
    detected_at      DATETIME(3)   NOT NULL,
    symptom          JSON,
    metric_anomalies JSON,
    log_anomalies    JSON,
    correlation      JSON,
    change_related   TINYINT(1)    NOT NULL DEFAULT 0,
    recent_change    JSON,
    verification     JSON,
    evidence         JSON          COMMENT '去重时追加的证据',
    trace_id         VARCHAR(64)   DEFAULT NULL,
    created_at       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    INDEX idx_group_key (group_key),
    INDEX idx_tenant_state (tenant_id, state),
    INDEX idx_tenant_domain_service (tenant_id, domain, service),
    INDEX idx_detected_at (detected_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS change_record (
    change_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    service       VARCHAR(64)  NOT NULL,
    type          VARCHAR(16)  NOT NULL COMMENT 'deployment/ddl/config',
    summary       VARCHAR(500) DEFAULT NULL,
    changed_at    DATETIME(3)  NOT NULL,
    metadata      JSON,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_service_time (tenant_id, service, changed_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS domain_config (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain     VARCHAR(32)  NOT NULL COMMENT '域 id，如 application',
    config     JSON         NOT NULL COMMENT '域检测规则(detectors/suppressors/correlation/verify)',
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    version    INT          NOT NULL DEFAULT 1,
    updated_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_domain (tenant_id, domain)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS monitor_target (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(32)  NOT NULL COMMENT '对外唯一 id，如 MT-0001',
    service       VARCHAR(64)  NOT NULL COMMENT '被监控服务，如 order-management',
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'log / metric',
    source_type   VARCHAR(16)  NOT NULL COMMENT 'http / prometheus / elk',
    domain        VARCHAR(32)  NOT NULL DEFAULT 'application' COMMENT '归属域（决定应用哪套检测规则）',
    source_config JSON         NOT NULL COMMENT '采集端点配置(url/method/headers/params/field_mapping)',
    schedule      JSON         NOT NULL COMMENT '定时任务(interval_sec 或 cron)',
    enabled       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_target_id (tenant_id, target_id),
    INDEX idx_tenant_service (tenant_id, service),
    INDEX idx_tenant_enabled (tenant_id, enabled)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS maintenance_window (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    service    VARCHAR(64)  NOT NULL,
    start_at   DATETIME(3)  NOT NULL,
    end_at     DATETIME(3)  NOT NULL,
    reason     VARCHAR(255) DEFAULT NULL,
    created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_service_time (tenant_id, service, start_at, end_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS suppress_blacklist (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id  VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain     VARCHAR(32)  NOT NULL,
    service    VARCHAR(64)  NOT NULL,
    signal     VARCHAR(64)  NOT NULL COMMENT 'metric/log pattern',
    reason     VARCHAR(255) DEFAULT NULL,
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_domain_service (tenant_id, domain, service)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fpr_table (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id           VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    group_key           VARCHAR(255) NOT NULL COMMENT 'tenant_id:domain:service:anomaly_type',
    false_positive_cnt  BIGINT NOT NULL DEFAULT 0,
    total_cnt           BIGINT NOT NULL DEFAULT 0,
    fpr                 DECIMAL(5,4) NOT NULL DEFAULT 0,
    updated_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_group_key (tenant_id, group_key)
) ENGINE=InnoDB;
```

## 7.3 DDL（v2 运行时/历史表，同一 `aiops_apm_runtime` 库）

以下 3 张 v2 表与 §7.2 同属单一 `aiops_apm_runtime` 库（不再独立 schema）。信号快照量大，建议按 `snapshot_ts` 分区/定期归档；状态/审计生命周期短可独立清理。

```sql
-- 信号历史快照：采集到的原始指标/日志，支撑基线/环比/ML（量大，建议分区/归档）
CREATE TABLE IF NOT EXISTS signal_snapshot (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    snapshot_ts   DATETIME(3)  NOT NULL COMMENT '采集轮次时间',
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(32)  NOT NULL COMMENT '来源监控端点',
    service       VARCHAR(64)  NOT NULL,
    domain        VARCHAR(32)  NOT NULL,
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'metric / log',
    metric        VARCHAR(64)  DEFAULT NULL COMMENT 'signal_type=metric',
    value         DOUBLE       DEFAULT NULL COMMENT 'signal_type=metric',
    level         VARCHAR(16)  DEFAULT NULL COMMENT 'signal_type=log',
    message       TEXT         DEFAULT NULL COMMENT 'signal_type=log',
    signature     VARCHAR(255) DEFAULT NULL COMMENT '日志堆栈签名',
    labels        JSON         DEFAULT NULL COMMENT 'metric labels / log 附加字段',
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_target_time (tenant_id, target_id, snapshot_ts),
    INDEX idx_tenant_service_metric (tenant_id, service, metric, snapshot_ts),
    INDEX idx_tenant_service_level (tenant_id, service, level, snapshot_ts)
) ENGINE=InnoDB COMMENT='原始信号快照，量大，建议按 snapshot_ts 分区/定期归档';

-- 跨轮检测状态：L3 持续性的「上一轮 anomaly keys」等
CREATE TABLE IF NOT EXISTS detection_state (
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain        VARCHAR(32)  NOT NULL,
    state_key     VARCHAR(64)  NOT NULL COMMENT '如 previous_keys',
    state_value   JSON         NOT NULL,
    updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, domain, state_key)
) ENGINE=InnoDB;

-- 轮次审计：每轮 trace_id + 阶段统计 + timeline
CREATE TABLE IF NOT EXISTS detection_round (
    round_id          VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '即 trace_id',
    tenant_id         VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    started_at        DATETIME(3)  NOT NULL,
    finished_at       DATETIME(3)  DEFAULT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/success/partial/failed',
    target_ids        JSON         COMMENT '本轮涉及的监控端点',
    signals_count     INT          NOT NULL DEFAULT 0,
    anomaly_count     INT          NOT NULL DEFAULT 0,
    record_count      INT          NOT NULL DEFAULT 0,
    suppressed_count  INT          NOT NULL DEFAULT 0,
    degraded_sources  JSON         COMMENT '降级的采集源',
    timeline          JSON         COMMENT '各阶段耗时/时间戳',
    created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_started_at (tenant_id, started_at)
) ENGINE=InnoDB;
```

---

