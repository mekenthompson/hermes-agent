"""A dead attempt behind an unresolved owner receipt must not hold its claim forever.

Regression for #140858. Card t_5ea03658 sat ``running`` for 16 hours: claimed 07:48, worker
killed by a restart at 08:59, claim expired 08:03, no heartbeat, and no ``worker_bound``
event. ``owner_reclaim_paused`` was true — a killed owner leaves a ``terminal/interrupted``
admission, or a pid that is gone — and every reclaim path read that as "leave it alone", so
``release_stale_claims``, ``detect_crashed_workers`` and ``reconcile_orphaned_running`` all
declined. ``bound_interpreter_gone``, the one override, needs the ``worker_bound`` event that
run never wrote. The card held its profile's ``max_in_progress_per_profile`` slot while every
card queued behind it was deferred, and no diagnostics rule could see a ``running`` card at
all, so the alarms named the victims and never the cause.

The pause is about effects that may still be in flight. These tests pin both sides of that
line: a live worker, a fresh heartbeat, a live claim, a foreign host's lock or an identity
that cannot be read all keep the card; a provably dead attempt is reclaimed by the platform's
own path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from gateway.drain_control import current_instantiation_epoch
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_owner_recovery
from hermes_cli.process_identity import worker_birth
from hermes_cli.kanban_owner_recovery import (
    bound_interpreter_gone,
    owner_attempt_provably_over,
    owner_reclaim_blocked,
    owner_reclaim_paused,
)

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
# A worker of THIS instantiation: these cards lose their worker while its instance keeps running.
# Another instantiation's fingerprint reads as the whole instance restarting (``owner_instance_replaced``).
FINGERPRINT = f"{current_instantiation_epoch() or '6a232414-2dbf-40ab-a534-d9b71db05658:116'}|4162240"


def _absent_pid(base: int = 3_500_000) -> int:
    """A pid that provably does not exist on this host, so a fixture can never hit a process."""
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


class _Board:
    """A scratch board plus the scratch admission ledger an owner receipt points at."""

    def __init__(self, root: Path):
        self.path = root / "kanban.db"
        kbc.init_db(self.path)
        self.conn = kbc.connect(self.path)
        self.ledger = root / "state.db"
        with sqlite3.connect(self.ledger) as store:
            store.execute(
                "CREATE TABLE session_admissions "
                "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
            )

    def close(self) -> None:
        self.conn.close()

    # --- seeding --------------------------------------------------------------------------
    def dead_attempt(
        self, tid: str = "t_ghost", *, worker_pid=None, worker_fingerprint=FINGERPRINT,
        worker_bound: bool = False, record_worker_pid: bool = True, owner_pid=None,
        owner_birth=None, admission=("terminal", "interrupted"), dead_hours: int = 15,
        expires_age_seconds=None, lock_host=None, heartbeat_age=None,
    ) -> int:
        """A ``running`` card whose worker died the way t_5ea03658's did."""
        worker_pid = _absent_pid() if worker_pid is None else worker_pid
        owner_pid = _absent_pid(base=3_600_000) if owner_pid is None else owner_pid
        lock = f"{lock_host or HOST}:{owner_pid}"
        expires = NOW - (expires_age_seconds if expires_age_seconds is not None
                         else dead_hours * 3600)
        started = NOW - dead_hours * 3600
        run_id = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'default', 'running', ?, ?, ?, ?)",
            (tid, lock, expires, worker_pid if record_worker_pid else None, started),
        ).lastrowid
        assert run_id is not None
        run = int(run_id)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'default', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, expires,
             worker_pid if record_worker_pid else None,
             worker_fingerprint if record_worker_pid else None,
             (NOW - heartbeat_age) if heartbeat_age is not None else None, run, started),
        )
        self.event(tid, run, "claimed", {"source_status": "ready"}, started)
        if record_worker_pid:
            spawned = {"pid": worker_pid, "started_at": worker_fingerprint}
        else:
            spawned = {}
        self.event(tid, run, "spawned", spawned, started)
        payload = {"db": str(self.ledger), "session_id": "sess-1", "pid": owner_pid,
                   "request_id": f'kanban:["default","{tid}",1]'}
        if owner_birth is not None:
            payload["birth"] = owner_birth
        self.event(tid, run, "owner_admitted", payload, started + 30)
        if worker_bound:
            self.event(tid, run, "worker_bound",
                       {"pid": worker_pid, "claim_lock": lock, "started_at": worker_fingerprint},
                       started + 60)
        if admission:
            with sqlite3.connect(self.ledger) as store:
                store.execute("INSERT INTO session_admissions VALUES ('sess-1', ?, ?, ?)",
                              (payload["request_id"], admission[0], admission[1]))
        self.conn.commit()
        return run

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    def ready(self, tid: str) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
            "VALUES (?, ?, 'default', 'ready', ?, 5)", (tid, tid, NOW - 86400))
        self.conn.commit()

    # --- reading --------------------------------------------------------------------------
    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def events(self, tid: str) -> list:
        return self.conn.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,)).fetchall()

    def diagnostics(self, tid: str, *, config: dict | None = None) -> list:
        return kd.compute_task_diagnostics(
            self.state(tid), self.events(tid), [], now=NOW, config=config or {})


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    scratch = _Board(tmp_path)
    yield scratch
    scratch.close()


# --- the incident shape is named by diagnostics and reclaimed by the platform's own paths --

def test_dead_attempt_behind_an_unresolved_receipt_is_named_and_reclaimed(board):
    """Every reclaim path declined this card and no diagnostic could see it; both must now act."""
    board.dead_attempt("t_ghost")
    board.dead_attempt("t_ghost2")
    board.dead_attempt("t_live", worker_pid=os.getpid(), worker_fingerprint=kbd._process_fingerprint(os.getpid()))
    assert owner_reclaim_paused(board.conn, "t_ghost") is True      # the pause still applies
    assert bound_interpreter_gone(board.conn, "t_ghost") is False   # nothing bound to read
    assert owner_reclaim_blocked(board.conn, "t_ghost") is False

    hits = [d for d in board.diagnostics("t_ghost") if d.kind == "stale_running"]
    assert [d.severity for d in hits] == ["critical"]                  # 15x the 1h grace
    assert [a.kind for a in hits[0].actions] == ["reclaim", "cli_hint"]
    assert not [d for d in board.diagnostics("t_live") if d.kind == "stale_running"]

    assert kb.release_stale_claims(board.conn) == 2
    row = board.state("t_ghost")
    assert (row["status"], row["claim_lock"], row["worker_pid"], row["current_run_id"]) == ("ready", None, None, None)
    run = board.conn.execute("SELECT status, outcome FROM task_runs WHERE task_id = 't_ghost'").fetchone()
    assert tuple(run) == ("reclaimed", "reclaimed")
    assert board.state("t_live")["status"] == "running"


@pytest.mark.parametrize("shape", [
    dict(worker_pid=os.getpid(), worker_fingerprint=kbd._process_fingerprint(os.getpid())),  # live worker
    dict(worker_bound=True, worker_pid=os.getpid(),
         worker_fingerprint=kbd._process_fingerprint(os.getpid())),                          # live bound interpreter
    # A settled admission is proof on its own (see the next test), so these model an attempt
    # that may still be executing: its admission is still ``started``.
    dict(heartbeat_age=300, admission=("started", None)),                                    # fresh heartbeat
    dict(expires_age_seconds=-3600, admission=("started", None)),                            # live claim
    dict(lock_host="some-other-host"),                                                       # foreign host
    dict(owner_pid=os.getpid(), owner_birth=None, admission=("started", None)),               # live owner in flight
    dict(record_worker_pid=False, admission=("started", None)),                              # no pid recorded
    dict(record_worker_pid=False, expires_age_seconds=-3600),                                # no pid yet, settled
], ids=["live-worker", "live-bound", "fresh-heartbeat", "live-claim", "foreign-host", "live-owner", "no-pid",
        "no-pid-live-claim"])
def test_the_pause_still_holds_every_attempt_that_may_be_alive(board, shape):
    """Missing evidence is never proof: reclaiming beside a possibly-live attempt repeats its effects."""
    board.dead_attempt("t_ghost", **shape)
    assert owner_attempt_provably_over(board.conn, "t_ghost") is False
    assert kb.release_stale_claims(board.conn) == 0
    assert kbd.detect_crashed_workers(board.conn) == []
    assert board.state("t_ghost")["status"] == "running"


def test_a_settled_admission_is_proof_even_while_the_owner_gateway_is_alive(board):
    """The owner is the long-lived gateway (alive by design): its settled admission, not its
    death, proves a lost managed worker's attempt over. A bound run stays the crash sweep's."""
    board.dead_attempt("t_ghost2")  # admission=("terminal", "interrupted"), dead owner pid
    board.dead_attempt("t_gateway", owner_pid=os.getpid(), owner_birth=None,
                       admission=("terminal", "interrupted"))
    board.dead_attempt("t_bound", worker_bound=True, owner_pid=os.getpid(), owner_birth=None,
                       admission=("terminal", "interrupted"))
    assert owner_reclaim_paused(board.conn, "t_gateway") is True   # effects still uncertain
    assert owner_attempt_provably_over(board.conn, "t_gateway") is True
    assert owner_attempt_provably_over(board.conn, "t_bound") is False
    assert kb.release_stale_claims(board.conn) == 2
    row = board.state("t_gateway")
    assert (row["status"], row["claim_lock"], row["worker_pid"]) == ("ready", None, None)
    run = board.conn.execute(
        "SELECT outcome, error FROM task_runs WHERE task_id = 't_gateway'").fetchone()
    assert run["outcome"] == "reclaimed"
    assert "owner admission settled interrupted" in run["error"]
    # Booked against the breaker like any other non-completing attempt (A3).
    assert board.state("t_gateway")["consecutive_failures"] == 1
    assert board.state("t_bound")["status"] == "running"


# --- the gate is decided under the same write lock as the release ---------------------------

_RECLAIM_PATHS = {
    "ttl": lambda conn: kb.release_stale_claims(conn),
    "stale": lambda conn: kbd.detect_stale_running(conn, stale_timeout_seconds=1, signal_fn=lambda *_: None),
    "runtime": lambda conn: kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None),
}


@pytest.mark.parametrize("path", sorted(_RECLAIM_PATHS))
def test_an_interpreter_bound_after_the_gate_keeps_its_claim(board, monkeypatch, path):
    """Binding changes neither claim_lock nor worker_pid, so only a re-check inside the
    reclaim transaction can see an owner that recovered after the first look."""
    run = board.dead_attempt("t_ghost")
    board.conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = 't_ghost'")
    board.conn.commit()
    real_gate, calls = owner_reclaim_blocked, []

    def gate_then_bind(conn, task_id, **kwargs):
        verdict = real_gate(conn, task_id, **kwargs)
        if not calls:  # the recovered owner binds a live interpreter right after the first look
            board.event(task_id, run, "worker_bound",
                        {"pid": os.getpid(), "started_at": kbd._process_fingerprint(os.getpid())}, NOW)
            board.conn.commit()
        calls.append(verdict)
        return verdict

    monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", gate_then_bind)
    assert not _RECLAIM_PATHS[path](board.conn)
    assert calls[0] is False and calls[-1] is True
    row = board.state("t_ghost")
    assert (row["status"], row["current_run_id"]) == ("running", run)



@pytest.mark.parametrize("path", ["ttl", "stale"])
def test_a_foreign_host_interpreter_bound_after_the_gate_keeps_its_claim(board, monkeypatch, path):
    """No local signal was sent for a foreign-host claim, so its bound interpreter's pid says
    nothing here: an absent local pid is no proof that the remote attempt ended."""
    run = board.dead_attempt("t_remote", lock_host="some-other-host", admission=("terminal", "completed"))
    real_gate, calls = owner_reclaim_blocked, []

    def gate_then_remote_bind(conn, task_id, **kwargs):
        verdict = real_gate(conn, task_id, **kwargs)
        if not calls:  # the remote owner admits and binds an interpreter right after the first look
            board.event(task_id, run, "worker_bound", {"pid": _absent_pid(base=3_700_000)}, NOW)
            board.conn.commit()
            with sqlite3.connect(board.ledger) as store:
                store.execute("UPDATE session_admissions SET status = 'unknown', outcome = NULL")
        calls.append(verdict)
        return verdict

    monkeypatch.setattr(kanban_owner_recovery, "owner_reclaim_blocked", gate_then_remote_bind)
    assert not _RECLAIM_PATHS[path](board.conn)
    row = board.state("t_remote")
    assert (row["status"], row["current_run_id"]) == ("running", run)

@pytest.mark.platforms("linux", "macos")
def test_a_timed_out_managed_run_is_booked_timed_out_not_left_for_the_crash_sweep(board):
    """Killing a managed run's worker takes its bound interpreter down with it, and the owner
    settles the admission ``unknown``. The under-lock re-check must not read that pause, which
    the timeout itself caused, as an owner still in flight: the run is ``timed_out``, never left
    running for the crash sweep to book ``crashed`` on the next tick."""
    worker, interpreter = (subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
                           for _ in range(2))
    try:
        run = board.dead_attempt(
            "t_slow", worker_pid=worker.pid, worker_fingerprint=kbd._process_fingerprint(worker.pid),
            owner_pid=os.getpid(), owner_birth=worker_birth(psutil.Process()), admission=("started", None))
        board.event("t_slow", run, "worker_bound", {"pid": interpreter.pid,
                    "started_at": kbd._process_fingerprint(interpreter.pid)}, NOW)
        board.conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = 't_slow'")
        board.conn.commit()

        def sigterm_ends_the_attempt(pid, sig):
            os.kill(pid, sig)
            interpreter.kill()  # _watch_submitter: the interpreter exits with its worker
            interpreter.wait()
            with sqlite3.connect(board.ledger) as store:  # the owner settles the lost turn
                store.execute("UPDATE session_admissions SET status = 'unknown'")

        assert kbd.enforce_max_runtime(board.conn, signal_fn=sigterm_ends_the_attempt) == ["t_slow"]
        outcome = board.conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run,)).fetchone()[0]
        assert (board.state("t_slow")["status"], outcome) == ("ready", "timed_out")
        assert kbd.detect_crashed_workers(board.conn) == []
    finally:
        for child in (worker, interpreter):
            child.kill()
            child.wait()


@pytest.mark.platforms("linux", "macos")
def test_an_exited_unreaped_owner_no_longer_holds_the_slot(board):
    """A zombie keeps its birth until reaped, but it has exited: it cannot be executing anything."""
    child = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    try:
        birth = worker_birth(psutil.Process(child.pid))
        child.stdin.close()
        deadline = time.time() + 10
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.time() < deadline, "child never exited"
            time.sleep(0.02)
        board.dead_attempt("t_ghost", owner_pid=child.pid, owner_birth=birth)
        assert kb.release_stale_claims(board.conn) == 1
        assert board.state("t_ghost")["status"] == "ready"
    finally:
        child.wait()


def test_an_earlier_runs_live_pid_does_not_hide_the_current_dead_run(board):
    """Only the current attempt's pids speak for it in the stale_running diagnostic."""
    run = board.dead_attempt("t_ghost")
    board.event("t_ghost", run - 1, "spawned", {"pid": os.getpid()}, NOW - 100_000)
    board.conn.commit()
    assert [d.kind for d in board.diagnostics("t_ghost") if d.kind == "stale_running"] == ["stale_running"]


# --- a planned gateway stop/restart is not a systemic failure ------------------------------
# 2026-11-01 00:40Z: a routine restart interrupted 5 running Kanban workers. Each owner admission
# settled ``interrupted``, each submitter exited 130 with the same error, and the crash sweep read
# 3+ identical errors as a systemic failure: all 5 were parked ``gave_up`` (sticky) until a human
# said "do all". The owner Stopped those turns on its own way out, so the cards did nothing wrong.

def _interrupted_by_stop(board, tid, *, exit_code=130, admission=("terminal", "interrupted"), **kw):
    """A dead managed attempt whose bound interpreter recorded ``exit_code`` before it went."""
    worker = _absent_pid(base=3_700_000 + sum(map(ord, tid)) * 10)
    run = board.dead_attempt(tid, worker_pid=worker, worker_bound=True, admission=admission,
                             dead_hours=0, expires_age_seconds=-3600, **kw)
    lock = board.state(tid)["claim_lock"]
    board.event(tid, run, "worker_result",
                {"pid": worker, "claim_lock": lock, "exit_code": exit_code, "last_output": ""}, NOW)
    board.conn.commit()
    return run


def test_workers_interrupted_by_a_planned_restart_all_go_back_to_ready(board):
    tids = [f"t_restart{i}" for i in range(5)]
    for tid in tids:
        _interrupted_by_stop(board, tid)

    assert sorted(kbd.detect_crashed_workers(board.conn)) == sorted(tids)
    assert kbd.detect_crashed_workers._last_auto_blocked == []
    for tid in tids:
        state = board.state(tid)
        assert state["status"] == "ready", tid
        assert state["consecutive_failures"] == 0     # a restart never spends the card's budget
        assert "gateway stop or restart" in state["last_failure_error"]
        kinds = [e["kind"] for e in board.events(tid)]
        assert "gave_up" not in kinds
        crashed = [json.loads(e["payload"]) for e in board.events(tid) if e["kind"] == "crashed"]
        assert crashed[-1]["owner_stopped"] is True
        assert kbd.check_respawn_guard(board.conn, tid) is None   # the dispatcher picks it up again


def test_a_second_restart_mid_retry_still_does_not_give_up(board):
    """DEFAULT_FAILURE_LIMIT is 2: two counted restarts in a row would have parked the card."""
    for _ in range(kbd.DEFAULT_FAILURE_LIMIT + 1):
        board.conn.execute("DELETE FROM tasks WHERE id='t_twice'")
        board.conn.commit()
        _interrupted_by_stop(board, "t_twice")
        kbd.detect_crashed_workers(board.conn)
        assert board.state("t_twice")["status"] == "ready"
    assert board.state("t_twice")["consecutive_failures"] == 0


@pytest.mark.parametrize("exit_code,flagged", [(130, False), (1, True)], ids=["planned-stop", "worker-crash"])
def test_repeated_crash_diagnostic_skips_planned_stops(board, exit_code, flagged):
    """Two rollouts in a row must not light up the board's repeated-crash alarm either."""
    for _ in range(2):
        board.conn.execute("DELETE FROM tasks WHERE id='t_rolled'")
        board.conn.commit()
        _interrupted_by_stop(board, "t_rolled", exit_code=exit_code)
        kbd.detect_crashed_workers(board.conn)
    runs = kb.list_runs(board.conn, "t_rolled")
    assert [r.outcome for r in runs] == ["crashed", "crashed"]
    diagnostics = kd.compute_task_diagnostics(
        board.state("t_rolled"), board.events("t_rolled"), runs, now=NOW,
        config={"crash_threshold": 2, "failure_threshold": 99})
    assert ("repeated_crashes" in {d.kind for d in diagnostics}) is flagged


@pytest.mark.parametrize("admission", [
    ("terminal", "failed"),
    # A killed owner: its interpreters die without an interrupted result and the successor
    # settles each lost card turn ``interrupted`` at boot. Not a planned stop.
    ("terminal", "interrupted"),
], ids=["worker-crash", "owner-killed"])
def test_genuine_same_error_crashes_still_trip_the_systemic_breaker(board, admission):
    tids = [f"t_crash{i}" for i in range(3)]
    for tid in tids:
        _interrupted_by_stop(board, tid, exit_code=1, admission=admission)

    kbd.detect_crashed_workers(board.conn)
    assert sorted(kbd.detect_crashed_workers._last_auto_blocked) == sorted(tids)
    for tid in tids:
        assert board.state(tid)["status"] == "blocked"
        gave_up = [json.loads(e["payload"]) for e in board.events(tid) if e["kind"] == "gave_up"]
        assert gave_up and gave_up[-1]["sticky"] is True


def test_an_interrupt_while_the_owner_stays_up_is_still_counted(board):
    """A user's Stop leaves the owner running: not a planned stop, so the breaker still counts it."""
    me = psutil.Process(os.getpid())
    _interrupted_by_stop(board, "t_user_stop", owner_pid=os.getpid(), owner_birth=worker_birth(me))

    kbd.detect_crashed_workers(board.conn)
    state = board.state("t_user_stop")
    assert state["status"] == "ready"
    assert state["consecutive_failures"] == 1
    assert "gateway stop or restart" not in state["last_failure_error"]


# --- a restart of the whole instance is not the card failing either ------------------------
# HF-617 (2026-11-02): ``docker compose restart`` killed the managed interpreter before the gateway
# could Stop it (the owner recorded ``worker_lost``, its admission ``unknown``) and then the gateway
# itself. Neither #200's planned-stop shape nor a settled result was left, so the restart was charged
# to the card and its respawn's refusal used up the rest of the budget: ``gave_up``.

OLD_INSTANCE = "00000000-0000-4000-8000-000000000000:1|4162240"  # a worker of an earlier instantiation


def _lost_in(board, tid, fingerprint, *, admission=("unknown", None), failures=0):
    """A dead managed attempt whose owner saw its worker die (``worker_lost``) and then died too."""
    board.conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    worker = _absent_pid(base=3_710_000 + sum(map(ord, tid)) * 10)
    run = board.dead_attempt(tid, worker_pid=worker, worker_fingerprint=fingerprint, worker_bound=True,
                             admission=admission, dead_hours=0, expires_age_seconds=-3600)
    board.conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (failures, tid))
    board.event(tid, run, "worker_lost", {"reason": "EOFError", "claim_lock": board.state(tid)["claim_lock"],
                                          "run_id": run, "pid": worker, "bound": True}, NOW)
    board.conn.commit()


@pytest.mark.parametrize("admission", [("unknown", None), ("terminal", "interrupted")],
                         ids=["before-successor-settles", "successor-settled"])
def test_a_worker_lost_to_a_restart_of_its_instance_requeues_without_counting(board, admission):
    for attempt in range(kbd.DEFAULT_FAILURE_LIMIT + 1):  # DEFAULT_FAILURE_LIMIT restarts used to park it
        _lost_in(board, "t_restarted", OLD_INSTANCE, admission=admission,
                 failures=board.state("t_restarted")["consecutive_failures"] if attempt else 0)
        assert kbd.detect_crashed_workers(board.conn) == ["t_restarted"]
        state = board.state("t_restarted")
        assert state["status"] == "ready" and state["consecutive_failures"] == 0
        assert "gateway stop or restart" in state["last_failure_error"]
    assert "gave_up" not in [e["kind"] for e in board.events("t_restarted")]


def test_a_card_that_keeps_taking_its_instance_down_still_gives_up(board):
    """Uncounted restarts are bounded: past RESTART_REQUEUE_LIMIT in a row each one is charged."""
    limit = kanban_owner_recovery.RESTART_REQUEUE_LIMIT
    for attempt in range(limit + kbd.DEFAULT_FAILURE_LIMIT):
        state = board.state("t_oom") if attempt else {"consecutive_failures": 0}
        _lost_in(board, "t_oom", OLD_INSTANCE, failures=state["consecutive_failures"])
        kbd.detect_crashed_workers(board.conn)
        state = board.state("t_oom")
        assert state["consecutive_failures"] == max(0, attempt + 1 - limit), attempt
    assert state["status"] == "blocked"
    assert "times in a row" in state["last_failure_error"]
    assert "gave_up" in [e["kind"] for e in board.events("t_oom")]


def test_a_worker_lost_while_its_instance_keeps_running_still_gives_up(board):
    """Same evidence, but the worker was of THIS instantiation: a real crash, charged every time."""
    _lost_in(board, "t_crashing", FINGERPRINT)
    kbd.detect_crashed_workers(board.conn)
    assert board.state("t_crashing")["consecutive_failures"] == 1
    _lost_in(board, "t_crashing", FINGERPRINT, failures=1)
    kbd.detect_crashed_workers(board.conn)
    assert board.state("t_crashing")["status"] == "blocked"
    assert "gave_up" in [e["kind"] for e in board.events("t_crashing")]


@pytest.mark.parametrize("shape", ["live-owner", "unreadable-fingerprint", "settled-failed"])
def test_restart_evidence_that_is_not_proof_is_still_counted(board, shape):
    me = psutil.Process(os.getpid())
    owner = dict(owner_pid=os.getpid(), owner_birth=worker_birth(me)) if shape == "live-owner" else {}
    fingerprint = "unreadable" if shape == "unreadable-fingerprint" else OLD_INSTANCE
    admission = ("terminal", "failed") if shape == "settled-failed" else ("unknown", None)
    worker = _absent_pid(base=3_720_000)
    board.dead_attempt("t_unproven", worker_pid=worker, worker_fingerprint=fingerprint, worker_bound=True,
                       admission=admission, dead_hours=0, expires_age_seconds=-3600, **owner)
    assert not kanban_owner_recovery.owner_instance_replaced(board.conn, "t_unproven")
