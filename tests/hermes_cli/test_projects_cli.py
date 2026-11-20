"""Tests for the `hermes project` CLI dispatch (hermes_cli/projects_cmd)."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


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


@pytest.mark.parametrize("board", ["missing-board", "bad/board"])
def test_create_rejects_unknown_board_without_creating_project(tmp_path, board):
    assert _run(["create", "Widget", str(tmp_path), "--board", board]) == 2
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget") is None


@pytest.mark.parametrize("board", ["missing-board", "bad/board"])
def test_bind_board_rejects_unknown_board_and_keeps_existing_binding(tmp_path, board):
    assert _run(["create", "Widget", str(tmp_path), "--board", "default"]) == 0
    assert _run(["bind-board", "widget", board]) == 2
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug == "default"


def test_bind_existing_board_and_explicit_unbind(tmp_path):
    kb.create_board("named-board")
    assert _run(["create", "Widget", str(tmp_path), "--board", "named-board"]) == 0
    assert _run(["bind-board", "widget", "default"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug == "default"
    assert _run(["bind-board", "widget", "named-board"]) == 0
    assert _run(["bind-board", "widget"]) == 0
    with pdb.connect_closing() as conn:
        assert pdb.get_project(conn, "widget").board_slug is None
