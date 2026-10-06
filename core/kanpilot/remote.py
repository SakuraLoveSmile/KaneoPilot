"""Remote Kaneo access. All network goes through bridge.Client; tests inject
a factory returning a stub client (e.g. bound to http://localhost fixtures)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import bridge

_factory: Optional[Callable[[], Any]] = None


def set_client_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Override client construction (tests only; production uses bridge)."""
    global _factory
    _factory = factory


def get_client() -> Any:
    if _factory is not None:
        return _factory()
    return bridge.Client()


def get_task(client: Any, task_id: str) -> dict:
    return bridge.command_get_task(client, task_id)


def get_task_labels(client: Any, task_id: str) -> list:
    return bridge.get_task_labels(client, task_id)


def find_marker_comment(client: Any, task_id: str, marker_prefix: str) -> Optional[dict]:
    return bridge.find_marker_comment(client, task_id, marker_prefix)


def post_comment(client: Any, task_id: str, content: str) -> dict:
    return bridge.post_comment(client, task_id, content)


def update_status(
    client: Any,
    task_id: str,
    project_id: str,
    status: str,
    expected_current_status: str,
) -> dict:
    return bridge.update_task_status(
        client, task_id, project_id, status, expected_current_status
    )


def create_todo(client: Any, project_id: str, dedupe_key: str, payload: dict) -> dict:
    return bridge.create_todo(client, project_id, dedupe_key, payload)
