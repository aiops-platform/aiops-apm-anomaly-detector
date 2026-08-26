-- V1 初始化：建齐单一 schema aiops_apm_runtime 的 12 张表。
-- 镜像设计文档 §7.2/7.3 DDL + M2 计划补充的 P0 列（problem_record severity/生命周期列）
-- 与 record_seq / scheduler_lease 两张运行时表。

CREATE DATABASE IF NOT EXISTS aiops_apm_runtime
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE aiops_apm_runtime;

-- problem_record：M5 emit 的最终产出，P0 列含 severity 与 open_group_key 原子去重机制。
-- 并发 write_or_append 同 group_key 只产生一条记录：open_group_key 生成列 + UNIQUE + ON DUPLICATE KEY UPDATE。
CREATE TABLE IF NOT EXISTS problem_record (
    record_id        VARCHAR(32)   NOT NULL PRIMARY KEY COMMENT 'PR-YYYYMMDD-NNNN',
    group_key        VARCHAR(255)  NOT NULL COMMENT 'tenant_id:domain:service:<hash> 去重键',
    source           VARCHAR(64)   NOT NULL DEFAULT 'apm-alert' COMMENT '记录来源模块',
    tenant_id        VARCHAR(64)   NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain           VARCHAR(32)   NOT NULL,
    state            VARCHAR(16)   NOT NULL DEFAULT 'pending' COMMENT 'pending/in_progress/resolved/closed/archived',
    service          VARCHAR(64)   NOT NULL,
    instance         VARCHAR(128)  DEFAULT NULL,
    severity         VARCHAR(16)   NOT NULL DEFAULT 'warning' COMMENT 'warning/high/critical',
    detected_at      DATETIME(3)   NOT NULL,
    first_seen_at    DATETIME(3)   DEFAULT NULL,
    last_seen_at     DATETIME(3)   DEFAULT NULL,
    occurrence_count INT           NOT NULL DEFAULT 1,
    resolved_at      DATETIME(3)   DEFAULT NULL,
    resolve_reason   VARCHAR(255)  DEFAULT NULL,
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
    -- 原子去重：state 为 open 时 group_key 参与 UNIQUE，resolved 后自动变 NULL 允许复发开新单
    open_group_key   VARCHAR(255) GENERATED ALWAYS AS (
        CASE WHEN state IN ('pending', 'in_progress') THEN group_key ELSE NULL END
    ) STORED,
    UNIQUE KEY uk_open_group_key (tenant_id, open_group_key),
    INDEX idx_group_key (group_key),
    INDEX idx_tenant_state (tenant_id, state),
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
    service       VARCHAR(64)  NOT NULL COMMENT '被监控服务',
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'log / metric',
    source_type   VARCHAR(16)  NOT NULL COMMENT 'http / prometheus / elk',
    domain        VARCHAR(32)  NOT NULL DEFAULT 'application' COMMENT '归属域',
    source_config JSON         NOT NULL COMMENT '采集端点配置',
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
    `signal`   VARCHAR(64)  NOT NULL COMMENT 'metric/log pattern',
    reason     VARCHAR(255) DEFAULT NULL,
    enabled    TINYINT(1)   NOT NULL DEFAULT 1,
    created_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_domain_service (tenant_id, domain, service)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fpr_table (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenant_id           VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    group_key           VARCHAR(255) NOT NULL,
    false_positive_cnt  BIGINT NOT NULL DEFAULT 0,
    total_cnt           BIGINT NOT NULL DEFAULT 0,
    fpr                 DECIMAL(5,4) NOT NULL DEFAULT 0,
    updated_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    UNIQUE KEY uk_tenant_group_key (tenant_id, group_key)
) ENGINE=InnoDB;

-- record_seq：record_id 原子取号（PR-YYYYMMDD-NNNN），按日期维护自增序列
CREATE TABLE IF NOT EXISTS record_seq (
    seq_date  VARCHAR(8)  NOT NULL PRIMARY KEY COMMENT 'YYYYMMDD',
    next_seq  BIGINT      NOT NULL DEFAULT 1
) ENGINE=InnoDB;

-- scheduler_lease：多副本选主（行锁 + TTL 续约 + 崩溃自动接管）
CREATE TABLE IF NOT EXISTS scheduler_lease (
    lease_name VARCHAR(64)  NOT NULL PRIMARY KEY,
    holder     VARCHAR(128) DEFAULT NULL,
    acquired_at DATETIME(3) DEFAULT NULL,
    expires_at DATETIME(3)  DEFAULT NULL,
    updated_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS signal_snapshot (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    snapshot_ts   DATETIME(3)  NOT NULL COMMENT '采集轮次时间',
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    target_id     VARCHAR(32)  NOT NULL COMMENT '来源监控端点',
    service       VARCHAR(64)  NOT NULL,
    domain        VARCHAR(32)  NOT NULL,
    signal_type   VARCHAR(16)  NOT NULL COMMENT 'metric / log',
    metric        VARCHAR(64)  DEFAULT NULL,
    value         DOUBLE       DEFAULT NULL,
    level         VARCHAR(16)  DEFAULT NULL,
    message       TEXT         DEFAULT NULL,
    signature     VARCHAR(255) DEFAULT NULL COMMENT '日志堆栈签名',
    labels        JSON         DEFAULT NULL,
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_target_time (tenant_id, target_id, snapshot_ts),
    INDEX idx_tenant_service_metric (tenant_id, service, metric, snapshot_ts),
    INDEX idx_tenant_service_level (tenant_id, service, level, snapshot_ts)
) ENGINE=InnoDB COMMENT='原始信号快照，量大，建议按 snapshot_ts 分区/定期归档';

CREATE TABLE IF NOT EXISTS detection_state (
    tenant_id     VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    domain        VARCHAR(32)  NOT NULL,
    state_key     VARCHAR(64)  NOT NULL COMMENT '如 previous_keys',
    state_value   JSON         NOT NULL,
    updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (tenant_id, domain, state_key)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS detection_round (
    round_id          VARCHAR(64)  NOT NULL PRIMARY KEY COMMENT '即 trace_id',
    tenant_id         VARCHAR(64)  NOT NULL DEFAULT 'default' COMMENT '多租户隔离',
    started_at        DATETIME(3)  NOT NULL,
    finished_at       DATETIME(3)  DEFAULT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/success/partial/failed',
    target_ids        JSON,
    signals_count     INT          NOT NULL DEFAULT 0,
    anomaly_count     INT          NOT NULL DEFAULT 0,
    record_count      INT          NOT NULL DEFAULT 0,
    suppressed_count  INT          NOT NULL DEFAULT 0,
    degraded_sources  JSON,
    timeline          JSON,
    created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_tenant_started_at (tenant_id, started_at)
) ENGINE=InnoDB;
