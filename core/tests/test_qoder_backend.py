"""Qoder planning and Review through isolated core state and real fake processes."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from helpers import KanpilotTestCase
from test_cli_review import runner_scene  # noqa: F401 - shared isolated fixture
from kanpilot import (cli, cli_review, constants, db, planner_config, planner_runner,
                       qoder_cli, review_config, util)


FLAGS = ('--print --output-format --tools --permission-mode --strict-mcp-config '
         '--setting-sources --settings --no-session-persistence --model '
         '--reasoning-effort --add-dir --max-model-request-retries')
CUSTOM_MODEL = 'qoder-custom-fixture/deepseek/deepseek-v4.1-flash'
PROVIDER_SECRET = 'fake-provider-secret-123'


def custom_settings(root):
    config_dir = root / 'qoder-config'
    config_dir.mkdir()
    settings = config_dir / 'settings.json'
    settings.write_text(json.dumps({'providers': {'qoder-custom-fixture': {
        'type': 'openai-compatible', 'protocol': 'openai', 'authType': 'bearer',
        'baseUrl': 'https://fixture.invalid/v1', 'apiKey': PROVIDER_SECRET,
        'model': 'another-model', 'routing': {'default': 'another-model'},
        'models': [{'model': 'deepseek/deepseek-v4.1-flash',
                    'displayName': 'DeepSeek V4.1 Flash', 'contextWindow': 1000000,
                    'capabilities': {'thinking': {'modes': ['enabled'], 'supportsEffort': True}}},
                   {'model': 'another-model'}]},
        'other-provider': {'apiKey': 'unselected-secret', 'model': 'unused'}},
        'hooks': {'anything': 'must-not-run'}, 'mcpServers': {'unused': {'command': 'false'}}}))
    return settings


def fake_qoder(root, output, *, exit_code=0):
    """A CLI-shaped executable, with results persisted outside the reviewed tree."""
    binary = root / 'qoder-fake'
    binary.write_text('#!' + sys.executable + '\n' +
        'import json, os, sys\nfrom pathlib import Path\n' +
        "if '--version' in sys.argv: print('1.1.74'); sys.exit(0)\n" +
        "if '--help' in sys.argv: print(" + repr(FLAGS) + '); sys.exit(0)\n' +
        'observed={"cwd":os.getcwd(),"argv":sys.argv[1:],"prompt":sys.stdin.read(),'
        '"selected_key_received":os.environ.get("KANPILOT_QODER_PROVIDER_API_KEY")=='+repr(PROVIDER_SECRET)+'}\n' +
        'Path(' + repr(str(root / 'observed.json')) + ').write_text(json.dumps(observed))\n' +
        "print(json.dumps({'type':'system','subtype':'init','tools':['Read','Grep','Glob'],"
        "'permissionMode':'dontAsk','plugins':[],'skills':[],'mcp_servers':[],"
        "'session_id':'qoder-session'}),flush=True)\n" +
        'print(' + repr(json.dumps({'type': 'result', 'subtype': 'success',
            'is_error': False, 'result': json.dumps(output, ensure_ascii=False),
            'session_id': 'qoder-session', 'total_credits': 3})) + ',flush=True)\n' +
        'sys.exit(' + str(exit_code) + ')\n')
    binary.chmod(0o755)
    return binary


class QoderBackendTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo('Feedback')
        self.task_id = 'taskqoder000000000000001'
        self.project_id = 'ryryo1reph1xfemqtwkjyiiz'
        self.call('set-project-mapping', {'project_id': self.project_id,
            'repository': str(self.repo), 'enabled': True, 'expected_revision': 0})
        self.call('set-planner-config', {'backend': 'qoder'})
        with db.connect(self.db_path) as conn:
            db.set_setting(conn, 'session_runs_root', str(self.runs_root))

    def prepare(self, status='ready', *, output=None, exit_code=0):
        self.fake.add_task(self.task_id, self.project_id, status='to-do', title='Qoder 规划')
        info = self.call('prepare-plan-input', {'task_id': self.task_id})
        entry = self.call('start-plans', {'items': [{'input_id': info['input_id'],
            'input_sha256': info['input_sha256'], 'planner_backend': 'qoder'}]})['results'][0]
        self.assertTrue(entry.get('request_id'), entry)
        result = output if output is not None else {'status': status,
            'plan_markdown': '# Qoder 计划\n\n修改入口\n\n```verify\npython3 -m unittest\n```\n',
            'questions': ['确认目标行为'] if status == 'needs_input' else [], 'evidence': []}
        binary = fake_qoder(self.root, result, exit_code=exit_code)
        patch = mock.patch.dict(os.environ, {'KANPILOT_QODER_BIN': str(binary)})
        patch.start()
        self.addCleanup(patch.stop)
        return entry['request_id']

    def test_configuration_is_offline_and_preserves_other_backends(self):
        before = self.call('get-planner-config')['config']
        self.assertEqual(before['qoder'], {'model': 'auto', 'reasoning_effort': 'inherit'})
        self.call('set-planner-config', {'backend': 'codex'})
        after = self.call('set-planner-config', {'backend': 'qoder',
            'qoder': {'model': 'performance', 'reasoning_effort': 'high'}})['config']
        self.assertEqual(after['standard'], before['standard'])
        self.assertEqual(after['claude'], before['claude'])
        for block in ({'model': '-bad', 'reasoning_effort': 'high'},
                      {'model': 'auto', 'reasoning_effort': 'ultra'},
                      {'model': 'auto', 'reasoning_effort': 'inherit', 'tools': 'Bash'}):
            with self.subTest(block=block):
                self.call_err('set-planner-config', {'qoder': block})
                self.assertEqual(self.call('get-planner-config')['config'], after)
        self.assertIn('builtin', self.call_err('set-planner-config',
                                               {'default_method': 'spec_plan'}).message)

    def test_publishes_frozen_model_cost_and_readable_session(self):
        request_id = self.prepare()
        self.call('set-planner-config', {'qoder': {'model': 'ultimate', 'reasoning_effort': 'max'}})
        with mock.patch.object(cli, 'run', wraps=cli.run) as calls:
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        costs = [c.args[1] for c in calls.call_args_list if c.args[0] == 'record-cost']
        self.assertEqual([(c['source'], c['cost_usd']) for c in costs], [('qoder', None)])
        task = self.call('get-task', {'task_id': self.task_id})['task']
        self.assertEqual(task['status'], 'ready')
        meta, _ = util.read_frontmatter(Path(task['plan_path']))
        self.assertEqual((meta['planner_model'], meta['reasoning_effort']), ('auto', 'inherit'))
        observed = json.loads((self.root / 'observed.json').read_text())
        self.assertEqual(observed['argv'][observed['argv'].index('--model') + 1], 'auto')
        self.assertNotIn('--reasoning-effort', observed['argv'])
        self.assertNotEqual(Path(observed['cwd']), self.repo)
        with db.connect(self.db_path) as conn:
            request = db.get_request(conn, request_id)
            run_id = request['run_id']
            row = conn.execute('SELECT * FROM execution_rounds WHERE run_id=?', (run_id,)).fetchone()
            self.assertEqual(row['backend'], 'qoder')
            self.assertEqual(json.loads(row['config_json'])['planner']['planner_model'], 'auto')
            self.assertEqual(conn.execute('SELECT state FROM worktree_slots').fetchone()['state'], 'free')
        session = self.call('get-session', {'task_id': self.task_id, 'run_id': run_id})
        self.assertNotEqual(session.get('status'), 'unavailable')
        self.assertEqual(json.loads((self.runs_root / self.task_id / run_id / 'job.json').read_text())['backend'], 'qoder')

    def test_needs_input_keeps_draft_and_questions(self):
        request_id = self.prepare('needs_input')
        self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        task = self.call('get-task', {'task_id': self.task_id})['task']
        self.assertEqual(task['status'], 'needs_user')
        self.assertTrue(list(self.runs_root.rglob('plan-draft.md')))

    def test_custom_planner_projects_provider_and_never_persists_key(self):
        settings = custom_settings(self.root)
        original = settings.read_bytes()
        with mock.patch.dict(os.environ, {'QODER_CONFIG_DIR': str(settings.parent)}):
            self.call('set-planner-config', {'qoder': {'model': CUSTOM_MODEL, 'reasoning_effort': 'inherit'}})
            request_id = self.prepare()
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        self.assertEqual(self.call('get-task', {'task_id': self.task_id})['task']['status'], 'ready')
        observed = json.loads((self.root / 'observed.json').read_text())
        self.assertTrue(observed['selected_key_received'])
        self.assertEqual(observed['argv'][observed['argv'].index('--model') + 1], CUSTOM_MODEL)
        projected = json.loads(observed['argv'][observed['argv'].index('--settings') + 1])
        self.assertEqual(set(projected['providers']), {'qoder-custom-fixture'})
        selected = projected['providers']['qoder-custom-fixture']
        self.assertEqual(selected['model'], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(len(selected['models']), 1)
        self.assertTrue(selected['models'][0]['capabilities']['thinking']['supportsEffort'])
        self.assertNotIn('routing', selected)
        self.assertEqual(settings.read_bytes(), original)
        for path in self.runs_root.rglob('*'):
            if path.is_file():
                self.assertNotIn(PROVIDER_SECRET.encode(), path.read_bytes(), str(path))
        self.assertNotIn(PROVIDER_SECRET, json.dumps(observed))

    def test_missing_custom_provider_fails_before_launch_and_releases_slot(self):
        empty = self.root / 'empty-qoder-config'
        empty.mkdir()
        with mock.patch.dict(os.environ, {'QODER_CONFIG_DIR': str(empty)}):
            self.call('set-planner-config', {'qoder': {'model': CUSTOM_MODEL, 'reasoning_effort': 'inherit'}})
            request_id = self.prepare()
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        self.assertFalse((self.root / 'observed.json').exists())
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_request(conn, request_id)['status'], 'failed')
            self.assertEqual(conn.execute('SELECT state FROM worktree_slots').fetchone()['state'], 'free')

    def test_missing_cli_fails_without_publishing_or_holding_slot(self):
        request_id = self.prepare()
        with mock.patch.dict(os.environ, {'KANPILOT_QODER_BIN': str(self.root / 'missing')}):
            self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_request(conn, request_id)['status'], 'failed')
            self.assertIsNone(conn.execute("SELECT 1 FROM worktree_slots WHERE state='leased'").fetchone())
        self.assertFalse((self.root / 'observed.json').exists())

    def test_nonzero_exit_does_not_publish_successful_looking_result(self):
        request_id = self.prepare(exit_code=1)
        self.assertTrue(planner_runner.run_one(self.db_path, request_id))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.get_request(conn, request_id)['status'], 'failed')
        self.assertNotEqual(self.call('get-task', {'task_id': self.task_id})['task']['status'], 'ready')

    def test_review_queue_keeps_its_model_after_settings_change(self):
        self.call('set-review-config', {'backend': 'qoder',
            'qoder': {'model': 'performance', 'reasoning_effort': 'high'}})
        with db.connect(self.db_path) as conn:
            frozen = review_config.queue_snapshot(conn)
        self.call('set-review-config', {'backend': 'claude'})
        with db.connect(self.db_path) as conn:
            self.assertEqual(review_config.snapshot(conn, config_override=review_config.for_request(
                conn, {'review_config': frozen})),
                {'backend': 'qoder', 'model': 'performance', 'reasoning_effort': 'high'})


@pytest.mark.parametrize('conclusion', ['pass', 'changes_requested', 'blocked'])
def test_review_real_fake_cli_settles_only_a_valid_result(runner_scene, tmp_path, monkeypatch, conclusion):
    conn, task, calls = runner_scene
    fix = {'title': '修复问题', 'description': '修复审查指出的问题', 'priority': 'medium'} if conclusion == 'changes_requested' else None
    # Priority values use the shared review contract, not Qoder-specific rules.
    if fix:
        fix['priority'] = constants.REVIEW_FIX_PRIORITIES[0]
    binary = fake_qoder(tmp_path, {'conclusion': conclusion, 'report_markdown': '审查结论', 'fix': fix})
    monkeypatch.setenv('KANPILOT_QODER_BIN', str(binary))
    config = {'backend': 'qoder', 'qoder': {'model': 'performance', 'reasoning_effort': 'high'}}
    code = cli_review.run_stage('review', conn=conn, db_path=tmp_path / 'isolated.db', timeout=5,
        log_path=tmp_path / 'review.log', record_run=lambda **fields: None, config_override=config)
    assert code == 0  # A valid blocked verdict is a completed Review, not a CLI failure.
    observed = json.loads((tmp_path / 'observed.json').read_text())
    assert observed['argv'][observed['argv'].index('--model') + 1] == 'performance'
    assert Path(observed['cwd']) != Path(task['execution_round']['worktree_path'])
    commands = [command for command, _ in calls]
    assert commands.index('record-cost') < commands.index('register-review') < commands.index('report')
    registered = next(payload for command, payload in calls if command == 'register-review')
    assert registered['conclusion'] == conclusion
    cost = next(payload for command, payload in calls if command == 'record-cost')
    assert cost['source'] == 'qoder' and cost['cost_usd'] is None


def test_qoder_review_bad_business_json_cannot_register_pass(runner_scene, tmp_path, monkeypatch):
    conn, _, calls = runner_scene
    binary = fake_qoder(tmp_path, {'conclusion': 'pass'})
    monkeypatch.setenv('KANPILOT_QODER_BIN', str(binary))
    assert cli_review.run_stage('review', conn=conn, db_path=tmp_path / 'isolated.db', timeout=5,
        log_path=tmp_path / 'review.log', record_run=lambda **fields: None,
        config_override={'backend': 'qoder', 'qoder': dict(planner_config.QODER_DEFAULT)}) == 1
    assert not any(command == 'register-review' for command, _ in calls)
    assert any(command == 'report' for command, _ in calls)


def test_custom_review_passes_selected_provider_to_child(runner_scene, tmp_path, monkeypatch):
    conn, _, calls = runner_scene
    settings = custom_settings(tmp_path)
    original = settings.read_bytes()
    binary = fake_qoder(tmp_path, {'conclusion': 'pass', 'report_markdown': 'No fixture defects', 'fix': None})
    monkeypatch.setenv('QODER_CONFIG_DIR', str(settings.parent))
    monkeypatch.setenv('KANPILOT_QODER_BIN', str(binary))
    assert cli_review.run_stage('review', conn=conn, db_path=tmp_path / 'isolated.db', timeout=5,
        log_path=tmp_path / 'review.log', record_run=lambda **fields: None,
        config_override={'backend': 'qoder', 'qoder': {'model': CUSTOM_MODEL, 'reasoning_effort': 'inherit'}}) == 0
    observed = json.loads((tmp_path / 'observed.json').read_text())
    assert observed['selected_key_received']
    assert observed['argv'][observed['argv'].index('--model') + 1] == CUSTOM_MODEL
    assert settings.read_bytes() == original
    assert PROVIDER_SECRET not in json.dumps(observed)
    registered = next(payload for command, payload in calls if command == 'register-review')
    assert registered['conclusion'] == 'pass'


def test_missing_custom_review_provider_cannot_launch_or_pass(runner_scene, tmp_path, monkeypatch):
    conn, _, calls = runner_scene
    empty = tmp_path / 'empty-qoder-config'
    empty.mkdir()
    binary = fake_qoder(tmp_path, {'conclusion': 'pass', 'report_markdown': 'must not run', 'fix': None})
    monkeypatch.setenv('QODER_CONFIG_DIR', str(empty))
    monkeypatch.setenv('KANPILOT_QODER_BIN', str(binary))
    assert cli_review.run_stage('review', conn=conn, db_path=tmp_path / 'isolated.db', timeout=5,
        log_path=tmp_path / 'review.log', record_run=lambda **fields: None,
        config_override={'backend': 'qoder', 'qoder': {'model': CUSTOM_MODEL, 'reasoning_effort': 'inherit'}}) == 1
    assert not (tmp_path / 'observed.json').exists()
    assert not any(command == 'register-review' for command, _ in calls)
    assert any(command == 'report' for command, _ in calls)
    assert conn.execute('SELECT state FROM worktree_slots').fetchone()['state'] == 'free'
