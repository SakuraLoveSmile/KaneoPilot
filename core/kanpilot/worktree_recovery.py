"""Explicit, backed-up manual recovery of abandoned historical Plus leases.

This command is never called by the automatic reaper. Human authorization is
recorded separately from the missing Plus terminal proof.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile

from . import constants, db, headless_adapter, runtime_operations, util
from . import worktree_pool as pool

_TERMINAL = ("succeeded", "failed", "cancelled")
_LEASE_KEYS = ("slot_id", "holder_id", "holder_stage", "lease_token", "path")
_TOOL = "worktree-slot-recover"
_HARNESS = "core_manual"


def _require(value, message):
    util.require(value, message, "conflict")


def _dead(pid):
    _require(type(pid) is int and pid > 0, "原持有进程身份缺失")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    raise util.KanpilotError("conflict", "原持有进程仍存活或无法核实，未恢复槽位")


def _checkpoint(conn, row):
    requests = conn.execute("SELECT * FROM requests WHERE run_id=? AND stage=?",
                            (row["holder_id"], row["holder_stage"])).fetchall()
    _require(len(requests) == 1, "原请求缺失或身份不唯一")
    request = requests[0]
    payload = db.request_payload(request)
    config = payload.get("review_config") or {}
    backend = payload.get("planner_backend" if row["holder_stage"] == "plan" else "review_backend")
    if backend is None and isinstance(config, dict):
        backend = config.get("backend")
    _require(request["status"] in _TERMINAL and backend == "pi_desktop",
             "仅能人工恢复已终态的原 Plus 规划/评审请求")
    frozen = payload.get("pool_lease")
    _require(isinstance(frozen, dict) and all(frozen.get(k) == row[k] for k in _LEASE_KEYS),
             "原请求冻结租约不匹配")
    if row["holder_stage"] == "plan":
        paths = [headless_adapter.resolve_source(conn).run_dir(
            request["task_id"], row["holder_id"]) / "job.json"]
    else:
        paths = (constants.app_support() / "executor-sessions" / "review").glob("*/job.json")
    matches = []
    for path in paths:
        pool._no_links(path)
        try:
            job = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if (isinstance(job, dict) and job.get("request_id") == request["request_id"]
                and job.get("task_id") == request["task_id"]
                and job.get("run_id") == row["holder_id"]):
            matches.append((path, job))
    _require(len(matches) == 1, "原执行检查点缺失或身份不唯一")
    path, job = matches[0]
    lease = job.get("pool_lease")
    _require(job.get("backend") == "pi_desktop"
             and job.get("status") in ("finished", "failed", "cancelled")
             and job.get("pid") == row["owner_pid"]
             and isinstance(lease, dict) and all(lease.get(k) == row[k] for k in _LEASE_KEYS)
             and all(isinstance(row[k], str) and row[k] and job.get(k) == row[k]
                     for k in ("session_id", "turn_id"))
             and (row["holder_stage"] != "review" or
                  (job.get("stage") == "review" and job.get("invocation_id") == path.parent.name)),
             "原检查点终态、进程、会话或租约身份不符")
    return request, path, job


def _file_set(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                files.append(name)
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                result[relative] = ("link", os.readlink(path))
            else:
                _require(stat.S_ISREG(mode), "槽位包含特殊文件，未恢复")
                result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(mode))
    return result


def _backup(row, directory):
    root = Path(directory).expanduser()
    _require(root.is_absolute(), "备份目录必须为绝对路径")
    pool._no_links(root)
    _require(not root.is_relative_to(Path(row["path"])), "备份不能位于待恢复槽位中")
    manifest_path = root / "manifest.json"
    pool._no_links(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    entries = [entry for entry in manifest.get("slots", [])
               if entry.get("slot_id") == row["slot_id"]]
    _require(len(entries) == 1, "备份清单缺少唯一槽位身份")
    entry = entries[0]
    _require(all(entry.get(k) == row[k] for k in ("holder_id", "path", "sha")),
             "备份不属于当前租约或冻结提交")
    name = entry.get("archive")
    _require(isinstance(name, str) and name and Path(name).name == name, "备份归档路径无效")
    archive = root / name
    pool._no_links(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    _require(digest == entry.get("archive_sha256"), "备份归档校验失败")
    slot = Path(row["path"])
    contents = {}
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            relative = PurePosixPath(member.name)
            _require(not relative.is_absolute() and ".." not in relative.parts,
                     "备份归档包含越界路径")
            # The original backup also retains the Git administrative index.
            if relative == PurePosixPath(slot.name + "-git-index"):
                _require(member.isfile(), "备份 Git 索引类型无效")
                continue
            _require(relative.parts and relative.parts[0] == slot.name,
                     "备份包含其他目录")
            if member.isdir():
                continue
            key = relative.relative_to(slot.name).as_posix()
            _require(key not in contents, "备份含重复文件")
            if member.issym():
                contents[key] = ("link", member.linkname)
            else:
                _require(member.isfile(), "备份包含不支持的文件类型")
                contents[key] = ("file", hashlib.sha256(handle.extractfile(member).read()).hexdigest(), member.mode)
    _require(contents == _file_set(slot), "备份与当前槽位文件不完整或不一致")
    return digest


def _native_processes():
    """Complete local argv scan; callers never log or return the argv contents."""
    try:
        result = subprocess.run(['/bin/ps', '-A', '-ww', '-o', 'pid=,ppid=,pgid=,uid=,args='],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
        _require(not result.stderr and len(result.stdout) < 16 * 1024 * 1024,
                 "本机进程扫描不完整，未恢复")
        rows = []
        for line in result.stdout.decode('utf-8', errors='strict').splitlines():
            fields = line.strip().split(None, 4)
            _require(len(fields) == 5 and all(v.isdigit() for v in fields[:3])
                     and re.fullmatch(r'-?\d+', fields[3]) is not None,
                     "本机进程扫描格式不可核验，未恢复")
            pid, ppid, pgid, uid = map(int, fields[:4])
            _require(pid > 0 and pgid > 0 and bool(fields[4]), "本机进程扫描身份无效")
            rows.append((pid, ppid, pgid, uid, fields[4]))
        _require(len({r[0] for r in rows}) == len(rows) and any(r[0] == os.getpid() for r in rows),
                 "本机进程扫描缺失或重复，未恢复")
        return rows
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError('conflict', '无法完成本机进程扫描，未恢复') from exc


def _native_file_users(roots, expected_pids):
    """Snapshot cwd/open-file users, including descendants with detached PGIDs."""
    def check_name(name, pid):
        if pid == os.getpid() or not name.startswith('/'):
            return
        value = Path(name.removesuffix(' (deleted)'))
        _require(not any(value == root or value.is_relative_to(root) for root in roots),
                 '仍有进程在槽位或原生规划证据目录中持有 cwd/打开文件，未恢复')

    found = set()
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/sbin/lsof', '-nP', '-u', str(os.geteuid()), '-F0pftn'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True)
            _require(not result.stderr and len(result.stdout) < 64 * 1024 * 1024,
                     '内核 cwd/文件扫描不完整，未恢复')
            pid, ftype, names = None, None, []
            def finish_file():
                if ftype in ('REG', 'DIR'):
                    _require(names and all(name.startswith('/') for name in names),
                             '进程文件路径不可核验，未恢复')
            for raw in result.stdout.split(b'\0'):
                raw = raw.lstrip(b'\n')
                if not raw:
                    continue
                field, value = chr(raw[0]), raw[1:].decode('utf-8', errors='strict')
                if field == 'p':
                    finish_file()
                    _require(value.isdigit(), '内核进程身份不可核验')
                    pid, ftype, names = int(value), None, []
                    found.add(pid)
                elif field == 'f':
                    finish_file()
                    _require(pid is not None, '内核文件缺少进程身份')
                    ftype, names = None, []
                elif field == 't':
                    ftype = value
                elif field == 'n':
                    _require(pid is not None, '内核路径缺少进程身份')
                    names.append(value)
                    check_name(value, pid)
                else:
                    _require(False, '内核文件扫描格式未知，未恢复')
            finish_file()
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise util.KanpilotError('conflict', '无法核验进程 cwd/打开文件，未恢复') from exc
    elif sys.platform.startswith('linux'):
        # Portable isolated tests: same-user kernel links supply the same proof.
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit(): continue
            pid = int(directory.name)
            try:
                if directory.stat().st_uid != os.geteuid(): continue
                check_name(os.readlink(directory / 'cwd'), pid)
                for fd in (directory / 'fd').iterdir():
                    try: check_name(os.readlink(fd), pid)
                    except FileNotFoundError: continue  # An individual FD closed.
                found.add(pid)
            except OSError:
                _dead(pid)  # Only ESRCH permits a process to vanish during the scan.
    else:
        _require(False, '此系统不能核验进程 cwd/打开文件，未恢复')
    _require(os.getpid() in found, '内核扫描缺少本进程，未恢复')
    for pid in expected_pids - found:
        _dead(pid)
    return 'same_uid_kernel_cwd_and_open_files'


_NATIVE_DIAGNOSTICS = {
    ('WARN', 'codex_rollout::list'): 'state db discrepancy during find_thread_path_by_id_str_in_subdir:',
    ('WARN', 'codex_core_plugins::manager'): 'failed to warm featured plugin ids cache error=',
    ('WARN', 'codex_core_plugins::manifest'): 'ignoring interface.defaultPrompt[',
    ('ERROR', 'codex_core::tools::router'): 'error=collab spawn failed:',
    ('WARN', 'codex_sandboxing::violation'): 'recorded sandbox violation:',
    ('WARN', 'codex_core::hook_runtime'): 'failed to resolve parent transcript path for subagent hook ',
}


def _native_log_clear(path, session_id):
    """Native stderr shares stdout: only specific complete diagnostic lines are allowed."""
    _require(isinstance(session_id, str) and bool(session_id), '原生规划缺少可核验会话身份')
    raw = path.read_bytes()
    _require(not raw or raw.endswith(b'\n'), '原生规划日志有不完整尾行，未恢复')
    active, commands, completed, seen, diagnostics = {}, [], set(), False, 0
    tool_kinds = {'command_execution'}
    passive = {'reasoning', 'agent_message', 'assistant_message', 'todo_list', 'error'}
    for line in raw.decode('utf-8', errors='strict').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            cleaned = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
            match = re.fullmatch(r'\d{4}-\d\d-\d\dT\S+Z\s+(WARN|ERROR)\s+([\w:]+): (.+)', cleaned)
            _require(match is not None and match.group(3).startswith(
                _NATIVE_DIAGNOSTICS.get((match.group(1), match.group(2)), '\0')),
                '原生规划日志包含未知诊断或损坏内容，未恢复')
            diagnostics += 1
            continue
        _require(isinstance(event, dict), '原生规划日志结构无效')
        kind = event.get('type')
        if kind == 'thread.started':
            _require(not seen and event.get('thread_id') == session_id,
                     '原生规划日志会话身份不符或包含多个会话，未恢复')
            seen = True
        elif kind in ('turn.started', 'turn.completed', 'turn.failed', 'error'):
            _require(seen, '原生规划日志缺少会话起点')
        elif kind in ('item.started', 'item.updated', 'item.completed'):
            item = event.get('item')
            _require(seen and isinstance(item, dict) and item.get('type') in tool_kinds | passive,
                     '原生规划日志包含未知工具类型，未恢复')
            if item['type'] in tool_kinds:
                ident = item.get('id')
                command = item.get('command')
                _require(isinstance(ident, str) and bool(ident) and isinstance(command, str) and bool(command),
                         '原生命令缺少身份或原始命令')
                if kind == 'item.completed':
                    _require(ident in active and active[ident]['command_sha256'] == util.sha256_text(command)
                             and item.get('status') in ('completed', 'failed') and type(item.get('exit_code')) is int,
                             '原生命令缺少匹配启动记录或完整终态，未恢复')
                    entry = active.pop(ident)
                    entry.update(status=item['status'], exit_code=item['exit_code'])
                    completed.add(ident)
                elif kind == 'item.started':
                    _require(ident not in active and ident not in completed, '原生命令身份重复，未恢复')
                    entry = dict(id=ident, command_sha256=util.sha256_text(command))
                    active[ident] = entry
                    commands.append(entry)
                else:
                    _require(ident in active and active[ident]['command_sha256'] == util.sha256_text(command),
                             '原生命令更新身份不符，未恢复')
        else:
            _require(False, '原生规划日志包含未知事件，未恢复')
    _require(seen and not active, '原生规划仍有未完成工具或缺少完整会话日志，未恢复')
    return {'native_log_sha256': hashlib.sha256(raw).hexdigest(), 'diagnostic_lines': diagnostics,
            'commands': commands, 'ledger_sha256': util.sha256_text(util.jdump(commands))}


def _native_manual_audit(payload, proof):
    """An explicit agent attestation; this is not a proof of descendant lifetime."""
    commands = proof.pop('commands')
    if not commands:
        return
    audit_path = Path(payload.get('native_audit_path') or '')
    backup = Path(payload['backup_dir'])
    _require(audit_path.is_absolute() and audit_path.parent == backup
             and audit_path.name != 'manifest.json', '原生命令历史需要独立的 agent 人工审查附件')
    pool._no_links(audit_path)
    raw = audit_path.read_bytes()
    _require(util.sha256_bytes(raw) == payload.get('native_audit_sha256'), '原生命令审查附件 SHA 不匹配')
    audit = json.loads(raw)
    expected = dict(version=1, auditor='codex_agent', no_background_or_async_actions=True,
        backup_manifest_sha256=util.sha256_bytes((backup / 'manifest.json').read_bytes()),
        native_log_sha256=proof['native_log_sha256'], ledger_sha256=proof['ledger_sha256'], commands=commands)
    _require(audit == expected, '原生命令逐条人工审查缺失、身份不符或清单不完整')
    proof.update(manual_command_audit=audit, manual_audit_sha256=util.sha256_bytes(raw),
                 attestation='agent_reviewed_history_no_background_or_external_async_actions')


def _native_stopped(row, path, job, payload):
    from . import process_owner
    _dead(row['owner_pid'])
    child = job.get('native_child')
    if child is not None:
        _require(isinstance(child, dict) and child.get('owner_pid') == row['owner_pid']
                 and child.get('workspace') == row['path']
                 and child.get('schema_path') == str(path.parent / 'output-schema.json')
                 and child.get('output_path') == str(path.parent / 'last-message.txt')
                 and type(child.get('pgid')) is int and child['pgid'] == child.get('pid')
                 and all(type(child.get(k)) is int for k in ('started_seconds', 'started_microseconds'))
                 and isinstance(child.get('owner_birth'), dict)
                 and all(type(child['owner_birth'].get(k)) is int for k in ('started_seconds', 'started_microseconds')),
                 '原生规划子进程检查点身份不符')
        current = process_owner.process_birth(child['pid'])
        _require(current is None, '原生规划子进程仍存活或 PID 已复用，未恢复')
        _dead(child['pid'])
    pool._no_links(path.parent / constants.PLANNER_CODEX_LOG_NAME)
    proof = _native_log_clear(path.parent / constants.PLANNER_CODEX_LOG_NAME, job.get('session_id'))
    _native_manual_audit(payload, proof)
    markers = (row['path'], str(path.parent / 'output-schema.json'), str(path.parent / 'last-message.txt'))
    processes = _native_processes()
    _require(not any(any(marker in command for marker in markers)
                     or child is not None and pgid == child['pgid']
                     for _pid, _ppid, pgid, _uid, command in processes),
             '仍有对应原生规划或进程组，未恢复')
    admin = Path(pool._git(Path(row['path']), 'rev-parse', '--git-dir').decode('utf-8').strip())
    if not admin.is_absolute(): admin = Path(row['path']) / admin
    proof['kernel_file_scan'] = _native_file_users(
        [Path(row['path']), path.parent, admin.resolve()],
        {pid for pid, _ppid, _pgid, uid, _command in processes if uid == os.geteuid()})
    proof.update(mechanism='pid_absence_kernel_snapshot_and_agent_history_review',
        scan='complete_local_ps_ww', owner='esrch', related_processes=0, unfinished_tools=0)
    return proof


def _native_evidence_backup(directory, backup_dir):
    root = Path(backup_dir)
    manifest = json.loads((root / 'manifest.json').read_bytes())
    saved = manifest.get('native_evidence') or {}
    files = {k: v for k, v in _file_set(directory).items() if k != '.settle.lock'}
    _require(all(name in files for name in ('job.json', 'events.jsonl', 'codex.jsonl', 'output-schema.json')),
             '原生规划证据缺失')
    for name, state in files.items():
        entry = saved.get(name) or {}
        destination = Path(entry.get('path') or '')
        _require(state[0] == 'file' and destination.is_absolute()
                 and destination.is_relative_to(root) and entry.get('sha256') == state[1],
                 '原生规划完整证据备份缺失或不匹配')
        pool._no_links(destination)
        _require(hashlib.sha256(destination.read_bytes()).hexdigest() == state[1],
                 '原生规划证据备份校验失败')
    return files


def _native_event_session(path, job):
    raw = path.read_bytes()
    _require(raw.endswith(b'\n'), '规划事件日志尾行不完整')
    events = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    _require(events and all(isinstance(e, dict) and type(e.get('seq')) is int and e['seq'] == n
             and all(e.get(k) in (None, job[k]) for k in ('task_id', 'request_id', 'run_id'))
             for n, e in enumerate(events, 1)) and not any(e.get('type') == 'terminal' for e in events),
             '规划事件日志不完整或已有待核对终态')
    sessions = {e.get('session_id') for e in events if e.get('type') == 'session' and e.get('session_id')}
    _require(len(sessions) == 1 and all(isinstance(value, str) for value in sessions),
             '规划事件缺少唯一会话身份')
    session = next(iter(sessions))
    _require(job.get('session_id') in (None, session), '规划检查点与事件会话不符')
    return session


def _native_preflight(conn, payload, row):
    from . import codex_planner, planner_runner, stage_contract
    _require(row['state'] == 'leased' and row['owner_state'] in ('running', 'unknown')
             and row['holder_stage'] == 'plan', '仅能恢复中断的 Codex 规划槽位')
    pool.validate_lease(conn, *(payload[k] for k in ('slot_id', 'holder_id', 'lease_token')))
    _require(not pool._unresolved_operation(conn, row), '槽位仍有关联未决操作')
    requests = conn.execute("SELECT * FROM requests WHERE run_id=? AND stage='plan'", (row['holder_id'],)).fetchall()
    _require(len(requests) == 1, '原生规划请求不唯一')
    request = requests[0]
    frozen = db.request_payload(request)
    _require(request['status'] in ('failed', 'cancelled') and frozen.get('planner_backend', 'codex') == 'codex'
             and all((frozen.get('pool_lease') or {}).get(k) == row[k] for k in _LEASE_KEYS),
             '原生规划请求尚未失败收尾或冻结租约不符')
    task = db.get_task(conn, request['task_id'])
    _require(task is not None and not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND stage='plan' "
                 "AND status IN ('queued','claimed') LIMIT 1", (request['task_id'],)).fetchone(),
             '任务缺失或已有活动规划请求，未恢复')
    path = headless_adapter.resolve_source(conn).run_dir(request['task_id'], row['holder_id']) / 'job.json'
    pool._no_links(path)
    job = json.loads(path.read_bytes())
    _require(isinstance(job, dict) and job.get('backend') == 'codex'
             and job.get('status') == 'running' and 'stage_result' not in job
             and job.get('pid') == row['owner_pid']
             and job.get('request_id') == request['request_id'] and job.get('task_id') == request['task_id']
             and job.get('run_id') == row['holder_id'] and job.get('worktree_path') == row['path']
             and all((job.get('pool_lease') or {}).get(k) == row[k] for k in _LEASE_KEYS),
             '中断规划检查点、终态或租约身份不符')
    contract = stage_contract.validate_contract(job.get('stage_contract'), expected={
        'stage': 'plan', 'harness_id': 'codex', 'task_id': request['task_id'],
        'request_id': request['request_id'], 'run_id': row['holder_id'], 'workspace': row['path']})
    context = dict(payload=frozen, input_id=frozen.get('input_id'), input_dir=contract['inputs'].get('input_dir'))
    prepared = planner_runner._prepare_stage_inputs(context, request['task_id'], request['request_id'], row['holder_id'], 'codex')
    _require(contract['config'] == prepared['config']
             and contract['allowed_actions'] == ['read_workspace', 'produce_plan']
             and contract['inputs'].get('execution_mode') is None
             and task['repository'] and str(Path(task['repository']).resolve()) == prepared['repository']
             and all(contract['inputs'].get(k) == v for k, v in prepared['inputs'].items())
             and (contract['inputs'].get('worktree_identity') or {}).get('pool') == {
                 'slot_id': row['slot_id'], 'holder_id': row['holder_id'],
                 'lease_sha256': util.sha256_text(row['lease_token'])},
             '原生规划契约输入或槽位绑定不符')
    pool._no_links(path.parent / 'output-schema.json')
    _require(json.loads((path.parent / 'output-schema.json').read_bytes()) == codex_planner.output_schema(),
             '原生规划输出契约已变化')
    for name in ('last-message.txt', 'plan-draft.md'):
        candidate = path.parent / name
        pool._no_links(candidate)
        _require(not candidate.exists() or candidate.is_file() and not candidate.read_bytes().strip(),
                 '原规划仍有待核对的完整输出，未作为中断回收')
    job['session_id'] = _native_event_session(path.parent / 'events.jsonl', job)
    proof = _native_stopped(row, path, job, payload)
    digest = _backup(row, payload['backup_dir'])
    # Native recovery additionally requires the administrative Git index.
    manifest = json.loads((Path(payload['backup_dir']) / 'manifest.json').read_bytes())
    entry = next(e for e in manifest['slots'] if e['slot_id'] == row['slot_id'])
    index = Path(pool._git(Path(row['path']), 'rev-parse', '--git-path', 'index').decode('utf-8').strip())
    if not index.is_absolute(): index = Path(row['path']) / index
    with tarfile.open(Path(payload['backup_dir']) / entry['archive'], 'r:gz') as archive:
        members = [m for m in archive if m.name == Path(row['path']).name + '-git-index']
        _require(len(members) == 1 and archive.extractfile(members[0]).read() == index.read_bytes(),
                 '槽位 Git 索引备份缺失或不一致')
    _native_evidence_backup(path.parent, payload['backup_dir'])
    result = dict(slot_id=row['slot_id'], holder_id=row['holder_id'], request_id=request['request_id'],
        path=row['path'], state='ready', recovery_kind='native_planner_interrupted',
        planner_result='unconfirmed', backup_sha256=digest, operation_id=payload['operation_id'],
        reason=payload['reason'], native_stop_proof=proof)
    return row, request, path, job, result


def _preflight(conn, payload):
    row = pool._owned(conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
    if row['holder_stage'] == 'plan':
        native = conn.execute("SELECT payload_json FROM requests WHERE run_id=? AND stage='plan'", (row['holder_id'],)).fetchall()
        if len(native) == 1 and (util.jload(native[0]['payload_json']) or {}).get('planner_backend', 'codex') == 'codex':
            return _native_preflight(conn, payload, row)
    _require(row["state"] == "leased" and row["owner_state"] == "unknown"
             and row["holder_stage"] in ("plan", "review"), "槽位不属于待人工恢复的未知旧租约")
    pool.validate_lease(conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
    _require(not pool._unresolved_operation(conn, row), "槽位仍有关联未决操作")
    _dead(row["owner_pid"])
    request, path, job = _checkpoint(conn, row)
    digest = _backup(row, payload["backup_dir"])
    result = {"slot_id": row["slot_id"], "holder_id": row["holder_id"],
              "request_id": request["request_id"], "path": row["path"],
              "state": "ready", "recovery_kind": "human_confirmed",
              "plus_terminal": "unknown", "backup_sha256": digest,
              "operation_id": payload["operation_id"], "reason": payload["reason"]}
    result["intent"] = {k: row[k] for k in
                        ("holder_stage", "owner_pid", "session_id", "turn_id", "sha")}
    result["intent"].update(task_id=request["task_id"], backup_dir=payload["backup_dir"])
    return row, request, path, job, result


def _recover_native(ctx, payload, scope, identity):
    from . import desktop_queue, planner_runner, task_workspace
    # Every preflight is repeated while holding the same per-run settlement lock.
    row, request, path, job, result = _preflight(ctx.conn, payload)
    with desktop_queue._serialized(path.parent):
        row, request, path, job, result = _preflight(ctx.conn, payload)
        frozen = dict(request)
        original = _native_evidence_backup(path.parent, payload['backup_dir'])
        with db.write_txn(ctx.conn):
            _require(db.get_runtime_operation(ctx.conn, scope, payload['operation_id']) is None
                     and not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                         "AND status IN ('prepared','unknown') LIMIT 1", (scope,)).fetchone(),
                     '槽位已有人工恢复回执，未重复操作')
            runtime_operations.begin_operation(ctx.conn, scope, payload['operation_id'], _HARNESS, _TOOL, identity)
            runtime_operations.complete_operation(ctx.conn, scope, payload['operation_id'], status='unknown', result=result)
        _require(_native_evidence_backup(path.parent, payload['backup_dir']) == original,
                 '原生规划证据在保全期间已变化')
        result['native_stop_proof'] = _native_stopped(row, path, job, payload)
        task_workspace.preserve_pool_artifacts(job, path.parent)
        current = db.get_request(ctx.conn, request['request_id'])
        _require(current is not None and dict(current) == frozen, '保全期间原生规划请求已变化')
        fresh = pool.validate_lease(ctx.conn, *(payload[k] for k in ('slot_id', 'holder_id', 'lease_token')))
        keys = ('owner_state', 'owner_pid', 'session_id', 'turn_id', 'sha')
        _require(all(fresh[k] == row[k] for k in keys), '保全期间原生规划租约已变化')
        _backup(fresh, payload['backup_dir'])
        result['native_stop_proof'] = _native_stopped(fresh, path, job, payload)
        job.update(status='failed', exit_code=None, ended_at=util.now_iso(),
            detail='native_planner_interrupted: 原生规划进程已退出，输出未完成；人工保全后释放槽位',
            native_interruption={'operation_id': payload['operation_id'], 'backup_dir': payload['backup_dir'],
                                 'stop_proof': result['native_stop_proof']})
        planner_runner._failed_stage_result(job, job['detail'], 'native_planner_interrupted')
        desktop_queue._write(path, job)
        planner_runner._EventLog(path.parent / 'events.jsonl').emit('terminal', util.now_iso(),
            outcome='failed', reason='native_planner_interrupted')
        released = pool.release(ctx.conn, *(payload[k] for k in ('slot_id', 'holder_id', 'lease_token')),
            owner_stopped=True, expected_owner={k: row[k] for k in keys}, expected_request=request)
        result.update(state=released['state'], reason=released['reason'] or payload['reason'])
        job.update(pool_release_state=released['state'], pool_released=released['state'] == 'free',
            pool_manual_recovery={'scope_id': scope, 'operation_id': payload['operation_id'],
                                  'planner_result': 'interrupted'})
        desktop_queue._write(path, job)
        with db.write_txn(ctx.conn):
            ctx.conn.execute("UPDATE execution_rounds SET run_status='failed',updated_at=? WHERE task_id=? AND run_id=?",
                             (util.now_iso(), request['task_id'], row['holder_id']))
            db.add_event(ctx.conn, util.now_iso(), 'native_planner_interrupted', task_id=request['task_id'],
                request_id=request['request_id'], actor='user', detail=result)
            runtime_operations.complete_operation(ctx.conn, scope, payload['operation_id'], result=result)
        return result


def cmd_worktree_slot_recover(ctx, payload):
    from . import desktop_queue, task_workspace

    _require(bool(payload["reason"].strip()), "人工恢复原因不能为空")
    scope = "manual-worktree-recovery:" + payload["slot_id"]
    operation_id = payload["operation_id"]
    identity = {k: payload[k] for k in
                ("slot_id", "holder_id", "lease_token", "operation_id", "backup_dir", "reason")}
    identity.update({k: payload[k] for k in ('native_audit_path', 'native_audit_sha256') if k in payload})
    existing = db.get_runtime_operation(ctx.conn, scope, operation_id)
    if existing is not None:
        _require(existing["request_sha256"] == runtime_operations.compute_request_sha256(identity)
                 and existing["tool"] == _TOOL and existing["harness_id"] == _HARNESS,
                 "人工恢复 operation_id 参数不匹配")
        if existing["status"] == "accepted":
            return util.jload(existing["result_json"])
        raise util.KanpilotError("conflict", "前次人工恢复结果尚未确认，保留现场，请先核对回执")
    _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                                 "AND status IN ('prepared','unknown') LIMIT 1", (scope,)).fetchone(),
             "该槽位已有未确认的人工恢复，保留现场")
    row, request, path, job, result = _preflight(ctx.conn, payload)
    if payload.get("dry_run", True):
        return result
    _require(payload.get("confirm") is True, "人工恢复需要 confirm=true")
    if result.get('recovery_kind') == 'native_planner_interrupted':
        return _recover_native(ctx, payload, scope, identity)
    with desktop_queue._serialized(path.parent):
        row, request, path, job, result = _preflight(ctx.conn, payload)
        frozen_payload = db.request_payload(request)
        with db.write_txn(ctx.conn):
            _require(not ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
                                         "AND status IN ('prepared','unknown') LIMIT 1", (scope,)).fetchone(),
                     "该槽位已有未确认的人工恢复，保留现场")
            _require(db.get_runtime_operation(ctx.conn, scope, operation_id) is None,
                     "人工恢复回执已变化，请重新核对")
            runtime_operations.begin_operation(ctx.conn, scope, operation_id, _HARNESS, _TOOL, identity)
            runtime_operations.complete_operation(ctx.conn, scope, operation_id,
                                                  status="unknown", result=result)
        # Never manufacture owner_stopped or a remote terminal observation.
        task_workspace.preserve_pool_artifacts(job, path.parent)
        desktop_queue._write(path, job)
        current = db.get_request(ctx.conn, request["request_id"])
        _require(current is not None and current["status"] in _TERMINAL
                 and current["run_id"] == row["holder_id"]
                 and current["stage"] == row["holder_stage"]
                 and current["task_id"] == request["task_id"]
                 and db.request_payload(current) == frozen_payload,
                 "保全期间原请求身份或冻结输入已变化")
        _dead(row["owner_pid"])
        fresh = pool.validate_lease(ctx.conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")))
        _require(all(fresh[k] == row[k] for k in
                     ("owner_state", "owner_pid", "session_id", "turn_id", "sha")),
                 "保全期间槽位持有者状态已变化")
        _backup(fresh, payload["backup_dir"])
        released = pool.release(ctx.conn, *(payload[k] for k in ("slot_id", "holder_id", "lease_token")),
                                owner_stopped=True,
                                expected_owner={k: row[k] for k in
                                                ("owner_state", "owner_pid", "session_id", "turn_id", "sha")},
                                expected_request=request)
        result["state"] = released["state"]
        result["reason"] = released["reason"] or payload["reason"]
        job.update(pool_release_state=released["state"], pool_released=released["state"] == "free",
                   pool_manual_recovery={"scope_id": scope, "operation_id": operation_id,
                                         "plus_terminal": "unknown"})
        desktop_queue._write(path, job)
        with db.write_txn(ctx.conn):
            runtime_operations.complete_operation(ctx.conn, scope, operation_id, result=result)
        return result
