"""Durable estimated-cost admission for explicitly policy-bound executions.

This is an estimate guard, not provider billing enforcement.  One request may be
in flight (and may therefore take an issue over its configured limit).
"""
from __future__ import annotations

import math
from typing import Any


class CostBudgetExceeded(RuntimeError):
    pass


def configure(agent: Any, policy: object) -> None:
    # Cached gateway agents are reused across independently authorized executions.
    # Clear stale policy before applying the new capability so one run cannot bill another.
    agent.__dict__.pop("_cost_budget", None)
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ValueError("internal execution cost policy must be a mapping")
    limit, key = policy.get("estimated_cost_limit_usd"), policy.get("issue_budget_key")
    if (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(float(limit))
            or float(limit) <= 0 or not isinstance(key, str) or not key.strip()):
        raise ValueError("internal execution cost policy requires positive estimated_cost_limit_usd and issue_budget_key")
    db = getattr(agent, "_session_db", None)
    if db is None or not callable(getattr(db, "_execute_write", None)):
        raise RuntimeError("persistent session database is required for cost budget")
    agent._cost_budget = (db, key, float(limit))


def _write(db, fn):
    db._execute_write(fn)


def admit(agent: Any) -> None:
    cfg = getattr(agent, "_cost_budget", None)
    if not cfg:
        return
    db, key, limit = cfg
    blocked = []
    def tx(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS issue_estimated_cost_budget (budget_key TEXT PRIMARY KEY, limit_usd REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0, inflight INTEGER NOT NULL DEFAULT 0, unknown_usage INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT OR IGNORE INTO issue_estimated_cost_budget (budget_key, limit_usd) VALUES (?, ?)", (key, limit))
        row = conn.execute("SELECT limit_usd, spent_usd, inflight, unknown_usage FROM issue_estimated_cost_budget WHERE budget_key = ?", (key,)).fetchone()
        if row is None or float(row[0]) != limit or int(row[2]) or int(row[3]) or float(row[1]) >= float(row[0]):
            blocked.append(True); return
        conn.execute("UPDATE issue_estimated_cost_budget SET inflight = 1 WHERE budget_key = ?", (key,))
    _write(db, tx)
    if blocked:
        raise CostBudgetExceeded("Estimated issue cost budget is exhausted, unknown, or has a pending request; no provider request was sent.")


def reconcile(agent: Any, amount: object, *, known: bool) -> None:
    cfg = getattr(agent, "_cost_budget", None)
    if not cfg:
        return
    db, key, _limit = cfg
    value = float(amount) if known and isinstance(amount, (int, float)) and math.isfinite(float(amount)) and float(amount) >= 0 else 0.0
    def tx(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS issue_estimated_cost_budget (budget_key TEXT PRIMARY KEY, limit_usd REAL NOT NULL, spent_usd REAL NOT NULL DEFAULT 0, inflight INTEGER NOT NULL DEFAULT 0, unknown_usage INTEGER NOT NULL DEFAULT 0)")
        # Unknown or an accounting failure deliberately leaves the durable in-flight debit.
        if known:
            conn.execute("UPDATE issue_estimated_cost_budget SET spent_usd = spent_usd + ?, inflight = 0 WHERE budget_key = ?", (value, key))
        else:
            conn.execute("UPDATE issue_estimated_cost_budget SET unknown_usage = 1 WHERE budget_key = ?", (key,))
    _write(db, tx)
