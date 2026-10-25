"""Process launch consumers and the native authority of a remote process reader."""

import shlex
import logging

logger = logging.getLogger("tools.process_registry")


def spawn_pty(registry, session, safe_command, env_vars):
    from tools import process_registry as facade
    from tools.approval_context import require_live_task_setup

    if facade._IS_WINDOWS:
        from winpty import PtyProcess
    else:
        from ptyprocess import PtyProcess
    argv = registry._scope_argv(session, safe_command, session.id, "PTY")
    env = registry._spawn_env(env_vars)
    if session.systemd_unit:
        env = facade.systemd_user_bus_env(env)
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("PAGER", "cat")
    require_live_task_setup()
    proc = PtyProcess.spawn(argv, cwd=session.cwd, env=env, dimensions=(30, 120))
    session.pid = proc.pid
    session.host_start_time = registry._safe_host_start_time(session.pid)
    session._pty = proc
    registry._track_started(session, registry._pty_reader_loop, f"proc-pty-reader-{session.id}")
    return session


def spawn_via_env(registry, env, command, cwd, task_id, session_key, timeout, owner_task_id, persist_on_release, prepare_session):
    from tools.approval_context import (
        release_background_approval_task, require_live_task_setup, reserve_background_approval_task,
    )

    require_live_task_setup()
    reader_reservation = reserve_background_approval_task()
    reader_started = False
    try:
        session = registry._new_session(command, task_id, owner_task_id, session_key, cwd,
            env_ref=env, pid_scope="sandbox", persist_on_release=persist_on_release)
        if prepare_session:
            prepare_session(session)
        temp_dir = registry._env_temp_dir(env)
        log_path, pid_path, exit_path = (f"{temp_dir}/hermes_bg_{session.id}.{ext}" for ext in ("log", "pid", "exit"))
        q = shlex.quote
        script = (
            f"mkdir -p {q(temp_dir)} && "
            f"( nohup bash -lc {q(command)} > {q(log_path)} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {q(exit_path)} ) & "
            f"echo $! > {q(pid_path)} && cat {q(pid_path)}")
        require_live_task_setup()
        # The same record survives owner death or a lost backend receipt. It is
        # not proof a process started, and its sandbox PID is never a host PID.
        retain_remote_unknown(registry, session, "Remote launch receipt is unknown; reconcile this session before retrying.")
        try:
            result = env.execute(script, cwd=cwd or "", timeout=timeout, rewrite_compound_background=False)
        except Exception:
            logger.exception("Remote process launch outcome is unknown for session %s", session.id)
            return session
        output = result.get("output", "").strip()
        session.pid = next((int(line) for line in map(str.strip, output.splitlines()) if line.isdigit()), None)
        if session.pid is None:
            return session
        session.reconciliation_required = ""
        registry._track_started(session, _read_remote, f"proc-poller-{session.id}",
            (registry, env, log_path, pid_path, exit_path, reader_reservation))
        reader_started = True
        return session
    finally:
        if not reader_started:
            release_background_approval_task(reader_reservation)


def retain_remote_unknown(registry, session, note):
    session.reconciliation_required = note
    with registry._lock:
        registry._running[session.id] = session
    registry._write_checkpoint()


def _read_remote(session, registry, env, log_path, pid_path, exit_path, reservation):
    from tools.approval_context import background_approval_task

    with background_approval_task(reservation, "process-reader:" + session.id):
        registry._env_poller_loop(session, env, log_path, pid_path, exit_path)


def preserve_remote_uncertainty(registry, session, *, inspection_failed=False):
    """Neither stopped authority nor failed inspection proves a remote process exited."""
    from tools.approval_context import approval_task_revoked

    stopped = approval_task_revoked()
    if not stopped and not inspection_failed:
        return False
    with session._lock:
        if not session.exited:
            session.reconciliation_required = (
                ("The originating native task stopped; " if stopped else "Remote process inspection failed; ") +
                "remote process state is unknown. "
                "Reconcile it using current native authorization before reporting it stopped or retrying work.")
    registry._write_checkpoint()
    return True
