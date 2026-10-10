"""Shim to allow `python3 -m pytest tests -q` without external pytest install.

仓库根目录位于 `sys.path[0]`，`import pytest` 会先命中本文件，因此本文件承担
两个职责：

1. 作为 ``pytest`` 模块名透出最小 API 桩（``from tests.pytest import *``），
   供「无真实 pytest + unittest discover」路径使用（tests/README.md）；
2. 以 ``__main__`` 运行时**真正执行测试**：环境装有真实 pytest 时透传给它
   （先把自己从 sys.path / sys.modules 里摘掉，否则永远命中本桩、命令空转）；
   没有真实 pytest 时回落 ``unittest discover``（dotted 模块名，避免
   ``tests/`` 与 ``core/tests/`` 同名模块碰撞）。
"""
from tests.pytest import *  # noqa: F401,F403

if __name__ == "__main__":
    import os
    import sys

    _ROOT = os.path.dirname(os.path.abspath(__file__))

    def _load_real_pytest():
        """摘掉本桩的路径遮蔽后导入真实 pytest；未安装返回 None。"""
        saved_path = sys.path[:]
        try:
            sys.path = [
                p for p in sys.path
                if p not in ("", ".")
                and os.path.abspath(p or ".") != _ROOT
            ]
            sys.modules.pop("pytest", None)
            import pytest as real  # noqa: PLC0415
            return real
        except ImportError:
            return None
        finally:
            sys.path = saved_path

    _real = _load_real_pytest()
    if _real is not None:
        raise SystemExit(_real.main(sys.argv[1:]))

    # —— 无真实 pytest：unittest discover 兜底（tests/pytest.py 桩 +
    # tests/unittest_compat.py 收集引擎；语义子集见 tests/README.md）——
    import unittest

    _args = sys.argv[1:]
    _paths = [a for a in _args if not a.startswith("-")]
    _start = _paths[0] if _paths else "tests"
    try:
        _suite = unittest.TestLoader().discover(_start, top_level_dir=".")
    except ImportError as _exc:
        sys.stderr.write(f"pytest-shim: discovery failed for {_start}: {_exc}\n")
        raise SystemExit(2)
    _result = unittest.TextTestRunner(
        verbosity=2 if "-v" in _args else 1).run(_suite)
    raise SystemExit(0 if _result.wasSuccessful() else 1)
