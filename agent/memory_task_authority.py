"""Carry existing native task reservations across the memory worker's queue."""
from tools.approval_context import (approval_task_revoked, background_approval_task,
                                    release_background_approval_task, reserve_background_approval_task)


def bind_memory_task(call):
    """Return a guarded call and idempotent settlement callback; no unscoped fallback."""
    if approval_task_revoked():
        return None, lambda *_: None
    reservation = reserve_background_approval_task()

    def release(_future=None):
        release_background_approval_task(reservation)

    def guarded():
        with background_approval_task(reservation, "memory-worker"):
            if not approval_task_revoked():
                return call()
        return None

    return guarded, release
