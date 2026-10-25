"""Existing adapter task ownership and shutdown tracking."""
from typing import Any


def _track_session_task(adapter, session_key: str, task: Any) -> bool:
    """Record ``task`` as the session owner and track it for shutdown; False when
    ``create_task`` was stubbed with an unhashable sentinel (tests) — the owner entry is left
    for the caller."""
    adapter._session_tasks[session_key] = task
    try:
        adapter._background_tasks.add(task)
    except TypeError:
        return False
    if hasattr(task, "add_done_callback"):
        task.add_done_callback(adapter._background_tasks.discard)
        task.add_done_callback(adapter._expected_cancelled_tasks.discard)
    return True
