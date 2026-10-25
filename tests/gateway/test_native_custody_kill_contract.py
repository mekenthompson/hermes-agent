"""Native process consumers retain output and notification semantics after scope cleanup."""

import asyncio
from types import SimpleNamespace

import pytest

from tests.gateway.test_native_terminal_stop_authority import _alive, _cleanup, _runtime, _start, _tree
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
@pytest.mark.parametrize('notify', [False, True])
@pytest.mark.parametrize('consumer', ['rpc', 'owner_stop'])
async def test_native_custody_consumers_keep_output_and_consumption_contract(tmp_path, monkeypatch, notify, consumer):
    from gateway.session_ancillary import kill_process

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'kill-output')
    tree.command = "printf 'native-kill-output\\n'; " + tree.command
    try:
        ref, session = await _start(runtime, tree, notify=notify)
        await async_wait_until(lambda: 'native-kill-output' in session.output_buffer,
                               timeout=10, what='actual native process output')
        shell, child = int(tree.shell.read_text()), int(tree.child.read_text())
        assert _alive(shell) and _alive(child) and not session._custody.settled()
        if consumer == 'rpc':
            connection = SimpleNamespace(authority=runtime.authority, actor=runtime.actor)
            result = await kill_process(connection, ref, {'session_id': ref.session_id, 'process_id': session.id})
            assert result['status'] == 'killed' and 'native-kill-output' in result['output'], result
            assert result['completion_reason'] == 'killed' and result['termination_source'] == 'process.kill', result
            assert isinstance(session.exit_code, int) and session.exit_code == session._custody.main_code
            assert runtime.registry.is_completion_consumed(session.id)
            repeated = await kill_process(connection, ref, {'session_id': ref.session_id, 'process_id': session.id})
            assert repeated['status'] == 'already_exited' and 'native-kill-output' in repeated['output'], repeated
        else:
            await runtime.authority.interrupt(runtime.actor, ref,
                runtime.authority._handle(ref).execution_generation)
            assert not runtime.registry.is_completion_consumed(session.id), 'owner cleanup does not consume explicit output'
        assert not _alive(shell) and not _alive(child) and not tree.effect.exists()
        assert session.exited and session.custody_scope_empty and session._custody.settled()
        assert not session.reconciliation_required
        assert runtime.registry.completion_queue.qsize() == int(notify), 'one effective completion across duplicate cleanup'
    finally:
        await _cleanup(runtime, (tree,))


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_native_custody_failed_resource_stop_retains_unconsumed_output(tmp_path, monkeypatch):
    from gateway.session_ancillary import kill_process

    runtime = await _runtime(tmp_path, monkeypatch)
    tree = _tree(tmp_path, 'denied-output')
    tree.command = "printf 'unconfirmed-native-output\\n'; " + tree.command
    try:
        ref, session = await _start(runtime, tree, notify=True)
        await async_wait_until(lambda: 'unconfirmed-native-output' in session.output_buffer,
                               timeout=10, what='actual output before denied cleanup')
        shell, child = int(tree.shell.read_text()), int(tree.child.read_text())

        def denied():
            raise PermissionError('test verified custody stop denied')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session._custody, 'stop', denied)
            result = await kill_process(SimpleNamespace(authority=runtime.authority, actor=runtime.actor),
                ref, {'session_id': ref.session_id, 'process_id': session.id})
            assert result['status'] in {'unknown', 'error'}, result
            assert _alive(shell) and _alive(child) and not session.exited and session.reconciliation_required
            assert not runtime.registry.is_completion_consumed(session.id)
            assert runtime.registry.completion_queue.empty() and not tree.effect.exists()
            assert 'unconfirmed-native-output' in runtime.registry.get(session.id).output_buffer
    finally:
        await _cleanup(runtime, (tree,))


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_native_process_projection_hides_settled_foreground_setup_but_keeps_receipts(tmp_path, monkeypatch):
    from gateway.session_ancillary import process_snapshot
    from tools.environments.local import LocalEnvironment

    runtime = await _runtime(tmp_path, monkeypatch)
    original = runtime.runner._run_agent

    async def model(**kwargs):
        # A genuine foreground setup command runs inside the admitted native turn,
        # then the same turn launches its requested background work.
        environment = LocalEnvironment(cwd=str(tmp_path))
        result = await asyncio.to_thread(environment.execute, 'printf native-setup', cwd=str(tmp_path))
        assert result['returncode'] == 0 and 'native-setup' in result['output']
        return await original(**kwargs)

    monkeypatch.setattr(runtime.runner, '_run_agent', model)
    tree = _tree(tmp_path, 'visible-background')
    try:
        ref, background = await _start(runtime, tree, notify=False)
        settled = [session for session in runtime.registry._finished.values()
                   if session._custody is not None and session.custody_scope_empty]
        assert settled, 'the actual foreground effect must have a retained physical settlement receipt'
        projection = process_snapshot(runtime.authority, ref, None, [])
        assert {row['session_id'] for row in projection} == {background.id}, projection
        assert {row['session_id'] for row in runtime.registry.list_sessions()} == {background.id}
        for session in settled:
            assert runtime.registry.get(session.id) is session
            assert runtime.registry.poll(session.id)['status'] == 'exited'
    finally:
        await _cleanup(runtime, (tree,))
