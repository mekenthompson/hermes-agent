"""PR detection must never turn an incomplete diff into a narrow CI selection."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "pr_changed_files", ROOT / "scripts/ci/pr_changed_files.py"
)
assert SPEC and SPEC.loader
pr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr
SPEC.loader.exec_module(pr)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_old.py").write_text(
        'import pytest\n@pytest.mark.platforms("macos")\ndef test_x(): pass\n'
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def test_exact_local_diff_selects_modified_paths(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    (repo / "agent").mkdir()
    (repo / "agent/runtime.py").write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")
    assert pr.changed_files(
        base, head, "example/repo", repo, compare=lambda *_: pytest.fail("API used")
    ) == ["agent/runtime.py"]


def test_diverged_pr_excludes_base_only_workflow_changes(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    workflow = repo / ".github/workflows/ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: original\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "common ancestor")
    common = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "tests/test_old.py").write_text("def test_x(): assert True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "test-only PR")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--detach", common)
    workflow.write_text("name: changed only on base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base advanced")
    base = _git(repo, "rev-parse", "HEAD")
    assert pr.changed_files(
        base, head, "example/repo", repo, compare=lambda *_: pytest.fail("API used")
    ) == ["tests/test_old.py"]


def test_unavailable_merge_base_uses_complete_api_fallback(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "commit", "-qm", "unrelated history")
    head = _git(repo, "rev-parse", "HEAD")
    seen = []

    def compare(repository: str, old: str, new: str) -> dict:
        seen.append((repository, old, new))
        return {
            "status": "ahead",
            "files": [{"status": "modified", "filename": "agent/turn.py"}],
        }

    assert pr.changed_files(base, head, "example/repo", repo, compare=compare) == [
        "agent/turn.py"
    ]
    assert seen == [("example/repo", base, head)]


def test_exact_local_diff_is_complete_past_api_cap(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    for i in range(301):
        (repo / f"file_{i}.py").write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    files = pr.changed_files(
        base,
        _git(repo, "rev-parse", "HEAD"),
        "example/repo",
        repo,
        compare=lambda *_: pytest.fail("capped API used"),
    )
    assert len(files) == 301
    assert "file_300.py" in files


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_removed_old_test_fails_open_even_with_exact_git_diff(
    tmp_path: Path, operation: str
) -> None:
    repo, base = _repo(tmp_path)
    old = repo / "tests/test_old.py"
    if operation == "delete":
        old.unlink()
    else:
        old.rename(repo / "tests/test_new.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    with pytest.raises(pr.FailOpen):
        pr.changed_files(base, _git(repo, "rev-parse", "HEAD"), "example/repo", repo)


def test_compare_cap_and_missing_or_failed_api_fail_open(tmp_path: Path) -> None:
    base, head = "a" * 40, "b" * 40
    for payload in (
        {
            "status": "diverged",
            "files": [{"status": "modified", "filename": "x.py"} for _ in range(300)],
        },
        {"status": "diverged", "files": None},
        {"status": "behind", "files": [{"status": "modified", "filename": "x.py"}]},
        {"status": "diverged", "files": [{"status": "added", "filename": " x.py"}]},
        {
            "status": "diverged",
            "files": [{"status": "removed", "filename": "tests/test_old.py"}],
        },
        {
            "status": "diverged",
            "files": [
                {
                    "status": "renamed",
                    "filename": "tests/test_new.py",
                    "previous_filename": "tests/test_old.py",
                }
            ],
        },
    ):
        with pytest.raises(pr.FailOpen):
            pr.changed_files(
                base, head, "example/repo", tmp_path, compare=lambda *_: payload
            )
    with pytest.raises(pr.FailOpen):
        pr.changed_files(
            base,
            head,
            "example/repo",
            tmp_path,
            compare=lambda *_: (_ for _ in ()).throw(RuntimeError("HTTP 500")),
        )


def test_complete_api_fallback_keeps_exact_range_and_known_status(
    tmp_path: Path,
) -> None:
    base, head = "a" * 40, "b" * 40
    seen: list[tuple[str, str, str]] = []

    def compare(repo: str, old: str, new: str) -> dict:
        seen.append((repo, old, new))
        return {
            "status": "diverged",
            "files": [{"status": "modified", "filename": "agent/turn.py"}],
        }

    assert pr.changed_files(base, head, "example/repo", tmp_path, compare=compare) == [
        "agent/turn.py"
    ]
    assert seen == [("example/repo", base, head)]


def test_detector_entrypoint_prints_no_paths_when_event_shas_are_missing(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    monkeypatch.setenv("REPO", "example/repo")
    assert pr.main() == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "all lanes run" in err


def test_composite_action_python_entrypoints_run_from_repo_root() -> None:
    env = {**os.environ, "BASE_SHA": "", "HEAD_SHA": "", "REPO": "example/repo"}
    detector = subprocess.run(
        [sys.executable, "-m", "scripts.ci.pr_changed_files"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert detector.stdout == ""
    assert "all lanes run" in detector.stderr
    classifier = subprocess.run(
        [sys.executable, "-m", "scripts.ci.classify_changes", "--fork"],
        cwd=ROOT,
        env=env,
        input="hermes_cli/update_cmd.py\n",
        check=True,
        capture_output=True,
        text=True,
    )
    assert "os_tests=true" in classifier.stdout
    assert "upgrade=true" in classifier.stdout


def test_composite_action_fails_open_on_unavailable_exact_range(tmp_path: Path) -> None:
    from ruamel.yaml import YAML

    action = YAML(typ="base").load(
        (ROOT / ".github/actions/detect-changes/action.yml").read_text()
    )
    output = tmp_path / "github-output"
    same_sha = "a" * 40
    env = {
        **os.environ,
        "EVENT_NAME": "pull_request",
        "REPO": "example/repo",
        "BASE_SHA": same_sha,
        "HEAD_SHA": same_sha,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", action["runs"]["steps"][0]["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "all lanes run" in result.stderr
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert selected["python"] == "true"
    assert selected["installer"] == "true"
    assert selected["upgrade"] == "true"
