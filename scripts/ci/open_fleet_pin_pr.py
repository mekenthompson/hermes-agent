#!/usr/bin/env python3
"""Pin a published Agent image in hermes-fleet by opening an auto-merging PR.

Ported from hermes-fleet-private ``scripts/release-conductor.py`` (stage
``fleet-pin-pr`` / ``ensure_file_pr``) so the hand-off no longer needs a human
to run the conductor. Runs from ``.github/workflows/release-handoff.yml`` with
a GitHub App token scoped to hermes-fleet; ``gh`` is the only network path.

Behaviour, in order:

1. Validate ``agent-image-manifest.json`` and bind its ``revision`` to the
   triggering ``workflow_run.head_sha`` (never ``github.sha``).
2. No-op when hermes-fleet ``main`` already pins exactly this content.
3. Branch ``release/pin-agent-<short>``: reuse when it already carries the
   desired content; fail closed when it exists with anything else.
4. PUT ``release/agent-image-manifest.json`` via the contents API, reuse or
   create the PR, then ``gh pr merge --auto --squash`` so hermes-fleet's
   required checks remain the merge gate.

``--dry-run`` performs every read and prints every write without performing
it (auto-merge included).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

AGENT_REPOSITORY = "ghcr.io/mekenthompson/hermes-agent"
DEFAULT_FLEET_REPO = "mekenthompson/hermes-fleet"
FLEET_MANIFEST_PATH = "release/agent-image-manifest.json"
MANIFEST_KEYS = {"schema_version", "repository", "revision", "digest", "immutable_ref"}
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

GhRunner = Callable[[list[str], "str | None"], str]


class HandoffError(RuntimeError):
    """Any condition that must stop the hand-off without writing anything further."""


class GhError(HandoffError):
    def __init__(self, args: list[str], code: int, stdout: str, stderr: str) -> None:
        self.args_list = args
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"gh {' '.join(args)} failed ({code}): {(stderr or stdout).strip()}")

    @property
    def not_found(self) -> bool:
        return "404" in self.stderr or "Not Found" in self.stderr


def short(sha: str) -> str:
    return sha[:8]


def canonical_json(payload: Any) -> str:
    """Match emit-image-manifest.py and hermes-fleet's writer: sorted keys, two-space indent, newline."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def validate_manifest(payload: Any, *, expected_revision: str) -> dict[str, Any]:
    """Accept only a schema-1 manifest for the fork Agent image at exactly ``expected_revision``."""
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise HandoffError(f"agent manifest must contain exactly {sorted(MANIFEST_KEYS)}")
    if payload["schema_version"] != 1:
        raise HandoffError(f"unsupported agent manifest schema_version {payload['schema_version']!r}")
    repository = payload["repository"]
    revision = payload["revision"]
    digest = payload["digest"]
    if repository != AGENT_REPOSITORY:
        raise HandoffError(f"agent manifest repository {repository!r} is not {AGENT_REPOSITORY}")
    if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
        raise HandoffError(f"agent manifest revision {revision!r} is not a 40-hex commit SHA")
    if not isinstance(expected_revision, str) or REVISION_RE.fullmatch(expected_revision) is None:
        raise HandoffError(f"expected revision {expected_revision!r} is not a 40-hex commit SHA")
    if revision != expected_revision:
        raise HandoffError(f"agent manifest revision {revision} != triggering run head_sha {expected_revision}")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        raise HandoffError(f"agent manifest digest {digest!r} is not a sha256 digest")
    if payload["immutable_ref"] != f"{repository}@{digest}":
        raise HandoffError("agent manifest immutable_ref does not equal repository@digest")
    return {key: payload[key] for key in sorted(MANIFEST_KEYS)}


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------


def run_gh(args: list[str], input_text: str | None = None) -> str:
    result = subprocess.run(  # windows-footgun: ok — CI-only script; gh emits UTF-8
        ["gh", *args],
        text=True,
        capture_output=True,
        input=input_text,
        check=False,
    )
    if result.returncode:
        raise GhError(args, result.returncode, result.stdout, result.stderr)
    return result.stdout


class GitHub:
    """Thin contents/refs/PR client over ``gh``; tests inject a fake runner."""

    def __init__(self, repo: str, gh: GhRunner = run_gh) -> None:
        self.repo = repo
        self.gh = gh

    def json(self, args: list[str], input_text: str | None = None) -> Any:
        raw = self.gh(args, input_text)
        try:
            return json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            raise HandoffError(f"gh {' '.join(args)} returned non-JSON output: {exc}") from exc

    def api(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        args = ["api", path, "-X", method]
        input_text = None
        if body is not None:
            args += ["--input", "-"]
            input_text = json.dumps(body)
        return self.json(args, input_text)

    def head(self, branch: str = "main") -> str:
        payload = self.api(f"repos/{self.repo}/git/ref/heads/{branch}") or {}
        return require_sha(payload.get("object", {}).get("sha"), f"{self.repo}@{branch} head")

    def branch_exists(self, branch: str) -> str | None:
        try:
            payload = self.api(f"repos/{self.repo}/git/ref/heads/{branch}") or {}
        except GhError as exc:
            if exc.not_found:
                return None
            raise
        return require_sha(payload.get("object", {}).get("sha"), f"{self.repo}@{branch}")

    def file(self, path: str, ref: str) -> tuple[bytes | None, str | None]:
        """Return (content, blob sha) for ``path`` at ``ref``, or (None, None) when absent."""
        try:
            payload = self.api(f"repos/{self.repo}/contents/{path}?ref={ref}")
        except GhError as exc:
            if exc.not_found:
                return None, None
            raise
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise HandoffError(f"{self.repo}:{path}@{ref} is not a file")
        if payload.get("encoding") != "base64":
            raise HandoffError(f"{self.repo}:{path}@{ref} uses unsupported encoding {payload.get('encoding')!r}")
        return base64.b64decode(payload["content"]), payload["sha"]

    def create_branch(self, branch: str, sha: str) -> None:
        self.api(f"repos/{self.repo}/git/refs", method="POST", body={"ref": f"refs/heads/{branch}", "sha": sha})

    def put_file(self, branch: str, path: str, content: bytes, message: str, existing_sha: str | None) -> str:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        payload = self.api(f"repos/{self.repo}/contents/{path}", method="PUT", body=body) or {}
        return require_sha((payload.get("commit") or {}).get("sha"), f"{self.repo}:{path} commit")

    def find_open_pr(self, branch: str) -> dict[str, Any] | None:
        prs = (
            self.json(
                [
                    "pr", "list", "-R", self.repo, "--head", branch, "--base", "main",
                    "--state", "open", "--json", "url,number,headRefOid",
                ]
            )
            or []
        )
        if len(prs) > 1:
            raise HandoffError(f"{self.repo} has several open PRs from {branch}; resolve by hand")
        return prs[0] if prs else None

    def create_pr(self, branch: str, title: str, body: str) -> str:
        out = self.gh(
            ["pr", "create", "-R", self.repo, "--base", "main", "--head", branch, "--title", title, "--body-file", "-"],
            body,
        )
        urls = [line.strip() for line in out.splitlines() if line.strip().startswith("https://github.com/")]
        if not urls:
            raise HandoffError(f"gh pr create did not print a PR URL: {out!r}")
        return urls[-1]

    def enable_auto_merge(self, url: str) -> str:
        """Arm squash auto-merge; a no-op when the PR is already merged or already armed."""
        number = pr_number_from_url(url)
        view = self.json(["pr", "view", str(number), "-R", self.repo, "--json", "state,autoMergeRequest"]) or {}
        if view.get("state") == "MERGED":
            return "already merged"
        if view.get("autoMergeRequest"):
            return "auto-merge already enabled"
        self.gh(["pr", "merge", "--auto", "--squash", "-R", self.repo, str(number)], None)
        return "auto-merge enabled"


def require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or REVISION_RE.fullmatch(value) is None:
        raise HandoffError(f"{label} is not a commit SHA: {value!r}")
    return value


def pr_number_from_url(url: str) -> int:
    match = re.search(r"/pull/(\d+)(?:[/?#].*)?$", url)
    if not match:
        raise HandoffError(f"cannot read a PR number from {url!r}")
    return int(match.group(1))


# ---------------------------------------------------------------------------
# The hand-off itself
# ---------------------------------------------------------------------------


def pin_branch(revision: str) -> str:
    return f"release/pin-agent-{short(revision)}"


def pr_title(revision: str) -> str:
    return f"release: pin Agent {short(revision)}"


def commit_message(manifest: dict[str, Any]) -> str:
    return (
        f"release: pin Agent {short(manifest['revision'])} ({manifest['digest'].removeprefix('sha256:')[:12]})\n\n"
        f"Published digest {manifest['digest']}.\n\n"
        "Release-Handoff-Stage: pin-fleet\n"
    )


def pr_body(manifest: dict[str, Any], source_run_url: str) -> str:
    return (
        "Pins the public Fleet child to the exact published Agent image.\n\n"
        f"- Agent revision: `{manifest['revision']}`\n"
        f"- Agent digest: `{manifest['digest']}`\n"
        f"- Immutable ref: `{manifest['immutable_ref']}`\n"
        f"- Source run: {source_run_url}\n\n"
        "Opened by scripts/ci/open_fleet_pin_pr.py (hermes-agent release-handoff.yml, job pin-fleet). "
        "Auto-merge is armed; hermes-fleet's required checks remain the merge gate.\n"
    )


def ensure_pin_pr(github: GitHub, manifest: dict[str, Any], *, source_run_url: str, dry_run: bool) -> dict[str, Any]:
    """Create or find the pin PR; returns a status dict for logging and tests."""
    content = canonical_json(manifest).encode("utf-8")
    revision = manifest["revision"]
    branch = pin_branch(revision)
    title = pr_title(revision)

    base_sha = github.head("main")
    current, base_blob_sha = github.file(FLEET_MANIFEST_PATH, base_sha)
    if current == content:
        log(f"{github.repo} main@{short(base_sha)} already pins Agent {short(revision)}; nothing to do")
        return {"status": "already-pinned", "pr_url": None}

    head_sha = github.branch_exists(branch)
    if head_sha:
        actual, _ = github.file(FLEET_MANIFEST_PATH, head_sha)
        if actual != content:
            raise HandoffError(
                f"branch {branch} already exists in {github.repo} (at {short(head_sha)}) but {FLEET_MANIFEST_PATH} "
                "differs from the published manifest; delete or rename that branch by hand before re-running"
            )
        existing = github.find_open_pr(branch)
        if existing:
            log(f"PR already open for {branch}: {existing['url']}")
            return finish(github, existing["url"], status="pr-existing", dry_run=dry_run)
        if dry_run:
            log(f"[dry-run] would create a PR titled {title!r} from existing branch {branch}@{short(head_sha)}")
            return {"status": "pr-created", "pr_url": None, "dry_run": True}
        url = github.create_pr(branch, title, pr_body(manifest, source_run_url))
        log(f"created PR {url} from existing branch {branch}")
        return finish(github, url, status="pr-created", dry_run=dry_run)

    if dry_run:
        log(f"[dry-run] would create {github.repo}@{branch} from main@{short(base_sha)}")
        log(f"[dry-run] would write {FLEET_MANIFEST_PATH}:\n{content.decode('utf-8')}")
        log(f"[dry-run] would create a PR titled {title!r} and arm squash auto-merge")
        return {"status": "pr-created", "pr_url": None, "dry_run": True}

    github.create_branch(branch, base_sha)
    commit = github.put_file(branch, FLEET_MANIFEST_PATH, content, commit_message(manifest), base_blob_sha)
    log(f"committed {FLEET_MANIFEST_PATH} to {branch} as {short(commit)}")
    url = github.create_pr(branch, title, pr_body(manifest, source_run_url))
    log(f"created PR {url}")
    return finish(github, url, status="pr-created", dry_run=dry_run)


def finish(github: GitHub, url: str, *, status: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        log(f"[dry-run] would arm squash auto-merge on {url}")
        return {"status": status, "pr_url": url, "dry_run": True}
    outcome = github.enable_auto_merge(url)
    log(f"{url}: {outcome}")
    return {"status": status, "pr_url": url, "auto_merge": outcome}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="downloaded agent-image-manifest.json")
    parser.add_argument(
        "--expected-revision",
        required=True,
        help="40-hex SHA the manifest must pin (github.event.workflow_run.head_sha)",
    )
    parser.add_argument("--source-run-url", required=True, help="URL of the Fork Agent Image run that published the image")
    parser.add_argument("--fleet-repo", default=DEFAULT_FLEET_REPO, help=f"owner/name of the fleet repo (default {DEFAULT_FLEET_REPO})")
    parser.add_argument("--dry-run", action="store_true", help="read everything, write nothing, arm nothing")
    return parser


def main(argv: list[str] | None = None, *, gh: GhRunner = run_gh) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        manifest = validate_manifest(payload, expected_revision=args.expected_revision)
        github = GitHub(args.fleet_repo, gh)
        result = ensure_pin_pr(github, manifest, source_run_url=args.source_run_url, dry_run=args.dry_run)
    except (HandoffError, OSError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
