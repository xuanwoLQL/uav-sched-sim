-- =============================================================================
-- 01_schema.sql —— 无人机集群并发调度仿真系统 · MySQL 8.0 建库建表脚本
-- 项目：《操作系统》课程实训 · 面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统
-- 作者：eval-db   版本：v1.0   目标库：uav_sched_sim
-- -----------------------------------------------------------------------------
-- 【企业业务层 ↔ OS 原理层 ↔ 数据表 映射】（四层映射的表级落点）
--   ① 业务运行层：一次"30 台无人机同时上传"的业务演练
--        ↔ experiment            实验元信息（一次仿真 = 一次业务演练，可回溯）
--   ② 业务度量层：业务健康度（吞吐/时延/饥饿/完整率）
--        ↔ exp_metric            指标长表（可任意 pivot，对接报告"参数设计依据"）
--   ③ 业务明细层：每一个上传任务的完整生命周期（提交→排队→运行→重传→完成）
--        ↔ task_record           任务级明细（对应 OS 的 TCB / 进程控制块）
--        ↔ queue_sample          就绪队列时序（对应 OS 的就绪队列长度曲线）
--        ↔ link_sample           链路质量时序（对应 OS 的 I/O 设备阻塞率曲线）
--   ④ 业务字典层：工况与策略的定义
--        ↔ scenario_def          三种工况（LIGHT/NORMAL/PEAK）参数表
--        ↔ scheduler_def         五种调度策略字典表
--        ↔ v_exp_summary（视图） 一次 JOIN 出全部指标宽表，供报告与 Excel 直接引用
--
-- 【设计约束】
--   * 全部 CREATE ... IF NOT EXISTS，脚本**幂等**，可反复执行；
--   * 所有子表带 exp_id 外键（ON DELETE CASCADE），保证"任何一个数字都能回溯到具体那次仿真"；
--   * 时间列语义：xxx_ts DOUBLE 存**仿真秒**（从 0.0 起算）；
--     started_at/finished_at 用 DATETIME(3) 存 **wall-clock**（仅审计，不参与指标计算）；
--   * 字符集 utf8mb4 / utf8mb4_0900_ai_ci，避免中文注释乱码。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS uav_sched_sim
    DEFAULT CHARSET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE uav_sched_sim;

-- -----------------------------------------------------------------------------
-- 1. scenario_def —— 工况字典表（四层映射④业务字典层）
--    作用：把"参数设计依据"固化进数据库，报告可直接 `SELECT * FROM scenario_def`。
--    说明：本表初值取自《接口契约》§4.2；若 src/uav_sim/scenarios.py 可导入，
--          db.py 会以运行期真实 Scenario 对象为准做一次同步刷新（保证库与代码一致）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenario_def
(
    code               VARCHAR(16)   NOT NULL COMMENT '工况编码：LIGHT/NORMAL/PEAK',
    label              VARCHAR(64)   NOT NULL COMMENT '工况中文名',
    duration_s         DOUBLE        NOT NULL COMMENT '仿真时长（仿真秒）',
    uav_count          INT           NOT NULL COMMENT '参与无人机数量（= 生产者线程数）',
    high_ratio         DECIMAL(5, 4) NOT NULL DEFAULT 0 COMMENT '高优先级（勘查）任务占比',
    low_ratio          DECIMAL(5, 4) NOT NULL DEFAULT 0 COMMENT '低优先级（运维）任务占比',
    ops_interval_min   DOUBLE        NOT NULL DEFAULT 0 COMMENT '运维任务每机生成间隔下限（仿真秒）',
    ops_interval_max   DOUBLE        NOT NULL DEFAULT 0 COMMENT '运维任务每机生成间隔上限（仿真秒）',
    survey_gap_k       INT           NOT NULL DEFAULT 2 COMMENT '每 k 个运维周期生成 1 个勘查任务',
    high_size_min      DOUBLE        NOT NULL DEFAULT 0 COMMENT '勘查任务数据量下限（MB）',
    high_size_max      DOUBLE        NOT NULL DEFAULT 0 COMMENT '勘查任务数据量上限（MB）',
    low_size_min       DOUBLE        NOT NULL DEFAULT 0 COMMENT '运维任务数据量下限（MB）',
    low_size_max       DOUBLE        NOT NULL DEFAULT 0 COMMENT '运维任务数据量上限（MB）',
    churn_prob         DOUBLE        NOT NULL DEFAULT 0 COMMENT '每采样周期无人机动态上下线概率',
    weaknet_level      VARCHAR(8)    NOT NULL DEFAULT 'none' COMMENT '弱网档位：none/low/high',
    target_load_factor DOUBLE        NOT NULL DEFAULT 0 COMMENT '标定目标负载率 = 提供负载/系统容量',
    health_mu          DOUBLE        NOT NULL DEFAULT 90 COMMENT '链路健康度分布中心（0~100）',
    health_sigma       DOUBLE        NOT NULL DEFAULT 5 COMMENT '链路健康度分布标准差',
    note               VARCHAR(255)  DEFAULT NULL COMMENT '备注（参数设计依据）',
    updated_at         DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (code)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='工况字典：三种标准测试工况的参数设计依据';

-- 工况初值（契约 §4.2；运维间隔与 survey_gap_k 按标定模型反解，使 load_factor 命中 0.22/0.85/1.38）
INSERT INTO scenario_def (code, label, duration_s, uav_count, high_ratio, low_ratio,
                          ops_interval_min, ops_interval_max, survey_gap_k,
                          high_size_min, high_size_max, low_size_min, low_size_max,
                          churn_prob, weaknet_level, target_load_factor, health_mu, health_sigma, note)
VALUES ('LIGHT', '轻载日常巡检', 30, 5, 0.6000, 0.4000, 2.0, 5.0, 3, 100, 1000, 0.5, 3, 0.00, 'none', 0.22, 92, 5,
        '容量=8线程×150MB/s×链路折损；提供负载×重传放大后≈235MB/s，负载率0.21'),
       ('NORMAL', '中载常规勘查', 60, 15, 0.7000, 0.3000, 1.0, 3.0, 8, 100, 1500, 0.5, 5, 0.02, 'low', 0.85, 74, 9,
        '提供负载×重传放大≈756MB/s，负载率0.78；偶发队列堆积'),
       ('PEAK', '高并发峰值勘查', 90, 30, 0.8000, 0.2000, 0.5, 1.5, 30, 100, 2000, 0.5, 5, 0.03, 'high', 1.38, 52, 14,
        '提供负载×重传放大≈1204MB/s，负载率1.57，故意过载以复现峰值故障')
AS nv
ON DUPLICATE KEY UPDATE label              = nv.label,
                        duration_s         = nv.duration_s,
                        uav_count          = nv.uav_count,
                        high_ratio         = nv.high_ratio,
                        low_ratio          = nv.low_ratio,
                        ops_interval_min   = nv.ops_interval_min,
                        ops_interval_max   = nv.ops_interval_max,
                        survey_gap_k       = nv.survey_gap_k,
                        high_size_min      = nv.high_size_min,
                        high_size_max      = nv.high_size_max,
                        low_size_min       = nv.low_size_min,
                        low_size_max       = nv.low_size_max,
                        churn_prob         = nv.churn_prob,
                        weaknet_level      = nv.weaknet_level,
                        target_load_factor = nv.target_load_factor,
                        health_mu          = nv.health_mu,
                        health_sigma       = nv.health_sigma,
                        note               = nv.note;

-- -----------------------------------------------------------------------------
-- 2. scheduler_def —— 调度策略字典表（四层映射④业务字典层）
--    作用：把"策略 ↔ OS 原理 ↔ 是否有链路感知"固化，报告与图表图例共用。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduler_def
(
    code        VARCHAR(16)  NOT NULL COMMENT '策略编码（与 models.SCHEDULER_NAMES 一致）',
    label       VARCHAR(64)  NOT NULL COMMENT '策略中文显示名（图表图例用）',
    link_aware  TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否具备链路感知（决定弱网走 CACHED 还是原样重传）',
    description VARCHAR(512) NOT NULL DEFAULT '' COMMENT '核心规则说明',
    sort_order  INT          NOT NULL DEFAULT 0 COMMENT '展示顺序',
    updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (code)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='调度策略字典：五种策略与 OS 原理对应关系';

INSERT INTO scheduler_def (code, label, link_aware, description, sort_order)
VALUES ('fcfs', 'FCFS 先来先服务', 0, '单一 FIFO 队列，不区分优先级；公平但无差别，核心勘查任务被运维任务挤占', 1),
       ('rr', '固定时间片轮转 RR', 0, '固定时间片轮转，未完成放回队尾；响应均匀但上下文切换多、大任务抖动', 2),
       ('priority', '静态优先级', 0, '严格优先级：高优先级队列非空必取高；低优先级任务饥饿（教学痛点复现）', 3),
       ('mlfq', '多级反馈队列 MLFQ', 0, '新任务进高队列，时间片耗尽降级到低队列；低队列定期 aging 升权，缓解饥饿但参数敏感', 4),
       ('dynamic', '动态权重自适应（本方案）', 1,
        'W=base×(w0+αQ+βW_wait+γL)×(1-δS)，饥饿硬补偿，自适应时间片，弱网降权+本地缓存，过载收敛', 5)
AS nv
ON DUPLICATE KEY UPDATE label       = nv.label,
                        link_aware  = nv.link_aware,
                        description = nv.description,
                        sort_order  = nv.sort_order;

-- -----------------------------------------------------------------------------
-- 3. experiment —— 实验元信息（四层映射①业务运行层）
--    exp_id 为 VARCHAR(40)：格式 <工况>-<策略>-<seed>-<时间戳>，可读且可回溯。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment
(
    exp_id      VARCHAR(40) NOT NULL COMMENT '实验唯一号（RunResult.exp_id）',
    scenario    VARCHAR(16) NOT NULL COMMENT '工况编码',
    scheduler   VARCHAR(16) NOT NULL COMMENT '调度策略编码',
    seed        INT         NOT NULL DEFAULT 0 COMMENT '随机种子（控制变量：同 seed 同轨迹）',
    duration_s  DOUBLE      NOT NULL DEFAULT 0 COMMENT '仿真时长（仿真秒）',
    uav_count   INT         NOT NULL DEFAULT 0 COMMENT '无人机数量',
    started_at  DATETIME(3) NULL COMMENT 'wall-clock 开始时刻（仅审计）',
    finished_at DATETIME(3) NULL COMMENT 'wall-clock 结束时刻（仅审计）',
    status      VARCHAR(16) NOT NULL DEFAULT 'OK' COMMENT 'OK / ERROR',
    error_text  TEXT        NULL COMMENT '异常摘要（优雅降级：失败也要留下痕迹）',
    config_json LONGTEXT    NULL COMMENT '本次运行的 config.yaml 快照（JSON）',
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (exp_id),
    KEY idx_experiment_scenario (scenario, scheduler),
    KEY idx_experiment_created (created_at),
    CONSTRAINT fk_experiment_scenario FOREIGN KEY (scenario) REFERENCES scenario_def (code) ON UPDATE CASCADE,
    CONSTRAINT fk_experiment_scheduler FOREIGN KEY (scheduler) REFERENCES scheduler_def (code) ON UPDATE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='实验元信息：一行 = 一次「工况×策略」仿真';

-- -----------------------------------------------------------------------------
-- 4. exp_metric —— 指标长表（四层映射②业务度量层）
--    长表设计：新增指标不需要改表结构，可直接 PIVOT 成宽表（见 v_exp_summary）。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exp_metric
(
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    exp_id       VARCHAR(40)     NOT NULL COMMENT '所属实验',
    metric_name  VARCHAR(64)     NOT NULL COMMENT '指标名（必须来自 models.METRIC_SPEC）',
    metric_value DOUBLE          NOT NULL DEFAULT 0 COMMENT '指标值',
    unit         VARCHAR(16)     NOT NULL DEFAULT '' COMMENT '单位',
    scope        VARCHAR(8)      NOT NULL DEFAULT 'ALL' COMMENT '统计范围 ALL/HIGH/LOW',
    PRIMARY KEY (id),
    UNIQUE KEY uk_exp_metric (exp_id, metric_name),
    KEY idx_exp_metric_name (metric_name),
    CONSTRAINT fk_exp_metric_exp FOREIGN KEY (exp_id) REFERENCES experiment (exp_id) ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='指标长表：一行 = 一个指标，便于任意 pivot';

-- -----------------------------------------------------------------------------
-- 5. task_record —— 任务级明细（四层映射③业务明细层；对应 OS 的 TCB）
--    报告附录/回溯用：任何一个"平均等待时延"都能下钻到每个任务。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_record
(
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    exp_id         VARCHAR(40)     NOT NULL,
    task_id        VARCHAR(64)     NOT NULL COMMENT '任务号（跨实验可追溯）',
    parent_task_id VARCHAR(64)     NULL COMMENT '分片的原始任务号（未分片为 NULL）',
    uav_id         INT             NOT NULL COMMENT '所属无人机',
    priority       VARCHAR(8)      NOT NULL COMMENT 'HIGH/LOW',
    task_type      VARCHAR(24)     NOT NULL COMMENT 'IMAGERY/MULTISPECTRAL/LIDAR/GPS/...',
    data_size_mb   DOUBLE          NOT NULL DEFAULT 0,
    segment_index  INT             NOT NULL DEFAULT 0,
    segment_total  INT             NOT NULL DEFAULT 1,
    service_s      DOUBLE          NOT NULL DEFAULT 0 COMMENT '标定服务时长（仿真秒）',
    submit_ts      DOUBLE          NOT NULL DEFAULT 0 COMMENT '提交时刻（仿真秒）',
    first_start_ts DOUBLE          NULL COMMENT '首次被调度时刻（仿真秒）',
    finish_ts      DOUBLE          NULL COMMENT '完成/丢弃时刻（仿真秒）',
    wait_ms        DOUBLE          NOT NULL DEFAULT 0 COMMENT '累计等待时延（毫秒）',
    turnaround_ms  DOUBLE          NOT NULL DEFAULT 0 COMMENT '周转时间（毫秒）',
    requeue_count  INT             NOT NULL DEFAULT 0,
    retry_count    INT             NOT NULL DEFAULT 0,
    preempt_count  INT             NOT NULL DEFAULT 0,
    link_health    DOUBLE          NOT NULL DEFAULT 100 COMMENT '提交时刻链路健康度 0~100',
    cached         TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否进入过本地缓存',
    state          VARCHAR(12)     NOT NULL DEFAULT 'NEW' COMMENT 'NEW/READY/RUNNING/BLOCKED/CACHED/DONE/DROPPED',
    drop_reason    VARCHAR(64)     NULL COMMENT '丢弃原因',
    PRIMARY KEY (id),
    KEY idx_task_exp (exp_id),
    KEY idx_task_exp_priority (exp_id, priority),
    KEY idx_task_exp_state (exp_id, state),
    CONSTRAINT fk_task_exp FOREIGN KEY (exp_id) REFERENCES experiment (exp_id) ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='任务级明细：一行 = 一个上传任务的完整生命周期';

-- -----------------------------------------------------------------------------
-- 6. queue_sample —— 就绪队列时序（四层映射③业务明细层）
--    对应 OS 的"就绪队列长度随时间变化曲线"，是复现"低优先级单调堆积"的证据源。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS queue_sample
(
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    exp_id             VARCHAR(40)     NOT NULL,
    ts                 DOUBLE          NOT NULL COMMENT '采样时刻（仿真秒）',
    high_depth         INT             NOT NULL DEFAULT 0 COMMENT '高优先级队列深度',
    low_depth          INT             NOT NULL DEFAULT 0 COMMENT '低优先级队列深度',
    running_count      INT             NOT NULL DEFAULT 0 COMMENT '运行中任务数',
    blocked_count      INT             NOT NULL DEFAULT 0 COMMENT '阻塞（弱网重传）任务数',
    system_load        DOUBLE          NOT NULL DEFAULT 0 COMMENT '系统负载 = 队列需求/容量',
    high_time_slice_ms DOUBLE          NOT NULL DEFAULT 0 COMMENT '当前高优先级时间片（毫秒）',
    low_time_slice_ms  DOUBLE          NOT NULL DEFAULT 0 COMMENT '当前低优先级时间片（毫秒）',
    high_weight        DOUBLE          NOT NULL DEFAULT 0 COMMENT '当前高优先级权重',
    low_weight         DOUBLE          NOT NULL DEFAULT 0 COMMENT '当前低优先级权重',
    PRIMARY KEY (id),
    KEY idx_queue_exp_ts (exp_id, ts),
    CONSTRAINT fk_queue_exp FOREIGN KEY (exp_id) REFERENCES experiment (exp_id) ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='队列时序采样：就绪队列堆积曲线数据源';

-- -----------------------------------------------------------------------------
-- 7. link_sample —— 链路质量时序（四层映射③业务明细层）
--    对应 OS 的"I/O 设备阻塞率/等待队列"，用于说明弱网降权与自适应机制。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS link_sample
(
    id        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    exp_id    VARCHAR(40)     NOT NULL,
    ts        DOUBLE          NOT NULL COMMENT '采样时刻（仿真秒）',
    uav_id    INT             NOT NULL,
    delay_ms  DOUBLE          NOT NULL DEFAULT 0 COMMENT '链路时延（毫秒）',
    loss_rate DOUBLE          NOT NULL DEFAULT 0 COMMENT '丢包率 0~1',
    rssi_dbm  DOUBLE          NOT NULL DEFAULT 0 COMMENT '信号强度 dBm',
    health    DOUBLE          NOT NULL DEFAULT 0 COMMENT '综合健康度 0~100',
    PRIMARY KEY (id),
    KEY idx_link_exp_uav_ts (exp_id, uav_id, ts),
    CONSTRAINT fk_link_exp FOREIGN KEY (exp_id) REFERENCES experiment (exp_id) ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='链路时序采样：弱网自适应机制证据源';

-- -----------------------------------------------------------------------------
-- 8. v_exp_summary —— 指标宽表视图（四层映射②业务度量层）
--    一次 JOIN 出全部指标，`SELECT * FROM v_exp_summary` 即得报告用总表。
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_exp_summary AS
SELECT e.exp_id                                                          AS exp_id,
       e.scenario                                                        AS scenario,
       d_s.label                                                         AS scenario_label,
       e.scheduler                                                       AS scheduler,
       d_c.label                                                         AS scheduler_label,
       e.seed                                                            AS seed,
       e.duration_s                                                      AS duration_s,
       e.uav_count                                                       AS uav_count,
       e.status                                                          AS status,
       MAX(CASE WHEN m.metric_name = 'throughput' THEN m.metric_value END)           AS throughput,
       MAX(CASE WHEN m.metric_name = 'avg_wait_high_ms' THEN m.metric_value END)     AS avg_wait_high_ms,
       MAX(CASE WHEN m.metric_name = 'avg_wait_low_ms' THEN m.metric_value END)      AS avg_wait_low_ms,
       MAX(CASE WHEN m.metric_name = 'avg_wait_all_ms' THEN m.metric_value END)      AS avg_wait_all_ms,
       MAX(CASE WHEN m.metric_name = 'avg_turnaround_ms' THEN m.metric_value END)    AS avg_turnaround_ms,
       MAX(CASE WHEN m.metric_name = 'p95_wait_high_ms' THEN m.metric_value END)     AS p95_wait_high_ms,
       MAX(CASE WHEN m.metric_name = 'p95_wait_low_ms' THEN m.metric_value END)      AS p95_wait_low_ms,
       MAX(CASE WHEN m.metric_name = 'starvation_rate' THEN m.metric_value END)      AS starvation_rate,
       MAX(CASE WHEN m.metric_name = 'starvation_rate_low' THEN m.metric_value END)  AS starvation_rate_low,
       MAX(CASE WHEN m.metric_name = 'resource_utilization' THEN m.metric_value END) AS resource_utilization,
       MAX(CASE WHEN m.metric_name = 'completion_rate' THEN m.metric_value END)      AS completion_rate,
       MAX(CASE WHEN m.metric_name = 'drop_rate' THEN m.metric_value END)            AS drop_rate,
       MAX(CASE WHEN m.metric_name = 'retry_rate' THEN m.metric_value END)           AS retry_rate,
       MAX(CASE WHEN m.metric_name = 'avg_requeue' THEN m.metric_value END)          AS avg_requeue,
       MAX(CASE WHEN m.metric_name = 'max_high_depth' THEN m.metric_value END)       AS max_high_depth,
       MAX(CASE WHEN m.metric_name = 'max_low_depth' THEN m.metric_value END)        AS max_low_depth,
       MAX(CASE WHEN m.metric_name = 'peak_queue_depth' THEN m.metric_value END)     AS peak_queue_depth,
       MAX(CASE WHEN m.metric_name = 'context_switch' THEN m.metric_value END)       AS context_switch,
       MAX(CASE WHEN m.metric_name = 'jitter_ms' THEN m.metric_value END)            AS jitter_ms,
       MAX(CASE WHEN m.metric_name = 'overload_seconds' THEN m.metric_value END)     AS overload_seconds,
       MAX(CASE WHEN m.metric_name = 'throttle_count' THEN m.metric_value END)       AS throttle_count,
       MAX(CASE WHEN m.metric_name = 'reject_large_count' THEN m.metric_value END)   AS reject_large_count,
       MAX(CASE WHEN m.metric_name = 'chunk_count' THEN m.metric_value END)          AS chunk_count,
       MAX(CASE WHEN m.metric_name = 'runtime_wall_s' THEN m.metric_value END)       AS runtime_wall_s
FROM experiment e
         LEFT JOIN exp_metric m ON m.exp_id = e.exp_id
         LEFT JOIN scenario_def d_s ON d_s.code = e.scenario
         LEFT JOIN scheduler_def d_c ON d_c.code = e.scheduler
GROUP BY e.exp_id, e.scenario, d_s.label, e.scheduler, d_c.label,
         e.seed, e.duration_s, e.uav_count, e.status;

-- -----------------------------------------------------------------------------
-- 执行结果自检（在 mysql 客户端会打印；在 db.py 里可通过 split 后忽略 SELECT）
-- -----------------------------------------------------------------------------
SELECT 'schema ready' AS step,
       (SELECT COUNT(*) FROM scenario_def)  AS scenario_def_rows,
       (SELECT COUNT(*) FROM scheduler_def) AS scheduler_def_rows;
