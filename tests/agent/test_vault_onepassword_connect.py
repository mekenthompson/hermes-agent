"""1Password Connect as a browser-vault login source (HF-739).

A profile whose secrets hold ``OP_CONNECT_HOST`` + ``OP_CONNECT_TOKEN`` has no master password to
type and ``op item list`` refuses to run against Connect, so before this change every such profile
reported 1Password as locked. These tests drive the real backend and tool code against a loopback
stand-in for the Connect REST API:

1. Connect counts as unlocked and lists only LOGIN items of the configured vaults; a vault
   selector that names nothing, or more than one vault, fails closed.
2. A fill or code read is only for an item that scoped listing returned and that is still saved
   for the websites it was listed with, and reads the login's own password field; refused handles
   cause no item request at all.
3. The bearer token never goes through a proxy or to a redirect target and never appears in an
   error or tool result.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from agent.vault_store import normalize_otp_secret, totp_now

TOKEN = "connect-token-for-tests-only"
OWN = "ownvaultid00000000000000aa"
ADMIN = "adminvaultid0000000000000b"
LOGIN = "loginitemid000000000000001"
SECOND = "loginitemid000000000000002"
ADMIN_LOGIN = "adminlogin0000000000000001"
NOTE = "secureitemnote000000000001"
PASSWORD = "synthetic-connect-password"
SEED = "JBSWY3DPEHPK3PXP"
WEBSITES = [{"href": "https://example.com/login", "primary": True}, {"href": "www.example.com/sso"}]
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


def _connect_state():
    """A Connect deployment shaped like the live one: an own vault and an admin vault with logins."""
    return {
        "vaults": [{"id": OWN, "name": "agent-own"}, {"id": ADMIN, "name": "agent-admin"}],
        "summaries": {
            OWN: [
                {"id": LOGIN, "title": "Example", "category": "LOGIN", "vault": {"id": OWN},
                 "additionalInformation": "jane", "createdAt": "2026-11-18T00:00:00Z", "urls": WEBSITES},
                {"id": SECOND, "title": "No website", "category": "LOGIN", "vault": {"id": OWN}},
                {"id": NOTE, "title": "Note", "category": "SECURE_NOTE", "vault": {"id": OWN},
                 "urls": [{"href": "https://example.com"}]},
            ],
            ADMIN: [{"id": ADMIN_LOGIN, "title": "Admin console", "category": "LOGIN", "vault": {"id": ADMIN},
                     "urls": [{"href": "https://admin.example.com"}]}],
        },
        "items": {
            (OWN, LOGIN): {"id": LOGIN, "category": "LOGIN", "vault": {"id": OWN}, "urls": WEBSITES, "fields": [
                {"id": "notesPlain", "purpose": "NOTES", "type": "STRING", "value": "not the password"},
                {"id": "custom", "label": "password", "type": "CONCEALED", "value": "decoy-custom-field"},
                {"id": "password", "purpose": "PASSWORD", "type": "CONCEALED", "value": PASSWORD},
                {"id": "otp-broken", "type": "OTP", "value": "not base32!"},
                {"id": "otp", "type": "OTP", "value": f"otpauth://totp/Example:jane?secret={SEED}&issuer=Example"},
            ]},
            (ADMIN, ADMIN_LOGIN): {"id": ADMIN_LOGIN, "category": "LOGIN", "vault": {"id": ADMIN},
                                   "urls": [{"href": "https://admin.example.com"}], "fields": [
                {"id": "password", "purpose": "PASSWORD", "type": "CONCEALED", "value": "admin-secret"}]},
        },
        "log": [],          # (path, sent the right bearer token)
        "override": {},     # path -> (status, headers, body: JSON value or raw bytes)
        "hold": None,       # threading.Event a request waits on before answering
    }


def _serve(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["log"].append((self.path, self.headers.get("Authorization") == f"Bearer {TOKEN}"))
            if state["hold"] is not None:
                state["hold"].wait(10)
            if self.path in state["override"]:
                status, headers, body = state["override"][self.path]
                return self._send(status, body, headers)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"status": 401, "message": "Invalid token signature"})
            parts = self.path.strip("/").split("/")
            if parts == ["v1", "vaults"]:
                return self._send(200, state["vaults"])
            if len(parts) == 4 and parts[:2] == ["v1", "vaults"] and parts[3] == "items":
                return self._send(200, state["summaries"].get(parts[2], []))
            if len(parts) == 5 and parts[:2] == ["v1", "vaults"] and parts[3] == "items":
                item = state["items"].get((parts[2], parts[4]))
                return self._send(200, item) if item else self._send(404, {"status": 404, "message": "item not found"})
            return self._send(404, {"status": 404, "message": "not found"})

        def _send(self, status, body, headers=None):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            try:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass  # the client gave up (timeout test); nothing to answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def connect(monkeypatch):
    """Loopback Connect server wired into the profile's secrets the way a deployment provides them."""
    for name in ("OP_SERVICE_ACCOUNT_TOKEN", *_PROXY_ENV):
        monkeypatch.delenv(name, raising=False)
    state = _connect_state()
    server = _serve(state)
    monkeypatch.setenv("OP_CONNECT_HOST", _url(server))
    monkeypatch.setenv("OP_CONNECT_TOKEN", TOKEN)
    yield state
    if state["hold"] is not None:
        state["hold"].set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def bystander():
    """A second loopback server that must never be contacted (proxy or redirect target)."""
    state = _connect_state()
    server = _serve(state)
    yield _url(server), state
    server.shutdown()
    server.server_close()


def _backend(**cfg):
    from agent.vault_backends.onepassword import OnePasswordLoginBackend
    return OnePasswordLoginBackend(cfg)


def _item_reads(state):
    return [path for path, _ in state["log"] if path.count("/") == 5]


def test_connect_is_headless_and_lists_only_scoped_login_items(connect):
    backend = _backend(vaults=["agent-own"])
    assert backend.is_unlocked()
    metas = {m.id: m for m in backend.list_items()}
    assert set(metas) == {f"op:{LOGIN}"}  # no admin login, no note, no item without a website
    meta = metas[f"op:{LOGIN}"]
    assert meta.identifier == "jane" and meta.identifier_type == "username"
    assert list(meta.allowed_origins) == ["https://example.com", "https://www.example.com"]  # schemeless → https
    assert meta.has_otp is False  # knowing would mean reading every full item just to list
    assert f"/v1/vaults/{ADMIN}/items" not in [path for path, _ in connect["log"]]
    assert _item_reads(connect) == []  # listing reads summaries only, never a secret
    # the same vault named by id; and no selector lists every visible vault
    assert {m.id for m in _backend(vaults=[OWN]).list_items()} == {f"op:{LOGIN}"}
    assert {m.id for m in _backend().list_items()} == {f"op:{LOGIN}", f"op:{ADMIN_LOGIN}"}


def test_vault_selector_fails_closed(connect):
    with pytest.raises(RuntimeError, match="not visible to this Connect token"):
        _backend(vaults=["agent-own", "agent-typo"]).list_items()
    with pytest.raises(RuntimeError, match="list of vault names"):
        _backend(vaults="agent-own").list_items()
    connect["vaults"].append({"id": "thirdvaultid000000000000cc", "name": "agent-own"})
    with pytest.raises(RuntimeError, match="names more than one vault"):
        _backend(vaults=["agent-own"]).list_items()
    connect["vaults"][-1] = {"id": "thirdvaultid000000000000cc", "name": OWN}  # a name that is another vault's id
    with pytest.raises(RuntimeError, match="names more than one vault"):
        _backend(vaults=[OWN]).list_items()
    assert [path for path, _ in connect["log"]] == ["/v1/vaults"] * 3  # nothing was listed, nothing widened


def test_summary_from_another_vault_is_not_adopted(connect):
    connect["summaries"][OWN].append(dict(connect["summaries"][ADMIN][0]))  # Connect says it lives in ADMIN
    assert {m.id for m in _backend(vaults=["agent-own"]).list_items()} == {f"op:{LOGIN}"}


def test_fill_reads_the_login_password_field_of_a_listed_item_only(connect):
    backend = _backend(vaults=["agent-own"])
    assert backend.resolve_password(f"op:{LOGIN}") == PASSWORD
    assert totp_now(normalize_otp_secret(SEED)) == backend.resolve_otp(f"op:{LOGIN}")  # broken seed skipped
    connect["log"].clear()
    for handle in (f"op:{ADMIN_LOGIN}", f"op:{NOTE}", f"op:{SECOND}", "op:../../v1/vaults", "op:ghost"):
        with pytest.raises(RuntimeError, match="not found"):
            _backend(vaults=["agent-own"]).resolve_password(handle)
        assert _backend(vaults=["agent-own"]).resolve_otp(handle) is None
    assert _item_reads(connect) == []  # every refusal happened before an item request


def test_item_changed_since_listing_is_refused(connect):
    backend = _backend()
    assert backend.get_meta(f"op:{LOGIN}") is not None  # what the fill was bound to
    connect["items"][(OWN, LOGIN)]["urls"] = [{"href": "https://example.com/login"}]  # www. binding removed
    with pytest.raises(RuntimeError, match="changed since it was listed"):
        backend.resolve_password(f"op:{LOGIN}")
    assert backend.resolve_otp(f"op:{LOGIN}") is None
    connect["items"][(OWN, LOGIN)] = dict(connect["items"][(OWN, LOGIN)], urls=WEBSITES, id=SECOND)
    with pytest.raises(RuntimeError, match="different item"):
        _backend().resolve_password(f"op:{LOGIN}")
    connect["items"][(OWN, LOGIN)] = dict(connect["items"][(OWN, LOGIN)], id=LOGIN, category="PASSWORD")
    with pytest.raises(RuntimeError, match="no longer a login"):
        _backend().resolve_password(f"op:{LOGIN}")
    connect["items"][(OWN, LOGIN)] = {"id": LOGIN, "category": "LOGIN", "vault": {"id": OWN}, "urls": WEBSITES,
                                      "fields": [{"id": "c", "label": "password", "type": "CONCEALED", "value": "decoy"}]}
    with pytest.raises(RuntimeError, match="no saved password"):
        _backend().resolve_password(f"op:{LOGIN}")
    assert _backend().resolve_otp(f"op:{LOGIN}") is None


def test_connect_computed_code_is_the_fallback_for_an_unreadable_seed(connect):
    connect["items"][(OWN, LOGIN)]["fields"] = [{"id": "otp", "type": "OTP", "value": "", "totp": "246810"}]
    assert _backend().resolve_otp(f"op:{LOGIN}") == "246810"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused_and_never_carry_the_token(connect, bystander, status):
    target, seen = bystander
    connect["override"]["/v1/vaults"] = (status, {"Location": f"{target}/v1/vaults"}, {})
    with pytest.raises(RuntimeError, match="redirect") as raised:
        _backend().list_items()
    assert seen["log"] == []
    assert TOKEN not in str(raised.value) and target not in str(raised.value)


def test_proxy_environment_never_sees_the_token(connect, bystander, monkeypatch):
    proxy, seen = bystander
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy)
    assert [m.id for m in _backend(vaults=["agent-own"]).list_items()] == [f"op:{LOGIN}"]
    assert seen["log"] == []


def test_unusable_answers_become_fixed_errors(connect):
    import os

    from agent.vault_backends.onepassword_connect import ConnectClient

    connect["override"]["/v1/vaults"] = (200, {}, b"<html>proxy login page</html>")
    with pytest.raises(RuntimeError, match="not JSON"):
        _backend().list_items()
    connect["override"]["/v1/vaults"] = (200, {}, b"[" + b" " * (9 * 1024 * 1024) + b"]")
    with pytest.raises(RuntimeError, match="oversized"):
        _backend().list_items()
    connect["override"].clear()
    connect["hold"] = threading.Event()
    client = ConnectClient.parse(os.environ["OP_CONNECT_HOST"], TOKEN, timeout=0.5)
    with pytest.raises(RuntimeError, match="did not answer"):
        client.vaults()


def test_rejected_token_and_half_configuration_are_errors_not_unlock_prompts(connect, monkeypatch):
    from agent.vault_backends.onepassword import OnePasswordLoginBackend

    monkeypatch.setenv("OP_CONNECT_TOKEN", "a-revoked-token")
    with pytest.raises(RuntimeError, match="rejected the token") as raised:
        _backend().list_items()
    assert "a-revoked-token" not in str(raised.value)
    monkeypatch.delenv("OP_CONNECT_TOKEN")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "service-account-for-tests")  # never a silent fallback
    backend = _backend()
    assert backend.is_unlocked()  # nothing to prompt for: the configuration is what is wrong
    with patch.object(OnePasswordLoginBackend, "_run", side_effect=AssertionError("op CLI must not run")):
        with pytest.raises(RuntimeError, match="needs both OP_CONNECT_HOST and OP_CONNECT_TOKEN"):
            backend.list_items()
        with pytest.raises(RuntimeError, match="needs both OP_CONNECT_HOST and OP_CONNECT_TOKEN"):
            backend.resolve_password(f"op:{LOGIN}")


def test_connect_outranks_a_service_account_token_like_op_does(connect, monkeypatch):
    from agent.vault_backends.onepassword import OnePasswordLoginBackend

    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "service-account-for-tests")
    with patch.object(OnePasswordLoginBackend, "_run", side_effect=AssertionError("op CLI must not run")):
        backend = _backend(vaults=["agent-own"])
        assert [m.id for m in backend.list_items()] == [f"op:{LOGIN}"]
        assert backend.resolve_password(f"op:{LOGIN}") == PASSWORD


def test_connect_credentials_come_from_the_profile_scope(connect, monkeypatch):
    """Under multiplexing each profile sees only its own Connect server; the launch environment's
    Connect credentials are never borrowed by a profile that has none."""
    import os

    from agent import secret_scope as ss

    host = os.environ["OP_CONNECT_HOST"]
    monkeypatch.setenv("OP_CONNECT_HOST", "http://launch-profile.invalid:8080")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "launch-profile-token")
    ss.set_multiplex_active(True)
    try:
        own = ss.set_secret_scope({"OP_CONNECT_HOST": host, "OP_CONNECT_TOKEN": TOKEN})
        try:
            assert [m.id for m in _backend(vaults=["agent-own"]).list_items()] == [f"op:{LOGIN}"]
        finally:
            ss.reset_secret_scope(own)
        other = ss.set_secret_scope({})
        try:
            backend = _backend()
            assert not backend.is_unlocked() and backend.list_items() == []
        finally:
            ss.reset_secret_scope(other)
    finally:
        ss.set_multiplex_active(False)
    assert all(ok for _, ok in connect["log"])


def test_cli_vault_selector_lists_each_configured_vault(monkeypatch):
    """Without Connect the same selector scopes `op item list`; with none configured the listing is
    exactly the unscoped call it always was."""
    from agent.vault_backends.onepassword import OnePasswordLoginBackend

    for name in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "service-account-for-tests")
    listing = json.dumps([{"id": "cli-item", "title": "Shop", "vault": {"id": "v1"}, "urls": [{"href": "shop.example.com"}]}])
    for cfg, expected in (({"vaults": ["Personal", "Work"]}, [("--vault", "Personal"), ("--vault", "Work")]), ({}, [()])):
        backend = OnePasswordLoginBackend(cfg)
        with patch.object(backend, "_run", return_value=listing) as run:
            metas = backend.list_items()
        assert [call.args[6:] for call in run.call_args_list] == expected
        assert [call.args[:6] for call in run.call_args_list] == [("item", "list", "--categories", "Login", "--format", "json")] * len(expected)
        assert metas[0].origin == "https://shop.example.com"


def test_only_a_plain_dns_name_is_read_as_a_schemeless_website():
    from agent.vault_backends.onepassword import _all_origins

    assert _all_origins(["github.com/login", "Sub.Example.co.uk:8443/x?y"]) == [
        "https://github.com", "https://sub.example.co.uk:8443"]
    assert _all_origins(["localhost:8080", "mailto:jane@example.com", "javascript:alert(1)", "jane@example.com",
                         "jane@example.com/login", "10.0.0.1:8080", "  ", "http//example.com"]) == []


def test_connect_profile_enables_1password_from_config_without_the_op_binary(connect, monkeypatch):
    """vault.onepassword.vaults is read from the profile's config.yaml by the real loader, and a
    Connect-only host (no `op` on PATH) still gets the source."""
    from agent.vault_backends import base
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("PATH", "")
    (get_hermes_home() / "config.yaml").write_text(
        "vault:\n  onepassword:\n    vaults: [agent-own]\n", encoding="utf-8")
    assert base.is_installed("onepassword")
    onepassword = next(b for b in base.enabled_backends() if b.name == "onepassword")
    assert [m.id for m in onepassword.list_items()] == [f"op:{LOGIN}"]


def test_browser_vault_list_and_fill_through_connect(connect):
    """The model-facing tool: listed without a lock, filled into the page, and neither the token nor
    the password comes back in a tool result."""
    from hermes_constants import get_hermes_home
    from tools import browser_vault_tool
    from tools.registry import registry

    (get_hermes_home() / "config.yaml").write_text(
        "vault:\n  onepassword:\n    vaults: [agent-own]\n", encoding="utf-8")
    listed = registry.dispatch("browser_vault", {"action": "list"})
    out = json.loads(listed)
    assert "locked" not in out and "errors" not in out
    assert [i["handle"] for i in out["items"] if i["backend"] == "onepassword"] == [f"op:{LOGIN}"]

    controls = [{"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "user", "type": "text"},
                {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"}]
    written = []

    def fake_eval(task_id, expression):
        if "location.href" in expression:
            return {"success": True, "result": "https://www.example.com/sso"}
        return {"success": True, "result": json.dumps(controls)}

    def fake_eval_secret(task_id, expression):
        written.append(expression)
        return {"success": True, "result": json.dumps({"filled": 1})}

    with patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret):
        filled = browser_vault_tool.browser_vault_fill(f"op:{LOGIN}")
    result = json.loads(filled)
    assert result["success"] and result["backend"] == "onepassword" and result["origin"] == "https://www.example.com"
    assert len(written) == 1 and PASSWORD in written[0]
    for text in (listed, filled):
        assert PASSWORD not in text and TOKEN not in text


def test_tool_errors_never_carry_the_token(connect, monkeypatch):
    from tools.registry import registry

    monkeypatch.setenv("OP_CONNECT_TOKEN", "a-revoked-token")
    raw = registry.dispatch("browser_vault", {"action": "list"})
    assert "rejected the token" in raw and "a-revoked-token" not in raw
