"""Local pipe-process launch evidence, never process-tree settlement proof."""
from uuid import uuid4
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


def reserve_local_process():
    from agent.chat_execution_context import concurrent_tool_resource_for_submit
    return concurrent_tool_resource_for_submit(str(uuid4()), kind="terminal_local_process")


def start_local_process(handle):
    if handle is None:
        return
    from agent.chat_execution_context import start_reserved_tool_resource
    if not start_reserved_tool_resource(handle):
        raise RuntimeError("Durable terminal process start refused")


def record_local_process(handle, process_session_id, pid, host_start_time):
    if handle is None:
        return
    from gateway.chat_execution_recovery import ChatExecutionStore
    if host_start_time is None or not ChatExecutionStore(handle.home).record_terminal_process_lineage(
            handle.session_key, handle.generation, handle.resource_id, handle.session_id,
            process_session_id, pid, host_start_time):
        raise RuntimeError("Durable terminal process lineage refused")


def spawn_local_pipe(registry, session, safe_command, env_vars, pty_scope_attempted, resource, *, log=None):
    # Late facade lookup preserves existing platform and subprocess test seams.
    from tools import process_registry as facade
    popen_kwargs: dict[str, Any] = {"creationflags": facade.windows_hide_flags()} if facade._IS_WINDOWS else {}
    suffix = f"{session.id}-pipe-fallback" if pty_scope_attempted else session.id
    argv = registry._scope_argv(
        session, facade.record_exit_command(log, safe_command) if log else safe_command, suffix, "Local")
    if session.native_job_scope:
        from tools.process_registry_job import job_receipt_path
        from hermes_dir_acl import make_user_dir
        log = job_receipt_path(session.id).parent / (session.id + '.log')
        make_user_dir(log.parent, mode=0o700, parents=True, exist_ok=True)
        argv[-1] = f'set +m; {safe_command}'  # The helper alone records scope completion.
    env = registry._spawn_env(env_vars)
    if session.systemd_unit:
        env = facade.systemd_user_bus_env(env)
    start_local_process(resource)
    # The scope does not create a new session; preserve TTY/process-group isolation.
    output = facade.open_output_log(log) if log else facade.subprocess.PIPE
    try:
        proc = facade.subprocess.Popen(
            argv, text=True, cwd=session.cwd, env=env, encoding="utf-8", errors="replace",
            stdout=output, stderr=facade.subprocess.STDOUT,
            stdin=facade.subprocess.DEVNULL, start_new_session=True, **popen_kwargs)
    finally:
        if log:
            os.close(output)
    session.process, session.output_log = proc, str(log or "")
    session.pid = proc.pid
    session.host_start_time = registry._safe_host_start_time(session.pid)
    try:
        record_local_process(resource, session.id, session.pid, session.host_start_time)
        registry._track_started(
            session, registry._log_reader_loop if log else registry._reader_loop, f"proc-reader-{session.id}")
    except Exception:
        registry._reap_untracked(session, proc)
        raise
    # Reader exit and checkpoint cleanup cannot settle descendants or external effects.
    return session


def reap_untracked(registry, session, proc):
    """Post-Popen failure cannot force-kill a native job's only reaper."""
    from contextlib import suppress
    from tools import process_registry as facade
    if session.native_job_scope:
        from tools.process_registry_job import request_job_stop, job_quiescent
        retain_failed_start(registry, session)
        request_job_stop(registry, session)
        # An existing reader alone owns the output cursor and finish event.
        # Starting a second one can duplicate or overwrite the original result.
        if session._completion_event.is_set() or (session._reader_thread is not None
                                                   and session._reader_thread.is_alive()):
            return
        reader = registry._pty_reader_loop if session._pty is not None else registry._log_reader_loop
        if job_quiescent(session):
            reader(session)  # Collect this original job's status/output, never replay its command.
        elif session._reader_thread is None or not session._reader_thread.is_alive():
            try:
                registry._track_started(session, reader, f'proc-start-recovery-{session.id}')
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Native job reader recovery failed; retained checkpoint %s", session.id)
        return
    with suppress(Exception):
        if session.systemd_unit:
            # Scope teardown is the authoritative cleanup for the worker cgroup;
            # the systemd-run wrapper PID is terminated below as fallback.
            registry._stop_scope(session)
        # _terminate_host_pid revalidates the kernel start time captured at
        # spawn before signalling: pgid == pid would only prove the pid leads a
        # group, and a reaped child can be recycled onto an unrelated leader.
        registry._terminate_host_pid(proc.pid, session.host_start_time)
    with suppress(Exception):
        proc.wait(timeout=5)


def retain_failed_start(registry, session):
    """Keep partial native launch evidence; failure never authorizes command replay."""
    from tools.process_registry_job import job_quiescent
    session._scope_stop_pending = not job_quiescent(session)
    with registry._lock:
        if session.id not in registry._finished:
            registry._running[session.id] = session
    registry._write_checkpoint()
    if session.systemd_unit:
        registry._stop_scope(session)


class _ForegroundOutput:
    """Independent log cursor: foreground capture never owns the job's pipe."""

    def __init__(self, handle):
        self.handle = handle
        self.closed = threading.Event()

    def __iter__(self):
        with open(self.handle._hermes_job_session.output_log, 'rb') as stream:
            while not self.closed.is_set():
                chunk = stream.read(65536)
                if chunk:
                    yield chunk
                elif self.handle.poll() is not None:
                    return
                else:
                    # Yield permits the shared drain's stop event to transfer
                    # this cursor promptly even while the command is silent.
                    yield b''
                    self.closed.wait(.02)

    def close(self):
        self.closed.set()


class _ForegroundJob:
    """The foreground observes primary status; the native registry owns scope fate."""

    _hermes_native_foreground = True

    def __init__(self, registry, session, ownership):
        self.registry = registry
        self._hermes_job_session = session
        self._ownership = ownership
        self.stdout = _ForegroundOutput(self)

    def __getattr__(self, name):
        return getattr(self._hermes_job_session.process, name)

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        from tools.process_registry_job import read_job_receipt
        session = self._hermes_job_session
        receipt = read_job_receipt(session)
        if receipt and type(receipt['exit_code']) is int:
            return receipt['exit_code']
        return session.process.poll()

    def wait(self, timeout=None):
        from subprocess import TimeoutExpired
        deadline = None if timeout is None else time.monotonic() + timeout
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutExpired(self._hermes_job_session.command, timeout)
            self._hermes_job_session._completion_event.wait(.02)
        return code

    def kill(self):
        return self.registry.kill_process(self._hermes_job_session.id, source='session.interrupt',
                                          expected_ownership=self._ownership)


def spawn_foreground(session, argv, env, cwd, stdin_data):
    """Capture native scope before the first user-command instruction can run."""
    from tools import process_registry as facade
    from tools.environments.base_output import _pipe_stdin
    from tools.process_registry_handoff import ownership_identity, check_control_ownership
    from tools.process_registry_job import job_argv, job_receipt_path
    registry = facade.process_registry
    session.cwd = cwd
    argv = job_argv(session, argv, registry._daemon_term_grace_seconds())
    log = facade.worker_output_log(session.id) or job_receipt_path(session.id).with_name(session.id + '.log')
    output = facade.open_output_log(log)
    resource = reserve_local_process()
    proc, launch_identity = None, None
    session.output_log = str(log)
    try:
        try:
            start_local_process(resource)
            proc = facade.subprocess.Popen(
                argv, text=True, cwd=cwd, env=env, encoding='utf-8', errors='replace',
                stdout=output, stderr=facade.subprocess.STDOUT,
                stdin=facade.subprocess.PIPE if stdin_data is not None else facade.subprocess.DEVNULL,
                start_new_session=True)
            session.process, session.pid = proc, proc.pid
            session.host_start_time = registry._safe_host_start_time(proc.pid)
            launch_identity = ownership_identity(session)
        finally:
            os.close(output)
        record_local_process(resource, session.id, session.pid, session.host_start_time)
        registry._track_started(session, registry._log_reader_loop, f'proc-foreground-{session.id}')
        handle = _ForegroundJob(registry, session, launch_identity)
        if stdin_data is not None:
            _pipe_stdin(handle, stdin_data)
        return handle
    except BaseException as error:
        if proc is None:
            raise
        try:
            if check_control_ownership(registry, session, launch_identity, False) is None:
                registry._reap_untracked(session, proc)
        except Exception:
            logger.exception('Native launch cleanup uncertain; retained job %s', session.id)
        if isinstance(error, Exception):
            from tools.environments.base import CommandExecutionUnknown
            raise CommandExecutionUnknown(session.id) from error
        raise


def adopt_foreground(registry, proc, *, command, cwd, task_id, session_key, owner_task_id,
                     notify_on_complete):
    """Yield the captured job without inventing new identity from a raw PID."""
    session = proc._hermes_job_session
    with registry._lock, session._lock:
        if (registry._running.get(session.id) is not session or session.exited
                or (session.command, session.task_id, session.owner_task_id, session.session_key)
                != (command, task_id, owner_task_id, session_key)):
            raise RuntimeError('Foreground command already completed or ownership changed')
        session.persist_on_release = False
        session.notify_on_complete = notify_on_complete
    proc.stdout.close()
    registry._write_checkpoint()
    return session
