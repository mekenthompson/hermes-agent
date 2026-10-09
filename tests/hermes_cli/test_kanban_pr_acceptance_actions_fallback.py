"""Actions evidence may satisfy a Checks-permission denial, and nothing weaker."""
import json
import os
import sys

from hermes_cli.kanban_pr_acceptance import collect_acceptance

SHA = "a" * 40
OTHER = "b" * 40
PR = "https://github.com/acme/repo/pull/7"
ACTIONS_APP = 15368


def _page(key, rows, total=None):
    return {"total_count": len(rows) if total is None else total, key: rows}


def _pr(sha=SHA):
    return {"data": {"repository": {"pullRequest": {
        "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
        "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
            {"context": "build", "app": {"databaseId": ACTIONS_APP}}]}}}}}}


def _run(sha=SHA, attempt=1, path=".github/workflows/ci.yml", event="pull_request", run_id=7,
         head_repo="acme/repo"):
    return {"id": run_id, "head_sha": sha, "run_attempt": attempt, "path": path,
            "event": event, "status": "completed", "conclusion": "success",
            "head_repository": {"full_name": head_repo},
            "html_url": "https://github.com/acme/repo/actions/runs/7"}


def _job(sha=SHA, attempt=1, name="build", conclusion: str | None = "success", status="completed", job_id=99):
    return {"id": job_id, "name": name, "head_sha": sha, "run_attempt": attempt,
            "status": status, "conclusion": conclusion,
            "html_url": "https://github.com/acme/repo/actions/runs/7/job/99"}


def _install_gh(tmp_path, monkeypatch, routes):
    log = tmp_path / "gh.log"
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"routes = json.loads(pathlib.Path({str(routes_path)!r}).read_text())\n"
        "endpoint = sys.argv[2]\n"
        f"pathlib.Path({str(log)!r}).open('a').write(endpoint + '\\n')\n"
        "for rule in routes:\n"
        "    if rule['match'] in endpoint:\n"
        "        if rule.get('stderr'):\n"
        "            sys.stderr.write(rule['stderr'])\n"
        "            raise SystemExit(rule.get('code', 1))\n"
        "        print(json.dumps(rule['body']))\n"
        "        raise SystemExit(0)\n"
        "sys.stderr.write('gh: HTTP 404: unmatched ' + endpoint)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    return log


def _calls(log):
    return log.read_text(encoding="utf-8").splitlines()


def _base_routes(**overrides):
    routes = [
        {"match": "graphql", "body": _pr()},
        {"match": "/rules/branches/", "body": [[]]},
        {"match": "/check-runs", "stderr":
            "gh: HTTP 403: Resource not accessible by integration "
            "(https://api.github.com/repos/acme/repo/commits/" + SHA + "/check-runs)\n"
            "X-Accepted-GitHub-Permissions: checks=read\n"},
        {"match": "/actions/runs?", "body": [_page("workflow_runs", [_run(attempt=2)])]},
        {"match": "/contents/", "body": {"type": "file", "path": ".github/workflows/ci.yml"}},
        {"match": "/jobs", "body": [_page("jobs", [_job(attempt=2)])]},
        {"match": "/pulls/7", "body": {"head": {"sha": SHA}, "base": {"ref": "main"}, "state": "open"}},
    ]
    routes.extend(overrides.get("extra", []))
    return routes


def test_actions_fallback_accepts_latest_attempt_on_exact_head(tmp_path, monkeypatch):
    log = _install_gh(tmp_path, monkeypatch, _base_routes())
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is True
    assert receipt["classification"] == "success"
    assert receipt["head_sha"] == SHA
    assert receipt["evidence_source"] == "actions"
    assert receipt["checks"][0]["name"] == "build"
    assert receipt["checks"][0]["conclusion"] == "success"
    calls = _calls(log)
    assert any("/check-runs" in call for call in calls)
    assert any("/actions/runs?" in call and f"head_sha={SHA}" in call for call in calls)
    assert any("/jobs" in call and "filter=latest" in call for call in calls)
    assert not any("/statuses" in call for call in calls)


def _refuse(tmp_path, monkeypatch, routes):
    log = _install_gh(tmp_path, monkeypatch, routes)
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is not True
    return receipt, _calls(log)


def _replace(routes, match, **fields):
    copied = []
    for rule in routes:
        rule = dict(rule)
        if rule["match"] == match:
            rule.update(fields)
        copied.append(rule)
    return copied


def test_actions_fallback_rejects_job_on_a_different_sha(tmp_path, monkeypatch):
    routes = _base_routes()
    routes = _replace(routes, "/actions/runs?", body=[_page("workflow_runs", [_run(sha=OTHER, attempt=2)])])
    routes = _replace(routes, "/jobs", body=[_page("jobs", [_job(sha=OTHER, attempt=2)])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "missing"
    assert receipt["evidence_source"] == "actions"


def test_actions_fallback_rejects_older_successful_attempt(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/jobs", body=[_page("jobs", [_job(attempt=1, conclusion="success")])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "missing"


def test_actions_fallback_rejects_failed_latest_attempt(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/jobs", body=[_page("jobs", [
        _job(attempt=1, conclusion="success", job_id=1),
        _job(attempt=2, conclusion="failure", job_id=2),
    ])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "failure"


def test_actions_fallback_rejects_pending_latest_attempt(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/jobs", body=[_page("jobs", [
        _job(attempt=2, conclusion=None, status="in_progress"),
    ])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "pending"


def test_actions_fallback_refuses_non_actions_requirement(tmp_path, monkeypatch):
    pr = _pr()
    pr["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"].append(
        {"context": "codecov", "app": {"databaseId": 222}})
    routes = _replace(_base_routes(), "graphql", body=pr)
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "auth"
    assert "codecov" in receipt["detail"]
    assert "15368" in receipt["detail"]
    assert not any("/actions/runs" in call for call in calls)


def test_actions_fallback_refuses_unpinned_requirement(tmp_path, monkeypatch):
    pr = _pr()
    pr["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"] = [
        {"context": "build", "app": None}]
    routes = _replace(_base_routes(), "graphql", body=pr)
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["ok"] is not True
    assert "build" in receipt["detail"]
    assert not any("/actions/runs" in call for call in calls)


def test_actions_fallback_refuses_any_app_sentinel(tmp_path, monkeypatch):
    pr = _pr()
    pr["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"] = [
        {"context": "build", "app": {"databaseId": -1}}]
    routes = _replace(_base_routes(), "graphql", body=pr)
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["ok"] is not True
    assert not any("/actions/runs" in call for call in calls)


def test_actions_fallback_rejects_missing_workflow_provenance(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", body=[_page("workflow_runs", [_run(path="")])])
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "infra"
    assert "provenance" in receipt["detail"]
    assert not any("/jobs" in call for call in calls)


def test_actions_fallback_rejects_untrusted_event(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", body=[_page("workflow_runs", [_run(event="workflow_dispatch")])])
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "provenance" in receipt["detail"]
    assert not any("/jobs" in call or "/contents/" in call for call in calls)


def test_actions_fallback_rejects_pull_request_target(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", body=[_page("workflow_runs", [_run(event="pull_request_target")])])
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "provenance" in receipt["detail"]
    assert not any("/jobs" in call for call in calls)


def test_actions_fallback_rejects_fork_head_repository(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", body=[_page("workflow_runs", [_run(head_repo="evil/fork")])])
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "untrusted provenance" in receipt["detail"]
    assert not any("/jobs" in call for call in calls)


def test_actions_fallback_rejects_workflow_absent_from_base(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/contents/", stderr="gh: HTTP 404: Not Found\n")
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "protected base branch" in receipt["detail"]
    assert not any("/jobs" in call for call in calls)


def test_actions_fallback_rejects_head_change_during_collection(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/pulls/7", body={"head": {"sha": OTHER}, "base": {"ref": "main"}, "state": "open"})
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "stale"
    assert receipt["ok"] is False


def test_checks_rate_limit_does_not_use_actions(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/check-runs", stderr="gh: HTTP 403: API rate limit exceeded\n")
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "auth"
    assert not any("/actions/runs" in call for call in calls)


def test_checks_401_does_not_use_actions(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/check-runs", stderr="gh: HTTP 401: Bad credentials\n")
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "auth"
    assert not any("/actions/runs" in call for call in calls)


def test_successful_check_run_does_not_consult_actions(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/check-runs", stderr=None, body=[_page("check_runs", [{
        "id": 42, "name": "build", "head_sha": SHA, "app": {"id": ACTIONS_APP},
        "status": "completed", "conclusion": "success",
        "html_url": "https://github.com/acme/repo/runs/42"}])])
    routes.append({"match": "/statuses", "body": [[]]})
    log = _install_gh(tmp_path, monkeypatch, routes)
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is True
    assert receipt["evidence_source"] == "checks"
    assert not any("/actions/runs" in call for call in _calls(log))


def test_failed_check_run_is_not_overridden_by_actions(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/check-runs", stderr=None, body=[_page("check_runs", [{
        "id": 42, "name": "build", "head_sha": SHA, "app": {"id": ACTIONS_APP},
        "status": "completed", "conclusion": "failure",
        "html_url": "https://github.com/acme/repo/runs/42"}])])
    routes.append({"match": "/statuses", "body": [[]]})
    log = _install_gh(tmp_path, monkeypatch, routes)
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is not True
    assert receipt["classification"] == "failure"
    assert receipt["evidence_source"] == "checks"
    assert not any("/actions/runs" in call for call in _calls(log))


def test_actions_api_denial_stays_closed(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", stderr="gh: HTTP 403: Resource not accessible by integration\n")
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "auth"
    assert "Neither source" in receipt["detail"]


def test_actions_pagination_mismatch_is_not_success(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/actions/runs?", body=[_page("workflow_runs", [_run(attempt=2)], total=2)])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "infra"
    assert "pagination" in receipt["detail"]


def test_skipped_actions_job_is_not_success(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/jobs", body=[_page("jobs", [_job(attempt=2, conclusion="skipped")])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["ok"] is not True
    assert receipt["classification"] != "success"


def test_name_collision_requires_every_matching_job(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/jobs", body=[_page("jobs", [
        _job(attempt=2, conclusion="success", job_id=1),
        _job(attempt=2, conclusion="failure", job_id=2),
    ])])
    receipt, _calls_made = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "failure"


def test_statuses_permission_denial_still_accepts_app_pinned_checks(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/check-runs", stderr=None, body=[_page("check_runs", [{
        "id": 42, "name": "build", "head_sha": SHA, "app": {"id": ACTIONS_APP},
        "status": "completed", "conclusion": "success"}])])
    routes.append({"match": "/statuses", "stderr":
                   "gh: HTTP 403: Resource not accessible by integration\n"})
    log = _install_gh(tmp_path, monkeypatch, routes)
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is True
    assert receipt["evidence_source"] == "checks"
    assert not any("/actions/runs" in call for call in _calls(log))


def test_ruleset_actions_requirement_uses_fallback(tmp_path, monkeypatch):
    pr = _pr()
    pr["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"] = []
    routes = _replace(_base_routes(), "graphql", body=pr)
    routes = _replace(routes, "/rules/branches/", body=[[{
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "build", "integration_id": ACTIONS_APP}]},
    }]])
    log = _install_gh(tmp_path, monkeypatch, routes)
    receipt = collect_acceptance(PR, PR)
    assert receipt["ok"] is True
    assert receipt["evidence_source"] == "actions"
    assert any("/actions/runs?" in call for call in _calls(log))


def test_ruleset_third_party_requirement_blocks_fallback(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/rules/branches/", body=[[{
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [{"context": "codecov", "integration_id": 222}]},
    }]])
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "codecov" in receipt["detail"]
    assert not any("/actions/runs" in call for call in calls)


def test_ruleset_denial_does_not_skip_ahead_to_actions(tmp_path, monkeypatch):
    routes = _replace(_base_routes(), "/rules/branches/",
                      stderr="gh: HTTP 403: Resource not accessible by integration\n")
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert receipt["classification"] == "auth"
    assert not any("/check-runs" in call for call in calls)
    assert not any("/actions/runs" in call for call in calls)


def test_statuses_permission_denial_does_not_skip_unpinned_context(tmp_path, monkeypatch):
    pr = _pr()
    pr["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"] = [
        {"context": "build", "app": None}]
    routes = _replace(_base_routes(), "graphql", body=pr)
    routes = _replace(routes, "/check-runs", stderr=None, body=[_page("check_runs", [])])
    routes.append({"match": "/statuses", "stderr": "gh: HTTP 403: Resource not accessible by integration\n"})
    receipt, calls = _refuse(tmp_path, monkeypatch, routes)
    assert "build" in receipt["detail"]
    assert "legacy status" in receipt["detail"]
    assert not any("/actions/runs" in call for call in calls)
