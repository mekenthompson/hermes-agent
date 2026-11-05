"""``sessions.unviewed_local_completions: turn``: an unviewed local completion runs its turn.

The gateway owns a local session whether or not a viewer is attached. With the default (``note``) an
unviewed background-process or delegation completion is written into history without a model call
(#134889 LC-7, main's CLI parity). With ``turn`` it runs the same turn it would run with a viewer
attached, so work the session started continues after the Desktop closes. A local cron result stays a
note either way.
"""
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_end_delegations import (
    _DELEGATION, _PROCESS, _admissions, _deliver, _local, _notes, _record_turns,
)
from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def _no_delegations():
    delegation._reset_for_tests()
    yield
    delegation._reset_for_tests()


def _policy(monkeypatch, value):
    monkeypatch.setattr('gateway.session_completion_notes.unviewed_completions_run_turns',
                        lambda authority: value)


@pytest.mark.asyncio
@pytest.mark.parametrize('producer', [_PROCESS, _DELEGATION], ids=['process', 'delegation'])
async def test_unviewed_completion_runs_a_turn_when_configured(tmp_path, monkeypatch, producer):
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        _policy(monkeypatch, True)
        target = await _deliver(runner, authority, ref, producer)
        assert not authority.sessions[ref.session_id].subscribers
        seen = _record_turns(runner)
        await authority._drain(ref)
        assert seen == [f'[finished] RESULT_{producer[1]}']
        assert _notes(authority, target) == []
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['completed']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_unviewed_completion_stays_a_note_by_default(tmp_path, monkeypatch):
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        _policy(monkeypatch, False)
        target = await _deliver(runner, authority, ref, _PROCESS)
        seen = _record_turns(runner)
        await authority._drain(ref)
        assert seen == []
        assert len(_notes(authority, target)) == 1
    finally:
        authority.db.close()


@pytest.mark.parametrize('body, expected', [
    ('sessions:\n  unviewed_local_completions: turn\n', True),
    ('sessions:\n  unviewed_local_completions: note\n', False),
    ('sessions:\n  auto_prune: true\n', False),
    ('sessions: turn\n', False),
    (None, False),
])
def test_policy_reads_the_owning_profile_config(tmp_path, body, expected):
    from gateway.session_completion_notes import unviewed_completions_run_turns
    if body is not None:
        (tmp_path / 'config.yaml').write_text(body)
    assert unviewed_completions_run_turns(SimpleNamespace(profile_id=str(tmp_path))) is expected
