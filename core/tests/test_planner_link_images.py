"""§5.22.1：描述/评论链接下载的图片按魔数补 media_type 与扩展名。

Kaneo 资源地址 ``/api/asset/<id>`` 没有扩展名；以前登记为 ``media_type=null``
且无扩展名，Codex/Claude（按扩展名）与 Plus（按类型/扩展名）都收不到截图。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import KanpilotTestCase
from kanpilot import codex_planner, pi_desktop_planner, planner_inputs

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
TEXT = "构建日志：plain text\n".encode("utf-8")

BODIES = {
    "pngasset": PNG,           # 无扩展名 → 补 .png
    "photo.jpeg": JPEG,        # 扩展名已匹配 → 保留原名
    "webpasset": WEBP,         # 无扩展名 → 补 .webp
    "mislabeled.jpg": PNG,     # 扩展名与内容不符 → 追加 .png
    "notes": TEXT,             # 非图片 → 原名、media_type=null
}
BASE = "https://kaneo.example/api/asset/"
DESCRIPTION = (
    f"截图：![shot]({BASE}pngasset)\n"
    f"照片：![p]({BASE}photo.jpeg)\n"
    f'<img src="{BASE}webpasset">\n'
    f"旧图：![m]({BASE}mislabeled.jpg)\n"
    f"日志：[notes]({BASE}notes)\n"
)


def fake_download(url, dest, max_bytes=0, timeout=30.0):
    Path(dest).write_bytes(BODIES[url.rsplit("/", 1)[-1]])
    return dest


class TestSniffImage(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="kanpilot-sniff-")
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def sniff(self, data: bytes):
        path = self.dir / "blob"
        path.write_bytes(data)
        return planner_inputs._sniff_image(path)

    def test_known_formats(self):
        self.assertEqual(self.sniff(PNG), ("image/png", (".png",)))
        self.assertEqual(self.sniff(JPEG), ("image/jpeg", (".jpg", ".jpeg")))
        self.assertEqual(self.sniff(WEBP), ("image/webp", (".webp",)))

    def test_non_images_are_not_guessed(self):
        self.assertIsNone(self.sniff(TEXT))
        self.assertIsNone(self.sniff(b""))
        self.assertIsNone(self.sniff(b"RIFF\x24\x00\x00\x00WAVEfmt "))
        self.assertIsNone(self.sniff(b"\x89PNG"))
        self.assertIsNone(self.sniff(b"GIF89a" + b"\x00" * 32))
        self.assertIsNone(self.sniff(b"RIFF\x24\x00\x00\x00WEB"))
        self.assertIsNone(planner_inputs._sniff_image(self.dir / "missing"))


class TestLinkImageMaterials(KanpilotTestCase):
    def prepare(self, description=DESCRIPTION, comments=None, downloader=fake_download,
                attachments=None, local_paths=None):
        task_id = "linkimagetask0000001"
        self.fake.add_task(task_id, "fixture-project-feedback",
                           description=description, attachments=attachments or [])
        self.make_repo("Feedback")
        original_request = self.fake.request

        def request(path, method="GET", body=None):
            if path == f"/api/comment/{task_id}" and method == "GET":
                return {"data": comments or []}
            return original_request(path, method, body)

        with mock.patch.object(planner_inputs.bridge, "download_attachment",
                               side_effect=downloader), mock.patch.object(
                                   self.fake, "request", side_effect=request):
            prepared = self.call("prepare-plan-input", {
                "task_id": task_id, "local_paths": local_paths or [],
            })
        input_dir = Path(prepared["input_dir"])
        manifest = json.loads((input_dir / "manifest.json").read_text(
            encoding="utf-8"))
        links = {entry["source"] + "|" + entry["name"]: entry
                 for entry in manifest["files"]
                 if entry["source"].startswith(("description:", "comment["))}
        return prepared, input_dir, manifest, links

    def assert_file_content(self, input_dir, entry, expected):
        path = input_dir / entry["path"]
        self.assertTrue(path.is_file(), entry)
        self.assertEqual(path.name, entry["name"])
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(entry["size"], len(expected))
        self.assertEqual(entry["sha256"], hashlib.sha256(expected).hexdigest())

    def assert_backends_pick_images(self, input_dir, manifest, links, expected):
        images = {Path(path).name
                  for path in codex_planner.image_files(manifest, input_dir)}
        # Codex 经 -i 传图；Claude 按同一份扩展名经 Read 读图。
        self.assertEqual(images, expected)
        plus_images = {entry["name"] for entry in links.values()
                       if pi_desktop_planner._is_image(entry["media_type"],
                                                       entry["name"])}
        self.assertEqual(plus_images, images)

    def test_images_get_media_type_and_extension(self):
        prepared, input_dir, _manifest, links = self.prepare()
        self.assertTrue(prepared["can_start"], prepared["issues"])
        expected = {
            "description:image|pngasset.png": "image/png",
            "description:image|photo.jpeg": "image/jpeg",
            "description:image|webpasset.webp": "image/webp",
            "description:image|mislabeled.jpg.png": "image/png",
            "description:file|notes": None,
        }
        self.assertEqual({key: entry["media_type"]
                          for key, entry in links.items()}, expected)
        for entry, content in zip(links.values(), BODIES.values()):
            self.assert_file_content(input_dir, entry, content)
        # 改名后不留无扩展名的旧文件。
        self.assertFalse((input_dir / "files" / "pngasset").exists())
        self.assertFalse((input_dir / "files" / "webpasset").exists())

    def test_all_backends_pick_up_link_images(self):
        _prepared, input_dir, manifest, links = self.prepare()
        self.assert_backends_pick_images(input_dir, manifest, links, {
            "pngasset.png", "photo.jpeg", "webpasset.webp", "mislabeled.jpg.png",
        })

    def test_comment_images_get_types_and_keep_original_bytes(self):
        prepared, input_dir, manifest, links = self.prepare(
            description="", comments=[{"content": DESCRIPTION}])
        self.assertTrue(prepared["can_start"], prepared["issues"])
        self.assertEqual({entry["name"]: entry["media_type"]
                          for entry in links.values()}, {
            "pngasset.png": "image/png", "photo.jpeg": "image/jpeg",
            "webpasset.webp": "image/webp", "mislabeled.jpg.png": "image/png",
            "notes": None,
        })
        self.assertTrue(all(entry["source"].startswith("comment[0]:")
                            for entry in links.values()))
        for entry, content in zip(links.values(), BODIES.values()):
            self.assert_file_content(input_dir, entry, content)
        self.assert_backends_pick_images(input_dir, manifest, links, {
            "pngasset.png", "photo.jpeg", "webpasset.webp", "mislabeled.jpg.png",
        })

    def test_appended_extensions_do_not_overwrite_existing_materials(self):
        urls = [BASE + "shot.png", BASE + "shot", BASE + "shot?second"]
        bodies = dict(zip(urls, (PNG + b"first", PNG + b"second", PNG + b"third")))

        def download(url, dest, **kwargs):
            Path(dest).write_bytes(bodies[url])
            return dest

        prepared, input_dir, manifest, links = self.prepare(
            description=f"![one]({urls[0]})",
            comments=[{"body": f"![two]({urls[1]})"}, f"![three]({urls[2]})"],
            downloader=download)
        self.assertTrue(prepared["can_start"], prepared["issues"])
        self.assertEqual([entry["name"] for entry in links.values()],
                         ["shot.png", "shot-1.png", "shot-2.png"])
        # API 评论经 canonical 排序：字符串评论先于对象评论。
        expected_bodies = {
            "shot.png": bodies[urls[0]],
            "shot-1.png": bodies[urls[2]],
            "shot-2.png": bodies[urls[1]],
        }
        for entry in links.values():
            self.assertEqual(entry["media_type"], "image/png")
            self.assert_file_content(input_dir, entry, expected_bodies[entry["name"]])
        self.assertEqual({path.name for path in (input_dir / "files").iterdir()},
                         {"shot.png", "shot-1.png", "shot-2.png"})
        self.assert_backends_pick_images(input_dir, manifest, links,
                                        {"shot.png", "shot-1.png", "shot-2.png"})

    def test_attachment_and_local_path_type_rules_remain_unchanged(self):
        local = self.root / "local-image"
        local.write_bytes(PNG)
        prepared, input_dir, manifest, links = self.prepare(
            description="", attachments=[{
                "id": "attached", "name": "mislabeled.jpg",
                "url": BASE + "mislabeled.jpg", "contentType": "image/jpeg",
            }], local_paths=[str(local)])
        self.assertTrue(prepared["can_start"], prepared["issues"])
        self.assertEqual(links, {})
        entries = {entry["source"]: entry for entry in manifest["files"]}
        self.assertEqual(entries["attachment"]["name"], "mislabeled.jpg")
        self.assertEqual(entries["attachment"]["media_type"], "image/jpeg")
        self.assertEqual(entries["local"]["name"], "local-image")
        self.assertIsNone(entries["local"]["media_type"])
        for source in ("attachment", "local"):
            self.assert_file_content(input_dir, entries[source], PNG)


if __name__ == "__main__":
    unittest.main()
