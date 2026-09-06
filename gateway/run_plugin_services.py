"""Profile-local plugin event dispatch and service lifecycle for GatewayRunner."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayPluginServicesMixin:
    """Keep plugin-owned execution explicitly local to a served profile."""

    @staticmethod
    def _validate_internal_plugin_event(event: MessageEvent) -> SessionSource:
        """Validate the narrow event shape allowed for profile-local plugins."""
        if not isinstance(event, MessageEvent):
            raise TypeError("internal plugin dispatch requires a MessageEvent")
        if event.internal is not True:
            raise PermissionError("internal plugin events must set internal=True")
        if event.allow_gateway_control is not False:
            raise PermissionError(
                "internal plugin events must set allow_gateway_control=False"
            )
        if event.message_type is not MessageType.TEXT:
            raise ValueError("internal plugin dispatch accepts text events only")

        source = event.source
        if source is None:
            raise ValueError("internal plugin events require a SessionSource")
        if source.platform is not Platform.LOCAL:
            raise PermissionError("internal plugin events must use the local platform")
        if not str(getattr(source, "profile", "") or "").strip():
            raise ValueError("internal plugin events require an explicit profile")
        if not str(getattr(source, "chat_id", "") or "").strip():
            raise ValueError("internal plugin events require a session chat_id")
        return source

    async def prepare_internal_plugin_session(self, event: MessageEvent) -> str:
        """Persist a plugin-owned session without beginning an agent turn."""
        from gateway.run import _profile_runtime_scope

        source = self._validate_internal_plugin_event(event)
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(source)
            with _profile_runtime_scope(profile_home):
                entry = await self.async_session_store.get_or_create_session(
                    source, touch_activity=False
                )
        else:
            entry = await self.async_session_store.get_or_create_session(
                source, touch_activity=False
            )
        return str(entry.session_id)

    @staticmethod
    def _validate_internal_plugin_execution_id(execution_id: str) -> str:
        """Return one bounded opaque plugin execution identity or reject it."""
        from gateway.run import _INTERNAL_PLUGIN_EXECUTION_ID_RE

        if not isinstance(execution_id, str) or not _INTERNAL_PLUGIN_EXECUTION_ID_RE.fullmatch(
            execution_id
        ):
            raise ValueError("execution_id must be a canonical opaque identifier")
        return execution_id

    def _internal_plugin_execution_records(self) -> dict:
        records = self.__dict__.get("_internal_plugin_execution_registry")
        if records is None:
            records = {}
            self.__dict__["_internal_plugin_execution_registry"] = records
        return records

    def _register_internal_plugin_execution(self, event: MessageEvent, session_key: str) -> None:
        execution_id = self._validate_internal_plugin_execution_id(
            getattr(event, "_internal_plugin_execution_id", "")
        )
        records = self._internal_plugin_execution_records()
        if execution_id in records or any(
            record["session_key"] == session_key for record in records.values()
        ):
            raise ValueError("internal plugin session already has a live execution")
        state = self._peek_session_state(session_key)
        expected_generation = (
            int(state.persistent.run_generation) + 1 if state is not None else 1
        )
        records[execution_id] = {
            "session_key": session_key,
            "source": event.source,
            "generation": expected_generation,
            "agent": None,
        }

    def _bind_internal_plugin_execution(
        self, event_or_execution_id: "MessageEvent | str", *, session_key: str,
        run_generation: int, agent: Any,
    ) -> bool:
        execution_id = (
            event_or_execution_id
            if isinstance(event_or_execution_id, str)
            else getattr(event_or_execution_id, "_internal_plugin_execution_id", None)
        )
        record = self._internal_plugin_execution_records().get(execution_id)
        state = self._peek_session_state(session_key)
        if (
            record is None
            or record["session_key"] != session_key
            or record["generation"] != run_generation
            or state is None
            or state.persistent.run_generation != run_generation
            or state.turn.agent is not agent
        ):
            return False
        record["agent"] = agent
        return True

    def _promote_running_agent(
        self, *, session_key: str, run_generation: Optional[int], agent: Any,
        internal_plugin_execution_id: Optional[str] = None,
    ) -> bool:
        """Publish one live agent and bind a matching internal execution safely."""
        state = self._peek_session_state(session_key)
        if state is None or (
            run_generation is not None
            and state.persistent.run_generation != run_generation
        ) or (internal_plugin_execution_id is not None and run_generation is None):
            return False
        state.turn.agent = agent
        if internal_plugin_execution_id is not None:
            assert run_generation is not None
            self._bind_internal_plugin_execution(
                internal_plugin_execution_id,
                session_key=session_key,
                run_generation=run_generation,
                agent=agent,
            )
        return True

    def _retire_internal_plugin_execution(self, execution_id: Optional[str]) -> None:
        if not execution_id:
            return
        record = self._internal_plugin_execution_records().pop(execution_id, None)
        if record is not None:
            retired = self.__dict__.setdefault("_internal_plugin_retired_executions", OrderedDict())
            retired[execution_id] = None
            retired.move_to_end(execution_id)
            while len(retired) > 256:
                retired.popitem(last=False)

    async def request_stop(
        self, *, session_key: str, expected_execution_id: str,
        reason: str = "Internal plugin stop requested",
    ) -> dict:
        from gateway.run import _AGENT_PENDING_SENTINEL, request_hard_interrupt

        execution_id = self._validate_internal_plugin_execution_id(expected_execution_id)
        receipt = {"session_key": session_key, "execution_id": execution_id}
        record = self._internal_plugin_execution_records().get(execution_id)
        if record is None:
            status = (
                "stale"
                if execution_id in self.__dict__.get("_internal_plugin_retired_executions", {})
                else "not_running"
            )
            return {"status": status, **receipt}
        state = self._peek_session_state(session_key)
        if (
            record["session_key"] != session_key
            or state is None
            or state.persistent.run_generation != record["generation"]
        ):
            return {"status": "stale", **receipt}
        agent = record["agent"]
        if (
            agent is None
            or agent is _AGENT_PENDING_SENTINEL
            or state.turn.agent is not agent
        ):
            # A pending state has no exact agent to which a Stop could have
            # been delivered.  Do not manufacture a binding from the live slot.
            return {"status": "not_running", **receipt}
        if not request_hard_interrupt(agent, reason):
            return {"status": "not_delivered", **receipt}
        # "accepted" acknowledges delivery to this bound agent only; it does
        # not claim that executor-backed work has physically stopped.
        return {"status": "accepted", **receipt}

    async def dispatch_internal_plugin_event(
        self,
        event: MessageEvent,
        *,
        execution_id: Optional[str] = None,
    ) -> Optional[str]:
        """Dispatch a validated plugin event through the normal scoped handler.

        With ``execution_id`` the turn is registered so ``request_stop`` can bind a Stop to
        exactly this execution; the record is retired only after the real handler returns.
        """
        source = self._validate_internal_plugin_event(event)
        if execution_id is None:
            return await self._primary_message_handler()(event)
        execution_id = self._validate_internal_plugin_execution_id(execution_id)
        setattr(event, "_internal_plugin_execution_id", execution_id)
        self._register_internal_plugin_execution(
            event, self._session_key_for_source(source)
        )
        handler_completed = False
        try:
            result = await self._primary_message_handler()(event)
            handler_completed = True
            return result
        finally:
            # A wrapper cancellation is not evidence that executor-backed work
            # stopped. Retire only after the real handler returned normally.
            if handler_completed:
                self._retire_internal_plugin_execution(execution_id)

    def _start_plugin_profile_services(self) -> None:
        """Start each registered profile service in its owning profile scope."""
        from gateway.run import _multiplex_profile_homes, _profile_runtime_scope
        from hermes_cli.plugins import get_plugin_manager
        from hermes_constants import get_hermes_home

        stop_event = getattr(self, "_profile_service_stop", None)
        if stop_event is None or stop_event.is_set():
            stop_event = asyncio.Event()
            self._profile_service_stop = stop_event
        self._profile_service_tasks = []

        active_profile = (
            os.getenv("HERMES_PROFILE") or os.getenv("HERMES_AGENT_PROFILE") or "default"
        ).strip() or "default"
        profiles = [(active_profile, Path(get_hermes_home()))]
        if getattr(self.config, "multiplex_profiles", False):
            profiles.extend(
                (name, Path(home))
                for name, home in _multiplex_profile_homes(self.config)
                if name != active_profile
            )

        for profile_name, profile_home in profiles:
            with _profile_runtime_scope(profile_home):
                runtime = SimpleNamespace(
                    profile_home=str(get_hermes_home()),
                    profile_name=profile_name,
                    stop_event=stop_event,
                    gateway=self,
                )
                try:
                    services = list(get_plugin_manager()._profile_services)
                except Exception:
                    logger.debug(
                        "Could not load plugin profile services for %s",
                        profile_name,
                        exc_info=True,
                    )
                    continue
                for name, factory in services:
                    try:
                        task = asyncio.create_task(
                            factory(runtime), name=f"profile-service:{profile_name}:{name}"
                        )
                    except Exception:
                        logger.exception(
                            "Failed to start plugin profile service %s for profile %s",
                            name,
                            profile_name,
                        )
                        continue
                    self._profile_service_tasks.append(task)
                    logger.info(
                        "Started plugin profile service %s for profile %s",
                        name,
                        profile_name,
                    )

    async def _stop_plugin_profile_services(self) -> None:
        stop_event = getattr(self, "_profile_service_stop", None)
        if stop_event is not None and not stop_event.is_set():
            stop_event.set()
        tasks = list(getattr(self, "_profile_service_tasks", []) or [])
        self._profile_service_tasks = []
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
