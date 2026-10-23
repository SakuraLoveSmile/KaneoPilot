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


def _binary(path, max_bytes, *, accept='application/octet-stream', allow_escape_sequences=False):
    args=_args(path,accept=accept)
    if allow_escape_sequences:
        args.append('--allow-escape-sequences')
    raw=gh_client._run(args,read_only=True,binary=True)
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


def _sha(value, *, message='GitHub SHA 身份不可确认'):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{40,64}',value):
        raise gh_client.GhError('transient',message)
    return value


def workflow_snapshot(repo,path,ref):
    repo=gh_client._repo(repo)
    value=_api('repos/'+repo+'/actions/workflows/'+quote(path.rsplit('/',1)[-1],safe=''))
    commit=_api('repos/'+repo+'/commits/'+quote(ref,safe=''))
    if (not isinstance(value,dict) or type(value.get('id')) is not int or value['id']<=0
            or value.get('path')!=path):
        raise gh_client.GhError('rejected','工作流不存在或路径不匹配')
    if value.get('state')!='active':
        reason='工作流已被手动停用' if value.get('state')=='disabled_manually' else '工作流未启用'
        raise gh_client.GhError('rejected',reason+'，请先在 GitHub 启用后重新准备发布')
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


def _plain_log(content):
    # Logs may contain terminal colours/hyperlinks, but never execute them in readers.
    content=re.sub(r'(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)','',content)
    content=re.sub(r'(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]','',content)
    content=content.replace('\r\n','\n').replace('\r','\n')
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]','',content)


def failure_logs(repo, run_id, attempt):
    """Read bounded logs only for failed jobs in this exact attempt; no writes."""
    repo = gh_client._repo(repo)
    rows = jobs(repo, run_id, attempt)
    failed = [row for row in rows if row.get('status') == 'completed'
              and row.get('conclusion') in ('failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure')]
    if not failed or len(failed) > 20:
        raise gh_client.GhError('rejected', '本次 attempt 缺少可读取的失败 job，或失败 job 过多')
    result, size = [], 0
    for job in failed:
        if (type(job.get('id')) is not int or job['id'] <= 0 or job.get('run_id') != run_id
                or job.get('run_attempt') != attempt):
            raise gh_client.GhError('rejected', '失败 job 与冻结运行或 attempt 不符')
        raw = _binary('repos/'+repo+'/actions/jobs/'+str(job['id'])+'/logs', 2*1024*1024,
                      accept='application/json',allow_escape_sequences=True)
        size += len(raw)
        if size > 4*1024*1024:
            raise gh_client.GhError('rejected', '失败日志超过 4 MiB，未截断或猜测修复')
        try:
            content = gh_client.redact(_plain_log(raw.decode('utf-8')))
        except UnicodeDecodeError as exc:
            raise gh_client.GhError('rejected', '失败日志不是完整 UTF-8 文本') from exc
        if not content.strip():
            raise gh_client.GhError('rejected', '失败 job 日志为空或不可用')
        result.append(dict(job_id=job['id'], name=gh_client.redact(_plain_log(str(job.get('name') or ''))),
                           conclusion=job['conclusion'], content=content))
    return result


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


def release_tag(repo, tag):
    """Return the immutable annotated tag, including its ownership message."""
    prefix = 'repos/' + gh_client._repo(repo) + '/git/'
    ref = _api(prefix + 'ref/tags/' + quote(tag, safe=''))
    obj = ref.get('object') if isinstance(ref, dict) else None
    if not isinstance(obj, dict) or obj.get('type') != 'tag':
        raise gh_client.GhError('rejected', '发布说明修正要求原 annotated tag')
    sha = _sha(obj.get('sha'))
    annotation = _api(prefix + 'tags/' + sha)
    if not isinstance(annotation, dict) or annotation.get('sha') != sha:
        raise gh_client.GhError('rejected', '发布 tag annotation 身份不符')
    return annotation


def patch_release_body(repo, release_id, body):
    """One body-only PATCH; the core caller must persist its intent first."""
    if type(release_id) is not int or release_id <= 0 or not isinstance(body, str):
        raise gh_client.GhError('rejected', 'Release 正文参数无效')
    path = 'repos/' + gh_client._repo(repo) + '/releases/' + str(release_id)
    with tempfile.TemporaryDirectory(prefix='kanpilot-release-notes-') as directory:
        source = Path(directory) / 'body.json'
        source.write_text(util.jdump(dict(body=body)), encoding='utf-8')
        return gh_client._run([*_args(path), '--method', 'PATCH', '--input', str(source)])


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
    repo=gh_client._repo(repo)
    value=gh_client.pr_view(repo,number,merge_commit=True)
    if (not isinstance(value,dict) or value.get('number')!=number
            or value.get('url')!='https://github.com/'+repo+'/pull/'+str(number)
            or value.get('state')!='MERGED' or value.get('headRefOid')!=head):
        raise gh_client.GhError('rejected','历史任务 PR 的最终合并身份不符')
    merge=value.get('mergeCommit')
    return _sha(merge.get('oid') if isinstance(merge,dict) else None,
                message='GitHub PR 合并提交版本缺失或格式无效，请重新核对该 PR')
