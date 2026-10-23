"""Description-only repair: isolated SQLite and GitHub, no real network."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from kanpilot import cli, db, runtime_operations, util
from kanpilot import release_delivery as delivery, release_notes as notes
import test_release_delivery as lifecycle


@pytest.fixture
def case():
    value = lifecycle.ReleaseLifecycleTest('test_unknown_recovers_unique_marker_and_never_resends')
    value.setUp()
    try:
        result = value.start()
        value.batch = delivery._batch(value.conn, result['release']['batch_id'])
        value.api.published = True
        value.remote = value.api.release(value.batch['repository'], value.batch['tag'])
        value.marker = notes.owner_marker(value.batch)
        value.remote['body'] = value.marker + '\n\nOld notes'
        value.tag = dict(sha='e' * 40, tag=value.batch['tag'], message=value.marker,
                         object=dict(type='commit', sha=value.batch['target_sha']))
        value.writes = []
        value.lose_reply = False
        value.change_assets = False
        value.request = dict(task_id='parent', batch_id=value.batch['batch_id'],
                             expected_context_sha256=value.batch['context_sha256'], release_id=10,
                             expected_body_sha256=util.sha256_text(value.remote['body']),
                             notes='## 更新内容\n\n修复歌词文件被识别为音频的问题。', confirm=True)

        def patch_body(repo, release_id, body):
            row = db.get_runtime_operation(value.conn, delivery._scope('project'), notes.operation_id(value.request))
            assert row['status'] == 'unknown'  # receipt must precede the external write
            assert not value.conn.in_transaction
            value.writes.append((repo, release_id, body))
            value.remote['body'] = body
            if value.change_assets:
                value.remote['assets'][0]['digest'] = 'sha256:' + '0' * 64
            if value.lose_reply:
                raise OSError('lost response')

        with patch.object(notes.github, 'release', lambda *args: deepcopy(value.remote)), \
             patch.object(notes.github, 'release_tag', lambda *args: deepcopy(value.tag)), \
             patch.object(notes.github, 'patch_release_body', patch_body):
            yield value
    finally:
        value.doCleanups()


def call(case, **changes):
    return notes.cmd_repair_release_notes(case.ctx, dict(case.request, **changes))


def test_body_only_write_has_receipt_and_preserves_batch_packages(case):
    before = deepcopy(case.batch)
    result = call(case)
    assert result['outcome'] == 'accepted'
    assert case.remote['body'] == '<!-- ' + case.marker + ' -->\n\n' + case.request['notes']
    assert len(case.writes) == 1
    assert delivery._batch(case.conn, case.batch['batch_id']) == before
    row = db.get_runtime_operation(case.conn, delivery._scope('project'), notes.operation_id(case.request))
    audit = util.jload(row['result_json'])
    assert row['status'] == 'accepted' and audit['old_body'] == case.marker + '\n\nOld notes'
    assert audit['new_body'] == case.remote['body']
    assert call(case)['outcome'] == 'accepted' and len(case.writes) == 1


def test_lost_reply_only_reconciles_without_second_patch(case):
    case.lose_reply = True
    assert call(case)['outcome'] == 'unknown'
    assert call(case)['outcome'] == 'accepted'
    assert len(case.writes) == 1


def test_late_writer_failure_cannot_downgrade_an_observed_acceptance(case):
    original = notes.github.patch_release_body

    def accepted_before_late_failure(*args):
        original(*args)
        assert call(case)['outcome'] == 'accepted'
        raise OSError('late lost response')

    with patch.object(notes.github, 'patch_release_body', accepted_before_late_failure):
        assert call(case)['outcome'] == 'accepted'
    row = db.get_runtime_operation(case.conn, delivery._scope('project'), notes.operation_id(case.request))
    assert row['status'] == 'accepted'
    assert util.jload(row['result_json'])['resolution_source'] == 'read_only_observation'
    assert len(case.writes) == 1


@pytest.mark.parametrize('change', ['context', 'release', 'old_body', 'owner', 'tag', 'duplicate_owner'])
def test_identity_or_drift_refuses_before_mutation(case, change):
    payload = {}
    if change == 'context': payload['expected_context_sha256'] = '0' * 64
    elif change == 'release': payload['release_id'] = 11
    elif change == 'old_body': payload['expected_body_sha256'] = '0' * 64
    elif change == 'owner': case.remote['body'] = case.remote['body'].replace('"workflow_run_id":100', '"workflow_run_id":101')
    elif change == 'tag': case.tag['object']['sha'] = '0' * 40
    else: case.remote['body'] += '\n' + case.marker
    with pytest.raises(delivery.ReleaseBlocked): call(case, **payload)
    assert case.writes == []


def test_hidden_existing_owner_is_compatible(case):
    case.remote['body'] = '<!-- ' + case.marker + ' -->\n\nExisting custom text'
    case.request['expected_body_sha256'] = util.sha256_text(case.remote['body'])
    assert call(case)['outcome'] == 'accepted'


def test_unknown_with_different_parameters_cannot_send(case):
    case.lose_reply = True
    assert call(case)['outcome'] == 'unknown'
    with pytest.raises((delivery.ReleaseBlocked, util.KanpilotError)):
        call(case, notes='different notes')
    with pytest.raises(delivery.ReleaseBlocked):
        call(case, expected_body_sha256=util.sha256_text(case.remote['body']))
    assert len(case.writes) == 1


def test_assets_changed_after_patch_cannot_be_reported_as_success(case):
    case.change_assets = True
    assert call(case)['outcome'] == 'unknown'
    assert call(case)['outcome'] == 'unknown'
    assert len(case.writes) == 1


@pytest.mark.parametrize('text', ['', '  ', 'kaneopilot-owner:bad', 'text\x00text'])
def test_bad_user_notes_do_not_send(case, text):
    with pytest.raises(delivery.ReleaseBlocked): call(case, notes=text)
    assert case.writes == []


def test_cli_schema_is_fixed(case):
    cli.validate_payload('repair-release-notes', case.request)
    with pytest.raises(util.KanpilotError):
        cli.validate_payload('repair-release-notes', dict(case.request, replace_assets=True))


def test_transport_patches_only_body_once_without_read_retry():
    from pathlib import Path
    import json
    from kanpilot import gh_client, release_github
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert args[0:2] == ['api', 'repos/owner/repo/releases/10']
        assert args[args.index('--method') + 1] == 'PATCH'
        assert json.loads(Path(args[args.index('--input') + 1]).read_text()) == dict(body='中文\n`literal` $(literal)')
        assert kwargs.get('read_only') is not True
        raise gh_client.GhError('transient', 'EOF')

    with patch.object(gh_client, '_run', run), pytest.raises(gh_client.GhError):
        release_github.patch_release_body('owner/repo', 10, '中文\n`literal` $(literal)')
    assert len(calls) == 1
