"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb
from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def isolated_project_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()


@pytest.mark.parametrize("board", ["missing-board", "bad/board"])
def test_create_rejects_bad_board_without_creating_project(tmp_path, board):
    assert _run(["create", "Widget", str(tmp_path), "--board", board]) != 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget") is None


@pytest.mark.parametrize("board", ["missing-board", "bad/board"])
def test_failed_bind_preserves_existing_binding(tmp_path, board):
    assert _run(["create", "Widget", str(tmp_path), "--board", "default"]) == 0
    assert _run(["bind-board", "widget", board]) != 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug == "default"


@pytest.mark.parametrize("board", ["default", "named-board"])
def test_valid_binding_and_explicit_unbind(tmp_path, board):
    if board != "default":
        kb.create_board(board)
    assert _run(["create", "Widget", str(tmp_path)]) == 0
    assert _run(["bind-board", "widget", board]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug == board
    assert kb.read_board_metadata(board)["default_workdir"] == str(tmp_path.resolve())
    assert _run(["bind-board", "widget"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug is None



def _run(argv):
    """Build the project subparser, parse argv, and dispatch. Returns rc."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    p = projects_cmd.build_parser(sub)
    p.set_defaults(func=projects_cmd.projects_command)
    args = parser.parse_args(["project", *argv])
    return projects_cmd.projects_command(args)


def test_create_list_show(capsys, tmp_path):
    assert _run(["create", "My App", str(tmp_path), "--use"]) == 0
    out = capsys.readouterr().out
    assert "Created project" in out

    with pdb.connect_closing() as conn:
        projects = pdb.list_projects(conn)
        assert len(projects) == 1
        assert projects[0].name == "My App"
        # --use set it active.
        assert pdb.get_active_id(conn) == projects[0].id

    assert _run(["list"]) == 0
    assert "my-app" in capsys.readouterr().out

    assert _run(["show", "my-app"]) == 0
    assert "My App" in capsys.readouterr().out




def test_rename_and_archive(tmp_path):
    _run(["create", "Old Name", str(tmp_path)])
    assert _run(["rename", "old-name", "New Name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "old-name").name == "New Name"

    assert _run(["archive", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.list_projects(conn) == []
        assert len(pdb.list_projects(conn, include_archived=True)) == 1

    assert _run(["restore", "old-name"]) == 0
    with pdb.connect_closing() as conn:
        assert len(pdb.list_projects(conn)) == 1




