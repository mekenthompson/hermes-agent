"""Whether a PR the ``active_pr`` respawn guard waits on is still open.

The guard holds a card while a recent comment links a GitHub PR, so a respawn
cannot open a duplicate. A comment cannot say when that PR merges, so the card
used to park as "waiting for PR #N to merge" long after the merge. The guard now
asks GitHub, and lifts once every PR it saw is closed (merged or not).

Fail closed: an unreachable API, a private repo without a token or a rate limit
answers "unknown", which keeps the guard. Answers are cached per process for
``_TTL_SECONDS`` because the dispatcher re-checks every tick and an anonymous
caller shares 60 requests an hour with everything behind the same IP.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.IGNORECASE)
_API_URL = "https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
_TIMEOUT_SECONDS = 5
_TTL_SECONDS = 300

_cache: dict[tuple[str, str, int], tuple[float, Optional[bool]]] = {}
_cache_lock = threading.Lock()


def fetch_pr_closed(owner: str, repo: str, number: int) -> Optional[bool]:
    """GitHub's answer: True closed (merged or not), False open, None unknown."""
    from hermes_cli.github_api import github_token

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-kanban-respawn-guard"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(_API_URL.format(owner=owner, repo=repo, number=number), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:  # URLError/HTTPError/timeouts are OSError
        logger.debug("kanban respawn guard: PR %s/%s#%s state unknown: %s", owner, repo, number, exc)
        return None
    state = data.get("state") if isinstance(data, dict) else None
    return True if state == "closed" else False if state == "open" else None


def pr_closed(url: str) -> Optional[bool]:
    """Cached :func:`fetch_pr_closed` for one PR URL; None for a URL that names no PR."""
    match = _PR_URL_RE.search(url)
    if match is None:
        return None
    key = (match[1].lower(), match[2].lower(), int(match[3]))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    closed = fetch_pr_closed(*key)
    with _cache_lock:
        _cache[key] = (now + _TTL_SECONDS, closed)
    return closed


def all_closed(urls: Iterable[str]) -> bool:
    """True only when there is at least one PR and GitHub reports every one closed."""
    unique = list(dict.fromkeys(urls))
    return bool(unique) and all(pr_closed(url) is True for url in unique)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
