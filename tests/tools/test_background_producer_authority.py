"""Reject absent native authority at the delegation producer before setup or fallback."""
import json
from tests.tools.test_delegate_apiserver_background import _fake_parent, _patch_delegate
from gateway.session_context import set_session_vars


def test_missing_authority_cannot_execute_background_via_no_async_fallback(tmp_path, monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    set_session_vars(platform='api_server', chat_id='', session_key='', session_id='', async_delivery=False)
    effect = tmp_path / 'unauthorized-child-effect.txt'
    def effectful_child(*args, **kwargs):
        effect.write_text('child executed without native task authority')
        return {'task_index': 0, 'status': 'completed', 'summary': 'effect produced'}
    monkeypatch.setattr(dt, '_run_single_child', effectful_child)
    result = json.loads(dt.delegate_task(goal='must require authority', background=True, parent_agent=_fake_parent()))
    assert not effect.exists(), result
    assert result.get('code') == 'missing_task_authority', result


def test_missing_authority_refuses_before_transcripts_or_child_build(tmp_path, monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    set_session_vars(platform='telegram', chat_id='chat', session_key='route', session_id='parent', async_delivery=True)
    built = []
    original = dt._build_child_agent
    def build(**kwargs):
        built.append(True)
        return original(**kwargs)
    monkeypatch.setattr(dt, '_build_child_agent', build)
    result = json.loads(dt.delegate_task(goal='must require authority', background=True, parent_agent=_fake_parent()))
    assert result.get('code') == 'missing_task_authority', result
    artifacts = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob('task-*.log')]
    assert not built and not artifacts, {'built': len(built), 'transcripts': artifacts, 'result': result}
