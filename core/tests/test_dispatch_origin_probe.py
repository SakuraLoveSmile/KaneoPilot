"""PR dispatch rejects unsafe delivery configuration before executor start."""
from unittest.mock import patch

import pytest

from kanpilot import core, db, dispatch_validate, git_delivery
import test_dispatch_branch as branch_fixture
from test_git_delivery import git


@pytest.fixture
def dispatch_case():
    case = branch_fixture.DispatchBranch(methodName='runTest')
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize('targets', [
    ['git@github.com:other/repo.git'],
    ['git@github.com:owner/repo.git', 'git@github.com:owner/repo.git'],
])
def test_unsafe_push_target_blocks_dispatch_without_remote_write(dispatch_case, targets):
    case = dispatch_case
    for target in targets:
        git(case.repo, 'config', '--add', 'remote.origin.pushurl', target)
    case.enqueue_dispatch('task1234')
    result = case.call('claim', {'stage': 'dispatch', 'executor': 'kaneo-dispatch'})
    assert not result['claimed']
    assert '单一 GitHub 仓库' in result['detail']
    assert case.fake.tasks['task1234']['status'] == 'to-do'
    assert db.require_task(case.ctx.conn, 'task1234')['run_id'] is None
    assert db.require_task(case.ctx.conn, 'task1234')['status'] == 'needs_user'


def test_temporary_origin_probe_failure_keeps_dispatch_retryable(dispatch_case):
    case = dispatch_case
    case.enqueue_dispatch('task1234')
    with patch.object(git_delivery, 'require_github_origin',
                      side_effect=git_delivery.GitDeliveryError('git_probe_timeout', 'SSH 配置探测超时')):
        result = case.call('claim', {'stage': 'dispatch', 'executor': 'kaneo-dispatch'})
    assert not result['claimed']
    assert result['code'] == 'remote_unavailable'
    assert db.require_task(case.ctx.conn, 'task1234')['status'] == 'ready'
    assert case.fake.tasks['task1234']['status'] == 'to-do'


def test_manual_non_pr_dispatch_does_not_require_github_remote(dispatch_case):
    case = dispatch_case
    with db.write_txn(case.ctx.conn):
        task = db.require_task(case.ctx.conn, 'task1234')
        core.update_delivery(case.ctx.conn, task, case.ctx.now_iso, mode='manual')
        core.update_automation(case.ctx.conn, db.require_task(case.ctx.conn, 'task1234'),
                               case.ctx.now_iso, manual_takeover=True)
    with patch.object(git_delivery, 'require_github_origin', side_effect=AssertionError('manual origin probe')):
        assert case.validate()['task_id'] == 'task1234'
