import sqlite3
from types import SimpleNamespace
import pytest
from agent.cost_budget import CostBudgetExceeded, admit, configure, reconcile


class DB:
    def __init__(self): self.conn = sqlite3.connect(":memory:")
    def _execute_write(self, fn):
        self.conn.execute("BEGIN")
        try:
            result = fn(self.conn); self.conn.commit(); return result
        except Exception:
            self.conn.rollback(); raise


def agent(db, key="issue-1", limit=1.0):
    a = SimpleNamespace(_session_db=db)
    configure(a, {"issue_budget_key": key, "estimated_cost_limit_usd": limit})
    return a


def test_resumed_same_issue_and_child_share_durable_one_request_overshoot():
    db = DB(); parent = agent(db); child = agent(db)
    admit(parent)
    with pytest.raises(CostBudgetExceeded): admit(child)
    reconcile(parent, 1.25, known=True)  # documented one-request estimate overshoot
    with pytest.raises(CostBudgetExceeded): admit(agent(db))


def test_unknown_usage_fails_closed_across_resume():
    db = DB(); first = agent(db); admit(first); reconcile(first, None, known=False)
    with pytest.raises(CostBudgetExceeded): admit(agent(db))


def test_missing_or_invalid_config_is_rejected_without_global_fallback():
    with pytest.raises(ValueError): configure(SimpleNamespace(_session_db=DB()), {"issue_budget_key": "i"})
    with pytest.raises(ValueError): configure(SimpleNamespace(_session_db=DB()), {"issue_budget_key": "i", "estimated_cost_limit_usd": 0})
    with pytest.raises(RuntimeError): configure(SimpleNamespace(_session_db=None), {"issue_budget_key": "i", "estimated_cost_limit_usd": 1})
