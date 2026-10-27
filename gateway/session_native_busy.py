"""Busy effects of authenticated native input, after its durable admission.

This is the messaging counterpart of queued-input steering. It grants no
permission and adds no queue: complete native envelopes stay in the owner FIFO.
"""
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

from gateway.platforms.event import MessageType
from hermes_state_runtime import RuntimeStoreError, cancel_session_input


@dataclass(frozen=True)
class Prepared:
    session_id: str
    route: str
    generation: int
    agent: object
    stop_epoch: int
    stop_sequence: int
    approval: tuple | None = None
    approval_text: str | None = None


def prepare(authority, event):
    """Capture a specific running generation/approval; never apply a busy effect."""
    source, runner = event.source, authority.runner
    route = runner.session_store._generate_session_key(source)
    entry = runner.session_store.lookup_by_session_key(route)
    if entry is None:
        return None
    session_id = authority.logical_owner(entry.session_id)
    generation = authority.executing.get(session_id)
    adopted_generation, agent = authority.adopted.get(session_id, (None, None))
    if generation is None or generation != adopted_generation or agent is None:
        return None
    from gateway.session_ancillary import process_stop_pending
    if (authority.delivered_stops.get(session_id) == generation
            or process_stop_pending(authority.db, session_id) is not None):
        return None  # later input stays queued; it must not steer the stopped generation
    approval = None
    if event.allow_gateway_control:
        from tools.approval import list_gateway_approvals
        words = getattr(runner, '_plaintext_approval_words', dict)()
        if match := words.get((event.text or '').strip().lower()):
            verb, raw_args = match
            pending = list_gateway_approvals(route)
            args = raw_args.split()
            targets = pending if 'all' in args else pending[:1]
            ids = tuple(item.get('request_id') for item in targets)
            if ids and all(isinstance(value, str) and value for value in ids):
                choice = ('always' if 'always' in args else 'session' if 'session' in args else 'once')
                approval = (verb, choice, ids)
            elif pending:
                approval = ('unbound', '', ())  # no bare-route fallback
    from hermes_state_runtime import native_stop_watermark
    return Prepared(session_id, route, generation, agent, authority.route_epoch(route),
                    native_stop_watermark(authority.db, session_id), approval, event.text if approval else None)


def _current(authority, event, prepared):
    from hermes_state_runtime import _epoch, native_stop_watermark
    authority._require_admission_open()
    with authority.db._read_ctx() as conn:
        _epoch(conn, authority.epoch)
    return (authority.route_epoch(prepared.route) == prepared.stop_epoch
            and native_stop_watermark(authority.db, prepared.session_id) == prepared.stop_sequence
            and authority.runner._is_user_authorized_for_source(event.source)
            and authority.delivered_stops.get(prepared.session_id) != prepared.generation)


async def apply(authority, event, receipt, prepared, fence):
    """Only a newly committed input may steer/approve/interrupt its captured target."""
    if prepared is None or receipt.ref.session_id != prepared.session_id:
        return
    from gateway.session_envelope import check_native_route, restore_native
    from hermes_state_runtime import get_session_admission
    row = get_session_admission(authority.db, admission_id=receipt.admission_id)
    if row['payload']['native_text_v1']['route'] != prepared.route:
        return  # a changed admission route cannot borrow the captured running target
    event = restore_native(row['payload'], authority.runner)
    from gateway.session_runtime_workers import tracked_write
    if not _current(authority, event, prepared):
        await tracked_write(authority, partial(cancel_session_input, authority.db,
            epoch=authority.epoch, admission_id=receipt.admission_id))
        return
    if fence is not None:
        try:
            fence()
        except RuntimeStoreError:
            await tracked_write(authority, partial(cancel_session_input, authority.db,
                epoch=authority.epoch, admission_id=receipt.admission_id))
            return
    # Recheck the committed sender's connector/route and current roles. The
    # caller may have changed any part of its event while the write was pending.
    await check_native_route(authority.runner, row['payload'], authority.physical_target(receipt.ref),
        event.source, authority.runner._adapter_for_source(event.source))
    if event.message_type != MessageType.TEXT or event.media_urls or event.media_types:
        return  # a complete media envelope must run as its own native turn
    runner, agent, route = authority.runner, prepared.agent, prepared.route
    mode = runner._effective_busy_input_mode(event.source)
    demoted_children = mode == 'interrupt' and runner._agent_has_active_subagents(agent)
    demoted_compression = mode == 'interrupt' and await runner._session_has_compression_in_flight(route)
    if demoted_children or demoted_compression:
        mode = 'queue'
    if runner._effective_busy_text_mode(event.source) == 'queue' and mode != 'steer':
        mode = 'queue'
    from gateway.session_queued_controls import transfer_native_input, _receiving_agent
    current = lambda: _current(authority, event, prepared)
    steered = redirected = False
    if prepared.approval is not None:
        # Prompt IDs were captured before admission; a different committed
        # instruction cannot borrow that earlier answer or its prompt targets.
        if not event.allow_gateway_control or event.text != prepared.approval_text:
            return
        verb, choice, ids = prepared.approval
        if verb == 'unbound':
            return
        from tools.approval import resolve_gateway_approval
        count = [0]
        def approve():
            count[0] = sum(resolve_gateway_approval(route, 'deny' if verb == 'deny' else choice,
                request_id=request_id) for request_id in ids)
            return bool(count[0])
        consumed = await transfer_native_input(authority, receipt.ref, receipt.admission_id,
            prepared.generation, agent, approve, current)
        if consumed:
            async def acknowledge():
                from agent.i18n import t
                key = ('gateway.deny.denied_' + ('plural' if count[0] > 1 else 'singular') if verb == 'deny'
                       else f"gateway.approve.{choice}_{'plural' if count[0] > 1 else 'singular'}")
                text = await runner._deliver_approval_confirmation(event, t(key, count=count[0]), verb)
                adapter = runner._delivery_adapter_for(event.source)
                if adapter and text:
                    content, _ttl = adapter._unwrap_ephemeral(text)
                    if content:
                        await runner._send_busy_reply(event, adapter, content, plain_anchor=True)
            return acknowledge
        return
    text = runner._steer_text_with_origin((event.text or '').strip(), event)
    if mode == 'steer' and text and callable(getattr(agent, 'steer', None)):
        def steer():
            recipients = []
            accepted = runner._steer_running_agent(agent, text, ticket=receipt.admission_id, recipients=recipients)
            if accepted:
                runner._fold_into_running_turn(agent, route, event)
                def consumed():
                    consumed_any = False
                    for recipient in recipients:
                        withdraw = getattr(recipient, 'withdraw_steer', None)
                        consumed_any |= not callable(withdraw) or not withdraw(receipt.admission_id)
                    return consumed_any
                return consumed
            return False
        steered = await transfer_native_input(authority, receipt.ref, receipt.admission_id,
            prepared.generation, agent, steer, current)
        if not steered:
            mode = 'queue'
    elif (mode == 'interrupt' and getattr(agent, '_supports_active_turn_redirect', False) is True
          and callable(getattr(agent, 'redirect', None))):
        def redirect():
            accepted = bool(agent.redirect(text))
            if accepted:
                runner._reanchor_redirected_turn(agent, route, event)
            return accepted
        redirected = await transfer_native_input(authority, receipt.ref, receipt.admission_id,
            prepared.generation, agent, redirect, current)
    already_interrupted = False
    live = authority.sessions[prepared.session_id]
    if mode == 'interrupt' and not redirected:
        with live.event_stream.lock:
            if current() and _receiving_agent(authority, prepared.session_id, prepared.generation) is agent:
                agent.interrupt(event.text)
                already_interrupted = True
    adapter = runner._delivery_adapter_for(event.source)
    if adapter is not None:
        outcome = SimpleNamespace(effective_mode=mode, steered=steered, redirected=redirected,
            demoted_for_subagents=demoted_children, demoted_for_compression=demoted_compression)
        state = runner._peek_session_state(route)
        async def acknowledge():
            await runner._busy_interrupt_and_ack(event, route, adapter, state, agent,
                outcome, already_interrupted=already_interrupted or mode == 'interrupt')
        return acknowledge
