"""Trusted cron producer and owner execution of the existing scheduler contract.

Public callers name a persisted job, never supply agent policy or arbitrary jobs.
The ordinary admission ledger owns execution; the scheduler owns final delivery.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading

from gateway.session_contract import Principal, SessionRef
from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admission

_execution: ContextVar[tuple | None] = ContextVar('cron_owner_execution', default=None)
_owners = {}


def bind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    _owners[home] = (os.getpid(), authority, asyncio.get_running_loop())


def unbind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    bound = _owners.get(home)
    if bound is not None and bound[1] is authority:
        del _owners[home]


def owner_for_home(home):
    bound = _owners.get(Path(home).resolve())
    if bound is not None and bound[0] == os.getpid() and not bound[2].is_closed():
        return bound[1:]
    return None


def current_execution():
    return _execution.get()


def bind_execution(authority, session_id, job_id, execution_id):
    """Bind this context's cron admission ``(authority, session_id, job_id, execution_id)``, the
    layout ``current_execution()`` readers index; returns the ``ContextVar`` reset token."""
    return _execution.set((authority, session_id, job_id, execution_id))


def _actor(authority, actor):
    if actor is None:
        return Principal(CRON_PRINCIPAL_ID, authority.profile_id,
                         frozenset({'session:create', 'session:submit', 'session:read', 'session:control'}), 'cron-ticker')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not {'session:create', 'session:submit', 'session:control'} <= actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    # Cron fires belong to the profile scheduler, not the transport that observes
    # them. The same execution ID from ticker and CLI must dedupe together.
    return Principal(CRON_PRINCIPAL_ID, authority.profile_id, actor.capabilities, actor.transport_id)


def _create(authority, actor, params):
    from cron.jobs import get_job
    from gateway.session_local_recovery import local_identity, restore_local_session
    from gateway.session_policy import build_policy, bind_launch_key
    from gateway.run import _load_gateway_config
    from cron.scheduler import _load_cron_job_config
    from gateway.config import Platform
    from gateway.session import SessionSource, SessionEntry
    from gateway.session_lifecycle import _now

    if set(params) != {'job_id', 'request_id', 'extra_prompt'}:
        raise RuntimeStoreError('invalid_params')
    jid, request_id, extra = params['job_id'], params['request_id'], params['extra_prompt']
    if (not isinstance(jid, str) or not jid or not isinstance(request_id, str)
            or not request_id or len(request_id) > 256 or (extra is not None and not isinstance(extra, str))):
        raise RuntimeStoreError('invalid_params')
    execution_id, request_id = request_id, cron_request_id(jid, request_id)
    sid = local_identity(authority.profile_id, actor.subject, request_id)
    if authority.db.get_session(sid) is not None:
        return restore_local_session(authority, sid), request_id
    if ':' in execution_id:
        # Fresh admissions only: ``cron:<job>:<execution>`` then splits at its one ``:`` after the
        # job. Ledger ids are hex, so only a hand-made id has one; an identity admitted before this
        # guard keeps its exact retry above, and run history files it by its stored policy.
        raise RuntimeStoreError('invalid_params')
    job = get_job(jid)
    if job is None or job.get('no_agent'):
        raise RuntimeStoreError('not_found')
    config = _load_gateway_config()
    private = {}
    launch = {'source': 'cli', 'request_id': request_id, 'toolsets': []}
    if job.get('workdir'):
        launch['cwd'] = job['workdir']
    policy = build_policy(launch, config, private_secrets=private)
    policy = replace(policy, source='cron', platform='cron', model=_load_cron_job_config(job, jid, job.get('name') or jid).model,
                     request_json=json.dumps({'cron_job': job, 'extra_prompt': extra, 'request_id': request_id}))
    policy = bind_launch_key(authority, sid, policy, None, config_secrets=private)
    from gateway.session_local_recovery import local_source
    source = local_source(authority, sid, actor.subject)
    route = authority.runner.session_store._generate_session_key(source)
    now = _now()
    entry = SessionEntry(route, sid, now, now, origin=source, platform=Platform.LOCAL)
    # The staged creation receipt: ``operation('submit')`` commits it off the owner loop.
    return {'profile_id': authority.profile_id, 'principal_id': actor.subject, 'request_id': request_id,
            'session_id': sid, 'route': route, 'entry': entry.to_dict(), 'policy': asdict(policy)}, request_id


# What a message typed into a scheduled job's session settles with (see ``execute``); its text is
# ``gateway.errors.cron_session_read_only``, rendered in the owning profile's language.
CRON_SESSION_READ_ONLY_SURFACE = {'layer': 'gateway', 'code': 'cron_session_read_only', 'retryable': False}

# Run-side verdicts run_job stamps on the job dict for the firing scheduler's bookkeeping tail
# (cron/quota_hold.py parks past a closed usage window; cron/unreachable_retry.py re-runs a
# never-reached model). They cross the authority boundary inside the cron result.
RUN_JOB_FLAGS = ('_model_unreachable', '_quota_hold_seconds')


async def operation(authority, name, params, actor=None):
    actor = _actor(authority, actor)
    from gateway.session_authorities import require_serving
    require_serving(authority)  # released ownership: nothing here is ours to touch
    # Only new work is fenced by a drain. ``recover``/``status``/``cancel`` observe or stop work
    # already admitted, which the drain is waiting for: refusing them makes the firer give up
    # (CronExecutionUnknown pauses the job) on every restart that overlaps a cron run.
    if name == 'recover':
        from gateway.session_local_recovery import local_identity
        if (set(params) != {'job_id', 'request_id', 'extra_prompt'}
                or any(not isinstance(params[k], str) or not params[k] for k in ('job_id', 'request_id'))
                or (params['extra_prompt'] is not None and not isinstance(params['extra_prompt'], str))):
            raise RuntimeStoreError('invalid_params')
        request_id = cron_request_id(params['job_id'], params['request_id'])
        sid = local_identity(authority.profile_id, actor.subject, request_id)
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
        row = next((r for r in rows if r['request_id'] == request_id and r['principal_id'] == actor.subject), None)
        if row is None:
            return {'status': 'missing', 'result': None}
        state = await operation(authority, 'status', {'session_id': sid, 'admission_id': row['admission_id']}, actor)
        from gateway.session_local_recovery import restore_local_session
        restore_local_session(authority, sid)
        from gateway.config import Platform
        from gateway.session_local_recovery import local_adapter_map
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        frozen = json.loads(policy.request_json)
        if frozen['extra_prompt'] != params['extra_prompt']:
            raise RuntimeStoreError('admission_conflict')
        state['job'] = frozen['cron_job']
        # The receipt a firer whose submit answer was lost polls, exactly as if it had arrived.
        state.update(session_id=sid, admission_id=row['admission_id'])
        return state
    if name == 'submit':
        authority._require_admission_open()
        from gateway.session_authorities import owner_scope
        # The job id, jobs file and gateway config belong to the OWNING profile.
        with owner_scope(authority):
            ref, request_id = _create(authority, actor, params)
        from functools import partial
        from gateway.session_runtime_workers import tracked_write
        if isinstance(ref, dict):
            # The creation commit waits on the writer off the owner loop, like session.create (N50).
            from gateway.session_local_recovery import restore_local_session
            from hermes_state_local import commit_local_session
            await tracked_write(authority, partial(commit_local_session, authority.db, epoch=authority.epoch, receipt=ref))
            ref = restore_local_session(authority, ref['session_id'])
        receipt = await tracked_write(authority, partial(
            admit_session_input, authority.db, epoch=authority.epoch, principal_id=actor.subject,
            session_id=ref.session_id, request_id=request_id, payload={'text': params['extra_prompt'] or ''},
            _authorize_write=authority._admission_gate()), then=partial(authority._admitted, ref, None), ordered=True,
            after=partial(authority._schedule_admitted, ref))
        return {'session_id': ref.session_id, 'admission_id': receipt.admission_id}
    if set(params) != {'session_id', 'admission_id'} or name not in {'status', 'cancel'}:
        raise RuntimeStoreError('invalid_params')
    ref = SessionRef(authority.profile_id, params['session_id'])
    authority.authorize(actor, ref, 'session:submit')
    row = get_session_admission(authority.db, admission_id=params['admission_id'])
    if row is None or row['target_session_id'] != ref.session_id or row['principal_id'] != actor.subject:
        raise RuntimeStoreError('permission_denied')
    if name == 'cancel':
        return await _cancel_fire(authority, actor, ref, row)
    result, flags = None, {}
    if row['status'] == 'terminal':
        from gateway.session_results import admission_result
        saved = admission_result(authority.db, row['admission_id'])
        result = saved['result'].get('cron_result') if saved else None
        flags = (saved['result'].get('cron_job_flags') or {}) if saved else {}
        if result is None:
            result = [False, '', '', row.get('outcome') or 'unknown_execution']
    from cron.scheduler_liveness import owner_progress
    return {'status': row['status'], 'result': result, 'job_flags': flags,
            'progress_at': owner_progress.get(row['request_id'].rsplit(':', 1)[-1])}


async def _cancel_fire(authority, actor, ref, row):
    if row['status'] == 'queued':
        row = await authority.cancel_or_claimed(actor, ref, row['admission_id'])
        if row is None:
            return {'ok': True}  # the queued cancellation committed before any effects were admitted
    generation = row.get('generation')
    if generation is None and row['status'] == 'terminal' and row.get('outcome') == 'cancelled':
        return {'ok': True}
    from gateway.session_native_approval import native_owner_current
    if (not native_owner_current(authority) or row.get('owner_epoch') != authority.epoch
            or type(generation) is not int or generation < 1):
        raise RuntimeStoreError('unknown_execution', detail='Original cron effect ownership is unconfirmed; reconcile its process and effects before retrying.')
    if row['status'] == 'started':
        # The claim commits before execute() registers its event: latch before the first await.
        _cancellations(authority).setdefault(row['admission_id'], threading.Event()).set()
    # A completed immutable fire may still own native services. Canonical Stop
    # revokes their leases and joins owned process cleanup, or reports unknown.
    await authority.interrupt(actor, ref, generation)
    return {'ok': True}


def observed_unknown_processes(authority, ref, candidates):
    """Read-only attribution of cold cron descriptors; IDs never restore a Stop/execution lease."""
    live = authority.sessions.get(ref.session_id)
    if live is None:
        return []
    from gateway.config import Platform
    if live.source is None or live.source.platform != Platform.LOCAL:
        return []
    candidates = [p for p in candidates if p.detached and p.reconciliation_required and not p.exited
                  and p.native_origin_profile == str(authority.profile_id)
                  and p.native_origin_session == live.route]
    if not candidates:
        return []
    from gateway.session_policy import policy_for_source
    policy = policy_for_source(authority.runner, live.source)
    if policy is None or policy.source != 'cron' or policy.platform != 'cron':
        return []
    request_id, prompt = _observed_fire_identity(authority, ref, live, policy)
    observed = []
    for process in candidates:
        try:
            row = get_session_admission(authority.db, admission_id=process.native_origin_task_id)
            valid = (row is not None and row['target_session_id'] == ref.session_id and row['principal_id'] == CRON_PRINCIPAL_ID
                     and row['request_id'] == request_id and type(row['generation']) is int and row['generation'] > 0
                     and str(row['generation']) == process.native_origin_run_id and row['payload'] == {'text': prompt})
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeStoreError('unknown_execution', detail='Original cron process admission is unreadable; reconcile its process and effects.') from exc
        if not valid:
            raise RuntimeStoreError('unknown_execution', detail='Original cron process admission cannot be verified; reconcile its process and effects.')
        observed.append(process)
    return observed


def _observed_fire_identity(authority, ref, live, policy):
    from hermes_state_local import local_receipt
    from gateway.session_policy import restore_policy
    try:
        receipt = local_receipt(authority.db, ref.session_id)
        frozen = json.loads(policy.request_json)
        request_id, job_id, prompt = frozen['request_id'], frozen['cron_job']['id'], frozen['extra_prompt'] or ''
        if (receipt['principal_id'] != CRON_PRINCIPAL_ID or receipt['profile_id'] != authority.profile_id
                or receipt['route'] != live.route or restore_policy(receipt['policy']) != policy
                or request_id != receipt['request_id'] or not isinstance(request_id, str)
                or not isinstance(job_id, str) or not job_id
                or not request_id.startswith(cron_request_id(job_id, ''))):
            raise RuntimeStoreError('unknown_execution', detail='Original cron process identity cannot be verified; reconcile its process and effects.')
        return request_id, prompt
    except RuntimeStoreError as exc:
        if exc.reason != 'not_found':
            raise
        raise RuntimeStoreError('unknown_execution', detail='Original cron process identity is missing; reconcile its process and effects.') from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeStoreError('unknown_execution', detail='Original cron process identity is unreadable; reconcile its process and effects.') from exc


def _cancellations(authority):
    cancellations = getattr(authority, '_cron_cancellations', None)
    if cancellations is None:
        cancellations = authority._cron_cancellations = {}
    return cancellations


async def rpc(connection, name, params):
    return await operation(connection.authority, name, params, connection.actor)


def scheduler_execution_id(job_id, request_id):
    """The scheduler's execution id this admission carries: ``_create`` admits under
    ``cron:<job>:<request_id>`` and the ticker's ``request_id`` IS its ``cron/executions`` row id."""
    return request_id.removeprefix(cron_request_id(job_id, ''))


def _run_identified(run_job, job, execution_id, **kwargs):
    """``run_job`` inside this fire's cron identity (``ctx.current_cron_execution()``).

    The scheduler binds it in the FIRING thread, but the owner executes on its own loop, whose
    context never saw that binding; re-bind it here from the same ledger row, in the profile scope
    the caller entered (so the row and the profile name are this profile's)."""
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution
    token = enter_cron_execution(job, execution_id, get_execution(execution_id) or {})
    try:
        return run_job(job, execution_id=execution_id, **kwargs)
    finally:
        exit_cron_execution(token)


def _approval_task_scope(authority, ref, row, policy, data, cancel):
    """Only the scheduler's committed frozen fire may lend authority to native children."""
    from weakref import WeakSet
    from gateway.session_local_recovery import owned_receipt
    from gateway.session_native_approval import native_claim_current, native_owner_current, native_task_context
    from gateway.session_policy import restore_policy
    from tools.approval_context import approval_task_context

    # A drain inherits its submitter's ContextVars, but this separately admitted fire owns
    # its own lifetime. Only the canonical receipt and current claim below can bind it.
    unverified = approval_task_context('', identity='unverified', profile='', session='', is_current=lambda: False)
    if getattr(policy, 'source', None) != 'cron' or getattr(policy, 'platform', None) != 'cron':
        return unverified
    if (row.get('status') != 'started' or type(row.get('generation')) is not int
            or row.get('owner_epoch') != authority.epoch
            or authority.executing.get(ref.session_id) != row['generation']):
        return unverified
    live = authority.sessions[ref.session_id]
    receipt, source, route, _entry = owned_receipt(authority, ref.session_id)
    if (ref.profile_id != authority.profile_id or receipt['principal_id'] != CRON_PRINCIPAL_ID
            or row['principal_id'] != CRON_PRINCIPAL_ID or source.to_dict() != live.source.to_dict()
            or route != live.route or receipt['request_id'] != row['request_id']
            or restore_policy(receipt['policy']) != policy or policy.source != 'cron'
            or policy.platform != 'cron'):
        return unverified
    arrival_epoch = authority.route_epoch(route)
    fields = ('owner_epoch', 'generation', 'target_session_id', 'principal_id', 'request_id', 'payload')

    def current(parent_returned=False):
        if (cancel.is_set() or not native_owner_current(authority)
                or authority.sessions.get(ref.session_id) is not live or live.superseded
                or authority.route_epoch(route) != arrival_epoch):
            return False
        committed = get_session_admission(authority.db, admission_id=row['admission_id'])
        if (committed is None or committed['owner_epoch'] != authority.epoch
                or committed['target_session_id'] != ref.session_id
                or any(committed[key] != row[key] for key in fields)):
            return False
        if committed['status'] == 'started':
            return (native_claim_current(authority, live, row, parent_returned=parent_returned)
                    and authority.pending_stops.get(ref.session_id) != row['generation']
                    and authority.delivered_stops.get(ref.session_id) != row['generation'])
        entry = authority.runner.session_store.lookup_by_session_key(route)
        return (committed['status'] == 'terminal' and committed['outcome'] == 'completed'
                and entry is not None and not entry.suspended
                and authority.logical_owner(entry.session_id) == ref.session_id)

    def own(lease):
        with live.event_stream.lock:
            leases = getattr(live, 'approval_task_leases', None)
            if leases is None:
                leases = live.approval_task_leases = WeakSet()
            leases.add(lease)
            if not current():
                lease.revoke()

    text = '\n\n'.join(value for value in (data['cron_job'].get('prompt'), data['extra_prompt']) if value)
    return native_task_context(text, identity=CRON_PRINCIPAL_ID, profile=str(authority.profile_id),
        session=route, task_id=row['admission_id'], run_id=str(row['generation']), is_current=current, owner=own)


async def execute(authority, ref, row, policy):
    from cron.scheduler import run_job
    from gateway.run import _profile_runtime_scope
    data = json.loads(policy.request_json)
    if (row['payload'] != {'text': data['extra_prompt'] or ''}
            or row['request_id'] != data['request_id']
            or row['principal_id'] != authority.sessions[ref.session_id].source.user_id):
        # A cron session only runs its own frozen fire; anything else typed into it fails with a
        # descriptor the viewer can render (and offer a new chat for), never a generic failure.
        from agent.i18n import t
        text = t('gateway.errors.cron_session_read_only')
        authority.pending_results[row['admission_id']] = {'result': {
            'final_response': '', 'messages': [], 'failed': True, 'completed': False,
            'error': text, 'error_surface': dict(CRON_SESSION_READ_ONLY_SURFACE)},
            'usage': {}}
        return text
    cancellations = _cancellations(authority)
    cancel = cancellations.setdefault(row['admission_id'], threading.Event())
    execution_id = scheduler_execution_id(data['cron_job']['id'], row['request_id'])
    # The frozen snapshot predates the fire's ledger row; the run's progress stamps need its id.
    job = dict(data['cron_job'], execution_id=execution_id)
    token = bind_execution(authority, ref.session_id, job['id'], execution_id)
    try:
        db_path = await asyncio.to_thread(Path(authority.db.db_path).resolve)
        with _profile_runtime_scope(db_path.parent), _approval_task_scope(authority, ref, row, policy, data, cancel):
            result = await asyncio.to_thread(_run_identified, run_job, job, execution_id,
                                             extra_prompt=data['extra_prompt'], cancel_event=cancel)
            # run_job stamps run-side verdicts on ITS job copy (quota hold, unreachable model); the
            # firing scheduler's bookkeeping reads them off its own dict, so they ride the result.
            flags = {key: job[key] for key in RUN_JOB_FLAGS if key in job}
            authority.pending_results[row['admission_id']] = {
                'result': {'final_response': result[2], 'cron_result': list(result), 'cron_job_flags': flags,
                           'failed': not result[0], 'completed': result[0]}, 'usage': {}}
            if not result[0]:
                raise RuntimeError(result[3] or 'cron execution failed')
            return result[2]
    finally:
        from cron.scheduler_liveness import owner_progress
        owner_progress.pop(execution_id, None)
        _execution.reset(token)
        cancellations.pop(row['admission_id'], None)
