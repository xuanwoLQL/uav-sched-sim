"""
config.py —— 全局配置加载、嵌套取值与路径解析
=============================================================================

四层映射（企业业务层 ↔ OS 原理层）：

| 层 | 落点 |
|---|---|
| 企业业务层 | 野外勘查作业参数（机群规模、任务数据量、弱网强度、数据库连接）全部外置为配置项，改工况不改代码 |
| OS 原理层   | 对应内核启动参数 / `/proc/sys` 可调参数：系统容量（worker_threads）、时间片、限流阈值、采样周期 |
| 算法设计层 | 动态权重系数（α/β/γ/δ）、饥饿补偿倍率、链路感知阈值全部从配置读取，便于实验对照 |
| 代码实现层 | `load_config()` / `get()` / `project_root()` / `resolve_path()` |

约定（对应契约 §4.1，签名逐字冻结）：
    * 项目根 = 本文件上溯两级：``src/uav_sim/config.py`` -> ``D:\\study\\uav_sched_sim``
    * 环境变量 ``UAV_DB_PASSWORD`` 优先于 ``config.yaml`` 里的 ``database.password``
      （生产环境只设环境变量，避免密码进入版本库）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: 本文件所在包目录 ``src/uav_sim`` 的上二级 = 源码根 ``src``
_SRC_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    # 保证 ``import uav_sim.*`` 在任意工作目录下都可用（测试直接跑脚本时兜底）
    sys.path.insert(0, str(_SRC_ROOT))

#: 覆盖数据库密码的环境变量名（对应契约 §4.1）
DB_PASSWORD_ENV: str = "UAV_DB_PASSWORD"

#: 默认配置文件名
CONFIG_FILENAME: str = "config.yaml"


def project_root() -> Path:
    """返回项目根目录。

    两种情况：
      * **源码运行**：``config.py`` 在 ``<root>/src/uav_sim/config.py``，上溯两级即根目录；
      * **PyInstaller 打包后**（``sys.frozen``）：优先用 **exe 所在目录**
        （这样用户可以在 exe 旁边改 config.yaml、产出 results/charts），
        找不到再退回打包内部临时目录 ``sys._MEIPASS``。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / CONFIG_FILENAME).is_file():
            return exe_dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return exe_dir
    return Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict:
    """读取 ``config.yaml``，返回嵌套 ``dict``。

    参数：
        path: 配置文件路径；``None`` 时取 ``<项目根>/config.yaml``。

    环境变量覆盖：
        若存在 ``UAV_DB_PASSWORD``，覆盖 ``cfg["database"]["password"]``
        （其它来源一律不覆盖，保证实验可复现）。

    异常：
        FileNotFoundError —— 配置文件不存在（附带中文定位提示）；
        RuntimeError      —— 缺少 PyYAML 依赖（提示安装命令）。
    """
    try:
        import yaml  # 延迟导入：让缺依赖时给出可读的中文报错
    except ImportError as exc:  # pragma: no cover - 环境已装 PyYAML
        raise RuntimeError(
            "缺少 PyYAML 依赖，无法解析 config.yaml。请执行："
            '& ".venv\\Scripts\\python.exe" -m pip install -r requirements.txt'
        ) from exc

    cfg_path = Path(path) if path is not None else project_root() / CONFIG_FILENAME
    if not cfg_path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    if not isinstance(cfg, dict):  # 防御：YAML 顶层不是映射
        raise ValueError(f"配置文件顶层必须是映射（dict）：{cfg_path}")

    # ---- 环境变量覆盖数据库密码（生产实践：密码只进环境变量） ----
    env_password = os.environ.get(DB_PASSWORD_ENV)
    if env_password:
        cfg.setdefault("database", {})
        if isinstance(cfg["database"], dict):
            cfg["database"]["password"] = env_password
    return cfg


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """按 ``'runtime.worker_threads'`` 形式取嵌套配置；缺失返回 ``default``。

    任何一层不是映射都视为缺失，不抛异常（配置容错，保证仿真不因缺项中断）。
    """
    cur: Any = cfg
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_path(cfg: dict, dotted_key: str) -> Path:
    """把 ``output.results_dir`` 之类相对路径解析成绝对 ``Path``，并创建目录。

    相对路径一律相对**项目根**解析（与 ``config.yaml`` 同处一级），
    因此无论从哪个工作目录启动 ``run.py``，产物都落在项目内。
    """
    raw = get(cfg, dotted_key)
    if raw is None:
        raise KeyError(f"配置项不存在：{dotted_key}")
    path = Path(str(raw))
    if not path.is_absolute():
        path = project_root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def cfg_get_float(cfg: dict, dotted: str, default: float) -> float:
    """取浮点配置的便捷封装（类型不合法时回退默认值）。"""
    value = get(cfg, dotted, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def cfg_get_int(cfg: dict, dotted: str, default: int) -> int:
    """取整型配置的便捷封装（类型不合法时回退默认值）。"""
    value = get(cfg, dotted, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


if __name__ == "__main__":  # pragma: no cover - 手工自检入口
    _cfg = load_config()
    print(f"项目根目录 : {project_root()}")
    print(f"worker_threads  : {get(_cfg, 'runtime.worker_threads')}")
    print(f"time_scale      : {get(_cfg, 'runtime.time_scale')}")
    print(f"数据库密码来源  : {'环境变量 ' + DB_PASSWORD_ENV if os.environ.get(DB_PASSWORD_ENV) else 'config.yaml'}")
    print(f"结果目录        : {resolve_path(_cfg, 'output.results_dir')}")
