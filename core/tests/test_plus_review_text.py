"""Plus Review raw Markdown transport: quote/backslash fidelity and strict framing."""
import json

import pytest

from kanpilot import pi_desktop_review


def envelope(conclusion="changes_requested", priority="medium", report=None,
             title='修复 "路径"', description=None):
    if report is None:
        report = '## 评审\n`"${baseUrl}${href}"`\n```dart\nfinal p = r"C:\\new\\track";\n```'
    if description is None:
        description = '保留 `\\n` 原文，修复 "路径"。\n增加测试。'
    return (f"<<<KANPILOT_REVIEW_V1>>>\nconclusion: {conclusion}\npriority: {priority}\n"
            f"<<<REPORT>>>\n{report}\n<<<FIX_TITLE>>>\n{title}\n"
            f"<<<FIX_DESCRIPTION>>>\n{description}\n<<<END_KANPILOT_REVIEW_V1>>>")


def parse(text):
    return pi_desktop_review.parse_result([{"role": "assistant", "content": text}])


def test_raw_code_quotes_and_backslashes_round_trip():
    report = '报告包含 `"${baseUrl}${href}"`、`\\n` 与 `C:\\new`。\n```json\n{"k": "v"}\n```'
    description = '原样保留 "引号"、反斜杠 \\ 及换行\n下一行'
    result, error = parse(envelope(report=report, description=description))
    assert error is None
    assert result == {"conclusion": "changes_requested", "report_markdown": report,
                      "fix": {"title": '修复 "路径"', "description": description, "priority": "medium"}}


@pytest.mark.parametrize("conclusion", ["pass", "blocked"])
@pytest.mark.parametrize("wrapper", ["", "```text\n", "```\n"])
def test_non_fix_conclusions(conclusion, wrapper):
    text = envelope(conclusion, "no-priority", title="", description="")
    if wrapper:
        text = wrapper + text + "\n```"
    result, error = parse(text)
    assert error is None
    assert result["conclusion"] == conclusion
    assert result["fix"] is None


@pytest.mark.parametrize("wrapper", ["", "```text\n", "```\n"])
def test_single_frame_allows_outer_narration_without_changing_report(wrapper):
    text = envelope("pass", "no-priority", title="", description="")
    if wrapper:
        text = wrapper + text + "\n```"
    normalizations, metadata = [], {}
    result, error = pi_desktop_review.parse_result([
        {"role": "assistant", "content": "复核完成：冻结材料与提交范围一致。\n\n" + text + "\n本轮复核结束。"},
    ], normalizations=normalizations, metadata=metadata)
    assert error is None
    assert result == parse(envelope("pass", "no-priority", title="", description=""))[0]
    assert normalizations == ["removed_text_frame_commentary"]
    assert metadata == {"result_format": "review_text_v1"}


@pytest.mark.parametrize("outside", [
    '{"conclusion":"blocked","report_markdown":"冲突","fix":null}',
    'conclusion: blocked', 'priority: urgent',
    '  Conclusion = blocked', '"conclusion": "blocked"', "'PRIORITY' = urgent",
    '- conclusion: blocked', '`conclusion`: blocked', '结果 conclusion: blocked',
    '**priority**: urgent',
    '<<<KANPILOT_REVIEW_V2>>>', '<<<REPORT>>>',
    '```json\n{}\n```', '```',
])
@pytest.mark.parametrize("before", [True, False])
def test_outer_structured_or_damaged_results_remain_blocked(outside, before):
    text = envelope()
    text = outside + "\n" + text if before else text + "\n" + outside
    assert parse(text)[0] is None


@pytest.mark.parametrize("change", [
    lambda s: s + "\n" + s,
    lambda s: s.replace("<<<REPORT>>>", "<<<REPORT>>>\n<<<REPORT>>>"),
    lambda s: s.replace("<<<FIX_TITLE>>>", "<<<FIX_DESCRIPTION>>>"),
    lambda s: s.replace("<<<FIX_DESCRIPTION>>>", ""),
    lambda s: s.replace("<<<END_KANPILOT_REVIEW_V1>>>", ""),
    lambda s: s.replace("conclusion: changes_requested", "conclusion: done"),
    lambda s: s.replace("priority: medium", "priority: P0"),
    lambda s: s.replace("priority: medium\n", ""),
    lambda s: s.replace("<<<KANPILOT_REVIEW_V1>>>", "<<<KANPILOT_REVIEW_V2>>>"),
])
def test_ambiguous_incomplete_and_invalid_frames_block(change):
    result, error = parse(change(envelope()))
    assert result is None
    assert error


@pytest.mark.parametrize("conclusion", ["pass", "blocked"])
def test_non_fix_conclusions_cannot_hide_fix_content(conclusion):
    assert parse(envelope(conclusion, "no-priority"))[0] is None
    assert parse(envelope(conclusion, "high", title="", description=""))[0] is None


@pytest.mark.parametrize("field", ["report", "title", "description"])
def test_changes_requested_requires_all_content(field):
    assert parse(envelope(**{field: ""}))[0] is None


def test_malformed_legacy_json_is_not_repaired_or_replaced_by_old_answer():
    broken = '{"conclusion":"changes_requested","report_markdown":"code "quoted"", "fix":null}'
    result, error = pi_desktop_review.parse_result([
        {"role": "assistant", "content": envelope()},
        {"role": "assistant", "content": broken},
    ])
    assert result is None
    assert error


def test_legacy_json_with_marker_in_report_remains_compatible():
    expected = {"conclusion": "pass", "report_markdown": "保留 <<<REPORT>>> 原文", "fix": None}
    assert parse(json.dumps(expected)) == (expected, None)


@pytest.mark.parametrize("damage", [
    lambda text: text.replace("<<<KANPILOT_REVIEW_V1>>>", ""),
    lambda text: text.replace("<<<KANPILOT_REVIEW_V1>>>", "<<<KANPILOT_REVIEW_V2>>>"),
    lambda text: "\n".join(" " + line if line.startswith("<<<") else line for line in text.splitlines()),
    lambda text: "\n".join(line + " " if line.startswith("<<<") else line for line in text.splitlines()),
    lambda text: text.replace("<<<KANPILOT_REVIEW_V1>>>", "前言<<<KANPILOT_REVIEW_V1>>>"),
])
def test_damaged_frame_never_promotes_json_example_in_its_report(damage):
    example = json.dumps({"conclusion": "pass", "report_markdown": "仅为示例", "fix": None})
    text = envelope(report="报告中的示例不能作为本轮结论：\n```json\n" + example + "\n```")
    result, error = parse(damage(text))
    assert result is None
    assert error
