"""A managed active-work heartbeat turn is marked on its persisted prompt row; a user-authored one never is.

Desktop collapses a managed check whose reply is the silence marker. It reads the mark from the row's
``display_metadata`` instead of matching the prompt text, so the mark has to survive admission (the event
a turn runs is rebuilt from the admitted payload) and must be absent for a user's own ``/heartbeat``.
"""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.response_filters import (
    ACTIVE_WORK_HEARTBEAT_DISPLAY_KEY, persisted_user_metadata_for_event,
)
from gateway.session import SessionSource
from gateway.session_automation import heartbeat_origin_field, restore_heartbeat_origin
from hermes_cli.heartbeat import ACTIVE_WORK_HEARTBEAT_ORIGIN, USER_HEARTBEAT_ORIGIN, HeartbeatState, save_heartbeat
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import storage_handle

SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id='1', user_id='1')


def _tick_event(origin):
    event = MessageEvent(text='[Heartbeat]', source=SOURCE, internal=True)
    event._heartbeat_session_id = 's1'
    event._heartbeat_origin = origin
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize('origin, marked', [(ACTIVE_WORK_HEARTBEAT_ORIGIN, True), (USER_HEARTBEAT_ORIGIN, False)])
async def test_the_admitted_tick_marks_only_a_managed_check(tmp_path, monkeypatch, origin, marked):
    from gateway.run_heartbeat_acceptance import admit_heartbeat
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = storage_handle(tmp_path)
    seed_session(store, 's1', 'telegram')
    save_heartbeat('s1', HeartbeatState(prompt='check', interval_seconds=60, created_at=1, origin=origin,
                                        work_fingerprint='fp-2', woken_fingerprint='fp-1'))
    admitted = []
    authority = SimpleNamespace(persistence=store, logical_owner=lambda sid: sid)

    async def admit_automation(adapter, event, identity):
        admitted.append(event)
    authority.admit_automation = admit_automation
    await admit_heartbeat(SimpleNamespace(session_authority=authority), object(), SOURCE, 's1', 'route')

    assert len(admitted) == 1
    metadata = persisted_user_metadata_for_event(admitted[0])
    assert metadata.get(ACTIVE_WORK_HEARTBEAT_DISPLAY_KEY, False) is marked


def test_the_mark_survives_the_admission_payload_for_a_managed_check_only():
    managed, user = _tick_event(ACTIVE_WORK_HEARTBEAT_ORIGIN), _tick_event(USER_HEARTBEAT_ORIGIN)
    assert heartbeat_origin_field(user) == {}  # a user heartbeat's payload (and fingerprint) is unchanged
    payload = heartbeat_origin_field(managed)
    assert payload == {'heartbeat_origin': ACTIVE_WORK_HEARTBEAT_ORIGIN}

    rebuilt = _tick_event(None)
    restore_heartbeat_origin(rebuilt, payload)
    assert persisted_user_metadata_for_event(rebuilt)[ACTIVE_WORK_HEARTBEAT_DISPLAY_KEY] is True

    forged = _tick_event(None)
    restore_heartbeat_origin(forged, {'heartbeat_origin': 'anything-else'})  # only the one known origin is accepted
    assert ACTIVE_WORK_HEARTBEAT_DISPLAY_KEY not in persisted_user_metadata_for_event(forged)
