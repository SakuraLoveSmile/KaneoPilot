"""GitHub source snapshots use only structured, complete, immutable responses."""
from unittest import TestCase
from unittest.mock import patch
from kanpilot import gh_client, parent_closeout_github as github, util


class CloseoutGithubTest(TestCase):
    def payloads(self, **override):
        commit=dict(sha='a'*40,commit=dict(tree=dict(sha='b'*40)))
        tree=dict(sha='b'*40,truncated=False,tree=[
            dict(path='test',mode='040000',type='tree',sha='d'*40),
            dict(path='test/a_test.dart',mode='100644',type='blob',sha='c'*40),
            dict(path='.pi/goal.md',mode='100644',type='blob',sha='e'*40)])
        tree.update(override)
        return commit,tree

    def test_remote_snapshot_matches_git_record_fingerprint(self):
        with patch.object(gh_client,'_run',side_effect=self.payloads()) as read:
            result=github.commit('owner/repo','feature/nested')
        self.assertEqual(result['source_sha256'],util.sha256_text(util.jdump([
            dict(path='test/a_test.dart',mode='100644',blob='c'*40)])))
        self.assertEqual(result['paths'],['test/a_test.dart'])
        self.assertEqual(read.call_args_list[0].args[0],['api','repos/owner/repo/commits/feature%2Fnested'])
        self.assertTrue(all(c.args[0][0]=='api' for c in read.call_args_list))

    def test_incomplete_or_unsafe_trees_fail_closed(self):
        for kwargs in [dict(truncated=True),dict(sha='f'*40),
                       dict(tree=[dict(path='../escape',type='blob',mode='100644',sha='c'*40)]),
                       dict(tree=[dict(path='submodule',type='commit',mode='160000',sha='c'*40)])]:
            with self.subTest(kwargs=kwargs),patch.object(gh_client,'_run',side_effect=self.payloads(**kwargs)):
                with self.assertRaises(gh_client.GhError):github.commit('owner/repo','main')

    def test_contains_requires_exact_base_and_merge_base(self):
        value=dict(status='ahead',base_commit=dict(sha='a'*40),merge_base_commit=dict(sha='a'*40))
        with patch.object(gh_client,'_run',return_value=value):
            self.assertTrue(github.contains('owner/repo','a'*40,'b'*40))
        value['merge_base_commit']['sha']='c'*40
        with patch.object(gh_client,'_run',return_value=value):
            self.assertFalse(github.contains('owner/repo','a'*40,'b'*40))
