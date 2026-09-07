"""Capability-bound, durable private follow-up continuation.

This module intentionally knows nothing about external providers or business objects.  A
plugin supplies opaque binding values and re-authorizes its own requester before every
prompt.  The core validates the signed/revocable capability, bounds the durable queue,
and dispatches only a canonical internal ``MessageEvent`` through the normal handler.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class ContinuationDenied(PermissionError):
    """A capability, binding, authorization, or readiness fence was not satisfied."""


def _canonical(value: dict[str, str]) -> str:
    if not value or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v for k, v in value.items()):
        raise ValueError("continuation binding must contain non-empty string values")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _pack(payload: dict[str, object], secret: bytes) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def _unpack(token: str, secret: bytes) -> dict[str, object]:
    try:
        body, encoded_sig = token.split(".", 1)
        supplied = base64.urlsafe_b64decode(encoded_sig + "=" * (-len(encoded_sig) % 4))
        expected = hmac.new(secret, body.encode(), hashlib.sha256).digest()
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
    except Exception as exc:  # malformed scopes are never diagnostic to callers
        raise ContinuationDenied("invalid continuation capability") from exc
    if not hmac.compare_digest(supplied, expected) or not isinstance(payload, dict):
        raise ContinuationDenied("invalid continuation capability")
    return payload


class PrivateContinuationStore:
    """Profile-local continuation queue; queue rows never expose prompt text in its API."""

    def __init__(self, database: Path, *, secret: bytes, max_per_owner: int = 10, now: Callable[[], float] = time.time) -> None:
        if len(secret) < 32 or max_per_owner < 1:
            raise ValueError("continuation secret must be at least 32 bytes and queue bound positive")
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database, self.secret, self.max_per_owner, self.now = database, secret, max_per_owner, now
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS private_continuation_scopes (
              nonce TEXT PRIMARY KEY, binding TEXT NOT NULL, owner_generation TEXT NOT NULL,
              expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS private_continuation_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL, binding TEXT NOT NULL,
              owner_key TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued'
            );
            """)

    def _connect(self):
        return sqlite3.connect(self.database, isolation_level=None, timeout=5)

    def mint(self, *, binding: dict[str, str], owner_generation: str, ttl_seconds: int) -> str:
        if not isinstance(owner_generation, str) or not owner_generation or ttl_seconds < 1:
            raise ValueError("owner_generation and positive ttl_seconds are required")
        canonical = _canonical(binding)
        nonce = secrets.token_urlsafe(24)
        expires = int(self.now()) + ttl_seconds
        with self._connect() as conn:
            conn.execute("INSERT INTO private_continuation_scopes VALUES (?, ?, ?, ?, 0)", (nonce, canonical, owner_generation, expires))
        return _pack({"v": 1, "n": nonce, "b": canonical, "g": owner_generation, "e": expires}, self.secret)

    def revoke(self, token: str) -> None:
        payload = _unpack(token, self.secret)
        with self._connect() as conn:
            conn.execute("UPDATE private_continuation_scopes SET revoked = 1 WHERE nonce = ?", (payload.get("n"),))

    def enqueue(self, *, token: str, binding: dict[str, str], owner_generation: str, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("continuation prompt is required")
        canonical = _canonical(binding)
        payload = _unpack(token, self.secret)
        if payload.get("v") != 1 or payload.get("b") != canonical or payload.get("g") != owner_generation:
            raise ContinuationDenied("continuation capability binding mismatch")
        nonce, expires = payload.get("n"), payload.get("e")
        if not isinstance(nonce, str) or type(expires) is not int or expires < int(self.now()):
            raise ContinuationDenied("continuation capability expired")
        owner_key = binding.get("owner")
        if not owner_key:
            raise ValueError("continuation binding requires owner")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT binding, owner_generation, expires_at, revoked FROM private_continuation_scopes WHERE nonce = ?", (nonce,)).fetchone()
            if row is None or row[0] != canonical or row[1] != owner_generation or int(row[2]) < int(self.now()) or int(row[3]):
                conn.execute("ROLLBACK")
                raise ContinuationDenied("continuation capability is unavailable")
            count = conn.execute("SELECT COUNT(*) FROM private_continuation_queue WHERE owner_key = ? AND state = 'queued'", (owner_key,)).fetchone()[0]
            if count >= self.max_per_owner:
                conn.execute("ROLLBACK")
                raise ContinuationDenied("continuation owner queue is full")
            conn.execute("INSERT INTO private_continuation_queue (nonce,binding,owner_key,prompt) VALUES (?,?,?,?)", (nonce, canonical, owner_key, prompt))
            conn.execute("COMMIT")

    def take(self, *, owner_key: str) -> tuple[int, str] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, prompt FROM private_continuation_queue WHERE owner_key = ? AND state = 'queued' ORDER BY id LIMIT 1", (owner_key,)).fetchone()
            if row is None:
                conn.execute("COMMIT"); return None
            conn.execute("UPDATE private_continuation_queue SET state = 'running' WHERE id = ?", (row[0],))
            conn.execute("COMMIT")
            return int(row[0]), str(row[1])

    def finish(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM private_continuation_queue WHERE id = ? AND state = 'running'", (item_id,))


class GatewayPrivateContinuationMixin:
    """Gateway ABI used by plugins after their per-prompt authorization and KEN-446 gate."""

    def configure_private_continuations(self, *, database: Path, secret: bytes, max_per_owner: int = 10) -> None:
        self._private_continuations = PrivateContinuationStore(database, secret=secret, max_per_owner=max_per_owner)

    def mint_private_continuation(self, *, binding: dict[str, str], owner_generation: str, ttl_seconds: int) -> str:
        return self._private_continuations.mint(binding=binding, owner_generation=owner_generation, ttl_seconds=ttl_seconds)

    async def enqueue_private_continuation(self, *, token: str, binding: dict[str, str], owner_generation: str, prompt: str, authorize: Callable[[], bool]) -> None:
        if not callable(authorize) or authorize() is not True:
            raise ContinuationDenied("continuation requester is not authorized")
        self._private_continuations.enqueue(token=token, binding=binding, owner_generation=owner_generation, prompt=prompt)

    async def drain_private_continuation(self, *, owner_key: str, make_event: Callable[[str], Any]) -> bool:
        item = self._private_continuations.take(owner_key=owner_key)
        if item is None:
            return False
        item_id, prompt = item
        try:
            event = make_event(prompt)
            # This is the normal inbound session path, not a mid-turn injection.
            await self.dispatch_internal_plugin_event(event)
        except Exception:
            raise
        else:
            self._private_continuations.finish(item_id)
            return True
