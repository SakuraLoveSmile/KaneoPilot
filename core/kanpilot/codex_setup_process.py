"""Owned, bounded subprocess for the no-model offline Flutter setup launcher."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time

from . import process_owner

MAX_OUTPUT_BYTES = 1024 * 1024  # Per stream; checked every poll and before return.
POLL_SECONDS = .1


def _members(group):
    view = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="],
                          capture_output=True, text=True, check=True, timeout=2)
    members = []
    for line in view.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == str(group) and not fields[2].startswith("Z"):
            pid = int(fields[0])
            members.append((pid, process_owner.process_birth(pid)))
    return members


def _signal_group(process, birth, signum):
    # A signal may interrupt the initial birth read. The Popen child is still
    # unreaped in that case, so its PID cannot yet belong to another process.
    current = process_owner.process_birth(process.pid)
    if birth is not None and current is not None and current != birth:
        raise RuntimeError("Codex 离线准备进程 PID 已被复用，拒绝发信号")
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for a group with an exited/zombie leader.
        # Signal only still-live members whose individual birth is unchanged.
        for pid, member_birth in _members(process.pid):
            if member_birth is None:
                continue
            if process_owner.process_birth(pid) != member_birth:
                continue
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass


def _stop_group(process, birth):
    # Never poll/wait the leader before group cleanup: its unreaped PID reserves
    # the group identity even after a launcher exits with background children.
    try:
        _signal_group(process, birth, signal.SIGTERM)
        time.sleep(POLL_SECONDS)
    finally:
        # A stop signal during the TERM grace period must still reach KILL.
        _signal_group(process, birth, signal.SIGKILL)
        process.wait(timeout=3)
        deadline = time.monotonic() + 2
        while _members(process.pid):
            if time.monotonic() >= deadline:
                raise RuntimeError("Codex 离线准备进程组未能完全退出")
            time.sleep(POLL_SECONDS)


def _text(stream):
    stream.seek(0)
    return stream.read(MAX_OUTPUT_BYTES).decode("utf-8", "replace")


def _check_output(stdout, stderr):
    if any(os.fstat(stream.fileno()).st_size > MAX_OUTPUT_BYTES
           for stream in (stdout, stderr)):
        raise subprocess.SubprocessError("Codex 离线准备输出超过每流 1 MiB 限制")


def run(argv, *, cwd, env, timeout=180, check=None):
    """Return text CompletedProcess; preserve errors after confirmed cleanup.

    Temporary files prevent inherited stdout/stderr handles from blocking EOF.
    Output beyond 1 MiB per stream aborts setup; no model or task verification
    command is introduced here. The caller supplies the sandbox launcher argv.
    """
    if check is not None:
        check()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        birth = None
        try:
            birth = process_owner.process_birth(process.pid)
            deadline = time.monotonic() + timeout
            while True:
                if check is not None:
                    check()
                _check_output(stdout, stderr)
                current = process_owner.process_birth(process.pid)
                if current is None:
                    break
                if birth is None or current != birth:
                    raise RuntimeError("Codex 离线准备进程出生身份不可确认")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(min(POLL_SECONDS, remaining))
        except BaseException as error:
            try:
                _stop_group(process, birth)
            except BaseException as cleanup_error:
                # A TimeoutExpired is only safe to classify as a stopped setup
                # after cleanup succeeded, even if cleanup itself timed out.
                raise RuntimeError("Codex 离线准备进程组清理未确认：" + str(cleanup_error)) from cleanup_error
            if isinstance(error, subprocess.TimeoutExpired):
                error.output = _text(stdout)
                error.stderr = _text(stderr)
            raise
        try:
            _stop_group(process, birth)
        except BaseException as cleanup_error:
            raise RuntimeError("Codex 离线准备进程组清理未确认：" + str(cleanup_error)) from cleanup_error
        _check_output(stdout, stderr)
        return subprocess.CompletedProcess(argv, process.returncode, _text(stdout), _text(stderr))
