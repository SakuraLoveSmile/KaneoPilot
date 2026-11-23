"""Non-interactive GitHub PR operations; never parse human-readable output."""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import constants, util

PR_FIELDS = "number,url,state,headRefOid,mergedAt,baseRefName,headRefName,isCrossRepository"
PR_MERGE_FIELDS = PR_FIELDS + ",baseRefOid,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup"
# protocol §5.27.20：PR 评论只经一次 GraphQL 读取（issue 评论 + review 正文 + reviewThreads）。
# `gh pr view --json` 不带线程正文与 isResolved/isOutdated，不能替代这次读取。
THREAD_QUERY = (
    "query KaneoPilotReviewComments($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
    " number state isDraft headRefOid reviewDecision"
    " comments(last: 30) { nodes { databaseId author { login } body createdAt url } }"
    " reviews(last: 20) { nodes { databaseId author { login } state body createdAt url } }"
    " reviewThreads(last: 30) { nodes { id isResolved isOutdated"
    " comments(last: 10) { nodes { databaseId author { login } body createdAt url path originalCommit { oid } } } } }"
    " } } }"
)


def redact(message):
    value = re.sub(r"(?:https?|ssh)://[^\s'\"]+", "[远端地址]", str(message))
    value = re.sub(r"(?i)((?:token|password|api[_-]?key|secret)\s*[=:]\s*)[^\s'\"]+",
                   r"\1«redacted»", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "«redacted»", value)
    return util.redact(value, secrets=[os.environ.get(key) for key in ("GH_TOKEN", "GITHUB_TOKEN")])


class GhError(Exception):
    def __init__(self, kind, message):
        self.kind, self.message = kind, redact(message)
        super().__init__(self.message)


def gh_env() -> dict:
    return dict(os.environ, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
                NO_COLOR="1", GH_PAGER="", GH_HOST="github.com")


def _read_command(args):
    """Only our structured PR view, REST GET and the one GraphQL read may retry."""
    if (len(args) == 7 and args[:2] == ["pr", "view"] and args[3] == "--repo"
            and args[5] == "--json" and not args[2].startswith("-")):
        return True
    if args == ["api", "user"]:
        return True
    if args[:2] == ["api", "graphql"]:
        # A GraphQL query has no side effect, but only our exact constant query is
        # allowed to retry — never an arbitrary caller-supplied mutation.
        fields = {}
        index = 2
        while index < len(args):
            if args[index] not in ("-f", "-F") or index + 1 >= len(args):
                return False
            key, sep, value = args[index + 1].partition("=")
            if not sep or not key or key in fields:
                return False
            fields[key] = value
            index += 2
        return (fields.get("query") == THREAD_QUERY
                and set(fields) == {"query", "owner", "name", "number"}
                and re.fullmatch(r"[A-Za-z0-9_.-]+", fields["owner"]) is not None
                and re.fullmatch(r"[A-Za-z0-9_.-]+", fields["name"]) is not None
                and fields["number"].isdigit())
    if len(args) < 2 or args[0] != "api" or not args[1].startswith("repos/"):
        return False
    # No fields/input: gh otherwise silently switches a REST request to POST.
    options = args[2:]
    job_log = re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/jobs/[1-9][0-9]*/logs", args[1])
    escape_flag = False
    i = 0
    while i < len(options):
        option = options[i]
        if option == "--allow-escape-sequences":
            if not job_log or escape_flag:
                return False
            escape_flag = True
            i += 1
            continue
        if i + 1 >= len(options):
            return False
        if option not in ("-H", "--header") and not (
                option in ("--method", "-X") and options[i + 1] == "GET"):
            return False
        i += 2
    return True


def _connection_failure(error):
    # Match gh's Go HTTP transport error, not arbitrary server prose containing EOF.
    if re.search(r"\bHTTP\s+[1-5][0-9]{2}\b", error, re.IGNORECASE):
        return False
    return bool(re.search(
        r'(?im)^(?:graphql:\s*)?(?:get|post|head) "https?://[^"\r\n]+": '
        r'(?:EOF|unexpected EOF|net/http: TLS handshake timeout|'
        r'(?:read|write|dial) tcp[^\r\n]*: (?:connection reset by peer|broken pipe|i/o timeout|connection refused))\s*$',
        error))


def _run(args, *, json_output=True, read_only=False, binary=False):
    if read_only and not _read_command(args):
        raise GhError("rejected", "此 GitHub 命令不允许只读重试")
    for attempt in range(3 if read_only else 1):
        try:
            result = subprocess.run([constants.gh_bin(), *args], capture_output=True, text=not binary,
                                    env=gh_env(), timeout=60)
        except FileNotFoundError as exc:
            # Missing binaries and process timeouts are environment failures, not read retries.
            raise GhError("missing", "未找到 gh 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhError("timeout", "GitHub 调用超时") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise GhError("transient", "GitHub 调用失败：" + str(exc)) from exc
        if not result.returncode:
            break
        stderr = result.stderr.decode(errors="replace") if binary else result.stderr
        error = stderr.lower()
        kind = "auth" if any(s in error for s in ("authentication", "not logged", "http 401", "http 403")) else (
            "not_found" if "not found" in error or "http 404" in error else (
            "rejected" if "validation failed" in error or any(code in error for code in
                ("http 400", "http 405", "http 409", "http 422")) else "transient"))
        if read_only and kind == "transient" and attempt < 2 and _connection_failure(stderr):
            time.sleep(0.25 * (attempt + 1))
            continue
        raise GhError(kind, "GitHub 调用失败：" + redact(stderr)[-2048:])
    if binary:
        return result.stdout
    if not json_output:
        return None
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise GhError("transient", "GitHub 返回了无效 JSON") from exc


def _repo(repo):
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise GhError("rejected", "GitHub 仓库标识无效")
    return repo


def _pr(value, *, number_required=True):
    if not isinstance(value, dict) or value.get("state") not in ("OPEN", "CLOSED", "MERGED"):
        raise GhError("transient", "GitHub PR 状态不可确认")
    try:
        parts = urlsplit(str(value.get("url") or ""))
    except ValueError as exc:
        raise GhError("rejected", "GitHub PR 地址无效") from exc
    if (parts.scheme != "https" or parts.netloc != "github.com" or parts.query or parts.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", parts.path)):
        raise GhError("rejected", "GitHub PR 地址无效或包含凭据")
    if number_required and (type(value.get("number")) is not int or value["number"] < 1):
        raise GhError("transient", "GitHub PR 编号不可确认")
    if not isinstance(value.get("headRefOid"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", value["headRefOid"]):
        raise GhError("transient", "GitHub PR 提交不可确认")
    if (any(not isinstance(value.get(key), str) or not value[key] for key in ("baseRefName", "headRefName"))
            or type(value.get("isCrossRepository")) is not bool):
        raise GhError("transient", "GitHub PR 分支来源不可确认")
    if value["state"] == "MERGED" and not util.parse_ts(value.get("mergedAt")):
        raise GhError("transient", "GitHub PR 合并时间不可确认")
    return value


def pr_list(repo_slug, head_branch) -> list:
    values = _run(["pr", "list", "--repo", _repo(repo_slug), "--head", head_branch,
                   "--state", "all", "--json", PR_FIELDS])
    if not isinstance(values, list):
        raise GhError("transient", "GitHub PR 列表不可确认")
    return [_pr(value) for value in values]


def run_rerun_failed(repo_slug, run_id):
    """Rerun only the failed jobs of one exact workflow run."""
    if type(run_id) is not int or run_id < 1:
        raise GhError("rejected", "工作流运行编号无效")
    # gh run rerun has no --json: it prints a human line on a terminal and nothing
    # otherwise. A zero exit is the acceptance signal; never parse the output.
    _run(["run", "rerun", str(run_id), "--repo", _repo(repo_slug), "--failed"], json_output=False)


def pr_view(repo_slug, number, *, merge_checks=False, merge_commit=False) -> dict:
    fields = PR_MERGE_FIELDS if merge_checks else PR_FIELDS
    if merge_commit:
        fields += ",mergeCommit"
    return _pr(_run(["pr", "view", str(number), "--repo", _repo(repo_slug),
                    "--json", fields], read_only=True))


def pr_merge(repo_slug, number, *, sha) -> dict:
    """GitHub atomically compares sha; never bypass protection or choose a fallback."""
    if type(number) is not int or number < 1 or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise GhError("rejected", "合并 PR 编号或提交无效")
    value = _run(["api", "--method", "PUT", f"repos/{_repo(repo_slug)}/pulls/{number}/merge",
                  "-f", "sha=" + sha, "-f", "merge_method=merge"])
    if not isinstance(value, dict) or type(value.get("merged")) is not bool:
        raise GhError("transient", "GitHub 合并结果不可确认")
    return {"merged": value["merged"], "message": redact(value.get("message") or "")}


def pr_create(repo_slug, *, base, head, title, body) -> dict:
    with tempfile.TemporaryDirectory(prefix="kanpilot-pr-") as root:
        path = Path(root) / "body.md"
        path.write_text(body, encoding="utf-8")
        # gh pr create has no --json flag. Ignore its prose, then read structured data.
        _run(["pr", "create", "--repo", _repo(repo_slug), "--base", base, "--head", head,
              "--title", title, "--body-file", str(path)], json_output=False)
    try:
        return pr_view(repo_slug, head)
    except GhError as exc:
        # The write command succeeded. A later read denial says nothing about
        # whether the PR exists; callers must reconcile rather than resend.
        exc.operation_dispatched = True
        raise


_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_COMMENT_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*#[A-Za-z0-9_-]+")
_OID = re.compile(r"[0-9a-f]{40,64}")


def _comment_author(node):
    author = node.get("author")
    if author is None:
        return None  # 已删除账号：保留评论但永不视为可信作者
    if not isinstance(author, dict) or not isinstance(author.get("login"), str) \
            or not _LOGIN.fullmatch(author["login"]):
        raise GhError("transient", "GitHub 评论作者不可确认")
    return author["login"]


def _comment(node, *, path=False):
    if not isinstance(node, dict) or not isinstance(node.get("body"), str):
        raise GhError("transient", "GitHub 评论内容不可确认")
    identifier, created, url = node.get("databaseId"), node.get("createdAt"), node.get("url")
    if identifier is not None and type(identifier) is not int:
        raise GhError("transient", "GitHub 评论编号不可确认")
    if not util.parse_ts(created) or not isinstance(url, str) or not _COMMENT_URL.fullmatch(url):
        raise GhError("transient", "GitHub 评论时间或地址不可确认")
    value = {"id": identifier, "author": _comment_author(node), "body": node["body"],
             "created_at": created, "url": url}
    if path:
        value["path"] = str(node.get("path") or "")[:400]
        commit = node.get("originalCommit")
        oid = commit.get("oid") if isinstance(commit, dict) else None
        value["commit_oid"] = oid if isinstance(oid, str) and _OID.fullmatch(oid) else None
    return value


def _nodes(container, label):
    if not isinstance(container, dict) or not isinstance(container.get("nodes"), list):
        raise GhError("transient", "GitHub " + label + " 不可确认")
    return container["nodes"]


def _reviews(pr):
    result = []
    for node in _nodes(pr.get("reviews"), "评审"):
        if not isinstance(node, dict) or not isinstance(node.get("state"), str):
            raise GhError("transient", "GitHub 评审状态不可确认")
        result.append(dict(_comment(node), state=node["state"][:40]))
    return result


def _pull_comments(value, repo_slug, number):
    """Strictly normalize one GraphQL read; unknown shapes never become evidence."""
    data = value.get("data") if isinstance(value, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    pr = repository.get("pullRequest") if isinstance(repository, dict) else None
    if (not isinstance(pr, dict) or pr.get("number") != number
            or pr.get("state") not in ("OPEN", "CLOSED", "MERGED")):
        raise GhError("transient", "GitHub PR 评论不可确认")
    head = pr.get("headRefOid")
    if not isinstance(head, str) or not _OID.fullmatch(head) or not isinstance(pr.get("isDraft"), bool):
        raise GhError("transient", "GitHub PR 身份不可确认")
    decision = pr.get("reviewDecision")
    if decision is not None and not isinstance(decision, str):
        raise GhError("transient", "GitHub 评审结论不可确认")
    threads = []
    for thread in _nodes(pr.get("reviewThreads"), "评论线程"):
        if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
                or not isinstance(thread.get("isResolved"), bool)
                or not isinstance(thread.get("isOutdated"), bool)):
            raise GhError("transient", "GitHub 评论线程不可确认")
        threads.append({"id": thread["id"], "resolved": thread["isResolved"],
                        "outdated": thread["isOutdated"],
                        "comments": [_comment(node, path=True)
                                     for node in _nodes(thread.get("comments"), "线程评论")]})
    return {
        "repo": _repo(repo_slug), "number": number, "state": pr["state"],
        "is_draft": pr["isDraft"], "head_sha": head, "review_decision": decision or "",
        "issue_comments": [_comment(node) for node in _nodes(pr.get("comments"), "评论")],
        "reviews": _reviews(pr),
        "review_threads": threads,
    }


def pr_comments(repo_slug, number) -> dict:
    """One bounded GraphQL read of the PR conversation (protocol §5.27.20)."""
    repo = _repo(repo_slug)
    if type(number) is not int or number < 1:
        raise GhError("rejected", "PR 编号无效")
    owner, name = repo.split("/")
    value = _run(["api", "graphql", "-f", "query=" + THREAD_QUERY, "-f", "owner=" + owner,
                  "-f", "name=" + name, "-F", "number=" + str(number)], read_only=True)
    return _pull_comments(value, repo, number)


def viewer_login() -> str:
    """The logged-in gh account — the default trusted comment author."""
    value = _run(["api", "user"], read_only=True)
    login = value.get("login") if isinstance(value, dict) else None
    if not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise GhError("transient", "GitHub 登录账号不可确认")
    return login