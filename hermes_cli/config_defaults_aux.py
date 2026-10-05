"""Small constructor for auxiliary model defaults."""


def _aux(timeout, *, reasoning_effort=True, **extra):
    """Build a standard auxiliary-task model block."""
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d
