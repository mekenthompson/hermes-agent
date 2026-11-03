"""Process-completion fan-in for GatewayRunner: the short-window batch a completion joins.

Split out of ``gateway/run_notifications.py``; bound onto ``GatewayRunner`` through
``GatewayNotificationsMixin``. Each completion is policy-filtered in its own profile scope and keyed
by route plus its batch origin before it can join (or suppress) a batch; concurrent preflights on one
route hold that route's flush until every one of them has decided.
"""

from __future__ import annotations

import asyncio
from typing import Optional


class GatewayCompletionBatchMixin:
    """Admission of policy-checked process completions into their route's coalescing batch."""

    async def _enqueue_process_completion_notification(self, synth_text: str, evt: dict) -> Optional[bool]:
        """Fan in policy-admitted process completions with compatible pinned owners."""
        # Lazy defaults: lifecycle tests build GatewayRunner via object.__new__.
        for attr, default in (
            ("_completion_notification_batches", dict), ("_completion_notification_batch_tasks", dict),
            ("_completion_notification_batch_flush_tasks", set),
            ("_completion_batch_preflights", dict), ("_completion_batch_preflight_ready", dict),
            ("_completion_notification_batch_window", lambda: 0.1),
            ("_completion_notification_batches_stopping", lambda: False), ("_background_tasks", set),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, default())
        if self._completion_notification_batches_stopping:
            return False
        # A route key alone is not ownership: /new can leave old and new pins
        # in the same queue. Never settle one pin on another pin's acceptance.
        route_key = self._event_route_key(
            evt, (*self._COMPLETION_BATCH_KEY_FIELDS, "parent_session_id", "origin_session_id"))
        self._completion_batch_preflights[route_key] = self._completion_batch_preflights.get(route_key, 0) + 1
        try:
            # Filter each event before it can contaminate (or suppress) a foreground batch.
            # The shared queue may be drained outside the owning profile's runtime scope.
            async with self._completion_event_scope(evt):
                from tools.process_registry_notifications import should_surface_notification
                if not should_surface_notification(evt):
                    return None
                from gateway.session_approval_completion import completion_batch_origin_for_event
                origin = completion_batch_origin_for_event(evt, str(evt.get('session_key') or ''))
            if self._completion_notification_batches_stopping:
                return False
            key = (*route_key, origin)
            future = asyncio.get_running_loop().create_future()
            self._completion_notification_batches.setdefault(key, []).append((synth_text, evt, future))
            if key not in self._completion_notification_batch_tasks:
                task = asyncio.create_task(self._flush_process_completion_batch(key))
                self._completion_notification_batch_tasks[key] = task
                # Keep the flush alive under the gateway's normal lifecycle accounting.
                self._retain_background_task(task)
                self._track_task_in(self._completion_notification_batch_flush_tasks, task)
        finally:
            remaining = self._completion_batch_preflights[route_key] - 1
            if remaining:
                self._completion_batch_preflights[route_key] = remaining
            else:
                self._completion_batch_preflights.pop(route_key)
                if ready := self._completion_batch_preflight_ready.pop(route_key, None):
                    ready.set()
        return await future
