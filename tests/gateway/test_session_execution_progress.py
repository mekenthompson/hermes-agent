"""A read-only observer can see the current tool without opening another viewer."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit


@pytest.mark.asyncio
async def test_progress_tracks_real_generation_and_hides_waits_stops_and_completed_tools(tmp_path, monkeypatch):
    from gateway import session_finite
    from gateway.session_execution_progress import execution_progress
    from gateway.session_ancillary import clear_process_stop, record_process_stop

    db, authority = _authority(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(owner, ref, row):
        entered.set()
        await release.wait()
        return 'done'

    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        await _submit(authority, 'actual-tool')
        try:
            await asyncio.wait_for(entered.wait(), 5)
            generation = authority._handle(REF).execution_generation
            assert execution_progress(authority, REF) is None
            authority.publish_execution('s', generation, 'tool.start',
                                        {'tool_id': 'one', 'name': 'terminal', 'arguments': 'private command'})
            progress = execution_progress(authority, REF)
            assert set(progress) == {'execution_generation', 'tool', 'progress_at'}
            assert progress['tool'] == 'terminal' and progress['execution_generation'] == generation
            assert progress['progress_at'] > 0
            authority.publish_execution('s', generation + 1, 'tool.start',
                                        {'tool_id': 'late', 'name': 'foreign'})
            assert execution_progress(authority, REF)['tool'] == 'terminal'
            authority.publish_execution('s', generation, 'tool.start', {'tool_id': 'two', 'name': 'read_file'})
            assert execution_progress(authority, REF)['tool'] == 'read_file'
            authority.publish_execution('s', generation, 'tool.complete', {'tool_id': 'two'})
            assert execution_progress(authority, REF)['tool'] == 'terminal'

            event = SimpleNamespace(is_set=lambda: False)
            authority.sessions['s'].controls.register_clarify('s', generation,
                SimpleNamespace(clarify_id='pick', question='Pick one', choices=['Blue', 'Green'],
                                multi_select=False, event=event))
            assert execution_progress(authority, REF) is None
            event.is_set = lambda: True
            assert execution_progress(authority, REF)['tool'] == 'terminal'
            authority.pending_stops['s'] = generation
            assert execution_progress(authority, REF) is None
            authority.pending_stops.clear()
            stop_token = record_process_stop(authority, REF, generation)
            assert execution_progress(authority, REF) is None
            clear_process_stop(authority, REF, stop_token)
            authority.publish_execution('s', generation, 'tool.complete', {'tool_id': 'one'})
            assert execution_progress(authority, REF) is None
            authority.publish_execution('s', generation, 'tool.start', {'tool_id': 'left', 'name': 'terminal'})
        finally:
            release.set()
            await asyncio.wait_for(authority.sessions['s'].task, 5)
        assert execution_progress(authority, REF) is None


@pytest.mark.asyncio
async def test_execution_only_info_is_authorized_typed_and_avoids_inventory_or_attach(tmp_path, monkeypatch):
    from gateway import session_local, session_local_inventory
    from gateway.session_controls import AuthorityConnection
    from hermes_state_runtime import RuntimeStoreError
    from tui_gateway.contracts.registry import CANONICAL_METHODS

    db, authority = _authority(tmp_path, monkeypatch)
    connection = AuthorityConnection(authority, object(), {'user_id': 'human'})
    envelope = dict(source='kanban', lazy=True, profile_id='owned', desktop_protocol='hermes-gateway-v1',
                    profile_name='owned')
    monkeypatch.setattr(session_local, 'local_session_info', lambda *args: {
        **envelope, 'system_prompt': 'private context', 'credential_hint': 'private hint'})

    async def forbidden(*args, **kwargs):
        pytest.fail('a progress read must not attach or build inventory')

    monkeypatch.setattr(session_local_inventory, 'snapshot_info', forbidden)
    monkeypatch.setattr(authority, 'attach', forbidden)
    with db:
        result = await connection.info(REF, {'session_id': 's', 'execution_only': True})
        assert result == {**envelope, 'execution_progress': None}
        CANONICAL_METHODS['session.info'].result.model_validate(result)
        for value in (1, 'true', None):
            with pytest.raises(RuntimeStoreError, match='invalid_params'):
                await connection.info(REF, {'session_id': 's', 'execution_only': value})
        authority.sessions.pop('s')
        with pytest.raises(RuntimeStoreError, match='not_found'):
            await connection.info(REF, {'session_id': 's', 'execution_only': True})
        connection.actor = SimpleNamespace(profile_id='foreign', subject='human', capabilities={'session:read'})
        with pytest.raises(RuntimeStoreError):
            await connection.info(REF, {'session_id': 's', 'execution_only': True})
