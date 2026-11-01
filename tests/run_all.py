"""
tests/run_all.py —— 一键运行全部测试（6 个独立测试脚本）

为什么不用 `unittest discover`：本项目测试写成"可直接运行的脚本"而不是
`unittest.TestCase` 子类，因为 ——
  * 教学场景下学生更习惯 `python tests/test_queue.py` 直接看输出；
  * 单元测试里大量使用 `assert` + 中文 [OK] 打印，比 pytest 的断言报告更直观。

用法：
    .venv\\Scripts\\python.exe tests\\run_all.py
    （或逐个运行：.venv\\Scripts\\python.exe tests\\test_queue.py ...）

退出码：0 = 全部通过；1 = 有失败。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

TESTS = [
    ("test_queue.py", "两级就绪队列 / 等待记账 / 饥饿补偿通道"),
    ("test_link.py", "链路健康度 / 确定性轨迹"),
    ("test_weather.py", "天气→弱网联动 / clear 零衰减 / 向后兼容回归"),
    ("test_limiter.py", "并发限流 / 大任务拒绝与分片 / 单机配额"),
    ("test_schedulers.py", "五种调度策略 / 时间片 / 教学结论断言"),
    ("test_statistics.py", "重复实验统计 / t 置信区间 / 配对显著性 / CRN 种子"),
    ("test_des_model.py", "GPU 分析模型（纯 Python 回退路径）确定性 / 区间 / 教学结论"),
    ("test_end_to_end.py", "轨迹可复现 / 端到端 / 指标口径 / MySQL 往返"),
]


def main() -> int:
    print("=" * 74)
    print("  UavSchedulerSim 测试套件")
    print(f"  解释器：{PY}")
    print("=" * 74)

    failed: list[str] = []
    total_start = time.perf_counter()

    for idx, (name, desc) in enumerate(TESTS, 1):
        path = ROOT / "tests" / name
        print(f"\n[{idx}/{len(TESTS)}] {name}  —— {desc}")
        print("-" * 74)
        t0 = time.perf_counter()
        proc = subprocess.run([PY, str(path)], cwd=str(ROOT))
        cost = time.perf_counter() - t0
        if proc.returncode == 0:
            print(f"  [PASS] {name}  ({cost:.1f}s)")
        else:
            print(f"  [FAIL] {name}  ({cost:.1f}s，退出码 {proc.returncode})")
            failed.append(name)

    total = time.perf_counter() - total_start
    print("\n" + "=" * 74)
    print(f"  合计 {len(TESTS)} 个文件，通过 {len(TESTS) - len(failed)}，"
          f"失败 {len(failed)}，总耗时 {total:.1f}s")
    if failed:
        print("  失败：" + ", ".join(failed))
        print("=" * 74)
        return 1
    print("  全部通过 [PASS]")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
