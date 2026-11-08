"""The ``active_pr`` respawn guard re-checks its PR before holding a card (HF-662).

Before: a card whose comment linked a PR parked as "waiting for PR #N to merge"
on every requeue for 24h, long after that PR merged, and every park pinged the
subscriber "blocked; needs attention". A worker that legitimately blocked twice
for a human step dropped to ``triage``, where neither ``unblock`` nor
``promote`` could return it.

Now: the guard asks GitHub and lifts once every linked PR is closed, a parked
card is released when its PR merges, the park neither pings nor wakes anyone,
and ``unblock`` returns a block-loop triage card to the queue.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_pr_state as prs

PR_44 = "https://github.com/example/repo/pull/44"
PR_45 = "https://github.com/example/repo/pull/45"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    prs.clear_cache()
    kb.init_db()
    yield home
    prs.clear_cache()


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> dict:
    """PR number -> what GitHub answers (True closed, False open, None unknown)."""
    states: dict[int, object] = {}
    monkeypatch.setattr(prs, "fetch_pr_closed", lambda owner, repo, number: states.get(number))
    return states


def _spawned():
    calls: list[str] = []
    return calls, lambda task, workspace, board=None: calls.append(task.id)


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "null") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]


def test_a_merged_pr_does_not_hold_the_card(kanban_home, github):
    calls, spawn = _spawned()
    github[44] = True
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="merged already", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"The change is merged ({PR_44}).")
        assert kbd.check_respawn_guard(conn, tid) is None
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _events(conn, tid, "blocked") == []


def test_an_open_or_unknown_pr_still_holds_the_card(kanban_home, github):
    """Fail closed: an open PR, or one GitHub cannot answer for, keeps the
    duplicate-PR protection, and so does one open PR beside a merged one."""
    calls, spawn = _spawned()
    github.update({44: True, 45: None})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="two PRs", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Merged {PR_44}.")
        kb.add_comment(conn, tid, author="dev", body=f"Follow-up opened: {PR_45}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        github[45] = False
        prs.clear_cache()
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == []
        assert [e["reason"] for e in _events(conn, tid, "blocked")] == ["waiting for PR #45 to merge"]


def test_a_parked_card_is_released_when_its_pr_merges(kanban_home, github):
    calls, spawn = _spawned()
    github[44] = False
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="waiting on review", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_44}")
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _status(conn, tid) == "blocked"

        github[44] = True
        prs.clear_cache()
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert _events(conn, tid, "promoted") == [{"source": "respawn_guard"}]
        assert _events(conn, tid, "unblocked") == []


def test_hf654_human_steps_after_a_merged_pr_never_park_or_triage(kanban_home, github):
    """Replay of the incident: a worker merges its PR, blocks for a human step,
    blocks again for the next one (the breaker sends it to triage), the
    operator unblocks it from triage, and the guard must not re-block it."""
    calls, spawn = _spawned()
    github[1101] = True
    pr = "https://github.com/mekenthompson/hermes-fleet-private/pull/1101"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="move gymbro", assignee="dev")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="approve access", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, author="dev", body=f"The change is merged ({pr}).")
        assert kb.block_task(conn, tid, reason="restart the gateway", kind="needs_input")
        assert _status(conn, tid) == "triage"

        # Supported path back: unblock (CLI ``hermes kanban unblock`` / ``kanban_unblock``).
        assert kb.promote_task(conn, tid, actor="ken")[0] is False
        assert kb.unblock_task(conn, tid)
        assert _status(conn, tid) == "ready"
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert calls == [tid]
        assert [e for e in _events(conn, tid, "blocked") if e.get("source") == "respawn_guard"] == []

        # The next genuine human step is a normal block, not an instant re-triage.
        assert kb.block_task(conn, tid, reason="sign in to Garmin", kind="needs_input")
        assert _status(conn, tid) == "blocked"


def test_unblock_does_not_skip_specification_of_a_filed_triage_card(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rough idea", assignee="dev", triage=True)
        assert _status(conn, tid) == "triage"
        assert kb.unblock_task(conn, tid) is False
        assert _status(conn, tid) == "triage"


def test_unblock_from_triage_honours_the_event_guard(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="loops", assignee="dev")
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            kb.claim_task(conn, tid)
            kb.block_task(conn, tid, reason="same ask", kind="needs_input")
            if _status(conn, tid) == "blocked":
                assert kb.unblock_task(conn, tid)
        assert _status(conn, tid) == "triage"
        latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        assert kb.unblock_task(conn, tid, expected_event_id=latest - 1) is False
        assert kb.unblock_task(conn, tid, expected_event_id=latest)
        assert _status(conn, tid) == "ready"


def test_the_park_neither_pings_nor_wakes_the_subscriber(kanban_home, github):
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS, _KanbanNotification
    from hermes_cli import kanban_db_notify as kbn

    github[44] = False
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="waiting on review", assignee="dev")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="ken", delivery_mode="notify+wake")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_44}")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert _status(conn, tid) == "blocked"
        sub = kbn.list_notify_subs(conn, tid)[0]
        old, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="ken", kinds=TERMINAL_KINDS)
        assert "blocked" in [ev.kind for ev in events]
        task = kb.get_task(conn, tid)

    sent: list[str] = []
    notice = _KanbanNotification(
        SimpleNamespace(_kanban_sub_op=Mock(return_value=True)),
        {"sub": sub, "task": task, "events": events, "old_cursor": old, "cursor": cursor, "board": "default"},
        platform_cls=None, sub_fail_counts={})

    async def record(ev, msg):
        sent.append(msg)
        return True

    notice._send_event = record
    asyncio.run(notice._send_pings())
    notice.build_wake_text()
    assert sent == []
    assert notice.wake_kinds == set()


def test_a_worker_block_still_pings(kanban_home):
    from gateway.kanban_watchers_notifier import _KanbanNotification

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="needs Ken", assignee="dev")
        kb.block_task(conn, tid, reason="approve access", kind="needs_input")
        events = [ev for ev in kb.list_events(conn, tid) if ev.kind == "blocked"]
        task = kb.get_task(conn, tid)
    sub = {"task_id": tid, "platform": "telegram", "chat_id": "ken", "delivery_mode": "notify+wake"}
    notice = _KanbanNotification(SimpleNamespace(), {"sub": sub, "task": task, "events": events},
                                 platform_cls=None, sub_fail_counts={})
    assert notice.format_event(events[0])
    notice.build_wake_text()
    assert notice.wake_kinds == {"blocked"}


class _GitHubStub(BaseHTTPRequestHandler):
    answers: dict[str, tuple[int, bytes]] = {}
    hits: list[str] = []

    def do_GET(self):
        type(self).hits.append(self.path)
        status, body = type(self).answers.get(self.path, (404, b'{"message": "Not Found"}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def github_http(monkeypatch: pytest.MonkeyPatch):
    server = HTTPServer(("127.0.0.1", 0), _GitHubStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _GitHubStub.answers, _GitHubStub.hits = {}, []
    monkeypatch.setattr(prs, "_API_URL", f"http://127.0.0.1:{server.server_port}/repos/{{owner}}/{{repo}}/pulls/{{number}}")
    monkeypatch.setattr("hermes_cli.github_api.github_token", lambda: None)
    prs.clear_cache()
    yield _GitHubStub
    server.shutdown()
    server.server_close()
    prs.clear_cache()


def test_pr_state_reads_githubs_answer_and_caches_it(github_http):
    github_http.answers = {
        "/repos/example/repo/pulls/44": (200, b'{"state": "closed", "merged": true}'),
        "/repos/example/repo/pulls/45": (200, b'{"state": "open"}'),
    }
    assert prs.pr_closed(PR_44) is True
    assert prs.pr_closed(PR_45) is False
    assert prs.pr_closed("https://github.com/example/repo/pull/46") is None  # 404: unknown
    assert prs.all_closed([PR_44, PR_44]) is True
    assert prs.all_closed([PR_44, PR_45]) is False
    assert prs.all_closed([]) is False
    assert github_http.hits.count("/repos/example/repo/pulls/44") == 1
