"""Event kind label parity (协议 §5.21.2 记录页可读性).

回归背景：核心自身写入了若干 kind（`round_observe`、`round_start_failed`、
`review_config_updated`、`planner_config_updated`），但 `_EVENT_KIND_CN` 未登记，
记录页把它们显示成「未识别事件（round_observe）」。同时 Swift 侧 `Labels.eventKind`
与核心表存在双向漂移（Swift 缺 `goal_report_evaluated`）。本测试在两侧都钉住：

1. 核心**实际写入**的每个 kind 都在 `_EVENT_KIND_CN` 里有中文标签；
2. `EventItem.summary` 对这些 kind 不返回「未识别事件」；
3. Swift `Labels.eventKind` 与核心表逐项同值（不多、不少、不异值）；
4. 未知 kind 仍原样保留（不猜测、不折叠）。
"""

import pathlib
import re
import unittest

from helpers import CORE  # noqa: F401 - installs isolated core import path
from kanpilot import derive

CORE_DIR = pathlib.Path(CORE).resolve() / "kanpilot"
SWIFT_LABELS = (pathlib.Path(CORE).resolve().parent
                / "macos" / "KaneoPilot" / "Views" / "Components.swift")

# `add_event(conn, now, "<kind>", ...)`：取第三个位置参数的字符串字面量。
_ADD_EVENT = re.compile(
    r'add_event\((?:[^()]|\([^()]*\))*?,\s*,\s*"([a-z_]+)"', re.S)


def emitted_event_kinds() -> set:
    """核心源码里作为 `add_event` 第三位置参数出现的 kind 字面量。"""
    kinds = set()
    for path in sorted(CORE_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        kinds.update(_ADD_EVENT.findall(text))
    return kinds


def swift_event_kind_map() -> dict:
    text = SWIFT_LABELS.read_text(encoding="utf-8")
    body = re.search(
        r'static func eventKind\(_ key: String\?\) -> String \{(.*?)\n    \}',
        text, re.S)
    if body is None:
        raise AssertionError("Components.swift 中找不到 Labels.eventKind")
    return dict(re.findall(r'case "([a-z_]+)": return "([^"]+)"', body.group(1)))


class EventKindLabelParityTests(unittest.TestCase):
    def test_every_emitted_kind_has_a_label(self):
        missing = sorted(emitted_event_kinds() - set(derive._EVENT_KIND_CN))
        self.assertEqual(
            missing, [],
            "核心写入但未登记中文标签的 kind（记录页会显示「未识别事件」）：%s"
            % missing)

    def test_emitted_kinds_render_a_real_summary(self):
        """发出的 kind 不得回落成「未识别事件（…）」。"""
        for kind in sorted(emitted_event_kinds()):
            summary = derive._event_summary(kind, {"reason": "r"})
            self.assertNotIn(
                "未识别事件", summary,
                "kind=%s 的摘要仍是未识别事件：%s" % (kind, summary))

    def test_round_observe_is_readable(self):
        """T4 验收点：`round_observe` 必须可读，不折叠、不断词。"""
        self.assertEqual(derive._EVENT_KIND_CN["round_observe"], "观察者续接")
        summary = derive._event_summary(
            "round_observe", {"run_id": "r1", "status": "running",
                              "detail": "观察进程已就绪"})
        self.assertTrue(summary.startswith("观察者续接"), summary)
        self.assertIn("观察进程已就绪", summary)

    # `lost_detected` 是文档化的展示层概念（docs/state-model.md：不落库，以租约
    # 派生为准），核心不会写入，因此允许 Swift 单侧存在；其余键必须逐项同值。
    DISPLAY_ONLY = {"lost_detected"}

    def test_swift_map_matches_core_exactly(self):
        swift_map = swift_event_kind_map()
        core_map = derive._EVENT_KIND_CN
        self.assertEqual(
            {k: v for k, v in swift_map.items() if k not in self.DISPLAY_ONLY},
            core_map,
            "Swift Labels.eventKind 与核心 _EVENT_KIND_CN 已漂移")
        self.assertEqual(sorted(set(swift_map) - set(core_map)),
                         sorted(self.DISPLAY_ONLY),
                         "Swift 多出的键只能是文档化的展示层概念")

    def test_unknown_kind_keeps_its_raw_value(self):
        self.assertEqual(
            derive._event_summary("brand_new_kind", {}),
            "未识别事件（brand_new_kind）")
        # 且确实不在表里——未知就是未知，不猜测。
        self.assertNotIn("brand_new_kind", derive._EVENT_KIND_CN)


if __name__ == "__main__":
    unittest.main()
