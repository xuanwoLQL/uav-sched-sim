"""
cross_node.py —— 跨节点负载均衡调度器（老师改造建议第五章 A3）
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 多地面站场景下，任务不能"就近全塞给某一个站"：需要把任务在中心云站与边缘站之间**动态均衡**，某站过载时把排队任务**迁移**到空闲站，边缘还能就近接住轻量运维任务 |
| OS 原理层   | **多处理器负载均衡（load balancing）+ 进程迁移（process migration）**：最少连接 ≈ 选最空闲的 CPU 队列；一致性哈希 ≈ 缓存/会话亲和性（避免抖动式迁移）；迁移代价 ≈ 迁移开销（在途时间 + 带宽损耗） |
| 算法设计层 | ① 选站打分 = `w_lc·最少连接分 + w_hash·一致性哈希分 + w_mec·MEC 亲和分`；② 水位触发迁移（源站负载 > 全局均值×ratio）；③ 迁移批量上限防震荡 |
| 代码实现层 | `CrossNodeScheduler.step() / _pick_station() / _rebalance()`；只依赖 `sim.stations` / `sim.queue` / `sim.limiter` 公共接口 |

--------------------------------------------------------------------------
优雅降级（**必须**，否则打坏既有 5 策略与 MockSim 测试）
--------------------------------------------------------------------------
调度器基类的通用测试用 ``MockSim``（没有 ``stations`` 属性）。因此本策略：

    * ``sim.stations is None``（单站 / MockSim）-> **完全退化为基线的单站行为**：
      走"高优先级优先 + 限流准入 + 错峰"这条最朴素、与其它策略同构的路径，
      不选站、不迁移。这样 test_schedulers.py 的通用循环（不丢任务、不超卖、
      step 不阻塞）对本策略同样成立。
    * 只有 ``sim.stations`` 存在且为多站时，才启用选站与迁移。

--------------------------------------------------------------------------
"迁移要有代价"在这里怎么落地
--------------------------------------------------------------------------
迁移本身由 ``StationCluster.migrate_task()`` 完成（含在途耗时 + 带宽损耗）。
本调度器只负责**决策**"哪些任务该迁、迁到哪"，然后调用它 —— 迁移不是瞬时的，
被迁任务会在 ``in_transit`` 里"在途"一段时间后才出现在目标站队列。
"""

from __future__ import annotations

import bisect
import hashlib
import sys
from pathlib import Path
from typing import Dict, Optional

_SRC_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from uav_sim.mec import desired_role_for  # noqa: E402
from uav_sim.models import SCHEDULER_LABELS, Task  # noqa: E402
from uav_sim.scheduler.dynamic import DynamicWeightScheduler  # noqa: E402

#: 一致性哈希虚拟节点数（纯函数默认值；实际环由 StationCluster 按 config 构建）
_DEFAULT_VNODES: int = 64


def _hash32(text: str) -> int:
    """稳定 32 位哈希（不用内置 hash()：它对 str 有进程级随机盐，不可复现）。"""
    return int.from_bytes(hashlib.md5(str(text).encode("utf-8")).digest()[:4], "big")


def least_connections(stats: Dict[int, dict]) -> int:
    """**最少连接选站**（纯函数，单独导出便于测试与报告引用）。

    ``stats`` 形如 ``{station_id: {"active": 在跑数, "queue_depth": 队列深度, "load": 负载}}``。
    打分顺序：① 归一化负载最低 → ② 在跑连接数最少 → ③ 队列最短 → ④ id 最小（稳定 tie-break）。

    为什么先看 load 再看 active：load 已经含"队列积压"的信息，是更全面的压力信号；
    active 只反映"正在跑"，会把"队列排了 200 个但一个都没跑"的站误判为空闲。
    需要排除某些站时，调用方先过滤 ``stats`` 再调用本函数（保持单一职责）。
    """
    pool = [int(sid) for sid in stats]
    if not pool:
        return 0

    def key(sid: int) -> tuple:
        s = stats.get(sid) or {}
        return (float(s.get("load", 0.0)), float(s.get("active", 0.0)),
                float(s.get("queue_depth", 0.0)), int(sid))

    return int(min(pool, key=key))


def consistent_hash(key, station_ids, cfg: Optional[dict] = None) -> int:
    """**一致性哈希选站**（纯函数）：同一 ``key`` 稳定映射到同一站。

    用于"会话/缓存亲和性"：同一架无人机的任务尽量落同一站，避免抖动式迁移。
    ``station_ids`` 为候选站 id 序列；``cfg`` 可选（读 ``stations.hash_virtual_nodes``）。
    """
    ids = sorted({int(x) for x in station_ids})
    if not ids:
        return 0
    vnodes = _DEFAULT_VNODES
    if isinstance(cfg, dict):
        try:
            vnodes = max(1, int((cfg.get("stations") or {}).get(
                "hash_virtual_nodes", _DEFAULT_VNODES)))
        except (TypeError, ValueError):
            vnodes = _DEFAULT_VNODES
    ring = []
    owner: Dict[int, int] = {}
    for sid in ids:
        for i in range(vnodes):
            k = _hash32(f"{sid}#{i}")
            while k in owner:
                k = (k + 1) % (1 << 32)
            ring.append(k)
            owner[k] = sid
    ring.sort()
    h = _hash32(str(key))
    idx = bisect.bisect_left(ring, h)
    if idx >= len(ring):
        idx = 0
    return int(owner[ring[idx]])


class CrossNodeScheduler(DynamicWeightScheduler):
    """跨节点负载均衡调度器 = **动态权重（节点内） + 跨节点选站/迁移（节点间）**。

    设计要点（为什么继承 ``DynamicWeightScheduler`` 而不是从基类重写）：

    * 企业需求是"**多基站**"，不是"换一套优先级规则"。节点**内部**该怎么调度
      （多级队列 + 动态权重 + 饥饿硬补偿 + 自适应时间片）已经有了经过 15 组
      实验验证的答案，跨节点改造只应在其上**叠加**"任务该放哪个站、什么时候
      搬走"这一层。否则"多站"与"调度策略"两个变量会纠缠在一起，实验无法归因。
    * 因此 **单站 / 无 stations 维度时，本策略逐位退化为动态权重策略** ——
      既有对照口径不被污染，通用测试（MockSim、不丢任务、不超卖、step 不阻塞）
      也自然成立。
    * ``link_aware=True``（继承）：弱网失败后走本地缓存而非原样重传。

    跨节点层做三件事：① 初始放置由 ``Simulator.make_task`` 结合 MEC 决定；
    ② 归属站宕机时**即时改派**（``_ensure_station``）；③ 站间水位失衡时
    **带代价迁移**（``_rebalance`` + ``StationCluster.migrate_task``）。
    """

    name: str = "cross_node"
    label: str = SCHEDULER_LABELS["cross_node"]
    link_aware: bool = True

    def __init__(self, sim, cfg: dict) -> None:
        super().__init__(sim, cfg)
        sc = (cfg or {}).get("stations", {}) or {}
        # ---- 选站打分权重（三路输入） ----
        self.w_least_conn: float = float(sc.get("w_least_conn", 0.55))
        self.w_hash: float = float(sc.get("w_hash", 0.25))
        self.w_mec: float = float(sc.get("w_mec", 0.20))
        # ---- 记忆化选站：记录每个任务上一次选中的站，减少抖动式改站 ----
        self._assigned: Dict[str, int] = {}
        # ---- 诊断量 ----
        self.migrations_decided: int = 0
        self.rebalance_rounds: int = 0
        self.last_pick: Dict[str, object] = {}

    # ==================================================================
    # 组件访问（全部按"可能不存在"处理）
    # ==================================================================
    @property
    def stations(self):
        return getattr(self.sim, "stations", None)

    def _mec(self):
        return getattr(self.sim, "mec", None)

    def _running_remaining(self) -> Dict[int, float]:
        """每站正在运行任务的剩余服务秒之和（供选站时估算真实负载）。"""
        out: Dict[int, float] = {}
        running = getattr(self.sim, "_running", None)
        try:
            values = list(running.values()) if running is not None else []
        except Exception:  # pragma: no cover
            values = []
        for item in values:
            try:
                task = item[0]
            except Exception:  # pragma: no cover
                continue
            sid = int(getattr(task, "station_id", 0))
            out[sid] = out.get(sid, 0.0) + float(getattr(task, "remaining_s", 0.0))
        return out

    # ==================================================================
    # 选站
    # ==================================================================
    def _pick_station(self, task: Task) -> Optional[int]:
        """为一条任务选站：最少连接 + 一致性哈希 + MEC 亲和，三路加权。

        * **最少连接**（动态）：占用率最低的站得分最高 —— 主均衡信号；
        * **一致性哈希**（静态亲和）：``uav:<id>`` 的环上归属站得分最高 ——
          避免"同一架机的任务每次都换站"造成的抖动与迁移风暴；
        * **MEC 亲和**：轻量运维任务偏向边缘站、重勘查任务偏向中心站。
        """
        st = self.stations
        if st is None or not st.is_multi:
            return None
        rr = self._running_remaining()
        alive = [n for n in st.nodes if n.online]
        if not alive:
            return None

        # ---- 两路参考落的点 ----
        lc_id = st.pick_least_connections(rr)
        hash_id = st.hash_owner(f"uav:{int(task.uav_id)}")
        # ---- MEC 期望角色对应的候选集合 ----
        mec = self._mec()
        prefer_role: Optional[str] = None
        if mec is not None and getattr(mec, "enabled", False):
            prefer_role = mec.desired_role(task)
        mec_candidates = {int(n.spec.id) for n in alive
                          if prefer_role is None or str(n.spec.role).upper() == prefer_role}
        if not mec_candidates:  # 没有对应角色的站 -> 不限
            mec_candidates = {int(n.spec.id) for n in alive}

        # ---- 打分 ----
        best_id: Optional[int] = None
        best_score = -1.0
        for node in alive:
            sid = int(node.spec.id)
            # 最少连接分：占用率越低越接近 1
            ratio = float(rr.get(sid, 0.0)) / max(1.0, float(node.spec.concurrency))
            # 也把队列深度算进"连接数"，否则队列很长但没在跑的站会被误判为空闲
            ratio += float(node.queue.total_depth) / max(1.0, float(node.spec.concurrency))
            lc_score = 1.0 / (1.0 + ratio)
            hash_score = 1.0 if sid == hash_id else 0.0
            mec_score = 1.0 if sid in mec_candidates else 0.0
            score = (self.w_least_conn * lc_score + self.w_hash * hash_score
                     + self.w_mec * mec_score)
            # 稳定 tie-break：id 小的优先，保证可复现
            if score > best_score + 1e-12 or (
                    abs(score - best_score) <= 1e-12 and (best_id is None or sid < best_id)):
                best_score, best_id = score, sid
        self.last_pick = {"task": task.task_id, "lc": lc_id, "hash": hash_id,
                          "role": prefer_role, "chosen": best_id, "score": round(best_score, 4)}
        return best_id

    def _ensure_station(self, task: Task) -> int:
        """确保任务有一个"合法且可用"的归属站；返回该站 id。

        单站 / MockSim 直接返回默认站（= 1）。多站时：若任务的原归属站已下线，
        立刻改派到存活站（这就是"节点宕机 -> 任务重调度"的即时兜底）。
        """
        st = self.stations
        if st is None:
            baseline = int(getattr(task, "station_id", 1) or 1)
            task.station_id = baseline if baseline in (1, getattr(st, "default_id", 1)) else 1
            if not hasattr(task, "home_station_id") or not task.home_station_id:
                task.home_station_id = task.station_id
            return task.station_id
        sid = int(getattr(task, "station_id", st.default_id) or st.default_id)
        node = st.node(sid)
        if node is not None and node.online:
            return sid
        # 归属站不可用 -> 选一个存活站
        pick = self._pick_station(task)
        if pick is None:
            pick = st.default_id
        task.station_id = int(pick)
        return task.station_id

    # ==================================================================
    # 任务迁移重平衡
    # ==================================================================
    def _rebalance(self, now: float) -> int:
        """水位触发的任务迁移：把过载站队列里的任务搬到空闲站。

        触发条件：某站负载 > 全局平均负载 × ``migration_load_ratio``。
        限制：单轮每站最多迁 ``migration_batch`` 个，防止一次搬空造成震荡。
        返回本轮成功发起的迁移数（真正落地由 ``flush_migrations`` 完成）。
        """
        st = self.stations
        if st is None or not st.is_multi:
            return 0
        rr = self._running_remaining()
        loads = {int(n.spec.id): n.load(float(rr.get(int(n.spec.id), 0.0)))
                 for n in st.nodes if n.online}
        if len(loads) < 2:
            return 0
        mean = sum(loads.values()) / len(loads)
        if mean <= 1e-9:
            return 0
        threshold = mean * float(getattr(st, "migration_load_ratio", 1.25))
        mec = self._mec()
        mec_enabled = bool(mec is not None and getattr(mec, "enabled", False))
        migrated = 0
        self.rebalance_rounds += 1
        for node in st.nodes:
            if not node.online:
                continue
            if loads[int(node.spec.id)] <= threshold:
                continue
            # 从该站队列里挑最靠后的（最不急的）任务迁出，避免动队头
            pending = node.queue.tasks()
            if not pending:
                continue
            batch = max(1, int(getattr(st, "migration_batch", 8)))
            moved_here = 0
            for task in reversed(pending):
                if moved_here >= batch:
                    break
                if int(getattr(task, "migrate_count", 0)) >= 2:
                    continue  # 迁移次数过多就不再折腾（防乒乓）
                targets = [int(n.spec.id) for n in st.nodes
                           if int(n.spec.id) != int(node.spec.id) and n.online
                           and loads.get(int(n.spec.id), 0.0) < threshold]
                # ---- 禁止"迁回老家"：迁移只做 home -> 其它站 的单向分流 ----
                # 把任务搬回它的 home 站不产生任何均衡收益（该站的负载正是它原本
                # 要分担的对象），却要白付一次迁移代价，还会造成来回乒乓。
                targets = [sid for sid in targets
                           if sid != int(getattr(task, "home_station_id", -1))]
                # ---- MEC 硬约束：迁移不得破坏"重勘查回中心 / 轻量下沉边缘" ----
                # 边缘算力不足以承载重勘查任务，因此在 MEC 开启时把迁移目标
                # 限制在**角色匹配**的站上；没有匹配目标就宁可不迁（任务留在原站）。
                if mec_enabled:
                    want = desired_role_for(task.task_type, self.cfg)
                    targets = [sid for sid in targets
                               if str(st.node(sid).spec.role).upper() == want]
                if not targets:
                    continue
                dst = st.pick_least_connections(rr, candidates=targets)
                if st.migrate_task(task, dst, now):
                    self._assigned[task.task_id] = int(dst)
                    migrated += 1
                    moved_here += 1
            if moved_here:
                self.migrations_decided += moved_here
        return migrated

    # ==================================================================
    # 调度主循环
    # ==================================================================
    def _admit(self, task: Task) -> str:
        """准入前先保证归属站可用 —— 站宕机时把任务**即时改派**到存活站。

        为什么要挂在 ``_admit`` 上：父类（动态权重）的 ``step()`` 在每次
        真正申请许可前都会调用 ``_admit()``，因此这是"任务出队后、占槽位前"
        唯一且必然经过的钩子。归属站不可用时，若不在这里改派，
        限流器会按旧 station_id 去问一个已经宕机的站的许可，任务将永久拿不到许可。
        """
        self._ensure_station(task)
        return super()._admit(task)

    def step(self) -> int:
        """先做跨节点重平衡（迁移），再交给父类的节点内动态权重调度。"""
        try:
            self._rebalance(self.now())
        except Exception:  # pragma: no cover - 迁移失败不得打断调度
            pass
        return int(super().step())

    # ==================================================================
    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["migrations_decided"] = int(self.migrations_decided)
        stats["rebalance_rounds"] = int(self.rebalance_rounds)
        stats["last_pick"] = dict(self.last_pick)
        return stats
