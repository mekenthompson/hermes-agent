"""Native local process custody using the existing process descriptor and lease.

The controller channel reports OS facts only. It never confers tool authority.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading

logger = logging.getLogger('tools.process_registry')


class CustodyUnknown(RuntimeError):
    """The controlled resource has no verified whole-scope settlement receipt."""


class LocalCustody:
    def __init__(self, registry, session):
        self.registry, self.session = registry, session
        self.socket, self.child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.write_lock = threading.Lock()
        self.ready = threading.Event()
        self.started = threading.Event()
        self.main_exited = threading.Event()
        self.empty = threading.Event()
        self.scope_observed = threading.Event()
        self.output_ready = threading.Event()
        self.proc = None
        self.error = ''
        self.uncertain = ''
        self.main_code = None
        self.main_remaining = True
        self.stop_source = ''
        self.stdout, self.stderr = b'', b''

    def send(self, operation, **values):
        with self.write_lock:
            self.socket.send(json.dumps({'op': operation, **values}).encode())

    def receive(self):
        try:
            while True:
                packet = self.socket.recv(65536)
                if not packet:
                    if not self.empty.is_set():
                        self.error = 'Custody channel ended without whole-scope settlement proof.'
                    break
                message = json.loads(packet)
                kind = message.get('kind')
                handler = {'ready': self._ready, 'started': self._started,
                    'main_exit': self._main_exit, 'scope_empty': self._empty,
                    'unknown': self._unknown, 'refused': self._refused}.get(kind)
                if handler is None:
                    raise CustodyUnknown('Unexpected custody control receipt.')
                handler(message)
        except (OSError, ValueError, KeyError, CustodyUnknown) as exc:
            if not self.empty.is_set():
                self.error = str(exc)
        finally:
            self.ready.set()
            self.started.set()
            self.main_exited.set()
            self.scope_observed.set()
            if self.error and not self.session.exited:
                mark_unknown(self.registry, self.session, self.error)

    def _ready(self, message):
        if message.get('pid') != self.proc.pid or message.get('birth') != self.session.custody_start_time:
            raise CustodyUnknown('Custodian identity could not be confirmed.')
        self.ready.set()

    def _started(self, message):
        with self.session._lock:
            if not self.session._local_launch_attempted:
                raise CustodyUnknown('Target started without the native launch gate.')
            self.session.pid = message['pid']
            self.session.host_start_time = message['birth']
        self.registry._write_checkpoint()
        self.started.set()

    def _main_exit(self, message):
        self.main_code = message['code']
        self.main_remaining = message['remaining']
        self.main_exited.set()

    def _empty(self, message):
        self.main_code = message.get('code')
        self.empty.set()
        self.scope_observed.set()

    def _unknown(self, message):
        self.uncertain = str(message.get('error') or 'Custody could not be confirmed.')
        mark_unknown(self.registry, self.session, self.uncertain)

    def _refused(self, message):
        self.error = str(message.get('error') or 'Custody could not be confirmed.')

    def wait_event(self, event, timeout=15):
        if not event.wait(timeout) or self.error:
            raise CustodyUnknown(self.error or self.uncertain or 'Custody receipt did not arrive before its deadline.')

    def settled(self):
        return bool(self.empty.is_set() and self.proc is not None and self.proc.poll() is not None)

    def wait_scope(self, timeout=15):
        self.wait_event(self.scope_observed, timeout)
        self.proc.wait(timeout=timeout)
        if not self.settled():
            raise CustodyUnknown('Custodian exit is not whole-scope settlement proof.')
        self.session.custody_scope_empty = True
        return self.main_code

    def stop(self):
        from tools.process_registry import ProcessRegistry
        if self.settled():
            return self.main_code
        if (self.proc is None or not ProcessRegistry._host_pid_is_ours(
                self.session.custody_pid, self.session.custody_start_time)):
            raise CustodyUnknown('Custodian identity is missing or stale; no process was signalled.')
        self.send('stop')
        return self.wait_scope()

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            logger.debug('Custody channel already ended', exc_info=True)
        try:
            self.socket.close()
        except OSError:
            logger.debug('Custody channel already closed', exc_info=True)


class CustodyPty:
    def __init__(self, custody):
        self.custody = custody

    @property
    def pid(self):
        return self.custody.session.pid

    @property
    def exitstatus(self):
        return self.custody.main_code if self.custody.settled() else None

    def isalive(self):
        return not self.custody.settled()

    def read(self, size):
        data = os.read(self.custody.proc.stdout.fileno(), size)
        if not data:
            raise EOFError()
        return data

    def write(self, value):
        self.custody.proc.stdin.write(value if isinstance(value, bytes) else value.encode())
        self.custody.proc.stdin.flush()

    def sendeof(self):
        self.write(b'\x04')

    def setwinsize(self, rows, cols):
        self.custody.send('resize', rows=rows, cols=cols)

    def wait(self):
        return self.custody.wait_scope(timeout=None)

    def terminate(self, force=False):
        self.custody.stop()

    def close(self):
        if self.custody.proc.stdin is not None:
            self.custody.proc.stdin.close()


def mark_unknown(registry, session, reason):
    session.reconciliation_required = reason + ' Reconcile this native process and its effects before retrying.'
    if registry._write_checkpoint() is False:
        session.reconciliation_required += (' Checkpoint write failed; only the in-memory process record '
            + session.id + ' remains (custodian PID ' + str(session.custody_pid)
            + ', birth ' + str(session.custody_start_time) + ').')


def stop_before_go(registry, session):
    """The descriptor lock fences GO; settle the armed keeper before claiming no start."""
    try:
        session._custody.stop()
    except (CustodyUnknown, OSError, subprocess.SubprocessError) as exc:
        mark_unknown(registry, session, str(exc))
        return {'status': 'unknown', 'session_id': session.id,
                'reconciliation_required': session.reconciliation_required}
    return None


def _refuse_custodian_start(registry, session, custody=None):
    """Only definite creation refusal may settle a keeper that never received GO."""
    if custody is not None:
        custody.close()
        custody.child_socket.close()
    from tools.process_registry_spawn import cancel_unstarted_local_launch
    with session._lock:
        session._custody = None
        session.custody_kind = ''
    cancel_unstarted_local_launch(registry, session, 'local_custodian_spawn_refused')


def spawn_custodied(registry, session, argv, env, *, pty=False, quick=False, log=None, reserve=True, stdin=False):
    """Arm custody and checkpoint its identity before authorizing the target's first fork."""
    from hermes_cli.process_custody import NATIVE_CUSTODIAN_SOURCE
    from tools import process_registry as facade
    from tools.approval_context import require_live_task_setup, reserve_background_approval_task

    require_live_task_setup()
    with session._lock:
        if session.exited or session.termination_source:
            raise CustodyUnknown('Native process was cancelled before custody start.')
    try:
        custody = LocalCustody(registry, session)
    except OSError:
        _refuse_custodian_start(registry, session)
        raise
    session._custody = custody
    session.custody_kind = 'linux_subreaper'
    if reserve and getattr(session, 'approval_task_reservation', None) is None:
        session.approval_task_reservation = reserve_background_approval_task()
    config = json.dumps({'argv': argv, 'pty': pty, 'persist': session.persist_on_release, 'grace': registry._daemon_term_grace_seconds()})
    try:
        output = facade.open_output_log(log) if log else subprocess.PIPE
    except OSError:
        _refuse_custodian_start(registry, session, custody)
        raise
    try:
        proc = facade.subprocess.Popen([sys.executable, '-I', '-S', '-c', NATIVE_CUSTODIAN_SOURCE,
            str(custody.child_socket.fileno()), config], cwd=session.cwd, env=env,
            stdin=subprocess.PIPE if pty or stdin else subprocess.DEVNULL, stdout=output,
            stderr=subprocess.PIPE if quick else subprocess.STDOUT,
            pass_fds=(custody.child_socket.fileno(),), start_new_session=True,
            text=not (quick or pty), **({'encoding': 'utf-8', 'errors': 'replace'} if not (quick or pty) else {}))
    except OSError:
        _refuse_custodian_start(registry, session, custody)
        raise
    else:
        # Retain the returned resource even if closing a parent FD now fails.
        custody.proc = session.process = proc
    finally:
        custody.child_socket.close()
        if log:
            os.close(output)
    session.output_log = str(log or '')
    session.custody_pid = proc.pid
    session.custody_start_time = registry._safe_host_start_time(proc.pid)
    from contextvars import copy_context
    threading.Thread(target=copy_context().run, args=(custody.receive,), daemon=True,
                     name='custody-control-' + session.id).start()
    try:
        custody.wait_event(custody.ready)
        if not session.custody_start_time:
            raise CustodyUnknown('Custodian birth could not be confirmed; target was not started.')
        if registry._write_checkpoint() is False:
            raise CustodyUnknown('Custody checkpoint could not be written; target was not started.')
        with session._lock:
            require_live_task_setup()
            if session.exited or session.termination_source:
                raise CustodyUnknown('Native process was cancelled before target start.')
            session._local_launch_attempted = True
            custody.send('go')
        custody.wait_event(custody.started)
        require_live_task_setup()
        if pty:
            session._pty = CustodyPty(custody)
        return proc
    except Exception:
        try:
            custody.stop()
        except (OSError, CustodyUnknown, subprocess.SubprocessError):
            logger.debug('Custody setup cleanup is unconfirmed', exc_info=True)
        raise


def finish_custodied(registry, session):
    custody = session._custody
    if custody is None:
        return False
    try:
        code = custody.wait_scope(timeout=None)
    except (CustodyUnknown, OSError, subprocess.SubprocessError) as exc:
        mark_unknown(registry, session, str(exc))
        return True
    finally:
        custody.output_ready.set()
    session.custody_scope_empty = True
    if custody.stop_source:
        return True  # the existing kill caller snapshots/consumes output before publishing exit
    registry._finish_exited(session, code)
    custody.close()
    return True


def stop_custodied(registry, session, source):
    if not session.custody_kind:
        return None
    custody = session._custody
    if custody is None:
        mark_unknown(registry, session, 'Custody control was lost; recorded PID/birth alone cannot prove scope exit.')
        return {'status': 'unknown', 'session_id': session.id, 'reconciliation_required': session.reconciliation_required}
    custody.stop_source = source
    try:
        custody.stop()
    except (CustodyUnknown, OSError, subprocess.SubprocessError) as exc:
        mark_unknown(registry, session, str(exc))
        return {'status': 'unknown', 'session_id': session.id, 'reconciliation_required': session.reconciliation_required}
    reader = session._reader_thread
    if reader is not None and reader is not threading.current_thread() and not custody.output_ready.wait(5):
        mark_unknown(registry, session, 'Custody scope ended, but its output receipt is not yet drained.')
        return {'status': 'unknown', 'session_id': session.id, 'reconciliation_required': session.reconciliation_required}
    with session._lock:
        session.custody_scope_empty = True
        session._local_launch_pending = False
        session.reconciliation_required = ''
    return None  # preserve the existing kill/output/consumption/notification finalizer
