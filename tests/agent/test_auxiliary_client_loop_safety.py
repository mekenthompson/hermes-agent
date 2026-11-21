"""Parallel async auxiliary calls on different event loops never close each other's client (HF-792).

``model_tools._run_async`` gives every worker thread its own loop, so two parallel ``vision_analyze``
calls hit ``_get_cached_client`` from different live loops. The loop-mismatch branch used to close the
entry's client — cancelling the other caller's in-flight request and leaving its same-provider retry
holding a closed object ("Claude client is closed"). A live foreign loop's entry is now only dropped.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux


class _FakeClient:
    base_url = "https://api.anthropic.com"
    api_key = "k"

    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class _LoopThread:
    """A live event loop running in its own thread, like one worker's ``_run_async`` loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        self.thread = threading.Thread(target=lambda: (started.set(), self.loop.run_forever()), daemon=True)
        self.thread.start()
        started.wait(10)

    def call(self, fn):
        async def run():
            return fn()
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(aux, "_client_cache", {})
    built = []

    def resolve(provider, model, async_mode, **kwargs):
        client = _FakeClient(f"built-{len(built)}")
        built.append(client)
        return client, "claude-haiku"

    monkeypatch.setattr(aux, "resolve_provider_client", resolve)
    monkeypatch.setattr(aux, "_compat_model", lambda client, model, default: model or default)
    return SimpleNamespace(built=built)


def _get():
    return aux._get_cached_client("anthropic", "claude-haiku", async_mode=True)[0]


def test_a_live_foreign_loops_client_is_not_closed_when_another_loop_replaces_the_entry(cache):
    loop_a, loop_b = _LoopThread(), _LoopThread()
    try:
        client_a = loop_a.call(_get)  # caller A has its request in flight on this client
        assert loop_a.call(_get) is client_a  # same loop: the cache serves it

        client_b = loop_b.call(_get)  # caller B arrives from another live loop

        assert client_b is not client_a
        assert client_a.closed is False, "B closed the client A's in-flight request is using"
        assert loop_b.call(_get) is client_b  # B's entry is now the cached one
        assert [e[0] for e in aux._client_cache.values()] == [client_b]
    finally:
        loop_a.stop()
        loop_b.stop()


def test_the_entry_of_a_closed_loop_is_still_retired(cache):
    loop_a, loop_b = _LoopThread(), _LoopThread()
    try:
        client_a = loop_a.call(_get)
    finally:
        loop_a.stop()  # the owner loop is closed: nothing can be in flight on its client
    try:
        client_b = loop_b.call(_get)
    finally:
        loop_b.stop()

    assert client_b is not client_a and client_a.closed is True


def _retry_client(monkeypatch, failed, error):
    fresh = _FakeClient("fresh")
    aux._client_cache[("k",)] = (failed, "m", None)
    req = SimpleNamespace(client=failed, resolved_provider="anthropic", resolved_model="claude-haiku")
    rebuilt = []
    monkeypatch.setattr(aux, "_prepare_same_provider_retry", lambda **kwargs: (rebuilt.append(kwargs), (fresh, {}))[1])
    failure = SimpleNamespace(error=RuntimeError(error))
    return aux._fresh_client_for_retry(req, {"task": "vision"}, failed, failure, async_mode=True), fresh, rebuilt


def test_a_retry_never_resends_on_a_client_the_failure_says_is_closed(monkeypatch, cache):
    failed = _FakeClient("failed")
    client, fresh, rebuilt = _retry_client(monkeypatch, failed, "Claude client is closed")

    assert client is fresh
    assert aux._client_cache == {}  # the dead client left the cache, and was not closed under other callers
    assert failed.closed is False
    assert rebuilt == [{"task": "vision", "resolved_provider": "anthropic", "resolved_model": "claude-haiku",
                        "async_mode": True}]


def test_a_retry_after_an_ordinary_transient_error_keeps_the_healthy_client(monkeypatch, cache):
    failed = _FakeClient("healthy")
    client, _, rebuilt = _retry_client(monkeypatch, failed, "connection reset by peer")

    assert client is failed and rebuilt == [] and len(aux._client_cache) == 1
