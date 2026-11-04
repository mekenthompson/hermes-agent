"""A completion folding into a queued result the running turn already consumed (process log/wait/poll).

The fold's write guard checks every identity in the batch, so a consumed waiting result refuses the
batch. The new result must then queue alone, checked on its own identity: it is neither refused and
retried on backoff nor overtaken by a later human message. A new result that is itself consumed,
and any result after a session Stop, are still refused.
"""
import pytest

from tests.gateway.test_completion_fold import _discord_session


def _event(complete, name):
    route = complete.runner.session_store._generate_session_key(
        complete.runner.adapters[next(iter(complete.runner.adapters))].build_source(
            chat_id='42', chat_type='dm', user_id='42'))
    return {'type': 'completion', 'session_id': name, 'started_at': 1.0, 'session_key': route,
            'parent_session_id': complete.owner, 'platform': 'discord', 'chat_type': 'dm', 'chat_id': '42',
            'user_id': '42', 'exit_code': 0, 'output': f'{name} output'}


async def _deliver(complete, name):
    return await complete.runner._inject_watch_notification(f'[IMPORTANT: {name} finished]', _event(complete, name))


def _texts(queued):
    return [row['payload']['text'] for row in queued()]


@pytest.mark.asyncio
async def test_a_result_behind_a_consumed_waiting_result_queues_alone(tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    authority, human, complete, queued = await _discord_session(tmp_path, monkeypatch)
    try:
        await complete('proc_a')
        process_registry._completion_consumed.add('proc_a')  # the running turn read proc_a's log
        assert await _deliver(complete, 'proc_b') is True
        texts = _texts(queued)
        assert texts[-1].startswith('[IMPORTANT: proc_b finished]')  # alone, not a fold
        assert 'proc_a' not in texts[-1]
        await human('later human message')
        assert _texts(queued)[-2:] == [texts[-1], 'later human message']  # nobody jumped ahead of it
    finally:
        process_registry._completion_consumed.discard('proc_a')
        authority.db.close()


@pytest.mark.asyncio
async def test_a_result_that_is_itself_consumed_is_still_refused(tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    authority, _human, complete, queued = await _discord_session(tmp_path, monkeypatch)
    try:
        await complete('proc_a')
        before = _texts(queued)
        process_registry._completion_consumed.update({'proc_a', 'proc_b'})
        assert await _deliver(complete, 'proc_b') is not True
        assert _texts(queued) == before
    finally:
        process_registry._completion_consumed.difference_update({'proc_a', 'proc_b'})
        authority.db.close()


@pytest.mark.asyncio
async def test_a_session_stop_still_refuses_a_result_behind_a_consumed_one(tmp_path, monkeypatch):
    from tools.process_registry import process_registry
    authority, _human, complete, queued = await _discord_session(tmp_path, monkeypatch)
    try:
        await complete('proc_a')
        before = _texts(queued)
        process_registry._completion_consumed.add('proc_a')
        authority.db._execute_write(lambda conn: conn.execute(
            'INSERT INTO state_meta(key, value) VALUES(?, ?)', ('gateway.process_stop.v1:' + complete.owner, '{}')))
        assert await _deliver(complete, 'proc_b') is not True
        assert _texts(queued) == before
    finally:
        process_registry._completion_consumed.discard('proc_a')
        authority.db.close()
