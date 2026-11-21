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


# A bad mid-run edit must keep the last good cap: never the memory-derived
# default, never None (unlimited). Review finding on PR #264 (HF-789).

_INVALID_CAP_LINES = {
    "abc": "  max_in_progress: abc\n",
    "zero": "  max_in_progress: 0\n",
    "negative": "  max_in_progress: -3\n",
    "empty_string": '  max_in_progress: ""\n',
    "empty_key": "  max_in_progress:\n",
}

_MTIME = [1_700_000_000_000_000_000]


def _write_kanban(home: Path, body: str) -> None:
    cfg = home / "config.yaml"
    cfg.write_text("kanban:\n" + body if body else "{}\n", encoding="utf-8")
    _MTIME[0] += 100_000_000_000
    os.utime(cfg, ns=(_MTIME[0], _MTIME[0]))


@pytest.fixture
def capture_caps(monkeypatch):
    seen: list = []

    def fake_dispatch_once(conn, **kwargs):
        seen.append(kwargs.get("max_in_progress"))
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    return seen


def _booted_dispatcher(home: Path, body: str) -> _KanbanDispatcher:
    from hermes_cli.config import load_config

    _write_kanban(home, body)
    kanban_cfg = (load_config() or {}).get("kanban", {})
    return _KanbanDispatcher(kb, _resolve_dispatcher_settings(kanban_cfg, kb))


@pytest.mark.parametrize("bad", sorted(_INVALID_CAP_LINES))
@pytest.mark.parametrize("derived", [99, None], ids=["derived99", "memory_unknown"])
def test_invalid_edit_keeps_last_good_cap(hermes_home, monkeypatch, capture_caps, bad, derived):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: derived)
    dispatcher = _booted_dispatcher(hermes_home, "  max_in_progress: 2\n")

    dispatcher.tick_once()
    _write_kanban(hermes_home, _INVALID_CAP_LINES[bad])
    dispatcher.tick_once()
    dispatcher.tick_once()

    assert capture_caps == [2, 2, 2]


def test_bad_edits_keep_cap_then_valid_edit_applies(hermes_home, monkeypatch, capture_caps, caplog):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 99)
    dispatcher = _booted_dispatcher(hermes_home, "  max_in_progress: 3\n")
    caplog.set_level("WARNING")

    dispatcher.tick_once()
    _write_kanban(hermes_home, _INVALID_CAP_LINES["abc"])
    dispatcher.tick_once()
    dispatcher.tick_once()
    _write_kanban(hermes_home, _INVALID_CAP_LINES["zero"])
    dispatcher.tick_once()
    _write_kanban(hermes_home, "  max_in_progress: 6\n")
    dispatcher.tick_once()

    assert capture_caps == [3, 3, 3, 3, 6]
    warnings = [r.getMessage() for r in caplog.records if "invalid kanban.max_in_progress" in r.getMessage()]
    # One warning per distinct bad value, not one per tick.
    assert len(warnings) == 2, warnings
    assert "'abc'" in warnings[0] and "keeping last good cap 3" in warnings[0]
    assert "0" in warnings[1]


def test_removed_key_falls_back_to_derived_default(hermes_home, monkeypatch, capture_caps):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 4)
    dispatcher = _booted_dispatcher(hermes_home, "  max_in_progress: 2\n")

    dispatcher.tick_once()
    _write_kanban(hermes_home, "")
    dispatcher.tick_once()

    assert capture_caps == [2, 4]


def test_removed_key_with_unknown_memory_keeps_last_cap(hermes_home, monkeypatch, capture_caps):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: None)
    dispatcher = _booted_dispatcher(hermes_home, "  max_in_progress: 2\n")

    dispatcher.tick_once()
    _write_kanban(hermes_home, "")
    dispatcher.tick_once()

    assert capture_caps == [2, 2]


def test_invalid_at_boot_uses_derived_default_then_valid_applies(hermes_home, monkeypatch, capture_caps):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 5)
    dispatcher = _booted_dispatcher(hermes_home, _INVALID_CAP_LINES["abc"])

    dispatcher.tick_once()
    _write_kanban(hermes_home, "  max_in_progress: 1\n")
    dispatcher.tick_once()
    _write_kanban(hermes_home, _INVALID_CAP_LINES["negative"])
    dispatcher.tick_once()

    assert capture_caps == [5, 1, 1]


def test_config_read_error_keeps_last_good_cap(hermes_home, monkeypatch, capture_caps):
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: None)
    dispatcher = _booted_dispatcher(hermes_home, "  max_in_progress: 2\n")
    dispatcher.tick_once()

    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(cfgmod, "load_config_readonly", boom)
    dispatcher.tick_once()

    assert capture_caps == [2, 2]
