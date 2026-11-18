"""1Password Login items as a vault backend.

Two transports read the same Login items; the backend picks one at construction:

- ``op`` CLI (``_CliTransport``). Unlock: ``op signin --raw`` with the master password on stdin
  (desktop-app integration or account-level auth) mints an ``OP_SESSION_<account>`` token. A
  configured service-account token skips the prompt entirely (headless). List: ``op item list
  --categories Login --format json`` → title, urls, username, vault. Resolve: ``op item get <id>
  --vault <vault-id> ...`` (service accounts reject an item read without a vault query).
- 1Password Connect (``_ConnectTransport``), selected by ``OP_CONNECT_HOST`` / ``OP_CONNECT_TOKEN``
  in the profile's secrets. Headless like a service account, and ``op`` itself prefers Connect over
  a service-account token. ``op item list`` refuses to run against Connect, so the Connect REST API
  is read directly (``onepassword_connect.py``).

``vault.onepassword.vaults`` (names or ids) limits either transport to those vaults; empty means
every vault the identity can see. A fill or code read is only ever for an item the current, scoped
listing returned.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_backends.onepassword_connect import ConnectClient
from agent.vault_store import VaultError, VaultItemMeta, normalize_origin, normalize_otp_secret, totp_now

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_CONNECT_ENV = ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN")
# A website saved without a scheme: a dotted DNS name with an alphabetic TLD, an optional port and
# path. Anything else without "://" (``localhost:8080``, ``mailto:``, ``user@host``) is not bound.
_BARE_WEBSITE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?(?::\d{1,5})?(?:[/?#][^\s@]*)?", re.IGNORECASE)


def connect_configured() -> bool:
    """The profile's secrets name a 1Password Connect server. Half a configuration counts: it is an
    error to report, not a reason to fall back to the CLI or an unlock prompt."""
    from agent.secret_scope import get_secret
    return any(get_secret(name, "") for name in _CONNECT_ENV)


@dataclass(frozen=True)
class _LoginSummary:
    """One listed item, whichever transport listed it. Metadata only."""

    item_id: str
    vault_id: str
    title: str
    hrefs: tuple
    username: str
    created_at: str


class _CliTransport:
    """Reads through the ``op`` CLI, authenticated by the backend's session or service account."""

    def __init__(self, backend: OnePasswordLoginBackend):
        self._backend = backend

    def summaries(self, vaults: tuple) -> list[_LoginSummary]:
        base = ("item", "list", "--categories", "Login", "--format", "json")
        out: list[_LoginSummary] = []
        for vault in vaults or (None,):
            raw = json.loads(self._backend._run(*base, *(("--vault", vault) if vault else ())) or "[]")
            for item in raw if isinstance(raw, list) else []:
                vault_ref = item.get("vault") or {}
                out.append(_summary(item, vault_ref.get("id") if isinstance(vault_ref, dict) else "",
                                    username=item.get("additional_information"), created=item.get("created_at")))
        return out

    def password(self, item_id: str, vault_id: str, bound: tuple) -> str:
        # `op item get --fields` returns no websites, so the listing's binding is the guard here.
        return self._backend._run("item", "get", *_selector(item_id, vault_id),
                                  "--fields", "label=password", "--reveal").rstrip("\r\n")

    def otp(self, item_id: str, vault_id: str, bound: tuple) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        code = self._backend._run("item", "get", *_selector(item_id, vault_id), "--otp").strip()
        return code if code.isdigit() else None


class _ConnectTransport:
    """Reads the 1Password Connect REST API."""

    def __init__(self, client: ConnectClient):
        self._client = client

    def summaries(self, vaults: tuple) -> list[_LoginSummary]:
        out: list[_LoginSummary] = []
        for vault_id in _selected_vault_ids(self._client.vaults(), vaults):
            for item in self._client.items(vault_id):
                listed_in = item.get("vault")
                if item.get("category") != "LOGIN" or (isinstance(listed_in, dict) and listed_in.get("id") != vault_id):
                    continue
                out.append(_summary(item, vault_id, username=item.get("additionalInformation"),
                                    created=item.get("createdAt")))
        return out

    def _login(self, item_id: str, vault_id: str, bound: tuple) -> dict[str, Any]:
        """The full item, still a login, still saved for every website the fill was bound to: an item
        edited between the listing and the fill is refused rather than filled on a stale binding."""
        item = self._client.item(vault_id, item_id)
        if item.get("category") != "LOGIN":
            raise RuntimeError("1Password item is no longer a login; list logins again")
        saved = _all_origins([str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")])
        if not bound or not set(bound) <= set(_web_origins(saved) if saved else ()):
            raise RuntimeError("1Password login changed since it was listed; list logins again")
        return item

    def password(self, item_id: str, vault_id: str, bound: tuple) -> str:
        """The login's own password: the ``PASSWORD``-purpose field, never another concealed field."""
        for f in self._login(item_id, vault_id, bound).get("fields") or []:
            if isinstance(f, dict) and f.get("purpose") == "PASSWORD" and isinstance(f.get("value"), str) and f["value"]:
                return f["value"]
        raise RuntimeError("1Password login has no saved password")

    def otp(self, item_id: str, vault_id: str, bound: tuple) -> Optional[str]:
        """Current code from the first usable one-time-password field. A field whose seed cannot be read
        never hides a later usable one; Connect's own computed ``totp`` is that field's fallback."""
        for f in self._login(item_id, vault_id, bound).get("fields") or []:
            if not isinstance(f, dict) or f.get("type") != "OTP":
                continue
            try:
                seed = normalize_otp_secret(str(f.get("value") or ""))
            except VaultError:
                seed = ""
            if seed:
                return totp_now(seed)
            code = str(f.get("totp") or "")
            if code.isdigit() and 6 <= len(code) <= 8:
                return code
        return None


class _Misconfigured:
    """Stands in for a transport the profile's settings cannot build; every use reports why."""

    def __init__(self, reason: str):
        self.reason = reason

    def summaries(self, vaults: tuple) -> list[_LoginSummary]:
        raise RuntimeError(self.reason)

    def password(self, item_id: str, vault_id: str, bound: tuple) -> str:
        raise RuntimeError(self.reason)

    def otp(self, item_id: str, vault_id: str, bound: tuple) -> Optional[str]:
        raise RuntimeError(self.reason)


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True  # a Connect or service-account identity simply reports itself unlocked

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        # item id -> (vault id, bound web origins) from the latest scoped listing
        self._listed_items: dict[str, tuple[str, tuple]] = {}
        self._uses_connect = connect_configured()
        # A configuration problem is reported when the backend is used (listing shows it under
        # `errors`); raising here would take every other login source down with this one.
        vaults = _parse_vault_selector(self.cfg.get("vaults"))
        self._vaults: tuple = vaults or ()
        if vaults is None:
            self._transport: Any = _Misconfigured("vault.onepassword.vaults must be a list of vault names or ids")
        elif self._uses_connect:
            try:
                self._transport = _ConnectTransport(ConnectClient.parse(
                    get_secret("OP_CONNECT_HOST", "") or "", get_secret("OP_CONNECT_TOKEN", "") or "",
                    timeout=_TIMEOUT))
            except ValueError as exc:
                self._transport = _Misconfigured(str(exc))
        else:
            self._transport = _CliTransport(self)

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in _CONNECT_ENV:
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        # Connect and service accounts are headless identities: there is no session to unlock.
        return self._uses_connect or bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            # A service account never holds a session, so an auth failure there is not a lock to undo:
            # surface op's own error instead of an unlock prompt that reports "already unlocked".
            if not self._service_token and any(w in err.lower() for w in ("session", "sign in", "not signed in")):
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ────────────────────────────────────────────────────

    def list_items(self) -> list[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        summaries = self._transport.summaries(self._vaults)
        out: list[VaultItemMeta] = []
        self._listed_items = {}
        for s in summaries:
            origins = _all_origins(list(s.hrefs))
            if s.item_id:
                self._listed_items[s.item_id] = (s.vault_id, _web_origins(origins) if origins else ())
            if not origins:
                continue
            out.append(VaultItemMeta(
                id=f"{self.prefix}{s.item_id}", kind="login", label=s.title or origins[0],
                origin=origins[0], created_at=s.created_at,
                identifier_type="username" if s.username else None, identifier=s.username or None,
                allowed_origins=_web_origins(origins)))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def _listed(self, handle: str) -> tuple[str, str, tuple]:
        """``(item id, vault id, bound origins)`` for a handle the current scoped listing returned. A
        handle nothing listed is refused before any secret read; the vault comes from that same
        listing, because a service account and Connect both have to name it."""
        item_id = handle[len(self.prefix):]
        if item_id not in self._listed_items:
            self.list_items()
        vault_id, bound = self._listed_items.get(item_id, ("", ()))
        if not bound:  # never listed, or listed without a website it could be filled on
            raise RuntimeError("1Password login not found; list logins again")
        return item_id, vault_id, bound

    def resolve_password(self, handle: str) -> str:
        return self._transport.password(*self._listed(handle))

    def resolve_otp(self, handle: str) -> Optional[str]:
        # No automatic code (locked, unlisted, unreadable, op or Connect refused): the user is asked.
        try:
            return self._transport.otp(*self._listed(handle))
        except (UnlockRequired, RuntimeError, OSError, ValueError):
            return None


def _selector(item_id: str, vault_id: str) -> list[str]:
    """``op item get`` target for a listed login."""
    return [item_id, "--vault", vault_id] if vault_id else [item_id]


def _parse_vault_selector(value: Any) -> Optional[tuple]:
    """``vault.onepassword.vaults`` as a tuple of names/ids; ``None`` when it is not a list of strings."""
    if value is None or value == []:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    return tuple(dict.fromkeys(v.strip() for v in value))


def _selected_vault_ids(vaults: list[dict[str, Any]], selector: tuple) -> list[str]:
    """Ids of the Connect vaults to list. Each configured name/id must name exactly one vault this
    token can see: a typo or an ambiguous name fails closed instead of listing nothing, the wrong
    vault or, worse, everything."""
    visible = [(str(v.get("id") or ""), str(v.get("name") or "")) for v in vaults if v.get("id")]
    if not selector:
        return [vid for vid, _ in visible]
    chosen: list[str] = []
    for wanted in selector:
        match = [vid for vid, name in visible if wanted in (vid, name)]
        if len(match) != 1:
            problem = "is not visible to this Connect token" if not match else "names more than one vault"
            raise RuntimeError(f"1Password vault {wanted!r} (vault.onepassword.vaults) {problem}")
        if match[0] not in chosen:
            chosen.append(match[0])
    return chosen


def _summary(item: dict[str, Any], vault_id: Any, *, username: Any, created: Any) -> _LoginSummary:
    hrefs = tuple(str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href"))
    return _LoginSummary(item_id=str(item.get("id") or ""), vault_id=str(vault_id or ""),
                         title=str(item.get("title") or ""), hrefs=hrefs,
                         username=str(username or "").strip(), created_at=str(created or ""))


def _web_origins(origins: list[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _website_url(value: str) -> str:
    """A saved website as a URL. 1Password keeps a website exactly as typed and opens a schemeless
    ``github.com/login`` as ``https://github.com/login``; only a plain DNS name gets that reading."""
    value = value.strip()
    if "://" in value:
        return value
    return f"https://{value}" if _BARE_WEBSITE.fullmatch(value) else ""


def _all_origins(urls: list[str]) -> list[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: list[str] = []
    for u in urls:
        try:
            origin = normalize_origin(_website_url(u))
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
