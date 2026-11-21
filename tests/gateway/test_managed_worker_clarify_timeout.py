"""A managed worker's clarify wait honours the profile's clarify timeout (<= 0 = unlimited)."""
import threading
import time

import pytest


class _Channel:
    def __init__(self):
        self.sent = []

    def send(self, kind, **payload):
        self.sent.append(kind)


def _controls():
    from agent import managed_worker as worker
    controls = worker.WorkerControls.__new__(worker.WorkerControls)  # no stdin reader thread
    controls.channel, controls.route, controls.agent = _Channel(), 'route', None
    controls.stopped, controls.lock, controls.clarifications = threading.Event(), threading.Lock(), {}
    return controls


def _config(tmp_path, monkeypatch, body):
    (tmp_path / 'config.yaml').write_text(body, encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))


def test_unanswered_clarify_times_out_at_the_configured_value(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, 'agent:\n  clarify_timeout: 1\n')
    controls, result = _controls(), []
    asker = threading.Thread(target=lambda: result.append(controls._ask('q?', [], False)), daemon=True)
    started = time.monotonic()
    asker.start()
    asker.join(10)
    assert not asker.is_alive(), 'the worker held the clarify past the configured 1 s timeout'
    assert result == [None] and time.monotonic() - started < 10
    assert controls.channel.sent == ['clarify', 'prompt_settled']


@pytest.mark.parametrize('body', ['agent:\n  clarify_timeout: 0\n', 'clarify:\n  timeout: -1\n'])
def test_nonpositive_clarify_timeout_waits_unbounded(tmp_path, monkeypatch, body):
    from agent import managed_worker as worker
    _config(tmp_path, monkeypatch, body)
    waits = []

    class _Event(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return True

    monkeypatch.setattr(worker.threading, 'Event', _Event)
    assert _controls()._ask('q?', [], False) is None
    assert waits == [None]


def test_private_cancellation_is_exact_and_never_an_answer():
    from agent import managed_worker as worker
    controls = _controls()
    state = {'event': threading.Event(), 'answer': None}
    controls.clarifications['current'] = state
    controls.prompt_control({'type': 'clarify_cancel', 'prompt_id': 'older'})
    assert not state['event'].is_set()
    with pytest.raises(ValueError, match='invalid_managed_control'):
        controls.prompt_control({'type': 'clarify_cancel', 'prompt_id': 'current', 'value': 'Blue'})
    assert not state['event'].is_set()
    controls.prompt_control({'type': 'clarify_cancel', 'prompt_id': 'current'})
    assert state['event'].is_set() and state['answer'] is worker._INTERRUPTED
