"""Truthful lifecycle receipts for internal plugin executions.

`accepted` means only that the bound in-process agent accepted an interrupt.
It is deliberately not a claim about executor threads, tools, children, processes,
or remote work; those stay occupied/unknown until the normal turn cleanup observes them.
"""
from __future__ import annotations

from collections import OrderedDict
import re
from typing import Any, Optional

_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LIFECYCLE_VERSION = "execution-lifecycle/v1"


class GatewayExecutionLifecycleMixin:
    @staticmethod
    def _validate_internal_plugin_execution_id(execution_id: str) -> str:
        if not isinstance(execution_id, str) or not _EXECUTION_ID_RE.fullmatch(execution_id):
            raise ValueError("execution_id must be a canonical opaque identifier")
        return execution_id

    def _internal_plugin_execution_records(self) -> dict:
        return self.__dict__.setdefault("_internal_plugin_execution_registry", {})

    def _register_internal_plugin_execution(self, event, session_key: str) -> None:
        execution_id = self._validate_internal_plugin_execution_id(getattr(event, "_internal_plugin_execution_id", ""))
        records = self._internal_plugin_execution_records()
        if execution_id in records or any(r["session_key"] == session_key for r in records.values()):
            raise ValueError("internal plugin session already has a live execution")
        state = self._peek_session_state(session_key)
        records[execution_id] = {"session_key": session_key, "source": event.source,
            "generation": int(state.persistent.run_generation) + 1 if state else 1,
            "agent": None, "accepted": False}

    def _bind_internal_plugin_execution(self, execution_id: Optional[str], *, session_key: str,
                                        run_generation: int, agent: Any) -> bool:
        if not isinstance(execution_id, str):
            execution_id = getattr(execution_id, "_internal_plugin_execution_id", None)
        record = self._internal_plugin_execution_records().get(execution_id)
        state = self._peek_session_state(session_key)
        if not record or record["session_key"] != session_key or record["generation"] != run_generation or not state or state.persistent.run_generation != run_generation or state.turn.agent is not agent:
            return False
        record["agent"] = agent
        return True

    def _promote_running_agent(self, *, session_key: str, run_generation: int, agent: Any,
                               internal_plugin_execution_id: Optional[str] = None) -> bool:
        """Atomically bind only the current generation; used at the promotion fence."""
        state = self._session_state(session_key)
        if state.persistent.run_generation != run_generation:
            return False
        state.turn.agent = agent
        if internal_plugin_execution_id is None:
            return True
        return self._bind_internal_plugin_execution(internal_plugin_execution_id,
                                                     session_key=session_key,
                                                     run_generation=run_generation, agent=agent)

    def _retire_internal_plugin_execution(self, execution_id: Optional[str]) -> None:
        record = self._internal_plugin_execution_records().pop(execution_id, None)
        if record is not None:
            retired = self.__dict__.setdefault("_internal_plugin_retired_executions", OrderedDict())
            retired[execution_id] = None
            while len(retired) > 256:
                retired.popitem(last=False)

    async def dispatch_internal_plugin_event(self, event, *, execution_id: Optional[str] = None):
        if execution_id is None:
            return await super().dispatch_internal_plugin_event(event)
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        source = self._validate_internal_plugin_event(event)
        event._internal_plugin_execution_id = execution_id
        self._register_internal_plugin_execution(event, self._session_key_for_source(source))
        completed = False
        try:
            result = await super().dispatch_internal_plugin_event(event)
            completed = True
            return result
        finally:
            # Cancellation of the wrapper is not evidence the to_thread worker ended.
            if completed:
                self._retire_internal_plugin_execution(execution_id)

    async def request_stop(self, *, session_key: str, expected_execution_id: str,
                           reason: str = "Internal plugin stop requested") -> dict:
        from gateway.run import _AGENT_PENDING_SENTINEL, request_hard_interrupt
        execution_id = self._validate_internal_plugin_execution_id(expected_execution_id)
        # Preserve PR23's exact delivery ABI. Versioning belongs to the new
        # observation query so older callbacks do not mistake it for completion.
        receipt = {"session_key": session_key, "execution_id": execution_id}
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            status = "stale" if execution_id in self.__dict__.get("_internal_plugin_retired_executions", {}) else "not_running"
            return {"status": status, **receipt}
        state = self._peek_session_state(session_key)
        if record["session_key"] != session_key or not state or state.persistent.run_generation != record["generation"]:
            return {"status": "stale", **receipt}
        agent = record["agent"]
        if agent is None or agent is _AGENT_PENDING_SENTINEL or state.turn.agent is not agent:
            return {"status": "not_running", **receipt}
        if not request_hard_interrupt(agent, reason):
            return {"status": "not_delivered", **receipt}
        record["accepted"] = True
        return {"status": "accepted", **receipt}

    async def get_execution_lifecycle(self, *, session_key: str, execution_id: str) -> dict:
        """Return only observed local state; unknown remote work remains occupied."""
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        receipt = {"lifecycle_version": LIFECYCLE_VERSION, "session_key": session_key, "execution_id": execution_id}
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            state = "retired" if execution_id in self.__dict__.get("_internal_plugin_retired_executions", {}) else "not_running"
            return {"state": state, "occupancy": "unknown" if state == "retired" else "not_occupied", **receipt}
        current = self._peek_session_state(session_key)
        if record["session_key"] != session_key or not current or current.persistent.run_generation != record["generation"]:
            return {"state": "stale", "occupancy": "unknown", **receipt}
        # We observe a bound agent, not the completion of its thread/tool/process tree.
        return {"state": "interrupt_accepted" if record["accepted"] else "running",
                "occupancy": "occupied", "agent_bound": record["agent"] is not None,
                "tools": "unknown", "children": "unknown", "processes": "unknown", "remote": "unknown", **receipt}
