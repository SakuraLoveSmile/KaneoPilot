import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from kanpilot import gh_client as gh


def pr(state="OPEN", number=7):
    return {"number": number, "url": f"https://github.com/owner/repo/pull/{number}",
            "state": state, "headRefOid": "a" * 40,
            "baseRefName": "main", "headRefName": "kaneo/task1234", "isCrossRepository": False,
            "mergedAt": "2026-10-05T00:00:00+00:00" if state == "MERGED" else None}


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    script = tmp_path / "configured-gh"
    log = tmp_path / "calls.jsonl"
    script.write_text("#!" + sys.executable + "\n" + '''import json,os,sys
from pathlib import Path
args=sys.argv[1:]
entry={"binary":sys.argv[0],"argv":args,"env":{k:os.environ.get(k) for k in ("GH_PROMPT_DISABLED","GH_NO_UPDATE_NOTIFIER","NO_COLOR","GH_PAGER")}}
if "--body-file" in args:
    entry["body"]=Path(args[args.index("--body-file")+1]).read_text()
with open(os.environ["FAKE_GH_LOG"],"a") as h: h.write(json.dumps(entry)+"\\n")
if os.environ.get("FAKE_GH_ERROR"):
    print(os.environ["FAKE_GH_ERROR"],file=sys.stderr);sys.exit(1)
if args[1]=="create": print("Do not parse this human output");sys.exit(0)
value=json.loads(os.environ["FAKE_GH_PR"])
print(json.dumps([value] if args[1]=="list" else value))
''')
    script.chmod(0o755)
    # Fail closed if code ignores KANPILOT_GH_BIN; no real gh can be reached.
    poison = tmp_path / "gh"
    poison.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(77)\n")
    poison.chmod(0o755)
    monkeypatch.setenv("KANPILOT_GH_BIN", str(script))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_PR", json.dumps(pr()))
    return log


def test_fake_cli_structured_argv_env_and_body(fake_gh):
    assert gh.pr_list("owner/repo", "kaneo/task1234") == [pr()]
    assert gh.pr_create("owner/repo", base="main", head="kaneo/task1234",
                        title="[Kaneo] test", body="first\nsecond\n") == pr()
    assert gh.pr_view("owner/repo", 7) == pr()
    entries = [json.loads(line) for line in fake_gh.read_text().splitlines()]
    assert [e["argv"][1] for e in entries] == ["list", "create", "view", "view"]
    assert all(e["argv"][e["argv"].index("--repo") + 1] == "owner/repo" for e in entries)
    assert all(e["env"] == {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
                            "NO_COLOR": "1", "GH_PAGER": ""} for e in entries)
    assert entries[1]["body"] == "first\nsecond\n"
    assert entries[2]["argv"][2] == "kaneo/task1234"
    assert "--json" in entries[0]["argv"] and "--json" in entries[2]["argv"]
    assert "--json" not in entries[1]["argv"]
    for entry in (entries[0], entries[2], entries[3]):
        assert set(entry["argv"][entry["argv"].index("--json") + 1].split(",")) == set(gh.PR_FIELDS.split(","))


def test_env_binary_override_all_pr_commands(fake_gh, monkeypatch):
    # This launches actual child processes and records their executable/argv.
    # PATH's gh is a failing local sentinel, not the user's installed binary.
    selected = os.environ["KANPILOT_GH_BIN"]
    assert gh.pr_list("owner/repo", "kaneo/task1234") == [pr()]
    assert gh.pr_create("owner/repo", base="main", head="kaneo/task1234",
                        title="[Kaneo] override", body="body\n") == pr()
    assert gh.pr_view("owner/repo", 7) == pr()
    entries = [json.loads(line) for line in fake_gh.read_text().splitlines()]
    assert [entry["argv"][1] for entry in entries] == ["list", "create", "view", "view"]
    assert all(entry["binary"] == selected for entry in entries)
    assert all(entry["argv"][entry["argv"].index("--repo") + 1] == "owner/repo" for entry in entries)
    assert entries[1]["body"] == "body\n"


@pytest.mark.parametrize("error,kind", [("authentication failed HTTP 401", "auth"),
    ("HTTP 404 not found", "not_found"), ("HTTP 422 validation failed", "rejected"),
    ("connection reset", "transient")])
def test_classification_redacts_urls(fake_gh, monkeypatch, error, kind):
    monkeypatch.setenv("FAKE_GH_ERROR", error + " https://user:secret@github.com token=secret123456")
    with pytest.raises(gh.GhError) as caught:
        gh.pr_view("owner/repo", 7)
    assert caught.value.kind == kind
    assert "secret" not in caught.value.message and "https://" not in caught.value.message


def test_timeout_and_malformed_json():
    def timeout(argv, **kw):
        assert kw["timeout"] == 60 and isinstance(argv, list)
        raise subprocess.TimeoutExpired(argv, 60)
    with patch.object(gh.subprocess, "run", side_effect=timeout), pytest.raises(gh.GhError) as caught:
        gh.pr_list("owner/repo", "branch")
    assert caught.value.kind == "timeout"   # P1: timeout is ladder-2 environmental, not transient
    with patch.object(gh.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "bad json", "")):
        with pytest.raises(gh.GhError, match="JSON"):
            gh.pr_view("owner/repo", 1)


def test_known_token_and_host_isolation(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "opaque-credential-value")
    monkeypatch.setenv("GH_HOST", "other-host.invalid")
    error = gh.GhError("auth", "opaque-credential-value ghp_fakeSecret github_pat_fakeSecret")
    assert "credential" not in error.message and "fakeSecret" not in error.message
    assert gh.gh_env()["GH_HOST"] == "github.com"


@pytest.mark.parametrize("changes", [{"url": "https://user:password@github.com/owner/repo/pull/7"},
    {"url": "https://[github.com/owner/repo/pull/7"},
    {"url": "https://github.com/owner/repo/pull/7?token=secret"}, {"state": "UNKNOWN"},
    {"number": True}, {"headRefOid": None}, {"headRefOid": "invalid"},
    {"baseRefName": None}, {"headRefName": ""}, {"isCrossRepository": None}, {"isCrossRepository": 0}, {"state": "MERGED", "mergedAt": None}])
def test_rejects_untrusted_pr_data(changes):
    with pytest.raises(gh.GhError):
        gh._pr(dict(pr(), **changes))
