"""Healthy SessionDB.close must disable SQLite's last-connection WAL reset.

Python 3.12+ can arm SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE. Fleet gateways and
dashboards are separate processes on one state.db; a short-lived closer must
not reset -wal/-shm under the live holder.
"""

import sqlite3
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
