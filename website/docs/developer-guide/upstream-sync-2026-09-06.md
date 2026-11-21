# September 6 upstream reconciliation

This release candidate imports the Nous source snapshot
`245e48008fa814b3251f50755eb656bd9fb86cb1` while retaining the fork's house
behavior from `a153086858e82a8d36141fd9fd65b6cc6f0f8b46`.

The effective prior imported source snapshot is
`63279301bcbdc185c1b07b98a9312eb0c862f26d`. The old graph merge base is not an
accurate source-delta baseline because earlier imports did not retain all
upstream ancestry. Future source reconciliation must use the explicit imported
snapshot above rather than interpreting a squash merge as thousands of missing
source changes.

The local integration commit retains both parents for audit. Protected `main`
requires linear history, so landing uses the normal reviewed squash path; no
branch protection is weakened. This record identifies the imported source even
when the integration parent relationship is not retained on `main`.

House gateway dispatch and service lifecycle behavior is ported into the new
split gateway modules rather than replacing upstream's new facade with the old
monolithic `gateway/run.py`. Other retained house areas include host-derived
tool invocation identity, per-profile task-card identity, shared session state,
external-process provider resolution, credential-vs-provider-failure
classification, and the fork image/provenance workflows.

Source tests, protected-main acceptance, image publication and deployment are
separate gates. This source record is not image or deployment evidence.
