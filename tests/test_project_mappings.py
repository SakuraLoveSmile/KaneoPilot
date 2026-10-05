"""Integration test for project mapping CLI commands."""

from __future__ import annotations

import json
import pytest

try:
    from conftest import CLI, PROJECTS, WORKSPACE_ID
except ImportError:
    from tests.conftest import CLI, PROJECTS, WORKSPACE_ID


def test_get_project_mappings_default(cli, tmp_env):
    res = cli("get-project-mappings")
    assert res["version"] == 1
    assert res["revision"] == 0
    assert res["workspace_id"] == WORKSPACE_ID
    assert len(res["items"]) >= 5


def test_set_project_mapping_crud(cli, tmp_env, tmp_path):
    my_repo = tmp_path / "CustomRepo"
    my_repo.mkdir()

    res = cli("set-project-mapping", {
        "project_id": "cust0000000000000001",
        "repository": str(my_repo),
        "enabled": True,
        "expected_revision": 0,
    })
    assert res["revision"] == 1
    assert not res["deduplicated"]
    assert res["item"]["project_id"] == "cust0000000000000001"
    assert res["item"]["mapping_state"] == "available"

    # Fetch again
    res2 = cli("get-project-mappings")
    assert res2["revision"] == 1
    items = {item["project_id"]: item for item in res2["items"]}
    assert "cust0000000000000001" in items
    assert items["cust0000000000000001"]["repository"] == str(my_repo.resolve())
