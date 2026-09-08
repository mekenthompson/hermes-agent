"""Truthful lifecycle receipts for internal plugin executions.

The lifecycle is deliberately local: a completed receipt means the gateway observed the
agent worker finish with no observed tool/process/child lifetime.  Any observed tool (or
untracked effect) remains unknown rather than being reported released.
"""
from __future__ import annotations

from collections import OrderedDict
import math
import re
from typing import Any, Optional

_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_POLICY_FIELDS = {"max_iterations", "wall_seconds"}
LIFECYCLE_VERSION = "execution-lifecycle/v2"


def validate_internal_execution_policy(value: object) -> dict:
    """Return the canonical, finite limits for one named plugin execution."""
    if not isinstance(value, dict) or set(value) != _POLICY_FIELDS:
        raise ValueError("internal execution policy fields are invalid")
    max_iterations = value["max_iterations"]
    wall_seconds = value["wall_seconds"]
    finite_positive = lambda item: (not isinstance(item, bool) and isinstance(item, (int, float))
                                    and math.isfinite(float(item)) and float(item) > 0)
    if (type(max_iterations) is not int or max_iterations < 1
            or not finite_positive(wall_seconds)):
        raise ValueError("internal execution policy requires positive finite limits")
    return {
        "max_iterations": max_iterations,
        "wall_seconds": float(wall_seconds),
    }


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
        quarantined = self.__dict__.get("_internal_plugin_quarantined_sessions", {})
        if session_key in quarantined:
            raise ValueError("internal plugin session is quarantined by an execution with unknown effects")
        if execution_id in records or any(r["session_key"] == session_key for r in records.values()):
            raise ValueError("internal plugin session already has a live execution")
        # Establish the Stop target before dispatch reaches the ordinary session-claim path.
        state = self._session_state(session_key)
        records[execution_id] = {
            "session_key": session_key, "source": event.source,
            "generation": int(state.persistent.run_generation) + 1 if state else 1,
            "agent": None, "accepted": False, "stop_requested": False,
            "worker_done": None, "worker_started": False, "observed_tool": False,
            "tool_calls": {}, "tool_lifetime_evidence_invalid": False,
            "task_id": "", "parent_session_id": "",
        }

    def _bind_internal_plugin_execution(self, execution_id: Optional[str], *, session_key: str,
                                        run_generation: int, agent: Any) -> bool:
        if not isinstance(execution_id, str):
            execution_id = getattr(execution_id, "_internal_plugin_execution_id", None)
        record = self._internal_plugin_execution_records().get(execution_id)
        state = self._peek_session_state(session_key)
        if (not record or record["stop_requested"] or record["session_key"] != session_key
                or record["generation"] != run_generation or not state
                or state.persistent.run_generation != run_generation or state.turn.agent is not agent):
            return False
        record["agent"] = agent
        return True

    def _promote_running_agent(self, *, session_key: str, run_generation: int, agent: Any,
                               internal_plugin_execution_id: Optional[str] = None) -> bool:
        """Promotion fence. A Stop accepted during preparation prevents launch."""
        state = self._session_state(session_key)
        if state.persistent.run_generation != run_generation:
            return False
        if internal_plugin_execution_id is not None:
            record = self._internal_plugin_execution_records().get(internal_plugin_execution_id)
            if not record or record["stop_requested"]:
                return False
        state.turn.agent = agent
        if internal_plugin_execution_id is None:
            return True
        return self._bind_internal_plugin_execution(internal_plugin_execution_id,
                                                     session_key=session_key,
                                                     run_generation=run_generation, agent=agent)

    def _track_internal_plugin_execution_worker(
        self, execution_id: Optional[str], worker_done: Any, *, task_id: str = "",
        session_key: str = "", parent_session_id: str = "",
    ) -> None:
        """Attach the physical worker completion Event, never an await-wrapper Task.

        Cancelling an asyncio wrapper around ``to_thread`` marks its Task done while the OS
        thread continues; the worker's threading.Event is the only completion evidence here.
        """
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is not None:
            record["worker_done"] = worker_done
            record["worker_started"] = True
            if session_key != record["session_key"]:
                record["tool_lifetime_evidence_invalid"] = True
            record["task_id"] = task_id
            record["parent_session_id"] = parent_session_id

    def _observe_internal_plugin_tool_event(
        self, execution_id: Optional[str], event_type: str, *, tool_call_id: Optional[str] = None,
        tool_lifetime: Optional[str] = None,
    ) -> None:
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None or event_type not in {"tool.started", "tool.completed"}:
            return
        record["observed_tool"] = True
        if not isinstance(tool_call_id, str) or not tool_call_id or tool_lifetime not in {"settled", "may_spawn", "unknown"}:
            record["tool_lifetime_evidence_invalid"] = True
            return
        calls = record["tool_calls"]
        if event_type == "tool.started":
            if tool_call_id in calls:
                record["tool_lifetime_evidence_invalid"] = True
                return
            calls[tool_call_id] = {"lifetime": tool_lifetime, "completed": False}
        elif tool_call_id not in calls or calls[tool_call_id]["completed"] or calls[tool_call_id]["lifetime"] != tool_lifetime:
            record["tool_lifetime_evidence_invalid"] = True
        else:
            calls[tool_call_id]["completed"] = True

    @staticmethod
    def _worker_finished(record: dict) -> bool:
        worker_done = record.get("worker_done")
        if worker_done is None:
            return True
        is_set = getattr(worker_done, "is_set", None)
        return bool(is_set and is_set())

    def _retire_internal_plugin_execution(self, execution_id: Optional[str], *, state: str, occupancy: str) -> None:
        record = self._internal_plugin_execution_records().pop(execution_id, None)
        if record is not None:
            retired = self.__dict__.setdefault("_internal_plugin_retired_executions", OrderedDict())
            retired[execution_id] = {
                "session_key": record["session_key"], "execution_id": execution_id,
                "generation": record["generation"], "state": state, "occupancy": occupancy,
                **self._lifetime_dimensions(record, settled=occupancy == "released"),
            }
            # Do not evict unknown receipts: losing this evidence would falsely
            # report a quarantined execution as not_running/not_occupied.
            while len(retired) > 256:
                old_execution_id, old = next(iter(retired.items()))
                if old["occupancy"] == "unknown":
                    break
                retired.pop(old_execution_id)
            if occupancy == "unknown":
                self.__dict__.setdefault("_internal_plugin_quarantined_sessions", {})[
                    record["session_key"]
                ] = execution_id

    def _complete_internal_plugin_execution(self, execution_id: Optional[str], *, wrapper_completed: bool = False) -> None:
        record = self._internal_plugin_execution_records().get(execution_id)
        # A normal outer handler return can still follow a timeout/cancelled await
        # wrapper.  Once a physical worker was registered, only its Event is
        # completion evidence; wrapper_completed is solely for no-worker turns.
        if record is None or (record["worker_started"] and not self._worker_finished(record)) or (
                not record["worker_started"] and not wrapper_completed):
            return
        settled = self._execution_lifetimes_settled(record)
        self._retire_internal_plugin_execution(execution_id, state="completed",
                                                occupancy="released" if settled else "unknown")

    @staticmethod
    def _lifetime_dimensions(record: dict, *, settled: bool) -> dict:
        if settled:
            # Receipt v2's "none" means no outstanding lifetime, not no calls observed.
            return {"tools": "none", "children": "none", "processes": "none", "remote": "none"}
        return {"tools": "unknown" if record["observed_tool"] else "none",
                "children": "unknown" if record["observed_tool"] else "none",
                "processes": "unknown" if record["observed_tool"] else "none",
                "remote": "unknown" if record["observed_tool"] else "none"}

    def _execution_lifetimes_settled(self, record: dict) -> bool:
        """Fail closed unless each real call and every native lifetime checker agrees."""
        if not record["observed_tool"]:
            return True
        calls = record["tool_calls"]
        if record["tool_lifetime_evidence_invalid"] or not calls:
            return False
        if any(call["lifetime"] != "settled" or not call["completed"] for call in calls.values()):
            return False
        if not record["task_id"] or not record["session_key"] or not record["parent_session_id"]:
            return False
        try:
            agent = record["agent"]
            children = getattr(agent, "_active_children")
            if not isinstance(children, (list, tuple, set)):
                return False
            lock = getattr(agent, "_active_children_lock", None)
            if lock is None:
                return False
            with lock:
                if children:
                    return False
            from tools.process_registry import process_registry
            from tools.async_delegation import has_live_for_session
            if process_registry.has_active_processes(record["task_id"]):
                return False
            if has_live_for_session(session_key=record["session_key"], parent_session_id=record["parent_session_id"]):
                return False
        except Exception:
            return False
        return True

    async def dispatch_internal_plugin_event(
        self, event, *, execution_id: Optional[str] = None, execution_policy: Optional[dict] = None,
        private_continuation_grant=None,
    ):
        if execution_id is None:
            return await super().dispatch_internal_plugin_event(
                event, execution_policy=execution_policy, private_continuation_grant=private_continuation_grant,
            )
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        source = self._validate_internal_plugin_event(
            event, private_continuation_grant=private_continuation_grant, execution_id=execution_id,
        )
        if execution_policy is not None:
            event._internal_plugin_execution_policy = validate_internal_execution_policy(execution_policy)
        event._internal_plugin_execution_id = execution_id
        session_key = self._session_key_for_source(source)
        existing = self._internal_plugin_execution_records().get(execution_id)
        if existing is None:
            self._register_internal_plugin_execution(event, session_key)
        elif (existing["session_key"] != session_key
              or existing["generation"] != getattr(private_continuation_grant, "generation", None)):
            raise ValueError("internal plugin execution reservation is stale")
        if self._internal_plugin_execution_records()[execution_id]["stop_requested"]:
            return None
        try:
            result = await super().dispatch_internal_plugin_event(
                event, execution_id=execution_id, execution_policy=execution_policy,
                private_continuation_grant=private_continuation_grant,
            )
        except BaseException:
            # Wrapper cancellation is not completion evidence for a to_thread worker.
            raise
        else:
            self._complete_internal_plugin_execution(execution_id, wrapper_completed=True)
            return result

    async def request_stop(self, *, session_key: str, expected_execution_id: str,
                           reason: str = "Internal plugin stop requested") -> dict:
        from gateway.run import _AGENT_PENDING_SENTINEL, request_hard_interrupt
        execution_id = self._validate_internal_plugin_execution_id(expected_execution_id)
        receipt = {"session_key": session_key, "execution_id": execution_id}
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            status = "stale" if execution_id in self.__dict__.get("_internal_plugin_retired_executions", {}) else "not_running"
            return {"status": status, **receipt}
        state = self._peek_session_state(session_key)
        # Registration reserves generation N+1 before the ordinary dispatcher claims it.
        # Stop is valid in that narrow pre-claim interval, but never for an unrelated
        # occupied session or a generation other than the reserved next one.
        pre_claim = bool(
            state and record["agent"] is None and state.turn.agent is None
            and state.persistent.run_generation == record["generation"] - 1
        )
        if (record["session_key"] != session_key or not state
                or (state.persistent.run_generation != record["generation"] and not pre_claim)):
            return {"status": "stale", **receipt}
        agent = record["agent"]
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            record["stop_requested"] = True
            record["accepted"] = True
            return {"status": "accepted", **receipt}
        if state.turn.agent is not agent or not request_hard_interrupt(agent, reason):
            return {"status": "not_delivered", **receipt}
        record["accepted"] = True
        return {"status": "accepted", **receipt}

    async def get_execution_lifecycle(self, *, session_key: str, execution_id: str) -> dict:
        """Observation ABI v2; `released` requires physical completion and proven settled lifetimes."""
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        receipt = {"lifecycle_version": LIFECYCLE_VERSION, "session_key": session_key, "execution_id": execution_id}
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            tombstone = self.__dict__.get("_internal_plugin_retired_executions", {}).get(execution_id)
            if tombstone is None:
                return {"state": "not_running", "occupancy": "not_occupied", **receipt}
            if tombstone["session_key"] != session_key:
                return {"state": "stale", "occupancy": "unknown", **receipt}
            return {**tombstone, **receipt}
        current = self._peek_session_state(session_key)
        if record["session_key"] != session_key or not current or current.persistent.run_generation != record["generation"]:
            return {"state": "stale", "occupancy": "unknown", "generation": record["generation"], **receipt}
        if record["worker_started"] and self._worker_finished(record):
            self._complete_internal_plugin_execution(execution_id)
            return await self.get_execution_lifecycle(session_key=session_key, execution_id=execution_id)
        stopped_before_agent = record["stop_requested"] and record["agent"] is None
        state = "stopped" if stopped_before_agent else (
            "stop_requested" if record["stop_requested"] else ("interrupt_accepted" if record["accepted"] else "running")
        )
        return {"state": state, "occupancy": "released" if stopped_before_agent else "occupied", "generation": record["generation"],
                "agent_bound": record["agent"] is not None,
                **self._lifetime_dimensions(record, settled=False), **receipt}
