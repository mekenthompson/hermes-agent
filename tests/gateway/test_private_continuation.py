import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.private_continuation import ContinuationDenied, GatewayPrivateContinuationMixin, PrivateContinuationStore
from gateway.session import SessionEntry, SessionSource

BINDING = {"profile": "private", "workspace": "w", "issue": "i", "owner": "owner-session"}


def test_scope_is_signed_expiring_revocable_and_exactly_bound(tmp_path):
    now = [100]
    store = PrivateContinuationStore(tmp_path / "continuations.db", secret=b"x" * 32, now=lambda: now[0])
    token = store.mint(binding=BINDING, owner_generation="g1", ttl_seconds=10)
    store.enqueue(token=token, binding=BINDING, owner_generation="g1", prompt="one")
    with pytest.raises(ContinuationDenied):
        store.enqueue(token=token + "x", binding=BINDING, owner_generation="g1", prompt="bad")
    with pytest.raises(ContinuationDenied):
        store.enqueue(token=token, binding={**BINDING, "issue": "other"}, owner_generation="g1", prompt="bad")
    store.revoke(token)
    with pytest.raises(ContinuationDenied):
        store.take(token=token, binding=BINDING, owner_generation="g1")
    fresh = store.mint(binding=BINDING, owner_generation="g1", ttl_seconds=1)
    now[0] = 102
    with pytest.raises(ContinuationDenied):
        store.enqueue(token=fresh, binding=BINDING, owner_generation="g1", prompt="expired")


def test_durable_owner_queue_is_bounded_fifo(tmp_path):
    store = PrivateContinuationStore(tmp_path / "continuations.db", secret=b"x" * 32, max_per_owner=2)
    token = store.mint(binding=BINDING, owner_generation="g", ttl_seconds=60)
    store.enqueue(token=token, binding=BINDING, owner_generation="g", prompt="first")
    store.enqueue(token=token, binding=BINDING, owner_generation="g", prompt="second")
    with pytest.raises(ContinuationDenied, match="queue is full"):
        store.enqueue(token=token, binding=BINDING, owner_generation="g", prompt="third")
    first = store.take(token=token, binding=BINDING, owner_generation="g")
    assert first and first[1] == "first"
    store.finish(first[0])
    second = store.take(token=token, binding=BINDING, owner_generation="g")
    assert second and second[1] == "second"


class _Gateway(GatewayPrivateContinuationMixin):
    def __init__(self, database, *, busy=False, origin_platform=Platform.LOCAL):
        self.configure_private_continuations(database=database, secret=b"x" * 32)
        origin = SessionSource(platform=origin_platform, chat_id="private-owner", profile="private")
        self.entry = SessionEntry("owner-key", "owner-session", datetime.now(), datetime.now(), origin=origin)
        self.session_store = SimpleNamespace(lookup_by_session_id=lambda owner: self.entry if owner == "owner-session" else None)
        self.events, self.busy = [], busy

    def _session_key_for_source(self, source):
        return "owner-key" if source.chat_id == "private-owner" else "other"

    def _is_session_running(self, key):
        return self.busy and key == "owner-key"

    async def dispatch_internal_plugin_event(self, event, *, execution_id=None):
        assert execution_id and execution_id.startswith("private-continuation-")
        self.events.append(event)


def test_drain_resolves_existing_private_owner_and_authorizes_every_prompt(tmp_path):
    gateway = _Gateway(tmp_path / "continuations.db")
    token = gateway.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True))
    assert asyncio.run(gateway.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    assert len(gateway.events) == 1
    assert gateway.events[0].source.platform is Platform.LOCAL
    assert gateway.events[0].source.chat_id == "private-owner"
    with pytest.raises(ContinuationDenied, match="not authorized"):
        asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="no", authorize=lambda: False))


def test_busy_owner_requeues_and_existing_telegram_owner_is_dispatched(tmp_path):
    gateway = _Gateway(tmp_path / "continuations.db", busy=True)
    token = gateway.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True))
    assert not asyncio.run(gateway.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    assert gateway.events == []
    gateway.busy = False
    assert asyncio.run(gateway.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    foreign = _Gateway(tmp_path / "foreign.db", origin_platform=Platform.TELEGRAM)
    token = foreign.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    asyncio.run(foreign.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True))
    assert asyncio.run(foreign.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    assert len(foreign.events) == 1
    assert foreign.events[0].source.platform is Platform.TELEGRAM


def test_dispatch_exception_is_durably_ambiguous_and_never_replayed(tmp_path):
    class BrokenGateway(_Gateway):
        async def dispatch_internal_plugin_event(self, event, *, execution_id=None):
            self.events.append(event)
            raise RuntimeError("provider outcome unknown")

    gateway = BrokenGateway(tmp_path / "continuations.db")
    token = gateway.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True))
    with pytest.raises(RuntimeError, match="unknown"):
        asyncio.run(gateway.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    assert not asyncio.run(gateway.drain_private_continuation(token=token, binding=BINDING, owner_generation="g", authorize=lambda: True))
    assert len(gateway.events) == 1
