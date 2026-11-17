"""Separate native Review quotas, using isolated real files and no executor/model."""
import json
import os
from pathlib import Path

import pytest

from helpers import CORE  # noqa: F401
from kanpilot import constants, pi_desktop_review as review, review_snapshot, util
from test_review_native_evidence import collect, scene  # noqa: F401


MIB = 1024 * 1024


def sized_file(path, size):
    """A real sparse file exercises complete reads without a huge Python fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)
        if size:
            stream.seek(size - 1)
            stream.write(b"Z")
    return path


def test_native_and_generic_quotas_are_distinct():
    assert constants.PLAN_INPUT_FILE_MAX_BYTES == 20 * MIB
    assert constants.PLAN_INPUT_TOTAL_MAX_BYTES == 100 * MIB
    assert constants.NATIVE_REVIEW_EVIDENCE_FILE_MAX_BYTES == 128 * MIB
    assert constants.NATIVE_REVIEW_EVIDENCE_TOTAL_MAX_BYTES == 512 * MIB


@pytest.mark.parametrize("branch", [True, False])
def test_large_native_file_is_copied_whole_and_final_hash_guarded(scene, branch):
    source, evidence, task, _directory = scene
    if not branch:
        task["delivery"] = {"mode": "manual"}
    large = sized_file(evidence / "older" / "full-video.mp4", 21 * MIB + 7)
    original_hash = util.sha256_file(large)
    snapshot = review_snapshot.freeze(source)
    materials = collect(scene)
    manifest = json.loads(Path(materials["files"]["native-evidence-manifest.json"]).read_text())
    # Existing evidence is retained alongside the large recording.
    assert len(manifest["files"]) == 5
    record = next(entry for entry in manifest["files"] if entry["relative_path"].endswith("full-video.mp4"))
    copied = Path(record["material_path"])
    assert copied.stat().st_size == large.stat().st_size == record["size"]
    assert record["sha256"] == original_hash == util.sha256_file(copied)
    assert record["media_type"] == "video/mp4"
    job = {"execution_run_id": task["run_id"], "execution_worktree": str(source),
           "review_root": str(source), "materials": materials["files"]}
    if branch:
        review._freeze_branch_job(job, task, materials)
        guard = lambda: review._guard_branch_job(job, task)
    else:
        review._freeze_manual_job(job, snapshot, materials)
        guard = lambda: review._guard_manual_job(job)
    assert job["material_hashes"][str(copied)] == original_hash
    guard()
    # A same-size mutation at the far end must fail the full-byte digest.
    with copied.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        stream.write(b"X")
    with pytest.raises(util.KanpilotError, match="冻结材料已变化"):
        guard()


def test_native_single_file_exact_128_mib_is_fully_read_and_verified(tmp_path):
    path = sized_file(tmp_path / ".pi/evidence/full.mp4", 128 * MIB)
    entries = review_snapshot.freeze_native_evidence(tmp_path)
    assert len(entries) == 1
    assert len(entries[0]["content"]) == 128 * MIB
    assert entries[0]["content"][-1:] == b"Z"
    assert util.sha256_bytes(entries[0]["content"]) == util.sha256_file(path)
    review_snapshot.verify_native_evidence(tmp_path, entries)


@pytest.mark.parametrize("limit", ["single", "total"])
def test_native_128_or_512_mib_plus_one_is_rejected_before_read(tmp_path, monkeypatch, limit):
    evidence = tmp_path / ".pi/evidence"
    if limit == "single":
        sized_file(evidence / "over.mp4", 128 * MIB + 1)
    else:
        for index in range(4):
            sized_file(evidence / f"part-{index}.mp4", 128 * MIB)
        sized_file(evidence / "over.png", 1)
    def forbidden_read(*args, **kwargs):
        raise AssertionError("Oversize native evidence must be rejected before byte allocation")
    monkeypatch.setattr(review_snapshot, "_read_file", forbidden_read)
    with pytest.raises(util.KanpilotError, match="大小越界"):
        review_snapshot.freeze_native_evidence(tmp_path)
    with pytest.raises(util.KanpilotError, match="大小越界"):
        review_snapshot.verify_native_evidence(tmp_path, [])


def test_native_total_exact_512_mib_retains_all_files_and_rechecks(tmp_path):
    evidence = tmp_path / ".pi/evidence"
    for index in range(4):
        sized_file(evidence / f"part-{index}.mp4", 128 * MIB)
    entries = review_snapshot.freeze_native_evidence(tmp_path)
    assert len(entries) == 4
    assert sum(len(entry["content"]) for entry in entries) == 512 * MIB
    assert all(entry["content"][-1:] == b"Z" for entry in entries)
    review_snapshot.verify_native_evidence(tmp_path, entries)


def test_generic_source_single_file_still_rejects_20_mib_plus_one(scene):
    source, _evidence, _task, _directory = scene
    sized_file(source / "source-input.bin", 20 * MIB + 1)
    with pytest.raises(util.KanpilotError, match="大小越界"):
        review_snapshot.freeze(source)


def test_generic_source_total_exact_100_mib_then_rejects_plus_one(scene):
    source, _evidence, _task, _directory = scene
    for index in range(5):
        sized_file(source / f"generic-{index}.bin", 20 * MIB)
    snapshot = review_snapshot.freeze(source)
    assert sum(len(entry["content"]) for entry in snapshot["untracked"]) == 100 * MIB
    del snapshot
    sized_file(source / "z-over.bin", 1)
    with pytest.raises(util.KanpilotError, match="大小越界"):
        review_snapshot.freeze(source)


@pytest.mark.parametrize("mutation", ["addition", "removal", "content", "link"])
def test_native_recheck_rejects_inventory_or_byte_drift(tmp_path, mutation):
    evidence = tmp_path / ".pi/evidence"
    target = sized_file(evidence / "proof.mp4", 64)
    entries = review_snapshot.freeze_native_evidence(tmp_path)
    if mutation == "addition":
        (evidence / "added.txt").write_text("new evidence")
    elif mutation == "removal":
        target.unlink()
    elif mutation == "link":
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    else:
        with target.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b"X")
    with pytest.raises(util.KanpilotError):
        review_snapshot.verify_native_evidence(tmp_path, entries)


def test_native_recheck_detects_replacement_during_full_file_read(tmp_path, monkeypatch):
    evidence = tmp_path / ".pi/evidence"
    target = sized_file(evidence / "proof.mp4", 64)
    entries = review_snapshot.freeze_native_evidence(tmp_path)
    original = review_snapshot._read_file
    def replace(*args, **kwargs):
        result = original(*args, **kwargs)
        target.unlink()
        target.write_bytes(result["content"])
        return result
    monkeypatch.setattr(review_snapshot, "_read_file", replace)
    with pytest.raises(util.KanpilotError, match="复核期间已变化"):
        review_snapshot.verify_native_evidence(tmp_path, entries)
