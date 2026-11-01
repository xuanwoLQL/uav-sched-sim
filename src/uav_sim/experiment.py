"""
experiment.py —— 对照实验框架（3 工况 × 5 策略 = 15 组）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 企业要的不是"跑一次看看"，而是**可复现的对照实验**：同样 30 台无人机、同样弱网强度、同样任务构成，只换调度策略，看指标怎么变 |
| OS 原理层   | 严格的**控制变量法**：输入轨迹（`WorkloadTrace`）固定，只变调度算法 -> 指标差异可归因于算法 |
| 算法设计层 | 一次实验 = generate_trace → Simulator → Scheduler → run → metrics → 落库/出图 |
| 代码实现层 | `ExperimentRunner.run_one()` / `run_matrix()` / `export_csv()` |

--------------------------------------------------------------------------
控制变量法的三个保证（报告"实验设计"章节必写）
--------------------------------------------------------------------------
1. **输入相同**：`generate_trace(scenario, seed)` 与策略无关 —— 5 个策略回放**同一条**
   任务到达轨迹与**同一条**链路质量轨迹（`LinkMonitor` 也按 seed 确定性预生成）。
2. **系统容量相同**：限流器（并发上限、单任务上限、单机配额）、工作线程数、
   带宽全部由 config 决定，5 个策略共用同一份系统约束。
3. **度量相同**：所有策略走同一个 `compute_metrics()`，指标口径完全一致。

--------------------------------------------------------------------------
异常收敛
--------------------------------------------------------------------------
任何一组实验抛异常都被收敛为 `RunResult.error`，`run_matrix()` 打印后继续下一组，
**不会因为一组失败导致 15 组全废**（教学场景下这非常重要：学生机器环境千差万别）。
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.config import cfg_get_float, cfg_get_int, load_config, resolve_path
from uav_sim.db import DBWriter
from uav_sim.metrics import compute_metrics, extra_stats, final_turnaround_ms, final_wait_ms
from uav_sim.models import (
    METRIC_SPEC,
    SCENARIO_CODES,
    SCENARIO_LABELS,
    SCHEDULER_LABELS,
    SCHEDULER_NAMES,
    RunResult,
)
from uav_sim.scenarios import (
    Scenario,
    calibrate,
    generate_trace,
    get_scenario,
    system_capacity_mbps,
)
from uav_sim.scheduler import create_scheduler
from uav_sim.simulator import Simulator


class ExperimentRunner:
    """对照实验执行器（契约 §4.10）。"""

    def __init__(self, cfg: Optional[dict] = None,
                 results_dir: Optional[str | Path] = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self.results_dir = Path(results_dir) if results_dir is not None \
            else resolve_path(self.cfg, "output.results_dir")
        self.raw_dir = self.results_dir / "raw"
        self.task_dir = self.results_dir / "task_records"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        if bool(cfg_get_float(self.cfg, "output.export_task_csv", 1)):
            self.task_dir.mkdir(parents=True, exist_ok=True)

        self.results: List[RunResult] = []
        self._db: Optional[DBWriter] = None
        self._db_ready: Optional[bool] = None

    # ------------------------------------------------------------ 数据库
    def _get_db(self) -> Optional[DBWriter]:
        """惰性创建 DBWriter；数据库不可用返回 None（一次失败后不再重试）。"""
        if not self.cfg.get("database", {}).get("enable", True):
            return None
        if self._db_ready is False:
            return None
        if self._db is None:
            self._db = DBWriter(self.cfg)
            self._db_ready = self._db.ensure_schema()
            if not self._db_ready:
                print("[experiment] 数据库不可用，后续结果只落本地 CSV/JSON（优雅降级）")
        return self._db if self._db_ready else None

    # ------------------------------------------------------------ 单组实验
    def run_one(self, scenario_code: str, scheduler_name: str,
                seed: Optional[int] = None, persist: bool = True,
                weather_preset: Optional[str] = None,
                save_detail: bool = True) -> RunResult:
        """跑一组「工况 × 策略」实验（契约 §4.10）。

        ``weather_preset``：天气预设（clear/rain/snow/dust），None = 取 config
        ``weather.default_preset``（默认 clear）。同一组实验天气曲线由 (seed, preset)
        确定性生成，5 个策略回放同一条曲线（控制变量，契约 §4.6.1）。

        ``save_detail``：是否把这次运行的完整明细（raw JSON + 任务级 CSV）落盘。
        重复实验（``run.py replicate``）每个单元要跑几十次，只保留聚合结果，
        因此传 False —— 否则 15 组 × 30 次会写出 450 个 JSON（数百 MB）。
        """
        code = str(scenario_code).strip().upper()
        sched = str(scheduler_name).strip().lower()
        if seed is None:
            seed = cfg_get_int(self.cfg, "simulation.random_seed", 20261031)
        seed = int(seed)

        started_wall = datetime.now()
        t0 = time.perf_counter()
        exp_id = f"{code}-{sched}-{seed}-{started_wall.strftime('%Y%m%d%H%M%S')}"

        scenario: Scenario = get_scenario(code)
        result = RunResult(
            exp_id=exp_id[:40],
            scenario=code,
            scheduler=sched,
            seed=seed,
            duration_s=float(scenario.duration_s),
            uav_count=int(scenario.uav_count),
            started_at=started_wall.isoformat(timespec="seconds"),
            finished_at="",
        )

        try:
            # ---- 1) 预生成确定性输入轨迹（控制变量） ----
            trace = generate_trace(scenario, seed, self.cfg)

            # ---- 2) 装配仿真器与调度策略 ----
            sim = Simulator(scenario, trace, self.cfg, scheduler_name=sched, seed=seed,
                            weather_preset=weather_preset)
            scheduler = create_scheduler(sched, sim, self.cfg)

            # ---- 3) 跑仿真 ----
            raw = sim.run(scheduler, duration_s=scenario.duration_s)
            wall = time.perf_counter() - t0

            # ---- 4) 采集指标 ----
            end_ts = float(raw.get("end_ts", scenario.duration_s))
            capacity = system_capacity_mbps(self.cfg, scenario.health_mu)
            metrics = compute_metrics(
                tasks=raw["tasks"],
                duration_s=raw["duration_s"],
                uav_count=scenario.uav_count,
                capacity_mbps=capacity,
                queue_samples=raw["queue_samples"],
                sched_stats=raw.get("scheduler_stats", {}),
                limiter_status=raw.get("limiter_status", {}),
                overload_seconds=raw.get("overload_seconds", 0.0),
                # 注意：这里传的是**评价阈值**（业务判定"已失联"），
                # 不是调度器的 aging 触发阈值（server.starvation_threshold_ms）。
                # 两者解耦的原因见 config.yaml 的注释与 metrics.py 的模块说明。
                starvation_threshold_ms=cfg_get_float(
                    self.cfg, "server.starvation_metric_threshold_ms", 15000.0),
                end_ts=end_ts,
                runtime_wall_s=wall,
                preempt_count=int(raw.get("preempt_count", 0)),
            )

            # ---- 5) 明细落盘（等待时延按仿真结束时刻结算，与指标口径一致） ----
            task_rows = []
            for task in raw["tasks"]:
                row = task.to_row()
                row["wait_ms"] = round(final_wait_ms(task, end_ts), 3)
                row["turnaround_ms"] = round(final_turnaround_ms(task, end_ts), 3)
                task_rows.append(row)

            result.metrics = metrics
            result.task_records = task_rows
            result.queue_samples = [s.to_row() for s in raw["queue_samples"]]
            result.link_samples = [s.to_row() for s in raw["link_samples"]]
            # 天气曲线快照（契约 §4.6.3 / §4.6.5：run.py weather 用它算链路扣分统计）
            result.weather_samples = list(raw.get("weather_samples") or [])
            result.finished_at = datetime.now().isoformat(timespec="seconds")
            result.logs = [
                f"scenario={code} scheduler={sched} seed={seed}",
                f"trace_events={len(trace.events)} churn={len(trace.churn)}",
                f"submitted={raw['submitted']} done={raw['done']} "
                f"still_in_queue={raw['still_in_queue']}",
                f"calibrated_load_factor={calibrate(scenario, self.cfg, seed)['load_factor']:.3f}",
                f"weather={sim.weather.preset} label={sim.weather.label} "
                f"mean_penalty={sim.weather.mean_penalty():.3f}",
                f"extra={json.dumps(extra_stats(raw['tasks']), ensure_ascii=False)}",
            ]

            # ---- 6) 持久化（JSON + 任务 CSV + MySQL） ----
            if save_detail:
                self.save_json(result)
                if bool(cfg_get_float(self.cfg, "output.export_task_csv", 1)):
                    self._save_task_csv(result)
            if persist:
                db = self._get_db()
                if db is not None:
                    db.save_run(result)

        except Exception as exc:  # noqa: BLE001 - 异常收敛
            result.error = f"{type(exc).__name__}: {exc}"
            result.finished_at = datetime.now().isoformat(timespec="seconds")
            result.metrics = {k: 0.0 for k in METRIC_SPEC}
            traceback.print_exc()

        self.results.append(result)
        return result

    # ------------------------------------------------------------ 批量实验
    def run_matrix(self, scenario_codes: Optional[Iterable[str]] = None,
                   scheduler_names: Optional[Iterable[str]] = None,
                   persist: bool = True,
                   progress: bool = True,
                   weather_preset: Optional[str] = None) -> List[RunResult]:
        """全量对照实验（默认 3 工况 × 5 策略 = 15 组，契约 §4.10）。

        ``weather_preset`` 透传给每一组（``run.py weather`` 用它跑 4 天气 × 5 策略
        的对照；``run.py all`` 不传 = 默认 clear，对既有 15 组结果零影响）。
        """
        codes = [str(c).upper() for c in (scenario_codes or SCENARIO_CODES)]
        scheds = [str(s).lower() for s in (scheduler_names or SCHEDULER_NAMES)]
        total = len(codes) * len(scheds)

        if progress:
            print(f"\n开始对照实验：{len(codes)} 工况 × {len(scheds)} 策略 = {total} 组")
            print(f"随机种子 = {cfg_get_int(self.cfg, 'simulation.random_seed', 20261031)}"
                  f"（固定种子 = 控制变量）\n")

        done_results: List[RunResult] = []
        k = 0
        for code in codes:
            for sched in scheds:
                k += 1
                if progress:
                    print(f"[{k:>2}/{total}] {code:<7} × {sched:<9} ... ", end="", flush=True)
                res = self.run_one(code, sched, persist=persist,
                                   weather_preset=weather_preset)
                done_results.append(res)
                if progress:
                    if res.error:
                        print(f"✗ {res.error}")
                    else:
                        m = res.metrics
                        print(f"吞吐={m['throughput']:.2f}/s  "
                              f"高优时延={m['avg_wait_high_ms']:.0f}ms  "
                              f"低优饥饿率={m['starvation_rate_low']:.1f}%  "
                              f"完整率={m['completion_rate']:.1f}%  "
                              f"(实耗 {m['runtime_wall_s']:.1f}s)")

        # ---- 汇总导出 ----
        self.export_csv(done_results)
        if progress:
            ok = sum(1 for r in done_results if not r.error)
            print(f"\n完成 {ok}/{total} 组；汇总表已写入 {self.summary_path()}")
        return done_results

    # ------------------------------------------------------------ 导出
    def summary_path(self) -> Path:
        return self.results_dir / "summary.csv"

    @staticmethod
    def _metric_columns() -> List[str]:
        return list(METRIC_SPEC.keys())

    def export_csv(self, results: Optional[Iterable[RunResult]] = None,
                   path: Optional[str | Path] = None) -> Path:
        """导出汇总表（一行一个「工况 × 策略」，列 = 全部指标）。

        用 ``utf-8-sig`` 编码：Excel 双击打开中文表头不会乱码（教学实务细节）。
        """
        results = list(results if results is not None else self.results)
        out = Path(path) if path is not None else self.summary_path()
        out.parent.mkdir(parents=True, exist_ok=True)

        head = ["exp_id", "scenario", "scenario_label", "scheduler", "scheduler_label",
                "seed", "duration_s", "uav_count"] + self._metric_columns()
        with out.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(head)
            for r in results:
                row = [
                    r.exp_id, r.scenario, SCENARIO_LABELS.get(r.scenario, r.scenario),
                    r.scheduler, SCHEDULER_LABELS.get(r.scheduler, r.scheduler),
                    r.seed, r.duration_s, r.uav_count,
                ]
                row += [f"{float((r.metrics or {}).get(k, 0.0)):.6f}" for k in self._metric_columns()]
                writer.writerow(row)

        # 另存一份「指标中文名对照」便于报告附录引用
        map_path = self.results_dir / "metric_dictionary.csv"
        with map_path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(["metric_name", "中文名", "单位", "越大越好"])
            for name, (cn, unit, higher_better) in METRIC_SPEC.items():
                writer.writerow([name, cn, unit, "是" if higher_better else "否"])
        return out

    def save_json(self, result: RunResult) -> Path:
        """把一次运行的完整结果写成 JSON（供 `run.py charts` 重新出图）。"""
        path = self.raw_dir / f"{result.exp_id}.json"
        data = {
            "exp_id": result.exp_id,
            "scenario": result.scenario,
            "scheduler": result.scheduler,
            "seed": result.seed,
            "duration_s": result.duration_s,
            "uav_count": result.uav_count,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "metrics": result.metrics,
            "task_records": result.task_records,
            "queue_samples": result.queue_samples,
            "link_samples": result.link_samples,
            "weather_samples": result.weather_samples,
            "logs": result.logs,
            "error": result.error,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def _save_task_csv(self, result: RunResult) -> Path:
        """任务级明细 CSV（报告附录 / 导师抽查用）。"""
        path = self.task_dir / f"{result.exp_id}.csv"
        rows = result.task_records
        if not rows:
            path.write_text("", encoding="utf-8-sig")
            return path
        cols = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(fp, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
        return path

    # ------------------------------------------------------------ 便捷入口
    def print_matrix(self, results: Optional[Iterable[RunResult]] = None) -> None:
        """控制台打印「策略 × 关键指标」对照矩阵（按工况分组）。"""
        results = list(results if results is not None else self.results)
        groups: Dict[str, List[RunResult]] = {}
        for r in results:
            groups.setdefault(r.scenario, []).append(r)

        cols = [
            ("throughput", "吞吐量", 2),
            ("avg_wait_high_ms", "高优时延ms", 0),
            ("avg_wait_low_ms", "低优时延ms", 0),
            ("starvation_rate_low", "低优饥饿%", 1),
            ("resource_utilization", "利用率%", 1),
            ("completion_rate", "完整率%", 1),
        ]
        for code in SCENARIO_CODES:
            rows = groups.get(code)
            if not rows:
                continue
            print(f"\n【{code} · {SCENARIO_LABELS.get(code, code)}】")
            print(f"{'策略':<26}" + "".join(f"{c[1]:>12}" for c in cols))
            print("-" * (26 + 12 * len(cols)))
            best = {}
            for key, _, _ in cols:
                vals = [r.metrics.get(key, 0.0) for r in rows]
                higher_better = key in ("throughput", "resource_utilization", "completion_rate")
                best[key] = max(vals) if higher_better else min(vals)
            for r in rows:
                line = f"{SCHEDULER_LABELS.get(r.scheduler, r.scheduler):<26}"
                for key, _, nd in cols:
                    v = r.metrics.get(key, 0.0)
                    mark = "*" if abs(v - best[key]) < 1e-9 else " "
                    line += f"{v:>11.{nd}f}{mark}"
                print(line)
        print("\n（* = 该指标本工况最优）")
