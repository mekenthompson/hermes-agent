"""Fork change-scoping contract (mekenthompson/hermes-agent).

Covers what upstream's tests do not: the push-range helper, fork-mode
classification, the fork-only gates in ci.yaml, the image workflow's detect
gate, and the upstream-only guards on standalone automations. Everything here
is about the fork; upstream behaviour is pinned by the sibling test modules.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
FORK = "mekenthompson/hermes-agent"
UPSTREAM = "NousResearch/hermes-agent"


def _load(name: str):
    path = ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


classify_mod = _load("classify_changes")
push_mod = _load("push_changed_files")
classify = classify_mod.classify


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))



# ───────────────────────── push_changed_files ─────────────────────────

BEFORE, AFTER = "a" * 40, "b" * 40


def _compare(status="ahead", files=("hermes_cli/config.py",), renamed=None):
    entries = [{"filename": f} for f in files]
    if renamed:
        entries.append({"filename": renamed[1], "previous_filename": renamed[0]})
    return lambda before, after: {"status": status, "files": entries}


def test_push_range_lists_files_of_a_fast_forward_push():
    files = push_mod.changed_files(BEFORE, AFTER, "false", _compare(files=("b.py", "a.py"), renamed=("old.md", "new.md")))
    assert files == ["a.py", "b.py", "new.md", "old.md"]


@pytest.mark.parametrize(
    "before,after,forced,compare,reason",
    [
        ("0" * 40, AFTER, "false", _compare(), "first push"),
        ("", AFTER, "false", _compare(), "no previous"),
        (BEFORE, AFTER, "true", _compare(), "force push"),
        (BEFORE, BEFORE, "false", _compare(), "empty push"),
        (BEFORE, AFTER, "false", _compare(status="diverged"), "not a fast-forward"),
        (BEFORE, AFTER, "false", _compare(status="behind"), "not a fast-forward"),
        (BEFORE, AFTER, "false", _compare(files=()), "no files"),
        (BEFORE, AFTER, "false", _compare(files=tuple(f"f{i}.py" for i in range(push_mod.COMPARE_FILE_CAP))), "truncated"),
        (BEFORE, AFTER, "false", lambda b, a: {"files": None}, "no files"),
    ],
)
def test_push_range_fails_open_when_the_range_cannot_be_trusted(before, after, forced, compare, reason):
    with pytest.raises(push_mod.FailOpen):
        push_mod.changed_files(before, after, forced, compare, sleep=lambda _: None)


def test_push_range_retries_the_compare_once_then_fails_open():
    calls: list[int] = []

    def flaky(before, after):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 502")
        return {"status": "ahead", "files": [{"filename": "x.py"}]}

    slept: list[float] = []
    assert push_mod.changed_files(BEFORE, AFTER, "false", flaky, sleep=slept.append) == ["x.py"]
    assert slept == [5]

    def dead(before, after):
        raise RuntimeError("HTTP 502")

    with pytest.raises(push_mod.FailOpen):
        push_mod.changed_files(BEFORE, AFTER, "false", dead, sleep=lambda _: None)


def test_push_range_main_prints_nothing_and_exits_zero_on_fail_open(monkeypatch, capsys):
    monkeypatch.setenv("REPO", FORK)
    monkeypatch.setenv("PUSH_BEFORE", "0" * 40)
    monkeypatch.setenv("PUSH_AFTER", AFTER)
    monkeypatch.setenv("PUSH_FORCED", "false")
    assert push_mod.main() == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "::warning::" in err and "failing open" in err


def test_push_range_main_prints_the_files(monkeypatch, capsys):
    monkeypatch.setenv("REPO", FORK)
    monkeypatch.setenv("PUSH_BEFORE", BEFORE)
    monkeypatch.setenv("PUSH_AFTER", AFTER)
    monkeypatch.setenv("PUSH_FORCED", "false")
    monkeypatch.setattr(push_mod, "compare_via_gh", lambda repo, b, a: {"status": "ahead", "files": [{"filename": "docs/x.md"}]})
    assert push_mod.main() == 0
    assert capsys.readouterr().out.strip() == "docs/x.md"


def test_compare_via_gh_uses_the_three_dot_compare_endpoint():
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "ahead", "files": []}), stderr="")

    push_mod.compare_via_gh(FORK, BEFORE, AFTER, call=fake_run)
    assert seen == [["gh", "api", f"repos/{FORK}/compare/{BEFORE}...{AFTER}"]]

    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: Not Found")

    with pytest.raises(RuntimeError):
        push_mod.compare_via_gh(FORK, BEFORE, AFTER, call=failing)


# ───────────────────────── fork-mode classification ─────────────────────────

ALL_OFF = {lane: False for lane in classify(["README.md"])}


def _fork(files):
    return classify(files, fork=True)


def test_fork_mode_is_selected_by_repo_or_flag():
    assert classify_mod.is_fork(["--fork"], {})
    assert classify_mod.is_fork([], {"REPO": FORK})
    assert classify_mod.is_fork([], {"GITHUB_REPOSITORY": FORK})
    assert not classify_mod.is_fork([], {"REPO": UPSTREAM})


def test_fork_mode_matches_upstream_outside_dot_github():
    for files in (["README.md"], ["hermes_cli/config.py"], ["tests/agent/test_x.py"], ["ui-tui/src/a.ts"], ["pyproject.toml"], []):
        upstream = classify(files)
        fork = _fork(files)
        assert {k: v for k, v in fork.items() if k != "docker"} == {k: v for k, v in upstream.items() if k != "docker"}, files


def test_fork_docs_only_runs_nothing():
    assert _fork(["README.md", "docs/fork-agent-image.md"]) == ALL_OFF


def test_fork_standalone_workflow_change_runs_no_ci_lane():
    lanes = _fork([".github/workflows/fork-agent-image.yml"])
    assert lanes == {**ALL_OFF, "docker": True, "ci_review": True}
    lanes = _fork([".github/workflows/install-e2e.yml", "docs/x.md"])
    assert lanes == {**ALL_OFF, "ci_review": True}


def test_fork_called_workflow_change_runs_only_the_lanes_gating_it():
    ci = _yaml(".github/workflows/ci.yaml")
    for job in ci["jobs"].values():
        uses = job.get("uses", "")
        if not uses.startswith("./"):
            continue
        expected = set(re.findall(r"needs\.detect\.outputs\.(\w+) == 'true'", job.get("if", "") or ""))
        lanes = _fork([uses[2:]])
        on = {k for k, v in lanes.items() if v}
        assert on == expected | {"ci_review"} | ({"docker"} if "python_prod" in expected else set()), uses


def test_fork_orchestrator_actions_and_scripts_still_fail_open():
    for f in (".github/workflows/ci.yaml", ".github/actions/retry/action.yml", ".github/scripts/run-workspace-checks.mjs", ".github/CODEOWNERS"):
        lanes = _fork([f])
        assert lanes["python"] and lanes["frontend"] and lanes["rust"] and lanes["os_tests"], f
        # .github/ is dockerignored: a CI-only diff never rebuilds the image.
        assert lanes["docker"] is False, f
    assert _fork([".github/workflows/ci.yaml", "hermes_cli/x.py"])["docker"] is True
    assert _fork([])["docker"] is True


def test_fork_image_lane_tracks_what_the_dockerfile_copies():
    assert _fork(["apps/desktop/src/app.tsx"])["docker"] is False
    assert _fork(["apps/shared/src/x.ts"])["docker"] is True
    assert _fork(["web/src/x.ts"])["docker"] is True
    assert _fork(["package-lock.json"])["docker"] is True
    assert _fork(["tests/agent/test_x.py"])["docker"] is False
    assert _fork(["Dockerfile"])["docker"] is True
    assert _fork(["skills/github/SKILL.md"])["docker"] is True


def test_fork_falls_back_to_fail_open_when_ci_yaml_is_unparseable(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yaml").write_text("jobs: [not, a, map", encoding="utf-8")
    lanes = classify([".github/workflows/fork-agent-image.yml"], fork=True, root=tmp_path)
    assert lanes["python"] and lanes["frontend"]


def test_main_uses_fork_mode_from_the_repo_env(monkeypatch, capsys):
    monkeypatch.setenv("REPO", FORK)
    monkeypatch.setattr(sys, "argv", ["classify_changes.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(".github/workflows/fork-agent-image.yml\n"))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_mod.main() == 0
    out = capsys.readouterr().out
    assert "python=false" in out and "docker=true" in out


# ───────────────────────── workflow contracts ─────────────────────────

def test_ci_push_trigger_has_no_paths_filter_so_every_main_sha_gets_a_run():
    """fork-agent-image.yml waits for a successful ci.yaml run of the exact SHA."""
    on = _yaml(".github/workflows/ci.yaml")[True]
    assert on["push"] == {"branches": ["main"]}


def test_detect_and_aggregate_can_never_be_skipped():
    jobs = _yaml(".github/workflows/ci.yaml")["jobs"]
    assert "if" not in jobs["detect"]
    assert jobs["all-checks-pass"]["if"] == "always()"
    # Every lane job feeds the gate, so a skipped lane is counted (as a pass).
    gated = {name for name, job in jobs.items() if "needs.detect.outputs" in (job.get("if") or "")}
    # infographic-check has never been in the gate's needs upstream (it runs
    # but is not required); the fork does not change that.
    assert gated - set(jobs["all-checks-pass"]["needs"]) <= {"infographic-check"}


def test_all_checks_pass_treats_a_fully_skipped_run_as_green(tmp_path):
    policy = _load("ci_policy")
    needs = {"detect": {"result": "success"}, "tests": {"result": "skipped"}, "osv-scanner": {"result": "skipped"}}
    assert policy.evaluate_needs(needs).failed == []


def test_fork_only_gates_in_ci_yaml():
    jobs = _yaml(".github/workflows/ci.yaml")["jobs"]
    fork_guard = "github.repository != 'mekenthompson/hermes-agent'"
    expected = {
        "tests-os": "needs.detect.outputs.os_tests == 'true'",
        "docs-site": None,
        "contributor-check": "needs.detect.outputs.event_name == 'pull_request'",
        "infographic-check": "needs.detect.outputs.binary_artifacts == 'true'",
        "profile-artifact-check": "needs.detect.outputs.binary_artifacts == 'true'",
        "osv-scanner": "needs.detect.outputs.uv_lock == 'true'",
    }
    for name, fork_condition in expected.items():
        cond = jobs[name]["if"]
        assert fork_guard in cond, name
        if fork_condition:
            assert fork_condition in cond, name
    assert jobs["osv-scanner"]["needs"] == "detect"
    # Dependency PRs keep the OSV scan.
    assert "needs.detect.outputs.npm_lock == 'true'" in jobs["osv-scanner"]["if"]
    assert "needs.detect.outputs.deps == 'true'" in jobs["osv-scanner"]["if"]
    # Upstream: the lanes still key on the upstream outputs alone.
    assert jobs["tests-os"]["if"].startswith("needs.detect.outputs.python == 'true' &&")
    assert jobs["docs-site"]["if"].startswith("needs.detect.outputs.site == 'true' &&")


def test_detect_declares_every_lane_the_fork_gates_read():
    ci = _yaml(".github/workflows/ci.yaml")
    action = _yaml(".github/actions/detect-changes/action.yml")
    for lane in ("os_tests", "binary_artifacts"):
        assert lane in ci["jobs"]["detect"]["outputs"]
        assert lane in action["outputs"]


def test_detect_changes_scopes_fork_pushes_only():
    text = (ROOT / ".github/actions/detect-changes/action.yml").read_text(encoding="utf-8")
    run = yaml.safe_load(text)["runs"]["steps"][0]
    assert run["env"]["PUSH_BEFORE"] == "${{ github.event.before }}"
    assert run["env"]["PUSH_AFTER"] == "${{ github.sha }}"
    assert run["env"]["PUSH_FORCED"] == "${{ github.event.forced }}"
    script = run["run"]
    assert 'elif [ "$EVENT_NAME" = "push" ] && [ "$REPO" = "mekenthompson/hermes-agent" ]; then' in script
    assert 'CHANGED="$(python3 scripts/ci/push_changed_files.py)"' in script
    # The PR branch is untouched.
    assert 'if [ "$EVENT_NAME" = "pull_request" ]; then' in script


def test_fork_image_workflow_gates_build_and_publish_on_the_docker_lane():
    wf = _yaml(".github/workflows/fork-agent-image.yml")
    detect = wf["jobs"]["detect"]
    assert detect["if"] == "github.repository == 'mekenthompson/hermes-agent'"
    assert detect["outputs"] == {"docker": "${{ steps.classify.outputs.docker }}"}
    assert detect["permissions"] == {"contents": "read"}
    assert [s.get("uses") for s in detect["steps"]][1] == "./.github/actions/detect-changes"
    for job in ("preflight", "publish"):
        assert wf["jobs"][job]["needs"] == "detect"
        assert wf["jobs"][job]["if"].lstrip().startswith("needs.detect.outputs.docker == 'true' &&")
    # detect must not gain registry or attestation rights.
    text = (ROOT / ".github/workflows/fork-agent-image.yml").read_text(encoding="utf-8")
    detect_block = text.split("\n  detect:\n", 1)[1].split("\n  preflight:\n", 1)[0]
    for scope in ("packages: write", "id-token: write", "attestations: write"):
        assert scope not in detect_block


def test_fork_python_lane_runs_eight_slices():
    text = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if "matrix:" in x)
    fork = json.loads(re.findall(r"'(\{.*?\})'", line)[0])
    assert fork == {"slice": [1, 2, 3, 4, 5, 6, 7, 8], "slices": [8]}


@pytest.mark.parametrize(
    "workflow,job",
    [
        ("install-e2e.yml", "pick-releases"),
        ("js-autofix.yml", "generate-patch"),
        ("ci-review-comment.yml", "comment"),
        ("publish-e2e-evidence.yml", "publish"),
        ("nix.yml", "detect"),
        ("docker.yml", "detect"),
        ("skills-index-freshness.yml", "check-freshness"),
        ("skills-index.yml", "build-index"),
    ],
)
def test_standalone_automations_are_upstream_only(workflow, job):
    cond = _yaml(f".github/workflows/{workflow}")["jobs"][job]["if"]
    assert "github.repository == 'NousResearch/hermes-agent'" in " ".join(cond.split())


def test_install_e2e_stays_dispatchable_from_the_fork():
    cond = _yaml(".github/workflows/install-e2e.yml")["jobs"]["pick-releases"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in cond
