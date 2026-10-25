"""Original native task claims narrow their admitted execution authority."""
import json


def original_claim_check(authority, ref):
    """Snapshot only the owner-validated task carrier; task prose grants nothing."""
    from gateway.session_policy import policy_for_source
    policy = policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
    if policy is None or policy.source != 'kanban':
        return None
    try:
        context = json.loads(policy.kanban_json)
        coordinates = {key: context[key] for key in ('task_id', 'db', 'run_id', 'claim_lock')}
    except (TypeError, ValueError, KeyError):
        return lambda: False

    def current():
        from agent.kanban_turn_recovery import worker_claim_is_live
        # The existing owner admission and worker registration already validate
        # this interpreter. The run PID belongs to its dispatcher submitter.
        return worker_claim_is_live(task_id=coordinates['task_id'], db_path=coordinates['db'],
            run_id=str(coordinates['run_id']), claim_lock=coordinates['claim_lock'], verify_pid=False)
    return current
