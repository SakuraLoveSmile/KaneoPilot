"""Task branch names. Persist the result; never rename registered work."""
import re
from datetime import datetime, timedelta, timezone

from . import util

_TYPES = r"(?:feat|fix|refactor|docs|chore|test)"
_TAIPEI = timezone(timedelta(hours=8))


def new_branch(task_id, task=None, *, kind=None, slug=None, created_at=None):
    values = dict(task) if task is not None else {}
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id):
        raise ValueError("无效的任务编号")
    title = values.get("title") or ""
    prefix = re.match(r"^(" + _TYPES + r")(?:\([^)]*\))?[!:：\s]+", title, re.I)
    kind = kind or ("fix" if values.get("plan_kind") == "review_fix" else
                    prefix.group(1).lower() if prefix else "feat")
    if not re.fullmatch(_TYPES, kind):
        raise ValueError("无效的分支类型")
    text = slug if slug is not None else title[prefix.end():] if prefix else title
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].rstrip("-") or "task"
    stamp = created_at or values.get("created_at") or util.now_iso()
    date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return f"{kind}/{date.astimezone(_TAIPEI):%Y%m%d}-{task_id.lower()}-{text}"


def matches(branch, task_id):
    """Recognize this task, including frozen legacy identities."""
    if not isinstance(branch, str) or not isinstance(task_id, str):
        return False
    return branch == "kaneo/" + task_id or re.fullmatch(
        _TYPES + r"/[0-9]{8}-" + re.escape(task_id.lower()) + r"-[a-z0-9]+(?:-[a-z0-9]+)*", branch) is not None


def reserve(conn, task_id, repository, task=None, *, worktrees_root=None):
    """Freeze the first automatic creation name before Git, including retries."""
    from pathlib import Path
    from . import db
    key = "task_branch_name:" + task_id
    repository = str(Path(repository).expanduser().resolve())
    candidate = None
    if db.get_setting(conn, key) is None and worktrees_root is not None:
        # Upgrade recovery: old auto intake may have created Git state before
        # persisting delivery. Reuse only the exact old task tree, then let the
        # normal adoption checks reject dirty or diverged work.
        from . import git_delivery
        path = Path(worktrees_root).expanduser().resolve() / task_id / "run"
        if path.is_dir() and not path.is_symlink():
            legacy = "kaneo/" + task_id
            git_delivery.verify_branch_worktree({"repository": repository,
                "worktree_path": str(path), "branch": legacy},
                require_clean=False, expected_head=None)
            candidate = legacy
    with db.write_txn(conn):
        saved = db.get_setting(conn, key)
        if saved is None:
            saved = {"repository": repository,
                     "branch": candidate or new_branch(task_id, task, created_at=util.now_iso())}
            db.set_setting(conn, key, saved)
        util.require(isinstance(saved, dict) and saved.get("repository") == repository
                     and matches(saved.get("branch"), task_id),
                     "任务分支命名登记与仓库不符", "worktree_invalid")
    return saved["branch"]
