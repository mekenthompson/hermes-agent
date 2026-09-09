"""Truthful lifecycle receipts for internal plugin executions.

The lifecycle is deliberately local: a completed receipt means the gateway observed the
agent worker finish with no observed tool/process/child lifetime.  Any observed tool (or
untracked effect) remains unknown rather than being reported released.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
import time
from typing import Any, Optional

_EXECUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LIFECYCLE_VERSION = "execution-lifecycle/v2"

# Tombstones are evidence, not history: `unknown` receipts are the only proof that a
# retired execution was never observed released, so they are capped on their own budget
# instead of pinning the whole list (the old single cap stopped evicting entirely as soon
# as the oldest receipt was `unknown`, so the list grew for the process lifetime).
RETIRED_TOMBSTONE_CAP = 256
UNKNOWN_TOMBSTONE_CAP = 256

# A session quarantined by an execution with unknown occupancy stays fail-closed, but the
# quarantine is not permanent: it lifts as soon as the worker completion Event proves the
# execution finished, and otherwise expires after this TTL.  Without a reopen path a single
# tool-using turn made the session unusable for the rest of the process lifetime.
QUARANTINE_TTL_SECONDS = 900.0

# Bound the number of retirements performed by one opportunistic sweep.
SWEEP_LIMIT = 64

# A record whose dispatch already ended without ever registering a physical worker is
# unprovable: it is retained (a cancellation can outrun worker registration) but not
# forever — after this TTL the sweep reclaims it fail-closed, as unknown occupancy, which
# quarantines the session under the quarantine TTL rather than wedging it permanently.
UNPROVEN_RECORD_TTL_SECONDS = 900.0


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
        # Opportunistic, bounded: retire records whose worker already finished before
        # deciding this session is occupied.
        self._sweep_internal_plugin_executions()
        records = self._internal_plugin_execution_records()
        if self._internal_plugin_session_quarantined(session_key):
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
            "active_tool_calls": 0, "completed_tool_calls": 0, "unproven_since": None,
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

    def _track_internal_plugin_execution_worker(self, execution_id: Optional[str], worker_done: Any) -> None:
        """Attach the physical worker completion Event, never an await-wrapper Task.

        Cancelling an asyncio wrapper around ``to_thread`` marks its Task done while the OS
        thread continues; the worker's threading.Event is the only completion evidence here.
        """
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is not None:
            record["worker_done"] = worker_done
            record["worker_started"] = True

    def _observe_internal_plugin_tool_event(self, execution_id: Optional[str], event_type: str) -> None:
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is not None:
            if event_type == "tool.started":
                record["observed_tool"] = True
                record["active_tool_calls"] += 1
            elif event_type == "tool.completed" and record["active_tool_calls"]:
                record["active_tool_calls"] -= 1
                record["completed_tool_calls"] += 1

    @staticmethod
    def _worker_finished(record: dict) -> bool:
        worker_done = record.get("worker_done")
        if worker_done is None:
            return True
        is_set = getattr(worker_done, "is_set", None)
        return bool(is_set and is_set())

    @staticmethod
    def _prune_retired_executions(retired: OrderedDict) -> None:
        """Evict oldest known receipts first, then cap unknown receipts separately.

        Unknown receipts must not be dropped merely because they are old (losing this
        evidence would falsely report a quarantined execution as not_running/not_occupied),
        but they cannot pin the list either.
        """
        if len(retired) <= RETIRED_TOMBSTONE_CAP:
            return
        for old_execution_id, old in list(retired.items()):
            if len(retired) <= RETIRED_TOMBSTONE_CAP:
                break
            if old["occupancy"] != "unknown":
                retired.pop(old_execution_id)
        unknown_ids = [i for i, r in retired.items() if r["occupancy"] == "unknown"]
        for old_execution_id in unknown_ids[: max(0, len(unknown_ids) - UNKNOWN_TOMBSTONE_CAP)]:
            retired.pop(old_execution_id)

    def _quarantine_internal_plugin_session(self, record: dict, execution_id: Optional[str]) -> None:
        self.__dict__.setdefault("_internal_plugin_quarantined_sessions", {})[record["session_key"]] = {
            "execution_id": execution_id,
            "worker_done": record.get("worker_done"),
            "worker_started": record.get("worker_started", False),
            "retired_at": time.monotonic(),
        }

    def _internal_plugin_session_quarantined(self, session_key: str) -> bool:
        """Fail-closed while the quarantining execution is unproven, with a reopen path.

        The quarantine lifts when the worker completion Event is observed set (the execution
        is proven finished) and, when there is no Event to observe at all, when the TTL
        expires.  Anything else keeps rejecting replacements.
        """
        quarantined = self.__dict__.get("_internal_plugin_quarantined_sessions", {})
        entry = quarantined.get(session_key)
        if entry is None:
            return False
        worker_done = entry["worker_done"]
        proven_finished = worker_done is not None and self._worker_finished({"worker_done": worker_done})
        if proven_finished or (time.monotonic() - entry["retired_at"]) >= QUARANTINE_TTL_SECONDS:
            quarantined.pop(session_key, None)
            return False
        return True

    def _retire_internal_plugin_execution(self, execution_id: Optional[str], *, state: str, occupancy: str) -> None:
        record = self._internal_plugin_execution_records().pop(execution_id, None)
        if record is not None:
            retired = self.__dict__.setdefault("_internal_plugin_retired_executions", OrderedDict())
            retired[execution_id] = {
                "session_key": record["session_key"], "execution_id": execution_id,
                "generation": record["generation"], "state": state, "occupancy": occupancy,
                "tools": "unknown" if record["observed_tool"] else "none",
                "children": "unknown" if record["observed_tool"] else "none",
                "processes": "unknown" if record["observed_tool"] else "none",
                "remote": "unknown" if record["observed_tool"] else "none",
            }
            self._prune_retired_executions(retired)
            if occupancy == "unknown":
                self._quarantine_internal_plugin_session(record, execution_id)

    def _fail_internal_plugin_execution(self, execution_id: Optional[str], *, cancelled: bool = False) -> None:
        """Retire a record whose dispatch raised, so the session is not wedged forever.

        Respects the worker Event: a physically live worker keeps its record (the sweep
        retires it once its Event is set), and a cancellation that could have outrun worker
        registration is left alone — cancelling an await-wrapper is not worker completion.
        """
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            return
        if record["worker_started"] and not self._worker_finished(record):
            return
        if cancelled and not record["worker_started"]:
            # Stamp the retention so the sweep can reclaim it later: with no worker Event to
            # observe, nothing else would ever free this session_key.
            if record["unproven_since"] is None:
                record["unproven_since"] = time.monotonic()
            return
        self._retire_internal_plugin_execution(
            execution_id, state="failed",
            occupancy="unknown" if record["observed_tool"] else "released",
        )

    def _sweep_internal_plugin_executions(self, *, limit: int = SWEEP_LIMIT) -> int:
        """Reclaim leaked records, retiring at most ``limit`` of them.

        Nothing else sweeps leaked records, so a dispatch that raised while its worker was
        still running would otherwise reject every later execution on that session_key.  Two
        kinds are reclaimable: a record whose physical worker Event is set (proven finished,
        retired as completed) and a record whose dispatch ended without ever registering a
        worker and has outlived UNPROVEN_RECORD_TTL_SECONDS (retired fail-closed as unknown,
        which quarantines the session under the quarantine TTL instead of forever).
        """
        records = self._internal_plugin_execution_records()
        swept = 0
        now = time.monotonic()
        for execution_id, record in list(records.items()):
            if swept >= limit:
                break
            if record["worker_started"]:
                if self._worker_finished(record):
                    self._complete_internal_plugin_execution(execution_id)
                    swept += 1
                continue
            unproven_since = record["unproven_since"]
            if unproven_since is not None and (now - unproven_since) >= UNPROVEN_RECORD_TTL_SECONDS:
                self._retire_internal_plugin_execution(
                    execution_id, state="failed", occupancy="unknown",
                )
                swept += 1
        return swept

    def _complete_internal_plugin_execution(self, execution_id: Optional[str], *, wrapper_completed: bool = False) -> None:
        record = self._internal_plugin_execution_records().get(execution_id)
        # A normal outer handler return can still follow a timeout/cancelled await
        # wrapper.  Once a physical worker was registered, only its Event is
        # completion evidence; wrapper_completed is solely for no-worker turns.
        if record is None or (record["worker_started"] and not self._worker_finished(record)) or (
                not record["worker_started"] and not wrapper_completed):
            return
        self._retire_internal_plugin_execution(
            execution_id, state="completed",
            occupancy="unknown" if record["observed_tool"] else "released",
        )

    async def dispatch_internal_plugin_event(self, event, *, execution_id: Optional[str] = None):
        if execution_id is None:
            return await super().dispatch_internal_plugin_event(event)
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        source = self._validate_internal_plugin_event(event)
        event._internal_plugin_execution_id = execution_id
        self._register_internal_plugin_execution(event, self._session_key_for_source(source))
        try:
            result = await super().dispatch_internal_plugin_event(event)
        except BaseException as exc:
            # Wrapper cancellation is not completion evidence for a to_thread worker, but a
            # raising dispatch must not leak the live record either: the session would reject
            # every later execution with "already has a live execution" and nothing sweeps it.
            self._fail_internal_plugin_execution(
                execution_id, cancelled=isinstance(exc, asyncio.CancelledError)
            )
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
        """Observation ABI v2; `released` requires actual worker completion and no tool event."""
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
        state = "stop_requested" if record["stop_requested"] else ("interrupt_accepted" if record["accepted"] else "running")
        return {"state": state, "occupancy": "occupied", "generation": record["generation"],
                "agent_bound": record["agent"] is not None,
                "tools": "unknown" if record["observed_tool"] else "none",
                "children": "unknown" if record["observed_tool"] else "none",
                "processes": "unknown" if record["observed_tool"] else "none",
                "remote": "unknown" if record["observed_tool"] else "none", **receipt}
