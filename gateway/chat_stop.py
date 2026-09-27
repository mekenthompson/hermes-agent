"""Generation-fenced cancellation of ordinary gateway chat turns for profile services."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from pathlib import Path

from hermes_constants import get_hermes_home, get_process_hermes_home, hermes_home_key

logger = logging.getLogger("gateway.run")


class GatewayChatStopMixin:
    """Profile-local API; the caller supplies a token captured in the originating tool turn."""

    def _chat_stop_observations(self) -> OrderedDict:
        return self.__dict__.setdefault("_chat_run_stop_observations", OrderedDict())

    def _track_chat_stop_worker(self, session_key: str, run_generation: int, worker_done) -> None:
        """A Stop during preparation may precede physical worker registration."""
        key = (hermes_home_key(), session_key, run_generation)
        observation = self._chat_stop_observations().get(key)
        if observation is not None:
            observation["worker_done"] = worker_done

    @staticmethod
    def _chat_worker_completion(worker_done) -> str:
        if worker_done is None:
            return "unknown"
        return "completed" if worker_done.is_set() else "pending"

    def _chat_stop_profile_matches(self, session_key: str, profile_home: str | Path) -> bool:
        state = self._peek_session_state(session_key)
        source = getattr(getattr(state.turn, "event", None), "source", None) if state else None
        if source is None:
            return False
        try:
            from gateway.session_identity import identity_of
            from hermes_cli.profiles import get_profile_dir, profile_exists
            identity = identity_of(source)
            if identity is not None:
                owner_home = identity.runtime_home
            elif source.profile:
                if source.profile == (getattr(self, "_primary_profile_name", None) or "default"):
                    owner_home = get_process_hermes_home()
                elif profile_exists(source.profile):
                    owner_home = get_profile_dir(source.profile)
                else:
                    return False
            elif not getattr(getattr(self, "config", None), "multiplex_profiles", False):
                owner_home = get_process_hermes_home()
            else:
                return False
            return (self._session_key_for_source(source) == session_key
                    and hermes_home_key(owner_home) == hermes_home_key(Path(profile_home)))
        except (TypeError, ValueError):
            return False

    async def request_chat_run_stop(
        self, *, session_key: str, expected_run_generation: int, profile_home: str | Path,
        reason: str = "Plugin requested chat stop",
    ) -> dict:
        """Request /stop cleanup for exactly one claimed chat run.

        Call on the gateway event loop. ``accepted`` is a routing/interrupt acknowledgement,
        never proof that the physical worker, tools, child processes or remote effects ended.
        The generation is obtained from ``ToolInvocationContext.run_generation`` when the
        plugin's chat tool records ownership; it is not read at webhook time.
        """
        if isinstance(expected_run_generation, bool) or not isinstance(expected_run_generation, int) or expected_run_generation < 1:
            raise ValueError("expected_run_generation must be a positive integer")
        receipt = {"session_key": session_key, "run_generation": expected_run_generation,
                   "worker_completion": "unknown", "effects": "unknown"}
        loop = getattr(self, "_gateway_loop", None)
        if loop is not None and loop is not asyncio.get_running_loop():
            return {"status": "unsupported", **receipt}
        if hermes_home_key(get_hermes_home()) != hermes_home_key(Path(profile_home)):
            return {"status": "stale", **receipt}
        state = self._peek_session_state(session_key)
        if state is None:
            return {"status": "not_running", **receipt}
        if not self._chat_stop_profile_matches(session_key, profile_home):
            return {"status": "stale", **receipt}
        if state.persistent.run_generation != expected_run_generation:
            return {"status": "stale", **receipt}
        if state.turn.agent is None:
            return {"status": "not_running", **receipt}

        worker_done = state.turn.worker_done
        observations = self._chat_stop_observations()
        key = (hermes_home_key(Path(profile_home)), session_key, expected_run_generation)
        observation = {"worker_done": worker_done, "stop_status": "unsupported"}
        observations[key] = observation
        observations.move_to_end(key)
        while len(observations) > 256:
            observations.popitem(last=False)
        delivery = {}
        try:
            # No await separates the fence above from the shared path's generation bump.
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(Path(profile_home)):
                await self._interrupt_and_clear_session(
                    session_key, state.turn.event.source, interrupt_reason=reason,
                    invalidation_reason="plugin_chat_stop", delivery_report=delivery,
                )
        except Exception:
            logger.warning("Chat run stop cleanup failed for %s", session_key, exc_info=True)
            # The shared path may already have invalidated this run. Never say accepted
            # when an exception prevented the full queue/slot cleanup.
            observation.update(delivery)
            return {**receipt, "status": "unsupported", "interrupt_delivery": delivery.get("interrupt", "unknown"),
                    "process_reap": delivery.get("reap", "unknown"),
                    "activity_clear": delivery.get("activity_clear", "unknown"),
                    "worker_completion": self._chat_worker_completion(worker_done)}
        status = "accepted" if delivery.get("interrupt") in {"prelaunch", "delivered"} else "unsupported"
        observation.update(delivery)
        observation["stop_status"] = status
        return {"status": status, "interrupt_delivery": delivery.get("interrupt", "unknown"),
                "process_reap": delivery.get("reap", "none"),
                "activity_clear": delivery.get("activity_clear", "unknown"),
                **receipt, "worker_completion": self._chat_worker_completion(worker_done)}

    async def get_chat_run_stop_observation(
        self, *, session_key: str, run_generation: int, profile_home: str | Path,
    ) -> dict:
        """Observe the physical worker Event retained by a prior stop request, if known."""
        if isinstance(run_generation, bool) or not isinstance(run_generation, int) or run_generation < 1:
            raise ValueError("run_generation must be a positive integer")
        if hermes_home_key(get_hermes_home()) != hermes_home_key(Path(profile_home)):
            return {"status": "stale", "worker_completion": "unknown", "effects": "unknown"}
        key = (hermes_home_key(Path(profile_home)), session_key, run_generation)
        observations = self._chat_stop_observations()
        if key not in observations:
            return {"status": "unknown", "worker_completion": "unknown", "effects": "unknown"}
        observation = observations[key]
        return {"status": "observed", "stop_status": observation["stop_status"],
                "interrupt_delivery": observation.get("interrupt", "unknown"),
                "process_reap": observation.get("reap", "unknown"),
                "worker_completion": self._chat_worker_completion(observation["worker_done"]),
                "effects": "unknown", "session_key": session_key, "run_generation": run_generation}
