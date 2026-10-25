"""Quick-shell output and whole-scope lifetime share the existing native process descriptor."""
import asyncio
import os
import logging

from agent.i18n import t
from agent.memory_provider import spawn_context_thread

logger = logging.getLogger(__name__)


def _read_output(session, registry):
    custody = session._custody
    errors = []

    def stderr():
        try:
            custody.stderr = custody.proc.stderr.read()
        except (OSError, ValueError) as exc:
            errors.append(exc)

    reader = spawn_context_thread(stderr, name='quick-stderr-' + session.id)
    reader.start()
    try:
        custody.stdout = custody.proc.stdout.read()
        reader.join()
        custody.wait_event(custody.main_exited, timeout=None)
        if errors:
            raise errors[0]
    except (OSError, ValueError, RuntimeError) as exc:
        from tools.process_registry_custody import mark_unknown
        custody.error = str(exc)
        mark_unknown(registry, session, str(exc))
    finally:
        custody.output_ready.set()
    from tools.process_registry_custody import finish_custodied
    finish_custodied(registry, session)


def _spawn(session, registry, command, environment):
    from tools.approval_context import require_live_task_setup
    from tools.process_registry_custody import spawn_custodied
    from tools.process_registry_spawn import publish_local_launch, reconcile_local_launch
    try:
        spawn_custodied(registry, session, ['/bin/sh', '-c', command], environment, quick=True)
        publish_local_launch(registry, session, lambda value: _read_output(value, registry), 'quick-output-' + session.id)
    except Exception:
        if session._local_launch_attempted:
            reconcile_local_launch(registry, session)
        else:
            registry.kill_process(session.id, source='quick_launch_refused', consume_output=False)
        raise
    require_live_task_setup()
    return session


async def _retain_scope(session, registry):
    """Keep the actual OS scope in native Stop/drain after its initial output returns."""
    from gateway.session_command_process import CommandProcessUnknown, _settle
    try:
        await asyncio.to_thread(session._custody.wait_scope, None)
    except asyncio.CancelledError:
        result = await _settle(asyncio.create_task(asyncio.to_thread(registry.kill_process, session.id,
            source='native_quick_scope_stop', consume_output=False)))
        if result.get('status') not in {'killed', 'already_exited'}:
            raise CommandProcessUnknown(session.reconciliation_required or 'Native command scope cleanup is unconfirmed.')
        raise
    except (RuntimeError, OSError) as exc:
        raise CommandProcessUnknown(session.reconciliation_required or str(exc)) from exc


def _track_scope(session, registry):
    from tools.approval_context import track_approval_task_effect
    custody = session._custody
    custody.effect_task = asyncio.create_task(_retain_scope(session, registry))
    # The existing native lease owns this concrete effect; this handle retains it
    # after the RPC observer returns, without creating another execution owner.
    def finished(task):
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.debug('Native quick scope requires reconciliation',
                             exc_info=(type(error), error, error.__traceback__))
        if custody.effect_task is task:
            custody.effect_task = None

    custody.effect_task.add_done_callback(finished)
    track_approval_task_effect(custody.effect_task)


async def run_custodied_quick(command, cwd, timeout, evidence):
    from gateway.session_command_process import CommandProcessUnknown, _settle
    from tools.approval_context import require_live_task_setup, revoke_current_approval_task
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import process_registry as registry
    from tools.process_registry_spawn import _LOCAL_PENDING, retain_remote_unknown

    require_live_task_setup()
    session = registry._new_session(command, evidence.task_id, evidence.task_id, evidence.session, cwd or os.getcwd())
    session._native_local_launch = session._local_launch_pending = True
    if retain_remote_unknown(registry, session, _LOCAL_PENDING) is False:
        registry.kill_process(session.id, source='quick_checkpoint_refused', consume_output=False)
        raise CommandProcessUnknown('Native quick command checkpoint failed; target was not started.')
    launch = asyncio.create_task(asyncio.to_thread(_spawn, session, registry, command, build_subprocess_env()))

    async def cleanup():
        result = await _settle(asyncio.create_task(asyncio.to_thread(registry.kill_process, session.id,
            source='native_quick_stop', consume_output=False)))
        if result.get('status') not in {'killed', 'already_exited'}:
            raise CommandProcessUnknown(session.reconciliation_required or 'Native command cleanup is unconfirmed.')

    try:
        await asyncio.shield(launch)
        require_live_task_setup()
        custody = session._custody
        ready = await asyncio.wait_for(asyncio.to_thread(custody.output_ready.wait, timeout), timeout=timeout + 1)
        if not ready:
            raise TimeoutError()
        require_live_task_setup()
        if custody.error:
            raise CommandProcessUnknown(session.reconciliation_required or custody.error)
        output = (custody.stdout or custody.stderr).decode().strip()
        if output:
            from agent.redact import redact_sensitive_text
            output = redact_sensitive_text(output)
        output = output or t('gateway.quick_command.no_output')
        if not custody.main_remaining:
            await asyncio.to_thread(custody.wait_scope)
        if not custody.settled():
            if registry._write_checkpoint() is False:
                from tools.process_registry_custody import mark_unknown
                mark_unknown(registry, session, 'Custody checkpoint write failed after the native command started.')
                raise CommandProcessUnknown(output + '\n' + session.reconciliation_required)
            _track_scope(session, registry)
            return output + '\nNative process ' + session.id + ' retains running descendants; use its status or Stop to reconcile them.'
        return output
    except (TimeoutError, asyncio.CancelledError) as exc:
        revoke_current_approval_task()
        try:
            await _settle(launch)
        except Exception:
            logger.debug('Stopped quick launch settled through its existing process descriptor', exc_info=True)
        await cleanup()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return t('gateway.quick_command.timed_out')
    except Exception:
        await cleanup()
        raise
