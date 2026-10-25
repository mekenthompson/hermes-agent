"""Live runtime ownership shared by native task authority producers."""
from contextlib import contextmanager


def native_owner_current(authority):
    """Historical completed admissions cannot authorize work after ownership changes."""
    from gateway.session_authorities import require_serving
    from hermes_state_runtime import RuntimeStoreError, _epoch

    try:
        require_serving(authority)
        with authority.db._read_ctx() as conn:
            _epoch(conn, authority.epoch)
    except RuntimeStoreError:
        return False
    return True


@contextmanager
def native_task_context(text, *, is_current, **kwargs):
    """Remember a clean producer return before the existing lease closes its parent."""
    from tools.approval_context import approval_task_context
    returned = False
    with approval_task_context(text, is_current=lambda: is_current(returned), **kwargs):
        yield
        returned = True


def native_claim_current(authority, live, row, *, parent_returned):
    """A reserved child spans the successful parent's off-loop terminal commit."""
    executing = authority.executing.get(row['target_session_id'])
    if executing is not None:
        return executing == row['generation']
    if not parent_returned:
        return False
    # _execute_claimed clears executing before settlement starts. The exact stream claim
    # and captured normal result still belong to this admission until its terminal write.
    with live.event_stream.lock:
        stamp = live.event_stream.execution
        expected = {'authority_epoch': authority.epoch, 'execution_generation': row['generation'],
                    'admission_id': row['admission_id']}
        result = (authority.pending_results.get(row['admission_id']) or {}).get('result')
        return (all(stamp.get(key) == value for key, value in expected.items())
                and isinstance(result, dict) and result.get('completed') is not False
                and not any(result.get(key) for key in ('failed', 'interrupted', 'error')))
