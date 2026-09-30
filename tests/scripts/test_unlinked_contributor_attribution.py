from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/audit_pr_attribution.py'


def module():
    spec = importlib.util.spec_from_file_location('unlinked_attribution_gate', SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Raw Contributor')
    git(tmp_path, 'config', 'user.email', 'raw@example.test')
    (tmp_path / 'payload').write_text('source')
    git(tmp_path, 'add', 'payload')
    git(tmp_path, 'commit', '-qm', 'import fixture')
    sha = git(tmp_path, 'rev-parse', 'HEAD')
    records = tmp_path / 'contributors/unlinked'
    records.mkdir(parents=True)
    path = records / 'raw@example.test.json'
    record = {'email': 'raw@example.test', 'commits': [{
        'sha': sha, 'name': 'Raw Contributor',
        'source_url': f'https://github.com/NousResearch/hermes-agent/commit/{sha}',
        'reason': 'Verified source commit has no linked GitHub author; preserve raw attribution.',
    }]}
    path.write_text(json.dumps(record))
    return tmp_path, path, record, sha


def test_exact_declared_source_identity_is_accepted(source):
    repo, _, _, sha = source
    assert module().verified_unlinked_author('raw@example.test', sha, repo=repo)


@pytest.mark.parametrize('change', ['sha', 'name', 'email', 'url', 'empty_reason', 'duplicate'])
def test_mismatched_or_ambiguous_record_fails_closed(source, change):
    repo, path, record, sha = source
    if change == 'email':
        record['email'] = 'somebody-else@example.test'
    elif change == 'duplicate':
        record['commits'].append(record['commits'][0].copy())
    else:
        item = record['commits'][0]
        if change == 'sha':
            item['sha'] = 'a' * 40
        elif change == 'name':
            item['name'] = 'Wrong Person'
        elif change == 'url':
            item['source_url'] = f'https://example.test/commit/{sha}'
        else:
            item['reason'] = ''
    path.write_text(json.dumps(record))
    assert not module().verified_unlinked_author('raw@example.test', sha, repo=repo)


def test_same_email_on_a_new_local_commit_is_not_whitelisted(source):
    repo, _, _, _ = source
    (repo / 'payload').write_text('new local change')
    git(repo, 'commit', '-qam', 'not the declared source commit')
    new_sha = git(repo, 'rev-parse', 'HEAD')
    assert not module().verified_unlinked_author('raw@example.test', new_sha, repo=repo)


def test_missing_and_malformed_records_are_rejected(source):
    repo, path, _, sha = source
    path.write_text('not-json')
    assert not module().verified_unlinked_author('raw@example.test', sha, repo=repo)
    path.unlink()
    assert not module().verified_unlinked_author('raw@example.test', sha, repo=repo)


def test_unlinked_record_never_invents_a_github_mention():
    from scripts.releases.authors import resolve_author
    assert resolve_author('doojiang', 'doojiang@tencent.com') == 'doojiang'
