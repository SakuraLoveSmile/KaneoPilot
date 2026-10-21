"""Real isolated gh subprocesses: retry reads, never repeat mutations or bad data."""
import base64
import json
import os
import sys
from types import SimpleNamespace

import pytest

from kanpilot import gh_client as gh, parent_closeout_github as closeout, release_github as release


def response(value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return dict(stdout=base64.b64encode(raw).decode())


@pytest.fixture
def isolated_gh(tmp_path, monkeypatch):
    executable = tmp_path / "fixture-gh"
    calls = tmp_path / "calls.jsonl"
    plan = tmp_path / "responses.json"
    executable.write_text("#!" + sys.executable + "\n" + '''import base64,json,os,sys
from pathlib import Path
log=Path(os.environ["READ_FIXTURE_CALLS"])
index=len(log.read_text().splitlines()) if log.exists() else 0
with log.open("a") as handle: handle.write(json.dumps(sys.argv[1:])+"\\n")
steps=json.loads(Path(os.environ["READ_FIXTURE_PLAN"]).read_text())
step=steps[min(index,len(steps)-1)]
sys.stderr.write(step.get("error",""))
sys.stdout.buffer.write(base64.b64decode(step.get("stdout","")))
sys.exit(1 if "error" in step else 0)
''')
    executable.chmod(0o755)
    # Both the configured executable and PATH fallback are local, fail-closed fixtures.
    poison = tmp_path / "gh"
    poison.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(77)\n")
    poison.chmod(0o755)
    monkeypatch.setenv("KANPILOT_GH_BIN", str(executable))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("READ_FIXTURE_CALLS", str(calls))
    monkeypatch.setenv("READ_FIXTURE_PLAN", str(plan))
    delays = []
    # subprocess also sleeps while waiting on POSIX; observe only gh retries.
    monkeypatch.setattr(gh, "time", SimpleNamespace(sleep=delays.append))

    def configure(steps):
        plan.write_text(json.dumps(steps))
        if calls.exists():
            calls.unlink()
    return configure, lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else [], delays


def pr():
    return dict(number=7, url="https://github.com/owner/repo/pull/7", state="OPEN",
                headRefOid="a" * 40, baseRefName="main", headRefName="fix", isCrossRepository=False)


READS = [lambda: release.run("owner/repo", 7), lambda: closeout.pr("owner/repo", 7)]
ERRORS = ['Get "https://api.github.com/repos/owner/repo": EOF\n',
          'Get "https://api.github.com/repos/owner/repo": net/http: TLS handshake timeout\n']


@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("error", ERRORS)
def test_connection_failure_then_success(isolated_gh, read, error):
    configure, calls, delays = isolated_gh
    configure([dict(error=error), response(pr())])
    assert read() == pr()
    assert len(calls()) == 2
    assert delays == [0.25]


def test_reads_recover_on_third_attempt_and_stop_at_three(isolated_gh):
    configure, calls, delays = isolated_gh
    configure([dict(error=ERRORS[0]), dict(error=ERRORS[1]), response({"id": 7})])
    assert release.run("owner/repo", 7) == {"id": 7}
    assert len(calls()) == 3 and delays == [0.25, 0.5]
    configure([dict(error=ERRORS[0])])
    delays.clear()
    with pytest.raises(gh.GhError) as failure:
        release.run("owner/repo", 7)
    assert failure.value.kind == "transient"
    assert len(calls()) == 3 and delays == [0.25, 0.5]
    assert "https://" not in str(failure.value)


@pytest.mark.parametrize("error,kind", [("HTTP 401 authentication failed: EOF", "auth"),
    ("HTTP 403 forbidden: EOF", "auth"), ("HTTP 404 not found: EOF", "not_found"),
    ("HTTP 422 validation failed: EOF", "rejected"), ("HTTP 500 server error: EOF", "transient"),
    ("unrecognized response: EOF", "transient"),
    ('Get "https://api.github.com": x509: certificate signed by unknown authority', "transient")])
def test_non_connection_errors_are_single_attempt(isolated_gh, error, kind):
    configure, calls, delays = isolated_gh
    configure([dict(error=error), response(pr())])
    with pytest.raises(gh.GhError) as failure:
        closeout.pr("owner/repo", 7)
    assert failure.value.kind == kind
    assert len(calls()) == 1 and delays == []


@pytest.mark.parametrize("value", [b"not json", {"state": "UNKNOWN"}, dict(pr(), headRefOid="wrong")])
def test_json_and_pr_identity_errors_do_not_retry(isolated_gh, value):
    configure, calls, delays = isolated_gh
    configure([response(value), response(pr())])
    with pytest.raises(gh.GhError):
        closeout.pr("owner/repo", 7)
    assert len(calls()) == 1 and delays == []


def test_closeout_rest_reads_retry_only_transport_not_snapshot_validation(isolated_gh):
    configure, calls, delays = isolated_gh
    commit = dict(sha="a" * 40, commit=dict(tree=dict(sha="b" * 40)))
    tree = dict(sha="b" * 40, truncated=False, tree=[])
    configure([dict(error=ERRORS[0]), response(commit), dict(error=ERRORS[1]), response(tree)])
    assert closeout.commit("owner/repo", "main")["sha"] == "a" * 40
    assert len(calls()) == 4 and delays == [0.25, 0.25]
    configure([response(commit), response(dict(tree, truncated=True)), response(tree)])
    delays.clear()
    with pytest.raises(gh.GhError):
        closeout.commit("owner/repo", "main")
    assert len(calls()) == 2 and delays == []


@pytest.mark.parametrize("error", ERRORS)
def test_binary_get_retries_connection_but_keeps_size_validation(isolated_gh, error):
    configure, calls, delays = isolated_gh
    configure([dict(error=error), response(b"evidence")])
    assert release._binary("repos/owner/repo/releases/assets/7", 8) == b"evidence"
    assert len(calls()) == 2 and delays == [0.25]
    configure([response(b"oversized"), response(b"small")])
    delays.clear()
    with pytest.raises(gh.GhError) as failure:
        release._binary("repos/owner/repo/releases/assets/7", 8)
    assert failure.value.kind == "rejected"
    assert len(calls()) == 1 and delays == []


def test_compare_read_retries_and_pr_graphql_transport_is_read_only(isolated_gh):
    configure, calls, delays = isolated_gh
    configure([dict(error=ERRORS[0]), response(dict(status="ahead", base_commit=dict(sha="a" * 40),
        merge_base_commit=dict(sha="a" * 40)))])
    assert closeout.contains("owner/repo", "a" * 40, "b" * 40)
    assert len(calls()) == 2 and delays == [0.25]
    configure([dict(error='Post "https://api.github.com/graphql": EOF\n'), response(pr())])
    delays.clear()
    assert closeout.pr("owner/repo", 7) == pr()
    assert len(calls()) == 2 and delays == [0.25]


@pytest.mark.parametrize("mutation", [
    lambda: gh.pr_merge("owner/repo", 7, sha="a" * 40),
    lambda: gh.pr_create("owner/repo", base="main", head="fix", title="test", body="body"),
    lambda: release.dispatch("owner/repo", 7, "main", {})])
@pytest.mark.parametrize("error", ERRORS)
def test_mutations_stay_single_attempt(isolated_gh, mutation, error):
    configure, calls, delays = isolated_gh
    configure([dict(error=error), response(pr())])
    with pytest.raises(gh.GhError):
        mutation()
    assert len(calls()) == 1 and delays == []


@pytest.mark.parametrize("args", [
    ["api", "repos/owner/repo/test", "--method", "POST"],
    ["api", "repos/owner/repo/test", "-XPUT"],
    ["api", "repos/owner/repo/test", "--input", "body.json"],
    ["api", "repos/owner/repo/test", "-f", "key=value"],
    ["pr", "merge", "7", "--repo", "owner/repo"],
    ["api", "graphql", "-f", "query=mutation {}"]])
def test_read_only_flag_cannot_mark_mutation_as_read(isolated_gh, args):
    configure, calls, delays = isolated_gh
    configure([dict(error=ERRORS[0])])
    with pytest.raises(gh.GhError) as failure:
        gh._run(args, read_only=True)
    assert failure.value.kind == "rejected"
    assert calls() == [] and delays == []
