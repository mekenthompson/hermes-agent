"""Background-process launch path: ``terminal(background=true)`` spawns a tracked
process via the process registry (Popen locally, ``env.execute`` in a sandbox),
stamps gateway routing metadata for completion / watch-pattern notifications and
returns the JSON result. Lazy ``tools.terminal_tool`` imports keep the origin's
monkeypatch points authoritative.
"""

import json
import logging
import sys
from contextlib import contextmanager
from typing import Any, List, Optional

logger = logging.getLogger("tools.terminal_tool")

# A silent background process (no notify_on_complete / watch_patterns) is right
# only for servers/watchers; for bounded tasks the agent almost always wanted a
# notification and forgot the flag, so nudge it (cheap false positive).
_SILENT_BACKGROUND_HINT = (
    'background=true without notify_on_complete=true means this process runs SILENTLY — you '
    'will not be told when it exits. If this is a bounded task (test suite, build, CI poller, '
    'deploy, anything with a defined end), you almost certainly wanted notify_on_complete=true '
    'so the system pings you on exit. Re-launch with notify_on_complete=true, or call '
    "process(action='poll') / process(action='wait') yourself to learn the outcome. Only "
    'ignore this hint for genuine long-lived processes that never exit (servers, watchers, '
    'daemons).'
)

# Homebrewed CI pollers built on `gh pr view --json statusCheckRollup` or
# `gh pr checks | jq` fail silently in known ways (block-buffered stdout never
# reaches capture, jq null-key edge cases exit the loop, conclusion-vs-status
# confusion declares all-green early, TTY-only banners never appear piped).
# Detector is deliberately narrow: the canonical column-2 awk poller is fine.
_HOMEBREW_CI_POLLER_HINT = (
    'This looks like a homebrewed CI poller built from `gh pr view --json statusCheckRollup` '
    'and/or `gh pr checks | jq`. That shape has burned us repeatedly in hermes-agent dev work '
    '(PRs #31329, #31448, #31695, #31709, #31745, #32264, #33131) — stdout buffering kills '
    'output capture, jq null-key edge cases silently exit the loop, conclusion-vs-status field '
    'confusion exits early with bogus all-green verdicts, TTY-only summary banners never '
    'appear when piped. Use the canonical snippets in the green-ci-policy skill instead: the '
    'exit-code-driven `gh pr checks $PR >/dev/null` (rc 0 = green, 8 = pending, else fail) for '
    'exit-on-first-fail behavior, or the column-2 awk-on-tabs poller (`awk -F"\\t" '
    '"$2==\\"pending\\""`) for sharded matrices. Load '
    "skill_view(name='github/hermes-agent-dev', file_path='references/green-ci-policy.md') for "
    'the verbatim snippets. If you must roll a custom loop with rich structured output, write '
    "each tick to a known file (`tee -a $TMPDIR/ci.log`) and rely on `process(action='log')` to "
    'read THAT file — do not rely on background-process stdout capture for line-buffered shell '
    'loops.'
)

# A foreground call whose only backgrounding was a trailing `&` is spawned as background=true.
_AUTO_BACKGROUND_NOTE = (
    "Ran in the background (the trailing & was converted to background=true; Hermes tracks the "
    "process). Do NOT re-run it. Use process_manage (poll/log/wait/kill, session_id above) to check it."
)

_ASYNC_UNSUPPORTED_NOTE = (
    'notify_on_complete / watch_patterns are not available in this session — it cannot receive '
    'an async completion after the turn ends (a one-shot runner such as `hermes -z`, a cron '
    'job, a Kanban worker, or a stateless HTTP endpoint). The process is running in the '
    "background; retrieve its result with process(action='poll') or process(action='wait')."
)

# proc_session attribute -> HERMES_SESSION_* env var carrying it.
_ROUTING_FIELDS = (
    ("watcher_chat_id", "HERMES_SESSION_CHAT_ID"),
    ("watcher_user_id", "HERMES_SESSION_USER_ID"),
    ("watcher_user_name", "HERMES_SESSION_USER_NAME"),
    ("watcher_thread_id", "HERMES_SESSION_THREAD_ID"),
    ("watcher_message_id", "HERMES_SESSION_MESSAGE_ID"),
    # The spawning conversation's session-db id lets the gateway's completion
    # pre-flight drop the notification if the user closed this session (/new)
    # before the process finished, instead of injecting it into the NEW one.
    ("parent_session_id", "HERMES_SESSION_ID"),
)


def _looks_like_homebrew_ci_poller(command: str) -> bool:
    has_gh = "gh pr view" in command or "gh pr checks" in command
    has_jq = " jq " in command or "| jq" in command or "$(jq" in command
    # `gh pr checks` doesn't emit JSON, so piping it to jq is confused intent.
    return "statusCheckRollup" in command or (has_gh and has_jq)


def _stamp_gateway_routing(proc_session, get_session_env) -> None:
    """Copy the spawning chat's routing metadata onto the process session so
    completion / watch notifications reach the right chat/thread."""
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if not platform:
        return
    proc_session.watcher_platform = platform
    for attr, var in _ROUTING_FIELDS:
        setattr(proc_session, attr, get_session_env(var, ""))


def _spawn(process_registry, *, env, env_type, command, cwd, effective_task_id, task_id,
           session_key, effective_pty, persist_on_release: bool = False, prepare_session=None):
    common = dict(command=command, cwd=cwd, task_id=effective_task_id,
                  owner_task_id=task_id or effective_task_id, session_key=session_key,
                  persist_on_release=persist_on_release, _prepare_session=prepare_session)
    from tools.managed_process_proxy import reserved_background
    if reserved := reserved_background():
        common['_reserved_session'] = reserved
    if env_type == "local":
        return process_registry.spawn_local(
            env_vars=env.env if hasattr(env, 'env') else None, use_pty=effective_pty, **common)
    return process_registry.spawn_via_env(env=env, **common)


def _apply_async_support(proc_session, result_data, notify_on_complete, watch_patterns):
    """Finite sessions (stateless HTTP, one-shot Kanban workers) can't route a
    completion back after the turn ends: drop the flags and tell the agent to
    poll. Otherwise stamp gateway routing. Returns (notify, watch_patterns)."""
    if not (notify_on_complete or watch_patterns):
        return notify_on_complete, watch_patterns
    from gateway.session_context import async_delivery_supported, get_session_env

    if async_delivery_supported():
        _stamp_gateway_routing(proc_session, get_session_env)
        return notify_on_complete, watch_patterns
    result_data["notify_on_complete"] = False
    result_data["notify_unsupported"] = _ASYNC_UNSUPPORTED_NOTE
    logger.info("background proc %s: async delivery unsupported on this "
                "session; notify_on_complete/watch_patterns disabled", proc_session.id)
    return False, None


def _register_completion_watcher(process_registry, proc_session, session_key) -> None:
    """Gateway mode: register a fast watcher so completion triggers a new
    agent turn (CLI mode uses the completion_queue directly).

    Armed on the live gateway loop right away: the post-turn drain alone leaves a
    process that finishes while its launching turn is still running unwatched, and
    the chat mute for as long as that turn lasts (#112033). Before the gateway
    serves, or while it stops, the descriptor waits in ``pending_watchers`` for the
    startup / post-turn drain instead."""
    proc_session.watcher_interval = 5
    watcher = {
        "session_id": proc_session.id, "check_interval": 5, "session_key": session_key,
        "platform": proc_session.watcher_platform,
        **{attr.removeprefix("watcher_"): getattr(proc_session, attr)
           for attr, _ in _ROUTING_FIELDS[:-1]},
        "notify_on_complete": True, "parent_session_id": proc_session.parent_session_id,
    }
    runner_ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
    runner = runner_ref() if callable(runner_ref) else None
    if runner is not None and runner.arm_process_watcher(watcher):
        return
    process_registry.pending_watchers.append(watcher)


@contextmanager
def background_spawn_authority(required=True):
    """Reserve the existing native child scope before terminal setup can wait."""
    from tools.approval_context import (
        background_approval_task, background_task_refusal, reserve_background_approval_task,
    )
    if not required:
        yield None
        return
    if refusal := background_task_refusal():
        yield refusal
        return
    reservation = reserve_background_approval_task()
    if reservation is None:
        yield {"status": "rejected", "code": "task_authority_revoked",
               "error": "The originating native task was stopped before background setup."}
        return
    with background_approval_task(reservation, "terminal-spawn"):
        yield None


def spawn_background_process(**kwargs) -> str:
    """Native background launch, also guarded for direct internal callers."""
    with background_spawn_authority() as refusal:
        if refusal:
            return json.dumps(refusal)
        return _spawn_background_process(**kwargs)


def _spawn_background_process(
    *, command: str, env: Any, env_type: str, effective_task_id: str, task_id: Optional[str],
    session_key: str, workdir: Optional[str], cwd: str, effective_pty: bool,
    notify_on_complete: bool, watch_patterns: Optional[list[str]], approval_note: Optional[str],
    completion_output_chars: int = 0,
    pty_disabled_reason: Optional[str],
    heartbeat_seconds: int = 0,
    persist_on_release: bool = False,
    mounted_host: Optional[str] = None,
    auto_backgrounded: bool = False,
) -> str:
    """Spawn *command* as a tracked background process and return the JSON result.

    ``auto_backgrounded`` marks a trailing-`&` conversion: the result carries the conversion note
    and skips the silent-background hint, whose "re-launch" advice would start a second copy.

    Never inline-polls ``is_interrupted()``: the spawn detaches and returns
    exit_code 0 immediately, so the stale-interrupt kill cannot occur here.
    """
    from tools.process_registry import process_registry
    from tools.terminal_tool import (
        _redact_terminal_error_text, _resolve_command_cwd, _resolve_notification_flag_conflict,
    )

    effective_cwd = _resolve_command_cwd(
        workdir=workdir, default_cwd=cwd, session_key=session_key, env_type=env_type,
        task_id=task_id,
        mounted_host=mounted_host if mounted_host is not None else getattr(env, "host_cwd", None),
        env=env,
    )
    from tools.approval_context import reserve_background_approval_task
    # Ownership outlives the producer even when the channel cannot deliver a notice.
    reservation = reserve_background_approval_task()
    prepared = None

    def prepare_session(session):
        nonlocal prepared
        prepared = session
        session.approval_task_reservation = reservation
        _record_native_origin(session, reservation)
        notify, patterns = _apply_async_support(session, {}, notify_on_complete, watch_patterns)
        patterns, _ = _resolve_notification_flag_conflict(
            notify_on_complete=bool(notify), watch_patterns=patterns, background=True)
        session.notify_on_complete = bool(notify)
        session.watch_patterns = list(patterns or [])

    try:
        proc_session = _spawn(
            process_registry, env=env, env_type=env_type, command=command, cwd=effective_cwd,
            effective_task_id=effective_task_id, task_id=task_id, session_key=session_key,
            effective_pty=effective_pty, persist_on_release=persist_on_release,
            prepare_session=prepare_session,
        )
        # This is process-owned in-memory evidence. Checkpoint recovery deliberately
        # omits it, so an owner restart cannot recreate a human grant from IDs.
        result_data = {"output": "Background process started", "session_id": proc_session.id,
                       "pid": proc_session.pid, "exit_code": 0, "error": None}
        if proc_session.reconciliation_required and proc_session.pid is None:
            result_data.update(output="Process launch outcome is unknown", status="unknown", exit_code=None,
                code="process_launch_uncertain", reconciliation_required=proc_session.reconciliation_required)
            return json.dumps(result_data, ensure_ascii=False)
        from tools.approval_context import approval_task_revoked
        if approval_task_revoked():
            note = ("Stop arrived while this process was starting. Its actual process is tracked; "
                    "reconcile this session before reporting it stopped or retrying the command.")
            proc_session.reconciliation_required = note
            process_registry._write_checkpoint()
            result_data.update(code="task_authority_revoked", reconciliation_required=note)
            return json.dumps(result_data, ensure_ascii=False)
        if proc_session.reconciliation_required:
            result_data.update(output="Process launch outcome is unknown", status="unknown", exit_code=None,
                code="process_launch_uncertain", reconciliation_required=proc_session.reconciliation_required)
            return json.dumps(result_data, ensure_ascii=False)
        if persist_on_release:
            result_data["persist_on_release"] = True
        if approval_note:
            result_data["approval"] = approval_note
        if pty_disabled_reason:
            result_data["pty_note"] = pty_disabled_reason
        if auto_backgrounded:
            result_data["auto_backgrounded"] = _AUTO_BACKGROUND_NOTE
        elif not notify_on_complete and not watch_patterns:
            result_data["hint"] = _SILENT_BACKGROUND_HINT
        if command and _looks_like_homebrew_ci_poller(command):
            existing = result_data.get("hint", "")
            result_data["hint"] = (existing + "\n\n" + _HOMEBREW_CI_POLLER_HINT if existing
                                   else _HOMEBREW_CI_POLLER_HINT)

        notify_on_complete, watch_patterns = _apply_async_support(
            proc_session, result_data, notify_on_complete, watch_patterns)
        watch_patterns, conflict_note = _resolve_notification_flag_conflict(
            notify_on_complete=bool(notify_on_complete), watch_patterns=watch_patterns, background=True,
        )
        if conflict_note:
            logger.warning("background proc %s: %s", proc_session.id, conflict_note)
            result_data["watch_patterns_ignored"] = conflict_note
        if notify_on_complete:
            proc_session.notify_on_complete = True
            result_data["notify_on_complete"] = True
            if completion_output_chars:
                proc_session.completion_output_chars = int(completion_output_chars)
            if proc_session.watcher_platform:
                _register_completion_watcher(process_registry, proc_session, session_key)
            from agent.delegation_context import is_delegated_child_context
            if is_delegated_child_context():
                result_data["notify_on_complete"] = False
                result_data["subagent_note"] = _SUBAGENT_NOTIFY_NOTE
            elif heartbeat_seconds:
                # Heartbeats ride the same delivery path as the completion notice, so they are
                # only armed where that notice can actually reach the agent.
                result_data["heartbeat_seconds"] = process_registry.arm_heartbeat(proc_session, heartbeat_seconds)
        elif heartbeat_seconds:
            result_data["heartbeat_ignored"] = "heartbeat needs notify=true delivery, which this session cannot receive"
        if watch_patterns:
            proc_session.watch_patterns = list(watch_patterns)
            result_data["watch_patterns"] = proc_session.watch_patterns
        return json.dumps(result_data, ensure_ascii=False)
    except Exception as e:
        logger.exception("Background process launch failed")
        _release_untracked_reservation(process_registry, prepared, reservation)
        return json.dumps({
            "output": "", "exit_code": -1,
            "error": _redact_terminal_error_text(f"Failed to start background process: {e}"),
        }, ensure_ascii=False)


def _release_untracked_reservation(registry, session, reservation):
    """A retained descriptor's actual finalizer owns its private reservation."""
    from tools.approval_context import release_background_approval_task
    with registry._lock:
        retained = session is not None and (
            registry._running.get(session.id) is session or registry._finished.get(session.id) is session)
    if not retained:
        release_background_approval_task(reservation)


def _record_native_origin(session, reservation):
    """Checkpoint description comes only from the already-minted private native reservation."""
    if reservation is None:
        return
    evidence = reservation.evidence
    for field in ('profile', 'session', 'task_id', 'run_id'):
        setattr(session, 'native_origin_' + field, getattr(evidence, field))


_SUBAGENT_NOTIFY_NOTE = (
    "You are a subagent: this process's completion notice will NOT reach your parent, and the process is killed when "
    "you finish. Before you finish, either wait for it (process_manage wait), kill it, or hand it to your parent with "
    "process_manage(action='handoff', session_id=..., data='<purpose>') so the parent receives its completion. For CI "
    "watchers prefer returning the fact (PR number, SHA) and letting the parent watch."
)

_YIELDED_NOTE = (
    "The user sent a message while this command was running, so it was moved to the "
    "background WITHOUT being killed and is still running. You will be notified when it "
    "exits (notify_on_complete). Read the user's message and respond to it now; use "
    "process(action='poll'|'wait'|'log', session_id=...) to check on this command."
)


def yield_to_background_handler(
    *, command: str, env_type: str, cwd: Optional[str], effective_task_id: str,
    task_id: Optional[str], session_key: str,
):
    """Build the ``yield_handler`` a foreground ``env.execute`` calls when the tool thread is
    asked to yield (a user message arrived mid-command). Local backend only: the live Popen
    is adopted by the process registry as a notify-on-complete background session and the
    partial output is returned to the model right away. Other backends return None (no
    adoptable host process) and the foreground wait continues."""
    if env_type != "local":
        return None

    def _handler(proc, output_so_far: str) -> dict | None:
        from tools.process_registry import process_registry
        from tools.approval_context import background_task_refusal, reserve_background_approval_task, release_background_approval_task
        if background_task_refusal():
            return None
        reservation = reserve_background_approval_task()
        if reservation is None:
            return None
        session = None
        try:
            session = process_registry.adopt_local(
                proc, command=command, cwd=cwd, task_id=effective_task_id,
                owner_task_id=task_id or effective_task_id, session_key=session_key,
                output_so_far=output_so_far)
            with process_registry._lock:
                finished = getattr(session, '_finish_claimed', False)
                if not finished:
                    session.approval_task_reservation = reservation
                    _record_native_origin(session, reservation)
            if finished:
                release_background_approval_task(reservation)
            _stamp_routing_if_gateway(process_registry, session, session_key)
        except BaseException:
            _release_untracked_reservation(process_registry, session, reservation)
            raise
        logger.info("foreground command yielded to background as %s (pid %s)", session.id, session.pid)
        return {
            "output": output_so_far, "returncode": None, "yielded_session_id": session.id, "pid": session.pid,
        }
    return _handler


def _stamp_routing_if_gateway(process_registry, session, session_key) -> None:
    """Route the adopted session's completion like a normal notify_on_complete spawn."""
    from gateway.session_context import async_delivery_supported, get_session_env
    if not async_delivery_supported():
        session.notify_on_complete = False
        return
    _stamp_gateway_routing(session, get_session_env)
    if session.watcher_platform:
        _register_completion_watcher(process_registry, session, session_key)
