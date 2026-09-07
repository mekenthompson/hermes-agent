import asyncio

import pytest

from gateway.private_continuation import (
    ContinuationDenied, GatewayPrivateContinuationMixin, PrivateContinuationStore,
)


BINDING = {"profile": "private", "workspace": "w", "issue": "i", "owner": "session"}


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
        store.enqueue(token=token, binding=BINDING, owner_generation="g1", prompt="revoked")
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
    first = store.take(owner_key="session")
    assert first and first[1] == "first"
    store.finish(first[0])
    second = store.take(owner_key="session")
    assert second and second[1] == "second"


class _Gateway(GatewayPrivateContinuationMixin):
    def __init__(self, database):
        self.configure_private_continuations(database=database, secret=b"x" * 32)
        self.events = []

    async def dispatch_internal_plugin_event(self, event):
        self.events.append(event)


def test_authorizes_every_prompt_and_uses_canonical_dispatch(tmp_path):
    gateway = _Gateway(tmp_path / "continuations.db")
    token = gateway.mint_private_continuation(binding=BINDING, owner_generation="g", ttl_seconds=60)
    asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="next", authorize=lambda: True))
    assert asyncio.run(gateway.drain_private_continuation(owner_key="session", make_event=lambda prompt: {"text": prompt}))
    assert gateway.events == [{"text": "next"}]
    with pytest.raises(ContinuationDenied, match="not authorized"):
        asyncio.run(gateway.enqueue_private_continuation(token=token, binding=BINDING, owner_generation="g", prompt="no", authorize=lambda: False))
