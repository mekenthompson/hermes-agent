"""Profile-local plugin event dispatch and service lifecycle for GatewayRunner."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayPluginServicesMixin:
    """Keep plugin-owned execution explicitly local to a served profile."""

    def _validate_internal_plugin_event(self, event: MessageEvent, *, private_continuation_grant=None,
                                        execution_id: str | None = None) -> SessionSource:
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
            # Provider routes have no caller-controlled provenance.  A one-shot
            # grant is created by the core after exact persisted-route lookup and
            # is consumed immediately before normal handler entry.
            grants = getattr(self, "_private_continuation_grants", {})
            grant = grants.get(id(private_continuation_grant))
            if (source.platform not in {Platform.SLACK, Platform.TELEGRAM}
                    or grant is None or grant is not private_continuation_grant or grant.used
                    or grant.event is not event or grant.source is not source
                    or grant.execution_id != execution_id
                    or getattr(source, "chat_type", None) != "dm"):
                raise PermissionError("internal plugin events must use the local platform")
            session_key = self._session_key_for_source(source)
            state = self._peek_session_state(session_key)
            if (session_key != grant.session_key or state is None
                    or state.persistent.run_generation != grant.generation - 1):
                raise PermissionError("private continuation owner route is unavailable")
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

    async def dispatch_internal_plugin_event(
        self, event: MessageEvent, *, execution_id: Optional[str] = None,
        execution_policy: Optional[dict] = None, private_continuation_grant=None,
    ) -> Optional[str]:
        """Dispatch a validated plugin event through the normal scoped handler."""
        self._validate_internal_plugin_event(
            event, private_continuation_grant=private_continuation_grant, execution_id=execution_id,
        )
        if execution_policy is not None:
            if execution_id is None:
                raise ValueError("internal execution policy requires an execution_id")
            from gateway.execution_lifecycle import validate_internal_execution_policy
            event._internal_plugin_execution_policy = validate_internal_execution_policy(execution_policy)
        if event.source.platform is not Platform.LOCAL:
            private_continuation_grant.used = True
            self._private_continuation_grants.pop(id(private_continuation_grant), None)
        return await self._primary_message_handler()(event)

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
