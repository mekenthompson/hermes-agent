"""Capability-bound, durable private follow-up continuation.

Plugins bind opaque business identity to a signed scope.  Core owns the dangerous part:
resolving the existing private owner route, checking the physical turn slot, and dispatching
only to that route.  A continuation never constructs a provider session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContinuationDenied(PermissionError):
    """A capability, binding, authorization, or readiness fence was not satisfied."""


@dataclass(frozen=True)
class PrivateContinuationExecution:
    """The exact core execution reserved for a private continuation."""

    execution_id: str
    session_key: str
    generation: int


class _PrivateContinuationGrant:
    """Unforgeable, one-shot admission held only by the GatewayRunner.

    This deliberately is not an event attribute or a serializable capability.  The
    normal handler admits a provider route only when it receives this exact object
    from the runner's private grant registry.
    """

    __slots__ = ("event", "source", "session_key", "generation", "execution_id", "item_id", "used")

    def __init__(self, event: Any, source: Any, session_key: str, generation: int, execution_id: str,
                 item_id: int | None = None) -> None:
        self.event, self.source, self.session_key = event, source, session_key
        self.generation, self.execution_id, self.item_id, self.used = generation, execution_id, item_id, False


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
    except Exception as exc:
        raise ContinuationDenied("invalid continuation capability") from exc
    if not hmac.compare_digest(supplied, expected) or not isinstance(payload, dict):
        raise ContinuationDenied("invalid continuation capability")
    return payload


class PrivateContinuationStore:
    """Profile-local continuation queue; every take revalidates the durable scope."""

    def __init__(self, database: Path, *, secret: bytes, max_per_owner: int = 10, now: Callable[[], float] = time.time) -> None:
        if len(secret) < 32 or max_per_owner < 1:
            raise ValueError("continuation secret must be at least 32 bytes and queue bound positive")
        self.database = self._validate_database_path(Path(database))
        self.secret, self.max_per_owner, self.now = secret, max_per_owner, now
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS private_continuation_scopes (
              nonce TEXT PRIMARY KEY, binding TEXT NOT NULL, owner_generation TEXT NOT NULL,
              expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS private_continuation_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL, binding TEXT NOT NULL,
              owner_generation TEXT NOT NULL, owner_key TEXT NOT NULL, prompt TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'queued'
            );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(private_continuation_queue)")}
            if "owner_generation" not in columns:
                raise RuntimeError("private continuation queue schema is incompatible")

    @staticmethod
    def _validate_database_path(database: Path) -> Path:
        """Accept only a user-owned private regular SQLite path; never repair unsafe state."""
        database = database.absolute()
        try:
            parent = database.parent.lstat()
        except OSError as exc:
            raise RuntimeError("private continuation database directory is unavailable") from exc
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700):
            raise RuntimeError("private continuation database directory is unsafe")
        try:
            info = database.lstat()
        except FileNotFoundError:
            return database
        except OSError as exc:
            raise RuntimeError("private continuation database is unavailable") from exc
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("private continuation database is unsafe")
        return database

    def _connect(self):
        new_database = not self.database.exists()
        if not new_database:
            self._validate_database_path(self.database)
        conn = sqlite3.connect(self.database, isolation_level=None, timeout=5)
        try:
            if new_database:
                os.chmod(self.database, 0o600)
            self._validate_database_path(self.database)
        except Exception:
            conn.close()
            raise
        return conn

    def mint(self, *, binding: dict[str, str], owner_generation: str, ttl_seconds: int) -> str:
        if not isinstance(owner_generation, str) or not owner_generation or type(ttl_seconds) is not int or ttl_seconds < 1:
            raise ValueError("owner_generation and positive integer ttl_seconds are required")
        canonical, nonce = _canonical(binding), secrets.token_urlsafe(24)
        expires = int(self.now()) + ttl_seconds
        with self._connect() as conn:
            conn.execute("INSERT INTO private_continuation_scopes VALUES (?, ?, ?, ?, 0)", (nonce, canonical, owner_generation, expires))
        return _pack({"v": 1, "n": nonce, "b": canonical, "g": owner_generation, "e": expires}, self.secret)

    def revoke(self, token: str) -> None:
        payload = _unpack(token, self.secret)
        with self._connect() as conn:
            conn.execute("UPDATE private_continuation_scopes SET revoked = 1 WHERE nonce = ?", (payload.get("n"),))

    def _validate_payload(self, *, token: str, canonical: str, owner_generation: str) -> tuple[str, int]:
        payload = _unpack(token, self.secret)
        if payload.get("v") != 1 or payload.get("b") != canonical or payload.get("g") != owner_generation:
            raise ContinuationDenied("continuation capability binding mismatch")
        nonce, expires = payload.get("n"), payload.get("e")
        if not isinstance(nonce, str) or type(expires) is not int or expires <= int(self.now()):
            raise ContinuationDenied("continuation capability expired")
        return nonce, expires

    def enqueue(self, *, token: str, binding: dict[str, str], owner_generation: str, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("continuation prompt is required")
        canonical = _canonical(binding)
        nonce, _ = self._validate_payload(token=token, canonical=canonical, owner_generation=owner_generation)
        owner_key = binding.get("owner")
        if not owner_key:
            raise ValueError("continuation binding requires owner")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT binding, owner_generation, expires_at, revoked FROM private_continuation_scopes WHERE nonce = ?", (nonce,)).fetchone()
            if row is None or row[0] != canonical or row[1] != owner_generation or int(row[2]) < int(self.now()) or int(row[3]):
                conn.execute("ROLLBACK")
                raise ContinuationDenied("continuation capability is unavailable")
            # Unknown dispatch outcomes retain a durable owner fence too.  Do not
            # let retries accumulate behind an execution that might still run.
            count = conn.execute("SELECT COUNT(*) FROM private_continuation_queue WHERE owner_key = ? AND state IN ('queued', 'running', 'ambiguous')", (owner_key,)).fetchone()[0]
            if count >= self.max_per_owner:
                conn.execute("ROLLBACK")
                raise ContinuationDenied("continuation owner queue is full")
            conn.execute("INSERT INTO private_continuation_queue (nonce,binding,owner_generation,owner_key,prompt) VALUES (?,?,?,?,?)", (nonce, canonical, owner_generation, owner_key, prompt))
            conn.execute("COMMIT")

    def take(self, *, token: str, binding: dict[str, str], owner_generation: str) -> tuple[int, str] | None:
        """Atomically claim one still-valid row for this exact capability or deny it."""
        canonical = _canonical(binding)
        nonce, _ = self._validate_payload(token=token, canonical=canonical, owner_generation=owner_generation)
        owner_key = binding.get("owner")
        if not owner_key:
            raise ValueError("continuation binding requires owner")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope = conn.execute("SELECT binding, owner_generation, expires_at, revoked FROM private_continuation_scopes WHERE nonce = ?", (nonce,)).fetchone()
            if scope is None or scope[0] != canonical or scope[1] != owner_generation or int(scope[2]) <= int(self.now()) or int(scope[3]):
                conn.execute("ROLLBACK")
                raise ContinuationDenied("continuation capability is unavailable")
            row = conn.execute("SELECT id, prompt FROM private_continuation_queue WHERE nonce = ? AND binding = ? AND owner_generation = ? AND owner_key = ? AND state = 'queued' ORDER BY id LIMIT 1", (nonce, canonical, owner_generation, owner_key)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute("UPDATE private_continuation_queue SET state = 'running' WHERE id = ? AND state = 'queued'", (row[0],))
            conn.execute("COMMIT")
            return int(row[0]), str(row[1])

    def requeue(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE private_continuation_queue SET state = 'queued' WHERE id = ? AND state = 'running'", (item_id,))

    def finish(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM private_continuation_queue WHERE id = ? AND state = 'running'", (item_id,))

    def mark_ambiguous(self, item_id: int) -> None:
        """Preserve a non-replayable record after dispatch might have started."""
        with self._connect() as conn:
            conn.execute("UPDATE private_continuation_queue SET state = 'ambiguous' WHERE id = ? AND state = 'running'", (item_id,))


class GatewayPrivateContinuationMixin:
    """Canonical private-route continuation ABI for profile-local plugins."""

    def configure_private_continuations(self, *, database: Path, secret: bytes, max_per_owner: int = 10,
                                       profile: str | None = None) -> None:
        """Install one durable store, optionally scoped to a multiplexed profile."""
        store = PrivateContinuationStore(database, secret=secret, max_per_owner=max_per_owner)
        if profile is None:
            self._private_continuations = store
            return
        if not isinstance(profile, str) or not profile:
            raise ValueError("private continuation profile is required")
        stores = getattr(self, "_private_continuations_by_profile", None)
        if stores is None:
            stores = {}
            self._private_continuations_by_profile = stores
        if profile in stores:
            raise RuntimeError("private continuations are already configured for this profile")
        stores[profile] = store

    def _private_continuation_store(self, binding: dict[str, str]) -> PrivateContinuationStore:
        stores = getattr(self, "_private_continuations_by_profile", {})
        if binding.get("profile") in stores:
            return stores[binding["profile"]]
        store = getattr(self, "_private_continuations", None)
        if store is None:
            raise ContinuationDenied("private continuation capability is unavailable")
        return store

    def mint_private_continuation(self, *, binding: dict[str, str], owner_generation: str, ttl_seconds: int) -> str:
        return self._private_continuation_store(binding).mint(binding=binding, owner_generation=owner_generation, ttl_seconds=ttl_seconds)

    async def enqueue_private_continuation(self, *, token: str, binding: dict[str, str], owner_generation: str, prompt: str, authorize: Callable[[], bool]) -> None:
        if not callable(authorize) or authorize() is not True:
            raise ContinuationDenied("continuation requester is not authorized")
        self._private_continuation_store(binding).enqueue(token=token, binding=binding, owner_generation=owner_generation, prompt=prompt)

    def _private_continuation_event(self, binding: dict[str, str], prompt: str,
                                    execution_id: str, *, item_id: int | None = None) -> tuple[Any, str, _PrivateContinuationGrant]:
        """Resolve only a pre-existing private owner route; never mint a Linear route."""
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageType
        owner, profile = binding.get("owner"), binding.get("profile")
        store = getattr(self, "session_store", None)
        entry = store.lookup_by_session_id(owner) if store is not None and owner else None
        origin = getattr(entry, "origin", None)
        allowed_platforms = {Platform.LOCAL, Platform.SLACK, Platform.TELEGRAM}
        if (entry is None or origin is None or origin.platform not in allowed_platforms
                or origin.profile != profile or getattr(origin, "chat_type", None) != "dm"):
            raise ContinuationDenied("private continuation owner route is unavailable")
        source = origin.from_dict(origin.to_dict())
        if self._session_key_for_source(source) != entry.session_key:
            raise ContinuationDenied("private continuation owner route changed")
        event = MessageEvent(text=prompt, message_type=MessageType.TEXT, source=source, internal=True,
                             allow_gateway_control=False, metadata={})
        state = self._session_state(entry.session_key)
        generation = int(state.persistent.run_generation) + 1
        grant = _PrivateContinuationGrant(event, source, entry.session_key, generation, execution_id, item_id)
        self.__dict__.setdefault("_private_continuation_grants", {})[id(grant)] = grant
        return event, entry.session_key, grant

    async def prepare_private_continuation(self, *, token: str, binding: dict[str, str],
                                           owner_generation: str, authorize: Callable[[], bool],
                                           execution_policy: dict | None = None) -> PrivateContinuationExecution:
        """Reserve one durable continuation and return its Stop/lifecycle identity before launch."""
        if not callable(authorize) or authorize() is not True:
            raise ContinuationDenied("continuation requester is not authorized")
        if execution_policy is not None:
            from gateway.execution_lifecycle import validate_internal_execution_policy
            # Keep a private canonical copy: callers cannot loosen a reserved execution later.
            execution_policy = dict(validate_internal_execution_policy(execution_policy))
        store = self._private_continuation_store(binding)
        item = store.take(token=token, binding=binding, owner_generation=owner_generation)
        if item is None:
            raise ContinuationDenied("private continuation is unavailable")
        item_id, prompt = item
        grant = None
        try:
            execution_id = "private-continuation-" + secrets.token_urlsafe(24)
            event, session_key, grant = self._private_continuation_event(
                binding, prompt, execution_id, item_id=item_id
            )
            if self._is_session_running(session_key):
                raise ContinuationDenied("private continuation owner is busy")
            event._internal_plugin_execution_id = execution_id
            self._register_internal_plugin_execution(event, session_key)
            handle = PrivateContinuationExecution(execution_id, session_key, grant.generation)
            self.__dict__.setdefault("_prepared_private_continuations", {})[id(handle)] = (
                handle, grant, store, execution_policy,
            )
            return handle
        except Exception:
            if grant is not None:
                self.__dict__.get("_private_continuation_grants", {}).pop(id(grant), None)
            store.requeue(item_id)
            raise

    async def launch_private_continuation(self, handle: PrivateContinuationExecution, *,
                                          authorize: Callable[[], bool]) -> bool:
        """Consume a prepared reservation through the normal handler exactly once."""
        prepared = self.__dict__.get("_prepared_private_continuations", {}).pop(id(handle), None)
        if (not isinstance(handle, PrivateContinuationExecution) or prepared is None
                or prepared[0] is not handle):
            raise ContinuationDenied("private continuation handle is unavailable")
        _, grant, store, execution_policy = prepared
        if not callable(authorize) or authorize() is not True:
            raise ContinuationDenied("continuation requester is not authorized")
        receipt = await self.get_execution_lifecycle(
            session_key=handle.session_key, execution_id=handle.execution_id
        )
        if receipt.get("state") == "stopped":
            return False
        pre_claim = (receipt.get("state") == "stale" and receipt.get("generation") == handle.generation
                     and self._session_state(handle.session_key).persistent.run_generation == handle.generation - 1)
        if receipt.get("state") != "running" and not pre_claim:
            raise ContinuationDenied("private continuation reservation is stale")
        try:
            await self.dispatch_internal_plugin_event(
                grant.event, execution_id=handle.execution_id, execution_policy=execution_policy,
                private_continuation_grant=grant,
            )
        except Exception:
            store.mark_ambiguous(grant.item_id)
            raise
        receipt = await self.get_execution_lifecycle(
            session_key=handle.session_key, execution_id=handle.execution_id
        )
        if not (
            receipt.get("session_key") == handle.session_key
            and receipt.get("execution_id") == handle.execution_id
            and receipt.get("generation") == handle.generation
            and receipt.get("state") == "completed"
            and receipt.get("occupancy") == "released"
            and all(receipt.get(field) == "none" for field in ("tools", "children", "processes", "remote"))
        ):
            return False
        store.finish(grant.item_id)
        return True

    async def drain_private_continuation(self, *, token: str, binding: dict[str, str], owner_generation: str,
                                         authorize: Callable[[], bool], execution_policy: dict | None = None) -> bool:
        """Compatibility one-shot prepare and launch for a private continuation."""
        try:
            handle = await self.prepare_private_continuation(
                token=token, binding=binding, owner_generation=owner_generation, authorize=authorize,
                execution_policy=execution_policy,
            )
        except ContinuationDenied as exc:
            if str(exc) == "private continuation is unavailable":
                return False
            raise
        return await self.launch_private_continuation(handle, authorize=authorize)
