"""Release checkpoint selection with isolated Git, SQLite and service fixtures."""
from unittest.mock import patch

import pytest

from kanpilot import core, db, git_delivery, release_delivery as release
from test_git_delivery import git
import test_release_delivery as lifecycle


@pytest.fixture
def case():
    value = lifecycle.ReleaseLifecycleTest('test_unknown_recovers_unique_marker_and_never_resends')
    value.setUp()
    try:
        f = value.fixture
        (f.repo / 'release').mkdir()
        for name in ('latest', 'preview'):
            (f.repo / 'release' / (name + '.json')).write_text('{"tag_name":"v0.1.0"}\n')
        git(f.repo, 'add', '.')
        git(f.repo, 'commit', '-m', 'existing channel feeds')
        f.add_task('fix3', commands=f.commands)
        core.update_delivery(value.conn, value.row('fix3'), value.ctx.now_iso,
            mode='pr', pr_number=3, pr_url='https://github.com/example/closeout/pull/3',
            merged_at=value.ctx.now_iso)
        value.verified = git(f.repo, 'rev-parse', 'HEAD')
        value.source_sha = git_delivery.tree_source_sha256(f.repo, value.verified)
        f.tip = value.verified

        def commit(repo, ref):
            result = f.commit(repo, ref)
            result['records'] = []
            for entry in git(f.repo, 'ls-tree', '-rz', result['sha']).split('\0'):
                if entry:
                    meta, path = entry.split('\t', 1)
                    mode, kind, blob = meta.split()
                    assert kind == 'blob'
                    result['records'].append(dict(path=path, mode=mode, blob=blob))
            return result

        with patch('kanpilot.parent_closeout_github.commit', side_effect=commit):
            yield value
    finally:
        value.doCleanups()


def advance(case, *paths):
    for relative in paths:
        path = case.fixture.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"tag_name":"v0.1.1"}\n')
    git(case.fixture.repo, 'add', '.')
    git(case.fixture.repo, 'commit', '-m', 'advance target branch')
    case.fixture.tip = git(case.fixture.repo, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('paths', [('release/preview.json',),
                                   ('release/latest.json', 'release/preview.json')])
def test_feed_only_change_builds_exact_verified_tree_without_new_verification(case, paths):
    advance(case, *paths)
    before = list(case.conn.iterdump())
    prepared = case.prepare('fix3')
    assert prepared['release']['can_start'], prepared
    assert prepared['release']['target_sha'] == case.verified
    assert list(case.conn.iterdump()) == before  # preparation remains read-only
    context = release._prepare(case.ctx, 'fix3')
    assert context['proof']['target_source_sha256'] == case.source_sha
    result = release.cmd_start_task_release(case.ctx, dict(task_id='fix3',
        expected_context_sha256=prepared['context_sha256']))
    assert result['release']['state'] == 'queued', result
    assert len(case.api.sent) == 1
    assert case.api.sent[0]['target_sha'] == case.verified
    assert case.fixture.tip != case.verified
    case.api.published = True
    assert case.sync(result['release']['batch_id'])['release']['state'] == 'published'
    assert release.completion_gate(case.conn, case.row('fix3')) is None


def test_checkpoint_stays_stable_when_feeds_advance_and_workflow_ref_is_unchanged(case):
    advance(case, 'release/preview.json')
    batch = release._reserve(case.ctx, release._prepare(case.ctx, 'fix3'))
    advance(case, 'release/latest.json')
    assert release._software_proof(case.conn, case.row('fix3')) == batch['proof']
    result = release._dispatch_reserved(case.ctx, batch, case.row('fix3'))
    assert result['release']['state'] == 'queued', result
    assert len(case.api.sent) == 1 and case.api.sent[0]['target_sha'] == case.verified


def test_workflow_head_drift_still_blocks_frozen_batch_before_send(case):
    advance(case, 'release/preview.json')
    batch = release._reserve(case.ctx, release._prepare(case.ctx, 'fix3'))
    advance(case, 'release/latest.json')
    assert release._software_proof(case.conn, case.row('fix3')) == batch['proof']
    current_workflow = dict(batch['workflow'], head_sha=case.fixture.tip)
    with patch.object(case.api, 'workflow_snapshot', return_value=current_workflow):
        result = release._dispatch_reserved(case.ctx, batch, case.row('fix3'))
    assert result['release']['state'] == 'blocked'
    assert result['release']['blockers'][0]['code'] == 'release_context_changed'
    assert case.api.sent == []
    assert release._batch(case.conn, batch['batch_id'])['dispatch_status'] == 'not_sent'
    assert db.get_runtime_operation(case.conn, release._scope('project'), 'dispatch:' + batch['batch_id']) is None


@pytest.mark.parametrize('path', ['lib/app.dart', 'release/other.json', '.github/workflows/release.yml',
                                 '.github/scripts/pilot_release.py', 'pubspec.yaml'])
def test_any_other_source_change_still_requires_supplemental_verification(case, path):
    advance(case, 'release/preview.json', path)
    result = case.prepare('fix3')
    assert result['context_sha256'] is None
    assert result['release']['blockers'][0]['code'] == 'release_target_unverified'
    assert case.api.sent == []


@pytest.mark.parametrize('change', ['add', 'delete', 'executable', 'symlink'])
def test_feed_file_identity_changes_cannot_select_an_old_checkpoint(case, change):
    path = case.fixture.repo / 'release/preview.json'
    if change == 'add':
        path = case.fixture.repo / 'release/latest.json'
        # A new allowlisted feed in the target is still not an ordinary update.
        git(case.fixture.repo, 'rm', str(path))
        git(case.fixture.repo, 'commit', '-m', 'remove feed before formal verification')
        case.fixture.add_task('fix4', commands=case.fixture.commands)
        core.update_delivery(case.conn, case.row('fix4'), case.ctx.now_iso,
            mode='pr', pr_number=4, pr_url='https://github.com/example/closeout/pull/4',
            merged_at=case.ctx.now_iso)
        task_id = 'fix4'
        path.write_text('{}')
    else:
        task_id = 'fix3'
        if change == 'delete':
            path.unlink()
        elif change == 'executable':
            path.chmod(0o755)
        else:
            path.unlink()
            path.symlink_to('latest.json')
    git(case.fixture.repo, 'add', '.')
    git(case.fixture.repo, 'commit', '-m', 'change feed identity')
    case.fixture.tip = git(case.fixture.repo, 'rev-parse', 'HEAD')
    assert case.prepare(task_id)['release']['state'] == 'blocked'
    assert case.api.sent == []


def test_source_drift_after_reservation_never_sends(case):
    advance(case, 'release/preview.json')
    batch = release._reserve(case.ctx, release._prepare(case.ctx, 'fix3'))
    advance(case, 'lib/app.dart')
    result = release._dispatch_reserved(case.ctx, batch, case.row('fix3'))
    assert result['release']['state'] == 'blocked'
    assert case.api.sent == []
    assert release._batch(case.conn, batch['batch_id'])['dispatch_status'] == 'not_sent'
    assert db.get_runtime_operation(case.conn, release._scope('project'), 'dispatch:' + batch['batch_id']) is None


def test_existing_supplemental_reference_retains_its_exact_target_proof(case):
    advance(case, 'release/preview.json')
    db.set_extra(case.conn, case.row('fix3'), case.ctx.now_iso,
        release_verification_ref=dict(verification_id='existing', context_sha256='frozen'))
    with patch('kanpilot.release_verification.verified_proof', return_value=None) as verified:
        assert case.prepare('fix3')['release']['state'] == 'blocked'
    verified.assert_called_once()
    assert case.api.sent == []


def test_explicit_target_and_supplemental_preparation_do_not_select_old_head(case):
    advance(case, 'release/preview.json')
    with pytest.raises(release.ReleaseBlocked, match='发布目标不是本轮已验证的完整源码'):
        release._single_proof(case.conn, case.row('fix3'), frozen_target=case.fixture.tip)
    proof = release._single_proof(case.conn, case.row('fix3'), allow_unverified_target=True)
    assert proof['target_sha'] == case.fixture.tip
    assert proof['target_source_sha256'] != case.source_sha


@pytest.mark.parametrize('bad', ['missing_records', 'wrong_source', 'wrong_sha'])
def test_incomplete_or_mismatched_head_snapshot_never_reuses_verification(case, bad):
    advance(case, 'release/preview.json')
    original = release.closeout.github.commit.side_effect
    def changed(repo, ref):
        result = original(repo, ref)
        if ref == case.verified:
            if bad == 'missing_records':
                result.pop('records')
            elif bad == 'wrong_source':
                result['source_sha256'] = '0' * 64
            else:
                result['sha'] = '0' * 40
        return result
    with patch('kanpilot.parent_closeout_github.commit', side_effect=changed):
        result = case.prepare('fix3')
    assert result['context_sha256'] is None
    assert case.api.sent == []
