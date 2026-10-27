"""Hold only the real managed worker's finished frame at its native parent."""
import json
import os
from pathlib import Path
import runpy

from gateway.session_managed_worker import ManagedWorker
from gateway.session_authority import SessionAuthority
from gateway import session_ancillary
from hermes_state_runtime import RuntimeStoreError
from tests.waiting import async_wait_until
from tools.process_registry import process_registry

home = Path(os.environ['HERMES_HOME'])
next_frame, kill = ManagedWorker.next_frame, process_registry.kill_process
drain = SessionAuthority._drain
interrupt = session_ancillary.interrupt_processes


async def unreadable_ownership(authority, ref, agent):
    fault = home / 'unreadable-ownership'
    if fault.exists():
        with process_registry._lock:
            owned = [p for p in (*process_registry._running.values(), *process_registry._finished.values())
                     if p.owner_task_id == ref.session_id]
        assert len(owned) == 1, owned
        path = home / 'logs' / 'process-results' / (owned[0].id + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{unreadable native retained ownership')
    try:
        return await interrupt(authority, ref, agent)
    except RuntimeStoreError as exc:
        if fault.exists() and exc.reason == 'process_stop_unknown':
            (home / 'ownership-read-refused').touch()
        raise


async def observed_drain(self, ref):
    try:
        return await drain(self, ref)
    finally:
        from gateway.session_ancillary import process_stop_pending
        from hermes_state_runtime import list_session_admissions
        from tools.process_registry_job import read_job_receipt
        with process_registry._lock:
            processes = list(process_registry._running.values()) + list(process_registry._finished.values())
        phase = {'refused': (home / 'refuse-cleanup').exists(),
                 'stop': process_stop_pending(self.db, ref.session_id),
                 'admissions': [(r['request_id'], r['status'], r['outcome']) for r in
                                list_session_admissions(self.db, session_id=ref.session_id, pending_only=False)],
                 'processes': [{'id': p.id, 'exited': p.exited, 'pending': p._scope_stop_pending,
                                'complete': p._result_complete, 'durable': p._result_persisted_generation,
                                'generation': p.ownership_generation, 'event': p._completion_event.is_set(),
                                'receipt': read_job_receipt(p)} for p in processes if p.owner_task_id == ref.session_id]}
        with (home / 'native-drain-phases.jsonl').open('a') as stream:
            stream.write(json.dumps(phase) + '\n')


async def held_finished(self, *args, **kwargs):
    frame = await next_frame(self, *args, **kwargs)
    if frame == {'type': 'finished'} and not (home / 'parent-held-finished.json').exists():
        (home / 'parent-held-finished.json').write_text(json.dumps({'worker_pid': self.process.pid}))
        await async_wait_until(lambda: (home / 'parent-release-finished').exists(), timeout=90,
                               what='test releases native finished frame')
    return frame


def refuse_cleanup(session_id, **kwargs):
    if (home / 'refuse-cleanup').exists() and kwargs.get('source') == 'session.interrupt':
        return {'status': 'error', 'error': 'fixture native termination refusal'}
    return kill(session_id, **kwargs)


ManagedWorker.next_frame = held_finished
SessionAuthority._drain = observed_drain
session_ancillary.interrupt_processes = unreadable_ownership
process_registry.kill_process = refuse_cleanup
runpy.run_module('gateway.run', run_name='__main__')
