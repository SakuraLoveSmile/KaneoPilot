"""交付阶段演练（scenarios.md 场景 13/14/15；protocol §5.27、state-model §8/§9）。

覆盖：
- 场景 13 ``push_rejected_no_force``：delivery-publish 采用非强制推送；裸
  remote 的 pre-receive 拒绝 → 交付失败并按阶梯归路（``branch_diverged`` →
  阶梯 3），不生成 ``delivery.pr_number``、不把 Kaneo 卡片推成 done；推送
  参数日志证明没有 ``--force``/``--force-with-lease``；钩子恢复 + ``resume``
  后可成功建 PR（失败不是永久损坏）。
- 场景 14 ``gh_not_logged_in_blocks_delivery``：假 gh 的 ``pr create``
  返回未登录 → 交付阶段环境失败，不自动重试成功、不伪造 PR、Kaneo 无 PR
  评论；中文原因不含 token；恢复 gh + ``resume`` 后可成功建 PR（环境失败
  而非永久失败）。
- 场景 15 ``pr_closed_unmerged_not_done``：delivery-sync 读到 PR 已关闭
  未合并 → ``pr_closed``/needs_user，Kaneo 卡片保持 in-review，保留
  ``delivery.pr_number`` 但不伪造合并；改回 OPEN 仍未合并不得 done，只有
  合并才 advance-task done。

基座直接复用 ``tests/test_auto_pipeline.py`` 的 v5 全链夹具
（``TestAutoPipeline``：假 Kaneo/Plus/Claude/gh + 真实本地 git 与裸
remote）。本文件只在该夹具上新增三个 ``test_scenario_*`` 用例，不改动任何
既有文件。运行：``python3 -m pytest tests/rehearsal/test_rehearsal_delivery.py -q``。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_PARENT)
for _p in (_REPO, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import test_auto_pipeline  # noqa: E402
from test_auto_pipeline import TASK  # noqa: E402
try:
    from tests.rehearsal.conftest import terminate_lingering_workers  # noqa: E402
except ImportError:  # 顶层模块收集（discover -s tests/rehearsal）
    from conftest import terminate_lingering_workers  # noqa: E402
from kanpilot import core, db, delivery_runner, util  # noqa: E402
from kanpilot.engine import ladder  # noqa: E402

# 假 gh 的 pr create 分支（setUp 原文）；场景 14 把它替换成未登录失败。
_GH_CREATE_LINE = ('elif a[:2]==["pr","create"]: state["created"]=True;'
                   'p.write_text(json.dumps(state));print(row["url"])')
_GH_CREATE_NOT_LOGGED_IN = ('elif a[:2]==["pr","create"]: '
                            'sys.stderr.write("gh: not logged in to any GitHub hosts\\n");'
                            'sys.exit(1)')

# 继承自 TestAutoPipeline、但由 tests/test_auto_pipeline.py 覆盖的用例名。
_COVERED_NOTE = ('已由 tests/test_auto_pipeline.py 覆盖；本文件只跑交付场景 13–15')
_COVERED_BY_AUTO_PIPELINE = (
    'test_failed_execution_replan_preserves_partial_work',
    'test_full_feedback_two_rounds_to_pr_merge',
    'test_missing_verification_user_accepts_then_review',
    'test_fix_round_limit_and_manual_takeover',
    'test_review_failure_manual_takeover_preserves_branch',
    'test_closed_pr_enters_pending_without_completing_kaneo',
)


class TestRehearsalDelivery(test_auto_pipeline.TestAutoPipeline):
    """场景 13/14/15：交付阶段故障注入（继承 v5 全链演练夹具）。

    runner 无关的生命周期：真实 pytest 经 unittest 支持自动跑 setUp/tearDown；
    ``python3 -m pytest tests/rehearsal -q`` 的 discover 兜底路径
    （tests/unittest_compat.py）直接调用方法、不跑 setUp——由
    ``_run_with_own_setup`` 自建并收口（addCleanup 的 mock/env 补丁不泄漏）。
    """
    # ------------------------------------------------------------- helpers

    def test_scenario_push_rejected_no_force(self):
        self._run_with_own_setup(self._scenario_push_rejected_no_force)

    def test_scenario_gh_not_logged_in_blocks_delivery(self):
        self._run_with_own_setup(
            self._scenario_gh_not_logged_in_blocks_delivery)

    def test_scenario_pr_closed_unmerged_not_done(self):
        self._run_with_own_setup(self._scenario_pr_closed_unmerged_not_done)

    def _run_with_own_setup(self, body):
        own = not hasattr(self, "root")   # 兜底路径：setUp 尚未跑过
        if own:
            self.setUp()
        try:
            body()
        finally:
            if own:
                self.tearDown()
                self.doCleanups()
            # 常驻 worker 会跨用例漂移（父夹具竞态）：收口时清理本进程树的。
            terminate_lingering_workers()
    def _review_pass_to_in_review(self):
        """走到 in_review：假 claude 首轮评审即 pass（review-count 预置 '1'）。"""
        (self.root / 'review-count').write_text('1')
        self._adopt_and_plan(); self._execute(); self._monitor(); self._review()
        value = self.task(TASK)
        self.assertEqual(value['status'], 'in_review', value)
        self.assertTrue(value['delivery'].get('head_sha'), value['delivery'])
        return value

    def _bare_remote(self):
        return Path(test_auto_pipeline.git(
            self.repo, 'remote', 'get-url', '--push', 'origin'))

    def _install_push_reject_hook(self, log):
        """裸 remote 装 pre-receive：记录被推送引用后退出 1（模拟拒绝）。"""
        hook = self._bare_remote() / 'hooks' / 'pre-receive'
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            '#!' + sys.executable + '\n'
            'import sys\n'
            'log = ' + repr(str(log)) + '\n'
            'rows = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]\n'
            'with open(log, "a", encoding="utf-8") as fh:\n'
            '    for row in rows:\n'
            '        fh.write(" ".join(row) + "\\n")\n'
            'sys.stderr.write("rehearsal: push rejected by hook\\n")\n'
            'sys.exit(1)\n', encoding='utf-8')
        hook.chmod(0o755)
        self._push_hook = hook
        self.addCleanup(hook.unlink, missing_ok=True)

    def _wrap_git_on_path(self, log):
        """PATH 前置 git 包装：逐条记录 argv（证明推送参数），再 exec 真 git。"""
        real = shutil.which('git')
        self.assertTrue(real, '未找到真实 git')
        bin_dir = self.root / 'git-wrap'
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / 'git'
        script.write_text(
            '#!' + sys.executable + '\n'
            'import os, sys\n'
            'with open(' + repr(str(log)) + ', "a", encoding="utf-8") as fh:\n'
            '    fh.write(" ".join(sys.argv[1:]) + "\\n")\n'
            'os.execv(' + repr(real) + ', ["git"] + sys.argv[1:])\n',
            encoding='utf-8')
        script.chmod(0o755)
        patch = mock.patch.dict(os.environ, {
            'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')})
        patch.start(); self.addCleanup(patch.stop)
        return log

    @staticmethod
    def _push_lines(log):
        text = Path(log).read_text(encoding='utf-8') if Path(log).exists() else ''
        return [line for line in text.splitlines() if ' push ' in line]

    def _break_gh_pr_create(self):
        """假 gh 的 pr create 改为未登录失败；恢复原文的 cleanup 已注册。"""
        gh = self.root / 'fake-gh'
        original = gh.read_text(encoding='utf-8')
        broken = original.replace(_GH_CREATE_LINE, _GH_CREATE_NOT_LOGGED_IN)
        self.assertNotEqual(broken, original, '假 gh 的 pr create 分支未被替换')
        gh.write_text(broken, encoding='utf-8')
        self.addCleanup(gh.write_text, original)

    # ------------------------------------------------------- 场景 13
    def _scenario_push_rejected_no_force(self):
        """场景 13：push 被拒 → 非强制推送 + 阶梯归路，恢复后可建 PR。"""
        self._review_pass_to_in_review()
        push_log = self.root / 'push-args.log'
        hook_log = self.root / 'pre-receive.log'
        self._wrap_git_on_path(push_log)
        self._install_push_reject_hook(hook_log)

        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['failure_code'], 'branch_diverged', value)
        self.assertEqual(value['status'], 'needs_user', value)
        self.assertEqual(value['failure_stage'], 'delivery', value)
        self.assertIsNone(value['delivery'].get('pr_number'), value['delivery'])
        self.assertEqual(self.fake.tasks[TASK]['status'], 'in-review')
        self.assertEqual(self.comments, [])
        self.assertIn('resume', value['allowed_actions'])
        # 分支偏离 → 阶梯 3（有界补救），不是阶梯 1 无限重试。
        self.assertEqual(ladder.classify(value['failure_code']),
                         ladder.LADDER_QUALITY)

        pushes = self._push_lines(push_log)
        self.assertTrue(pushes, '未记录到任何 git push 调用')
        for line in pushes:
            self.assertNotIn('--force', line, line)
            self.assertNotIn('--force-with-lease', line, line)
            self.assertIn('refs/heads/kaneo/' + TASK, line)
        hook_rows = [row for row in
                     (hook_log.read_text(encoding='utf-8').splitlines()
                      if hook_log.exists() else []) if row.strip()]
        self.assertTrue(hook_rows, 'pre-receive 钩子未被调用')
        self.assertIn('refs/heads/kaneo/' + TASK, hook_rows[0])

        # 环境修复：移除远端 pre-receive 钩子（恢复接受推送）。
        self._push_hook.unlink(missing_ok=True)
        # 环境修复（移除钩子）+ resume → 同一提交能成功建 PR：失败非永久损坏。
        self.call('resolve', {'task_id': TASK, 'action': 'resume',
                              'note': '远端钩子已修复'})
        self.assertEqual(self.task(TASK)['status'], 'in_review')
        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['delivery'].get('pr_number'), 7, {
            'status': value['status'], 'failure': value['failure_code'],
            'events': self.call('list-events', {'task_id': TASK, 'limit': 12}),
            'push_log': push_log.read_text(encoding='utf-8')})
        self.assertEqual(value['status'], 'in_review', value)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.comments[0]['content'],
                         '[kaneopilot:delivery:' + TASK + '] 已创建 PR：'
                         'https://github.com/owner/repo/pull/7')
        self.assertEqual(self.fake.tasks[TASK]['status'], 'in-review')

    # ------------------------------------------------------- 场景 14
    def _scenario_gh_not_logged_in_blocks_delivery(self):
        """场景 14：gh 未登录 → 交付环境失败，恢复 gh 后可手动 resume 成功。"""
        self._review_pass_to_in_review()
        self._break_gh_pr_create()

        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        # 交付层透出未登录码（P1 细化后）：环境失败，归阶梯 2
        # （scenarios.md 场景 14 预期行为，与 ladder 表一致）。
        self.assertEqual(value['failure_code'], 'gh_not_logged_in', value)
        self.assertEqual(value['status'], 'needs_user', value)
        self.assertEqual(value['failure_stage'], 'delivery', value)
        self.assertIsNone(value['delivery'].get('pr_number'), value['delivery'])
        self.assertEqual(self.comments, [], '不得伪造 PR / 评论')
        self.assertIn('resume', value['allowed_actions'])
        self.assertIn('not logged in', (value['failure_reason'] or '').lower(),
                      value['failure_reason'])
        blob = json.dumps(value, ensure_ascii=False)
        for secret in ('ghp_', 'github_pat_', 'GH_TOKEN=', 'Authorization',
                       'Bearer '):
            self.assertNotIn(secret, blob, '失败原因泄露凭据样式内容')
        # 阶梯 2 环境失败：不自动重试、不升级失败，等待用户修复环境。
        self.assertEqual(ladder.classify(value['failure_code']),
                         ladder.LADDER_ENVIRONMENTAL)
        self.assertEqual(ladder.ladder_type(ladder.classify(value['failure_code'])),
                         'environmental')
        self.assertFalse(ladder.auto_retry_allowed(
            ladder.classify(value['failure_code'])))

        # 恢复 gh 登录环境 + resume → 成功建 PR：环境失败，不是永久失败。
        (self.root / 'fake-gh').write_text(
            (self.root / 'fake-gh').read_text(encoding='utf-8')
            .replace(_GH_CREATE_NOT_LOGGED_IN, _GH_CREATE_LINE), encoding='utf-8')
        self.call('resolve', {'task_id': TASK, 'action': 'resume',
                              'note': 'gh 已登录'})
        self.assertEqual(self.task(TASK)['status'], 'in_review')
        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['delivery'].get('pr_number'), 7, {
            'status': value['status'], 'failure': value['failure_code'],
            'gh_log': self.gh_state.read_text(encoding='utf-8')})
        self.assertEqual(value['status'], 'in_review', value)
        self.assertEqual(len(self.comments), 1)

    # ------------------------------------------------------- 场景 15
    def _scenario_pr_closed_unmerged_not_done(self):
        """场景 15：PR 关闭未合并 → 不 done、不伪造合并；只有合并才 done。"""
        self._review_pass_to_in_review()
        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['delivery'].get('pr_number'), 7, value)
        self.assertEqual(value['delivery'].get('pr_state'), 'OPEN', value['delivery'])

        self.gh_state.write_text(json.dumps(
            {'created': True, 'state': 'CLOSED', 'mergedAt': None}))
        conn = db.connect(self.db_path)
        try:
            core.update_delivery(conn, db.require_task(conn, TASK),
                                 util.now_iso(), next_sync_at=None)
        finally:
            conn.close()
        self.call('tick'); self._stage(delivery_runner, 'delivery')

        value = self.task(TASK)
        self.assertEqual(value['failure_code'], 'pr_closed', value)
        self.assertEqual(value['status'], 'needs_user', value)
        self.assertEqual(value['failure_stage'], 'delivery', value)
        self.assertEqual(self.fake.tasks[TASK]['status'], 'in-review')
        self.assertEqual(value['delivery'].get('pr_number'), 7, value['delivery'])
        self.assertIsNone(value['delivery'].get('merged_at'), value['delivery'])
        self.assertIn('resume', value['allowed_actions'])
        self.assertEqual(ladder.classify(value['failure_code']),
                         ladder.LADDER_NEEDS_YOU)

        # PR 重新打开且仍未合并 → 依然不得 done。
        self.gh_state.write_text(json.dumps(
            {'created': True, 'state': 'OPEN', 'mergedAt': None}))
        self.call('resolve', {'task_id': TASK, 'action': 'resume',
                              'note': '重新打开核对'})
        self.assertEqual(self.task(TASK)['status'], 'in_review')
        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['status'], 'in_review', value)
        self.assertNotEqual(self.fake.tasks[TASK]['status'], 'done')
        self.assertIsNone(value['delivery'].get('merged_at'), value['delivery'])

        # 只有合并才 done（state-model §9）。
        self.gh_state.write_text(json.dumps(
            {'created': True, 'state': 'MERGED', 'mergedAt': util.now_iso()}))
        conn = db.connect(self.db_path)
        try:
            core.update_delivery(conn, db.require_task(conn, TASK),
                                 util.now_iso(), next_sync_at=None)
        finally:
            conn.close()
        self.call('tick'); self._stage(delivery_runner, 'delivery')
        value = self.task(TASK)
        self.assertEqual(value['status'], 'done', value)
        self.assertEqual(self.fake.tasks[TASK]['status'], 'done')
        self.assertEqual(value['delivery'].get('merged_at'),
                         json.loads(self.gh_state.read_text(encoding='utf-8'))['mergedAt'])

    # ------------------------------------------- 继承用例：已在父模块覆盖
    # TestAutoPipeline 自带的 5 个用例由 ``tests/test_auto_pipeline.py`` 按
    # 原顺序覆盖；在本文件按新顺序重跑会触发该夹具的顺序相关竞态——上一用例
    # 的真 ``pi_desktop_worker`` 结束后仍常驻轮询（源码注释即「测试负责终止
    # 该进程」），新用例的假 Plus 服务常复用同一临时端口，常驻 worker 于是
    # 消费新服务器的 turn 状态队列，使后续用例的 ``_execute`` 观测漂移
    # （``execution_failed`` 观测不到）。该竞态与交付场景无关，此处显式跳过；
    # 修复归属父夹具（tests/test_auto_pipeline.py / core/tests 假 Plus）。
    def test_failed_execution_replan_preserves_partial_work(self):
        self.skipTest(_COVERED_NOTE)

    def test_full_feedback_two_rounds_to_pr_merge(self):
        self.skipTest(_COVERED_NOTE)

    def test_missing_verification_user_accepts_then_review(self):
        self.skipTest(_COVERED_NOTE)

    def test_fix_round_limit_and_manual_takeover(self):
        self.skipTest(_COVERED_NOTE)

    def test_review_failure_manual_takeover_preserves_branch(self):
        self.skipTest(_COVERED_NOTE)

    def test_closed_pr_enters_pending_without_completing_kaneo(self):
        self.skipTest(_COVERED_NOTE)



# 覆盖核对：TestAutoPipeline 的用例集若变化（新增/改名），必须同步上面的
# _COVERED_BY_AUTO_PIPELINE 或本文件的场景方法，否则会有未遮盖的继承用例
# 在新顺序下重跑并触发父夹具的顺序相关竞态。
_SCENARIOS = tuple(name for name in vars(TestRehearsalDelivery)
                   if name.startswith('test_scenario_'))
_INHERITED = frozenset(name for name in dir(test_auto_pipeline.TestAutoPipeline)
                       if name.startswith('test_'))
assert _INHERITED == frozenset(_COVERED_BY_AUTO_PIPELINE), (
    'TestAutoPipeline 用例集已变化，请同步 _COVERED_BY_AUTO_PIPELINE：'
    f'{sorted(_INHERITED)}')
assert _SCENARIOS == ('test_scenario_push_rejected_no_force',
                      'test_scenario_gh_not_logged_in_blocks_delivery',
                      'test_scenario_pr_closed_unmerged_not_done'), _SCENARIOS

def load_tests(loader, standard_tests, pattern):
    import sys
    try:
        import unittest_compat
    except ImportError:  # 包上下文（tests.test_x 导入形态）
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
