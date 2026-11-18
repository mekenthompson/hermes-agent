"""1Password Connect REST client for the 1Password login backend.

A Connect server (``OP_CONNECT_HOST`` + ``OP_CONNECT_TOKEN``) serves the same vault items as the
``op`` CLI, but ``op item list`` refuses to run against Connect, so the login backend reads the
Connect API directly. Read-only (GET). The bearer token travels in the request header only: never
in a URL, argv, log line or error message, never through a proxy and never to a redirect target.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# 1Password vault and item ids are 26 lowercase base32 characters; anything outside this alphabet
# could add path segments or a query to the request URL, so it is refused before any request.
_ID = re.compile(r"[A-Za-z0-9]{1,64}")
# A vault listing is a few KB per hundred items; anything this large is not a Connect answer.
_MAX_BODY = 8 * 1024 * 1024


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A followed redirect would re-send the bearer token to the new location: surface the 3xx instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ConnectClient:
    base_url: str
    token: str = field(repr=False)
    timeout: float = 30.0

    @classmethod
    def parse(cls, host: str, token: str, *, timeout: float) -> ConnectClient:
        """Validate the profile's Connect settings. Raises ``ValueError`` naming the problem (never the token)."""
        host, token = (host or "").strip(), (token or "").strip()
        if not host or not token:
            raise ValueError("1Password Connect needs both OP_CONNECT_HOST and OP_CONNECT_TOKEN")
        parts = urllib.parse.urlsplit(host)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("OP_CONNECT_HOST must be an http(s) URL such as http://connect:8080")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("OP_CONNECT_HOST must not carry credentials, a query or a fragment")
        return cls(base_url=host.rstrip("/"), token=token, timeout=timeout)

    def vaults(self) -> list[dict[str, Any]]:
        return _as_list(self._get("/v1/vaults"))

    def items(self, vault_id: str) -> list[dict[str, Any]]:
        """Item summaries (no field values) of one vault."""
        return _as_list(self._get(f"/v1/vaults/{_segment(vault_id)}/items"))

    def item(self, vault_id: str, item_id: str) -> dict[str, Any]:
        """One full item, refused unless Connect answered for exactly the requested vault and item."""
        got = self._get(f"/v1/vaults/{_segment(vault_id)}/items/{_segment(item_id)}")
        vault = got.get("vault") if isinstance(got, dict) else None
        if (not isinstance(got, dict) or got.get("id") != item_id
                or not isinstance(vault, dict) or vault.get("id") != vault_id):
            raise RuntimeError("1Password Connect returned a different item than requested")
        return got

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(
            self.base_url + path, method="GET",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        # No proxy: the process environment's proxy belongs to whoever launched it (another profile
        # under multiplexing), and on plain-http Connect it would read the bearer token.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                body = response.read(_MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(_http_error(exc)) from None
        except TimeoutError:
            raise RuntimeError(f"1Password Connect did not answer within {self.timeout:g}s") from None
        except OSError as exc:  # URLError, refused connection, TLS failure
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(f"1Password Connect is unreachable ({type(reason).__name__})") from None
        if len(body) > _MAX_BODY:
            raise RuntimeError("1Password Connect returned an oversized response")
        try:
            return json.loads(body or b"null")
        except ValueError:
            raise RuntimeError("1Password Connect returned a response that is not JSON") from None


def _segment(value: str) -> str:
    if not _ID.fullmatch(value or ""):
        raise RuntimeError("1Password login not found; list logins again")
    return value


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _http_error(exc: urllib.error.HTTPError) -> str:
    if 300 <= exc.code < 400:
        return f"1Password Connect answered with a redirect (HTTP {exc.code}); refusing to follow it"
    if exc.code in (401, 403):
        return f"1Password Connect rejected the token (HTTP {exc.code})"
    message = ""
    try:
        payload = json.loads(exc.read(64 * 1024) or b"null")
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
    except (ValueError, OSError):
        pass  # the status alone still names the failure
    return f"1Password Connect error (HTTP {exc.code})" + (f": {message[:160]}" if message else "")
