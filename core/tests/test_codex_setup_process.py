"""Real Python process fixtures; no Flutter, model, database or user data."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest import mock

import pytest

from kanpilot import codex_setup_process as setup, process_owner


def launcher(tmp_path, *, background=False, noisy=False):
    script = tmp_path / "launcher.py"
    script.write_text("""import os, signal, subprocess, sys, time
from pathlib import Path
Path('launcher.pid').write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
Path('child.pid').write_text(str(child.pid))
print('setup stdout', flush=True)
print('setup stderr', file=sys.stderr, flush=True)
""" + ("sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stdout.flush()\n" if noisy else "")
        + ("sys.exit(7)\n" if background else "time.sleep(60)\n"))
    return [sys.executable, str(script)]


def isolated_env(tmp_path):
    return {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def assert_gone(tmp_path):
    for name in ("launcher.pid", "child.pid"):
        path = tmp_path / name
        assert path.exists(), name
        assert process_owner.process_birth(int(path.read_text())) is None, name


def emergency_cleanup(tmp_path):
    # Only the fixture's private session group; never a shared test process.
    path = tmp_path / "launcher.pid"
    if path.exists():
        try:
            os.killpg(int(path.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_timeout_kills_descendant_holding_inherited_stdout(tmp_path):
    argv = launcher(tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as error:
            setup.run(argv, cwd=tmp_path, env=isolated_env(tmp_path), timeout=.5)
        assert error.value.output == "setup stdout\n"
        assert error.value.stderr == "setup stderr\n"
        assert time.monotonic() - started < 5
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)


def test_normal_launcher_exit_cleans_background_child(tmp_path):
    try:
        argv = launcher(tmp_path, background=True)
        result = setup.run(argv, cwd=tmp_path, env=isolated_env(tmp_path), timeout=5)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.args == argv
        assert result.returncode == 7
        assert result.stdout == "setup stdout\n"
        assert result.stderr == "setup stderr\n"
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)


class Cancelled(BaseException):
    pass


@pytest.mark.parametrize("error", [Cancelled("stop fixture"), process_owner.OwnerClosed("owner closed"),
                                  RuntimeError("owner check failed")])
def test_check_error_is_preserved_and_kills_all_children(tmp_path, error):
    calls = []
    def check():
        calls.append(time.monotonic())
        if (tmp_path / "child.pid").exists():
            raise error
    try:
        with pytest.raises(type(error)) as raised:
            setup.run(launcher(tmp_path), cwd=tmp_path, env=isolated_env(tmp_path),
                      timeout=5, check=check)
        assert raised.value is error
        assert len(calls) >= 2
        assert max(right-left for left, right in zip(calls, calls[1:])) <= .25
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)


def test_initial_check_failure_does_not_launch(tmp_path):
    error = process_owner.OwnerClosed("already closed")
    with mock.patch.object(setup.subprocess, "Popen") as popen:
        with pytest.raises(process_owner.OwnerClosed) as raised:
            setup.run(["unused"], cwd=tmp_path, env={}, check=mock.Mock(side_effect=error))
    assert raised.value is error
    popen.assert_not_called()


def test_success_returns_text_and_uses_supplied_directory_and_environment(tmp_path):
    argv = [sys.executable, "-c", "import os,sys; print(os.getcwd()); print(os.environ['HOME'],file=sys.stderr)"]
    result = setup.run(argv, cwd=tmp_path, env=isolated_env(tmp_path), timeout=5)
    assert result.returncode == 0
    assert result.stdout == str(tmp_path) + "\n"
    assert result.stderr == str(tmp_path) + "\n"


def test_interrupted_initial_birth_read_still_cleans_new_process(tmp_path):
    error = Cancelled("interrupted identity read")
    original_popen = subprocess.Popen
    original_birth = process_owner.process_birth
    processes = []
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    first = True
    def birth(pid):
        nonlocal first
        if first:
            first = False
            raise error
        return original_birth(pid)
    with mock.patch.object(setup.subprocess, "Popen", side_effect=popen), \
         mock.patch.object(setup.process_owner, "process_birth", side_effect=birth):
        with pytest.raises(Cancelled) as raised:
            setup.run([sys.executable,"-c","import time; time.sleep(60)"],
                      cwd=tmp_path, env=isolated_env(tmp_path), timeout=5)
    assert raised.value is error
    assert processes[0].returncode is not None
    assert original_birth(processes[0].pid) is None


def test_excessive_output_aborts_and_cleans_process_group(tmp_path):
    try:
        with pytest.raises(subprocess.SubprocessError, match="1 MiB"):
            setup.run(launcher(tmp_path, noisy=True), cwd=tmp_path, env=isolated_env(tmp_path), timeout=5)
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)


def test_pid_reuse_never_signals_unrelated_group():
    process = mock.Mock(pid=12345)
    with mock.patch.object(setup.process_owner, "process_birth", return_value={"start":2}), \
         mock.patch.object(setup.os, "killpg") as killpg:
        with pytest.raises(RuntimeError, match="PID 已被复用"):
            setup._signal_group(process, {"start":1}, signal.SIGTERM)
    killpg.assert_not_called()


def test_macos_permission_fallback_checks_each_member_birth():
    process = mock.Mock(pid=12345)
    leader = {"start":1}
    child = {"start":2}
    with mock.patch.object(setup.process_owner, "process_birth", side_effect=[None,child,{"start":99}]), \
         mock.patch.object(setup.os, "killpg", side_effect=PermissionError), \
         mock.patch.object(setup, "_members", return_value=[(12346,child),(12347,child)]), \
         mock.patch.object(setup.os, "kill") as kill:
        setup._signal_group(process,leader,signal.SIGKILL)
    kill.assert_called_once_with(12346,signal.SIGKILL)


def test_stop_during_cleanup_grace_still_kills_and_reaps(tmp_path):
    error = Cancelled("stop during TERM grace")
    original_sleep = time.sleep
    interrupted = False
    def sleep(seconds):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise error
        original_sleep(seconds)
    process = subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],
                               start_new_session=True, cwd=tmp_path, env=isolated_env(tmp_path))
    birth = process_owner.process_birth(process.pid)
    with mock.patch.object(setup.time, "sleep", side_effect=sleep):
        with pytest.raises(Cancelled) as raised:
            setup._stop_group(process,birth)
    assert raised.value is error
    assert process.returncode is not None
    assert process_owner.process_birth(process.pid) is None


def test_cleanup_error_reports_unconfirmed_cleanup_and_keeps_check_context(tmp_path):
    error = Cancelled("original stop")
    def check():
        if (tmp_path / "child.pid").exists():
            raise error
    original = setup._stop_group
    def cleanup(process, birth):
        original(process, birth)
        raise RuntimeError("fixture cleanup error")
    try:
        with mock.patch.object(setup, "_stop_group", side_effect=cleanup):
            with pytest.raises(RuntimeError, match="进程组清理未确认") as raised:
                setup.run(launcher(tmp_path), cwd=tmp_path, env=isolated_env(tmp_path), timeout=5, check=check)
        assert str(raised.value.__cause__) == "fixture cleanup error"
        assert raised.value.__cause__.__context__ is error
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)


@pytest.mark.parametrize("background", [False, True])
def test_cleanup_failure_cannot_be_classified_as_confirmed_setup_timeout(tmp_path, background):
    original = setup._stop_group
    cleanup_error = subprocess.TimeoutExpired("fixture cleanup", 3)
    def cleanup(process, birth):
        original(process, birth)
        raise cleanup_error
    try:
        with mock.patch.object(setup, "_stop_group", side_effect=cleanup):
            with pytest.raises(RuntimeError, match="进程组清理未确认") as raised:
                setup.run(launcher(tmp_path, background=background), cwd=tmp_path,
                          env=isolated_env(tmp_path), timeout=5 if background else .3)
        assert not isinstance(raised.value, subprocess.TimeoutExpired)
        assert raised.value.__cause__ is cleanup_error
        if not background:
            assert isinstance(cleanup_error.__context__, subprocess.TimeoutExpired)
            assert cleanup_error.__context__.cmd != "fixture cleanup"
        assert_gone(tmp_path)
    finally:
        emergency_cleanup(tmp_path)
