"""Session title helpers shared by TUI session handlers."""

def _branch_title(db, parent_key: str) -> str:
    """Next title in the parent's lineage (mirrors the TUI /branch naming)."""
    current = db.get_session_title(parent_key) or "branch"
    if hasattr(db, "get_next_title_in_lineage"):
        return db.get_next_title_in_lineage(current)
    return f"{current} (branch)"
