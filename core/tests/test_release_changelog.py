import json

from kanpilot import release_delivery


def _task(tmp_path, source=None, plan=None):
    task = {"task_id": "t1"}
    if source is not None:
        path = tmp_path / "source.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        task["source_path"] = str(path)
    if plan is not None:
        path = tmp_path / "plan.md"
        path.write_text(plan, encoding="utf-8")
        task["plan_path"] = str(path)
    return task


def test_reporter_and_one_sentence_summary(tmp_path):
    plan = '''---
task_id: t1
---

## 目标

轻档，采用隐藏方案：移除截图中的六张统计卡片；补充证据。首页导航保留。

## 步骤

1. x
'''
    task = _task(tmp_path, source={"task": {"assigneeName": "上杉九月"}}, plan=plan)
    assert release_delivery._changelog_reporter(task) == "上杉九月"
    assert release_delivery._changelog_summary(task) == "移除截图中的六张统计卡片"
    assert release_delivery._changelog_context(task) == {
        "task_id": "t1", "reporter": "上杉九月", "summary": "移除截图中的六张统计卡片"}


def test_summary_drops_the_dangling_colon_before_a_list(tmp_path):
    plan = '''---
task_id: t1
---

## 目标

用户在“一起看”页填写自建服务地址后，不再出现无信息量的“一起看操作失败，请稍后重试”：

1. 反代前缀缺少 v1 时自动补全。
2. 服务端错误码都有中文提示。

## 步骤

1. x
'''
    task = _task(tmp_path, plan=plan)
    assert release_delivery._changelog_summary(task) == (
        "用户在“一起看”页填写自建服务地址后，不再出现无信息量的“一起看操作失败，请稍后重试”")


def test_missing_or_broken_materials_are_empty(tmp_path):
    assert release_delivery._changelog_context({"task_id": "t1"}) == {"task_id": "t1"}
    broken_source = tmp_path / "source.json"
    broken_source.write_text("not json", encoding="utf-8")
    broken_plan = tmp_path / "plan.md"
    broken_plan.write_text("## 目标\n\n\n## 步骤\n", encoding="utf-8")
    task = {"task_id": "t1", "source_path": str(broken_source), "plan_path": str(broken_plan)}
    assert release_delivery._changelog_context(task) == {"task_id": "t1"}


def test_inputs_attach_reporter_and_summary_to_root_member():
    batch = dict(
        batch_id="b", repository="owner/repo", project_id="p", root_task_id="root",
        target_sha="a" * 40,
        members=[
            dict(task_id="root", run_id="run", plan_sha256="b" * 64,
                 requirements_sha256="c" * 64, head_sha="d" * 40),
            dict(task_id="fix", run_id="run2", plan_sha256="e" * 64,
                 requirements_sha256="f" * 64, head_sha="1" * 40),
        ],
        policy=dict(channel="prerelease", required_assets=["asset"]),
        version="0.3.9", tag="v0.3.9-pilot.1", build_number=1,
        changelog={"task_id": "root", "reporter": "上杉九月", "summary": "隐藏统计区"},
    )
    manifest, inputs = release_delivery._inputs(batch)
    assert manifest["members"][0]["reporter"] == "上杉九月"
    assert manifest["members"][0]["summary"] == "隐藏统计区"
    assert "reporter" not in manifest["members"][1]
    assert "summary" not in manifest["members"][1]
    assert json.loads(inputs["task_manifest_json"]) == manifest
    assert inputs["task_manifest_sha256"]
