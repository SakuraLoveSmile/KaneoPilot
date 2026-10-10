"""scenarios.md 冻结场景清单的可执行门控（场景 1–20 + 清单核对）。

引擎门控（scenarios.md 硬约束）：涉及 ``engine run``/``engine status``/
``attention-list``/``session-view`` 等 §5.27 命令的场景，在核心尚未实现这些
命令时，用例以明确的「引擎未实现」失败收场——**不得**出现导入错误或收集
错误。``require_engine`` 只经 ``cli`` 调用命令面并断言未知命令错误信封；
引擎落地后这些用例继续以「主体未实现」失败，直到场景主体被补齐。

场景 21–25（doctor / git 探针 / 夹具新鲜度）已在
``test_doctor_live.py``、``test_git_probe.py``、``test_real_shape_fixtures.py``
实现；本文件末尾的清单核对测试保证 25 个场景名与 ``test_scenario_<name>``
一一对应。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

try:
    from tests.rehearsal import conftest as _rehearsal_conftest  # noqa: F401,E402
    from tests.rehearsal.conftest import FAULTS  # noqa: E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    import conftest as _rehearsal_conftest  # noqa: F401,E402
    from conftest import FAULTS  # noqa: E402

try:
    from conftest import CLI  # noqa: E402
except ImportError:  # dotted 收集（tests.rehearsal.test_rehearsal_scenarios）
    from tests.conftest import CLI  # noqa: E402

SCENARIOS_MD = Path(__file__).resolve().parent / "scenarios.md"

# 场景名 → 引擎门控探针（CLI 参数组；命令缺失时以"引擎未实现"失败）。
# 只读探测：``engine status`` / ``attention-list``（绝不触达 ``engine run``）。
ENGINE_GATED = {
}

TEST_MODULES = (
    "test_doctor_live",
    "test_git_probe",
    "test_real_shape_fixtures",
    "test_real_shape_recordings",
    "test_rehearsal_golden",
    "test_rehearsal_plus",
    "test_rehearsal_engine",
    "test_rehearsal_delivery",
    "test_rehearsal_scenarios",
)


def require_engine(cli, *probes):
    """引擎门：命令未实现 → 明确失败（引擎未实现），而非跳过或导入错误。

    ``probes`` 为只读 CLI 参数组（如 ``("engine", "status")``）；命令缺失时
    核心返回 ``unknown command`` 错误信封（exit 1）。
    """
    missing = []
    for probe in probes:
        proc = cli.raw([*probe, "--json", "{}"])
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            data = {}
        error = str(data.get("error") or "")
        if proc.returncode != 0 and "unknown command" in error:
            missing.append(" ".join(probe))
    assert not missing, (
        f"引擎未实现：命令 {missing} 尚未落地（Phase 3 engine_builder 范围，"
        f"protocol §5.27）；本场景在引擎落地前保持红色。")


def _engine_gated(name):
    def test(cli):
        require_engine(cli, *ENGINE_GATED[name])
        pytest.fail(
            f"场景 {name} 主体未实现：引擎命令已就绪，请按 scenarios.md 的"
            f"预期行为补齐（夹具基座已在 tests/rehearsal/ 就绪）。")
    test.__doc__ = f"引擎门控场景：{name}（scenarios.md）"
    return test


for _name, _commands in ENGINE_GATED.items():
    globals()["test_scenario_" + _name] = _engine_gated(_name)


# ---------------------------------------------------------------------------
# 清单核对（可执行清单属性）
# ---------------------------------------------------------------------------


def _scenario_names():
    text = SCENARIOS_MD.read_text(encoding="utf-8")
    return re.findall(r"^- \*\*场景名\*\*：`([a-z0-9_]+)`", text, re.M)


def _scenario_tests():
    """模块级 test_scenario_* 函数 + Test* 类里的 test_scenario_* 方法。"""
    import inspect
    found = {}
    for module_name in TEST_MODULES:
        for candidate in ("tests.rehearsal." + module_name, module_name):
            module = sys.modules.get(candidate)
            if module is not None:
                break
        else:
            try:
                module = importlib.import_module("tests.rehearsal." + module_name)
            except ImportError:
                module = importlib.import_module(module_name)
        for attr in dir(module):
            value = getattr(module, attr)
            if attr.startswith("test_scenario_") and inspect.isfunction(value):
                found[attr[len("test_scenario_"):]] = f"{module_name}.{attr}"
            elif (inspect.isclass(value) and attr.startswith("Test")
                    and value.__module__ == module.__name__):
                for method_name, method in inspect.getmembers(
                        value, predicate=inspect.isfunction):
                    if method_name.startswith("test_scenario_"):
                        found[method_name[len("test_scenario_"):]] = (
                            f"{module_name}.{attr}.{method_name}")
    return found


def test_scenario_checklist_matches_scenarios_md():
    """scenarios.md 25 项与 test_scenario_<name> 一一对应（可执行清单）。"""
    names = _scenario_names()
    assert len(names) == 25, f"scenarios.md 应有 25 个场景，实际 {len(names)}"
    assert len(set(names)) == len(names), "scenarios.md 场景名重复"
    tests = _scenario_tests()
    missing = [name for name in names if name not in tests]
    assert not missing, f"场景缺少对应用例：{missing}"


def test_scenario_checklist_fault_wordlist_matches_conftest():
    """scenarios.md 故障词表与 conftest.FAULTS 一致（16 个开关）。"""
    text = SCENARIOS_MD.read_text(encoding="utf-8")
    wordlist_line = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## 故障注入开关词表"):
            for candidate in lines[index + 1:]:
                if candidate.startswith("`"):
                    wordlist_line = candidate
                    break
            break
    assert wordlist_line, "scenarios.md 缺少故障注入开关词表"
    listed = set(re.findall(r"`([a-z0-9_]+)`", wordlist_line))
    assert listed == set(FAULTS), (
        f"词表不一致：scenarios.md={sorted(listed)} conftest={sorted(FAULTS)}")


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
