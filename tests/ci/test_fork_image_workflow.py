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
SHA = "1" * 40
DIGEST = "sha256:" + "2" * 64
REPOSITORY = "ghcr.io/mekenthompson/hermes-agent"


class ForkImageWorkflowTests(unittest.TestCase):
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
        self.assertEqual(text.count("docker/build-push-action@"), 1)
        self.assertIn("docker save", text)
        self.assertIn("set -euo pipefail\n          docker save", text)
        self.assertIn("agent-image.tar.gz", text)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", text)
        self.assertIn("docker load", text)
        self.assertIn('docker push "$TEST_IMAGE"', text)
        self.assertIn("actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e", text)

    def test_publish_attests_the_exact_sha_tag_and_digest(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('docker push "$TEST_IMAGE"', text)
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
            "environment approval",
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
