"""A delegated child's store inside a worker: owner-reserved, never self-registered (#141793).

The parent's ``RuntimeSessionStore`` asks the owner for a child on its own receipt stream; the
owner mints the child's session row and execution (gateway/session_worker_child.py) and the
child persists through the ordinary ``worker.persist`` path under its own claim. A refusal
(capacity, depth, draining) is a ValueError so ``delegate_task`` reports it as a tool error.
"""
import secrets
from functools import partial

from agent.runtime_session_store import RuntimeSessionStore, call_off_event_loop

_REFUSALS = {
    'delegation_capacity': 'the gateway owner refused another subagent: this execution already runs '
                           'delegation.max_concurrent_children of them',
    'delegation_depth_exceeded': 'the gateway owner refused a subagent this deep (delegation.max_spawn_depth)',
}


class ChildSessionStore(RuntimeSessionStore):
    binds_delegation_ledger = False

    def account_processes(self):
        if self._process_scope_facts is None or self._process_cancelled:
            return {}
        return call_off_event_loop(self._apply, 'child.account', {})

    def bind_process_scope(self, facts):
        from agent.runtime_session_store import call_off_event_loop
        with self._process_control_lock:
            if self._process_cancelled:
                raise ValueError('The native child was stopped before process setup.')
            if self._process_scope_facts is not None:
                if self._process_scope_facts != facts:
                    raise ValueError('The original native child process scope cannot change.')
                return
        call_off_event_loop(self._apply, 'child.bind', facts)
        with self._process_control_lock:
            if self._process_cancelled:
                raise ValueError('The native child was stopped during process setup.')
            self._process_scope_facts = facts

    def cancel_process_scope(self):
        from agent.runtime_session_store import call_off_event_loop
        with self._process_control_lock:
            if self._process_cancel_acked:
                return
            self._process_cancelled = True
        # A separate existing WorkerRPC connection avoids an in-flight process.call
        # monopolizing the data connection or the receipt journal's lock.
        call_off_event_loop(self._process_control_rpc, 'worker.persist', **dict(self.scope),
            sequence=max(0, self.journal['next_sequence'] - 1), operation='child.cancel', payload={})
        with self._process_control_lock:
            self._process_cancel_acked = True

    @property
    def reserved_session_id(self):
        return self.scope['session_id']

    def create_session(self, session_id, source, **fields):
        # The owner created this row at reservation with its source, lineage and profile; only
        # the constructor facts are the child's to add.
        from hermes_state_worker_lifecycle import _CONSTRUCTOR_CONFIG
        config = {k: v for k, v in (fields.get('model_config') or {}).items() if k in _CONSTRUCTOR_CONFIG}
        return super().create_session(session_id, 'subagent', model=fields.get('model'),
                                      model_config=config or None, system_prompt=fields.get('system_prompt'))

    def close(self):
        # The child's agent closes once, after its last write: that is this execution's end.
        try:
            if not self._outbox_owner.closed and not self.failure:
                self.finish()
        finally:
            try:
                super().close()
            finally:
                self._process_control_rpc.close()


def reserve_child_store(parent):
    """A ``ChildSessionStore`` the owner reserved under ``parent``'s verified execution."""
    secret = secrets.token_urlsafe(32)
    # Reached from delegate_task's tool callback, which can run on Relay's loop (#139029).
    result = call_off_event_loop(parent._apply, 'child.reserve', {'secret': secret})
    if 'refused' in result:
        raise ValueError(_REFUSALS.get(result['refused'], 'the gateway owner refused a subagent: ' + result['refused']))
    child = result['child']
    scope = dict(parent.scope, **child, secret=secret)
    outbox = parent.path.parent / ('child-' + child['execution_id'].rsplit(':', 1)[1])
    # Every child RPC hops off a running loop, not only this reservation: the child is built, run
    # and closed inside delegate_task's callback, and its constructor already reads the store
    # (ContextCompressor.bind_session_state), which WorkerRPC refuses on Relay's loop.
    store = ChildSessionStore(partial(call_off_event_loop, parent.rpc), scope, outbox)
    from agent.runtime_session_store import WorkerRPC
    import threading
    store._process_control_rpc = WorkerRPC(scope['profile_id'])
    store._process_control_lock = threading.RLock()
    store._process_cancelled = False
    store._process_cancel_acked = False
    store._process_scope_facts = None
    from tools.managed_process_proxy import _STORE
    store.process_frame = _STORE[1] if _STORE is not None else None
    return store
