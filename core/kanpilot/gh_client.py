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
    """Only our structured PR view and REST GET forms may opt into retries."""
    if (len(args) == 7 and args[:2] == ["pr", "view"] and args[3] == "--repo"
            and args[5] == "--json" and not args[2].startswith("-")):
        return True
    if len(args) < 2 or args[0] != "api" or not args[1].startswith("repos/"):
        return False
    # No fields/input: gh otherwise silently switches a REST request to POST.
    options = args[2:]
    return len(options) % 2 == 0 and all(
        options[i] in ("-H", "--header")
        or (options[i] in ("--method", "-X") and options[i + 1] == "GET")
        for i in range(0, len(options), 2))


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


def pr_view(repo_slug, number, *, merge_checks=False) -> dict:
    return _pr(_run(["pr", "view", str(number), "--repo", _repo(repo_slug),
                    "--json", PR_MERGE_FIELDS if merge_checks else PR_FIELDS], read_only=True))


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
    return pr_view(repo_slug, head)
