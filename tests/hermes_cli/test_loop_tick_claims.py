"""Native periodic claims cannot duplicate work or overwrite newer human controls."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest


def test_two_native_store_handles_claim_one_tick(tmp_path, monkeypatch):
    from hermes_cli import loops
    from hermes_state import SessionDB

    local = threading.local()
    with closing(SessionDB(tmp_path / 'state.db')) as first, closing(SessionDB(tmp_path / 'state.db')) as second:
        monkeypatch.setattr(loops, '_get_session_db', lambda: getattr(local, 'db', first))
        loops.LoopManager('periodic').set('/native-command', interval_seconds=60)
        barrier = threading.Barrier(2)

        def claim(database):
            local.db = database
            manager = loops.LoopManager('periodic')
            barrier.wait(timeout=10)
            return manager.fire_tick()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (first, second)))
        assert results.count('/native-command') == 1 and results.count(None) == 1
        state = loops.LoopManager('periodic').state
        assert state.awaiting_response and state.ticks_fired == 1


@pytest.mark.parametrize('action', ['pause', 'edit', 'clear'])
@pytest.mark.parametrize('closeout', ['complete', 'abandon'])
def test_old_native_tick_cannot_replace_current_control(tmp_path, monkeypatch, action, closeout):
    from hermes_cli import loops
    from hermes_state import SessionDB

    with closing(SessionDB(tmp_path / 'state.db')) as database:
        monkeypatch.setattr(loops, '_get_session_db', lambda: database)
        old = loops.LoopManager('periodic')
        old.set('/old-work', interval_seconds=60)
        assert old.fire_tick() == '/old-work'
        current = loops.LoopManager('periodic')
        if action == 'pause':
            current.pause()
        elif action == 'edit':
            current.set('/new-work', interval_seconds=120)
        else:
            current.clear()
        expected = database.get_meta('loop:periodic')
        if closeout == 'complete':
            assert old.complete_tick('LOOP_COMPLETE')['reason'] == 'tick ownership changed'
        else:
            old.abandon_tick()
        assert database.get_meta('loop:periodic') == expected
        assert json.loads(expected)['status'] == {'pause': 'paused', 'edit': 'active', 'clear': 'cleared'}[action]
