"""In-process worker execution with native persistence and an in-memory owner transport."""
from contextlib import closing, contextmanager
import json
from pathlib import Path


@contextmanager
def managed_worker_owner(frame, monkeypatch):
    from agent import runtime_session_store
    from agent.managed_worker import hello, validate_bootstrap
    from hermes_state import SessionDB
    from hermes_state_runtime import (
        admit_session_input, adopt_worker_execution, begin_runtime_epoch,
        claim_session_input, mutate_worker_execution, register_worker_execution,
    )
    from tools import async_delegation_worker, managed_process_proxy

    frame['scope']['birth'] = hello()['birth']
    validate_bootstrap(frame)
    scope = frame['scope']
    calls = []
    with closing(SessionDB(Path(frame['home']) / 'fixture-owner.db')) as db:
        db.create_session(scope['session_id'], source='cli')
        epoch = begin_runtime_epoch(db, instance_id='managed-worker-fixture')
        assert epoch == scope['epoch']
        admit_session_input(db, epoch=epoch, principal_id=frame['user_id'],
                            session_id=scope['session_id'], request_id='fixture-input',
                            payload={'text': frame['text']})
        claimed = claim_session_input(db, epoch=epoch, session_id=scope['session_id'])
        assert claimed['generation'] == scope['generation']
        assignment = {key: scope[key] for key in ('execution_id', 'session_id', 'generation')}
        register_worker_execution(db, epoch=epoch, **assignment, kind='compute', adoption_secret=scope['secret'])

        class OwnerTransport:
            def send(self, request_id, message):
                self.request = json.loads(message)
                assert self.request['id'] == request_id

            def receive(self, timeout):
                method, params = self.request['method'], dict(self.request['params'])
                expected = {key: value for key, value in scope.items() if key != 'epoch' or method != 'worker.adopt'}
                assert {key: params.pop(key) for key in expected} == expected
                calls.append((method, dict(params)))
                if method == 'worker.adopt':
                    assert not params
                    result = adopt_worker_execution(db, epoch=epoch, **assignment, adoption_secret=scope['secret'])
                else:
                    assert method == 'worker.persist'
                    result = mutate_worker_execution(db, epoch=epoch, **assignment, **params)
                return {'id': self.request['id'], 'result': result}

        transport = OwnerTransport()
        # Restore process-wide bindings after simulating one interpreter's successful adoption.
        with monkeypatch.context() as patches:
            patches.setattr(runtime_session_store, '_worker_process', runtime_session_store._worker_process)
            patches.setattr(managed_process_proxy, '_STORE', None)
            patches.setattr(async_delegation_worker, '_store', None)
            patches.setattr(runtime_session_store.WorkerRPC, '_connection', lambda self: transport)
            yield calls
