"""Read-only GitHub evidence. No fetch, checkout, push, merge, or model calls."""
import re
from urllib.parse import quote

from . import gh_client, git_delivery, util


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40,64}', value):
        raise gh_client.GhError('transient', 'GitHub 提交或 tree 身份不可确认')
    return value


def pr(repo, number):
    return gh_client.pr_view(repo, number, merge_checks=True)


def commit(repo, ref):
    repo = gh_client._repo(repo)
    if not isinstance(ref, str) or not ref or any(c in ref for c in '\x00\r\n'):
        raise gh_client.GhError('rejected', '目标提交或分支无效')
    value = gh_client._run(['api', 'repos/' + repo + '/commits/' + quote(ref, safe='')], read_only=True)
    try:
        sha = _sha(value['sha'])
        tree = _sha(value['commit']['tree']['sha'])
    except (KeyError, TypeError) as exc:
        raise gh_client.GhError('transient', 'GitHub 提交树不可确认') from exc
    raw = gh_client._run(['api', 'repos/' + repo + '/git/trees/' + tree + '?recursive=1'], read_only=True)
    if not isinstance(raw, dict) or raw.get('sha') != tree or raw.get('truncated') is not False or not isinstance(raw.get('tree'), list):
        raise gh_client.GhError('transient', 'GitHub 源码树不完整，不能收尾')
    records, paths, seen = [], [], set()
    for entry in raw['tree']:
        if not isinstance(entry, dict):
            raise gh_client.GhError('transient', 'GitHub 源码树条目无效')
        path = entry.get('path')
        if (not isinstance(path, str) or not path or path.startswith('/')
                or any(p in ('', '.', '..') for p in path.split('/')) or path in seen):
            raise gh_client.GhError('transient', 'GitHub 源码路径无效或重复')
        seen.add(path)
        if entry.get('type') == 'tree':
            continue
        if path.startswith('.pi/') or git_delivery.is_build_artifact(path):
            continue
        if entry.get('type') != 'blob' or entry.get('mode') not in ('100644','100755','120000'):
            raise gh_client.GhError('transient', 'GitHub 源码树包含无法核验的文件类型')
        records.append(dict(path=path, mode=entry['mode'], blob=_sha(entry.get('sha'))))
        if entry['mode'] in ('100644','100755'):
            paths.append(path)
    return dict(sha=sha, tree_sha=tree,
                source_sha256=util.sha256_text(util.jdump(sorted(records,key=lambda r:r['path']))),
                paths=sorted(paths))


def contains(repo, base, head):
    base, head = _sha(base), _sha(head)
    if base == head:
        return True
    value = gh_client._run(['api', 'repos/' + gh_client._repo(repo) + '/compare/' + base + '...' + head], read_only=True)
    if (not isinstance(value, dict) or value.get('status') not in ('ahead','behind','diverged','identical')
            or (value.get('base_commit') or {}).get('sha') != base):
        raise gh_client.GhError('transient', 'GitHub 提交包含关系不可确认')
    return (value.get('status') in ('ahead','identical')
            and (value.get('merge_base_commit') or {}).get('sha') == base)
