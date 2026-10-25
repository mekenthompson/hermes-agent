"""Native recurring work binds its persisted instruction, never a plugin's notification text."""
import json

from hermes_state_runtime import RuntimeStoreError


def _state(authority, kind, session_id):
    from hermes_cli.heartbeat import HeartbeatState
    from hermes_cli.loops import LoopState
    from hermes_cli.goals import GoalState
    types = {'heartbeat': HeartbeatState, 'loop-tick': LoopState, 'goal-continuation': GoalState}
    prefixes = {'heartbeat': 'heartbeat:', 'loop-tick': 'loop:', 'goal-continuation': 'goal:'}
    if kind not in types:
        raise RuntimeStoreError('permission_denied')
    raw = authority.db.get_meta(prefixes[kind] + session_id)
    return types[kind].from_json(raw) if raw else None


def _instruction(kind, state):
    if kind != 'goal-continuation':
        return state.prompt
    # The contract and explicit subgoals remain part of the persisted user instruction.
    text = state.goal
    if state.has_contract():
        text += '\n' + state.contract.render_block()
    if state.subgoals:
        text += '\n' + state.render_subgoals_block()
    return text


def _configuration(kind, state):
    names = {
        'heartbeat': ('prompt', 'interval_seconds', 'created_at', 'origin'),
        'loop-tick': ('prompt', 'mode', 'interval_seconds', 'times', 'until', 'max_ticks', 'created_at', 'route'),
        'goal-continuation': ('goal', 'max_turns', 'created_at', 'subgoals', 'contract', 'gates'),
    }[kind]
    data = json.loads(state.to_json())
    if kind == 'goal-continuation':
        data['gates'] = [{key: gate[key] for key in ('command', 'timeout_seconds', 'max_retries')}
                         for gate in data['gates']]
    return {key: data[key] for key in names}


def snapshot_policy(authority, event, identity, native):
    """Called only from the built-in producer's private admission argument."""
    kind, session_id, expected = native
    state = _state(authority, kind, session_id)
    if (state is None or state.status != 'active' or json.loads(state.to_json()) != json.loads(expected)
            or not _instruction(kind, state).strip()):
        raise RuntimeStoreError('admission_conflict')
    if kind == 'heartbeat':
        expected_identity = json.dumps(['heartbeat', session_id, state.created_at, state.fire_count], separators=(',', ':'))
        if (identity != expected_identity or state.fire_count < 1 or state.last_fired_at <= 0
                or event.text != state.render_prompt()):
            raise RuntimeStoreError('admission_conflict')
    elif kind == 'loop-tick' and (not state.awaiting_response or state.ticks_fired < 1):
        raise RuntimeStoreError('admission_conflict')
    return {'kind': kind, 'session_id': session_id, 'profile': str(authority.profile_id),
            'configuration': _configuration(kind, state), 'instruction': _instruction(kind, state),
            'text': event.text}


def _descriptor(row):
    payload = row['payload']
    return payload.get('local_automation_v1') or payload.get('native_text_v1', {}).get('automation') or {}


def periodic_origin(authority, ref, row):
    """Validate the native snapshot against the live persisted job on the owning store."""
    from gateway.session_native_approval import native_claim_current, native_owner_current
    from hermes_state_runtime import get_session_admission
    descriptor = _descriptor(row)
    policy = descriptor.get('native_periodic_v1')
    if not isinstance(policy, dict):
        return None
    live = authority.sessions.get(ref.session_id)
    if (live is None or live.superseded or not native_owner_current(authority)
            or policy.get('profile') != str(authority.profile_id)
            or row['principal_id'] != 'automation:' + live.route
            or descriptor.get('owner') != ref.session_id or descriptor.get('identity') != row['request_id']
            or policy.get('text') != row['payload'].get('text')
            or authority.logical_owner(policy.get('session_id', '')) != ref.session_id
            or authority.pending_stops.get(ref.session_id) == row['generation']
            or authority.delivered_stops.get(ref.session_id) == row['generation']):
        return None
    committed = get_session_admission(authority.db, admission_id=row['admission_id'])
    if (committed is None or committed['payload'] != row['payload']
            or committed['owner_epoch'] != authority.epoch or committed['generation'] != row['generation']
            or committed['target_session_id'] != ref.session_id
            or committed['principal_id'] != row['principal_id']):
        return None
    if committed['status'] == 'started':
        # The common scope also covers the successful parent's pending terminal commit.
        if not native_claim_current(authority, live, row, parent_returned=True):
            return None
    elif committed['status'] != 'terminal' or committed['outcome'] != 'completed':
        return None
    state = _state(authority, policy['kind'], policy['session_id'])
    # A loop cap or successful goal judge ends scheduling, not its already-reserved children.
    statuses = ('active',) if policy['kind'] == 'heartbeat' else ('active', 'done')
    if (state is None or state.status not in statuses
            or _configuration(policy['kind'], state) != policy['configuration']
            or _instruction(policy['kind'], state) != policy['instruction']):
        return None
    return {'text': policy['instruction'], 'identity': row['principal_id'],
            'profile': str(authority.profile_id), 'session': live.route,
            'admission': row['admission_id'], 'generation': row['generation']}


def periodic_task_scope(authority, ref, row, live, event):
    origin = periodic_origin(authority, ref, row)
    if origin is None:
        return None
    from gateway.session_ingress import _native_task_scope
    return _native_task_scope(authority, ref, row, live, event, origin['text'],
                              producer_current=lambda: periodic_origin(authority, ref, row) is not None)
