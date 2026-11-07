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


def run_blocking_off_running_loop(fn):
    """Run ``fn`` off this thread when it already has a running event loop.

    ``asyncio.run`` cannot start on that thread. The worker gets a copy of the
    caller's contextvars, including the profile home. Joining does not drain
    the caller's ready queue: a Desktop approval answer is delivered by another
    thread setting a ``threading.Event``, and socket reads belong to the loop
    that owns the transport.
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
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
