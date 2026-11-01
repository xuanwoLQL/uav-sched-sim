"""
db.py —— MySQL 8.0 持久化层（实验数据落库）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 每次仿真就是一次"业务演练"，企业需要事后复盘：这次演练的每个指标、每个任务、每条链路的时序都要能查 |
| OS 原理层   | 等价于内核的**审计日志 / 性能采样留痕**（`/var/log`、`ftrace`、`perf`）：没有留痕就没有可回溯性 |
| 算法设计层 | 长表（metric）+ 明细表（task/queue/link）分离，支持任意 PIVOT；外键保证"任何一个数字都能回溯到那次仿真" |
| 代码实现层 | `DBWriter`（PyMySQL）+ `sql/01_schema.sql` |

--------------------------------------------------------------------------
优雅降级（**铁律**）
--------------------------------------------------------------------------
数据库不可用（没启动 / 密码错 / 端口不通 / 权限不足）时：
    * `connect()` 返回 False 并打印中文提示；
    * `save_run()` 返回 False，**绝不抛异常**，实验继续跑、结果照常落 CSV/JSON。
理由：教学实验的主产物是"仿真 + 数据 + 图表"，MySQL 只是增强项；
让数据库故障把 15 组实验全废掉是不可接受的设计。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import project_root
from uav_sim.models import METRIC_SPEC

#: 语句切分：以行尾的 ';' 作为语句边界（`sql/01_schema.sql` 里没有存储过程，安全）
_STMT_END = re.compile(r";\s*$")


def _split_sql(text: str) -> List[str]:
    """把 .sql 文件切成可逐条执行的语句列表（跳过 `--` 注释行）。"""
    stmts: List[str] = []
    buf: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if _STMT_END.search(stripped):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        stmts.append(tail)
    return stmts


def _to_mysql_dt(value: Any) -> Optional[str]:
    """把 ISO8601 字符串 / datetime 转成 MySQL DATETIME(3) 可接受的文本。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return text[:23]


class DBWriter:
    """MySQL 写入器（契约 §4.9）。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}
        self.db_cfg: Dict[str, Any] = dict(self.cfg.get("database", {}) or {})
        self.database: str = str(self.db_cfg.get("database", "uav_sched_sim"))
        self.batch_size: int = int(self.db_cfg.get("batch_size", 2000) or 2000)
        self._conn = None
        self._schema_ready = False
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ 连接
    @property
    def password(self) -> str:
        return (os.environ.get("UAV_DB_PASSWORD")
                or str(self.db_cfg.get("password", "")))

    def connect(self) -> bool:
        """建立连接（**不指定默认库**，以便执行 CREATE DATABASE）。失败返回 False。"""
        if self._conn is not None:
            return True
        try:
            import pymysql  # 延迟导入：缺依赖时给出友好提示
        except ImportError as exc:  # pragma: no cover
            self.last_error = f"缺少 PyMySQL：{exc}"
            print("[db] 未安装 PyMySQL，跳过入库（结果仍会写入 CSV/JSON）")
            print("     安装：.venv\\Scripts\\python.exe -m pip install PyMySQL")
            return False
        try:
            self._conn = pymysql.connect(
                host=str(self.db_cfg.get("host", "127.0.0.1")),
                port=int(self.db_cfg.get("port", 3306)),
                user=str(self.db_cfg.get("user", "root")),
                password=self.password,
                charset=str(self.db_cfg.get("charset", "utf8mb4")),
                autocommit=True,
                connect_timeout=5,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 任何连接异常都降级
            self.last_error = str(exc)
            self._conn = None
            print(f"[db] MySQL 连接失败：{exc}")
            print(f"     目标 {self.db_cfg.get('host')}:{self.db_cfg.get('port')} "
                  f"user={self.db_cfg.get('user')} —— 实验继续，结果只落本地文件。")
            print("     排查：1) MySQL 服务是否启动  2) 密码/环境变量 UAV_DB_PASSWORD  "
                  "3) 端口是否放通")
            return False

    def available(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------ 建表
    def ensure_schema(self) -> bool:
        """执行建库建表脚本（幂等）并同步工况字典，失败返回 False。"""
        if self._schema_ready:
            return True
        if not self.connect():
            return False
        schema_rel = str(self.db_cfg.get("schema_file", "sql/01_schema.sql"))
        schema_path = Path(schema_rel)
        if not schema_path.is_absolute():
            schema_path = project_root() / schema_path
        if not schema_path.is_file():
            self.last_error = f"建表脚本不存在：{schema_path}"
            print(f"[db] 建表脚本不存在：{schema_path}")
            return False
        try:
            text = schema_path.read_text(encoding="utf-8")
            with self._conn.cursor() as cur:
                for stmt in _split_sql(text):
                    cur.execute(stmt)
                    try:
                        cur.fetchall()   # 吸掉最后一处自检 SELECT 的结果集
                    except Exception:
                        pass
            self._schema_ready = True
            self.sync_scenario_def()
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            print(f"[db] 建表失败：{exc}")
            return False

    def sync_scenario_def(self) -> None:
        """把运行期 `scenarios.SCENARIOS` 的真实参数同步进 `scenario_def`。

        这样"库里的参数"与"代码里的参数"永远一致，报告里引用数据库即引用代码。
        """
        try:
            from uav_sim.scenarios import list_scenarios

            sql = (
                "INSERT INTO scenario_def (code, label, duration_s, uav_count, high_ratio, low_ratio,"
                " ops_interval_min, ops_interval_max, survey_gap_k, high_size_min, high_size_max,"
                " low_size_min, low_size_max, churn_prob, weaknet_level, target_load_factor,"
                " health_mu, health_sigma, note)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " AS nv ON DUPLICATE KEY UPDATE label=nv.label, duration_s=nv.duration_s,"
                " uav_count=nv.uav_count, high_ratio=nv.high_ratio, low_ratio=nv.low_ratio,"
                " ops_interval_min=nv.ops_interval_min, ops_interval_max=nv.ops_interval_max,"
                " survey_gap_k=nv.survey_gap_k, high_size_min=nv.high_size_min,"
                " high_size_max=nv.high_size_max, low_size_min=nv.low_size_min,"
                " low_size_max=nv.low_size_max, churn_prob=nv.churn_prob,"
                " weaknet_level=nv.weaknet_level, target_load_factor=nv.target_load_factor,"
                " health_mu=nv.health_mu, health_sigma=nv.health_sigma, note=nv.note"
            )
            rows = []
            for sc in list_scenarios():
                rows.append((
                    sc.code, sc.label, sc.duration_s, sc.uav_count,
                    sc.high_ratio, sc.low_ratio,
                    sc.ops_interval_s[0], sc.ops_interval_s[1], sc.survey_gap_k,
                    sc.high_size_range_mb[0], sc.high_size_range_mb[1],
                    sc.low_size_range_mb[0], sc.low_size_range_mb[1],
                    sc.churn_prob, sc.weaknet_level, sc.target_load_factor,
                    sc.health_mu, sc.health_sigma,
                    f"勘查任务间隔 = 运维间隔×{sc.survey_gap_k}（容量标定反解）",
                ))
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
        except Exception as exc:  # noqa: BLE001 - 同步失败不影响主流程
            print(f"[db] 工况字典同步失败（不影响实验）：{exc}")

    # ------------------------------------------------------------------ 写入
    def save_run(self, result) -> bool:
        """单事务写入一次仿真的全部数据（契约 §4.9）。

        写入顺序：experiment -> exp_metric -> task_record -> queue_sample -> link_sample。
        任何异常都回滚并返回 False，不向上抛。
        """
        if not self.ensure_schema():
            return False
        metrics = dict(getattr(result, "metrics", {}) or {})
        try:
            with self._conn.cursor() as cur:
                cur.execute("START TRANSACTION")

                # ---- 1) experiment ----
                cur.execute(
                    "REPLACE INTO experiment (exp_id, scenario, scheduler, seed, duration_s,"
                    " uav_count, started_at, finished_at, status, error_text, config_json)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        str(result.exp_id)[:40],
                        str(result.scenario),
                        str(result.scheduler),
                        int(getattr(result, "seed", 0) or 0),
                        float(getattr(result, "duration_s", 0.0) or 0.0),
                        int(getattr(result, "uav_count", 0) or 0),
                        _to_mysql_dt(getattr(result, "started_at", None)),
                        _to_mysql_dt(getattr(result, "finished_at", None)),
                        "ERROR" if getattr(result, "error", None) else "OK",
                        getattr(result, "error", None),
                        json.dumps(self._cfg_snapshot(), ensure_ascii=False)[:60000],
                    ),
                )

                # ---- 2) exp_metric（长表） ----
                metric_rows = []
                for name, value in metrics.items():
                    spec = METRIC_SPEC.get(name)
                    unit = spec[1] if spec else ""
                    metric_rows.append((str(result.exp_id)[:40], name,
                                        float(value), unit, "ALL"))
                if metric_rows:
                    cur.executemany(
                        "INSERT INTO exp_metric (exp_id, metric_name, metric_value, unit, scope)"
                        " VALUES (%s,%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE metric_value=VALUES(metric_value),"
                        " unit=VALUES(unit), scope=VALUES(scope)",
                        metric_rows,
                    )

                # ---- 3) task_record（批量） ----
                task_rows = []
                for row in (getattr(result, "task_records", []) or []):
                    task_rows.append((
                        str(result.exp_id)[:40],
                        str(row.get("task_id", ""))[:64],
                        (str(row["parent_task_id"])[:64]
                         if row.get("parent_task_id") else None),
                        int(row.get("uav_id", 0) or 0),
                        str(row.get("priority", ""))[:8],
                        str(row.get("task_type", ""))[:24],
                        float(row.get("data_size_mb", 0.0) or 0.0),
                        int(row.get("segment_index", 0) or 0),
                        int(row.get("segment_total", 1) or 1),
                        float(row.get("service_s", 0.0) or 0.0),
                        float(row.get("submit_ts", 0.0) or 0.0),
                        _f(row.get("first_start_ts")),
                        _f(row.get("finish_ts")),
                        float(row.get("wait_ms", 0.0) or 0.0),
                        float(row.get("turnaround_ms", 0.0) or 0.0),
                        int(row.get("requeue_count", 0) or 0),
                        int(row.get("retry_count", 0) or 0),
                        int(row.get("preempt_count", 0) or 0),
                        float(row.get("link_health", 100.0) or 0.0),
                        int(row.get("cached", 0) or 0),
                        str(row.get("state", "NEW"))[:12],
                        (str(row["drop_reason"])[:64] if row.get("drop_reason") else None),
                    ))
                for start in range(0, len(task_rows), self.batch_size):
                    cur.executemany(
                        "INSERT INTO task_record (exp_id, task_id, parent_task_id, uav_id,"
                        " priority, task_type, data_size_mb, segment_index, segment_total,"
                        " service_s, submit_ts, first_start_ts, finish_ts, wait_ms,"
                        " turnaround_ms, requeue_count, retry_count, preempt_count,"
                        " link_health, cached, state, drop_reason)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        task_rows[start:start + self.batch_size],
                    )

                # ---- 4) queue_sample ----
                queue_rows = [(
                    str(result.exp_id)[:40], float(s.get("ts", 0.0)),
                    int(s.get("high_depth", 0)), int(s.get("low_depth", 0)),
                    int(s.get("running_count", 0)), int(s.get("blocked_count", 0)),
                    float(s.get("system_load", 0.0)),
                    float(s.get("high_time_slice_ms", 0.0)),
                    float(s.get("low_time_slice_ms", 0.0)),
                    float(s.get("high_weight", 0.0)), float(s.get("low_weight", 0.0)),
                ) for s in (getattr(result, "queue_samples", []) or [])]
                if queue_rows:
                    cur.executemany(
                        "INSERT INTO queue_sample (exp_id, ts, high_depth, low_depth,"
                        " running_count, blocked_count, system_load, high_time_slice_ms,"
                        " low_time_slice_ms, high_weight, low_weight)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        queue_rows,
                    )

                # ---- 5) link_sample ----
                link_rows = [(
                    str(result.exp_id)[:40], float(s.get("ts", 0.0)),
                    int(s.get("uav_id", 0)), float(s.get("delay_ms", 0.0)),
                    float(s.get("loss_rate", 0.0)), float(s.get("rssi_dbm", 0.0)),
                    float(s.get("health", 0.0)),
                ) for s in (getattr(result, "link_samples", []) or [])]
                if link_rows:
                    cur.executemany(
                        "INSERT INTO link_sample (exp_id, ts, uav_id, delay_ms, loss_rate,"
                        " rssi_dbm, health) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        link_rows,
                    )

                cur.execute("COMMIT")
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            try:
                with self._conn.cursor() as cur:
                    cur.execute("ROLLBACK")
            except Exception:
                pass
            print(f"[db] 写入 {getattr(result, 'exp_id', '?')} 失败（已回滚）：{exc}")
            return False

    def _cfg_snapshot(self) -> dict:
        """写进 experiment.config_json 的配置快照（去掉密码）。"""
        snap = json.loads(json.dumps(self.cfg, ensure_ascii=False, default=str))
        if isinstance(snap.get("database"), dict):
            snap["database"]["password"] = "***"
        return snap

    # ------------------------------------------------------------------ 查询
    def query(self, sql: str, args: tuple = ()) -> List[tuple]:
        """只读查询（DEBUG 用；失败返回空列表）。"""
        if not self.ensure_schema():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, args)
                return list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            print(f"[db] 查询失败：{exc}")
            return []

    def count(self, table: str) -> int:
        rows = self.query(f"SELECT COUNT(*) FROM {self.database}.{table}")
        return int(rows[0][0]) if rows else 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _f(value: Any) -> Optional[float]:
    """None -> None，其它转 float（MySQL 空值用 NULL）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def db_available(cfg: dict) -> bool:
    """探测数据库是否可用（`run.py check` 使用，不建表）。"""
    writer = DBWriter(cfg)
    ok = writer.connect()
    writer.close()
    return ok
