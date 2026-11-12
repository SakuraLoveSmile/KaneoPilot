"""Native planning interruption recovery: disposable worktrees and kernel fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_worktree_pool import scene, acquire, stopped_checkpoint  # noqa: F401
from test_worktree_recovery import backup
from test_planner_errors import _RunnerCase
from kanpilot import (codex_planner, db, desktop_queue, pi_desktop_planner,
                     planner_runner, process_owner, task_workspace, util)
from kanpilot import worktree_pool as pool, worktree_recovery as recovery


def save_native_backup(case):
    if not case.backup.exists(): backup(case.backup, case.lease)
    manifest_path = case.backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = manifest['slots'][0]
    archive = case.backup / entry['archive']
    slot = Path(case.lease['path'])
    index = Path(pool._git(slot, 'rev-parse', '--git-path', 'index').decode().strip())
    with tarfile.open(archive, 'w:gz', dereference=False) as handle:
        handle.add(slot, arcname=slot.name)
        handle.add(index, arcname=slot.name + '-git-index')
    entry['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest['native_evidence'] = {}
    for path in case.directory.iterdir():
        if not path.is_file() or path.name == '.settle.lock': continue
        dest = case.backup / ('original-' + path.name)
        dest.write_bytes(path.read_bytes())
        manifest['native_evidence'][path.name] = dict(path=str(dest), sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
    manifest_path.write_text(util.jdump(manifest))
    if hasattr(case, 'payload'): save_native_audit(case)


def save_native_audit(case):
    ledger = recovery._native_log_clear(case.directory / 'codex.jsonl', 'native-thread')
    audit = dict(version=1, auditor='codex_agent', no_background_or_async_actions=True,
        backup_manifest_sha256=util.sha256_bytes((case.backup / 'manifest.json').read_bytes()),
        **{k: ledger[k] for k in ('native_log_sha256', 'ledger_sha256', 'commands')})
    path = case.backup / 'native-command-audit.json'
    path.write_text(util.jdump(audit))
    case.payload.update(native_audit_path=str(path), native_audit_sha256=util.sha256_bytes(path.read_bytes()))


@pytest.fixture
def native(scene, monkeypatch):
    repo, conn, root = scene
    lease = acquire(scene)
    request, directory, job = stopped_checkpoint(scene, lease, backend='codex', status='failed')
    job.update(status='running', send_state='unknown', session_id='native-thread')
    job.pop('owner_stopped')
    db.update_task(conn, 'task', util.now_iso(), repository=str(repo))
    manifest = dict(input_id='input-1', task=dict(task_id='task', repository=str(repo)))
    payload = dict(planner_backend='codex', pool_lease=lease, input_id='input-1', manifest=manifest,
                   planner_input_sha256=pi_desktop_planner.manifest_sha(manifest), project_snapshot={'repository': str(repo)})
    db.merge_request_payload(conn, request['request_id'], util.now_iso(), **payload)
    prepared = planner_runner._prepare_stage_inputs(dict(payload=payload, input_id='input-1'), 'task', request['request_id'], lease['holder_id'], 'codex')
    planner_runner._freeze_plan_contract(job, prepared, lease['path'])
    (directory / 'job.json').write_text(util.jdump(job))
    (directory / 'output-schema.json').write_text(util.jdump(codex_planner.output_schema()))
    events = [dict(seq=1, type='session', session_id='native-thread')]
    (directory / 'events.jsonl').write_text(''.join(util.jdump(e) + '\n' for e in events))
    raw = [dict(type='thread.started', thread_id='native-thread'), dict(type='turn.started'),
           dict(type='item.started', item=dict(type='command_execution', id='tool-1', command='pwd')),
           dict(type='item.completed', item=dict(type='command_execution', id='tool-1', command='pwd', status='completed', exit_code=0))]
    (directory / 'codex.jsonl').write_text(''.join(util.jdump(e) + '\n' for e in raw))
    case = SimpleNamespace(conn=conn, lease=lease, request=request, directory=directory,
        path=directory / 'job.json', job=job, backup=root.parent / 'native-backup',
        ctx=SimpleNamespace(conn=conn, db_path=root.parent / 'isolated.db'))
    save_native_backup(case)
    case.payload = {k: lease[k] for k in ('slot_id', 'holder_id', 'lease_token')}
    case.payload.update(operation_id='native-recovery-1', backup_dir=str(case.backup), reason='isolated interrupted planner')
    save_native_audit(case)
    monkeypatch.setattr(recovery, '_native_processes', lambda: [(os.getpid(), 1, os.getpgrp(), os.geteuid(), 'isolated test')])
    monkeypatch.setattr(recovery, '_native_file_users', lambda *args: 'isolated_kernel_scan')
    return case


def recover(case):
    return recovery.cmd_worktree_slot_recover(case.ctx, dict(case.payload, dry_run=False, confirm=True))


def test_native_preview_is_read_only_and_legacy_recovery_preserves_original_failed_result(native):
    before = native.path.read_bytes()
    changes = native.conn.total_changes
    result = recovery.cmd_worktree_slot_recover(native.ctx, native.payload)
    assert result['state'] == 'ready' and result['planner_result'] == 'unconfirmed'
    assert native.conn.total_changes == changes and native.path.read_bytes() == before
    assert not (native.directory / '.settle.lock').exists()
    result = recover(native)
    assert result['state'] == 'free'
    job = json.loads(native.path.read_bytes())
    assert job['status'] == 'failed' and job['exit_code'] is None
    assert job['stage_result']['outcome'] == 'failed'
    assert 'owner_stopped' not in job and job['send_state'] == 'unknown'
    assert (native.backup / 'original-job.json').read_bytes() == before
    assert db.get_request(native.conn, native.request['request_id'])['status'] == 'failed'
    assert native.lease['lease_token'] not in util.jdump(result)
    with patch.object(pool, 'release', side_effect=AssertionError('no second release')):
        assert recover(native) == result


@pytest.mark.parametrize('fault', ['live_owner', 'matching_cli', 'pending_tool', 'unknown_tool', 'broken_tail',
    'foreign_thread', 'foreign_event', 'active_request', 'contract', 'null_result', 'missing_backup', 'incomplete_index'])
def test_native_recovery_refuses_live_unknown_or_changed_identity(native, monkeypatch, fault):
    if fault == 'live_owner':
        native.job['pid'] = os.getpid()
        task_workspace.pool_owner(native.conn, native.job, state='running', pid=os.getpid())
        native.path.write_text(util.jdump(native.job))
    elif fault == 'matching_cli':
        monkeypatch.setattr(recovery, '_native_processes', lambda: [(os.getpid(), 1, 1, os.geteuid(), 'codex -C ' + native.lease['path'])])
    elif fault in ('pending_tool', 'unknown_tool', 'broken_tail', 'foreign_thread'):
        path = native.directory / 'codex.jsonl'
        raw = path.read_text()
        if fault == 'pending_tool': raw += util.jdump(dict(type='item.started', item=dict(id='pending', type='mcp_tool_call'))) + '\n'
        if fault == 'unknown_tool': raw += util.jdump(dict(type='item.started', item=dict(id='pending', type='future_tool'))) + '\n'
        if fault == 'broken_tail': raw += '{"type":'
        if fault == 'foreign_thread': raw = raw.replace('native-thread', 'other-thread')
        path.write_text(raw)
    elif fault == 'foreign_event':
        (native.directory / 'events.jsonl').write_text(util.jdump(dict(seq=1, type='session', session_id='native-thread', run_id='foreign')) + '\n')
    elif fault == 'active_request':
        db.update_request(native.conn, native.request['request_id'], util.now_iso(), status='claimed')
    elif fault == 'contract':
        native.job['stage_contract']['workspace'] = str(native.directory)
        native.path.write_text(util.jdump(native.job))
    elif fault == 'null_result':
        native.job['stage_result'] = None
        native.path.write_text(util.jdump(native.job))
    elif fault == 'missing_backup':
        (native.backup / 'original-codex.jsonl').unlink()
    elif fault == 'incomplete_index':
        # A normal slot archive alone deliberately lacks its administrative index.
        archive = native.backup / (Path(native.lease['path']).name + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as handle: handle.add(native.lease['path'], arcname=Path(native.lease['path']).name)
        manifest = json.loads((native.backup / 'manifest.json').read_bytes())
        manifest['slots'][0]['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (native.backup / 'manifest.json').write_text(util.jdump(manifest))
    with pytest.raises((util.KanpilotError, OSError)):
        recover(native)
    assert pool._owned(native.conn, native.lease['slot_id'], native.lease['holder_id'], native.lease['lease_token'])['state'] == 'leased'


@pytest.mark.parametrize('fault', ['active_child', 'reused_child', 'remaining_group'])
def test_recorded_child_identity_and_group_must_be_gone(native, monkeypatch, fault):
    native.job['native_child'] = dict(pid=2147483646, pgid=2147483646, owner_pid=native.job['pid'],
        started_seconds=1, started_microseconds=0, owner_birth=dict(started_seconds=1, started_microseconds=0),
        workspace=native.lease['path'], schema_path=str(native.directory / 'output-schema.json'),
        output_path=str(native.directory / 'last-message.txt'), argv_sha256='a' * 64)
    native.path.write_text(util.jdump(native.job))
    save_native_backup(native)
    if fault in ('active_child', 'reused_child'):
        monkeypatch.setattr(process_owner, 'process_birth', lambda pid: dict(started_seconds=1 if fault == 'active_child' else 2, started_microseconds=0))
    else:
        monkeypatch.setattr(recovery, '_native_processes', lambda: [(os.getpid(), 1, 2147483646, os.geteuid(), 'detached tool')])
    with pytest.raises(util.KanpilotError): recover(native)


def test_known_complete_native_diagnostics_are_allowed_but_unknown_stderr_is_not(native):
    path = native.directory / 'codex.jsonl'
    path.write_text('2026-11-12T08:00:00.000Z WARN codex_rollout::list: state db discrepancy during find_thread_path_by_id_str_in_subdir: falling_back\n' + path.read_text())
    assert recovery._native_log_clear(path, 'native-thread')['diagnostic_lines'] == 1
    path.write_text(path.read_text() + 'unexpected truncated stderr\n')
    with pytest.raises(util.KanpilotError): recovery._native_log_clear(path, 'native-thread')


def test_real_kernel_scan_ignores_own_lock_but_refuses_detached_process_cwd(tmp_path):
    root = tmp_path / 'slot'
    root.mkdir()
    with desktop_queue._serialized(root):
        assert recovery._native_file_users([root], {os.getpid()})
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=root, start_new_session=True)
        try:
            with pytest.raises(util.KanpilotError):
                recovery._native_file_users([root], {os.getpid(), proc.pid})
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def test_ps_failure_or_incomplete_output_refuses_scan():
    with patch.object(recovery.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 10)):
        with pytest.raises(util.KanpilotError): recovery._native_processes()


def test_ps_accepts_signed_system_uid_without_losing_process():
    raw = f'{os.getpid()} 1 {os.getpgrp()} {os.geteuid()} isolated\n2147483646 1 2147483646 -2 system\n'.encode()
    with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(stdout=raw, stderr=b'')):
        assert recovery._native_processes()[-1][3] == -2
    with patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(stdout=b'1 0 1 0 truncated\n', stderr=b'')):
        with pytest.raises(util.KanpilotError): recovery._native_processes()


def test_native_child_identity_is_persisted_before_stdin(tmp_path):
    proc = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE, start_new_session=True)
    try:
        job = dict(pid=os.getpid())
        planner_runner._checkpoint_native_child(proc, job, tmp_path, tmp_path, tmp_path / 'schema.json', tmp_path / 'out.txt', ['isolated'])
        saved = json.loads((tmp_path / 'job.json').read_bytes())
        assert saved['native_child']['pid'] == proc.pid
        assert saved['native_child']['pgid'] == proc.pid
        assert saved['native_child']['owner_birth'] == process_owner.process_birth(os.getpid())
        proc.stdin.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None: proc.terminate(); proc.wait(timeout=5)


@pytest.mark.parametrize('fault', ['missing', 'sha', 'ledger', 'omitted', 'duplicate', 'auditor', 'manifest'])
def test_manual_command_audit_is_explicit_complete_and_bound(native, fault):
    path = Path(native.payload['native_audit_path'])
    audit = json.loads(path.read_bytes())
    if fault == 'missing': native.payload.pop('native_audit_path')
    elif fault == 'sha': native.payload['native_audit_sha256'] = 'f' * 64
    else:
        if fault == 'ledger': audit['ledger_sha256'] = 'f' * 64
        if fault == 'omitted': audit['commands'] = []
        if fault == 'duplicate': audit['commands'] *= 2
        if fault == 'auditor': audit['auditor'] = 'human'
        if fault == 'manifest': audit['backup_manifest_sha256'] = 'f' * 64
        path.write_text(util.jdump(audit))
        native.payload['native_audit_sha256'] = util.sha256_bytes(path.read_bytes())
    with pytest.raises(util.KanpilotError): recover(native)
    assert pool._owned(native.conn, native.lease['slot_id'], native.lease['holder_id'], native.lease['lease_token'])['state'] == 'leased'


@pytest.mark.parametrize('kind', ['mcp_tool_call', 'tool_call', 'file_change', 'web_search', 'collab_tool_call'])
def test_completed_other_tools_cannot_be_overridden_by_manual_audit(native, kind):
    path = native.directory / 'codex.jsonl'
    path.write_text(path.read_text().replace('command_execution', kind))
    with pytest.raises(util.KanpilotError): recover(native)


@pytest.mark.parametrize('fault', ['command_changed', 'duplicate', 'missing_status', 'boolean_exit'])
def test_command_ledger_rejects_incomplete_or_changed_tool_records(native, fault):
    path = native.directory / 'codex.jsonl'
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    if fault == 'command_changed': lines[-1]['item']['command'] = 'other'
    if fault == 'duplicate': lines.append(lines[-1])
    if fault == 'missing_status': lines[-1]['item'].pop('status')
    if fault == 'boolean_exit': lines[-1]['item']['exit_code'] = False
    path.write_text(''.join(util.jdump(line) + '\n' for line in lines))
    with pytest.raises(util.KanpilotError): recovery._native_log_clear(path, 'native-thread')


def test_native_preservation_failure_keeps_unknown_receipt_and_blocks_replay(native):
    with patch.object(task_workspace, 'preserve_pool_artifacts', side_effect=OSError('isolated preservation failure')):
        with pytest.raises(OSError): recover(native)
    operation = db.get_runtime_operation(native.conn, 'manual-worktree-recovery:' + native.lease['slot_id'], native.payload['operation_id'])
    assert operation['status'] == 'unknown'
    assert pool._owned(native.conn, native.lease['slot_id'], native.lease['holder_id'], native.lease['lease_token'])['state'] == 'leased'
    with pytest.raises(util.KanpilotError): recover(native)


@pytest.mark.parametrize('fault', ['request', 'owner'])
def test_native_preservation_rechecks_request_and_owner_before_release(native, fault):
    def changed(*args):
        if fault == 'request': db.update_request(native.conn, native.request['request_id'], util.now_iso(), status='claimed')
        else: task_workspace.pool_owner(native.conn, native.job, state='running', pid=os.getpid())
    with patch.object(task_workspace, 'preserve_pool_artifacts', side_effect=changed), \
         patch.object(pool, 'release', side_effect=AssertionError('must not release changed identity')):
        with pytest.raises(util.KanpilotError): recover(native)
    assert pool._owned(native.conn, native.lease['slot_id'], native.lease['holder_id'], native.lease['lease_token'])['state'] == 'leased'


def test_real_kernel_scan_refuses_detached_open_file(tmp_path):
    root = tmp_path / 'slot'
    root.mkdir()
    target = root / 'held.txt'
    target.write_text('isolated')
    proc = subprocess.Popen([sys.executable, '-c',
        'import sys,time; f=open(sys.argv[1]); print("ready",flush=True); time.sleep(30)', str(target)],
        cwd=tmp_path, stdout=subprocess.PIPE, start_new_session=True)
    try:
        assert proc.stdout.readline() == b'ready\n'
        with pytest.raises(util.KanpilotError): recovery._native_file_users([root], {os.getpid(), proc.pid})
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class TestNativeCheckpointFailure(_RunnerCase):
    def test_checkpoint_persistence_failure_stops_actual_child_without_prompt(self):
        task, source_sha, req_sha = self._new_task('checkpoint-failure')
        self._queue(task, source_sha, req_sha)
        self._prepare_run()
        self._use_codex_stub('checkpoint', sleep_seconds=30)
        original_write, original_popen = planner_runner._write_json, subprocess.Popen
        children, writes = [], []
        def popen(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            if kwargs.get('stdin') is subprocess.PIPE:
                children.append(proc)
                stream = proc.stdin
                class Stdin:
                    def write(self, value): writes.append(value); return stream.write(value)
                    def close(self): return stream.close()
                proc.stdin = Stdin()
            return proc
        def write(path, job):
            if job.get('status') == 'running' and job.get('native_child'):
                raise OSError('isolated checkpoint persistence failure')
            return original_write(path, job)
        try:
            with patch.object(planner_runner.subprocess, 'Popen', side_effect=popen), \
                 patch.object(planner_runner, '_write_json', side_effect=write):
                self.assertTrue(planner_runner.run_one(self.db_path))
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(writes, [])
            jobs = list(self.runs_root.glob('*/*/job.json'))
            job = json.loads(jobs[0].read_bytes())
            self.assertEqual(job['send_state'], 'not_sent')
            self.assertEqual(job['status'], 'failed')
        finally:
            for proc in children:
                if proc.poll() is None: proc.terminate(); proc.wait(timeout=5)
