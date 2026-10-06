"""Kaneo fork response contracts; all clients and roots are isolated.

Source basis: Fork Project/Kaneo/apps/api/src (read on 2026-10-05):
* index.ts mounts label/comment at /api/label and /api/comment.
* label/index.ts + controllers/get-labels-by-task-id.ts:
  GET /api/label/task/{taskId} -> [{id, name, color, taskId, ...}].
* task/controllers/get-tasks.ts + task/response.ts:
  GET /api/task/tasks/{projectId} -> {data:{columns:[{tasks:[{labels:
  [{id,name,color}], ...}]}]},pagination}; no label requests are needed.
* comment/index.ts + controllers/get-comments.ts + response.ts:
  GET /api/comment/{taskId} -> [{id,content,createdAt,...}], oldest first.
* comment/index.ts + schema.ts + activity/response.ts:
  POST /api/comment/{taskId}, {content} -> activity object containing id.
  Timestamps serialize to ISO datetime strings (openapi.ts responseTimestamp).
"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from helpers import KanpilotTestCase
from kanpilot import bridge, remote


class FixtureClient:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def request(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)


class TestLabelsComments(unittest.TestCase):
    def test_get_labels_normal_and_empty(self):
        for labels in ([{"id": "l1", "name": "Bug", "color": "red"}], []):
            with self.subTest(labels=labels):
                client = FixtureClient(labels)
                self.assertEqual(remote.get_task_labels(client, "task/a b"),
                                 [item["name"] for item in labels])
                self.assertEqual(client.calls, [
                    ("/api/label/task/task%2Fa%20b", "GET", None)])

    def test_labels_404_is_error_not_empty(self):
        # Construct transport without reading any credential file; HTTP is stubbed.
        client = bridge.Client.__new__(bridge.Client)
        client.base_url = "http://127.0.0.1:1/"
        client.token = "fixture-token"
        client.timeout = 1
        with patch.object(bridge, "urlopen", side_effect=HTTPError(
            "http://127.0.0.1:1/api/label/task/missing", 404, "missing", {}, None
        )) as request:
            with self.assertRaisesRegex(bridge.BridgeError, "HTTP 404"):
                remote.get_task_labels(client, "missing")
            self.assertEqual(request.call_count, 1)

    def test_invalid_labels_fail_closed(self):
        for value in (None, {}, [None], [{"id": "l1"}], [{"name": None}]):
            with self.subTest(value=value), self.assertRaises(bridge.BridgeError):
                remote.get_task_labels(FixtureClient(value), "task")

    def test_find_marker_earliest_and_preserve_content(self):
        marker = "[kaneopilot:delivery:task]"
        comments = [
            {"id": "new", "content": marker + " new",
             "createdAt": "2026-10-05T01:00:00.000Z"},
            {"id": "user", "content": "mention " + marker,
             "createdAt": "2026-10-01T00:00:00.000Z"},
            {"id": "old", "content": " \n" + marker + " old",
             "createdAt": "2026-10-05T08:00:00+08:00"},
        ]
        client = FixtureClient(comments)
        self.assertEqual(remote.find_marker_comment(client, "task/a", marker), {
            "id": "old", "content": comments[2]["content"],
            "created_at": comments[2]["createdAt"],
        })
        self.assertEqual(client.calls, [("/api/comment/task%2Fa", "GET", None)])

    def test_find_marker_absent_and_empty(self):
        for comments in ([], [{"id": "c", "content": "human feedback"}]):
            with self.subTest(comments=comments):
                self.assertIsNone(remote.find_marker_comment(
                    FixtureClient(comments), "task", "[kaneopilot:delivery:task]"))

    def test_find_marker_invalid_response(self):
        for value in (None, {}, [None], [{"content": None}],
                      [{"id": "c", "content": "[kaneopilot:x]", "createdAt": "bad"}],
                      [{"content": "[kaneopilot:x]", "createdAt": "2026-10-05T00:00:00Z"}]):
            with self.subTest(value=value), self.assertRaises(bridge.BridgeError):
                remote.find_marker_comment(FixtureClient(value), "task", "[kaneopilot:")

    def test_post_comment_verbatim(self):
        client = FixtureClient({"id": "created", "type": "comment"})
        content = " \n[kaneopilot:delivery:task] 已创建 PR：https://example.invalid/1\n"
        self.assertEqual(remote.post_comment(client, "task/a", content), {"id": "created"})
        self.assertEqual(client.calls, [
            ("/api/comment/task%2Fa", "POST", {"content": content})])

    def test_post_comment_response_and_input_validation(self):
        for value in (None, {}, {"id": None}, {"id": ""}):
            with self.subTest(value=value), self.assertRaises(bridge.BridgeError):
                remote.post_comment(FixtureClient(value), "task", "content")
        for content in (None, ""):
            client = FixtureClient({"id": "c"})
            with self.subTest(content=content), self.assertRaises(bridge.BridgeError):
                remote.post_comment(client, "task", content)
            self.assertEqual(client.calls, [])

    def test_remote_wrappers_preserve_bridge_errors(self):
        for call, args in ((remote.get_task_labels, ("task",)),
                           (remote.find_marker_comment, ("task", "[kaneopilot:")),
                           (remote.post_comment, ("task", "content"))):
            error = bridge.BridgeError("Kaneo 请求失败: HTTP 503")
            with self.subTest(call=call), self.assertRaises(bridge.BridgeError) as caught:
                call(FixtureClient(error), *args)
            self.assertIs(caught.exception, error)


def golden_sources():
    # Fixed values captured from the unmodified requirements_hash implementation.
    return [
        ({"task": {"id": "task-1", "projectId": "project-1", "title": "需求标题",
                   "description": "保留\n原文"}, "comments": [], "relations": []},
         "db992de50e396eae1cad8783a790970b85f83f3eff55083dd9c4517056b2d5b1"),
        ({"task": {"id": "task-1", "projectId": "project-1", "title": "需求标题",
                   "description": "保留\n原文", "customFields": [{"fieldId": "f", "value": "v"}],
                   "attachments": [{"id": "a", "url": "https://example.invalid/a"}]},
          "comments": [{"id": "c2", "content": "第二条", "createdAt": "2026-10-02T01:00:00.000Z"},
                       {"id": "c1", "content": "第一条", "createdAt": "2026-10-01T01:00:00.000Z"}],
          "relations": [{"id": "r", "relatedTaskId": "other"}]},
         "568aff0d3bf243db39b604c12af96bcdf7c2c6baa6009560bdd400cf8dc2ecb7"),
        ({"task": {"id": "task-1", "projectId": "project-1", "title": "需求标题",
                   "description": None}, "comments": None, "relations": None},
         "4070e289b3f2b3cf61da91861494324b124c55439fc137733bbd86aa510a56b3"),
    ]


class TestRequirementsCompatibility(unittest.TestCase):
    def test_unmarked_golden_hashes(self):
        for source, expected in golden_sources():
            with self.subTest(expected=expected):
                self.assertEqual(bridge.requirements_hash(source), expected)

    def test_marker_excluded_and_source_not_mutated(self):
        for original, expected in golden_sources()[:2]:
            for prefix in ("", " \t\n", "\u3000"):
                with self.subTest(expected=expected, prefix=prefix):
                    source = copy.deepcopy(original)
                    source["comments"].append({"id": "delivery", "content": prefix +
                        "[kaneopilot:delivery:task-1] 已创建 PR：https://example.invalid/1",
                        "createdAt": "2026-10-05T00:00:00Z"})
                    before = copy.deepcopy(source)
                    self.assertEqual(bridge.requirements_hash(source), expected)
                    self.assertEqual(source, before)

    def test_human_comment_with_embedded_marker_still_changes_hash(self):
        source, expected = golden_sources()[0]
        for content in ("建议 [kaneopilot:x] 修改", "[KaneoPilot:x]", "[kaneopilot]"):
            changed = copy.deepcopy(source)
            changed["comments"].append({"id": "human", "content": content})
            with self.subTest(content=content):
                self.assertNotEqual(bridge.requirements_hash(changed), expected)


class TestListLabels(KanpilotTestCase):
    def test_normalized_todos_labels_without_extra_request(self):
        labels = [{"id": "l1", "name": "Bug", "color": "red"}]
        self.fake.add_task("tasklabels", "example-project-05", labels=labels)
        result = bridge.normalize_todos(self.fake, "workspace")
        item = next(item for project in result["projects"] for item in project["tasks"])
        self.assertEqual(item["labels"], ["Bug"])
        self.assertEqual(item["requirements_sha256"], bridge.requirements_hash(item))
        self.assertFalse(any("/api/label/" in path for _, path, _ in self.fake.requests_log))

    def test_fetch_todos_labels(self):
        self.make_repo("Feedback")
        self.fake.add_task("tasklabels", "example-project-05",
                           labels=[{"id": "l1", "name": "Bug", "color": "red"}])
        items = self.call("fetch-todos", {})["items"]
        self.assertEqual(items[0]["labels"], ["Bug"])
        self.assertFalse(any("/api/label/" in path for _, path, _ in self.fake.requests_log))
