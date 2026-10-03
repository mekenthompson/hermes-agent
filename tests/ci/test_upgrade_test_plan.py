"""CI partitions preserve coverage and avoid sharing the long install fixtures."""

import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/upgrade_test_plan.py"
plan = runpy.run_path(str(SCRIPT))["upgrade_matrix"]


@pytest.mark.parametrize("repository", ["mekenthompson/hermes-agent", "NousResearch/hermes-agent"])
def test_partition_is_disjoint_and_covers_new_files_and_suites(tmp_path, repository):
    inventory = [
        "test_upgrade_path.py", "test_other.py", "test_new.py",
        "git/test_shallow_install.py", "git/test_other.py", "git/test_new.py",
        "new-suite/nested/test_new.py", "handoff/test_existing.py",
    ]
    for relative in inventory:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    matrix = plan(tmp_path, repository)["include"]
    selected = [file for job in matrix for file in job["files"]]
    expected = inventory if repository != "mekenthompson/hermes-agent" else [
        file for file in inventory if not file.startswith("handoff/")
    ]
    assert sorted(selected) == sorted(expected)
    assert len(selected) == len(set(selected))
    assert all(job["files"] for job in matrix)
    if repository == "mekenthompson/hermes-agent":
        for long in ("test_upgrade_path.py", "git/test_shallow_install.py"):
            assert next(job["files"] for job in matrix if long in job["files"]) == [long]
    else:
        assert {job["shard"] for job in matrix} == {"core", "git", "new-suite", "handoff"}


def test_empty_inventory_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="no test files"):
        plan(tmp_path, "mekenthompson/hermes-agent")


def test_cli_output_drives_matrix_without_a_second_discovery(tmp_path):
    output = tmp_path / "output"
    child = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=ROOT, check=True,
        env={**os.environ, "GITHUB_REPOSITORY": "mekenthompson/hermes-agent", "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True,
    )
    assert output.read_text() == child.stdout
    value = json.loads(output.read_text().removeprefix("shards="))
    inventory = ROOT / "tests/e2e/core/upgrade"
    selected = [file for job in value["include"] for file in job["files"]]
    expected = [path.relative_to(inventory).as_posix() for path in inventory.rglob("test_*.py")
                if "handoff" not in path.relative_to(inventory).parts]
    assert sorted(selected) == sorted(expected)
    assert len(selected) == len(set(selected))


def _matrix_choices(path, job):
    workflow = YAML(typ="base").load((ROOT / path).read_text())
    expression = workflow["jobs"][job]["strategy"]["matrix"]
    return [json.loads(value) for value in re.findall(r"'(\{.*?\})'", expression)]


def test_general_e2e_real_inventory_is_run_once_by_fork_shards():
    runner = runpy.run_path(str(ROOT / "scripts/run_tests_parallel.py"))
    files = sorted(path for path in (ROOT / "tests/e2e").rglob("test_*.py")
                   if not path.is_relative_to(ROOT / "tests/e2e/core/upgrade"))
    assert files
    for matrix in _matrix_choices(".github/workflows/tests.yml", "e2e"):
        selected = [path for index in matrix["slice"] for path in runner["_slice_files"](
            files, index, matrix["slices"][0], {}, ROOT)]
        assert sorted(selected) == files
        assert len(selected) == len(set(selected))


def test_fork_windows_matrix_does_not_run_the_x64_fallback_twice():
    fork, upstream = _matrix_choices(".github/workflows/tests-os.yml", "os-tests")
    fork_windows = [row for row in fork["include"] if row["marker"] == "windows"]
    assert len(fork_windows) == 1
    assert "arm" not in fork_windows[0]["name"].lower()
    assert any("arm" in row["runner"] for row in upstream["include"])
