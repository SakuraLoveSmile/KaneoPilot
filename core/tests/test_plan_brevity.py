"""Plan brevity contract: the planner prompt must cap plan length for every backend."""
from kanpilot import codex_planner, constants, pi_desktop_planner, planfile


def _prompt() -> str:
    return (constants.core_root() / "executor_prompts" / "planner_prompt.md").read_text(encoding="utf-8")


def test_planner_prompt_states_the_tiered_length_contract():
    text = _prompt()
    assert "先定档再写，篇幅与风险成比例" in text
    assert "60 行" in text and "180 行" in text
    # 禁止项必须点名历史轮次与旧计划的篇幅继承
    assert "不复述材料、历史轮次或旧计划" in text
    assert "不列证据清单" in text


def test_every_backend_inherits_the_contract():
    # Plus 后端：附加指令不得再要求“自包含/文件级步骤”而无篇幅约束
    assert "篇幅与内容按上文「先定档再写」契约" in pi_desktop_planner._PLUS_INSTRUCTION
    # Codex spec_plan 后端：技能模板被显式限制
    assert "篇幅按 KaneoPilot 的定档契约" in codex_planner.SPEC_PROMPT_PREFIX
    assert "不要套用技能里的完整章节模板" in codex_planner.SPEC_PROMPT_PREFIX




def test_manual_acceptance_section_is_parsed_and_never_blocks():
    fence = chr(96) * 3
    plan = ("# 计划\n\n## 步骤\n\n改 lib/a.dart\n\n"
            "## 人工验收（不阻塞）\n\n"
            "- 真机播放：手机上切歌确认音量\n"
            "* 跨平台：Windows 听感对比\n"
            "\n## 风险\n\n- 无\n")
    assert planfile.manual_acceptance(plan) == [
        "真机播放：手机上切歌确认音量",
        "跨平台：Windows 听感对比",
    ]


def test_manual_acceptance_absent_or_empty_is_no_items():
    assert planfile.manual_acceptance("# 计划\n\n普通正文\n") == []
    assert planfile.manual_acceptance("## 人工验收（不阻塞）\n\n- 无\n") == []


def test_review_and_planner_prompts_state_manual_acceptance_never_blocks():
    root = constants.core_root() / "executor_prompts"
    for name in ("review.md", "review_cli.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "人工验收" in text and "待体验" in text, name
        assert "不构成" in text or "不算阻塞" in text or "不阻塞" in text, name
    planner = (root / "planner_prompt.md").read_text(encoding="utf-8")
    assert "## 人工验收（不阻塞）" in planner
    assert "不作为完成前提" in planner


def test_delivery_kind_defaults_to_source_and_parses_evidence_only():
    assert planfile.delivery_kind("# 计划\n\n## 交付类型\n\n源码改动\n") == "source"
    assert planfile.delivery_kind("# 计划\n\n## 交付类型\n\n证据/记录收尾\n") == "evidence_only"
    assert planfile.delivery_kind("# 计划\n\n## 交付类型\n\n只补证据、不改源码\n") == "source"
    # 没有该段 = 源码交付（默认），不能默认放行空 diff
    assert planfile.delivery_kind("# 计划\n\n## 步骤\n\n改 a.dart\n") == "source"
    # 其他章节里的相似措辞不算声明
    assert planfile.delivery_kind("# 计划\n\n## 风险\n\n证据收尾风险\n") == "source"


def test_body_line_count_ignores_frontmatter_and_blank_lines():
    plan = b"---\ntask_id: t\n---\n\n# title\n\nstep\n"
    assert planfile.body_line_count(plan) == 2
    # 无 frontmatter 时按全文计
    assert planfile.body_line_count(b"# a\n\nb\n") == 2
