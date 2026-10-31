"""Legacy Desktop/TUI notification polling presents checkpoints without a model turn."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server as notifications


def test_legacy_desktop_checkpoint_is_passive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Desktop update")
        kbn.add_notify_sub(conn, task_id=task_id, platform="tui", chat_id="desktop-session")
        kb.add_comment(conn, task_id, "worker", "\n# Progress\n\nFull body.\n",
                       human_checkpoint=True, request_id="desktop-cp-1")
    emitted = []
    texts = notifications._kb_poll_board(kb, "default", "desktop-session")
    assert texts == ["\n# Progress\n\nFull body.\n"]
    monkeypatch.setattr(notifications, "_collect_kanban_notifications", lambda session: texts)
    monkeypatch.setattr(notifications, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(notifications, "_notif_claim_turn", lambda session: (_ for _ in ()).throw(
        AssertionError("checkpoint must not start a model turn")))
    session = {"session_key": "desktop-session"}
    notifications._notif_poll_kanban_scoped("desktop-tab", session)
    assert any(item[2]["text"] == "\n# Progress\n\nFull body.\n" for item in emitted)
    assert not session.get("_kanban_pending")
