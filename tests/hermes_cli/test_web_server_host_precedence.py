"""Host-readable precedence remains independent of Docker translation fixtures."""

import json

import pytest

from hermes_cli import web_server_files


@pytest.mark.platforms("posix")
def test_readable_host_file_wins_over_docker_volume(monkeypatch, tmp_path):
    host = tmp_path / "host"
    container = tmp_path / "container"
    host.mkdir()
    container.mkdir()
    (host / "note.txt").write_text("container copy")
    target = container / "note.txt"
    target.write_text("host copy")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps([f"{host}:{container}"]))
    for raw in (str(target), target.as_uri()):
        resolved = web_server_files._agent_fs_path(raw)
        assert resolved == target
        assert resolved.read_text() == "host copy"


def test_translation_fixture_refuses_non_scratch_staging(absent_container_host_paths, tmp_path):
    from hermes_cli.web_routers import files

    with pytest.raises(AssertionError):
        files.tempfile.mkstemp(dir=str(tmp_path.parent))
