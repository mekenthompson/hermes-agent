from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/open_fleet_pin_pr.py"

spec = importlib.util.spec_from_file_location("open_fleet_pin_pr", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

REPO = "mekenthompson/hermes-fleet"
SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + "1" * 64
OLD_DIGEST = "sha256:" + "0" * 64
MAIN_SHA = "c" * 40
BRANCH_SHA = "d" * 40
BRANCH = f"release/pin-agent-{SHA[:8]}"
PR_URL = "https://github.com/mekenthompson/hermes-fleet/pull/77"
RUN_URL = "https://github.com/mekenthompson/hermes-agent/actions/runs/123"


def manifest(**overrides: Any) -> dict[str, Any]:
    data = {
        "schema_version": 1,
        "repository": mod.AGENT_REPOSITORY,
        "revision": SHA,
        "digest": DIGEST,
        "immutable_ref": f"{mod.AGENT_REPOSITORY}@{DIGEST}",
    }
    data.update(overrides)
    return data


def contents_payload(content: bytes, blob_sha: str = "blob" + "0" * 36) -> dict[str, Any]:
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(content).decode("ascii"), "sha": blob_sha}


class FakeGh:
    """Scripted stand-in for the gh CLI; records every call and serves canned responses."""

    def __init__(
        self,
        *,
        main_manifest: dict[str, Any] | None,
        branch_manifest: dict[str, Any] | None = None,
        branch_present: bool = False,
        open_pr: dict[str, Any] | None = None,
        pr_view: dict[str, Any] | None = None,
    ) -> None:
        self.main_manifest = main_manifest
        self.branch_manifest = branch_manifest
        self.branch_present = branch_present
        self.open_pr = open_pr
        self.pr_view = pr_view or {"state": "OPEN", "autoMergeRequest": None}
        self.pr_view_calls = 0
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], input_text: str | None = None) -> str:
        self.calls.append((list(args), input_text))
        if args[0] == "api":
            return self.api(args[1], args[3], input_text)
        if args[:2] == ["pr", "list"]:
            return json.dumps([self.open_pr] if self.open_pr else [])
        if args[:2] == ["pr", "create"]:
            return f"Creating pull request\n{PR_URL}\n"
        if args[:2] == ["pr", "view"]:
            self.pr_view_calls += 1
            view = {"headRefOid": BRANCH_SHA, "mergeStateStatus": "CLEAN", "url": PR_URL, "number": 77, **self.pr_view}
            if self.pr_view_calls > 1 and view["state"] == "OPEN":
                view["state"] = "MERGED"
            return json.dumps(view)
        if args[:2] == ["pr", "merge"]:
            return ""
        raise AssertionError(f"unexpected gh call {args}")

    def api(self, path: str, method: str, input_text: str | None) -> str:
        if path == f"repos/{REPO}/git/ref/heads/main":
            return json.dumps({"object": {"sha": MAIN_SHA}})
        if path == f"repos/{REPO}/git/ref/heads/{BRANCH}":
            if self.branch_present:
                return json.dumps({"object": {"sha": BRANCH_SHA}})
            raise mod.GhError(["api", path], 1, "", "HTTP 404: Not Found")
        if path == f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}?ref={MAIN_SHA}":
            if self.main_manifest is None:
                raise mod.GhError(["api", path], 1, "", "HTTP 404: Not Found")
            return json.dumps(contents_payload(mod.canonical_json(self.main_manifest).encode("utf-8")))
        if path == f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}?ref={BRANCH_SHA}":
            if self.branch_manifest is None:
                raise mod.GhError(["api", path], 1, "", "HTTP 404: Not Found")
            return json.dumps(contents_payload(mod.canonical_json(self.branch_manifest).encode("utf-8")))
        if path == f"repos/{REPO}/git/refs" and method == "POST":
            return json.dumps({"ref": f"refs/heads/{BRANCH}"})
        if path == f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}" and method == "PUT":
            payload = json.loads(input_text or "{}")
            self.branch_manifest = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
            self.branch_present = True
            return json.dumps({"commit": {"sha": "e" * 40}})
        raise AssertionError(f"unexpected api call {method} {path}")

    def writes(self) -> list[list[str]]:
        return [
            args
            for args, _ in self.calls
            if (args[0] == "api" and args[3] in {"POST", "PUT", "PATCH", "DELETE"}) or args[:2] in (["pr", "create"], ["pr", "merge"])
        ]


def run_main(gh: FakeGh, *, dry_run: bool = False, data: dict[str, Any] | None = None, expected: str = SHA) -> tuple[int, dict[str, Any] | None, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "agent-image-manifest.json"
        path.write_text(json.dumps(data if data is not None else manifest()), encoding="utf-8")
        argv = ["--manifest", str(path), "--expected-revision", expected, "--source-run-url", RUN_URL, "--fleet-repo", REPO]
        if dry_run:
            argv.append("--dry-run")
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(argv, gh=gh)
        result = json.loads(out.getvalue()) if code == 0 else None
        return code, result, err.getvalue()


class ManifestValidationTests(unittest.TestCase):
    def test_accepts_a_manifest_for_the_expected_revision(self) -> None:
        validated = mod.validate_manifest(manifest(), expected_revision=SHA)
        self.assertEqual(list(validated), sorted(mod.MANIFEST_KEYS))
        self.assertEqual(mod.canonical_json(validated), json.dumps(manifest(), indent=2, sort_keys=True) + "\n")

    def test_rejects_revision_that_is_not_the_triggering_head_sha(self) -> None:
        with self.assertRaisesRegex(mod.HandoffError, "head_sha"):
            mod.validate_manifest(manifest(), expected_revision=OTHER_SHA)
        with self.assertRaisesRegex(mod.HandoffError, "expected revision"):
            mod.validate_manifest(manifest(), expected_revision="main")

    def test_rejects_malformed_fields(self) -> None:
        cases = {
            "wrong repository": manifest(repository="ghcr.io/nousresearch/hermes-agent", immutable_ref=f"ghcr.io/nousresearch/hermes-agent@{DIGEST}"),
            "short revision": manifest(revision=SHA[:7]),
            "non-hex revision": manifest(revision="g" * 40),
            "bad digest": manifest(digest="sha256:abc", immutable_ref=f"{mod.AGENT_REPOSITORY}@sha256:abc"),
            "mismatched immutable_ref": manifest(immutable_ref=f"{mod.AGENT_REPOSITORY}@{OLD_DIGEST}"),
            "tag ref": manifest(immutable_ref=f"{mod.AGENT_REPOSITORY}:sha-{SHA}"),
            "schema": manifest(schema_version=2),
            "extra key": manifest(extra=True),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(mod.HandoffError):
                mod.validate_manifest(data, expected_revision=SHA)
        with self.assertRaises(mod.HandoffError):
            mod.validate_manifest({k: v for k, v in manifest().items() if k != "digest"}, expected_revision=SHA)
        with self.assertRaises(mod.HandoffError):
            mod.validate_manifest([manifest()], expected_revision=SHA)

    def test_cli_fails_closed_on_invalid_manifest_without_touching_github(self) -> None:
        gh = FakeGh(main_manifest=None)
        code, _, err = run_main(gh, expected=OTHER_SHA)
        self.assertEqual(code, 1)
        self.assertIn("::error::", err)
        self.assertEqual(gh.calls, [])


class PinPrTests(unittest.TestCase):
    def test_no_op_when_main_already_pins_identical_content(self) -> None:
        gh = FakeGh(main_manifest=manifest())
        code, result, _ = run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "already-pinned")
        self.assertEqual(gh.writes(), [])
        self.assertFalse(any(args[:2] == ["pr", "merge"] for args, _ in gh.calls))

    def test_creates_branch_file_pr_and_arms_auto_merge(self) -> None:
        gh = FakeGh(main_manifest=manifest(digest=OLD_DIGEST, revision=OTHER_SHA, immutable_ref=f"{mod.AGENT_REPOSITORY}@{OLD_DIGEST}"))
        code, result, _ = run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(result, {"status": "pr-created", "pr_url": PR_URL, "auto_merge": "auto-merge enabled", "merge": "merged"})

        writes = gh.writes()
        self.assertEqual([w[:4] if w[0] == "api" else w[:2] for w in writes], [
            ["api", f"repos/{REPO}/git/refs", "-X", "POST"],
            ["api", f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}", "-X", "PUT"],
            ["pr", "create"],
            ["pr", "merge"],
        ])

        create_body = json.loads(next(text for args, text in gh.calls if args[1] == f"repos/{REPO}/git/refs"))
        self.assertEqual(create_body, {"ref": f"refs/heads/{BRANCH}", "sha": MAIN_SHA})

        put_body = json.loads(next(text for args, text in gh.calls if args[3] == "PUT"))
        self.assertEqual(put_body["branch"], BRANCH)
        self.assertEqual(put_body["sha"], "blob" + "0" * 36)
        self.assertEqual(base64.b64decode(put_body["content"]).decode("utf-8"), mod.canonical_json(manifest()))
        self.assertTrue(put_body["message"].startswith(f"release: pin Agent {SHA[:8]} ({'1' * 12})\n"))

        pr_create, body = next((args, text) for args, text in gh.calls if args[:2] == ["pr", "create"])
        self.assertEqual(pr_create[pr_create.index("--title") + 1], f"release: pin Agent {SHA[:8]}")
        self.assertEqual(pr_create[pr_create.index("--head") + 1], BRANCH)
        self.assertEqual(pr_create[pr_create.index("-R") + 1], REPO)
        for needle in (SHA, DIGEST, f"{mod.AGENT_REPOSITORY}@{DIGEST}", RUN_URL):
            self.assertIn(needle, body)

        merge = next(args for args, _ in gh.calls if args[:2] == ["pr", "merge"])
        self.assertEqual(merge, ["pr", "merge", "--auto", "--squash", "-R", REPO, "77"])

    def test_creates_file_when_fleet_has_no_manifest_yet(self) -> None:
        gh = FakeGh(main_manifest=None)
        code, result, _ = run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "pr-created")
        put_body = json.loads(next(text for args, text in gh.calls if args[3] == "PUT"))
        self.assertNotIn("sha", put_body)

    def test_reuses_existing_branch_and_open_pr_with_matching_content(self) -> None:
        gh = FakeGh(
            main_manifest=manifest(digest=OLD_DIGEST, immutable_ref=f"{mod.AGENT_REPOSITORY}@{OLD_DIGEST}"),
            branch_present=True,
            branch_manifest=manifest(),
            open_pr={"url": PR_URL, "number": 77, "headRefOid": BRANCH_SHA},
        )
        code, result, _ = run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "pr-existing")
        self.assertEqual(result["pr_url"], PR_URL)
        self.assertEqual([w[:2] for w in gh.writes()], [["pr", "merge"]])

    def test_reuses_existing_branch_without_pr_and_opens_one(self) -> None:
        gh = FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest())
        code, result, _ = run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "pr-created")
        self.assertEqual([w[:2] for w in gh.writes()], [["pr", "create"], ["pr", "merge"]])

    def test_fails_closed_when_branch_exists_with_different_content(self) -> None:
        gh = FakeGh(
            main_manifest=None,
            branch_present=True,
            branch_manifest=manifest(digest=OLD_DIGEST, immutable_ref=f"{mod.AGENT_REPOSITORY}@{OLD_DIGEST}"),
            open_pr={"url": PR_URL, "number": 77, "headRefOid": BRANCH_SHA},
        )
        code, _, err = run_main(gh)
        self.assertEqual(code, 1)
        self.assertIn(BRANCH, err)
        self.assertIn("differs", err)
        self.assertEqual(gh.writes(), [])

    def test_fails_closed_when_branch_exists_without_the_file(self) -> None:
        gh = FakeGh(main_manifest=None, branch_present=True, branch_manifest=None)
        code, _, _ = run_main(gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.writes(), [])

    def test_fails_closed_on_several_open_prs(self) -> None:
        gh = FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest())
        gh.open_pr = None

        def pr_list_twice(args: list[str], input_text: str | None = None) -> str:
            if args[:2] == ["pr", "list"]:
                gh.calls.append((list(args), input_text))
                return json.dumps([{"url": PR_URL, "number": 77}, {"url": PR_URL + "8", "number": 778}])
            return FakeGh.__call__(gh, args, input_text)

        code, _, err = run_main(pr_list_twice)  # type: ignore[arg-type]
        self.assertEqual(code, 1)
        self.assertIn("several open PRs", err)
        self.assertEqual(gh.writes(), [])

    def test_auto_merge_is_not_re_armed_or_applied_to_merged_prs(self) -> None:
        for view, expected in (
            ({"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}, "auto-merge already enabled"),
            ({"state": "MERGED", "autoMergeRequest": None}, "already merged"),
        ):
            with self.subTest(expected):
                gh = FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest(), open_pr={"url": PR_URL, "number": 77}, pr_view=view)
                code, result, _ = run_main(gh)
                self.assertEqual(code, 0)
                self.assertEqual(result["auto_merge"], expected)
                self.assertEqual(result["merge"], "merged")
                self.assertEqual(gh.writes(), [])

    def test_dry_run_reads_everything_and_writes_nothing(self) -> None:
        cases = {
            "fresh": FakeGh(main_manifest=None),
            "branch-without-pr": FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest()),
            "branch-with-pr": FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest(), open_pr={"url": PR_URL, "number": 77}),
        }
        for label, gh in cases.items():
            with self.subTest(label):
                code, result, err = run_main(gh, dry_run=True)
                self.assertEqual(code, 0)
                self.assertTrue(result.get("dry_run"))
                self.assertIn("[dry-run]", err)
                self.assertEqual(gh.writes(), [])
                self.assertTrue(any(args[0] == "api" for args, _ in gh.calls))
        self.assertIn(mod.canonical_json(manifest()), run_main(cases["fresh"], dry_run=True)[2])

    def test_dry_run_still_fails_closed_on_divergent_branch(self) -> None:
        gh = FakeGh(main_manifest=None, branch_present=True, branch_manifest=manifest(digest=OLD_DIGEST, immutable_ref=f"{mod.AGENT_REPOSITORY}@{OLD_DIGEST}"))
        code, _, _ = run_main(gh, dry_run=True)
        self.assertEqual(code, 1)
        self.assertEqual(gh.writes(), [])

    def test_gh_failures_other_than_404_propagate_as_errors(self) -> None:
        def broken(args: list[str], input_text: str | None = None) -> str:
            raise mod.GhError(args, 1, "", "HTTP 500: boom")

        code, _, err = run_main(broken)  # type: ignore[arg-type]
        self.assertEqual(code, 1)
        self.assertIn("500", err)


class StrictMainReconciliationTests(unittest.TestCase):
    def test_behind_pr_waits_for_async_update_then_reconciles_a_later_main_advance(self) -> None:
        calls: list[tuple[list[str], str | None]] = []
        sleeps: list[float] = []
        statuses = iter([
            {"url": PR_URL, "number": 77, "state": "OPEN", "headRefOid": BRANCH_SHA, "mergeStateStatus": "BEHIND", "autoMergeRequest": {"enabledAt": "now"}},
            # update-branch is asynchronous: the old head can remain BEHIND briefly.
            {"url": PR_URL, "number": 77, "state": "OPEN", "headRefOid": BRANCH_SHA, "mergeStateStatus": "BEHIND", "autoMergeRequest": {"enabledAt": "now"}},
            # A concurrent main advance leaves the refreshed head behind again.
            {"url": PR_URL, "number": 77, "state": "OPEN", "headRefOid": "e" * 40, "mergeStateStatus": "BEHIND", "autoMergeRequest": {"enabledAt": "now"}},
            {"url": PR_URL, "number": 77, "state": "MERGED", "headRefOid": "f" * 40, "mergeStateStatus": "CLEAN", "autoMergeRequest": {"enabledAt": "now"}},
        ])

        def gh(args: list[str], input_text: str | None = None) -> str:
            calls.append((args, input_text))
            if args[:2] == ["pr", "view"]:
                return json.dumps(next(statuses))
            if args[:2] == ["api", f"repos/{REPO}/pulls/77/update-branch"]:
                self.assertEqual(args[3], "PUT")
                return json.dumps({"message": "Updating pull request branch."})
            if args[:2] == ["api", f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}?ref={'e' * 40}"] or args[:2] == ["api", f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}?ref={'f' * 40}"]:
                return json.dumps(contents_payload(mod.canonical_json(manifest()).encode("utf-8")))
            raise AssertionError(args)

        github = mod.GitHub(REPO, gh, sleep=sleeps.append)
        outcome = mod.reconcile_and_wait(github, PR_URL, expected_content=mod.canonical_json(manifest()).encode("utf-8"))

        self.assertEqual(outcome, "merged")
        updates = [(args, json.loads(body or "{}")) for args, body in calls if args[:2] == ["api", f"repos/{REPO}/pulls/77/update-branch"]]
        self.assertEqual([body for _, body in updates], [{"expected_head_sha": BRANCH_SHA}, {"expected_head_sha": "e" * 40}])
        self.assertEqual(sleeps, [mod.WAIT_SECONDS, mod.WAIT_SECONDS, mod.WAIT_SECONDS])

    def test_merged_pr_must_still_contain_the_exact_expected_pin(self) -> None:
        def gh(args: list[str], input_text: str | None = None) -> str:
            if args[:2] == ["pr", "view"]:
                return json.dumps({"url": PR_URL, "number": 77, "state": "MERGED", "headRefOid": BRANCH_SHA, "mergeStateStatus": "CLEAN"})
            if args[:2] == ["api", f"repos/{REPO}/contents/{mod.FLEET_MANIFEST_PATH}?ref={BRANCH_SHA}"]:
                return json.dumps(contents_payload(b"wrong manifest\n"))
            raise AssertionError(args)

        github = mod.GitHub(REPO, gh, sleep=lambda _: None)
        with self.assertRaisesRegex(mod.HandoffError, "expected pin content"):
            mod.reconcile_and_wait(github, PR_URL, expected_content=mod.canonical_json(manifest()).encode("utf-8"))

    def test_wait_budget_is_twelve_minutes(self) -> None:
        self.assertEqual(mod.WAIT_ATTEMPTS * mod.WAIT_SECONDS, 12 * 60)

    def test_ambiguous_branch_creation_is_reconciled_only_at_expected_base(self) -> None:
        calls: list[list[str]] = []

        def gh(args: list[str], input_text: str | None = None) -> str:
            calls.append(args)
            if args[:2] == ["api", f"repos/{REPO}/git/refs"]:
                raise mod.GhError(args, 1, "", "network timeout")
            if args[:2] == ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}"]:
                return json.dumps({"object": {"sha": MAIN_SHA}})
            raise AssertionError(args)

        github = mod.GitHub(REPO, gh, sleep=lambda _: None)
        github.create_branch_reconciled(BRANCH, MAIN_SHA)
        self.assertEqual(calls[-1], ["api", f"repos/{REPO}/git/ref/heads/{BRANCH}", "-X", "GET"])

    def test_ambiguous_pr_creation_reuses_only_the_matching_open_branch_pr(self) -> None:
        def gh(args: list[str], input_text: str | None = None) -> str:
            if args[:2] == ["pr", "create"]:
                raise mod.GhError(args, 1, "", "network timeout")
            if args[:2] == ["pr", "list"]:
                return json.dumps([{"url": PR_URL, "number": 77, "headRefOid": BRANCH_SHA}])
            raise AssertionError(args)

        github = mod.GitHub(REPO, gh, sleep=lambda _: None)
        self.assertEqual(github.create_pr_reconciled(BRANCH, "title", "body"), PR_URL)

    def test_transient_idempotent_read_retries_but_fails_closed_after_bound(self) -> None:
        for failure in ("HTTP 429: rate limited", "HTTP 503: unavailable"):
            with self.subTest(failure=failure):
                attempts = 0

                def gh(args: list[str], input_text: str | None = None) -> str:
                    nonlocal attempts
                    attempts += 1
                    raise mod.GhError(args, 1, "", failure)

                github = mod.GitHub(REPO, gh, sleep=lambda _: None)
                with self.assertRaisesRegex(mod.GhError, failure.split()[1]):
                    github.head()
                self.assertEqual(attempts, mod.READ_RETRY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
