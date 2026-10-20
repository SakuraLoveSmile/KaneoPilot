"""Read-only direct task relations come only from persisted review source IDs."""

from helpers import KanpilotTestCase
from kanpilot import db, derive, util


class TaskRelationsTest(KanpilotTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = util.now_dt()

    def add_task(self, task_id, **fields):
        values = dict(task_id=task_id, project_id="project-a", project_name="Music",
                      repository="/isolated/Music", title="Title " + task_id,
                      stage="plan", status="needs_plan", created_at="2026-10-18T00:00:00Z",
                      updated_at="2026-10-18T00:00:00Z")
        values.update(fields)
        self.conn.execute(
            "INSERT INTO tasks (" + ",".join(values) + ") VALUES (" +
            ",".join("?" for _ in values) + ")", tuple(values.values()))
        return db.require_task(self.conn, task_id)

    def relations(self, task_id):
        return derive.task_summary(self.conn, db.require_task(self.conn, task_id),
                                   self.now)["task_relations"]

    def brief(self, task_id, status="待规划", available=True):
        return dict(task_id=task_id, title="Title " + task_id if available else None,
                    display_status=status if available else None, available=available)

    def test_original_without_children_has_explicit_empty_relations(self):
        self.add_task("original")
        self.assertEqual(self.relations("original"), {"parent": None, "children": []})

    def test_bidirectional_direct_relations_are_shared_by_list_and_detail(self):
        self.add_task("original")
        self.add_task("fix", review_source_task_id="original", status="running")
        self.add_task("grandchild", review_source_task_id="fix")
        expected = {"parent": None, "children": [self.brief("fix", "执行中")]}
        self.assertEqual(self.relations("original"), expected)
        self.assertEqual(self.relations("fix"), {
            "parent": self.brief("original"), "children": [self.brief("grandchild")]})
        self.assertEqual(self.task("original")["task_relations"], expected)
        listed = {t["task_id"]: t for t in self.call("list-tasks")["tasks"]}
        self.assertEqual(listed["original"]["task_relations"], expected)

    def test_children_include_saved_records_and_have_stable_creation_order(self):
        self.add_task("original")
        self.add_task("b-fix", review_source_task_id="original", save_state="archived")
        self.add_task("z-fix", review_source_task_id="original", save_state="trashed",
                      created_at="2026-10-17T00:00:00Z")
        self.add_task("a-fix", review_source_task_id="original")
        self.assertEqual(self.relations("original")["children"],
                         [self.brief(i) for i in ("z-fix", "a-fix", "b-fix")])

    def test_parent_archived_or_trashed_remains_available(self):
        for save_state in ("archived", "trashed"):
            with self.subTest(save_state=save_state):
                self.add_task(save_state, save_state=save_state)
                self.add_task(save_state + "-fix", review_source_task_id=save_state)
                self.assertEqual(self.relations(save_state + "-fix")["parent"],
                                 self.brief(save_state))

    def test_unavailable_parent_retains_only_source_identity(self):
        self.add_task("other-project", project_id="project-b")
        self.add_task("other-repository", repository="/isolated/Other")
        self.add_task("purged", save_state="purged")
        for source in ("missing", "other-project", "other-repository", "purged", "self"):
            with self.subTest(source=source):
                child_id = "self" if source == "self" else "fix-" + source
                self.add_task(child_id, review_source_task_id=source)
                self.assertEqual(self.relations(child_id)["parent"],
                                 self.brief(source, available=False))

    def test_children_exclude_cross_scope_self_and_purged(self):
        self.add_task("original", review_source_task_id="original")
        self.add_task("project-child", review_source_task_id="original", project_id="project-b")
        self.add_task("repo-child", review_source_task_id="original", repository="/isolated/Other")
        self.add_task("purged-child", review_source_task_id="original", save_state="purged")
        self.add_task("valid-child", review_source_task_id="original")
        self.assertEqual(self.relations("original")["children"], [self.brief("valid-child")])

    def test_legacy_source_id_needs_no_plan_kind_or_review_key_and_no_guessing(self):
        self.add_task("original", fix_task_id="stale-claim", review_key="shared-key")
        self.add_task("legacy-fix", review_source_task_id="original")
        self.add_task("stale-claim", title="Title original", review_key="shared-key",
                      plan_kind="review_fix")
        self.assertEqual(self.relations("original")["children"], [self.brief("legacy-fix")])
        self.assertEqual(self.relations("stale-claim")["parent"], None)

    def test_read_queries_leave_all_persisted_facts_unchanged(self):
        self.add_task("original", fix_task_id="fix")
        self.add_task("fix", review_source_task_id="original", status="needs_user",
                      failure_code="review_blocked", failure_stage="in_review")
        before = list(self.conn.iterdump())
        self.conn.execute("PRAGMA query_only=ON")
        for task_id in ("original", "fix"):
            derive.task_summary(self.conn, db.require_task(self.conn, task_id), self.now)
            derive.task_detail(self.conn, db.require_task(self.conn, task_id), self.now)
        self.assertEqual(list(self.conn.iterdump()), before)
