"""Healthy SessionDB.close must disable SQLite's last-connection WAL reset.

Python 3.12+ can arm SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE. Fleet gateways and
dashboards are separate processes on one state.db; a short-lived closer must
not reset -wal/-shm under the live holder.
"""

import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

from tests.hermes_state._wal_generation_harness import make_db, pin_wal, require_wal


def test_healthy_close_arms_no_ckpt_on_close(tmp_path, monkeypatch):
    flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    if flag is None or not hasattr(sqlite3.Connection, "setconfig"):
        pytest.skip("Connection.setconfig / SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE unavailable")

    pin_wal(monkeypatch)
    db = make_db(tmp_path / "state.db", "s", "before-close")
    require_wal(db)
    setconfig = db._conn.setconfig
    with patch.object(db._conn, "setconfig", wraps=setconfig) as mock_setconfig:
        db.close()
        armed = [
            call
            for call in mock_setconfig.call_args_list
            if call[0] and call[0][0] == flag and call[0][1] is True
        ]
        assert armed, "healthy close did not disable SQLite's last-connection WAL reset"


def test_close_preserves_sidecars_until_last_in_process_holder(tmp_path, monkeypatch):
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    first = make_db(path, "s", "first")
    second = make_db(path, "s", "second")
    wal, shm = path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")
    require_wal(first)
    generation = (wal.stat().st_ino, shm.stat().st_ino)

    try:
        first.close()
        assert (wal.stat().st_ino, shm.stat().st_ino) == generation
        second.append_message("s", role="user", content="after-first-close")
    finally:
        first.close()
        second.close()

    assert not wal.exists() and not shm.exists(), "the true last close must retire the WAL generation"


def test_close_does_not_reset_wal_under_foreign_writer(tmp_path, monkeypatch):
    pin_wal(monkeypatch)
    path = tmp_path / "state.db"
    db = make_db(path, "s", "before-foreign-writer")
    require_wal(db)
    wal, shm = path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")
    generation = (wal.stat().st_ino, shm.stat().st_ino)
    code = (
        "import sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], timeout=10)\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "print('READY', flush=True)\n"
        "sys.stdin.readline()\n"
        "conn.rollback()\n"
        "conn.close()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "READY"
        db.close()
        assert (wal.stat().st_ino, shm.stat().st_ino) == generation
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        db.close()

    assert not wal.exists() and not shm.exists(), "the last holder should retire the WAL generation"
