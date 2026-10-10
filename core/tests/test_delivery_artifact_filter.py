"""Delivery must not commit build artifacts (sandbox §12.4).

The executor runs the plan's verify commands inside the task worktree, which
necessarily produces ``__pycache__/*.pyc``, ``.pytest_cache/`` and friends.
``commit_worktree`` used to ``git add -A`` everything, so those binaries landed in
the task branch; two task branches then carried different ``.pyc`` content and PR
#2/#3 came back CONFLICTING. Note that the ``.pi/**`` exclusion only applies to
the *effective-diff verdict*, never to what gets committed.

These tests pin: artifacts are excluded from the commit, they stay in the
worktree, the post-commit "clean" check tolerates them, a change set made only of
artifacts is a ``no_changes`` failure, and the committed-file list (the delivery
record) never claims an artifact.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import CORE  # noqa: E402
from kanpilot import constants, git_delivery as gd  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True,
                          env=gd.git_env()).stdout.decode().strip()


def make_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    bare, repo = root / "origin.git", root / "repo"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Delivery Test")
    git(repo, "config", "user.email", "delivery@example.invalid")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return repo


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path)
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
    yield repo
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))


def branch(repo, root, task_id="task1234"):
    return dict(gd.prepare_branch_worktree(str(repo), task_id, base_branch="main",
                                           worktrees_root=root), repository=str(repo))


def commit(delivery, run="run1"):
    return gd.commit_worktree(delivery, task_id="task1234", title="修复反馈",
                              execution_run_id=run, fix_round=0)


def _write_artifacts(worktree):
    """Simulate what the executor's verify run leaves behind."""
    cache = Path(worktree) / "tests" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "test_thing.cpython-311.pyc").write_bytes(b"\x33\x44binary\x00")
    (Path(worktree) / ".pytest_cache" / "v" / "cache").mkdir(parents=True, exist_ok=True)
    (Path(worktree) / ".pytest_cache" / "v" / "cache" / "lastfailed").write_text("{}\n")
    (Path(worktree) / ".DS_Store").write_bytes(b"\x00\x01")


# --------------------------------------------------------------- 纯函数

ARTIFACT_CASES = {
    "src/app.py": False,
    "README.md": False,
    "tests/test_thing.py": False,
    "mybuild.py": False,               # 名字含 build 但不是产物
    "building/main.py": False,         # 目录名含 build 但不是产物
    "distributed.py": False,
    "ds_store": False,                 # 没有前导点，不是 .DS_Store
    "tests/__pycache__/test_thing.cpython-311.pyc": True,
    "__pycache__/": True,
    "pkg/mod/__pycache__/m.pyc": True,
    "a/b/test.pyc": True,
    "app.pyo": True,
    ".pytest_cache/v/cache/lastfailed": True,
    ".mypy_cache/3.11/x.json": True,
    ".ruff_cache/0.14/x": True,
    "node_modules/left-pad/index.js": True,
    "dist/bundle.js": True,
    "build/lib.o": True,
    ".coverage": True,
    "htmlcov/index.html": True,
    "pkg.egg-info/PKG-INFO": True,
    "lib/native.so": True,
    "lib/native.dylib": True,
    "A.class": True,
    ".DS_Store": True,
    ".ds_store": True,
    "Thumbs.db": True,
    ".venv/lib/python3.11/site-packages/x.py": True,
    "venv/pyvenv.cfg": True,
}


@pytest.mark.parametrize("path,expected", sorted(ARTIFACT_CASES.items()))
def test_is_build_artifact(path, expected):
    assert gd.is_build_artifact(path) is expected


def test_deliverable_paths_splits():
    changes = [{"path": path} for path in ARTIFACT_CASES]
    deliverable, artifacts = gd.deliverable_paths(changes)
    assert sorted(deliverable) == sorted(p for p, a in ARTIFACT_CASES.items() if not a)
    assert sorted(artifacts) == sorted(p for p, a in ARTIFACT_CASES.items() if a)
    # 每个路径只归一边，不重不漏。
    assert len(deliverable) + len(artifacts) == len(changes)


def test_artifact_patterns_cover_the_sandbox_offenders():
    for pattern in ("__pycache__/", "*.py[cod]", ".pytest_cache/"):
        assert pattern in constants.DELIVERY_ARTIFACT_PATTERNS


# --------------------------------------------------------------- 集成


def test_commit_excludes_artifacts_and_keeps_them_in_worktree(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    (worktree / "feature.py").write_text("VALUE = 1\n")      # 真交付物
    _write_artifacts(worktree)                               # 执行器留下的产物

    result = commit(delivery)
    head = result["sha"]

    # 提交里只有交付物。
    assert "feature.py" in result["files"]
    assert not any(gd.is_build_artifact(p) for p in result["files"]), result["files"]
    tree = git(worktree, "ls-tree", "-r", "--name-only", head).splitlines()
    assert "feature.py" in tree
    assert not any(gd.is_build_artifact(p) for p in tree), tree

    # 产物仍留在工作树（未被删除、未被提交）。
    assert (worktree / "tests" / "__pycache__" / "test_thing.cpython-311.pyc").is_file()
    assert (worktree / ".DS_Store").is_file()

    # 提交后的干净核验放行产物：否则每次 commit 都会被自己的产物绊倒。
    delivery = dict(delivery, head_sha=head)
    gd.verify_branch_worktree(delivery, require_clean=True, expected_head=head)


def test_clean_check_still_rejects_real_leftovers(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    (worktree / "feature.py").write_text("VALUE = 1\n")
    _write_artifacts(worktree)
    head = commit(delivery)["sha"]

    (worktree / "uncommitted.txt").write_text("leftover\n")
    with pytest.raises(gd.GitDeliveryError, match="未提交改动"):
        gd.verify_branch_worktree(dict(delivery, head_sha=head),
                                  require_clean=True, expected_head=head)


def test_artifacts_only_is_no_changes(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    _write_artifacts(Path(delivery["worktree_path"]))
    with pytest.raises(gd.GitDeliveryError) as exc:
        commit(delivery)
    assert exc.value.code == "no_changes"
    assert "构建产物" in exc.value.message
    # 没有生成提交。
    assert git(Path(delivery["worktree_path"]), "rev-parse", "HEAD") == delivery["head_sha"]


def test_second_round_also_excludes_artifacts(repo, tmp_path):
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    (worktree / "feature.py").write_text("VALUE = 1\n")
    _write_artifacts(worktree)
    first = commit(delivery, run="run1")

    # 修复轮：再改一个交付文件，产物照旧在工作树。
    delivery = dict(delivery, head_sha=first["sha"])
    (worktree / "feature.py").write_text("VALUE = 2\n")
    (worktree / "tests" / "__pycache__" / "test_thing.cpython-311.pyc").write_bytes(
        b"\x99\x88different\x00")
    second = commit(delivery, run="run2")
    assert "feature.py" in second["files"]
    assert not any(gd.is_build_artifact(p) for p in second["files"]), second["files"]
    gd.verify_branch_worktree(dict(delivery, head_sha=second["sha"]),
                              require_clean=True, expected_head=second["sha"])


def test_repo_with_gitignore_commits_only_deliverable(repo, tmp_path):
    """仓库自带 .gitignore 时 add -A 本就不暂存产物；reset 必须是无害空操作。

    `:(exclude)` 对被 .gitignore 忽略的路径会让 `git add` 返回 1（即使暂存结果
    正确），所以实现走 add -A + reset；这条用例钉住 .gitignore 存在时的路径。
    """
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    (worktree / ".gitignore").write_text(".DS_Store\n__pycache__/\n*.pyc\n")
    (worktree / "feature.py").write_text("VALUE = 1\n")
    _write_artifacts(worktree)

    result = commit(delivery)
    assert not any(gd.is_build_artifact(p) for p in result["files"]), result["files"]
    tree = git(worktree, "ls-tree", "-r", "--name-only", result["sha"]).splitlines()
    assert ".gitignore" in tree and "feature.py" in tree
    assert not any(gd.is_build_artifact(p) for p in tree), tree
    gd.verify_branch_worktree(dict(delivery, head_sha=result["sha"]),
                              require_clean=True, expected_head=result["sha"])


def test_rename_and_delete_survive_artifact_filter(repo, tmp_path):
    """重命名/删除的交付路径不能因产物过滤而丢暂存（pathspec 方案的回归点）。"""
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    git(worktree, "mv", "tracked.txt", "renamed file.txt")
    (worktree / "added.py").write_text("NEW = 1\n")
    _write_artifacts(worktree)

    result = commit(delivery)
    # _committed_files 用 --no-renames，重命名会报「删除的旧名 + 新增的新名」两侧，
    # 所以 tracked.txt 出现在 files 里是正确的；关键是树里只剩新名。
    assert "renamed file.txt" in result["files"] and "added.py" in result["files"]
    assert not any(gd.is_build_artifact(p) for p in result["files"]), result["files"]
    tree = git(worktree, "ls-tree", "-r", "--name-only", result["sha"]).splitlines()
    assert "renamed file.txt" in tree and "added.py" in tree
    assert "tracked.txt" not in tree
    assert not any(gd.is_build_artifact(p) for p in tree), tree


def test_push_still_works_with_artifacts_present(repo, tmp_path):
    """推送前的干净核验同样放行产物（push 的 GitHub 目标按既有测法打桩）。"""
    delivery = branch(repo, tmp_path / "worktrees")
    worktree = Path(delivery["worktree_path"])
    (worktree / "feature.py").write_text("VALUE = 1\n")
    _write_artifacts(worktree)
    head = commit(delivery)["sha"]
    bare = tmp_path / "origin.git"
    with patch.object(gd, "github_repository",
                      side_effect=lambda url, **kw: "owner/repo" if url == str(bare)
                      else gd.github_repository(url, **kw)):
        assert gd.push_branch(dict(delivery, head_sha=head),
                              expected_repo="owner/repo") == {
                                  "pushed_sha": head}
    assert git(repo, "rev-parse", "refs/heads/kaneo/task1234") == head
