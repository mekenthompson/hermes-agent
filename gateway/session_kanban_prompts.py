"""A card's clarification consumer is an existing, authorized native admission.

Only the receiving adapter can associate its admitted conversation with a card.
Task text, metadata and local wire callers never declare this exception.
"""
import asyncio
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3

from hermes_state_runtime import RuntimeStoreError, get_session_admission


async def consumer(authority, context, admission_id=None):
    from gateway.session_envelope import check_native_route, restore_native_source
    from gateway.session_local_recovery import local_adapter_map
    from gateway.session_contract import SessionRef
    from gateway.session_kanban_stop import require_current_claim
    def current():
        with closing(sqlite3.connect(Path(context['db']).as_uri() + '?mode=ro', uri=True)) as conn:
            require_current_claim(conn, context)
    await asyncio.to_thread(current)
    card = {key: context[key] for key in ('board', 'task_id', 'run_id', 'db')}
    if admission_id is not None:
        card['consumer_admission_id'] = admission_id
    for adapter in tuple(local_adapter_map(authority).values()):
        callback = getattr(adapter, 'kanban_prompt_consumer', None)
        if not callable(callback):
            continue
        candidate = await asyncio.to_thread(callback, card)
        if candidate is None or admission_id is not None and candidate != admission_id:
            continue
        row = get_session_admission(authority.db, admission_id=candidate)
        if (row is None or row['status'] == 'unknown' or not row['payload'].get('native_text_v1')):
            raise RuntimeStoreError('invalid_kanban_prompt_consumer')
        sid = authority.logical_owner(row['target_session_id'])
        source = restore_native_source(row['payload'], authority.runner)
        if authority.runner._adapter_for_source(source) is not adapter:
            raise RuntimeStoreError('invalid_kanban_prompt_consumer')
        envelope = row['payload']['native_text_v1']
        if envelope['event'].get('structured_control') or not envelope['source'].get('user_id') or envelope['source'].get('is_bot'):
            raise RuntimeStoreError('invalid_kanban_prompt_consumer')
        await check_native_route(authority.runner, row['payload'],
                                 authority.physical_target(SessionRef(authority.profile_id, sid)), source, adapter)
        await asyncio.to_thread(current)  # Stop during provider/route reauthorization wins.
        return candidate, row, adapter
    return None


async def bind_consumer(authority, policy):
    from dataclasses import replace
    context = json.loads(policy.kanban_json)
    if bound := await consumer(authority, context):
        context['prompt_consumer_admission_id'] = bound[0]
        return replace(policy, kanban_json=json.dumps(context), toolsets=tuple(sorted(set(policy.toolsets) | {'clarify'})))
    return policy


async def require_consumer(authority, policy):
    context = json.loads(policy.kanban_json)
    admission_id = context.get('prompt_consumer_admission_id')
    bound = await consumer(authority, context, admission_id) if admission_id else None
    if bound is None or bound[0] != admission_id:
        raise RuntimeStoreError('kanban_prompt_consumer_unavailable')
    return bound


async def check_response(authority, ref, kind):
    from gateway.session_policy import policy_for_source
    if kind == 'clarify':
        policy = policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
        if policy is not None and policy.kanban_json is not None:
            await require_consumer(authority, policy)


async def deliver(authority, ref, row, policy, worker, frame):
    """The worker's real clarify IPC joins the consumer's ordinary waiter registry.
    The existing adapter owns rendering and input authorization; the owner alone
    forwards that settled result to this generation's private worker pipe.
    """
    from gateway.session_envelope import restore_native_source
    from gateway.session_managed_worker import _prompt_frame
    from tools import clarify_gateway as clarify
    consumer_id, admitted, adapter = await require_consumer(authority, policy)
    source = restore_native_source(admitted['payload'], authority.runner)
    route = admitted['payload']['native_text_v1']['route']
    validate_frame(frame)
    controls = authority.sessions[ref.session_id].controls
    with authority.sessions[ref.session_id].event_stream.lock:
        authority.check_approval_generation(ref.session_id, row['generation'])
        if len(controls.remote_responders) >= 16 or frame['prompt_id'] in controls.pending:
            raise RuntimeStoreError('worker_control_backpressure')
        with clarify._lock:
            if frame['prompt_id'] in clarify._entries:
                raise RuntimeStoreError('invalid_worker_frame')
            entry = clarify.register(frame['prompt_id'], route, frame['question'], frame['choices'], frame['multi_select'])
        entry.producer = {'session_id': ref.session_id, 'generation': row['generation'],
                          'admission_id': row['admission_id'], 'consumer_admission_id': consumer_id}
        _prompt_frame(authority, ref, row, worker, frame, clarify_entry=entry)
        controls.remote_responders[entry.clarify_id] = lambda _kind, prompt_id, value: clarify.resolve_gateway_clarify(prompt_id, value)

    async def relay():
        forwarded = False
        try:
            timeout = clarify.resolve_clarify_timeout(policy.config())
            response = await asyncio.to_thread(clarify.wait_for_response, entry.clarify_id, timeout)
            if response is not None and response != clarify.CANCELLED:
                await require_consumer(authority, policy)
                authority.check_approval_generation(ref.session_id, row['generation'])
                if not worker.stop.is_set():
                    worker.respond('clarify', entry.clarify_id, response)
                    forwarded = True
        finally:
            retire_prompt(entry)
            controls.remote_responders.pop(entry.clarify_id, None)
            controls.snapshot(ref.session_id, row['generation'])
            retire = getattr(adapter, 'retire_clarify_card', None)
            if not forwarded and callable(retire):
                await retire(entry.clarify_id, 'The question is no longer waiting for an answer.')

    task = asyncio.create_task(relay())
    if not hasattr(worker, 'clarify_tasks'):
        worker.clarify_tasks = {}
    worker.clarify_tasks[entry.clarify_id] = (entry, task)
    def settled(done):
        worker.clarify_tasks.pop(entry.clarify_id, None)
        if not done.cancelled() and (error := done.exception()) is not None:
            logging.getLogger(__name__).warning('Card clarification withdrawn (%s)', type(error).__name__)
    task.add_done_callback(settled)
    result = await adapter.send_clarify(source.chat_id, entry.question, entry.choices, entry.clarify_id,
                                       route, metadata={'thread_id': source.thread_id})
    if not result.success and not result.retryable:
        retire_prompt(entry)


def retire_prompt(entry):
    from tools import clarify_gateway as clarify
    clarify.resolve_gateway_clarify(entry.clarify_id, clarify.CANCELLED)


async def retire_worker(worker):
    pending = getattr(worker, 'clarify_tasks', {})
    for entry, _task in pending.values():
        retire_prompt(entry)
    if pending:
        await asyncio.gather(*(task for _entry, task in pending.values()), return_exceptions=True)
        pending.clear()


def validate_frame(frame):
    if (set(frame) != {'type', 'prompt_id', 'question', 'choices', 'multi_select'}
            or not isinstance(frame['prompt_id'], str) or not frame['prompt_id']
            or not isinstance(frame['question'], str) or not isinstance(frame['choices'], list)
            or any(not isinstance(c, str) for c in frame['choices']) or type(frame['multi_select']) is not bool):
        raise RuntimeStoreError('invalid_worker_frame')


async def dispatch_prompt_frame(authority, ref, row, policy, worker, frame):
    from gateway.session_managed_worker import _prompt_frame
    if frame.get('type') == 'clarify' and policy.kanban_json is not None:
        await deliver(authority, ref, row, policy, worker, frame)
        return True
    with authority.sessions[ref.session_id].event_stream.lock:
        return _prompt_frame(authority, ref, row, worker, frame)
