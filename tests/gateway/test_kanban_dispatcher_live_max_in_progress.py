"""The embedded gateway dispatcher re-reads ``kanban.max_in_progress`` each tick.

The standalone daemon (:func:`hermes_cli.kanban_db_dispatch.run_daemon`)
already re-resolves the cap every tick, so an operator edit to config.yaml
applies without a restart. The gateway-embedded dispatcher resolved it once at
boot, so changing the cap on a live agent needed a gateway restart (HF-789).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _resolve_dispatcher_settings,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_cap(home: Path, cap: int, *, mtime_ns: int) -> None:
    cfg = home / "config.yaml"
    cfg.write_text(f"kanban:\n  max_in_progress: {cap}\n", encoding="utf-8")
    # Config loads are mtime-cached; pin distinct mtimes so the edit is seen
    # even when both writes land inside one filesystem timestamp tick.
    os.utime(cfg, ns=(mtime_ns, mtime_ns))


def test_config_edit_between_ticks_changes_the_cap(hermes_home, monkeypatch):
    from hermes_cli.config import load_config

    # Keep the memory-derived fallback out of the picture.
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 99)

    seen: list = []

    def fake_dispatch_once(conn, **kwargs):
        seen.append(kwargs.get("max_in_progress"))
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)

    _write_cap(hermes_home, 2, mtime_ns=1_700_000_000_000_000_000)
    kanban_cfg = (load_config() or {}).get("kanban", {})
    settings = _resolve_dispatcher_settings(kanban_cfg, kb)
    assert settings.max_in_progress == 2
    dispatcher = _KanbanDispatcher(kb, settings)

    dispatcher.tick_once()
    _write_cap(hermes_home, 5, mtime_ns=1_700_000_100_000_000_000)
    dispatcher.tick_once()

    assert seen == [2, 5]
