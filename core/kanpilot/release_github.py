"""Pinned GitHub REST transport; installation packages are never downloaded."""
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from . import gh_client, util

API_VERSION = '2026-03-10'
MANIFEST_NAME = 'kaneopilot-release.json'
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _args(path, *, accept='application/vnd.github+json'):
    return ['api', path, '-H', 'X-GitHub-Api-Version: '+API_VERSION, '-H',
            'Accept: '+accept]


def _api(path):
    return gh_client._run(_args(path), read_only=True)


def _binary(path, max_bytes, *, accept='application/octet-stream'):
    raw=gh_client._run(_args(path,accept=accept),read_only=True,binary=True)
    if len(raw)>max_bytes:
        raise gh_client.GhError('rejected','发布证据超过大小限制')
    return raw


def _pages(path,key=None):
    rows=[]
    for page in range(1,101):
        value=_api(path+('&' if '?' in path else '?')+'per_page=100&page='+str(page))
        part=value.get(key) if isinstance(value,dict) and key else value
        if not isinstance(part,list):
            raise gh_client.GhError('transient','GitHub 分页列表不可确认')
        rows.extend(part)
        if len(part)<100:
            if key and type(value.get('total_count')) is int and len(rows)<value['total_count']:
                raise gh_client.GhError('transient','GitHub 分页列表不完整')
            return rows
    raise gh_client.GhError('rejected','GitHub 分页列表过大，未取得完整证据')


def _sha(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{40,64}',value):
        raise gh_client.GhError('transient','GitHub SHA 身份不可确认')
    return value


def workflow_snapshot(repo,path,ref):
    repo=gh_client._repo(repo)
    value=_api('repos/'+repo+'/actions/workflows/'+quote(path.rsplit('/',1)[-1],safe=''))
    commit=_api('repos/'+repo+'/commits/'+quote(ref,safe=''))
    if (not isinstance(value,dict) or type(value.get('id')) is not int or value['id']<=0
            or value.get('path')!=path or value.get('state')!='active'):
        raise gh_client.GhError('rejected','工作流不存在、未启用或路径不匹配')
    return dict(id=value['id'],path=path,ref=ref,head_sha=_sha(commit.get('sha')),state='active',repository=repo)


def list_releases(repo):
    return _pages('repos/'+gh_client._repo(repo)+'/releases')


def dispatch(repo,workflow,ref,inputs):
    body=util.jdump(dict(ref=ref,inputs=inputs))
    with tempfile.TemporaryDirectory(prefix='kanpilot-release-') as directory:
        path=Path(directory)/'dispatch.json';path.write_text(body,encoding='utf-8')
        return gh_client._run([*_args('repos/'+gh_client._repo(repo)+'/actions/workflows/'+str(workflow)+'/dispatches'),
                              '--method','POST','--input',str(path)])


def run(repo,run_id):
    return _api('repos/'+gh_client._repo(repo)+'/actions/runs/'+str(run_id))


def find_runs(repo,workflow):
    return _pages('repos/'+gh_client._repo(repo)+'/actions/workflows/'+str(workflow)+'/runs?event=workflow_dispatch','workflow_runs')


def jobs(repo,run_id,attempt):
    return _pages('repos/'+gh_client._repo(repo)+'/actions/runs/'+str(run_id)+'/attempts/'+str(attempt)+'/jobs','jobs')


def release(repo,tag):
    try:
        value=_api('repos/'+gh_client._repo(repo)+'/releases/tags/'+quote(tag,safe=''))
    except gh_client.GhError as exc:
        if exc.kind=='not_found':
            return None
        raise
    if not isinstance(value,dict) or type(value.get('id')) is not int:
        raise gh_client.GhError('transient','Release 身份不可确认')
    value=dict(value)
    value['assets']=_pages('repos/'+gh_client._repo(repo)+'/releases/'+str(value['id'])+'/assets')
    return value


def tag_commit(repo,tag):
    prefix='repos/'+gh_client._repo(repo)+'/git/'
    try:
        value=_api(prefix+'ref/tags/'+quote(tag,safe=''))
    except gh_client.GhError as exc:
        if exc.kind=='not_found':
            return None
        raise
    obj=value.get('object') if isinstance(value,dict) else None
    seen=set()
    for _ in range(10):
        if not isinstance(obj,dict):
            break
        sha=_sha(obj.get('sha'))
        if obj.get('type')=='commit':
            return sha
        if obj.get('type')!='tag' or sha in seen:
            break
        seen.add(sha)
        obj=_api(prefix+'tags/'+sha).get('object')
    raise gh_client.GhError('rejected','Release tag 无法唯一解引用到提交')


def provenance(repo,run_id):
    rows=_pages('repos/'+gh_client._repo(repo)+'/actions/runs/'+str(run_id)+'/artifacts','artifacts')
    matches=[row for row in rows if isinstance(row,dict) and row.get('name')=='kaneopilot-provenance']
    if len(matches)!=1:
        raise gh_client.GhError('rejected','本次 Actions 缺少唯一 provenance artifact')
    artifact=matches[0]
    if (artifact.get('expired') is not False or type(artifact.get('id')) is not int
            or type(artifact.get('size_in_bytes')) is not int or not 0<artifact['size_in_bytes']<=MAX_ARTIFACT_BYTES):
        raise gh_client.GhError('rejected','本次 provenance artifact 已过期或大小不可确认')
    # Actions negotiates JSON before redirecting to ZIP; binary output is independent of Accept.
    raw=_binary('repos/'+gh_client._repo(repo)+'/actions/artifacts/'+str(artifact['id'])+'/zip',MAX_ARTIFACT_BYTES,
                accept='application/vnd.github+json')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files=[info for info in archive.infolist() if not info.is_dir()]
            if (len(files)!=1 or files[0].filename!=MANIFEST_NAME or files[0].flag_bits & 1
                    or not 0<files[0].file_size<=MAX_MANIFEST_BYTES):
                raise ValueError('invalid provenance archive')
            data=archive.read(files[0])
    except (ValueError,OSError,zipfile.BadZipFile,RuntimeError) as exc:
        raise gh_client.GhError('rejected','provenance artifact 内没有唯一且完整的发布 manifest') from exc
    if len(data)>MAX_MANIFEST_BYTES:
        raise gh_client.GhError('rejected','发布 manifest 超过大小限制')
    return data


def asset_bytes(repo,asset):
    if (type(asset.get('id')) is not int or type(asset.get('size')) is not int
            or not 0<asset['size']<=MAX_MANIFEST_BYTES):
        raise gh_client.GhError('rejected','Release manifest 资产身份或大小无效')
    raw=_binary('repos/'+gh_client._repo(repo)+'/releases/assets/'+str(asset['id']),MAX_MANIFEST_BYTES,
                accept='application/octet-stream')
    if len(raw)!=asset['size']:
        raise gh_client.GhError('transient','Release manifest 资产大小不一致')
    return raw


def merge_commit_sha(repo,number,head):
    value=_api('repos/'+gh_client._repo(repo)+'/pulls/'+str(number))
    if (not isinstance(value,dict) or value.get('merged') is not True
            or (value.get('head') or {}).get('sha')!=head):
        raise gh_client.GhError('rejected','历史任务 PR 的最终合并身份不符')
    return _sha(value.get('merge_commit_sha'))
