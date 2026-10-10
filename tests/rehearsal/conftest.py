"""演练台共享基座（tests/rehearsal/）。

复用 ``tests/conftest.py`` 的假 Kaneo / factory / cli / tmp_env；本目录新增：

- 假 Plus（``fake_plus.py``：PI-Desktop MCP 控制面 JSON-RPC stub）；
- 假 gh（``fake_gh.py``：POSIX shell 桩，经 ``KANPILOT_GH_BIN`` 指向）；
- 临时 git 仓库 + 裸 remote + GitHub 形状 origin（``url.<bare>.insteadOf``
  重写，preflight 看到 GitHub、push 不触网）；
- 故障注入开关：环境变量 ``KANPILOT_REHEARSAL_FAULT``（逗号分隔）或显式参数。

运行方式（无真实 pytest 亦可）：``python3 -m pytest tests/rehearsal -q``
（仓库根 ``pytest.py`` 桩：有真实 pytest 透传，否则 unittest discover 兜底）。
bootstrap 在任何 ``import pytest`` 之前固定 sys.path，两种收集模式下
父 conftest 的 fixture 桶（键 ``conftest``）均可被解析。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)          # tests/
_REPO = os.path.dirname(_PARENT)          # 仓库根
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

# 父 conftest 的 fixture 与 helper 再导出（fixture 桶键同为 'conftest'，
# tests/unittest_compat.py 的 fixture 解析可见）。
from tests.conftest import *  # noqa: E402,F401,F403
from tests.conftest import (  # noqa: E402
    PROJECTS, WORKSPACE_ID, FakeKaneo, TaskFactory, run_cli_raw,
)

try:  # 包上下文（真实 pytest / dotted discover）
    from tests.rehearsal.fake_gh import write_fake_gh
    from tests.rehearsal.fake_plus import FakePlus
except ImportError:  # 顶层模块上下文（discover -s tests/rehearsal 无 -t）
    from fake_gh import write_fake_gh  # type: ignore
    from fake_plus import FakePlus  # type: ignore

# ---------------------------------------------------------------------------
# 故障注入词表（scenarios.md 冻结；每项可经环境变量或显式参数激活）
# ---------------------------------------------------------------------------

FAULTS = frozenset({
    # Kaneo
    "kaneo_timeout", "kaneo_500",
    # Plus（PI-Desktop 控制面）。词表与 scenarios.md 冻结的 16 个开关一致；
    # plus_auth_fail / plus_tools_missing 仅支持 FakePlus 显式构造注入，
    # 不进入环境变量词表（冻结清单未列）。
    "plus_unreachable", "plus_timeout", "plus_send_lost",
    # gh / git / 交付
    "gh_missing", "gh_timeout", "gh_not_logged_in",
    "push_rejected", "pr_closed_unmerged",
    "git_slow", "git_hang",
    # 引擎/调度（Phase 3 落地后由场景主体消费）
    "verify_fail", "empty_diff", "engine_kill_at_step", "lease_expired",
})


def active_faults() -> frozenset:
    """从 KANPILOT_REHEARSAL_FAULT 解析激活的故障名（未知名报错）。"""
    raw = os.environ.get("KANPILOT_REHEARSAL_FAULT", "")
    faults = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = faults - FAULTS
    if unknown:
        raise AssertionError(
            f"未知故障注入开关：{sorted(unknown)}（可用：{sorted(FAULTS)}）")
    return frozenset(faults)


def terminate_lingering_workers(timeout: float = 5.0) -> list:
    """终止本进程派生的常驻 worker 子进程，返回被终止的 pid 列表。

    ``tests/test_auto_pipeline.py`` 的 worker 以 ``start_new_session=True``
    派生、用例结束后仍常驻轮询；下一用例的假 Plus 若复用同一临时端口，常驻
    worker 会连接新服务器并消费其 turn 状态队列，造成跨用例观测漂移（父夹具
    已知竞态，根因修复归属父文件）。这里在用例收口时按 **ppid==本进程** 清理
    worker——只动本进程树的子进程，不影响其他测试进程/ agent。
    """
    mine = os.getpid()
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "-o", "ppid=", "-o", "command="],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    victims = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if ppid != mine:
            continue
        command = parts[2]
        if ("kanpilot.pi_desktop_worker" in command
                or "kanpilot.pi_worker" in command):
            victims.append(pid)

    def _alive(pid):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
        return True

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(_alive(pid) for pid in victims):
                break
            time.sleep(0.05)
    return victims


@pytest.fixture(autouse=True)
def _rehearsal_worker_cleanup():
    """用例收口（最后执行的 finalizer）：清理常驻 worker，防跨用例漂移。"""
    yield
    terminate_lingering_workers()


@pytest.fixture
def rehearsal_faults():
    """当前激活的故障注入名集合。"""
    return active_faults()


@pytest.fixture
def tmp_path():
    """pytest 等价 tmp_path：解析符号链接 + 用后清理。

    macOS 上 ``/var`` → ``/private/var``：核心在子进程里 ``Path.resolve()``，
    夹具路径字符串若不解析，父 conftest 的 factory 全链会因仓库路径不一致
    validation_failed。unittest discover 兜底路径没有 pytest 内建 fixture，
    这里同时补上（解析语义与 pytest 一致）。
    """
    path = Path(tempfile.mkdtemp(prefix="kp-rehearsal-")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 假 Plus / 假 gh / 演练 git 仓库
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_plus(tmp_path, rehearsal_faults):
    """假 PI-Desktop MCP 控制面 + 连接文件（写进假 HOME 默认路径）。

    连接文件路径 = ``$HOME/.pi-desktop-plus/mcp-control.json``（tmp_env 已把
    HOME 指向 ``tmp_path/home``），doctor 的 plus 探针按默认路径读取。
    """
    server = FakePlus(faults=rehearsal_faults).start()
    connection_file = tmp_path / "home" / ".pi-desktop-plus" / "mcp-control.json"
    server.write_connection_file(connection_file)
    yield SimpleNamespace(
        server=server, connection_file=connection_file,
        base_url=server.base_url, token=server.token,
    )
    server.stop()


@pytest.fixture
def fake_gh(tmp_path, rehearsal_faults):
    """假 gh 可执行；返回 KANPILOT_GH_BIN 环境覆盖（None = gh_missing）。"""
    if "gh_missing" in rehearsal_faults:
        return str(tmp_path / "definitely-missing-gh")
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir(exist_ok=True)
    path = write_fake_gh(bin_dir / "gh", faults=rehearsal_faults)
    return str(path)



# ---------------------------------------------------------------------------
# 兼容助手：最小 monkeypatch（unittest discover 兜底路径无 pytest 内建 fixture）
# ---------------------------------------------------------------------------

_MISSING = object()


class _MiniMonkeyPatch:
    """pytest monkeypatch 的最小子集（setenv/delenv/setattr/undo）。

    tests/unittest_compat.py 收集引擎不提供内建 fixture；父 conftest 的
    tmp_env 依赖 ``monkeypatch``。真实 pytest 下本机会覆盖内建 fixture
    （仅本目录生效），行为与 pytest 语义一致：退出时逆序还原。
    """

    def __init__(self):
        self._undo = []

    def setenv(self, name, value):
        self._undo.append(("env", name, os.environ.get(name)))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        self._undo.append(("env", name, os.environ.get(name)))
        os.environ.pop(name, None)

    def setattr(self, target, name, value, raising=True):
        if isinstance(target, str):
            from importlib import import_module
            module_name, attr = target.rsplit(".", 1)
            target = import_module(module_name)
            name = attr
        self._undo.append(("attr", target, name,
                           getattr(target, name, _MISSING)))
        setattr(target, name, value)

    def undo(self):
        for item in reversed(self._undo):
            if item[0] == "env":
                _, name, value = item
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            else:
                _, target, name, value = item
                if value is _MISSING:
                    try:
                        delattr(target, name)
                    except AttributeError:
                        pass
                else:
                    setattr(target, name, value)
        self._undo.clear()


@pytest.fixture
def monkeypatch():
    """最小 monkeypatch（见 _MiniMonkeyPatch）；yield 收口逆序还原。"""
    patch = _MiniMonkeyPatch()
    yield patch
    patch.undo()


class env_override:
    """进程内环境变量覆盖（with 语法，退出还原）；不依赖任何 fixture。"""

    def __init__(self, **values):
        self._values = values
        self._saved = {}

    def __enter__(self):
        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def make_rehearsal_git(root, faults=()):
    """构造演练 git 仓库 + 裸 remote + GitHub 形状 origin（pushInsteadOf）。

    ``url.<bare>.pushInsteadOf`` 只作用于推送：``git remote get-url origin``
    仍返回 GitHub 形状 URL（automation_preflight 核验 origin 用），push 实际
    落到本地裸 remote、不触网。``push_rejected`` → 裸 remote 装 pre-receive
    拒绝钩子。fixture 与显式调用（测试按需注入故障）共用。
    """
    root = Path(root)
    repo = root / "repo"
    bare = root / "remote.git"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(bare)],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True, timeout=30)
    for args in (("config", "user.name", "Rehearsal"),
                 ("config", "user.email", "rehearsal@example.invalid")):
        subprocess.run(["git", "-C", str(repo), *args],
                       check=True, capture_output=True, timeout=30)
    (repo / "README.md").write_text("rehearsal\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"],
                   check=True, capture_output=True, timeout=30)
    origin_url = "https://github.com/rehearsal-owner/rehearsal-repo.git"
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    origin_url], check=True, capture_output=True, timeout=30)
    # 仅推送重写：get-url 仍返回 GitHub 形状 URL，push 落到本地裸 remote。
    subprocess.run(["git", "-C", str(repo), "config",
                    f"url.{bare}.pushInsteadOf", origin_url],
                   check=True, capture_output=True, timeout=30)
    if "push_rejected" in faults:
        hook = bare / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\necho 'rehearsal: push rejected by hook' >&2\nexit 1\n",
            encoding="utf-8")
        hook.chmod(0o755)
    return SimpleNamespace(repo=repo, bare=bare, origin_url=origin_url)


@pytest.fixture
def rehearsal_git(tmp_path, rehearsal_faults):
    """演练 git 仓库夹具（故障来自 KANPILOT_REHEARSAL_FAULT）。"""
    return make_rehearsal_git(tmp_path, rehearsal_faults)



@pytest.fixture
def rehearsal_git_bin(tmp_path, rehearsal_faults):
    """PATH 前置的假 git：``git_slow``（慢 0.3s 但成功）/ ``git_hang``（挂起）。

    返回 PATH 前缀（无故障返回 None）。供 70ms 探针回归：慢而正常的 git
    不得误报 not_git_repo；挂起必须报 git_probe_timeout。
    """
    if not rehearsal_faults & {"git_slow", "git_hang"}:
        return None
    real_git = shutil.which("git")
    assert real_git, "未找到真实 git（演练 git 包装器需要）"
    bin_dir = tmp_path / "git-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "git"
    delay = "30" if "git_hang" in rehearsal_faults else "0.3"
    script.write_text(f"#!/bin/sh\nsleep {delay}\nexec {real_git} \"$@\"\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return str(bin_dir)


# ---------------------------------------------------------------------------
# 复合夹具：环境覆盖收集 + CLI 注入
# ---------------------------------------------------------------------------


@pytest.fixture
def rehearsal(tmp_env, rehearsal_faults, fake_plus, fake_gh,
              rehearsal_git, rehearsal_git_bin):
    """演练环境聚合：返回 env 覆盖字典与各假件句柄。

    ``rehearsal_cli`` 把这些覆盖合并进 CLI 子进程环境——monkeypatch 在
    tmp_env 快照 env 之后设置不生效，必须显式合并。
    """
    env = {
        "KANPILOT_GH_BIN": fake_gh,
        "KANPILOT_REHEARSAL_FAULT": ",".join(sorted(rehearsal_faults)),
    }
    if rehearsal_git_bin:
        env["PATH"] = rehearsal_git_bin + os.pathsep + os.environ.get("PATH", "")
    return SimpleNamespace(
        env=env,
        faults=rehearsal_faults,
        plus=fake_plus,
        gh=fake_gh,
        git=rehearsal_git,
        git_bin=rehearsal_git_bin,
        tmp_env=tmp_env,
    )


@pytest.fixture
def rehearsal_cli(cli, rehearsal):
    """已注入故障开关的 CLI（合并 rehearsal.env 覆盖）。"""
    cli.env.update(rehearsal.env)
    return cli


@pytest.fixture
def rehearsal_factory(rehearsal_cli, tmp_env, fake_kaneo):
    """带故障注入的 TaskFactory（复用父 conftest factory 的构造方式）。"""
    return TaskFactory(rehearsal_cli, tmp_env, fake_kaneo)
