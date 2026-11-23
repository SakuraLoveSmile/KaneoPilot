"""One bounded GraphQL read of a PR conversation; strict, no silent shapes."""
import json
import os
import sys

import pytest

from kanpilot import gh_client as gh, util

GOOD = {"data": {"repository": {"pullRequest": {
    "number": 7, "state": "OPEN", "isDraft": False, "headRefOid": "a" * 40,
    "reviewDecision": None,
    "comments": {"nodes": [{"databaseId": 11, "author": {"login": "sakura"},
                              "body": "please fix", "createdAt": "2026-10-05T02:00:00Z",
                              "url": "https://github.com/owner/repo/pull/7#issuecomment-11"}]},
    "reviews": {"nodes": [{"databaseId": 12, "author": {"login": "sakura"},
                             "state": "CHANGES_REQUESTED", "body": "needs work",
                             "createdAt": "2026-10-05T02:10:00Z",
                             "url": "https://github.com/owner/repo/pull/7#pullrequestreview-12"}]},
    "reviewThreads": {"nodes": [{"id": "T1", "isResolved": False, "isOutdated": True,
                                   "comments": {"nodes": [{
                                       "databaseId": 21, "author": {"login": "sakura"},
                                       "body": "line note", "createdAt": "2026-10-05T02:20:00Z",
                                       "url": "https://github.com/owner/repo/pull/7#discussion_r21",
                                       "path": "src/a.py",
                                       "originalCommit": {"oid": "b" * 40}}]}}]},
}}}}


SCRIPT = '''import json, os, sys\nfrom pathlib import Path\nargs = sys.argv[1:]\nwith open(os.environ["FAKE_GH_LOG"], "a") as handle:\n    handle.write(json.dumps(args) + "\\n")\nif os.environ.get("FAKE_GH_ERROR"):\n    print(os.environ["FAKE_GH_ERROR"], file=sys.stderr)\n    sys.exit(1)\nif args[:2] == ["api", "graphql"]:\n    print(json.dumps(json.loads(os.environ["FAKE_GH_COMMENTS"])))\n    sys.exit(0)\nif args[:2] == ["api", "user"]:\n    print(json.dumps({"login": os.environ.get("FAKE_GH_LOGIN", "sakura")}))\n    sys.exit(0)\nsys.exit(2)\n'''


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    script = tmp_path / "gh-comments"
    log = tmp_path / "calls.jsonl"
    script.write_text("#!" + sys.executable + "\n" + SCRIPT)
    script.chmod(0o755)
    poison = tmp_path / "gh"
    poison.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(77)\n")
    poison.chmod(0o755)
    monkeypatch.setenv("KANPILOT_GH_BIN", str(script))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_COMMENTS", json.dumps(GOOD))
    return log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_pr_comments_normalizes_one_bounded_read(fake_gh):
    value = gh.pr_comments("owner/repo", 7)
    assert value["repo"] == "owner/repo" and value["number"] == 7
    assert value["state"] == "OPEN" and value["is_draft"] is False
    assert value["head_sha"] == "a" * 40 and value["review_decision"] == ""
    assert value["issue_comments"][0] == {
        "id": 11, "author": "sakura", "body": "please fix",
        "created_at": "2026-10-05T02:00:00Z",
        "url": "https://github.com/owner/repo/pull/7#issuecomment-11"}
    assert value["reviews"][0]["state"] == "CHANGES_REQUESTED"
    thread = value["review_threads"][0]
    assert (thread["resolved"], thread["outdated"]) == (False, True)
    assert thread["comments"][0]["path"] == "src/a.py"
    assert thread["comments"][0]["commit_oid"] == "b" * 40
    entry = calls(fake_gh)[0]
    assert entry[:2] == ["api", "graphql"]
    assert "query=" + gh.THREAD_QUERY in entry
    assert "owner=owner" in entry and "name=repo" in entry and "number=7" in entry


def test_viewer_login_is_the_default_trusted_author(fake_gh):
    assert gh.viewer_login() == "sakura"
    assert calls(fake_gh)[0] == ["api", "user"]


def test_only_the_exact_graphql_query_may_retry():
    good = ["api", "graphql", "-f", "query=" + gh.THREAD_QUERY,
            "-f", "owner=o", "-f", "name=r", "-F", "number=7"]
    assert gh._read_command(good) is True
    assert gh._read_command(["api", "user"]) is True
    assert gh._read_command(good[:3] + ["query=query { mutation { x } }"]) is False
    assert gh._read_command(["api", "graphql", "-f", "query=query { x }"]) is False
    assert gh._read_command(good[:-1]) is False
    assert gh._read_command(good + ["-f", "owner=other"]) is False


@pytest.mark.parametrize("change", [
    lambda pr: pr.update(number=8),
    lambda pr: pr.update(state="DRAFT"),
    lambda pr: pr.update(headRefOid="zz"),
    lambda pr: pr.update(isDraft="no"),
    lambda pr: pr["comments"]["nodes"][0].update(url="https://example.com/pull/7"),
    lambda pr: pr["comments"]["nodes"][0].update(createdAt="yesterday"),
    lambda pr: pr["reviewThreads"]["nodes"][0].update(isResolved="yes"),
    lambda pr: pr["reviews"]["nodes"][0].pop("state"),
])
def test_unconfirmed_shapes_are_rejected(fake_gh, monkeypatch, change):
    value = json.loads(json.dumps(GOOD))
    change(value["data"]["repository"]["pullRequest"])
    monkeypatch.setenv("FAKE_GH_COMMENTS", json.dumps(value))
    with pytest.raises(gh.GhError):
        gh.pr_comments("owner/repo", 7)


def test_connection_drop_retries_the_read(fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_ERROR", 'Get "https://api.github.com/graphql": EOF')
    with pytest.raises(gh.GhError) as error:
        gh.pr_comments("owner/repo", 7)
    assert error.value.kind == "transient"
    assert len(calls(fake_gh)) == 3

