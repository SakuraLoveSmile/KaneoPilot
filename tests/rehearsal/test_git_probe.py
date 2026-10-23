"""70ms git 探针修复回归（automation_policy.automation_preflight）。

历史缺陷：``automation_preflight`` 的 git 探测超时为 0.07s，冷启动/大盘仓库下
git 进程本身就可能超过 70ms，``TimeoutExpired`` 被 except 分支吞掉后误报
``not_git_repo``（阶梯 2 环境失败），把可用仓库判成不可用。

本文件钉住修复后契约：
- 慢而正常的 git（``git_slow``：0.3s）→ 不得报 ``not_git_repo``；
- 挂起的 git（``git_hang``）→ 报 ``git_probe_timeout``，与 ``not_git_repo``
  分开（超时 ≠ 仓库不可用）；
- 真失败仍可判：非 git 目录 → ``not_git_repo``；非 GitHub origin →
  ``origin_not_github``；
- ``KANPILOT_GIT_PROBE_TIMEOUT`` 可覆盖超时，非法值回落缺省。

驱动方式：核心内函数直调（``kanpilot.automation_policy``），与
``core/tests/test_automation_policy.py`` 同构；非 git 就绪项按同一方式打桩
（手写 save/restore，不依赖 pytest monkeypatch——unittest discover 兜底
路径同样可跑）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

# 注册演练 conftest 的 fixture（unittest discover 兜底路径不会自动加载）。
try:
    from tests.rehearsal import conftest as _rehearsal_conftest  # noqa: F401,E402
    from tests.rehearsal.conftest import env_override  # noqa: E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    import conftest as _rehearsal_conftest  # noqa: F401,E402
    from conftest import env_override  # noqa: E402

try:
    from tests.rehearsal.conftest import active_faults, env_override  # noqa: E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    from conftest import active_faults, env_override  # noqa: E402

# tests/conftest 导入时已把 core/ 插入 sys.path（KANPILOT_CORE_BIN 约定）。
from kanpilot import (  # noqa: E402
    automation_policy as policy, db, exec_config, review_config, util,
)
NOW = "2026-10-10T00:00:00+00:00"
PID = "project00001"


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    yield SimpleNamespace(conn=conn, now_iso=NOW, now=util.parse_ts(NOW))
    conn.close()


@pytest.fixture
def ready():
    """非 git 就绪项全部打桩成就绪，只剩 git 探测结果进入 errors。

    手写 save/restore：不依赖 pytest monkeypatch（两种收集路径通用）。
    """
    patches = [
        (policy, "_planner_ready", lambda conn: True),
        (policy, "_executable", lambda binary: True),
        (review_config, "load", lambda conn: {"backend": "codex"}),
        (exec_config, "load",
         lambda conn: {"backend": "pi_desktop",
                       "pi_desktop": {"permission_mode": "auto"}}),
    ]
    saved = [(module, name, getattr(module, name)) for module, name, _ in patches]
    for module, name, value in patches:
        setattr(module, name, value)
    try:
        yield
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


def _map_repo(conn, repo):
    db.set_setting(conn, "project_mappings",
                   {"items": {PID: {"repository": str(repo)}}})


def _remove_push_redirect(git):
    # Preflight is local-only: an unrewritten GitHub origin needs no network.
    # Keep the redirect in delivery fixtures, where it prevents real pushes.
    subprocess.run(["git", "-C", str(git.repo), "config", "--unset-all",
                    f"url.{git.bare}.pushInsteadOf"], check=True,
                   capture_output=True, timeout=30)


def _codes(errors):
    return {e["code"] for e in errors}


def _git_wrapper(tmp_path, delay):
    """PATH 前置的假 git：先 sleep ``delay`` 秒再执行真 git。"""
    real_git = shutil.which("git")
    assert real_git, "未找到真实 git（演练 git 包装器需要）"
    bin_dir = tmp_path / "git-wrapper"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "git"
    script.write_text(f"#!/bin/sh\nsleep {delay}\nexec {real_git} \"$@\"\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return str(bin_dir)


def test_scenario_git_probe_timeout_default_and_override():
    # 缺省 5s；env 覆盖生效；非法值回落缺省（不炸）。
    assert policy.git_probe_timeout() == policy.GIT_PROBE_TIMEOUT_SECONDS
    with env_override(KANPILOT_GIT_PROBE_TIMEOUT="2.5"):
        assert policy.git_probe_timeout() == 2.5
    with env_override(KANPILOT_GIT_PROBE_TIMEOUT="not-a-number"):
        assert policy.git_probe_timeout() == policy.GIT_PROBE_TIMEOUT_SECONDS
    with env_override(KANPILOT_GIT_PROBE_TIMEOUT="-1"):
        assert policy.git_probe_timeout() == policy.GIT_PROBE_TIMEOUT_SECONDS


def test_scenario_git_probe_slow_not_false_not_git_repo(
        ctx, ready, rehearsal_git, tmp_path):
    """git_slow（每条 git 0.3s）下有效仓库必须通过——旧 0.07s 超时会误报。"""
    _remove_push_redirect(rehearsal_git)
    wrapper = _git_wrapper(tmp_path, "0.3")
    _map_repo(ctx.conn, rehearsal_git.repo)
    with env_override(PATH=wrapper + os.pathsep + os.environ.get("PATH", "")):
        assert policy.automation_preflight(ctx.conn, PID) == []


def test_scenario_git_probe_timeout_distinct_from_not_git_repo(
        ctx, ready, rehearsal_git, tmp_path):
    """git_hang → git_probe_timeout；不得误报 not_git_repo。"""
    wrapper = _git_wrapper(tmp_path, "30")
    _map_repo(ctx.conn, rehearsal_git.repo)
    with env_override(PATH=wrapper + os.pathsep + os.environ.get("PATH", ""),
                      KANPILOT_GIT_PROBE_TIMEOUT="1"):
        errors = policy.automation_preflight(ctx.conn, PID)
    codes = _codes(errors)
    assert "git_probe_timeout" in codes, errors
    assert "not_git_repo" not in codes, errors
    timeout_error = next(e for e in errors if e["code"] == "git_probe_timeout")
    assert "超时" in timeout_error["message"], timeout_error


def test_scenario_git_probe_not_git_repo_still_detected(ctx, ready, tmp_path):
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    _map_repo(ctx.conn, plain)
    assert "not_git_repo" in _codes(
        policy.automation_preflight(ctx.conn, PID))


def test_scenario_git_probe_origin_not_github(ctx, ready, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True,
                   timeout=30)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://example.invalid/owner/repo.git"],
                   check=True, capture_output=True, timeout=30)
    _map_repo(ctx.conn, repo)
    errors = policy.automation_preflight(ctx.conn, PID)
    assert "origin_not_github" in _codes(errors)
    assert "not_git_repo" not in _codes(errors), errors


def test_scenario_git_probe_github_origin_passes(ctx, ready, rehearsal_git):
    """无故障：未改写的 GitHub origin 本地探针全绿。"""
    _remove_push_redirect(rehearsal_git)
    _map_repo(ctx.conn, rehearsal_git.repo)
    assert policy.automation_preflight(ctx.conn, PID) == []


def test_scenario_git_probe_local_push_redirect_is_rejected(ctx, ready, rehearsal_git):
    """GitHub fetch URL 不掩盖改写到本地裸仓库的推送目标。"""
    _map_repo(ctx.conn, rehearsal_git.repo)
    errors = policy.automation_preflight(ctx.conn, PID)
    assert "branch_diverged" in _codes(errors), errors
    assert "origin_not_github" not in _codes(errors), errors


def test_scenario_git_probe_push_rejected_fault(tmp_path):
    """push_rejected 故障注入生效：裸 remote 拒绝推送（交付场景的前置）。"""
    git = _rehearsal_conftest.make_rehearsal_git(tmp_path, ("push_rejected",))
    result = subprocess.run(
        ["git", "-C", str(git.repo), "push", "origin", "main"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "rejected" in (result.stderr or "").lower(), result.stderr


def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
