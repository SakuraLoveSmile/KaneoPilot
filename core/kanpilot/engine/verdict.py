"""Engine-owned verdict (protocol §5.27 / target A10, §5.18.6 verify env).

The core — not a model transcript — decides the verdict:

1. **effective diff** — the worktree's changed files *excluding* ``.pi/**``
   (the engine/plan bookkeeping tree). An empty effective diff is a no-op
   execution (空转) → quality ladder (阶梯 3).
2. **core-run verify** — every command the plan declared (``plan_verify``) is
   run by the core in the user's **login shell** with the project ``verify_env``
   injected and a configurable timeout. Plus transcripts are UI-only.

Shell choice (A4 spike ③, ``docs/evidence/spike-verify-env-20261010.md``): the
default is ``$SHELL -l -c`` (the user's login shell — zsh here, whose
``~/.zprofile`` carries homebrew/pyenv PATH), **not** ``/bin/bash -l -c`` which
misses that PATH and yields ``rc=127`` for ``gh``/``pytest``. Override with
``KANPILOT_VERIFY_SHELL``. When ``verify_env`` has no ``PATH`` the login shell's
``PATH`` is probed once and injected (never hardcoded ``/usr/bin:/bin``).

``rc=127`` / "command not found" is attributed as an **environment failure**
(阶梯 2), separate from a verify failure (阶梯 3); :func:`run_command` reports
``command_not_found`` and :func:`evaluate` surfaces ``environment_failure`` so
the caller dispatches to the right ladder.

This module does **not** modify :mod:`kanpilot.verification_checks`; it reuses
its data sources (``plan_verify`` commands, ``round_store`` worktree) and check
shape, replacing only *how the shell is started* and the *diff criterion*. The
verdict receipt is recorded by the ``engine-verdict`` command handler.

Receipt decision (intentional — do NOT "fix" by hard-wiring
``verification_checks``): the engine records its verdict in
``extra.engine_verdict`` rather than reusing ``verification_checks``' receipt.
That receipt's validation (per-command output-file sha + worktree binding
freeze + ``verification_recovery.recover``) is built for the interactive,
user-driven rerun path and does not map onto the engine's synchronous in-loop
run; forcing it would risk corrupting the interactive recover path for marginal
gain. A10's acceptance — "the core decides the verdict, the core runs the
verify commands, and the Plus transcript never decides or is persisted" — is
fully met by ``extra.engine_verdict``.

Spec gap resolved: ``verify_env`` now read from project mapping
``items[project_id].verify_env`` (dict, default empty) per protocol §5.18.6.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

from .. import db, git_delivery, project_config, round_store, util

# .pi/** is engine/plan bookkeeping; it never counts as task work.
PI_EXCLUDE_PREFIX = ".pi/"
DEFAULT_VERIFY_TIMEOUT = 600
# A4 spike ③: the user's login shell (zsh) sources the PATH that has gh/pytest;
# /bin/bash -l misses it. Used only when neither KANPILOT_VERIFY_SHELL nor $SHELL
# is set.
DEFAULT_VERIFY_SHELL_BINARY = "/bin/zsh"
_OUTPUT_TAIL_BYTES = 4000


def verify_shell() -> List[str]:
    """Login-shell argv prefix ``[shell, '-l', '-c']`` (A4 spike ③).

    ``shell = KANPILOT_VERIFY_SHELL || $SHELL || /bin/zsh``.
    """
    binary = (os.environ.get("KANPILOT_VERIFY_SHELL", "").strip()
              or os.environ.get("SHELL", "").strip()
              or DEFAULT_VERIFY_SHELL_BINARY)
    return [binary, "-l", "-c"]


def verify_timeout() -> int:
    """Per-command verify timeout in seconds (KANPILOT_VERIFY_TIMEOUT)."""
    raw = os.environ.get("KANPILOT_VERIFY_TIMEOUT", "")
    try:
        value = int(raw) if raw else DEFAULT_VERIFY_TIMEOUT
    except ValueError:
        value = DEFAULT_VERIFY_TIMEOUT
    return value if value > 0 else DEFAULT_VERIFY_TIMEOUT


def project_verify_env(conn, project_id) -> Dict[str, str]:
    """Project verify_env dict injected into the verify shell (default {})."""
    _, _, items, _ = project_config.load_raw_mappings(conn)
    env = (items.get(project_id) or {}).get("verify_env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def effective_changes(worktree, *, exclude_prefix: str = PI_EXCLUDE_PREFIX) -> List[str]:
    """Changed files in the worktree, excluding the ``.pi/**`` bookkeeping tree.

    An empty list means the execution produced no countable task work.
    """
    prefix = exclude_prefix if exclude_prefix.endswith("/") else exclude_prefix + "/"
    changes = git_delivery.worktree_changes(str(worktree))
    return [c.get("path") or "" for c in changes
            if not (c.get("path") or "").startswith(prefix)]


def has_effective_changes(worktree, **kw) -> bool:
    return bool(effective_changes(worktree, **kw))


def login_shell_path() -> Optional[str]:
    """PATH as seen by the login shell (probed once; A4 spike ③).

    Returns ``None`` when the login shell cannot be probed; callers then keep
    the inherited PATH. Never hardcodes ``/usr/bin:/bin``.
    """
    try:
        proc = subprocess.run(verify_shell() + ['printf %s "$PATH"'],
                              capture_output=True, timeout=60)
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace").strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _command_not_found(code, tail: str) -> bool:
    """rc=127 or the shell's "command not found" → environment failure (阶梯 2)."""
    return code == 127 or "command not found" in tail.lower()


def run_command(command: str, *, cwd, env: Optional[Dict[str, str]] = None,
                timeout: Optional[int] = None) -> dict:
    """Run one verify command in the login shell; return an independent check.

    ``env`` is merged onto the inherited environment. ``command_not_found``
    flags rc=127 / "command not found" (environment failure, ladder 2).
    """
    timeout = timeout or verify_timeout()
    child = dict(os.environ)
    if env:
        child.update({str(k): str(v) for k, v in env.items()})
    shell = verify_shell()
    started = util.now_iso()
    timed_out, code, tail, detail = False, None, "", None
    try:
        proc = subprocess.run(shell + [command], cwd=str(cwd), env=child,
                              capture_output=True, timeout=timeout)
        code = proc.returncode
        tail = _decode(proc.stdout) + _decode(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out, detail = True, "验证命令超时，未认定通过"
        tail = _decode(exc.stdout) + _decode(exc.stderr)
    except OSError as exc:
        detail = "验证命令无法启动：" + str(exc)
    return {"command": command, "exit_code": code, "timed_out": timed_out,
            "command_not_found": _command_not_found(code, tail),
            "detail": detail, "started_at": started, "finished_at": util.now_iso(),
            "output_tail": tail[-_OUTPUT_TAIL_BYTES:]}


def run_verify_commands(commands, *, cwd, verify_env=None, timeout=None) -> List[dict]:
    """Run all verify commands; probe+inject the login-shell PATH when verify_env
    does not set PATH (A4 spike ③)."""
    env = dict(verify_env or {})
    if "PATH" not in env:
        probed = login_shell_path()
        if probed:
            env["PATH"] = probed
    return [run_command(c, cwd=cwd, env=env, timeout=timeout) for c in (commands or [])]


def evaluate(commands, worktree, *, verify_env=None, timeout=None,
             exclude_prefix: str = PI_EXCLUDE_PREFIX) -> dict:
    """Core verdict: non-empty effective diff AND every verify command exits 0.

    ``environment_failure`` (any rc=127 / command-not-found) is a ladder-2
    environment problem, distinct from a ladder-3 quality verify failure.
    """
    commands = list(commands or [])
    changes = effective_changes(worktree, exclude_prefix=exclude_prefix)
    empty_diff = not changes
    checks: List[dict] = []
    if not empty_diff:
        checks = run_verify_commands(commands, cwd=worktree, verify_env=verify_env,
                                     timeout=timeout)
    env_failure = any(c.get("command_not_found") for c in checks)
    if empty_diff:
        passed, detail = False, "执行完成后没有可计入的改动（.pi/** 已排除），判定空转"
    elif not commands:
        passed, detail = False, "计划未声明 verify 命令，需人工接受验证结果"
    elif env_failure:
        passed = False
        detail = "验证命令不存在（环境类，阶梯 2），请修复 verify 环境后重试"
    else:
        passed = len(checks) == len(commands) and all(
            type(c["exit_code"]) is int and c["exit_code"] == 0 and not c["timed_out"]
            for c in checks)
        detail = ("有效改动非空且验证命令逐条通过" if passed
                  else "验证未全部通过，请查看各条独立退出码与输出")
    return {"verification_ok": passed, "empty_diff": empty_diff,
            "environment_failure": env_failure, "effective_changes": changes,
            "checks": checks, "detail": detail}


def _task_verify_commands(task) -> List[str]:
    extra = db.task_extra(task)
    return list((extra.get("plan_verify") or {}).get("commands") or [])


def evaluate_task(conn, task, *, verify_env=None, timeout=None) -> dict:
    """Verdict for a task: commands from ``plan_verify``, cwd = execution worktree."""
    commands = _task_verify_commands(task)
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    worktree = (row["worktree_path"] if row and row["worktree_path"] else task["repository"])
    if verify_env is None:
        verify_env = project_verify_env(conn, task["project_id"])
    return evaluate(commands, worktree, verify_env=verify_env, timeout=timeout)
