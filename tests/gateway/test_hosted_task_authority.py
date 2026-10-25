"""Hosted request names never substitute for the private room producer's authorization."""

import pytest


@pytest.mark.asyncio
async def test_public_hosted_request_prefix_cannot_borrow_task_authority(tmp_path, monkeypatch):
    from gateway.session_contract import Principal, Submission
    from hermes_state_runtime import RuntimeStoreError, get_session_admission
    from tests.gateway.test_native_terminal_background_authority import _runtime

    runner, authority, actor, ref, _registry = await _runtime(tmp_path, monkeypatch)
    viewer = Principal(actor.subject, actor.profile_id, actor.capabilities | {'session:submit'}, 'viewer')
    # Keep the real submit/claim validators but claim explicitly to observe its refusal.
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    called = []

    async def model(**kwargs):
        called.append(kwargs)
        raise AssertionError('An unverified hosted prefix reached execution')

    monkeypatch.setattr(runner, '_run_agent', model)
    receipt = await authority.submit(viewer, Submission('hosted:[{},1]', ref,
        {'text': 'A public request is not a private room task'}, 'queue'))
    try:
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await authority._claim_next(ref, authority.sessions[ref.session_id])
        row = get_session_admission(authority.db, admission_id=receipt.admission_id)
        assert row['status'] == 'queued' and '_hosted_authorized' not in row
        assert not called
    finally:
        authority.db.close()
