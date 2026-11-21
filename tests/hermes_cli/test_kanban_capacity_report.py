"""A dispatcher at its spawn cap says so; it is not "stuck" (HF-788).

With ``kanban.max_in_progress`` (or ``max_spawn``) filled by running workers a tick
returns before it looks at ready work, so it spawns nothing by design. The
embedded dispatcher counted that as an unhealthy tick and, after six, logged
"dispatcher stuck ... Check profile health", naming none of the holds that
``describe_suppression`` knows. The tick now records which cap is full and how
full, ``describe_suppression`` names it, and an all-boards-capped queue logs an
INFO line instead of the health warning.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fill(conn, workers: int) -> None:
    for n in range(workers):
        tid = kb.create_task(conn, title=f"busy-{n}", assignee="alice")
        assert kb.claim_task(conn, tid) is not None


def _never_spawn(*_args, **_kwargs):
    raise AssertionError("a tick at capacity must not spawn")


def test_a_full_max_in_progress_names_the_cap_and_its_fill(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        _fill(conn, 2)
        kb.create_task(conn, title="waits for a slot", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=_never_spawn, max_in_progress=2)

    assert res.spawned == []
    assert res.at_capacity == ("kanban.max_in_progress", 2, 2)
    assert kbd.describe_suppression([res]) == "at_capacity=2/2 (kanban.max_in_progress)"
    assert kbd.describe_capacity([res]) == "2/2 (kanban.max_in_progress)"


def test_the_host_cap_counts_workers_on_every_board(kanban_home, all_assignees_spawnable):
    kb.create_board("second")
    with kbc.connect(board="second") as other:
        _fill(other, 2)
    with kbc.connect() as conn:
        _fill(conn, 1)
        res = kbd.dispatch_once(conn, spawn_fn=_never_spawn, max_in_progress=3)

    assert res.at_capacity == ("kanban.max_in_progress", 3, 3)


def test_a_full_max_spawn_names_that_cap(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        _fill(conn, 1)
        res = kbd.dispatch_once(conn, spawn_fn=_never_spawn, max_spawn=1)

    assert kbd.describe_suppression([res]) == "at_capacity=1/1 (kanban.max_spawn)"


def test_headroom_is_not_capacity(kanban_home, all_assignees_spawnable):
    spawned: list[str] = []
    with kbc.connect() as conn:
        _fill(conn, 1)
        kb.create_task(conn, title="fits", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: spawned.append(task.id) or 42,
                                max_in_progress=2)

    assert len(spawned) == 1
    assert res.at_capacity is None
    assert kbd.describe_suppression([res]) == ""
    assert kbd.describe_capacity([res]) == ""


def test_one_board_under_its_cap_keeps_the_stuck_diagnosis():
    capped = kbd.DispatchResult(at_capacity=("kanban.max_spawn", 1, 1))
    free = kbd.DispatchResult()

    assert kbd.describe_capacity([capped, capped]) == "1/1 (kanban.max_spawn)"
    assert kbd.describe_capacity([capped, free]) == ""
    assert kbd.describe_capacity([None, capped]) == "1/1 (kanban.max_spawn)"
    assert kbd.describe_capacity([]) == ""
    # Still named when mixed with other holds, so the line explains the whole tick.
    assert kbd.describe_suppression([capped, kbd.DispatchResult(memory_pressure="elevated")]) == (
        "at_capacity=1/1 (kanban.max_spawn), memory_pressure=elevated")


def _run_embedded_dispatcher(monkeypatch, caplog, *, kanban_cfg: dict, ticks: int) -> list[logging.LogRecord]:
    """Drive the real embedded dispatcher loop over the real board for ``ticks`` ticks."""
    import hermes_cli.config as cfg_mod
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"kanban": kanban_cfg})

    class Runner(GatewayKanbanWatchersMixin):
        _running = True
        seen = 0

        async def _sleep_between_ticks(self, interval: float) -> None:
            self.seen += 1
            self._running = self.seen < ticks

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", no_wait)
    caplog.set_level(logging.INFO, logger="gateway.run")  # the gateway's shared logger
    asyncio.run(asyncio.wait_for(Runner()._kanban_dispatcher_watcher(), timeout=60))
    return [r for r in caplog.records if "kanban dispatcher" in r.getMessage()]


def test_embedded_dispatcher_at_capacity_logs_the_cap_not_a_health_warning(
        kanban_home, all_assignees_spawnable, monkeypatch, caplog):
    with kbc.connect() as conn:
        _fill(conn, 2)
        kb.create_task(conn, title="waits for a slot", assignee="alice")

    records = _run_embedded_dispatcher(
        monkeypatch, caplog, kanban_cfg={"dispatch_interval_seconds": 1, "max_in_progress": 2}, ticks=8)

    messages = [(r.levelno, r.getMessage()) for r in records]
    assert not any("stuck" in text or "profile health" in text for _level, text in messages), messages
    [(level, text)] = [m for m in messages if "at capacity" in m[1]]
    assert level == logging.INFO
    assert "running 2/2 (kanban.max_in_progress)" in text


def test_embedded_dispatcher_still_warns_when_it_is_stuck_for_another_reason(
        kanban_home, all_assignees_spawnable, monkeypatch, caplog):
    """Headroom, ready work, nothing spawned (memory pressure): the health warning stays."""
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "critical")
    with kbc.connect() as conn:
        kb.create_task(conn, title="starved", assignee="alice")

    records = _run_embedded_dispatcher(
        monkeypatch, caplog, kanban_cfg={"dispatch_interval_seconds": 1, "max_in_progress": 4}, ticks=8)

    [warning] = [r for r in records if r.levelno == logging.WARNING]
    assert "dispatcher stuck" in warning.getMessage()
    assert "memory_pressure=critical" in warning.getMessage()
    assert not any("at capacity" in r.getMessage() for r in records)
