"""Step 15-G: manual legacy execution through real runner/core boundaries.

The conftest Kaneo service is loopback only. Pi execution and Codex Review use
isolated fake executables; no installed model or production database is used.
"""
import json
import sys
import subprocess
import time
from pathlib import Path

import pytest

from kanpilot import auto_dispatch, auto_monitor, cli_review, constants, db


def stage(module, name, env):
    conn = db.connect(env.db)
    records = []
    try:
        rc = module.run_stage(name, conn=conn, db_path=env.db, timeout=180,
                              log_path=env.root / (name + '.log'),
                              record_run=lambda **fields: records.append(fields))
    finally:
        conn.close()
    assert rc == 0, records
    assert records[-1]['exit_code'] == 0


def monitor(module, env):
    # Only advance the scheduler's clock boundary, never task or result state.
    conn = db.connect(env.db)
    try:
        conn.execute("UPDATE requests SET not_before=NULL WHERE kind='monitor' AND status='queued'")
    finally:
        conn.close()
    stage(module, 'monitor', env)


# PR/发布授权字段全集：仅由 PR 发布/同步链路写入（cmd_delivery_pr 的
# pr_number/pr_url/pr_state/pushed_sha/merged_at/publish_failures/last_sync_at、
# cmd_queue delivery resolve 的 next_sync_at），manual 分支工作区任务永不应出现。
PR_PUBLISH_FIELDS = ("pr_url", "pr_number", "pr_state", "pushed_sha",
                     "merged_at", "publish_failures", "last_sync_at", "next_sync_at")


def assert_legacy(task, reviewed=False):
    """manual legacy 形态：无 automation、无 plan_verify，delivery 只是本地分支工作区。

    docs/state-model「新任务分支工作区（2026-10-09）」条款：首次规划（start-plans 经
    task_workspace.prepare_for_submission 落库）就登记 delivery=mode=manual +
    workspace_version=1 + branch/worktree/base/head 完整本地登记，这是符合规格的
    形态而非 PR 升级，不能再断言其不存在。阶段边界按 auto_monitor._finish_complete：
    验证通过才会执行 delivery-commit——done/评审通过前（reviewed=False）head_sha
    不得离开 base_sha、不得有 commits；done/评审通过后（reviewed=True）允许
    head_sha 与 commits 前进。两阶段都严禁 PR_PUBLISH_FIELDS 出现。
    """
    assert not task.get('automation')
    assert not task['extra'].get('automation')
    assert 'plan_verify' not in task['extra']
    delivery = task.get('delivery') or task['extra'].get('delivery') or {}
    assert delivery.get('mode') == 'manual', task
    assert delivery.get('workspace_version') == 1, task
    assert not [key for key in PR_PUBLISH_FIELDS if key in delivery], task
    if not reviewed:
        assert delivery.get('head_sha') == delivery.get('base_sha'), task
        assert not delivery.get('commits'), task


@pytest.fixture
def legacy_execution(factory, cli, tmp_env, monkeypatch):
    repo = tmp_env.repos_root / 'StudyTool'
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                 ('config', 'user.email', 'fixture@example.test')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    (repo / 'baseline.txt').write_text('legacy baseline\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'baseline'], check=True, capture_output=True)
    ready = factory.make_ready(title='Legacy manual execution without verify')
    tid = ready.task.task_id
    factory.enqueue_dispatch(tid)
    stage(auto_dispatch, 'dispatch', tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'running', task
    # Seed the pre-v5 persisted shape, rather than testing the new publisher's
    # empty/error declaration. This is fixture setup only; all later state
    # transitions must pass through the runner and core commands.
    extra = dict(task['extra'])
    extra.pop('plan_verify', None)
    conn = db.connect(tmp_env.db)
    try:
        conn.execute('UPDATE tasks SET extra_json=? WHERE task_id=?', (json.dumps(extra), tid))
    finally:
        conn.close()
    task = factory.get_task(tid)
    assert_legacy(task)
    # The real subprocess fake Pi worker records the round through core commands.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = cli('get-session', {'task_id': tid, 'run_id': task['run_id']}).get('round_detail') or {}
        if detail.get('terminal'):
            break
        time.sleep(.05)
    assert detail.get('terminal', {}).get('outcome') == 'completed', detail
    assert detail.get('exit_code') == 0, detail
    return tid, task['run_id']


def test_legacy_user_acceptance_reaches_real_codex_review(legacy_execution, factory, cli, tmp_env, monkeypatch):
    tid, execution_run = legacy_execution
    # 任务28B（配置时序）：review 请求按入队时冻结后端快照——db.create_request
    # 把 review_backend/review_config 写进请求 payload，进入 in_review 后再改配置
    # 不生效。codex fake 与 set-review-config 必须移到进入 in_review（第二次
    # monitor 推进）之前；该接口只改 role=review 独立配置，不触碰
    # planner/exec，规划与派发已在 legacy_execution 夹具内完成，用例开头即是
    # 不影响任何前段的最早安全点。
    codex = tmp_env.root / 'codex-review'
    codex.write_text('#!' + sys.executable + '\nimport json,pathlib,sys\n'
        'if "--version" in sys.argv: print("codex fake"); sys.exit(0)\n'
        'sys.stdin.read()\n'
        'out=sys.argv[sys.argv.index("-o")+1]\n'
        'pathlib.Path(out).write_text(json.dumps({"conclusion":"pass","report_markdown":"# Review\\nLegacy result accepted; code checked.","fix":None}))\n'
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))\n')
    codex.chmod(0o755)
    monkeypatch.setenv('KANPILOT_CODEX_BIN', str(codex))
    # CLI fixture subprocesses retain their isolated env snapshot.
    cli.env['KANPILOT_CODEX_BIN'] = str(codex)
    cli('set-review-config', {'backend': 'codex', 'codex': {'model':'gpt-6.1-sol', 'reasoning_effort':'medium'}})
    monitor(auto_monitor, tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'needs_user', task
    assert task['failure_code'] == 'verification_unconfirmed', task
    assert 'accept_verification' in task['allowed_actions']
    assert_legacy(task)
    cli('resolve', {'task_id': tid, 'action': 'accept_verification', 'note': 'User checked legacy result'})
    accepted = factory.get_task(tid)
    assert accepted['status'] == 'running'
    assert accepted['extra']['verification_override']['execution_run_id'] == execution_run
    # 任务28A（真实改动前提）：workspace_version=1 → auto_monitor._finish_complete
    # 验证通过后必须先 delivery-commit；隔离工作树无改动时 git_delivery.
    # commit_worktree 抛 no_changes，cmd_delivery_commit 返回 blocked 并阻塞 →
    # needs_user，进不了 in_review。参照 conftest make_in_review：在第二次
    # monitor 完成提交前，从权威 delivery.worktree_path 写一个非产物文件
    # （untracked 计真实改动：status --untracked-files=all）。
    delivery = accepted.get('delivery') or {}
    assert delivery.get('worktree_path'), delivery
    (Path(delivery['worktree_path']) / 'legacy-manual-result.py').write_text(
        'VALUE = 1\n', encoding='utf-8')
    monitor(auto_monitor, tmp_env)
    assert factory.get_task(tid)['status'] == 'in_review', factory.get_task(tid)
    stage(cli_review, 'review', tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'done', task
    assert task['extra']['review_conclusion'] == 'pass', task
    assert factory.kaneo.get_task(tid)['status'] == 'done'
    assert_legacy(task, reviewed=True)
    conn = db.connect(tmp_env.db)
    try:
        # schema 版本只引用核心权威常量 constants.SCHEMA_VERSION（不作硬编码
        # 字面量）：db._init_schema/_upgrade_schema 的收敛终点即该常量，
        # _UPGRADES 链 1→…→6 由提交 13713b9（worktree slots，_upgrade_v5_to_v6）
        # 推进到 6；后续核心再升版本时本断言随常量前进，不再重复漂移。
        assert conn.execute('SELECT version FROM schema_meta').fetchone()[0] \
            == constants.SCHEMA_VERSION
    finally:
        conn.close()


@pytest.mark.parametrize('current_override,exit_code,expected', [
    (False, 0, 'verification_unconfirmed'), (True, 2, 'execution_failed'),
    (True, None, 'execution_failed'),
])
def test_legacy_override_identity_and_execution_gate(legacy_execution, factory, cli, tmp_env,
                                                       monkeypatch, current_override, exit_code, expected):
    tid, execution_run = legacy_execution
    monitor(auto_monitor, tmp_env)
    cli('resolve', {'task_id': tid, 'action': 'accept_verification', 'note': 'User checked result'})
    # Fake observation boundary only. All monitor state changes, remote recheck,
    # report/advance/claim and the persisted override stay actual core commands.
    real_run = auto_dispatch.cli.run
    observed = []
    def observation(command, payload, db_path):
        result = real_run(command, payload, db_path)
        if command == 'get-task' and payload.get('task_id') == tid and not current_override:
            result['task']['extra']['verification_override']['execution_run_id'] = 'previous-execution'
        if command == 'get-session':
            observed.append(payload['run_id'])
            result['round_detail']['exit_code'] = exit_code
        return result
    monkeypatch.setattr(auto_dispatch.cli, 'run', observation)
    monitor(auto_monitor, tmp_env)
    task = factory.get_task(tid)
    assert task['status'] == 'needs_user', task
    assert task['failure_code'] == expected, task
    assert observed == [execution_run]
    assert factory.kaneo.get_task(tid)['status'] == 'in-progress'
    assert_legacy(task)
