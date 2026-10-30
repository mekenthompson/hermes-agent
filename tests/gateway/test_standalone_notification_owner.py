"""Standalone launch pins own transports, not local task/session namespaces."""
import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn


class Adapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, kwargs))

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def runner_for(home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(home))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._init_runtime_caches()
    runner.adapters = {Platform.TELEGRAM: Adapter()}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.SLACK])
@pytest.mark.parametrize("outcome", ["done", "blocked", "review"])
def test_launch_owner_delivers_persisted_subscriptions_without_rekeying(tmp_path, monkeypatch, platform, outcome):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "service-a")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    first = runner_for(home, monkeypatch)
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="completion", assignee="default")
        kbn.add_notify_sub(conn, task_id=task, platform=platform.value, chat_id="chat",
                           thread_id="topic", chat_type="dm", notifier_profile="service-a",
                           delivery_mode="notify+wake")
        if outcome == "done":
            kb.complete_task(conn, task, result="finished")
        elif outcome == "blocked":
            kb.block_task(conn, task, reason="operator stop", kind="needs_input")
        else:
            kb.request_review(conn, task, summary="review requested")
    # Reconstruct the owner after restart, retaining the existing row and cursors.
    runner = runner_for(home, monkeypatch)
    adapter = Adapter()
    runner.adapters = {platform: adapter}
    assert runner._primary_profile_name == first._primary_profile_name == "default"
    rows = _notifier_collect(runner, kb, notifier_profile=runner._kanban_notifier_profile,
                             gc_due=False, gc_retention_days=30)
    assert [r["task"].id for r in rows] == [task]
    assert rows[0]["task"].assignee == "default"
    async def deliver():
        for row in rows:
            await _KanbanNotification(runner, row, platform_cls=Platform, sub_fail_counts={}).deliver()
    asyncio.run(deliver())
    assert len(adapter.sent) == len(adapter.handled) == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, task).status == outcome
    source = adapter.handled[0].source
    assert (source.chat_id, source.thread_id, source.chat_type, source.profile) == (
        "chat", "topic", "dm", "default")
    assert runner._authorization_adapter(platform, "service-a") is adapter
    assert runner._authorization_adapter(platform, "service-b") is None
    assert not _notifier_collect(runner, kb, notifier_profile=runner._kanban_notifier_profile,
                                 gc_due=False, gc_retention_days=30)


@pytest.mark.parametrize("owner", ["default", "service-a"])
def test_local_and_launch_owned_rows_survive_together(tmp_path, monkeypatch, owner):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "service-a")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    runner = runner_for(home, monkeypatch)
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="local completion", assignee="default")
        kbn.add_notify_sub(conn, task_id=task, platform="telegram", chat_id="chat",
                           notifier_profile=owner, delivery_mode="notify")
        kb.complete_task(conn, task, result="finished")
    rows = _notifier_collect(runner, kb, notifier_profile=runner._kanban_notifier_profile,
                             gc_due=False, gc_retention_days=30)
    assert [r["task"].id for r in rows] == [task]


@pytest.mark.parametrize("named,pin,expected", [
    (False, None, "default"), (True, None, "secondary"),
    (True, "secondary", "secondary"), (False, "service-a", "service-a"),
])
def test_launch_identity_matrix(tmp_path, monkeypatch, named, pin, expected):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    if named:
        home = home / "profiles" / "secondary"
    home.mkdir(parents=True)
    for key in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_AGENT_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    if pin:
        monkeypatch.setenv("HERMES_PROFILE", pin)
    runner = runner_for(home, monkeypatch)
    assert runner._kanban_notifier_profile == expected
    assert runner._authorization_adapter(Platform.TELEGRAM, expected) is runner.adapters[Platform.TELEGRAM]
    assert runner._authorization_adapter(Platform.TELEGRAM, "unrelated") is None


@pytest.mark.parametrize("pin,owner", [("Service-A", "service-a"), ("../escape", "default")])
def test_default_home_preserves_launch_pin_validation(tmp_path, monkeypatch, pin, owner):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", pin)
    monkeypatch.setenv("HERMES_AGENT_PROFILE", "legacy")
    runner = runner_for(home, monkeypatch)
    assert runner._kanban_notifier_profile == owner
    assert runner._authorization_adapter(Platform.TELEGRAM, owner) is runner.adapters[Platform.TELEGRAM]
    assert runner._authorization_adapter(Platform.TELEGRAM, pin) is None
    assert runner._authorization_adapter(Platform.TELEGRAM, "legacy") is None
    assert runner._configured_runtime_profile_name == (owner if owner != "default" else None)


def test_bound_home_ignores_launch_pin(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    secondary = home / "profiles" / "secondary"
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_PROFILE", "service-a")
    token = set_hermes_home_override(secondary)
    try:
        runner = runner_for(home, monkeypatch)
        assert runner._kanban_notifier_profile == "secondary"
        assert runner._authorization_adapter(Platform.TELEGRAM, "service-a") is None
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("pin", ["foreign", "../escape"])
def test_named_home_rejects_conflicting_transport_pin(tmp_path, monkeypatch, pin):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes" / "profiles" / "secondary"
    home.mkdir(parents=True)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", pin)
    with pytest.raises(ValueError):
        runner_for(home, monkeypatch)


def test_transport_owner_uses_subscription_pin_precedence_and_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_PROFILE", "lower-priority")
    monkeypatch.setenv("HERMES_PROFILE_NAME", "service-a")
    runner = runner_for(home, monkeypatch)
    assert runner._kanban_notifier_profile == "service-a"
    monkeypatch.setenv("HERMES_PROFILE_NAME", "foreign")
    assert runner._authorization_adapter(Platform.TELEGRAM, "service-a") is runner.adapters[Platform.TELEGRAM]
    assert runner._authorization_adapter(Platform.TELEGRAM, "foreign") is None


def test_multiplex_never_borrows_standalone_launch_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "service-a")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._init_runtime_caches()
    runner.adapters = {Platform.TELEGRAM: Adapter()}
    secondary = Adapter()
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: secondary}, "failed": {}}
    assert runner._authorization_adapter(Platform.TELEGRAM, "secondary") is secondary
    assert runner._authorization_adapter(Platform.TELEGRAM, "failed") is None
    assert runner._authorization_adapter(Platform.TELEGRAM, "service-a") is None
