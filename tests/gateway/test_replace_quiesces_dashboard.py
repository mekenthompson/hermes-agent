"""Gateway --replace must stop dashboard before the old gateway exits.

A live dashboard holding deleted state.db-wal/shm is the DeletedWalGenerationError
latch. These tests pin the quiesce helper and fail-closed wait.
"""
from __future__ import annotations

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
    import sys
    fake = MagicMock()
    fake.iter_deleted_sqlite_sidecar_holders = lambda path: []
    monkeypatch.setitem(sys.modules, "hermes_state_dbfile", fake)
    assert writers.wait_deleted_sidecar_holders_gone(tmp_path / "state.db", timeout_s=0.2) is True


def test_wait_deleted_sidecar_holders_gone_false_when_stuck(monkeypatch, tmp_path):
    import sys
    fake = MagicMock()
    fake.iter_deleted_sqlite_sidecar_holders = lambda path: [(99, "state.db-wal (deleted)")]
    monkeypatch.setitem(sys.modules, "hermes_state_dbfile", fake)
    assert writers.wait_deleted_sidecar_holders_gone(tmp_path / "state.db", timeout_s=0.2) is False


def test_fail_closed_if_deleted_holders_aborts_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(writers, "wait_deleted_sidecar_holders_gone", lambda db_path, timeout_s=10.0: False)
    assert writers.fail_closed_if_deleted_holders(tmp_path) is False


def test_fail_closed_if_deleted_holders_allows_open(monkeypatch, tmp_path):
    monkeypatch.setattr(writers, "wait_deleted_sidecar_holders_gone", lambda db_path, timeout_s=10.0: True)
    assert writers.fail_closed_if_deleted_holders(tmp_path) is True
