"""Non-interactive GitHub PR operations; never parse human-readable output."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import constants, util

PR_FIELDS = "number,url,state,headRefOid,mergedAt,baseRefName,headRefName,isCrossRepository"


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


def _run(args, *, json_output=True):
    try:
        result = subprocess.run([constants.gh_bin(), *args], capture_output=True, text=True,
                                env=gh_env(), timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhError("transient", "GitHub 调用失败：" + str(exc)) from exc
    if result.returncode:
        error = result.stderr.lower()
        kind = "auth" if any(s in error for s in ("authentication", "not logged", "http 401", "http 403")) else (
            "not_found" if "not found" in error or "http 404" in error else (
            "rejected" if "validation failed" in error or "http 422" in error else "transient"))
        raise GhError(kind, "GitHub 调用失败：" + redact(result.stderr)[-2048:])
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


def pr_view(repo_slug, number) -> dict:
    return _pr(_run(["pr", "view", str(number), "--repo", _repo(repo_slug),
                    "--json", PR_FIELDS]))


def pr_create(repo_slug, *, base, head, title, body) -> dict:
    with tempfile.TemporaryDirectory(prefix="kanpilot-pr-") as root:
        path = Path(root) / "body.md"
        path.write_text(body, encoding="utf-8")
        # gh pr create has no --json flag. Ignore its prose, then read structured data.
        _run(["pr", "create", "--repo", _repo(repo_slug), "--base", base, "--head", head,
              "--title", title, "--body-file", str(path)], json_output=False)
    return pr_view(repo_slug, head)
