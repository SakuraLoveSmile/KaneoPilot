"""正文链接识别（protocol §5.22.1）：只收链接语法，跨源链接不阻塞派发。

2026-11-23：反馈正文里引用了一个被截断的中文域名地址（
``https://watch.%E4%B8%8A%E6%9...``），原实现把正文里所有裸 URL 都当成待
下载附件，跨源拒下后变成 issue，自动派发因此永远停在 validation_failed。
"""
import unittest
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import bridge, db, planner_inputs

PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class TestDescriptionLinks(unittest.TestCase):
    def links(self, description="", comments=()):
        source = {"task": {"description": description},
                  "comments": [{"content": text} for text in comments]}
        return planner_inputs._description_links(source)

    def test_prose_url_is_not_material(self):
        # 反馈正文引用报错地址：它是叙述内容，不是附件。
        self.assertEqual(self.links(
            "地址栏显示 https://watch.%E4%B8%8A%E6%9... 这种百分号编码"), [])

    def test_markdown_and_html_links_keep_order_and_kind(self):
        text = ("看图 ![shot](https://kaneo.test/api/asset/a1) 和 "
                "[日志](https://kaneo.test/api/asset/a2) 还有 "
                '<img width="667" src="https://kaneo.test/api/asset/a3">')
        self.assertEqual(
            [(link["where"], link["kind"], link["url"].rsplit("/", 1)[-1])
             for link in self.links(text)],
            [("description", "image", "a1"),
             ("description", "file", "a2"),
             ("description", "image", "a3")])

    def test_duplicates_and_non_http_links_are_dropped(self):
        links = self.links("![x](https://kaneo.test/api/asset/a1)",
                           ["[a1](https://kaneo.test/api/asset/a1)",
                            "[z](ftp://nope/a4)"])
        self.assertEqual([(link["where"], link["url"]) for link in links],
                         [("description", "https://kaneo.test/api/asset/a1")])


class TestPreparePlanInputLinks(KanpilotTestCase):
    """prepare-plan-input：跨源只备注，同源失效仍报错。"""

    def write_png(self, url, dest, max_bytes=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(PNG_HEAD)
        return dest

    def prepare(self, task_id, description):
        task = self.fake.add_task(task_id, "fixture-project-feedback",
                                  status="to-do", description=description)
        self.make_repo("Feedback")
        with mock.patch.object(bridge, "load_credentials",
                               return_value=("https://kaneo.test/", "token")), \
             mock.patch.object(bridge, "download_attachment", self.write_png):
            prepared = self.call("prepare-plan-input", {"task_id": task["id"]})
        conn = db.connect(self.db_path)
        try:
            manifest = db.planner_input_manifest(
                db.get_planner_input(conn, prepared["input_id"]))
        finally:
            conn.close()
        return prepared, manifest

    def test_prose_and_cross_origin_links_do_not_block(self):
        prepared, manifest = self.prepare(
            "tasklink00000000001",
            "地址栏显示 https://watch.%E4%B8%8A%E6%9... 这种编码；"
            "另见 [外部文档](https://example.com/doc.png) 与 "
            "![shot](https://kaneo.test/api/asset/shot-1)")
        self.assertTrue(prepared["can_start"])
        self.assertEqual(prepared["issues"], [])
        self.assertEqual(manifest["issues"], [])
        self.assertEqual([link["url"] for link in manifest["skipped_links"]],
                         ["https://example.com/doc.png"])
        images = [f for f in manifest["files"] if f["source"] == "description:image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["media_type"], "image/png")

    def test_same_origin_link_failure_still_blocks(self):
        def fail(url, dest, max_bytes=None):
            raise bridge.BridgeError("附件下载失败：HTTP 404")

        task = self.fake.add_task(
            "tasklink00000000002", "fixture-project-feedback", status="to-do",
            description="![shot](https://kaneo.test/api/asset/missing)")
        self.make_repo("Feedback")
        with mock.patch.object(bridge, "load_credentials",
                               return_value=("https://kaneo.test/", "token")), \
             mock.patch.object(bridge, "download_attachment", fail):
            prepared = self.call("prepare-plan-input", {"task_id": task["id"]})
        self.assertFalse(prepared["can_start"])
        self.assertEqual(prepared["issues"][0]["url"],
                         "https://kaneo.test/api/asset/missing")
