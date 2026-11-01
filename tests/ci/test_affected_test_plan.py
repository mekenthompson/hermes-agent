"""Behavior contracts for conservative fork test selection."""

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci import classify_changes
from scripts.ci.affected_test_plan import python_plan


def test_standalone_test_selects_one_file_and_one_shard(tmp_path, monkeypatch, capsys):
    target = tmp_path / "tests" / "test_example.py"
    target.parent.mkdir()
    target.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "mekenthompson/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO("tests/test_example.py\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["python_files"]) == ["tests/test_example.py"]
    assert json.loads(outputs["python_matrix"]) == {"slice": [1], "slices": [1]}

@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("unit", "workspace"), [("web", "web"), ("tui", "ui-tui")])
def test_js_workflow_executes_only_selected_workspace(unit, workspace, tmp_path):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/js-tests.yml").read_text(encoding="utf-8-sig"))
    step = next(s for s in workflow["jobs"]["check"]["steps"] if s.get("name") == "Run all workspace checks")
    npm = tmp_path / "npm"
    npm.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*"\nexit 7\n', encoding="utf-8")
    npm.chmod(0o755)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "CI_FORK": "true", "CHECK_UNIT": unit}, capture_output=True, text=True)
    assert result.returncode == 7  # selected-check failure must propagate
    assert result.stdout.strip() == f"run --prefix {workspace} check"


def test_js_workflow_routes_matrix_and_runs_whatsapp_once():
    root = Path(__file__).resolve().parents[2]
    ci = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text(encoding="utf-8-sig"))
    workflow = yaml.safe_load((root / ".github/workflows/js-tests.yml").read_text(encoding="utf-8-sig"))
    assert ci["jobs"]["js-tests"]["with"]["js_matrix"] == "${{ needs.detect.outputs.js_matrix }}"
    assert "inputs.js_matrix" in workflow["jobs"]["check"]["strategy"]["matrix"]
    for name in ("Install WhatsApp bridge dependencies", "Run WhatsApp bridge node tests"):
        step = next(s for s in workflow["jobs"]["check"]["steps"] if s.get("name") == name)
        assert step["if"] == "matrix.unit == 'all' || matrix.unit == 'checks' || matrix.unit == 'whatsapp'"



def test_workflow_threads_selected_files_and_dynamic_matrix():
    root = Path(__file__).resolve().parents[2]
    ci = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text(encoding="utf-8-sig"))
    tests = yaml.safe_load((root / ".github/workflows/tests.yml").read_text(encoding="utf-8-sig"))
    action = yaml.safe_load((root / ".github/actions/detect-changes/action.yml").read_text(encoding="utf-8-sig"))
    for key in ("python_files", "python_matrix"):
        assert key in action["outputs"]
        assert key in ci["jobs"]["detect"]["outputs"]
        assert ci["jobs"]["tests"]["with"][key] == "${{ needs.detect.outputs." + key + " }}"
    assert "inputs.python_matrix" in tests["jobs"]["test"]["strategy"]["matrix"]
    step = next(s for s in tests["jobs"]["test"]["steps"] if s.get("name") == "Run tests")
    assert "--files-from" in step["run"]
    assert step["env"]["PYTHON_FILES"] == "${{ inputs.python_files }}"


@pytest.mark.parametrize("changed", [[], ["tests/conftest.py"], ["tests/helpers/test_helper.py"],
    ["tests/test_deleted.py"], ["agent/run.py"], [".github/workflows/tests.yml"],
    ["skills/sample/SKILL.md"], ["website/docs/guide.md"]])
def test_unproven_inputs_retain_full_suite(changed, tmp_path):
    plan = python_plan(changed, root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == []
    assert plan["python_matrix"]["slices"] == [16]


@pytest.mark.parametrize("relative", ["tests/test_helper.py", "tests/test_shared.py", "tests/test_fixtures.py"])
def test_helper_named_modules_do_not_narrow(relative, tmp_path):
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_text("def test_example(): pass\n", encoding="utf-8")
    assert python_plan([relative], root=tmp_path, fork=True, enabled=True)["python_files"] == []


@pytest.mark.parametrize("count, shards", [(1, 1), (20, 1), (21, 2), (41, 3)])
def test_selected_shards_are_small_complete_and_cache_independent(count, shards, tmp_path):
    from scripts.run_tests_parallel import _slice_files
    tests = tmp_path / "tests"
    tests.mkdir()
    files = [f"tests/test_case_{index:04d}.py" for index in range(count)]
    for name in files:
        (tmp_path / name).write_text("def test_ok(): pass\n", encoding="utf-8")
    plan = python_plan(files, root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == files
    assert plan["python_matrix"]["slices"] == [shards]
    paths = [tmp_path / name for name in files]
    selected = [p for index in range(1, shards + 1)
        for p in _slice_files(paths, index, shards, {files[0]: index * 9000}, tmp_path)]
    assert sorted(selected) == sorted(paths)
    assert len(selected) == len(set(selected))


def test_exact_oauth_document_selects_verified_consumer(tmp_path):
    document = "website/static/oauth/client-metadata.json"
    consumer = "tests/tools/test_mcp_cimd.py"
    for name in (document, consumer):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    plan = python_plan([document], root=tmp_path, fork=True, enabled=True)
    assert plan["python_files"] == [consumer]
    assert plan["python_matrix"]["slices"] == [1]
    # Missing/deleted data or consumer must broaden, never become an empty lane.
    (tmp_path / consumer).unlink()
    assert python_plan([document], root=tmp_path, fork=True, enabled=True)["python_files"] == []


def test_imported_test_retains_full_suite(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("from tests.test_a import test_a\n", encoding="utf-8")
    assert python_plan(["tests/test_a.py"], root=tmp_path, fork=True, enabled=True)["python_files"] == []



@pytest.mark.parametrize(("paths", "units"), [
    (["web/src/App.tsx"], ["web"]),
    (["ui-tui/src/entry.tsx"], ["tui"]),
    (["scripts/whatsapp-bridge/index.mjs"], ["whatsapp"]),
    (["web/src/App.tsx", "scripts/whatsapp-bridge/index.mjs"], ["web", "whatsapp"]),
    (["ui-tui/src/lib/pythonHelpers.ts"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
    (["apps/shared/src/client.ts"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
    (["package-lock.json"], ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]),
])
def test_js_selection_isolated_sources_and_shared_fallback(paths, units, monkeypatch, capsys):
    monkeypatch.setenv("REPO", "mekenthompson/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO("\n".join(paths)))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["js_matrix"]) == {"unit": units}


def test_fork_empty_exact_diff_cannot_be_narrowed_by_live_pr_files(monkeypatch, capsys):
    monkeypatch.setenv("REPO", "mekenthompson/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(classify_changes, "pull_request_changed_files", lambda: ["README.md"])
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    assert "python=true" in capsys.readouterr().out


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
@pytest.mark.parametrize("changed", ["tests/test_example.py", "web/src/App.tsx"])
def test_non_pull_request_events_always_plan_the_full_suite(event, changed, tmp_path, monkeypatch, capsys):
    target = tmp_path / "tests" / "test_example.py"
    target.parent.mkdir()
    target.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "mekenthompson/hermes-agent")
    monkeypatch.setenv("EVENT_NAME", event)
    monkeypatch.setattr(classify_changes.sys, "stdin", io.StringIO(changed + "\n"))
    monkeypatch.setattr(classify_changes, "pull_request_labels", list)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert classify_changes.main() == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert json.loads(outputs["python_files"]) == []
    if outputs["python"] == "true":
        assert json.loads(outputs["python_matrix"]) == {"slice": list(range(1, 17)), "slices": [16]}
    assert json.loads(outputs["js_matrix"]) == {"unit": ["checks", "ui-1/3", "ui-2/3", "ui-3/3"]}
