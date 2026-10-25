"""Process launch consumers and the native authority of a remote process reader."""

import shlex
import logging

logger = logging.getLogger("tools.process_registry")

_LOCAL_PENDING = 'Local process launch receipt is pending; reconcile this session before retrying.'


def spawn_local(registry, command, cwd, task_id, session_key, env_vars, use_pty,
                owner_task_id, persist_on_release, prepare_session):
    from tools import process_registry as facade
    from tools.approval_context import current_approval_task_context, require_live_task_setup
    from tools.terminal_tool_sudo import _rewrite_compound_background
    from tools.process_registry_chat_execution import reserve_local_process, spawn_local_pipe

    require_live_task_setup()
    session = registry._new_session(command, task_id, owner_task_id, session_key,
        facade._resolve_safe_cwd(cwd or facade.os.getcwd()), persist_on_release=persist_on_release)
    if prepare_session:
        prepare_session(session)
    session._native_local_launch = current_approval_task_context() is not None
    session._local_launch_pending = True
    try:
        persisted = retain_remote_unknown(registry, session, _LOCAL_PENDING)
        if session._native_local_launch and persisted is False:
            raise OSError('Native local launch checkpoint could not be written; no process was started.')
        resource = reserve_local_process() if facade._IS_LINUX and not use_pty else None
        log = facade.worker_output_log(session.id)
        safe_command = _rewrite_compound_background(command)
        if use_pty and log is None:
            try:
                return registry._spawn_local_pty(session, safe_command, env_vars)
            except ImportError:
                if session._local_launch_attempted:
                    raise  # a lost launch receipt cannot authorize another launch
                logger.warning('PTY backend not installed, falling back to pipe mode')
        return spawn_local_pipe(registry, session, safe_command, env_vars, False, resource, log=log)
    except Exception:
        if not session._local_launch_attempted:
            with registry._lock:
                registry._running.pop(session.id, None)
            registry._write_checkpoint()
            raise
        logger.exception('Local process launch could not be confirmed for %s', session.id)
        reconcile_local_launch(registry, session)
        return session


def begin_local_launch(session):
    from tools.approval_context import require_live_task_setup
    with session._lock:
        require_live_task_setup()
        if session.exited:
            raise ValueError('Local process launch was cancelled before it started.')
        session._local_launch_attempted = True


def cancel_unstarted_local_launch(registry, session, source):
    """One lock orders the no-effect cancellation against the producer's launch attempt."""
    if (session._local_launch_attempted and not session.exited) or (not session._local_launch_pending and not (
            session.exited and session.pid is None and session.completion_reason == 'failed_start')):
        return None
    with session._lock:
        if session.exited and session.pid is None and session.completion_reason == 'failed_start':
            return {'status': 'already_exited', 'session_id': session.id, 'not_started': True}
        if not session._local_launch_pending or session._local_launch_attempted:
            return None
        session.termination_source = source  # a request fences GO even if custody cleanup is unknown
        if session._custody is not None:
            from tools.process_registry_custody import stop_before_go
            if result := stop_before_go(registry, session):
                return result
        session.exited = True
        session.exit_code = None  # no OS process or collected exit exists
        session.completion_reason = 'failed_start'
        session.termination_source = source
        session.reconciliation_required = ''
        session._local_launch_pending = False
        session.notify_on_complete = False
    registry._move_to_finished(session)
    return {'status': 'already_exited', 'session_id': session.id, 'not_started': True}


def local_process_receipt(registry, session, proc, *, pty=False, log=None):
    birth = registry._safe_host_start_time(proc.pid)
    with session._lock:
        session.host_start_time = birth
        session.pid = proc.pid
        if pty:
            session._pty = proc
        else:
            session.output_log = str(log or '')
            session.process = proc
    registry._write_checkpoint()


def publish_local_launch(registry, session, reader_target, reader_name):
    from tools.approval_context import require_live_task_setup
    require_live_task_setup()
    with session._lock:
        if (not session._native_local_launch or session.host_start_time is not None or session.custody_start_time is not None) and session.reconciliation_required == _LOCAL_PENDING:
            session.reconciliation_required = ''
    registry._write_checkpoint()
    registry._track_started(session, reader_target, reader_name)
    require_live_task_setup()
    with session._lock:
        if not session._native_local_launch or session.host_start_time is not None or session.custody_start_time is not None:
            session._local_launch_pending = False
    registry._write_checkpoint()


def reconcile_local_launch(registry, session):
    """Cleanup the same descriptor; a missing receipt never becomes permission to replay."""
    session.reconciliation_required = (
        'Local process launch or cleanup is unconfirmed. Reconcile the actual process and external effects before retrying.')
    registry._write_checkpoint()
    if session.process is not None or session._pty is not None:
        result = registry.kill_process(session.id, source='local_launch_reconciliation', consume_output=False)
        if result.get('status') in {'killed', 'already_exited'} and 'scope_stop_failed' not in result:
            session._local_launch_pending = False
            return
    elif session.systemd_unit:
        registry._stop_scope(session)
    registry._write_checkpoint()


def spawn_pty(registry, session, safe_command, env_vars):
    from tools import process_registry as facade
    if session._native_local_launch and facade._IS_LINUX:
        from tools.process_registry_custody import spawn_custodied
        argv = registry._scope_argv(session, safe_command, session.id, 'PTY')
        env = registry._spawn_env(env_vars)
        env.setdefault('GIT_PAGER', 'cat')
        env.setdefault('PAGER', 'cat')
        spawn_custodied(registry, session, argv, env, pty=True)
        publish_local_launch(registry, session, registry._pty_reader_loop, f'proc-pty-reader-{session.id}')
        return session
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
    begin_local_launch(session)
    proc = PtyProcess.spawn(argv, cwd=session.cwd, env=env, dimensions=(30, 120))
    local_process_receipt(registry, session, proc, pty=True)
    publish_local_launch(registry, session, registry._pty_reader_loop, f"proc-pty-reader-{session.id}")
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
    return registry._write_checkpoint()


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
