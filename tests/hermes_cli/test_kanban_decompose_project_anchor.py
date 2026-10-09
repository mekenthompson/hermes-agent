"""Project decomposition on an unbound mixed-product board (HF-459)."""
from pathlib import Path
import subprocess
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw, projects_db as pdb
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.mark.parametrize('registry_available', [True, False])
def test_project_children_materialize_distinct_worktrees_without_board_default(tmp_path, monkeypatch, registry_available):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                               '-c', 'user.email=test@example.com',
                               '-c', 'commit.gpgsign=false', *args],
                              check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    (repo / 'README.md').write_text('base\n')
    git('add', 'README.md')
    git('commit', '-m', 'base')
    with pdb.connect_closing() as pc:
        project_id = pdb.create_project(pc, name='Product', primary_path=str(repo))
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='root', project_id=project_id, triage=True)
        if not registry_available:
            # A different executor profile lacks the creator's projects.db.
            monkeypatch.setattr(pdb, 'get_project', lambda *args: None)
        ids = decompose_triage_task(conn, root, root_assignee='default', children=[
            {'title': 'first', 'assignee': 'default'},
            {'title': 'second', 'assignee': 'default'},
        ], auto_promote=False)
        assert ids is not None
        tasks = [kb.get_task(conn, tid) for tid in ids]
        assert all(t is not None and t.project_id == project_id for t in tasks)
        paths = []
        for task in tasks:
            assert task is not None
            path, branch = kbw._resolve_worktree_workspace(task, board='default')
            paths.append(path)
            assert path == repo / '.worktrees' / task.id
            assert git('-C', str(path), 'branch', '--show-current') == branch
        assert len(set(paths)) == len(tasks)
        assert repo / '.worktrees' / root not in paths
        # Durable binding survives connection teardown, without a board-wide default.
    with kbc.connect_closing() as reopened:
        for tid, path in zip(ids, paths):
            task = kb.get_task(reopened, tid)
            assert task is not None
            assert kbw._resolve_worktree_workspace(task, board='default')[0] == path


def test_explicit_child_workspace_does_not_inherit_project(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    with pdb.connect_closing() as pc:
        project_id = pdb.create_project(pc, name='Product', primary_path=str(repo))
    override = str(tmp_path / 'other-repo')
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='root', project_id=project_id, triage=True)
        ids = decompose_triage_task(conn, root, root_assignee='default', children=[
            {'title': 'scratch', 'workspace_kind': 'scratch'},
            {'title': 'dir', 'workspace_kind': 'dir', 'workspace_path': override},
            {'title': 'worktree', 'workspace_kind': 'worktree', 'workspace_path': override},
        ], auto_promote=False)
        assert ids is not None
        for tid in ids:
            child = kb.get_task(conn, tid)
            assert child is not None
            assert child.project_id is None
            assert child.workspace_path == (None if child.workspace_kind == 'scratch' else override)
