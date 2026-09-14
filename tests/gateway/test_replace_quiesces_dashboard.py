"""Gateway --replace must stop dashboard before the old gateway exits.

A live dashboard holding deleted state.db-wal/shm is the DeletedWalGenerationError
latch. These tests pin the quiesce helper and the start_gateway call order.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import gateway.session_db_writers as writers


def test_hold_supervised_dashboard_down_uses_s6_svc_d(monkeypatch, tmp_path):
    service = tmp_path / "dashboard"
    (service / "supervise").mkdir(parents=True)
    (service / "run").write_text("#!/bin/sh\n")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        result.stdout = b""
        return result

    monkeypatch.setattr(writers, "_DASHBOARD_SERVICE_CANDIDATES", (service,))
    monkeypatch.setattr(writers.subprocess, "run", fake_run)

    held = writers.hold_supervised_dashboard_down()
    assert held == [service]
    assert calls == [["s6-svc", "-d", str(service)]]


def test_quiesce_signals_dashboard_after_s6_hold(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(writers, "hold_supervised_dashboard_down", lambda: [tmp_path / "svc"])
    monkeypatch.setattr(writers, "iter_same_home_dashboard_pids", lambda hermes_home, except_pids=(): [4242])
    signalled = []
    monkeypatch.setattr(writers, "terminate_pids", lambda pids, force=False: signalled.append((list(pids), force)))

    held = writers.quiesce_session_db_writers_for_replace(home)
    assert held == [tmp_path / "svc"]
    assert signalled == [([4242], False)]


def test_wait_deleted_sidecar_holders_gone_true_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        writers, "iter_deleted_sqlite_sidecar_holders", lambda path: [], raising=False,
    )
    # Patch the imported name used inside the waiter via hermes_state_dbfile
    import sys
    fake = MagicMock()
    fake.iter_deleted_sqlite_sidecar_holders = lambda path: []
    monkeypatch.setitem(sys.modules, "hermes_state_dbfile", fake)
    assert writers.wait_deleted_sidecar_holders_gone(tmp_path / "state.db", timeout_s=0.2) is True


def test_start_gateway_source_quiesces_before_replace():
    source = (Path(__file__).resolve().parents[2] / "gateway" / "run.py").read_text(encoding="utf-8")
    start = source.index("async def start_gateway(")
    body = source[start:]
    quiesce_at = body.index("held_dashboard = quiesce_session_db_writers_for_replace")
    replace_at = body.index("await _start_gateway_replace_existing_instance")
    wait_at = body.index("wait_deleted_sidecar_holders_gone(get_hermes_home()")
    assert quiesce_at < replace_at < wait_at
    assert "release_supervised_dashboard(held_dashboard)" in body
