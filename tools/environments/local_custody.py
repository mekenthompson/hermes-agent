"""Foreground native commands retain one existing registry resource until their OS scope ends."""
import sys

from tools.approval_context import approval_task_revoked, current_approval_task_context, require_live_task_setup


def native_local_custody():
    if not sys.platform.startswith('linux'):
        return False
    if current_approval_task_context() is not None or approval_task_revoked():
        return True
    module = sys.modules.get('gateway.session_api_turn')
    execution = getattr(module, 'api_execution', None)
    current = execution.get() if execution is not None else None
    return current is not None and callable(current.get('native_effect_current'))


class ForegroundCustody:
    """Popen-shaped foreground handle; main-shell exit alone never satisfies poll/wait."""
    def __init__(self, registry, session):
        self.registry, self.session = registry, session
        self.custody = session._custody
        self.stdout = self.custody.proc.stdout
        self.stdin = self.custody.proc.stdin
        self.stderr = self.custody.proc.stderr
        self._hermes_process_session = session

    @property
    def pid(self):
        return self.session.pid

    @property
    def returncode(self):
        return self.custody.main_code if self.custody.settled() else None

    def poll(self):
        if self.custody.error:
            from tools.process_registry_custody import CustodyUnknown
            raise CustodyUnknown(self.session.reconciliation_required or self.custody.error)
        if self.custody.settled():
            require_live_task_setup()
            self.finish()
            return self.custody.main_code
        return None

    def wait(self, timeout=None):
        result = self.custody.wait_scope(timeout)
        require_live_task_setup()
        self.finish()
        return result

    def finish(self):
        if not self.custody.settled():
            from tools.process_registry_custody import CustodyUnknown
            raise CustodyUnknown('Foreground scope exit has not been confirmed.')
        if not self.session.exited:
            self.session.custody_scope_empty = True
            # The foreground drain owns its pipe. Do not close it before BaseEnvironment
            # joins that drain; moving the descriptor must not discard buffered output.
            self.session.process = None
            self.registry._finish_exited(self.session, self.custody.main_code)

    def terminate(self):
        result = self.registry.kill_process(self.session.id, source='foreground_stop', consume_output=False)
        if result.get('status') not in {'killed', 'already_exited'}:
            from tools.process_registry_custody import CustodyUnknown
            raise CustodyUnknown(self.session.reconciliation_required or 'Foreground process cleanup is unconfirmed.')

    kill = terminate


def spawn_foreground(argv, command, cwd, environment, *, stdin=False):
    from gateway.session_context import get_session_env
    from tools.process_registry import process_registry as registry
    from tools.process_registry_custody import spawn_custodied
    from tools.process_registry_spawn import _LOCAL_PENDING, retain_remote_unknown, reconcile_local_launch

    require_live_task_setup()
    evidence = current_approval_task_context()
    route = evidence.session if evidence else get_session_env('HERMES_SESSION_KEY', '')
    owner = evidence.task_id if evidence else get_session_env('HERMES_SESSION_ID', '')
    session = registry._new_session(command, owner, owner, route, cwd, foreground=True)
    session._native_local_launch = session._local_launch_pending = True
    if retain_remote_unknown(registry, session, _LOCAL_PENDING) is False:
        registry.kill_process(session.id, source='foreground_checkpoint_refused', consume_output=False)
        raise OSError('Native foreground process checkpoint failed; target was not started.')
    try:
        spawn_custodied(registry, session, argv, environment, reserve=False, stdin=stdin)
        with session._lock:
            session._local_launch_pending = False
            if session.reconciliation_required == _LOCAL_PENDING:
                session.reconciliation_required = ''
        registry._write_checkpoint()
        require_live_task_setup()
        return ForegroundCustody(registry, session)
    except Exception as exc:
        if not (session.exited and session.pid is None and session.completion_reason == 'failed_start'):
            reconcile_local_launch(registry, session)
        if session._local_launch_attempted:
            from tools.process_registry_custody import CustodyUnknown
            raise CustodyUnknown(session.reconciliation_required or str(exc)) from exc
        raise
