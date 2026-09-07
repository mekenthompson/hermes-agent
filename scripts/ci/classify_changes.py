#!/usr/bin/env python3
"""Classify a PR's changed files into CI work lanes.

Reads newline-separated changed paths on stdin and writes ``key=value``
booleans (one per lane) to ``$GITHUB_OUTPUT`` and stdout. The
``detect-changes`` composite action consumes them so steps gate on
``if: steps.changes.outputs.<lane> == 'true'``.

Lanes:

* ``python``      — pytest / ruff / ty / footguns.
* ``python_prod`` — Python changes OUTSIDE tests/ — gates jobs that ship or
  run the product (Desktop E2E backend, Docker image) but never import the
  test suite. A tests-only PR keeps ``python`` (pytest must run) while
  skipping those product jobs.
* ``docker_meta`` — Dockerfiles etc.
* ``docker`` — any product change + docker meta
* ``nix``         — ``nix flake check``: the flake inputs and any product change.
* ``frontend``    — TS typecheck matrix + desktop build.
* ``site``        — Docusaurus + generated skill docs.
* ``scan``        — supply-chain scan (Python files, .pth, setup hooks).
* ``deps``        — pyproject.toml dependency bounds check.
* ``uv_lock``     — ``uv lock --check``. Re-resolves the whole graph against
  PyPI, so a diff that touches neither ``pyproject.toml`` nor ``uv.lock``
  must not run it.
* ``npm_lock``    — semantic package-lock.json diff PR comment.
* ``installer``   — PowerShell installer tests (Windows runner).
* ``desktop_updater`` — the Windows desktop-update hand-off script and the
  tests that drive the REAL ``windows.ps1`` (``-SelfTestUi`` / pipe drain /
  retry policy). These are integration tests of a PowerShell process on a
  shared runner; running them on every Python PR made their timing noise
  everyone's problem. They still run on push (fail-open) and whenever the
  script, its siblings, or their tests change.
* ``rust``        — ``cargo test`` for the Tauri bootstrap installer. ``.rs``
  lives under ``apps/``, so without this lane a Rust change matched ``frontend``
  and only the TypeScript matrix ran.
* ``mcp_catalog`` — bundled MCP catalog / installer review.
* ``os_tests``    — the macOS / Windows pytest lanes. Upstream ignores this
  key (its OS lanes ride on ``python``); the fork consumes it so a change
  that touches no OS-specific surface does not pay for a Windows and a
  macOS runner. It is true whenever the diff carries a platform-specific
  path, an OS-marked test file, the test config, or a dependency manifest.
* ``binary_artifacts`` — a changed image / archive file. The fork gates the
  committed-infographic and profile-archive tree checks on it; upstream runs
  those on every event regardless.

Fork mode (``--fork`` / ``REPO=mekenthompson/hermes-agent``) refines only
the ``.github/`` rule below: a workflow ci.yaml never calls cannot change
what CI runs, and a workflow it does call only affects the lanes that gate
it. Everything else in this file behaves identically upstream and in the
fork.

Docker is not a lane — it builds on push-to-main and release only,
never per-PR.

Contract — *fail open, never closed*. We may run a lane we didn't need, but
must never skip one a change could break:

* An empty diff, or any ``.github/`` change, runs everything.
* ``python`` is a denylist: skipped only when *every* file is provably prose
  or a frontend-only package; an unrecognized path keeps it on.
* ``skills/`` (incl. ``SKILL.md``) is python-relevant — the skill-doc tests
  read that tree, so a doc-looking edit can still break Python.
* ``nix/``, ``flake.nix`` and ``flake.lock`` are the exception the other way:
  only the flake reads them, so they skip the Python lanes and run ``nix``
  alone. ``pyproject.toml`` and ``uv.lock`` are flake inputs too, but the
  packaging tests read them, so they keep every Python lane.
* ``website/static/oauth/`` is python-relevant too: it publishes the OAuth
  Client ID Metadata Document that ``tests/tools/test_mcp_cimd.py`` checks
  against the pinned callback ports in ``tools/mcp_oauth.py``.
* ``website/docs/`` and ``website/scripts/`` are python-relevant for the same
  reason: the docs tree generates ``llms.txt``, and
  ``tests/website/test_generate_llms_txt.py`` asserts every page reaches it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_FRONTEND = ("ui-tui/", "web/", "apps/")  # TS typecheck-matrix packages
# Shipped page outside those packages, exercised by the desktop Electron suite.
_FRONTEND_FILES = {"scripts/desktop-update/ui.html"}
# Frontend trees the Dockerfile copies (apps/ is in .dockerignore except
# apps/shared). Fork mode narrows ``docker`` to these; upstream keeps the
# whole ``frontend`` lane as an image input.
_IMAGE_FRONTEND = ("ui-tui/", "web/", "apps/shared/")
_ROOT_NPM = {"package.json", "package-lock.json"}  # shifts every package's tree
_DOCKER_META = ("docker/", ".hadolint.yml", "Dockerfile") # docker setup
_NIX_PATHS = ("nix/",) # nix files
_NIX_FILES = {"flake.nix", "flake.lock"} # base nix files
_SITE = ("website/", "skills/", "optional-skills/")  # docs site + skill pages
# Prose/frontend trees that can't touch Python. skills/ is excluded on purpose.
_PY_SKIP = ("docs/", "website/") + _FRONTEND
# Published artifacts that live under website/ but that Python asserts about.
# The OAuth Client ID Metadata Document is cross-checked against the pinned
# callback ports in tools/mcp_oauth.py, so editing it alone must still run the
# Python lane — otherwise dropping a redirect URI goes green here and breaks
# every CIMD login on main.
# website/docs/ and website/scripts/ are asserted about the same way. The docs
# tree generates llms.txt — the index every LLM (Hermes included, via the
# hermes-agent skill) reads to learn what Hermes can do — and
# tests/website/test_generate_llms_txt.py holds every page to appearing in it.
# Skipping Python on a docs-only PR is how the index drifted to 53% coverage.
_PY_RELEVANT_SITE = (
    "website/static/oauth/",
    "website/docs/",
    "website/scripts/",
)

# CI-sensitive files: eslint config, workflow files, composite actions.
# Changes here can influence what code the autofix job executes and pushes to
# main, so they require explicit maintainer review (ci-reviewed label).
#
# package.json is deliberately NOT listed here: npm scripts only execute on the
# unprivileged generate-patch runner (contents: read), never on the privileged
# apply-patch job. The two-job split means a malicious package.json script
# can't get push access — it runs on an ephemeral runner with zero write perms.
_CI_REVIEW_FILES = {
    ".prettierrc",
}
_CI_REVIEW_PATHS = (".github/workflows/", ".github/actions/")

# Supply-chain scan: files that can execute code at install/import time.
_SCAN_EXTS = (".py", ".pth")
_SCAN_FILES = {"setup.cfg", "pyproject.toml"}

# MCP catalog files that require explicit security review.
_MCP_CATALOG_PATHS = ("optional-mcps/",)
_MCP_CATALOG_FILES = {"hermes_cli/mcp_catalog.py"}

# Windows installer + its PowerShell tests. These only run on a Windows runner,
# so they get their own lane rather than riding along with ``python``.
_INSTALLER_PATHS = ("scripts/tests/",)
_INSTALLER_FILES = {"scripts/install.ps1", "scripts/install.cmd"}

# Windows desktop-update hand-off (scripts/desktop-update/windows.ps1 + the
# Electron side that launches it) and the pytest files that spawn it.
_DESKTOP_UPDATER_PATHS = ("scripts/desktop-update/",)
_DESKTOP_UPDATER_TEST_PREFIX = "tests/test_desktop_update_"
_DESKTOP_UPDATER_FILES = {
    "apps/desktop/electron/updater-process.ts",
    "apps/desktop/electron/managed-ssh-update.ts",
    "tests/conftest.py",
    "pyproject.toml",
}

# Rust crates — currently just the Tauri bootstrap installer (Hermes-Setup).
# These live under ``apps/``, so before this lane existed a ``.rs`` edit matched
# ``frontend`` and nothing more: the TypeScript matrix built, cargo never ran,
# and the crate's unit tests had never executed in CI at all.
_RUST_PATHS = ("apps/bootstrap-installer/src-tauri/",)
_RUST_FILENAMES = {"Cargo.toml", "Cargo.lock"}

# The maintained fork. Its CI runs on 4-core hosted runners under a 20-job
# concurrency cap, so it scopes harder than upstream where this file marks it.
FORK_REPOSITORY = "mekenthompson/hermes-agent"
_FORK_IMAGE_WORKFLOW = ".github/workflows/fork-agent-image.yml"
_CI_ORCHESTRATOR = ".github/workflows/ci.yaml"

# OS-specific surfaces: a path fragment that names a platform, the files the
# OS lanes' selection depends on, and anything the installer / desktop
# updater lanes already fire for. A changed test file that carries an OS
# marker counts too (checked by content, see ``_is_os_marked_test``).
_OS_PATH_FRAGMENTS = (
    "windows", "win32", "winpty", "win_pty", "macos", "darwin", "powershell",
    "platform", "desktop-update", "install", "clipboard", "keychain",
    "launchd", "wsl", "msys", "appdata",
)
_OS_TESTS_FILES = {
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    "scripts/ci/list_os_marked_tests.py",
}
_OS_MARKER_RE = re.compile(r"\b(?:windows_only|macos_only)\b")

# Tree checks the fork gates: committed infographics (images) and profile
# archives. Mirrors the extensions those two checks reject.
_BINARY_ARTIFACT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tar.gz", ".tgz")

def _is_docs(p: str) -> bool:
    if p.startswith(("skills/", "optional-skills/")):
        return False
    return p.endswith((".md", ".mdx")) or p.startswith("docs/") or p.startswith("LICENSE")


def _is_nix(p: str) -> bool:
    return p.startswith(_NIX_PATHS) or p in _NIX_FILES


def _py_irrelevant(p: str) -> bool:
    if p.startswith(_PY_RELEVANT_SITE):
        return False
    return (
        _is_docs(p)
        or p in _ROOT_NPM
        or p.startswith(_PY_SKIP)
        or p.startswith(_DOCKER_META)
        or _is_nix(p)
    )


def _py_test_only(p: str) -> bool:
    """Is ``p`` inside the test suite (never shipped / imported by the product)?

    Product jobs (Desktop E2E's ``hermes serve`` backend, the Docker image)
    run installed code — nothing under ``tests/`` is packaged or importable
    there. scripts/run_tests.sh and run_tests_parallel.py are deliberately
    NOT test-only: they are runner infrastructure, and a bad edit there can
    mask real failures, so they stay conservative (python_prod=true).
    """
    return p.startswith("tests/")


def _is_scan(p: str) -> bool:
    return p.endswith(_SCAN_EXTS) or p in _SCAN_FILES


def _is_mcp_catalog(p: str) -> bool:
    return p.startswith(_MCP_CATALOG_PATHS) or p in _MCP_CATALOG_FILES


def _is_installer(p: str) -> bool:
    return p.startswith(_INSTALLER_PATHS) or p in _INSTALLER_FILES


def _is_desktop_updater(p: str) -> bool:
    return (
        p.startswith(_DESKTOP_UPDATER_PATHS)
        or p.startswith(_DESKTOP_UPDATER_TEST_PREFIX)
        or p in _DESKTOP_UPDATER_FILES
    )


def _is_rust(p: str) -> bool:
    return (
        p.endswith(".rs")
        or p.startswith(_RUST_PATHS)
        or os.path.basename(p) in _RUST_FILENAMES
    )


def _is_os_marked_test(p: str, root: Path) -> bool:
    """Does the changed test file itself carry an OS marker?

    Reads only the changed file (never the whole tree), so a diff of N files
    costs N small reads at most. A file that is gone from the checkout was
    deleted by the diff; a deletion cannot break the OS lanes (their
    zero-tests guard covers the last marked file), so it is not marked.
    """
    if not (p.startswith("tests/") and p.endswith(".py")):
        return False
    try:
        text = (root / p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_OS_MARKER_RE.search(text))


def _is_os_specific(p: str, root: Path) -> bool:
    """Could ``p`` change what the macOS / Windows pytest lanes exercise?

    Frontend and prose trees never can (they are not Python), whatever their
    name says — ``apps/bootstrap-installer`` is TypeScript.
    """
    if _is_installer(p) or _is_desktop_updater(p) or p in _OS_TESTS_FILES:
        return True
    if _py_irrelevant(p):
        return False
    lowered = p.lower()
    return any(fragment in lowered for fragment in _OS_PATH_FRAGMENTS) or _is_os_marked_test(p, root)


def _is_binary_artifact(p: str) -> bool:
    return p.lower().endswith(_BINARY_ARTIFACT_SUFFIXES)


def _is_workflow(p: str) -> bool:
    return p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))


def _ci_called_workflow_lanes(root: Path) -> dict[str, set[str]] | None:
    """Map each workflow ci.yaml calls to the detect lanes gating that call.

    ``{'.github/workflows/tests.yml': {'python'}, ...}``. A called workflow
    whose job has no lane condition maps to an empty set. Returns ``None``
    when ci.yaml cannot be parsed (no PyYAML, unreadable, malformed) so the
    caller falls back to the upstream fail-open rule.
    """
    try:
        import yaml
    except ImportError:
        return None
    try:
        ci = yaml.safe_load((root / _CI_ORCHESTRATOR).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    jobs = ci.get("jobs") if isinstance(ci, dict) else None
    if not isinstance(jobs, dict):
        return None
    lanes: dict[str, set[str]] = {}
    for job in jobs.values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(uses, str) or not uses.startswith("./"):
            continue
        cond = job.get("if")
        gates = set(re.findall(r"needs\.detect\.outputs\.(\w+) == 'true'", cond)) if isinstance(cond, str) else set()
        lanes.setdefault(uses[2:], set()).update(gates)
    return lanes


def _is_ci_review(p: str) -> bool:
    if p in _CI_REVIEW_FILES or p.startswith(_CI_REVIEW_PATHS):
        return True
    # Any eslint config file at any path — eslint configs can define custom
    # fix functions that execute arbitrary code, so they all require review.
    return os.path.basename(p).startswith("eslint.config.")


def ci_review_files(files: list[str]) -> list[str]:
    """Return the CI-sensitive paths that need maintainer review."""
    return sorted({f.strip() for f in files if f.strip() and _is_ci_review(f.strip())})


def classify(files: list[str], *, fork: bool = False, root: Path | None = None) -> dict[str, bool]:
    """Map changed paths to ``{lane: should_run}``.

    ``fork`` enables the fork-only ``.github/`` refinement (see the module
    docstring); ``root`` is the checkout the changed paths are relative to
    (defaults to the repository this script lives in).
    """
    root = root or Path(__file__).resolve().parents[2]
    files = [f.strip() for f in files if f.strip()]

    # Fork: a workflow ci.yaml never calls (fork-agent-image.yml, the
    # scheduled / workflow_run automations) cannot change what CI runs, and a
    # called workflow only affects the lanes gating its call. Only ci.yaml
    # itself, composite actions, .github/scripts and anything unparseable
    # keep the upstream run-everything rule. Scoped workflow files leave the
    # per-path lane computation entirely: they are neither prose nor Python,
    # so the ``python`` denylist would otherwise keep every lane on for them.
    github = [f for f in files if f.startswith(".github/")]
    scoped: list[str] = []
    called: dict[str, set[str]] = {}
    if fork and github:
        parsed = _ci_called_workflow_lanes(root)
        if parsed is not None and all(_is_workflow(f) and f != _CI_ORCHESTRATOR for f in github):
            called, scoped, github = parsed, github, []
    lane_files = [f for f in files if f not in scoped]

    python = any(not _py_irrelevant(f) for f in lane_files)
    python_prod = any(not _py_irrelevant(f) and not _py_test_only(f) for f in lane_files)
    frontend = any(
        f.startswith(_FRONTEND) or f in _ROOT_NPM or f in _FRONTEND_FILES
        for f in lane_files
    )
    deps = any(f == "pyproject.toml" for f in lane_files)
    npm_lock = any(f.split("/")[-1] == "package-lock.json" for f in lane_files)
    docker_meta = any(f.startswith(_DOCKER_META) for f in lane_files)
    image_frontend = frontend and (
        not fork or any(f.startswith(_IMAGE_FRONTEND) or f in _ROOT_NPM for f in lane_files)
    )

    ret = {
        "python": python,
        "python_prod": python_prod,
        "docker": docker_meta or python_prod or image_frontend,
        "docker_meta": docker_meta,
        "frontend": frontend,
        "site": any(f.startswith(_SITE) for f in lane_files),
        "scan": any(_is_scan(f) for f in lane_files),
        "deps": deps,
        "uv_lock": any(f in ("pyproject.toml", "uv.lock") for f in lane_files),
        "npm_lock": npm_lock,
        "installer": any(_is_installer(f) for f in lane_files),
        "desktop_updater": any(_is_desktop_updater(f) for f in lane_files),
        "rust": any(_is_rust(f) for f in lane_files),
        "mcp_catalog": any(_is_mcp_catalog(f) for f in lane_files),
        "ci_review": any(_is_ci_review(f) for f in files),
        "nix": python_prod or frontend or any(_is_nix(f) for f in lane_files),
        "os_tests": any(_is_os_specific(f, root) for f in lane_files),
        "binary_artifacts": any(_is_binary_artifact(f) for f in lane_files),
    }
    for f in scoped:
        for lane in called.get(f, set()):
            if lane in ret:
                ret[lane] = True
        if f == _FORK_IMAGE_WORKFLOW:
            # The one .github file that is an image input (the build runs it).
            ret["docker"] = True
    if not files or github:
        ret["python"] = True
        ret["python_prod"] = True
        ret["docker"] = True
        ret["docker_meta"] = True
        ret["frontend"] = True
        ret["site"] = True
        ret["scan"] = True
        ret["deps"] = True
        ret["uv_lock"] = True
        ret["npm_lock"] = True
        ret["installer"] = True
        ret["desktop_updater"] = True
        ret["rust"] = True
        ret["nix"] = True
        ret["ci_review"] = True
        ret["os_tests"] = True
        ret["binary_artifacts"] = True
        if fork and files:
            # .github/ is in .dockerignore: a CI-only diff cannot change the
            # image, so the fork does not rebuild and republish it for one.
            ret["docker"] = any(f == _FORK_IMAGE_WORKFLOW or not f.startswith(".github/") for f in files)

        # explicitly skip mcp catalog here. it's not needed unless those files are modified.
    return ret


def _pull_request_number() -> str | None:
    """Read the PR number from the Actions event payload, if present."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    number = (payload.get("pull_request") or {}).get("number")
    return str(number) if number else None


def pull_request_changed_files() -> list[str]:
    """Recover the PR file list when the compare API returned nothing.

    ``detect-changes`` calls ``repos/.../compare/base...head`` with raw SHAs.
    A fork force-push can 404 for ~30s until GitHub attaches the new head SHA
    to the base repo, so the action fails open with an empty file list. That
    forces ``ci_review=true`` and blocks the PR on a ``ci-reviewed`` label
    even when no CI-sensitive file changed.

    The pull-request files endpoint already knows the PR's files (it is how
    this action used to classify), so use it as a fallback on pull_request
    events only. Push/dispatch keep the empty-diff fail-open.
    """
    if os.environ.get("EVENT_NAME") != "pull_request":
        return []
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    pr = _pull_request_number()
    if not repo or not pr:
        return []
    try:
        completed = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/pulls/{pr}/files",
                "--jq",
                ".[].filename",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def is_fork(argv: list[str] | None = None, environ: "os._Environ[str] | dict[str, str] | None" = None) -> bool:
    """``--fork`` on the command line, or REPO / GITHUB_REPOSITORY naming the fork."""
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if environ is None else environ
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    return "--fork" in argv or repo == FORK_REPOSITORY


def main() -> int:
    files = sys.stdin.read().splitlines()
    if not any(f.strip() for f in files):
        recovered = pull_request_changed_files()
        if recovered:
            print(
                f"compare API returned no files; recovered {len(recovered)} "
                "path(s) from the pull request files endpoint",
                file=sys.stderr,
            )
            files = recovered
    lanes = classify(files, fork=is_fork())
    out = "\n".join([
        *(f"{key}={str(value).lower()}" for key, value in lanes.items()),
        f"ci_review_files={json.dumps(ci_review_files(files))}",
    ])
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(out + "\n")
    print(out)  # echo for local runs + CI step logs
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
