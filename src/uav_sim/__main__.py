"""``python -m uav_sim`` / ``uavsim`` 控制台入口。

CLI 实现仍是项目根目录的 ``run.py``（保留 ``python run.py ...`` 这一教学用法，
避免把 1000+ 行入口脚本搬进包里导致文档与命令全部失效）。本模块只做两件事：

1. 用 ``config.project_root()`` 定位工程根目录（源码/editable/exe 三种形态通用）；
2. 把它挂到 ``sys.path`` 后转发给 ``run.main()``。

这样 ``uavsim check`` 与 ``python run.py check`` 完全等价。
"""

from __future__ import annotations

import sys
from typing import List, Optional

from uav_sim.config import project_root


def main(argv: Optional[List[str]] = None) -> int:
    """转发到根目录 ``run.py`` 的 ``main()``。"""
    root = project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import run  # noqa: PLC0415 —— 必须在 sys.path 就绪后导入
    except ImportError:  # pragma: no cover - 仅在包被安装到 site-packages 时触发
        sys.stderr.write(
            "无法定位 run.py。请改用项目根目录下的入口：\n"
            '    python run.py <check|calibrate|all|ui|...>\n'
        )
        return 2
    return int(run.main(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
