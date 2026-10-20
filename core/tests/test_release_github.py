"""Pinned REST adapter checks; every gh process and response is isolated."""
import io
import json
import base64
import os
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

from kanpilot import gh_client, release_github as github


def archive(entries):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as value:
        for name,raw in entries:value.writestr(name,raw)
    return output.getvalue()


@pytest.fixture
def media_gh(tmp_path, monkeypatch):
    """Endpoint behavior fixture, reached through the real gh runner and subprocess."""
    executable=tmp_path/'media-gh'
    calls=tmp_path/'calls.jsonl'
    payload=tmp_path/'payload.json'
    manifest=b'{"schema":1,"batch_id":"fixture-batch"}'
    zipped=archive([(github.MANIFEST_NAME,manifest)])
    payload.write_text(json.dumps(dict(manifest=base64.b64encode(manifest).decode(),
        zipped=base64.b64encode(zipped).decode())))
    executable.write_text('#!'+sys.executable+'\n'+'''import base64,json,os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['MEDIA_GH_CALLS']).open('a') as log: log.write(json.dumps(args)+'\\n')
data=json.loads(Path(os.environ['MEDIA_GH_PAYLOAD']).read_text())
if len(args)<2 or args[0]!='api': sys.exit(77)
path=args[1].split('?')[0]
headers=[args[i+1] for i,arg in enumerate(args[:-1]) if arg=='-H']
accept=next((value.split(':',1)[1].strip() for value in headers if value.lower().startswith('accept:')),'')
json_media=accept in ('application/json','application/vnd.github+json')
if path=='repos/example/repo/actions/runs/42/artifacts':
    if not json_media: sys.exit(77)
    print(json.dumps({'total_count':1,'artifacts':[{'id':5,'name':'kaneopilot-provenance','expired':False,'size_in_bytes':len(base64.b64decode(data['zipped']))}]}))
elif path=='repos/example/repo/actions/artifacts/5/zip':
    if not json_media:
        print("gh: Unsupported 'Accept' header: '"+accept+"'. Must accept 'application/json'. (HTTP 415)",file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(base64.b64decode(data['zipped']))
elif path=='repos/example/repo/releases/assets/2':
    if accept=='application/octet-stream': sys.stdout.buffer.write(base64.b64decode(data['manifest']))
    elif json_media: print(json.dumps({'id':2,'name':'kaneopilot-release.json'}))
    else: sys.exit(77)
else: sys.exit(77)
''')
    executable.chmod(0o755)
    poison=tmp_path/'gh'
    poison.write_text('#!'+sys.executable+'\nimport sys\nsys.exit(77)\n')
    poison.chmod(0o755)
    monkeypatch.setenv('KANPILOT_GH_BIN',str(executable))
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('MEDIA_GH_CALLS',str(calls))
    monkeypatch.setenv('MEDIA_GH_PAYLOAD',str(payload))
    delays=[]
    monkeypatch.setattr(time,'sleep',delays.append)
    return manifest,lambda:[json.loads(line) for line in calls.read_text().splitlines()],delays


def test_actions_zip_media_type_returns_binary_through_subprocess(media_gh):
    manifest,calls,delays=media_gh
    assert github.provenance('example/repo',42)==manifest
    assert len(calls())==2 and delays==[]


def test_release_manifest_media_type_downloads_content_not_metadata(media_gh):
    manifest,calls,delays=media_gh
    assert github.asset_bytes('example/repo',dict(id=2,size=len(manifest)))==manifest
    assert len(calls())==1 and delays==[]


def test_endpoint_media_type_rejection_is_not_retried(media_gh):
    _,calls,delays=media_gh
    with pytest.raises(gh_client.GhError,match='HTTP 415'):
        github._binary('repos/example/repo/actions/artifacts/5/zip',1024)
    assert len(calls())==1 and delays==[]


class ReleaseGithubTest(TestCase):
    def test_full_pagination_and_incomplete_total_fail_closed(self):
        with patch.object(github,'_api',side_effect=[list(range(100)),[100]]) as observed:
            self.assertEqual(len(github.list_releases('example/repo')),101)
            self.assertIn('page=2',observed.call_args.args[0])
        with patch.object(github,'_api',return_value=dict(workflow_runs=[],total_count=1)):
            with self.assertRaises(gh_client.GhError):github.find_runs('example/repo',44)

    def test_workflow_snapshot_binds_id_path_and_ref_head(self):
        workflow=dict(id=44,path='.github/workflows/pilot-release.yml',state='active')
        with patch.object(github,'_api',side_effect=[workflow,dict(sha='a'*40)]) as observed:
            value=github.workflow_snapshot('example/repo',workflow['path'],'feature/release')
            self.assertEqual(value['head_sha'],'a'*40);self.assertEqual(value['ref'],'feature/release')
            self.assertIn('feature%2Frelease',observed.call_args.args[0])
        with patch.object(github,'_api',side_effect=[dict(workflow,path='other.yml'),dict(sha='a'*40)]):
            with self.assertRaises(gh_client.GhError):github.workflow_snapshot('example/repo',workflow['path'],'main')

    def test_dispatch_uses_pinned_api_and_json_file_with_exact_five_string_inputs(self):
        inputs=dict(target_sha='a'*40,release_batch_id='batch',task_manifest_json='{"title":"$(not-shell)\\n`literal`"}',
                    task_manifest_sha256='b'*64,tag_name='v0.1.0-pilot.1')
        def invoke(args):
            self.assertIn('X-GitHub-Api-Version: 2026-03-10',args)
            self.assertEqual(args[args.index('--method')+1],'POST')
            body=json.loads(Path(args[args.index('--input')+1]).read_text())
            self.assertEqual(body,dict(ref='main',inputs=inputs))
            return dict(workflow_run_id=42,run_url='api-url',html_url='html-url')
        with patch.object(gh_client,'_run',side_effect=invoke):
            self.assertEqual(github.dispatch('example/repo',44,'main',inputs)['workflow_run_id'],42)

    def test_json_and_binary_requests_pin_api_and_binary_bounds(self):
        with patch.object(gh_client,'_run',return_value={}) as observed:
            github.run('example/repo',42)
            self.assertIn('X-GitHub-Api-Version: 2026-03-10',observed.call_args.args[0])
            self.assertEqual(observed.call_args.kwargs,dict(read_only=True))
        with patch.object(gh_client.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'123',stderr=b'')) as observed:
            self.assertEqual(github._binary('repos/example/repo/evidence',3),b'123')
            self.assertIn('Accept: application/octet-stream',observed.call_args.args[0])
            self.assertIn('X-GitHub-Api-Version: 2026-03-10',observed.call_args.args[0])
            with self.assertRaises(gh_client.GhError):github._binary('repos/example/repo/evidence',2)

    def test_jobs_uses_specific_run_attempt_and_release_assets_paginate(self):
        with patch.object(github,'_api',return_value=dict(jobs=[],total_count=0)) as observed:
            github.jobs('example/repo',42,3)
            self.assertIn('/runs/42/attempts/3/jobs?',observed.call_args.args[0])
        with patch.object(github,'_api',side_effect=[dict(id=10,assets=[]),[{}]*100,[dict(id=101)]]) as observed:
            result=github.release('example/repo','v0.1.0')
            self.assertEqual(len(result['assets']),101)
            self.assertIn('/releases/10/assets?',observed.call_args.args[0])

    def test_tag_dereferences_annotated_tags_and_rejects_cycles(self):
        annotated=dict(object=dict(type='tag',sha='a'*40))
        with patch.object(github,'_api',side_effect=[annotated,dict(object=dict(type='commit',sha='b'*40))]):
            self.assertEqual(github.tag_commit('example/repo','v0.1.0'),'b'*40)
        with patch.object(github,'_api',return_value=annotated):
            with self.assertRaises(gh_client.GhError):github.tag_commit('example/repo','v0.1.0')
        with patch.object(github,'_api',side_effect=gh_client.GhError('not_found','missing')):
            self.assertIsNone(github.tag_commit('example/repo','v0.1.0'))

    def test_provenance_requires_unique_unexpired_artifact_and_exact_archive_member(self):
        valid=dict(name='kaneopilot-provenance',expired=False,id=5,size_in_bytes=100)
        for rows in ([],[valid,valid],[dict(valid,expired=True)],[dict(valid,size_in_bytes=3*1024*1024)]):
            with self.subTest(rows=rows),patch.object(github,'_pages',return_value=rows),patch.object(github,'_binary') as binary:
                with self.assertRaises(gh_client.GhError):github.provenance('example/repo',42)
                binary.assert_not_called()
        for entries in ([('other.json',b'{}')],[('../kaneopilot-release.json',b'{}')],
                        [(github.MANIFEST_NAME,b'{}'),('extra.txt',b'x')],
                        [(github.MANIFEST_NAME,b'{}'),(github.MANIFEST_NAME,b'{}')],
                        [(github.MANIFEST_NAME,b'x'*(github.MAX_MANIFEST_BYTES+1))]):
            with self.subTest(names=[row[0] for row in entries]),patch.object(github,'_pages',return_value=[valid]),patch.object(github,'_binary',return_value=archive(entries)):
                with self.assertRaises(gh_client.GhError):github.provenance('example/repo',42)
        with patch.object(github,'_pages',return_value=[valid]),patch.object(github,'_binary',return_value=archive([(github.MANIFEST_NAME,b'{}')])):
            self.assertEqual(github.provenance('example/repo',42),b'{}')

    def test_manifest_asset_checks_size_without_downloading_installation_packages(self):
        with patch.object(github,'_binary',return_value=b'{}') as binary:
            self.assertEqual(github.asset_bytes('example/repo',dict(id=2,size=2)),b'{}')
            with self.assertRaises(gh_client.GhError):github.asset_bytes('example/repo',dict(id=2,size=100))
            with self.assertRaises(gh_client.GhError):github.asset_bytes('example/repo',dict(id=3,size=100000000))
            self.assertEqual(binary.call_count,2)

    def test_historical_merge_identity_must_match_frozen_pr_head(self):
        with patch.object(github,'_api',return_value=dict(merged=True,head=dict(sha='a'*40),merge_commit_sha='b'*40)):
            self.assertEqual(github.merge_commit_sha('example/repo',42,'a'*40),'b'*40)
            with self.assertRaises(gh_client.GhError):github.merge_commit_sha('example/repo',42,'c'*40)
