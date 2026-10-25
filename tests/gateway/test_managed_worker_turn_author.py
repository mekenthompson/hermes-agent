"""A managed worker's turn carries its author so memory providers attribute it like the owner path."""
import json
import os
from types import SimpleNamespace

import pytest


def _policy(tmp_path):
    from gateway.session_local import _bypass_policy
    return _bypass_policy({'cwd': str(tmp_path), 'model': 'safe-fixture', 'provider': 'custom',
                           'base_url': 'http://127.0.0.1:9/v1', 'ignore_user_config': True}, private_secrets={})


@pytest.fixture
def owner_epoch(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch
    db = SessionDB(tmp_path / 'state.db')
    try:
        yield db, begin_runtime_epoch(db, instance_id='author-frame-owner')
    finally:
        db.close()


def test_bootstrap_frame_carries_the_admitted_author(tmp_path, monkeypatch, owner_epoch):
    from gateway import session_managed_worker as smw
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_local import LocalSessionAdapter
    from agent.managed_worker import validate_bootstrap

    monkeypatch.setattr('gateway.session_policy.launch_key', lambda authority, policy: None)
    source = SessionSource(platform=Platform.LOCAL, user_id='u', chat_id='c')
    live = SimpleNamespace(source=source, route='route')
    db, epoch = owner_epoch
    authority = SimpleNamespace(sessions={'sid': live}, profile_id=str(tmp_path), db=db, epoch=epoch)
    policy = _policy(tmp_path)
    adapter = LocalSessionAdapter(authority)
    adapter.register_source(source)
    adapter.policies[source.chat_id] = policy
    authority.runner = SimpleNamespace(_adapter_for_source=lambda candidate: adapter if candidate is source else None)
    ref = SimpleNamespace(session_id='sid')
    scope = {'profile_id': str(tmp_path), 'session_id': 'sid', 'execution_id': 'x', 'generation': 1,
             'pid': os.getpid(), 'birth': 0, 'secret': 's', 'epoch': 1}
    author = {'id': 'bot-7', 'name': 'Seven', 'is_bot': True}
    row = {'request_id': 'local-automation', 'admission_id': 'author-admission', 'generation': 1,
           'payload': {'text': 'hi', 'local_automation_v1': {'turn_author': author}}}
    frame = smw._bootstrap(authority, ref, row, policy, scope)
    assert frame['turn_author'] == author
    wire = json.loads(json.dumps(frame))
    assert validate_bootstrap(wire) is wire
    plain = smw._bootstrap(authority, ref, {**row, 'payload': {'text': 'hi', 'local_automation_v1': {}}}, policy, scope)
    assert plain['turn_author'] is None
    assert validate_bootstrap(json.loads(json.dumps(plain)))['turn_author'] is None
    for bad in ('bot-7', {}, {'is_bot': True}):
        with pytest.raises(ValueError):
            validate_bootstrap({**wire, 'turn_author': bad})


def test_worker_turn_passes_the_author_to_run_conversation():
    from gateway.session_kanban import run_worker_turns

    seen = []
    agent = SimpleNamespace(run_conversation=lambda text, **kw: seen.append((text, kw)) or {'final_response': 'ok'})
    author = {'id': 'alice', 'name': 'alice', 'is_bot': True}
    run_worker_turns(agent, {'policy': {'kanban_json': None}, 'text': 'hello', 'turn_author': author}, [])
    run_worker_turns(agent, {'policy': {'kanban_json': None}, 'text': 'again', 'turn_author': None}, [])
    assert seen == [('hello', {'conversation_history': [], 'turn_author': author}),
                    ('again', {'conversation_history': []})]
