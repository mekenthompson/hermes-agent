"""Bound native quick-shell effects to their existing command lease and process evidence."""
import asyncio
import logging

from agent.i18n import t

logger = logging.getLogger('gateway.run')


class CommandProcessUnknown(RuntimeError):
    """A stopped command is not evidence that its process or external effects stopped."""


def _retain_unknown(proc, command, cwd, evidence):
    from tools.process_registry import process_registry as registry
    existing = getattr(proc, '_hermes_unknown_process', None)
    if existing:
        return existing
    owner = evidence.task_id if evidence is not None else ''
    session = registry._new_session(command, owner, owner, evidence.session if evidence else '', cwd)
    session.detached = True
    session.pid = getattr(proc, 'pid', None)
    session.host_start_time = getattr(proc, '_hermes_host_start_time', None)
    session.reconciliation_required = 'Native quick-command cleanup is unconfirmed. Reconcile the actual process and external effects before retrying.'
    with registry._lock:
        registry._running[session.id] = session
    registry._write_checkpoint()
    if proc is not None:
        proc._hermes_unknown_process = session.id
    return session.id


async def stop_exec_tree(proc):
    """Use native PID identity and tree cleanup; an exit acknowledgement alone is insufficient."""
    from tools.process_registry import ProcessRegistry
    expected = getattr(proc, '_hermes_host_start_time', None)
    if expected is None or not ProcessRegistry._host_pid_is_ours(proc.pid, expected):
        # The shell's exit does not prove that children holding its pipes exited.
        raise CommandProcessUnknown('Quick command process identity or descendant exit cannot be confirmed.')
    import psutil
    from agent.deadline import kill_process_tree
    root = psutil.Process(proc.pid)
    descendants = root.children(recursive=True)
    def terminate():
        # The worker may wait for an executor slot; recheck its PID birth there too.
        if not ProcessRegistry._host_pid_is_ours(proc.pid, expected):
            raise CommandProcessUnknown('Quick command process identity changed before cleanup.')
        kill_process_tree(proc.pid)
        # SIGKILL delivery precedes physical exit; wait before judging survivors.
        psutil.wait_procs(descendants, timeout=5)
    await asyncio.to_thread(terminate)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError as exc:
        raise CommandProcessUnknown('Quick command shell exit is unconfirmed.') from exc
    if any(ProcessRegistry._proc_alive(child) for child in descendants):
        raise CommandProcessUnknown('Quick command descendants are still running.')


async def _spawn(exec_cmd, cwd, environment):
    from tools.approval_context import require_live_task_setup
    from tools.process_registry import ProcessRegistry
    require_live_task_setup()
    proc = await asyncio.create_subprocess_shell(exec_cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment, cwd=cwd, start_new_session=True)
    proc._hermes_host_start_time = ProcessRegistry._safe_host_start_time(proc.pid)
    return proc


async def _settle(task):
    """Repeated observer/Stop cancellation cannot detach a spawn receipt or cleanup."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _execute(exec_cmd, cwd, timeout, stop_tree, evidence):
    from tools.approval_context import require_live_task_setup, revoke_current_approval_task
    from tools.environments.local import build_subprocess_env
    proc = None

    async def cleanup():
        if proc is None:
            return
        try:
            await _settle(asyncio.create_task(stop_tree(proc)))
        except (Exception, asyncio.CancelledError) as exc:
            logger.exception('Native quick command process cleanup is unconfirmed')
            try:
                process_id = _retain_unknown(proc, exec_cmd, cwd, evidence)
                detail = 'Reconcile native process ' + process_id + ' and external effects before retrying.'
            except Exception:
                logger.exception('Could not retain native quick command process reconciliation')
                detail = 'Process ' + str(proc.pid) + ' remains unconfirmed; inspect its identity and effects before retrying.'
            raise CommandProcessUnknown(detail) from exc

    try:
        require_live_task_setup()
        environment = build_subprocess_env()
        require_live_task_setup()
        # Cancellation may arrive after fork but before asyncio returns the process.
        # Keep that receipt alive and join it before claiming physical Stop.
        spawn = asyncio.create_task(_spawn(exec_cmd, cwd, environment))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            try:
                proc = await _settle(spawn)
            except asyncio.CancelledError as exc:
                process_id = _retain_unknown(None, exec_cmd, cwd, evidence)
                raise CommandProcessUnknown('Native launch receipt lost; reconcile process ' + process_id + ' before retrying.') from exc
            raise
        require_live_task_setup()
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        require_live_task_setup()
        output = (stdout or stderr).decode().strip()
        if output:
            from agent.redact import redact_sensitive_text
            output = redact_sensitive_text(output)
        return output or t('gateway.quick_command.no_output')
    except (TimeoutError, asyncio.CancelledError) as stopped:
        revoke_current_approval_task()
        await cleanup()
        if isinstance(stopped, asyncio.CancelledError):
            raise
        return t('gateway.quick_command.timed_out')
    except CommandProcessUnknown:
        raise
    except Exception as exc:
        logger.debug('Native quick command failed', exc_info=True)
        revoke_current_approval_task()
        if proc is not None and proc.returncode is None:
            await cleanup()
        return t('gateway.quick_command.error', error=exc)


async def execute_quick_command(exec_cmd, cwd, timeout, stop_tree):
    """The RPC remains a coordinator; native Stop owns and joins the actual shell consumer."""
    from tools.approval_context import (current_approval_task_context, require_live_task_setup,
                                        track_approval_task_effect)
    require_live_task_setup()
    evidence = current_approval_task_context()
    effect = asyncio.create_task(_execute(exec_cmd, cwd, timeout, stop_tree, evidence))
    try:
        track_approval_task_effect(effect)
        return await asyncio.shield(effect)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            if not effect.cancelling():
                effect.cancel()
            try:
                await asyncio.shield(effect)
            except asyncio.CancelledError:
                pass
            raise
        return t('gateway.quick_command.error', error='The native command stopped; its process cleanup completed.')
    except CommandProcessUnknown as exc:
        return t('gateway.quick_command.error', error=exc)
