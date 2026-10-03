# Fork Agent image publication

This fork packages the complete Hermes Agent runtime from its existing root `Dockerfile`. It does not maintain a selected-path copy of upstream source.

## Boundaries

- Pull requests build, smoke-test, generate an image SBOM, and run the critical-vulnerability gate without registry credentials.
- Publication is automatic: every push to `main` that changes an image input runs the protected publish job. A `detect` job classifies the pushed range first (`scripts/ci/classify_changes.py`, `docker` lane); a push that touches only files the image never copies (`docs/`, `tests/`, `website/`, CI workflows other than this one — all in `.dockerignore`) skips both build and publication, so the fleet keeps the previous digest for that commit. A forced or first push, or any diff the classifier cannot read, fails open and publishes. A `workflow_dispatch` with `publish: true` remains available to re-run publication for the selected `main` commit (for example after a transient registry failure); a `publish: false` dispatch runs only the credential-free preflight.
- Publication is accepted only for an exact pushed commit on `main`, whether it arrives by push or by dispatch.
- Publication has no manual reviewer prerequisite. It requires the `Image required checks pass` job from the newest `CI` push run for this exact `main` SHA and its current attempt. The gate waits up to 30 minutes, polling every 20 seconds, before any registry credential is used. Missing, skipped, failed, cancelled, duplicate or stale job proof blocks publication. A rerun cannot borrow success from an earlier attempt.
- The image aggregate reuses the existing CI results: Python tests (including runtime E2E and upgrade configuration, entrypoint, profile, and state-import coverage), JS tests for the bundled web/TUI, lint, dependency/security checks and source guards. Native installation/self-update, OS-specific tests, Windows installers, bootstrap installers, Desktop E2E and the documentation site are separate surfaces; they keep running and remain in the full `All required checks pass` merge gate, but do not delay Linux image publication. No tests are disabled or marked `continue-on-error`. Upstream and stable-release gates are unchanged.
- Publication additionally requires the image's own runtime checks and critical-vulnerability scan to pass; a green `CI` run alone does not publish.
- The workflow publishes only `ghcr.io/mekenthompson/hermes-agent:sha-<commit>` and does not create `latest`.
- This workflow does not deploy containers, update Fleet, mutate profile state, or migrate production.

## Evidence and provenance

The preflight job builds the complete image, verifies `/etc/hermes/image-provenance.json` contains the workflow commit, fork image identity, and installed runtime version, runs the Hermes version command, emits a full file-level SPDX JSON image SBOM, derives a bounded package-level SPDX document for GitHub attestation, and fails on fixable critical vulnerabilities. The evidence artifact retains both SPDX documents for review. The package-level document preserves every package identity and package/document dependency relationship while removing file and snippet records, relationships involving removed elements, and package-internal file-derived fields (`hasFiles`, `packageVerificationCode`, and `licenseInfoFromFiles`). It filters `documentDescribes` to retained packages, marks packages `filesAnalyzed: false`, recursively rejects any surviving removed-element ID, and remains within GitHub's 16 MiB attestation limit.

The protected publish job builds, runtime-checks, SBOMs, and scans its own trusted `linux/amd64` candidate without any registry credential (its base images are public), then verifies the successful image aggregate for the exact pushed `main` SHA, and only then logs in to GHCR. Other surfaces' CI may still be running or have failed; the image aggregate itself must have passed. It pushes that image once under a unique `candidate-<run-id>-<run-attempt>` tag, captures the digest printed by that push (never by resolving a mutable tag), verifies the remote OCI manifest config digest against the locally scanned Docker image ID, and uses an immutable `repository@sha256:...` reference for metadata-only SHA-tag promotion. GitHub-signed build-provenance and package-SBOM attestations are emitted for that resulting digest. Multi-architecture publication is intentionally deferred until it can preserve this same build-once promotion guarantee.

The scan, exact-candidate verification, and CI gate are automated release prerequisites. They do not authorize deployment.

## Fleet handoff

A successful publication emits `agent-image-manifest.json` containing:

- the public image repository;
- the exact source revision;
- the registry image digest;
- the immutable `repository@sha256:...` image reference.

Fleet must consume the image digest from this manifest. It must not consume the SHA tag as its parent reference.

The hand-off to Fleet is automatic. `.github/workflows/release-handoff.yml` runs on `workflow_run` when a "Fork Agent Image" run for `main` completes successfully. It never builds or publishes anything: it downloads the `agent-image-manifest-<sha>` artifact of the triggering run by run ID, mints a short-lived token for the GitHub App `hermes-release-bot` scoped to `mekenthompson/hermes-fleet` only, and runs `scripts/ci/open_fleet_pin_pr.py`, which:

- validates the manifest (schema 1, repository `ghcr.io/mekenthompson/hermes-agent`, a 40-hex `revision` equal to the triggering run's `head_sha`, a `sha256` digest, and `immutable_ref == repository@digest`); a `workflow_run` job sees `github.sha` as the current `main` HEAD, so the workflow binds every step to `github.event.workflow_run.head_sha` and `.id` instead;
- does nothing when hermes-fleet `main` already carries an identical `release/agent-image-manifest.json`;
- otherwise creates branch `release/pin-agent-<short sha>` from hermes-fleet `main`, commits the manifest through the contents API as `release: pin Agent <short> (<digest12>)`, opens a PR titled `release: pin Agent <short>` (body: revision, digest, immutable ref, source run URL), and arms squash auto-merge with `gh pr merge --auto --squash`. hermes-fleet's required checks remain the merge gate;
- serializes all source SHAs through one Fleet handoff queue and waits for each PR to merge. If strict `main` makes an open PR `BEHIND`, it calls GitHub's update-branch REST operation, observes the resulting new head, and waits for its protection/check state and merge. It never force-pushes or bypasses protection;
- retries bounded, idempotent reads on rate-limit, 5xx, and network failures. Ambiguous branch, contents, PR, and auto-merge writes are reconciled by reading the expected branch base, exact manifest content, or matching open PR rather than replayed blindly;
- fails closed when the branch already exists with different content (a half-finished or foreign branch of the same name is never built upon), when several PRs are open from the branch, a refreshed head changes the exact pin, protection fails, or the PR does not merge within the 12-minute wait.

When "Fork Agent Image" succeeded but its `publish` job was skipped (a docs-only push), no manifest artifact exists; the handoff job then ends successfully with a notice and pins nothing. The App private key (`RELEASE_BOT_PRIVATE_KEY`) and app ID (`RELEASE_BOT_APP_ID`) are referenced only by this workflow, whose job holds `actions: read` and `contents: read` and no `packages` permission, so the key is never present in a job that can write the registry. The script accepts `--dry-run` for local rehearsal against a real manifest: it performs every read and prints every write without performing it.

## Rollback

Rollback selects a previously reviewed `agent-image-manifest.json` and restores the prior image digest in Fleet. Rebuilding an old tag is not rollback. Publishing this Agent image alone does not deploy or restart any Fleet profile.

Ordinary fork CI partitions the existing upgrade inventory between runtime and native installation calls to the same test workflow. Each file runs once; new root files and new suites default to runtime coverage. Upstream and stable releases keep the original combined test call. Native installation failures remain visible and fail the full source merge gate.
