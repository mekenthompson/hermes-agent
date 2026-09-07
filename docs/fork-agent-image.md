# Fork Agent image publication

This fork packages the complete Hermes Agent runtime from its existing root `Dockerfile`. It does not maintain a selected-path copy of upstream source.

## Boundaries

- Pull requests build, smoke-test, generate an image SBOM, and run the critical-vulnerability gate without registry credentials.
- Publication is automatic: every push to `main` runs the protected publish job. A `workflow_dispatch` with `publish: true` remains available to re-run publication for the selected `main` commit (for example after a transient registry failure); a `publish: false` dispatch runs only the credential-free preflight.
- Publication is accepted only for an exact pushed commit on `main`, whether it arrives by push or by dispatch.
- Publication has no manual reviewer prerequisite. It fails closed unless the repository's `CI` push run for this exact `main` SHA completed successfully; a green run for a different SHA is not accepted. Because the image build starts in parallel with `CI`, the gate waits (up to 30 minutes, polling every 20 seconds) for the latest `CI` run of that SHA to complete before any registry credential is used; a run that is cancelled, superseded by a later run for the same SHA, fails, or never completes within the timeout blocks publication.
- Publication additionally requires the image's own runtime checks and critical-vulnerability scan to pass; a green `CI` run alone does not publish.
- The workflow publishes only `ghcr.io/mekenthompson/hermes-agent:sha-<commit>` and does not create `latest`.
- This workflow does not deploy containers, update Fleet, mutate profile state, or migrate production.

## Evidence and provenance

The preflight job builds the complete image, verifies `/etc/hermes/image-provenance.json` contains the workflow commit, fork image identity, and installed runtime version, runs the Hermes version command, emits a full file-level SPDX JSON image SBOM, derives a bounded package-level SPDX document for GitHub attestation, and fails on fixable critical vulnerabilities. The evidence artifact retains both SPDX documents for review. The package-level document preserves every package identity and package/document dependency relationship while removing file and snippet records, relationships involving removed elements, and package-internal file-derived fields (`hasFiles`, `packageVerificationCode`, and `licenseInfoFromFiles`). It filters `documentDescribes` to retained packages, marks packages `filesAnalyzed: false`, recursively rejects any surviving removed-element ID, and remains within GitHub's 16 MiB attestation limit.

The protected publish job builds, runtime-checks, SBOMs, and scans its own trusted `linux/amd64` candidate without any registry credential (its base images are public), then waits for and verifies the successful `CI` run for the exact pushed `main` SHA, and only then logs in to GHCR. It pushes that image once under a unique `candidate-<run-id>-<run-attempt>` tag, captures the digest printed by that push (never by resolving a mutable tag), verifies the remote OCI manifest config digest against the locally scanned Docker image ID, and uses an immutable `repository@sha256:...` reference for metadata-only SHA-tag promotion. GitHub-signed build-provenance and package-SBOM attestations are emitted for that resulting digest. Multi-architecture publication is intentionally deferred until it can preserve this same build-once promotion guarantee.

The scan, exact-candidate verification, and CI gate are automated release prerequisites. They do not authorize deployment.

## Fleet handoff

A successful publication emits `agent-image-manifest.json` containing:

- the public image repository;
- the exact source revision;
- the registry image digest;
- the immutable `repository@sha256:...` image reference.

Fleet must consume the image digest from this manifest. It must not consume the SHA tag as its parent reference.

## Rollback

Rollback selects a previously reviewed `agent-image-manifest.json` and restores the prior image digest in Fleet. Rebuilding an old tag is not rollback. Publishing this Agent image alone does not deploy or restart any Fleet profile.
