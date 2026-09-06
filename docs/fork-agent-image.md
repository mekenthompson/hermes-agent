# Fork Agent image publication

This fork packages the complete Hermes Agent runtime from its existing root `Dockerfile`. It does not maintain a selected-path copy of upstream source.

## Boundaries

- Pull requests build, smoke-test, generate an image SBOM, and run the critical-vulnerability gate without registry credentials.
- Manual publication is the only publishing path. Merging to `main` does not publish an image.
- Publication is accepted only for an exact pushed commit selected from `main`.
- The `agent-image-publish` GitHub environment must require environment approval before its first use.
- The workflow publishes only `ghcr.io/mekenthompson/hermes-agent:sha-<commit>` and does not create `latest`.
- This workflow does not deploy containers, update Fleet, mutate profile state, or migrate production.

## Evidence and provenance

The preflight job builds the complete image, verifies `/etc/hermes/image-provenance.json` contains the workflow commit, fork image identity, and installed runtime version, runs the Hermes version command, emits a full file-level SPDX JSON image SBOM, derives a bounded package-level SPDX document for GitHub attestation, and fails on fixable critical vulnerabilities. The evidence artifact retains both SPDX documents for review. The package-level document preserves every package identity and package/document dependency relationship while removing file and snippet records, relationships involving removed elements, and package-internal file-derived fields (`hasFiles`, `packageVerificationCode`, and `licenseInfoFromFiles`). It filters `documentDescribes` to retained packages, marks packages `filesAnalyzed: false`, recursively rejects any surviving removed-element ID, and remains within GitHub's 16 MiB attestation limit.

The protected publish job downloads and re-verifies that same scanned `linux/amd64` candidate, pushes it without rebuilding, and emits GitHub-signed build-provenance and package-SBOM attestations for the resulting registry digest. Multi-architecture publication is intentionally deferred until it can preserve this same build-once promotion guarantee.

Review the SBOM for dependency and license anomalies and review the vulnerability report before granting environment approval. The environment approval is the human release gate; it is not replaced by a successful build.

## Fleet handoff

A successful publication emits `agent-image-manifest.json` containing:

- the public image repository;
- the exact source revision;
- the registry image digest;
- the immutable `repository@sha256:...` image reference.

Fleet must consume the image digest from this manifest. It must not consume the SHA tag as its parent reference.

## Rollback

Rollback selects a previously reviewed `agent-image-manifest.json` and restores the prior image digest in Fleet. Rebuilding an old tag is not rollback. Publishing this Agent image alone does not deploy or restart any Fleet profile.
