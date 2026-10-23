"""Local-only origin probes distinguish uncertainty from invalid configuration."""
import subprocess

import pytest

from kanpilot import git_delivery as gd


@pytest.fixture
def origin_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    for name in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/Owner/Repo.git"], check=True, capture_output=True)
    return repo


def test_alias_slow_local_startup_is_not_an_invalid_repository(monkeypatch):
    """A 150ms local startup exceeded the former 100ms alias budget."""
    def run(args, **kwargs):
        if args[0] == "git":
            if kwargs["timeout"] < .15:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return subprocess.CompletedProcess(args, 1, b"", b"")
        assert args[:3] == ["ssh", "-G", "-oCanonicalizeHostname=no"]
        return subprocess.CompletedProcess(args, 0, "hostname ssh.github.com\nuser git\nport 443\n", "")
    for name in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gd.subprocess, "run", run)
    assert gd.github_repository("git@github-personal:Owner/Repo.git", cwd="repo") == "Owner/Repo"


@pytest.mark.parametrize("stage", ["origin", "config", "ssh"])
@pytest.mark.parametrize("failure,code", [
    (subprocess.TimeoutExpired("private command", 5, stderr="token=private-token"), "git_probe_timeout"),
    (OSError("private command token=private-token"), "git_probe_failed"),
    (FileNotFoundError("private command token=private-token"), "git_unavailable"),
])
def test_strict_probe_keeps_transient_errors_private(origin_repo, monkeypatch, stage, failure, code):
    real_run = subprocess.run
    def run(args, **kwargs):
        command = "ssh" if args[0] == "ssh" else ("config" if "config" in args else "origin")
        if command == stage:
            raise failure
        if command == "origin":
            return subprocess.CompletedProcess(args, 0, b"git@github-personal:Owner/Repo.git\n", b"")
        if command == "config":
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return real_run(args, **kwargs)
    monkeypatch.setattr(gd.subprocess, "run", run)
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.require_github_origin(str(origin_repo))
    assert error.value.code == code
    assert "private" not in str(error.value)


def test_strict_origin_distinguishes_missing_invalid_and_success(origin_repo):
    assert gd.require_github_origin(str(origin_repo), check_push=True) == "Owner/Repo"
    subprocess.run(["git", "-C", str(origin_repo), "remote", "set-url", "origin",
                    "https://example.invalid/Owner/Repo.git"], check=True, capture_output=True)
    with pytest.raises(gd.GitDeliveryError) as invalid:
        gd.require_github_origin(str(origin_repo))
    assert invalid.value.code == "origin_not_github"
    subprocess.run(["git", "-C", str(origin_repo), "remote", "remove", "origin"],
                   check=True, capture_output=True)
    with pytest.raises(gd.GitDeliveryError) as missing:
        gd.require_github_origin(str(origin_repo))
    assert missing.value.code == "origin_missing"


def test_strict_probe_nonzero_is_not_an_invalid_origin(origin_repo, monkeypatch):
    monkeypatch.setattr(gd.subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 128, b"", b"private malformed configuration"))
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.require_github_origin(str(origin_repo))
    assert error.value.code == "git_probe_failed"
    assert "private" not in str(error.value)


def test_push_probe_timeout_preserves_uncertainty(origin_repo, monkeypatch):
    real_run = subprocess.run
    def run(args, **kwargs):
        if "--push" in args:
            raise subprocess.TimeoutExpired(args, 5)
        return real_run(args, **kwargs)
    monkeypatch.setattr(gd.subprocess, "run", run)
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.require_github_origin(str(origin_repo), check_push=True)
    assert error.value.code == "git_probe_timeout"


def test_push_alias_timeout_is_not_branch_divergence(origin_repo, monkeypatch):
    subprocess.run(["git", "-C", str(origin_repo), "config", "remote.origin.pushurl",
                    "git@github-personal:Owner/Repo.git"], check=True, capture_output=True)
    real_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == "ssh":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="private config")
        return real_run(args, **kwargs)
    monkeypatch.setattr(gd.subprocess, "run", run)
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.require_github_origin(str(origin_repo), check_push=True)
    assert error.value.code == "git_probe_timeout"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("kind", ["different", "multiple", "rewrite"])
def test_preflight_push_guard_is_read_only(origin_repo, kind):
    def config(*args):
        subprocess.run(["git", "-C", str(origin_repo), "config", *args], check=True, capture_output=True)
    if kind == "different":
        config("remote.origin.pushurl", "https://github.com/Other/Repo.git")
    elif kind == "multiple":
        config("--add", "remote.origin.pushurl", "https://github.com/Owner/Repo.git")
        config("--add", "remote.origin.pushurl", "https://github.com/Other/Repo.git")
    else:
        # Keep fetch origin valid while testing the push-only rewrite chain.
        config("remote.origin.url", "git@github.com:Owner/Repo.git")
        config("remote.origin.pushurl", "alias://delivery")
        config("url.https://github.com/Owner/Repo.git.pushInsteadOf", "alias://delivery")
        config("url.https://example.invalid/Owner/Repo.git.insteadOf", "https://github.com/Owner/Repo.git")
    before = (origin_repo / ".git" / "config").read_bytes()
    with pytest.raises(gd.GitDeliveryError) as error:
        gd.require_github_origin(str(origin_repo), check_push=True)
    assert error.value.code == "branch_diverged"
    assert (origin_repo / ".git" / "config").read_bytes() == before
