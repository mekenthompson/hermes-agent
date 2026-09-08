"""GatewayRunner integration coverage for private continuation admission."""
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.private_continuation import ContinuationDenied, PrivateContinuationExecution, PrivateContinuationStore
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource

BINDING = {"profile": "private", "workspace": "w", "issue": "i", "owner": "owner-session"}
FINITE_CONTROLS = {"max_iterations": 3, "wall_seconds": 45}


def test_scope_is_signed_expiring_revocable_and_exactly_bound(tmp_path):
    now = [100]
    store = PrivateContinuationStore(tmp_path / "continuations.db", secret=b"x" * 32, now=lambda: now[0])
    token = store.mint(binding=BINDING, owner_generation="g1", ttl_seconds=10)
    store.enqueue(token=token, binding=BINDING, owner_generation="g1", prompt="one")
    with pytest.raises(ContinuationDenied):
        store.enqueue(token=token + "x", binding=BINDING, owner_generation="g1", prompt="bad")
    store.revoke(token)
    with pytest.raises(ContinuationDenied):
        store.take(token=token, binding=BINDING, owner_generation="g1")


class _FakeTransport:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    gateway = GatewayRunner(GatewayConfig())
    # Real GatewayRunner and normal dispatch/handler; this is the provider boundary only.
    gateway._run_agent = AsyncMock(return_value={"final_response": "ok", "messages": []})
    gateway.configure_private_continuations(database=home / "continuations.db", secret=b"x" * 32)
    return gateway


def _install_owner(gateway, platform):
    source = SessionSource(platform=platform, chat_id=f"{platform.value}-owner", profile="private", chat_type="dm")
    key = gateway._session_key_for_source(source)
    gateway.session_store._entries[key] = SessionEntry(key, "owner-session", datetime.now(), datetime.now(), origin=source)
    gateway.session_store._loaded = True
    gateway.adapters[platform] = _FakeTransport()
    return key


async def _queued_handle(gateway, platform=Platform.SLACK, execution_policy=None):
    key = _install_owner(gateway, platform)
    token = gateway.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    await gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True)
    handle = await gateway.prepare_private_continuation(
        token=token, binding=BINDING, owner_generation="g", authorize=lambda: True,
        execution_policy=execution_policy,
    )
    assert handle.session_key == key
    return token, handle


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
async def test_prepare_launch_uses_real_runner_normal_handler_and_releases(runner, platform):
    _, handle = await _queued_handle(runner, platform)
    assert await runner.launch_private_continuation(handle, authorize=lambda: True) is True
    runner._run_agent.assert_awaited_once()
    receipt = await runner.get_execution_lifecycle(session_key=handle.session_key, execution_id=handle.execution_id)
    assert receipt["state"] == "completed"
    assert receipt["occupancy"] == "released"
    assert all(receipt[k] == "none" for k in ("tools", "children", "processes", "remote"))


@pytest.mark.asyncio
async def test_private_continuation_forwards_the_same_finite_controls(runner):
    """A continuation does not bypass the bounded execution policy capability."""
    controls = dict(FINITE_CONTROLS)
    _, handle = await _queued_handle(runner, Platform.SLACK, execution_policy=controls)
    controls["max_iterations"] = 999

    assert await runner.launch_private_continuation(handle, authorize=lambda: True) is True

    assert runner._run_agent.await_args.kwargs["internal_plugin_execution_policy"] == {
        "max_iterations": 3,
        "wall_seconds": 45.0,
    }


@pytest.mark.asyncio
async def test_forged_and_replayed_handles_never_enter_normal_handler(runner):
    _, handle = await _queued_handle(runner)
    forged = PrivateContinuationExecution(handle.execution_id, handle.session_key, handle.generation)
    with pytest.raises(ContinuationDenied):
        await runner.launch_private_continuation(forged, authorize=lambda: True)
    assert runner._run_agent.await_count == 0
    assert await runner.launch_private_continuation(handle, authorize=lambda: True)
    with pytest.raises(ContinuationDenied):
        await runner.launch_private_continuation(handle, authorize=lambda: True)
    assert runner._run_agent.await_count == 1


@pytest.mark.asyncio
async def test_busy_racing_admission_requeues_without_provider_entry(runner):
    key = _install_owner(runner, Platform.SLACK)
    runner._session_state(key).turn.agent = object()  # race after durable item exists, before reservation
    token = runner.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    await runner.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True)
    with pytest.raises(ContinuationDenied, match="busy"):
        await runner.prepare_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True)
    assert runner._run_agent.await_count == 0
    runner._session_state(key).turn.agent = None
    handle = await runner.prepare_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True)
    assert await runner.launch_private_continuation(handle, authorize=lambda: True)


@pytest.mark.asyncio
async def test_stop_prelaunch_refuses_provider_entry(runner):
    _, handle = await _queued_handle(runner, Platform.TELEGRAM)
    stopped = await runner.request_stop(session_key=handle.session_key, expected_execution_id=handle.execution_id)
    assert stopped["status"] == "accepted"
    assert await runner.launch_private_continuation(handle, authorize=lambda: True) is False
    assert runner._run_agent.await_count == 0


@pytest.mark.asyncio
async def test_stale_generation_refuses_provider_entry(runner):
    _, stale = await _queued_handle(runner, Platform.SLACK)
    runner._session_state(stale.session_key).persistent.run_generation += 2
    with pytest.raises(ContinuationDenied, match="stale"):
        await runner.launch_private_continuation(stale, authorize=lambda: True)
    assert runner._run_agent.await_count == 0


@pytest.mark.asyncio
async def test_observed_tool_lifetime_retains_durable_queue_fence(runner):
    _, handle = await _queued_handle(runner, Platform.SLACK)
    # Simulate a tool event at the actual provider/agent boundary; dispatch and handler remain real.
    runner._observe_internal_plugin_tool_event(handle.execution_id, "tool.started")
    assert await runner.launch_private_continuation(handle, authorize=lambda: True) is False
    receipt = await runner.get_execution_lifecycle(session_key=handle.session_key, execution_id=handle.execution_id)
    assert receipt["state"] == "completed"
    assert receipt["occupancy"] == "unknown"
    assert receipt["tools"] == "unknown"


def test_store_refuses_unsafe_database_and_keeps_new_database_private(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="database directory is unsafe"):
        PrivateContinuationStore(unsafe / "continuations.db", secret=b"x" * 32)
