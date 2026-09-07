from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/fork-agent-image.yml"
DOC = ROOT / "docs/fork-agent-image.md"
MANIFEST = ROOT / "scripts/emit-image-manifest.py"
COMPACT_SBOM = ROOT / "scripts/compact-spdx-sbom.py"
REMOTE_CONFIG = ROOT / "scripts/verify-remote-image-config.py"
PUSH_DIGEST = ROOT / "scripts/extract-image-push-digest.py"
SHA = "1" * 40
DIGEST = "sha256:" + "2" * 64
REPOSITORY = "ghcr.io/mekenthompson/hermes-agent"


class ForkImageWorkflowTests(unittest.TestCase):
    def test_publishing_retains_scan_evidence_even_on_failure(self) -> None:
        publish = WORKFLOW.read_text(encoding="utf-8").split("\n  publish:\n", 1)[1]
        self.assertIn("name: Upload publication evidence", publish)
        evidence = publish.split("name: Upload publication evidence", 1)[1].split("name: Upload Fleet handoff", 1)[0]
        self.assertIn("if: always()", evidence)
        for artifact in ("agent-image.spdx.json", "agent-image.attestation.spdx.json", "trivy-image.json", "remote-manifest.json"):
            self.assertIn(artifact, evidence)

    def test_required_files_exist(self) -> None:
        for path in (WORKFLOW, DOC, MANIFEST):
            self.assertTrue(path.is_file(), path)

    def test_workflow_is_fork_scoped_and_manual_publish_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("github.repository == 'mekenthompson/hermes-agent'", text)
        self.assertRegex(text, r"(?m)^\s*pull_request:\s*$")
        self.assertNotIn("    paths:", text)
        self.assertRegex(text, r"(?m)^\s*workflow_dispatch:\s*$")
        self.assertNotRegex(text, r"(?m)^\s*push:\s*$")
        self.assertIn("type: boolean", text)
        self.assertIn("default: false", text)
        self.assertIn("github.event.inputs.publish == 'true'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("environment: agent-image-publish", text)

    def test_workflow_has_least_privilege_boundaries(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", text)
        self.assertRegex(text, r"(?s)publish:.*?permissions:.*?contents: read.*?packages: write.*?id-token: write")
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("secrets.", text)

    def test_publish_requires_a_successful_ci_run_for_this_exact_main_sha(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Verify exact main CI gate", text)
        self.assertIn("actions: read", text)
        self.assertIn("scripts/verify-exact-main-ci.py", text)
        self.assertIn('--workflow "ci.yaml"', text)
        self.assertIn('--workflow-path ".github/workflows/ci.yaml"', text)
        self.assertIn("before registry access", text)
        self.assertNotIn("needs: preflight", text)

    def test_publish_checks_out_the_gate_script_before_running_it(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        publish = text.split("\n  publish:\n", 1)[1]
        self.assertLess(publish.index("Checkout exact pushed commit"), publish.index("Verify exact main CI gate"))
        self.assertIn("persist-credentials: false", publish.split("Verify exact main CI gate", 1)[0])
        self.assertLess(publish.index("Verify exact main CI gate"), publish.index("Log in to GHCR"))

    def test_preflight_builds_complete_image_and_gates_evidence(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG HERMES_IMAGE_IDENTITY=nousresearch/hermes-agent", dockerfile)
        self.assertIn("HERMES_IMAGE_IDENTITY=${{ env.IMAGE_REPOSITORY }}", text)
        self.assertIn('data["image"] == os.environ["EXPECTED_IMAGE_IDENTITY"]', text)
        self.assertEqual(text.count('data["version"] == metadata.version("hermes-agent")'), 2)
        self.assertIn("context: .", text)
        self.assertIn("file: Dockerfile", text)
        self.assertIn("platforms: linux/amd64", text)
        self.assertIn("load: true", text)
        self.assertIn("push: false", text)
        self.assertIn("HERMES_GIT_SHA=${{ github.sha }}", text)
        self.assertIn("/etc/hermes/image-provenance.json", text)
        self.assertIn("anchore/sbom-action@3ad7283483fc7af8ff2b4ea19663c2d5ca935e26", text)
        self.assertIn("aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25", text)
        self.assertIn("severity: CRITICAL", text)
        self.assertIn("exit-code: 1", text)

    def test_node_source_pin_contains_fixed_bundled_tar(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        vulnerable = "sha256:9e6f9357d371591e32ab6f2d8a26d63bdd0d17c29eee3f4f3e7e454d9634bf73"
        fixed = "sha256:367679cf9792759492a486e4aa4b421764d71a9546a6dae8aab81a99eb797b3e"
        self.assertNotIn(vulnerable, dockerfile)
        self.assertIn(f"FROM node:26-bookworm-slim@{fixed} AS node_source", dockerfile)

    def test_publish_promotes_the_scanned_candidate_without_rebuilding(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("docker/build-push-action@"), 2)
        self.assertNotIn("docker save", text)
        self.assertNotIn("docker load", text)
        self.assertNotIn("agent-image.tar.gz", text)
        self.assertIn("candidate-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}", text)
        self.assertIn('docker push "$CANDIDATE_IMAGE"', text)
        self.assertIn("scripts/verify-remote-image-config.py", text)
        self.assertIn("docker buildx imagetools create", text)
        self.assertIn("actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e", text)

    def test_pr_preflight_cannot_push_or_receive_registry_write_credentials(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        preflight = text.split("\n  publish:\n", 1)[0]
        self.assertNotIn("packages: write", preflight)
        self.assertNotIn("id-token: write", preflight)
        self.assertNotIn("docker/login-action", preflight)
        self.assertNotIn("docker push", preflight)

    def test_push_digest_extractor_accepts_actual_docker_push_summary(self) -> None:
        output = """The push refers to repository [ghcr.io/mekenthompson/hermes-agent]
abc123: Pushed
candidate-123-1: digest: sha256:2222222222222222222222222222222222222222222222222222222222222222 size: 1234
"""
        result = subprocess.run(
            ["python3", str(PUSH_DIGEST)],
            cwd=ROOT,
            input=output,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), DIGEST)

    def test_push_digest_extractor_rejects_missing_or_ambiguous_summaries(self) -> None:
        for output in (
            "The push refers to repository [ghcr.io/mekenthompson/hermes-agent]\n",
            f"tag-a: digest: {DIGEST} size: 1\ntag-b: digest: {DIGEST} size: 2\n",
        ):
            with self.subTest(output=output):
                result = subprocess.run(
                    ["python3", str(PUSH_DIGEST)],
                    cwd=ROOT,
                    input=output,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("expected exactly one", result.stderr)

    def test_manual_non_publish_dispatch_runs_the_no_credentials_preflight(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        preflight = text.split("\n  publish:\n", 1)[0]
        self.assertIn("github.event_name == 'workflow_dispatch'", preflight)
        self.assertIn("github.event.inputs.publish != 'true'", preflight)
        self.assertNotIn("packages: write", preflight)
        self.assertNotIn("id-token: write", preflight)
        self.assertNotIn("docker/login-action", preflight)

    def test_publish_does_not_also_run_the_duplicate_preflight_build_and_scan(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        preflight = text.split("\n  publish:\n", 1)[0]
        self.assertIn("github.event.inputs.publish != 'true'", preflight)

    def test_promotion_preserves_the_scanned_manifest_digest_at_the_sha_tag(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--prefer-index=false", text)
        self.assertIn('docker buildx imagetools inspect --format \'{{.Manifest.Digest}}\' "$TEST_IMAGE"', text)
        self.assertIn('test "$promoted_digest" = "$digest"', text)

    def test_remote_config_verifier_binds_remote_manifest_to_scanned_image_id(self) -> None:
        manifest = {"schemaVersion": 2, "config": {"digest": DIGEST}}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manifest.json"
            source.write_text(json.dumps(manifest), encoding="utf-8")
            result = subprocess.run(
                ["python3", str(REMOTE_CONFIG), "--image-id", DIGEST, "--manifest", str(source)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            mismatch = subprocess.run(
                ["python3", str(REMOTE_CONFIG), "--image-id", "sha256:" + "3" * 64, "--manifest", str(source)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("does not match", mismatch.stderr)

    def test_publish_attests_the_exact_sha_tag_and_digest(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('docker push "$CANDIDATE_IMAGE"', text)
        self.assertIn('docker buildx imagetools create --prefer-index=false --tag "$TEST_IMAGE" "$immutable_ref"', text)
        self.assertIn("ghcr.io/mekenthompson/hermes-agent:sha-${{ github.sha }}", text)
        self.assertIn("actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8", text)
        self.assertIn("actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e", text)
        self.assertIn("steps.publish.outputs.digest", text)
        self.assertIn("scripts/emit-image-manifest.py", text)

    def test_every_external_action_is_sha_pinned(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        for action in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text):
            if action.startswith("./"):
                continue
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", action)

    def test_documentation_states_release_boundary_and_handoff(self) -> None:
        text = DOC.read_text(encoding="utf-8").lower()
        for phrase in (
            "manual publication",
            "exact pushed commit",
            "no manual reviewer prerequisite",
            "image digest",
            "fleet",
            "rollback",
            "does not deploy",
        ):
            self.assertIn(phrase, text)

    def test_publish_uses_bounded_package_sbom_and_preserves_full_evidence(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("scripts/compact-spdx-sbom.py", text)
        self.assertIn("--max-bytes 16777216", text)
        self.assertIn("sbom-path: agent-image.attestation.spdx.json", text)
        self.assertRegex(
            text,
            r"(?s)Upload pre-publication evidence.*?agent-image\.spdx\.json.*?agent-image\.attestation\.spdx\.json",
        )

    def test_compact_sbom_retains_packages_and_package_relationships(self) -> None:
        document = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "documentDescribes": ["SPDXRef-Package-alpha", "SPDXRef-File-a"],
            "packages": [
                {
                    "name": "alpha",
                    "SPDXID": "SPDXRef-Package-alpha",
                    "filesAnalyzed": True,
                    "hasFiles": ["SPDXRef-File-a"],
                    "packageVerificationCode": {
                        "packageVerificationCodeValue": "abc123"
                    },
                    "licenseInfoFromFiles": ["MIT"],
                },
                {"name": "beta", "SPDXID": "SPDXRef-Package-beta"},
            ],
            "files": [{"fileName": "/bin/a", "SPDXID": "SPDXRef-File-a"}],
            "snippets": [
                {
                    "name": "snippet-a",
                    "SPDXID": "SPDXRef-Snippet-a",
                    "snippetFromFile": "SPDXRef-File-a",
                    "ranges": [],
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-alpha",
                },
                {
                    "spdxElementId": "SPDXRef-Package-beta",
                    "relationshipType": "DEPENDENCY_OF",
                    "relatedSpdxElement": "SPDXRef-Package-alpha",
                },
                {
                    "spdxElementId": "SPDXRef-Package-alpha",
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": "SPDXRef-File-a",
                },
                {
                    "spdxElementId": "SPDXRef-Package-alpha",
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": "SPDXRef-Snippet-a",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "full.json"
            output = Path(directory) / "attestation.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [
                    "python3",
                    str(COMPACT_SBOM),
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--max-bytes",
                    "16777216",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            compact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(compact["packages"]), 2)
            alpha = compact["packages"][0]
            self.assertEqual(alpha["SPDXID"], "SPDXRef-Package-alpha")
            self.assertFalse(alpha["filesAnalyzed"])
            self.assertNotIn("hasFiles", alpha)
            self.assertNotIn("packageVerificationCode", alpha)
            self.assertNotIn("licenseInfoFromFiles", alpha)
            self.assertEqual(compact["files"], [])
            self.assertEqual(compact["snippets"], [])
            self.assertEqual(compact["documentDescribes"], ["SPDXRef-Package-alpha"])
            self.assertEqual(compact["relationships"], document["relationships"][:2])
            self.assertLessEqual(output.stat().st_size, 16_777_216)

    def test_compact_sbom_rejects_dangling_relationships(self) -> None:
        document = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [{"name": "alpha", "SPDXID": "SPDXRef-Package-alpha"}],
            "files": [],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-Package-alpha",
                    "relationshipType": "DEPENDS_ON",
                    "relatedSpdxElement": "SPDXRef-Missing",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "full.json"
            output = Path(directory) / "attestation.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                ["python3", str(COMPACT_SBOM), "--input", str(source), "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dangling SPDX relationship", result.stderr)
            self.assertFalse(output.exists())

    def test_compact_sbom_rejects_malformed_element_ids(self) -> None:
        valid = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [{"name": "alpha", "SPDXID": "SPDXRef-Package-alpha"}],
            "files": [{"fileName": "/bin/a", "SPDXID": "SPDXRef-File-a"}],
            "snippets": [{"SPDXID": "SPDXRef-Snippet-a"}],
            "relationships": [],
        }
        malformed_ids = {
            "document": ("SPDXID", None, "not-an-spdx-id"),
            "package": ("packages", 0, 123),
            "file": ("files", 0, "SPDXRef-File bad"),
            "snippet": ("snippets", 0, "SPDXRef-Snippet_bad"),
        }
        for label, (field, index, malformed_id) in malformed_ids.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                document = json.loads(json.dumps(valid))
                if index is None:
                    document[field] = malformed_id
                else:
                    document[field][index]["SPDXID"] = malformed_id
                source = Path(directory) / "full.json"
                output = Path(directory) / "attestation.json"
                source.write_text(json.dumps(document), encoding="utf-8")
                result = subprocess.run(
                    ["python3", str(COMPACT_SBOM), "--input", str(source), "--output", str(output)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid SPDXID", result.stderr)
                self.assertFalse(output.exists())

    def test_compact_sbom_rejects_non_object_relationships(self) -> None:
        document = {
            "SPDXID": "SPDXRef-DOCUMENT",
            "packages": [{"name": "alpha", "SPDXID": "SPDXRef-Package-alpha"}],
            "files": [],
            "relationships": ["not-an-object"],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "full.json"
            output = Path(directory) / "attestation.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                ["python3", str(COMPACT_SBOM), "--input", str(source), "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SPDX relationships must be objects", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())

    def test_manifest_generator_emits_immutable_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            result = subprocess.run(
                [
                    "python3",
                    str(MANIFEST),
                    "--repository",
                    REPOSITORY,
                    "--revision",
                    SHA,
                    "--digest",
                    DIGEST,
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["repository"], REPOSITORY)
            self.assertEqual(manifest["revision"], SHA)
            self.assertEqual(manifest["digest"], DIGEST)
            self.assertEqual(manifest["immutable_ref"], f"{REPOSITORY}@{DIGEST}")

    def test_manifest_generator_rejects_malformed_inputs(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(MANIFEST),
                "--repository",
                REPOSITORY,
                "--revision",
                "main",
                "--digest",
                "sha256:bad",
                "--output",
                "/tmp/must-not-exist.json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
