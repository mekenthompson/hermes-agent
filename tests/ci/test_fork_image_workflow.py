from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/fork-agent-image.yml"
DOC = ROOT / "website/docs/developer-guide/fork-agent-image.md"
MANIFEST = ROOT / "scripts/emit-image-manifest.py"
COMPACT_SBOM = ROOT / "scripts/compact-spdx-sbom.py"
REMOTE_CONFIG = ROOT / "scripts/verify-remote-image-config.py"
PUSH_DIGEST = ROOT / "scripts/extract-image-push-digest.py"
EVIDENCE_ACTION = ROOT / ".github/actions/image-evidence/action.yml"
EVIDENCE_SCRIPT = ROOT / ".github/actions/image-evidence/run.sh"
EVIDENCE_USES = "./.github/actions/image-evidence"
FETCH_ASSET = ROOT / ".github/actions/image-evidence/fetch-pinned-release-asset.sh"
SYFT_SHA256 = "8fcb33017a0dc1058298c923c436d19dfa68ae93968e0b423248542e3afb9fc3"
TRIVY_SHA256 = "8b4376d5d6befe5c24d503f10ff136d9e0c49f9127a4279fd110b727929a5aa9"
SHA = "1" * 40
DIGEST = "sha256:" + "2" * 64
REPOSITORY = "ghcr.io/mekenthompson/hermes-agent"


class ForkImageWorkflowTests(unittest.TestCase):
    def test_both_builds_stamp_the_exact_source_before_building(self) -> None:
        import yaml

        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for name in ("preflight", "publish"):
            steps = workflow["jobs"][name]["steps"]
            stamp = next(i for i, step in enumerate(steps)
                         if "scripts/write_install_stamp.py" in step.get("run", ""))
            build = next(i for i, step in enumerate(steps)
                         if str(step.get("uses", "")).startswith("docker/build-push-action@"))
            self.assertLess(stamp, build)
            self.assertIn('--commit "$GITHUB_SHA"', steps[stamp]["run"])
            self.assertIn("--update-mechanism external", steps[stamp]["run"])
            self.assertTrue(any('stamp["commit"] == os.environ["EXPECTED_REVISION"]'
                                in step.get("run", "") for step in steps[build + 1:]))

    def test_stamp_step_leaves_the_cached_build_context_unchanged(self) -> None:
        # .dockerignore excludes only root-level __pycache__. Bytecode written
        # into pm/ by the stamp step changed `COPY pm/ pm/` on every build and
        # invalidated every dependency stage after it.
        import os
        import shutil

        import yaml

        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for name in ("preflight", "publish"):
            step = next(step for step in workflow["jobs"][name]["steps"]
                        if "scripts/write_install_stamp.py" in step.get("run", ""))
            with tempfile.TemporaryDirectory() as td:
                context = Path(td)
                for tree in ("pm", "hermes_cli", "scripts"):
                    shutil.copytree(ROOT / tree, context / tree,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                for module in ROOT.glob("*.py"):
                    shutil.copy2(module, context / module.name)
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith("PYTHON")}
                env.update({key: str(value) for key, value in step.get("env", {}).items()})
                env["GITHUB_SHA"] = SHA
                subprocess.run(["bash", "-c", step["run"]], cwd=context, env=env, check=True,
                               capture_output=True, text=True)
                self.assertTrue((context / "install-stamp.json").is_file())
                bytecode = sorted(str(path.relative_to(context)) for path in context.rglob("*.pyc"))
                self.assertEqual(bytecode, [], name)

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

    def test_workflow_publishes_only_on_explicit_main_dispatch(self) -> None:
        import yaml

        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^  pull_request:\s*$")
        self.assertNotRegex(text, r"(?m)^  push:\s*$")
        self.assertNotIn("    paths:", text)
        self.assertRegex(text, r"(?m)^  workflow_dispatch:\s*$")
        self.assertIn("type: boolean", text)
        self.assertIn("default: false", text)
        self.assertIn("environment: agent-image-publish", text)
        workflow = yaml.safe_load(text)
        self.assertEqual(list(workflow[True]), ["workflow_dispatch"])
        preflight = " ".join(workflow["jobs"]["preflight"]["if"].split())
        publish = " ".join(workflow["jobs"]["publish"]["if"].split())
        self.assertEqual(
            publish,
            "needs.detect.outputs.docker == 'true' && "
            "github.repository == 'mekenthompson/hermes-agent' && "
            "github.ref == 'refs/heads/main' && "
            "github.event_name == 'workflow_dispatch' && "
            "github.event.inputs.publish == 'true'",
        )
        self.assertEqual(
            preflight,
            "needs.detect.outputs.docker == 'true' && "
            "github.repository == 'mekenthompson/hermes-agent' && "
            "github.event_name == 'workflow_dispatch' && "
            "github.event.inputs.publish != 'true'",
        )
        concurrency = workflow["concurrency"]
        self.assertEqual(concurrency["group"], "fork-agent-image-${{ github.sha }}")
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_publish_waits_for_exact_main_ci_before_registry_write(self) -> None:
        import yaml

        text = WORKFLOW.read_text(encoding="utf-8")
        steps = yaml.safe_load(text)["jobs"]["publish"]["steps"]
        names = [step.get("name", "") for step in steps]
        gate = next(i for i, step in enumerate(steps) if "scripts/verify-exact-main-ci.py" in step.get("run", ""))
        self.assertIn("--wait", steps[gate]["run"])
        self.assertIn("--timeout-minutes 30", steps[gate]["run"])
        self.assertIn("--interval-seconds 20", steps[gate]["run"])
        login = next(i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith("docker/login-action@"))
        push = next(i for i, step in enumerate(steps) if "docker push" in step.get("run", ""))
        build = next(i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith("docker/build-push-action@"))
        scan = next(i for i, step in enumerate(steps) if step.get("uses") == EVIDENCE_USES)
        self.assertLess(build, gate, names)
        self.assertLess(scan, gate, names)
        self.assertLess(gate, login, names)
        self.assertLess(login, push, names)
        for step in steps[:gate]:
            self.assertNotIn("docker push", step.get("run", ""), step.get("name"))
            self.assertNotIn("docker/login-action", str(step.get("uses", "")), step.get("name"))
            self.assertNotIn("push-to-registry: true", str(step.get("with", {})), step.get("name"))
        for step in steps[:login]:
            self.assertFalse(str(step.get("uses", "")).startswith("actions/attest-"), step.get("name"))

    def test_workflow_has_least_privilege_boundaries(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", text)
        self.assertRegex(text, r"(?s)publish:.*?permissions:.*?contents: read.*?packages: write.*?id-token: write")
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("secrets.", text)

    def test_publish_requires_a_successful_ci_run_for_this_exact_main_sha(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Verify exact main image CI gate", text)
        self.assertIn("actions: read", text)
        self.assertIn("scripts/verify-exact-main-ci.py", text)
        self.assertIn('--workflow "ci.yaml"', text)
        self.assertIn('--workflow-path ".github/workflows/ci.yaml"', text)
        self.assertIn('--required-job "Image required checks pass"', text)
        self.assertIn("before registry access", text)
        self.assertNotIn("needs: preflight", text)

    def test_publish_checks_out_the_gate_script_before_running_it(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        publish = text.split("\n  publish:\n", 1)[1]
        self.assertLess(publish.index("Checkout exact pushed commit"), publish.index("Verify exact main image CI gate"))
        self.assertIn("persist-credentials: false", publish.split("Verify exact main image CI gate", 1)[0])
        self.assertLess(publish.index("Verify exact main image CI gate"), publish.index("Log in to GHCR"))

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
        self.assertEqual(text.count(f"uses: {EVIDENCE_USES}\n"), 2)
        action = EVIDENCE_ACTION.read_text(encoding="utf-8")
        self.assertIn("fetch-pinned-release-asset.sh", action)
        self.assertIn(SYFT_SHA256, action)
        self.assertIn(TRIVY_SHA256, action)
        script = EVIDENCE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--severity CRITICAL", script)
        self.assertIn("--exit-code 1", script)

    def test_node_source_pin_uses_sha_verified_pm_artifacts(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        lock_text = (ROOT / "pm/lock.json").read_text(encoding="utf-8")
        lock = json.loads(lock_text)
        node = lock["packages"]["node"]
        vulnerable = "sha256:9e6f9357d371591e32ab6f2d8a26d63bdd0d17c29eee3f4f3e7e454d9634bf73"
        self.assertNotIn(vulnerable, dockerfile + lock_text)
        # Preserve the security floor without rejecting reviewed upstream updates.
        self.assertGreaterEqual(tuple(map(int, node["version"].split("."))), (26, 7, 0))
        self.assertIn(
            f'/v{node["version"]}/node-v{node["version"]}-linux-x64.tar.xz',
            node["artifacts"]["linux-x64"]["url"],
        )
        self.assertTrue(node["artifacts"])
        for artifact in node["artifacts"].values():
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("pm", dockerfile)

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
        for path in (WORKFLOW, EVIDENCE_ACTION):
            text = path.read_text(encoding="utf-8")
            for action in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text):
                if action.startswith("./"):
                    continue
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", f"{path.name}: {action}")

    def test_documentation_states_release_boundary_and_handoff(self) -> None:
        text = DOC.read_text(encoding="utf-8").lower()
        for phrase in (
            "intentional",
            "workflow_dispatch",
            "publish: true",
            "fails open",
            "exact selected commit",
            "no manual reviewer prerequisite",
            "image digest",
            "fleet",
            "rollback",
            "does not deploy",
            "does not schedule them",
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


class ImageEvidenceStepTests(unittest.TestCase):
    """SBOM and critical scan run together but remain two fail-closed gates."""

    def _steps(self, job: str) -> list[dict]:
        import yaml

        return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][job]["steps"]

    def test_both_jobs_sbom_and_scan_the_built_image_before_any_consumer(self) -> None:
        for job in ("preflight", "publish"):
            with self.subTest(job=job):
                steps = self._steps(job)
                names = [step.get("name", "") for step in steps]
                build = next(i for i, s in enumerate(steps)
                             if str(s.get("uses", "")).startswith("docker/build-push-action@"))
                evidence = [i for i, s in enumerate(steps) if s.get("uses") == EVIDENCE_USES]
                self.assertEqual(len(evidence), 1, names)
                evidence = evidence[0]
                step = steps[evidence]
                self.assertNotIn("if", step)
                self.assertNotIn("continue-on-error", step)
                # Same image the build loaded and the runtime checks verified.
                self.assertEqual(steps[build]["with"]["tags"], "${{ env.TEST_IMAGE }}")
                self.assertEqual(step["with"], {
                    "image": "${{ env.TEST_IMAGE }}",
                    "sbom-output": "agent-image.spdx.json",
                    "scan-output": "trivy-image.json",
                })
                self.assertLess(build, evidence, names)
                for i, s in enumerate(steps):
                    if '"$TEST_IMAGE"' in s.get("run", "") and "docker run" in s.get("run", ""):
                        self.assertLess(i, evidence, names)
                compact = next(i for i, s in enumerate(steps)
                               if "scripts/compact-spdx-sbom.py" in s.get("run", ""))
                self.assertLess(evidence, compact, names)
                self.assertFalse(any("anchore/" in str(s.get("uses", "")) or "aquasecurity/" in str(s.get("uses", ""))
                                     for s in steps), names)

    def test_publication_starts_only_after_sbom_and_scan(self) -> None:
        steps = self._steps("publish")
        names = [step.get("name", "") for step in steps]
        evidence = next(i for i, s in enumerate(steps) if s.get("uses") == EVIDENCE_USES)
        for i, s in enumerate(steps):
            uses = str(s.get("uses", ""))
            run = s.get("run", "")
            if (uses.startswith(("docker/login-action@", "actions/attest-"))
                    or "docker push" in run or "scripts/verify-exact-main-ci.py" in run):
                self.assertGreater(i, evidence, names[i])
                self.assertNotIn("if", s, names[i])

    def test_action_reuses_the_replaced_actions_tools_and_cache(self) -> None:
        import yaml

        action = yaml.safe_load(EVIDENCE_ACTION.read_text(encoding="utf-8"))
        steps = action["runs"]["steps"]
        by_uses = {str(s.get("uses", "")).split("@")[0]: s for s in steps if s.get("uses")}
        by_name = {s["name"]: s for s in steps}
        # Same pinned Syft 1.51.1 / Trivy 0.70.0 archives, by published digest.
        syft = by_name["Install pinned Syft"]
        self.assertEqual(syft["id"], "syft")
        self.assertEqual(syft["env"]["SYFT_SHA256"], SYFT_SHA256)
        self.assertIn("syft_1.51.1_linux_amd64.tar.gz", syft["run"])
        self.assertIn('echo "cmd=$dir/syft" >> "$GITHUB_OUTPUT"', syft["run"])
        trivy = by_name["Install pinned Trivy"]
        self.assertEqual(trivy["env"]["TRIVY_SHA256"], TRIVY_SHA256)
        self.assertIn("trivy_0.70.0_Linux-64bit.tar.gz", trivy["run"])
        for step in (syft, trivy):
            self.assertNotIn("uses", step)
            self.assertIn("fetch-pinned-release-asset.sh", step["run"])
            self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        cache = by_uses["actions/cache"]["with"]
        self.assertEqual(cache["path"], "${{ github.workspace }}/.cache/trivy")
        self.assertEqual(cache["key"], "cache-trivy-${{ steps.date.outputs.date }}")
        run = steps[-1]
        self.assertEqual(run["env"]["TRIVY_CACHE_DIR"], "${{ github.workspace }}/.cache/trivy")
        self.assertEqual(run["env"]["SYFT_CMD"], "${{ steps.syft.outputs.cmd }}")
        self.assertIn("run.sh", run["run"])
        script = EVIDENCE_SCRIPT.read_text(encoding="utf-8")
        for flag in ("-o spdx-json", "--format json", "--scanners vuln", "--severity CRITICAL",
                     "--ignore-unfixed", "--exit-code 1"):
            self.assertIn(flag, script)
        self.assertIn("SYFT_CHECK_FOR_APP_UPDATE=false", script)
        self.assertIn('wait "$sbom_pid" || sbom_status=$?', script)
        self.assertIn('wait "$scan_pid" || scan_status=$?', script)

    # --- behaviour, with stub docker/syft/trivy on PATH -------------------

    STUBS = {
        "docker": """#!/usr/bin/env bash
case "$*" in
  *"{{.Id}}"*) cat "$STUB_STATE/image_id" ;;
  *"{{.Size}}"*) echo "${STUB_IMAGE_SIZE:-1000}" ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
""",
        "syft": """#!/usr/bin/env bash
[ "$1 $2" = "scan $IMAGE" ] && [ "$3 $4" = "-o spdx-json" ] || { echo "bad syft args $*" >&2; exit 98; }
touch "$STUB_STATE/syft.started"
for _ in $(seq 1 "${STUB_WAIT_TICKS:-50}"); do
  [ -e "$STUB_STATE/trivy.started" ] && { echo overlap > "$STUB_STATE/syft.saw"; break; }
  sleep 0.1
done
[ -n "${STUB_RETAG:-}" ] && echo sha256:other > "$STUB_STATE/image_id"
echo "syft log line" >&2
[ -n "${STUB_SYFT_FAIL:-}" ] && { echo "syft exploded" >&2; echo partial; exit 3; }
echo '{"spdxVersion": "SPDX-2.3"}'
""",
        "trivy": """#!/usr/bin/env bash
touch "$STUB_STATE/trivy.started"
for _ in $(seq 1 "${STUB_WAIT_TICKS:-50}"); do
  [ -e "$STUB_STATE/syft.started" ] && { echo overlap > "$STUB_STATE/trivy.saw"; break; }
  sleep 0.1
done
out=""
while [ $# -gt 0 ]; do
  case "$1" in --output) out=$2; shift ;; esac
  shift
done
echo '{"Results": []}' > "$out"
echo "trivy log line"
[ -n "${STUB_TRIVY_FAIL:-}" ] && { echo "CRITICAL found" ; exit 1; }
exit 0
""",
    }

    def _run(self, **env_overrides: str):
        import os

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        bin_dir, state, work = root / "bin", root / "state", root / "work"
        for d in (bin_dir, state, work):
            d.mkdir()
        for name, body in self.STUBS.items():
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        (state / "image_id").write_text("sha256:" + "a" * 64 + "\n", encoding="utf-8")
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(root),
            "RUNNER_TEMP": str(root),
            "IMAGE": "ghcr.io/example/image:sha-test",
            "SBOM_OUTPUT": str(work / "agent-image.spdx.json"),
            "SCAN_OUTPUT": str(work / "trivy-image.json"),
            "SYFT_CMD": str(bin_dir / "syft"),
            "TRIVY_CACHE_DIR": str(root / "cache"),
            "STUB_STATE": str(state),
        }
        env.update(env_overrides)
        result = subprocess.run(["bash", str(EVIDENCE_SCRIPT)], cwd=work, env=env,
                                capture_output=True, text=True, timeout=60, check=False)
        return result, state, work

    def test_runs_both_tools_concurrently_and_writes_unchanged_outputs(self) -> None:
        result, state, work = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((state / "syft.saw").exists(), "syft never overlapped trivy")
        self.assertTrue((state / "trivy.saw").exists(), "trivy never overlapped syft")
        self.assertEqual(json.loads((work / "agent-image.spdx.json").read_text())["spdxVersion"], "SPDX-2.3")
        self.assertTrue((work / "trivy-image.json").is_file())
        self.assertIn("syft log line", result.stdout)
        self.assertIn("trivy log line", result.stdout)

    def test_sbom_failure_fails_closed_with_logs_and_no_partial_sbom(self) -> None:
        result, _, work = self._run(STUB_SYFT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SBOM generation failed with exit 3", result.stdout)
        self.assertIn("syft exploded", result.stdout + result.stderr)
        self.assertFalse((work / "agent-image.spdx.json").exists())
        self.assertTrue((work / "trivy-image.json").is_file())

    def test_scan_failure_fails_closed_and_keeps_report(self) -> None:
        result, _, work = self._run(STUB_TRIVY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Critical vulnerability scan failed with exit 1", result.stdout)
        self.assertIn("CRITICAL found", result.stdout + result.stderr)
        self.assertTrue((work / "trivy-image.json").is_file())

    def test_both_failures_are_reported(self) -> None:
        result, _, _ = self._run(STUB_SYFT_FAIL="1", STUB_TRIVY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SBOM generation failed", result.stdout)
        self.assertIn("Critical vulnerability scan failed", result.stdout)

    def test_image_changing_under_the_tools_fails_closed(self) -> None:
        result, _, _ = self._run(STUB_RETAG="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed from", result.stdout)

    def test_low_disk_falls_back_to_sequential_and_still_gates(self) -> None:
        huge = str(1 << 60)
        result, state, _ = self._run(STUB_IMAGE_SIZE=huge, STUB_WAIT_TICKS="2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sequentially", result.stdout)
        self.assertFalse((state / "syft.saw").exists())
        result, _, _ = self._run(STUB_IMAGE_SIZE=huge, STUB_WAIT_TICKS="2", STUB_TRIVY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        result, _, _ = self._run(STUB_IMAGE_SIZE=huge, STUB_WAIT_TICKS="2", STUB_SYFT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)



if __name__ == "__main__":
    unittest.main()
