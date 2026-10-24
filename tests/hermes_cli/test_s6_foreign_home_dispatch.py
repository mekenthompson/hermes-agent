"""s6 lifecycle verbs only act on slots that serve the caller's HERMES_HOME.

Every s6 slot lives in ONE container scandir and runs under the container's own HERMES_HOME
(``/run/s6/container_environment/HERMES_HOME``, sourced by with-contenv). A CLI started with a
different home inside the same container (a disposable test root, a second install) must reach its
own gateway, not stop/start/register/unregister the container's live ``gateway-<profile>`` slot.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _Recorder:
    kind = "s6"

    def __init__(self, profiles: tuple[str, ...] = ("default", "coder")) -> None:
        self.calls: list[tuple[str, str]] = []
        self._profiles = list(profiles)

    def start(self, name: str) -> None:
        self.calls.append(("start", name))

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    def restart(self, name: str) -> None:
        self.calls.append(("restart", name))

    def list_profile_gateways(self) -> list[str]:
        return list(self._profiles)

    def supports_runtime_registration(self) -> bool:
        return True

    def register_profile_gateway(self, profile: str, start_now: bool = False) -> None:
        self.calls.append(("register", profile))

    def unregister_profile_gateway(self, profile: str) -> None:
        self.calls.append(("unregister", profile))


@pytest.fixture
def container(monkeypatch, tmp_path):
    """An s6 container whose slots run under ``<tmp>/container``; returns (root, recorder)."""
    from hermes_cli import service_manager as sm

    root = tmp_path / "container"
    (root / "profiles" / "coder").mkdir(parents=True)
    contenv = tmp_path / "container_environment" / "HERMES_HOME"
    contenv.parent.mkdir()
    contenv.write_text(f"{root}\n", encoding="utf-8")
    rec = _Recorder()
    monkeypatch.setattr(sm, "_S6_CONTAINER_HERMES_HOME", contenv, raising=False)
    monkeypatch.setattr(sm, "detect_service_manager", lambda: "s6")
    monkeypatch.setattr(sm, "get_service_manager", lambda: rec)
    return root, rec


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_foreign_home_never_drives_the_container_slot(monkeypatch, tmp_path, container, action):
    from hermes_cli import gateway as gw

    _root, rec = container
    foreign = tmp_path / "disposable-test-root"
    foreign.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(foreign))

    assert gw._dispatch_via_service_manager_if_s6(action) is False
    assert rec.calls == []


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_foreign_home_all_never_drives_container_slots(monkeypatch, tmp_path, container, action):
    from hermes_cli import gateway as gw

    _root, rec = container
    foreign = tmp_path / "disposable-test-root"
    foreign.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(foreign))

    assert gw._dispatch_all_via_service_manager_if_s6(action) is False
    assert rec.calls == []


@pytest.mark.parametrize(("relative", "service"), [
    pytest.param("", "gateway-default", id="root-home"),
    pytest.param("profiles/coder", "gateway-coder", id="named-profile"),
])
def test_container_homes_still_dispatch_to_their_slot(monkeypatch, container, relative, service):
    from hermes_cli import gateway as gw

    root, rec = container
    monkeypatch.setenv("HERMES_HOME", str(root / relative) if relative else str(root))

    assert gw._dispatch_via_service_manager_if_s6("stop") is True
    assert rec.calls == [("stop", service)]


def test_foreign_profile_root_never_registers_or_unregisters_container_slots(
    monkeypatch, tmp_path, container,
):
    from hermes_cli import profiles

    _root, rec = container
    foreign = tmp_path / "disposable-test-root"
    (foreign / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(foreign))

    profiles._maybe_register_gateway_service("coder")
    profiles._maybe_unregister_gateway_service("coder")

    assert rec.calls == []


def test_container_profile_root_still_registers_and_unregisters(monkeypatch, container):
    from hermes_cli import profiles

    root, rec = container
    monkeypatch.setenv("HERMES_HOME", str(root))

    profiles._maybe_register_gateway_service("coder")
    profiles._maybe_unregister_gateway_service("coder")

    assert rec.calls == [("register", "coder"), ("unregister", "coder")]


def test_unreadable_container_identity_keeps_slot_dispatch(monkeypatch, tmp_path, container):
    """Images without s6-overlay's container_environment keep today's behaviour."""
    from hermes_cli import gateway as gw
    from hermes_cli import service_manager as sm

    _root, rec = container
    monkeypatch.setattr(sm, "_S6_CONTAINER_HERMES_HOME", Path(tmp_path / "absent" / "HERMES_HOME"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "anything"))

    assert gw._dispatch_via_service_manager_if_s6("stop") is True
    assert rec.calls == [("stop", "gateway-default")]
