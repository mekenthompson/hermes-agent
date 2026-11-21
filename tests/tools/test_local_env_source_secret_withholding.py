"""Names an external secret source hydrated (``secrets.onepassword.env`` and friends) are arbitrary
operator choices no static blocklist knows: ``CLOUDFLARE_API_TOKEN``, ``POSTIZ_API_KEY`` ... With
inherit-all, every terminal command, background job, script and browser started holding all of them,
so one ``env`` in a probe copied them into tool logs, coding-CLI shell snapshots and Chromium crash
dumps. Under ``secrets.child_env: declared`` (the default) a spawned child gets such a name only by
declaration (``terminal.env_passthrough`` / a skill's ``required_environment_variables``); helper
children (browser, TTS, LSP, ACP) never get one. Hermes processes acting for the profile keep them.
Every assertion observes a real child process."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import env_loader
from tests.tools._child_env_fixtures import child_env, observe_child
from tools import env_passthrough
from tools.environments import local, local_env_policy

# Source-hydrated names no blocklist enumerates, one of them case-folded.
SOURCE_NAMES = ["CLOUDFLARE_API_TOKEN", "POSTIZ_API_KEY", "HA_ACCESS_TOKEN"]
# Inherited names no source supplied: the scrub must not widen to them.
OPERATOR_KEPT = ["MY_CUSTOM_VAR", "AWS_SECRET_ACCESS_KEY"]
# A provider credential a source also supplied: Tier 2, kept for credentialed children only.
SOURCE_PROVIDER = "OPENAI_API_KEY"


@pytest.fixture
def hydrated(monkeypatch, child_env):  # health: allow F811 -- pytest fixture name doubles as the parameter, the shape every file in tests/tools uses
    for name in [*SOURCE_NAMES, *OPERATOR_KEPT, SOURCE_PROVIDER]:
        monkeypatch.setenv(name, "fake-" + name)
    # What load_hermes_dotenv records after ``secrets.onepassword.env`` hydrated these names.
    monkeypatch.setattr(env_loader, "_SECRET_SOURCES", {n: "onepassword" for n in SOURCE_NAMES[:2]})
    monkeypatch.setattr(env_loader, "_SOURCE_SUPPLIED_NAMES", {SOURCE_NAMES[2].lower(), SOURCE_PROVIDER})
    # The real reader against the fixture's empty profile home: the default applies.
    monkeypatch.setattr(local_env_policy, "_raw_config_behind_effective", dict, raising=False)
    return child_env


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(local_env_policy, "_raw_config_behind_effective",
                        lambda: {"secrets": {"child_env": mode}})


TERMINAL_BUILDERS = [
    pytest.param(lambda: local._make_run_env({}), id="terminal"),
    pytest.param(lambda: local._sanitize_subprocess_env(local.routed_terminal_base()), id="background"),
    pytest.param(lambda: local.build_subprocess_env(), id="script"),
]
HELPER_BUILDERS = [
    pytest.param(lambda: local.hermes_subprocess_env(), id="helper"),
    pytest.param(lambda: local.served_profile_child_env(inherit_credentials=False), id="browser"),
]
CREDENTIALED_HELPER = pytest.param(lambda: local.hermes_subprocess_env(inherit_credentials=True), id="acp")


@pytest.mark.parametrize("builder", [*TERMINAL_BUILDERS, *HELPER_BUILDERS, CREDENTIALED_HELPER])
def test_undeclared_source_secrets_never_reach_a_child(hydrated, builder):
    observed = observe_child(builder(), [*SOURCE_NAMES, *OPERATOR_KEPT])
    assert {k: observed[k] for k in SOURCE_NAMES} == dict.fromkeys(SOURCE_NAMES)
    assert {k: observed[k] for k in OPERATOR_KEPT} == {k: "fake-" + k for k in OPERATOR_KEPT}


def test_env_dump_in_a_terminal_command_shows_no_source_secret(hydrated):
    env = local.LocalEnvironment(cwd=str(hydrated), timeout=30, persistent=False)
    try:
        result = env.execute("env")
    finally:
        env.cleanup()
    assert result["returncode"] == 0, result
    for name in SOURCE_NAMES:
        assert "fake-" + name not in result["output"]
    assert "fake-MY_CUSTOM_VAR" in result["output"]


@pytest.mark.parametrize("declare", ["config", "skill"])
@pytest.mark.parametrize("builder", TERMINAL_BUILDERS)
def test_declared_source_secret_reaches_terminal_children(hydrated, monkeypatch, builder, declare):
    if declare == "config":
        monkeypatch.setattr(env_passthrough, "_load_config_passthrough", lambda: frozenset({"CLOUDFLARE_API_TOKEN"}))
    else:
        assert env_passthrough.register_env_passthrough(["CLOUDFLARE_API_TOKEN"]) == []
    observed = observe_child(builder(), SOURCE_NAMES)
    assert observed == {"CLOUDFLARE_API_TOKEN": "fake-CLOUDFLARE_API_TOKEN",
                        "POSTIZ_API_KEY": None, "HA_ACCESS_TOKEN": None}


@pytest.mark.parametrize("builder", HELPER_BUILDERS)
def test_declaration_does_not_open_helper_children(hydrated, monkeypatch, builder):
    # The browser / TTS / LSP children have no passthrough contract; a terminal declaration is not one.
    monkeypatch.setattr(env_passthrough, "_load_config_passthrough", lambda: frozenset({"CLOUDFLARE_API_TOKEN"}))
    assert observe_child(builder(), ["CLOUDFLARE_API_TOKEN"]) == {"CLOUDFLARE_API_TOKEN": None}


def test_explicit_caller_mapping_is_not_inheritance(hydrated):
    # A background command's own ``env`` / a terminal backend's ``env`` set the name on purpose.
    background = local._sanitize_subprocess_env(local.routed_terminal_base(), {"POSTIZ_API_KEY": "mine"})
    terminal = local._make_run_env({"POSTIZ_API_KEY": "mine"})
    for env in (background, terminal):
        assert observe_child(env, ["POSTIZ_API_KEY", "CLOUDFLARE_API_TOKEN"]) == {
            "POSTIZ_API_KEY": "mine", "CLOUDFLARE_API_TOKEN": None}


def test_credentialed_children_keep_source_provider_credentials(hydrated):
    # Tier 2 semantics are unchanged: an ACP / codex child still gets the provider key.
    observed = observe_child(local.hermes_subprocess_env(inherit_credentials=True), [SOURCE_PROVIDER])
    assert observed == {SOURCE_PROVIDER: "fake-" + SOURCE_PROVIDER}
    assert observe_child(local.hermes_subprocess_env(), [SOURCE_PROVIDER]) == {SOURCE_PROVIDER: None}


@pytest.mark.parametrize("builder", [
    pytest.param(lambda: local.build_subprocess_env(withhold_source_secrets=False), id="worker"),
    pytest.param(lambda: local.served_profile_child_env(inherit_credentials=True), id="profile-agent"),
])
def test_hermes_agent_children_keep_the_profile_secrets(hydrated, builder):
    observed = observe_child(builder(), SOURCE_NAMES)
    assert observed == {k: "fake-" + k for k in SOURCE_NAMES}


WORKER_BUILDERS = [
    pytest.param(lambda: local.build_subprocess_env(withhold_source_secrets=False), id="kanban-worker"),
    pytest.param(lambda: local.build_subprocess_env(scrub_secrets=False), id="unscrubbed-worker"),
    pytest.param(lambda: local.served_profile_child_env(inherit_credentials=True), id="profile-agent"),
]
# The worker re-runs its own source fetch, and that fetch fails (rate limit, Connect down): it records
# no provenance of its own, yet its environ holds every value the parent's source hydrated.
_WORKER = """
import json, sys
from pathlib import Path
from hermes_cli import env_loader
home = Path(sys.argv[1])
env_loader.load_hermes_dotenv(hermes_home=home)
from tools.environments import local
names = json.loads(sys.argv[2])
print(json.dumps({
    "own_record": list(env_loader.source_supplied_names()),
    "terminal": {k: local.build_subprocess_env().get(k) for k in names},
    "background": {k: local._sanitize_subprocess_env(dict(__import__("os").environ)).get(k) for k in names},
    "helper": {k: local.hermes_subprocess_env().get(k) for k in names},
}))
"""


@pytest.mark.platforms("posix")  # the fake ``op`` is a shell script
@pytest.mark.parametrize("builder", WORKER_BUILDERS)
def test_worker_whose_own_source_fetch_fails_still_withholds(hydrated, builder):
    home = hydrated / "hermes"
    home.mkdir(exist_ok=True)
    op = hydrated / "op"
    op.write_text("#!/bin/sh\necho '[ERROR] rate limited' >&2\nexit 1\n")
    op.chmod(0o755)
    (home / "config.yaml").write_text(
        "secrets:\n  onepassword:\n    enabled: true\n    binary_path: " + str(op) + "\n"
        "    cache_ttl_seconds: 0\n    env:\n      CLOUDFLARE_API_TOKEN: op://v/i/f\n")
    env = builder()
    env.update(HERMES_HOME=str(home), OP_SERVICE_ACCOUNT_TOKEN="fake-bootstrap")
    # The worker holds the parent's values: it is a Hermes agent for the profile.
    assert {k: env.get(k) for k in SOURCE_NAMES} == {k: "fake-" + k for k in SOURCE_NAMES}
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", _WORKER, str(home), json.dumps([*SOURCE_NAMES, *OPERATOR_KEPT])],
                            env=env, cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["own_record"] == []  # the precondition: the worker's own fetch recorded nothing
    for child in ("terminal", "background", "helper"):
        assert {k: observed[child][k] for k in SOURCE_NAMES} == dict.fromkeys(SOURCE_NAMES), child
    for child in ("terminal", "background"):
        assert {k: observed[child][k] for k in OPERATOR_KEPT} == {k: "fake-" + k for k in OPERATOR_KEPT}, child


def test_carried_names_are_names_only_and_validated(hydrated, monkeypatch):
    env = local.build_subprocess_env(withhold_source_secrets=False)
    carried = env[env_loader.SOURCE_SUPPLIED_NAMES_ENV]
    assert not any("fake-" in part for part in carried.split(","))
    monkeypatch.setenv(env_loader.SOURCE_SUPPLIED_NAMES_ENV, "GOOD_NAME, bad name,1BAD,,=X")
    assert env_loader.inherited_source_supplied_names() == {"GOOD_NAME"}


@pytest.mark.parametrize("builder", [*TERMINAL_BUILDERS, *HELPER_BUILDERS])
def test_inherit_mode_restores_the_previous_behaviour(hydrated, monkeypatch, builder):
    _set_mode(monkeypatch, "inherit")
    observed = observe_child(builder(), SOURCE_NAMES)
    assert observed == {k: "fake-" + k for k in SOURCE_NAMES}


@pytest.mark.parametrize("raw, mode", [
    ({}, "declared"),
    ({"secrets": {"child_env": "inherit"}}, "inherit"),
    ({"secrets": {"child_env": " Inherit "}}, "inherit"),
    ({"secrets": {"child_env": "declared"}}, "declared"),
    ({"secrets": {"child_env": "all"}}, "declared"),  # unknown value fails closed
    ({"secrets": {"child_env": True}}, "declared"),
    ({"secrets": "garbage"}, "declared"),
    ("garbage", "declared"),
])
def test_mode_reads_config_and_fails_closed(monkeypatch, raw, mode):
    monkeypatch.setattr(local_env_policy, "_raw_config_behind_effective", lambda: raw)
    assert local_env_policy.source_secret_child_env_mode() == mode


def test_unreadable_config_fails_closed(monkeypatch):
    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr(local_env_policy, "_raw_config_behind_effective", boom)
    assert local_env_policy.source_secret_child_env_mode() == "declared"


def test_default_config_is_declared():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["secrets"]["child_env"] == "declared"
