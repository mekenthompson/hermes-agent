"""Dashboard schema heal must not open a writable SessionDB under a live gateway."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

from hermes_cli.web_server_sessions import _open_session_db_at_path
from hermes_state_common import DEFERRED_INDEX_SQL, SCHEMA_SQL


def _legacy_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(DEFERRED_INDEX_SQL)
        conn.execute("DROP INDEX IF EXISTS idx_sessions_effective_activity")
        conn.execute("ALTER TABLE sessions DROP COLUMN last_activity_at")
        conn.commit()
    finally:
        conn.close()


def test_dashboard_heal_skips_writable_open_when_gateway_holds_store(tmp_path):
    db_path = tmp_path / "state.db"
    _legacy_store(db_path)
    with patch("hermes_state_registry.acquire") as acquire, patch(
        "hermes_state_holders.foreign_state_db_holders",
        return_value=[(99, "hermes gateway run --replace")],
    ):
        db = _open_session_db_at_path(db_path, read_only=True)
        try:
            acquire.assert_not_called()
            assert db.read_only is True
        finally:
            db.close()
