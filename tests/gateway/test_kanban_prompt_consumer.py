"""Only a trusted receiver and its actual native admission enable card questions."""
import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_question_consumer_requires_current_native_route_and_claim(tmp_path, monkeypatch):
    from tests.gateway.test_native_inbound_retry import NativeInboundRetryTests
    from gateway.session_contract import Principal
    from gateway.session_kanban import build_kanban_policy
    from gateway.session_kanban_prompts import bind_consumer, require_consumer
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_state_runtime import RuntimeStoreError

    fixture = NativeInboundRetryTests()
    await fixture.asyncSetUp()
    try:
        home = fixture.home
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
        monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(home))
        await fixture.start()
        kb.create_board('owned')
        with connect_closing(board='owned') as conn:
            tid = kb.create_task(conn, title='Question consumer', assignee='default',
                body='Untrusted text cannot grant an interactive receiver.',
                workspace_kind='dir', workspace_path=str(home))
            claim = kb.claim_task(conn, tid)
        actor = Principal('owner', str(home), frozenset({'session:create'}), 'test')
        connection = SimpleNamespace(native_owner=True, actor=actor, authority=fixture.authority)
        params = dict(board='owned', task_id=tid, run_id=claim.current_run_id, claim_lock=claim.claim_lock)
        policy, _secrets = build_kanban_policy(connection, params,
            {'model': {'provider': 'custom', 'default': 'test'}, 'platform_toolsets': {'cli': ['terminal']}})
        ordinary = await bind_consumer(fixture.authority, policy)
        assert 'prompt_consumer_admission_id' not in json.loads(ordinary.kanban_json)
        assert 'clarify' not in ordinary.toolsets

        fixture.adapter.kanban_prompt_consumer = lambda card: fixture.running['admission_id']
        bound = await bind_consumer(fixture.authority, policy)
        assert json.loads(bound.kanban_json)['prompt_consumer_admission_id'] == fixture.running['admission_id']
        assert 'clarify' in bound.toolsets
        # Assemble the real worker prompt from the actually admitted/frozen
        # consumer, not an env hint or a synthetic consumer admission ID.
        from agent.agent_init import _load_tools
        from agent.system_prompt import build_system_prompt
        from agent.prompt_builder import KANBAN_GUIDANCE
        from gateway.session_policy import policy_scope
        from tests.agent.test_system_prompt import _make_agent
        import model_tools

        with monkeypatch.context() as prompt_patch:
            prompt_patch.setenv('HERMES_KANBAN_TASK', tid)
            prompt_patch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
            prompt_patch.setattr('agent.prompt_builder.build_environment_hints', lambda: '')
            prompt_patch.setattr('hermes_cli.left_core_migration.recover_at_startup', list)
            prompt_patch.setattr(model_tools, 'get_tool_definitions', lambda **kwargs: [
                {'function': {'name': name}} for name in ('kanban_show', 'clarify')])

            def assembled(frozen, *, fallback=False):
                agent = _make_agent(quiet_mode=True, skip_context_files=True)
                with policy_scope(frozen):
                    _load_tools(agent, enabled_toolsets=list(frozen.toolsets), disabled_toolsets=None)
                    if fallback:
                        del agent._kanban_worker_guidance
                    prompt = build_system_prompt(agent)
                return agent, prompt

            for fallback in (False, True):
                _, headless = assembled(ordinary, fallback=fallback)
                assert 'Do not call `clarify`' in headless
                assert 'Block on genuine ambiguity' in headless
                frozen_agent, interactive = assembled(bound, fallback=fallback)
                assert 'trusted human conversation for `clarify`' in interactive
                assert 'Do not call `clarify`' not in interactive
                assert 'Block on genuine ambiguity' not in interactive
                assert 'there is no live user to answer' not in interactive
                from pathlib import Path
                from agent.turn_context import build_api_messages
                frozen_agent._current_turn_timestamp = 0
                frozen_agent.ephemeral_system_prompt = (Path(__file__).resolve().parents[2] /
                    'skills/devops/sdlc-review/SKILL.md').read_text()
                request, effective = build_api_messages(frozen_agent, [], current_turn_user_idx=None,
                    ext_prefetch_cache='', plugin_user_context='', moa_config=None, active_system_prompt=interactive)
                assert request[0]['content'] == effective
                assert 'Do not call `clarify`' not in effective
                assert 'Block on genuine ambiguity' not in effective
                assert 'ask the required human question with `clarify` and wait' in effective
                assert 'does not replace independent' in effective
                if not fallback:
                    # Scope changes never rewrite an existing agent's cached prefix.
                    with policy_scope(ordinary):
                        assert build_system_prompt(frozen_agent) == interactive

            with policy_scope(bound):
                from agent.prompt_builder import build_kanban_guidance
                assert build_kanban_guidance({'kanban_show'}) == KANBAN_GUIDANCE

        followup = await fixture.authority.admit_native(fixture.event('ordinary-status-followup', 'What is the status?'))
        assert followup.admission_id != fixture.running['admission_id']
        fixture.adapter.kanban_prompt_consumer = lambda card: card.get('consumer_admission_id', followup.admission_id)
        verified, _row, _adapter = await require_consumer(fixture.authority, bound)
        assert verified == fixture.running['admission_id']
        fixture.allowed.remove(fixture.source.user_id)
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await require_consumer(fixture.authority, bound)
        fixture.allowed.add(fixture.source.user_id)
        fixture.adapter.kanban_prompt_consumer = lambda card: 'another-admission'
        with pytest.raises(RuntimeStoreError, match='consumer_unavailable'):
            await require_consumer(fixture.authority, bound)
        def stop_during_provider_read(card):
            with connect_closing(board='owned') as conn:
                kb.block_task(conn, tid, reason='Operator Stop', expected_run_id=claim.current_run_id)
            return fixture.running['admission_id']
        fixture.adapter.kanban_prompt_consumer = stop_during_provider_read
        with pytest.raises(RuntimeStoreError, match='stale_kanban_claim'):
            await require_consumer(fixture.authority, bound)
    finally:
        fixture.doCleanups()
