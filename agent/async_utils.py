"""Async/sync bridging helpers.

``asyncio.run_coroutine_threadsafe`` can raise ``RuntimeError`` (loop closed during a
shutdown race); the coroutine is then never awaited or closed, which triggers a
"coroutine was never awaited" RuntimeWarning and leaks its frame. The helpers here
close the coroutine on scheduling failure. ``future.result()`` failures are deliberately
NOT handled: once the loop accepts the coroutine its lifecycle belongs to the loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import Future
from typing import Any, Coroutine, Optional


_DEFAULT_LOGGER = logging.getLogger(__name__)


def safe_schedule_threadsafe(
    coro: Coroutine[Any, Any, Any], loop: Optional[asyncio.AbstractEventLoop], *,
    logger: Optional[logging.Logger] = None,
    log_message: str = "Failed to schedule coroutine on loop", log_level: int = logging.DEBUG,
) -> Optional[Future]:
    """Schedule ``coro`` on ``loop`` from a sync context, leak-safe.

    Returns the Future on success, or ``None`` if the loop is missing or scheduling
    raised; in every failure path the coroutine is closed. Callers keep full control
    over the returned future (``.result(timeout=...)``, callbacks, fire-and-forget).
    """
    log = logger if logger is not None else _DEFAULT_LOGGER
    try:
        if loop is None:
            raise RuntimeError("loop is None")
        return asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:
        if asyncio.iscoroutine(coro):
            coro.close()
        log.log(log_level, "%s: %s", log_message, exc)
        return None


def consume_detached_task_result(task: asyncio.Future[Any]) -> None:
    """``add_done_callback`` for cancelled-and-detached tasks: observe the exception so the
    loop does not log "exception was never retrieved"; cancellation and terminal errors
    are swallowed because the task's owner already gave up on it."""
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _drain_scheduled_callbacks(loop: asyncio.AbstractEventLoop) -> None:
    """Run callbacks already queued on ``loop`` without waiting for its selector.

    A sync wait on the loop thread otherwise freezes ``call_soon`` / ``create_task``
    work (the approval prompt write, a same-loop answer) until the wait returns.
    Socket reads stay with the loop that owns the transport; this only drains the
    ready queue so a loop that is also the waiter can still settle scheduled work.
    """
    ready = getattr(loop, "_ready", None)
    if not ready:
        return
    pending = list(ready)
    ready.clear()
    for handle in pending:
        if getattr(handle, "_cancelled", False):
            continue
        run = getattr(handle, "_run", None)
        if callable(run):
            run()


def run_blocking_off_running_loop(fn):
    """Run ``fn`` off this thread when it already has a running event loop.

    ``asyncio.run`` cannot start on that thread. The worker gets a copy of the
    current context. While it runs, callbacks already scheduled on this loop are
    drained so the loop stays responsive to work queued before and during the call.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    if not loop.is_running():
        return fn()
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = fn()
        except BaseException:
            # The caller re-raises this. Log here so the thread boundary is not a
            # silent swallow if the caller is gone.
            _DEFAULT_LOGGER.exception("off-loop call failed")
            box["error"] = sys.exc_info()[1]

    from agent.memory_provider import spawn_context_thread
    thread = spawn_context_thread(worker, name="hermes-off-loop", daemon=True)
    thread.start()
    while thread.is_alive():
        _drain_scheduled_callbacks(loop)
        thread.join(0.01)
    thread.join()
    _drain_scheduled_callbacks(loop)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def wait_event_on_running_loop(event, timeout: float) -> bool:
    """Wait for ``event`` without freezing callbacks already queued on this loop.

    Off the loop this is ``event.wait``. On a running loop, blocking the thread
    leaves ``call_soon`` answers unrun until timeout, which is a silence the user
    did not choose.
    """
    import time

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return event.wait(timeout)
    if not loop.is_running():
        return event.wait(timeout)
    deadline = time.monotonic() + max(timeout, 0)
    while not event.is_set() and time.monotonic() < deadline:
        _drain_scheduled_callbacks(loop)
        if event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        event.wait(min(0.01, remaining))
    _drain_scheduled_callbacks(loop)
    return event.is_set()
