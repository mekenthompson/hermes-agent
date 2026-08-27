# House fork policy

This repository is the public house fork of
[`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent).
House image and fleet release policy lives in the private
`mekenthompson/hermes-fleet` repository at `docs/build-and-release.md`.

## Remotes and main

Canonical clones use:

```text
origin   https://github.com/mekenthompson/hermes-agent.git
upstream https://github.com/NousResearch/hermes-agent.git
```

`main` is an upstream mirror. Update it only by fetching `upstream` and
fast-forwarding when `origin/main` has zero unique commits. Never merge house
work into `main`, rebase published shared branches, or force-push.

## House core branches

Use a fresh `house/core-*` branch from a recorded upstream commit when the fleet
needs an unmerged Hermes core fix. Keep one concern per commit, reproduce the bug,
add regression coverage, and open an upstream PR when the fix is generally useful.

Before building, classify every fork-only change as:

```text
keep-as-core | already-upstream | drop-moved-to-fleet | drop-obsolete | rewrite-then-keep
```

Historical `house/*` branches are retained as audit evidence. They are not current
build candidates and must not be mass-deleted or force-updated. In particular,
`house/alignment-79b8703d00` is a historical alignment stack, not the base for a
new release.

## Layer boundary

Hermes core contains framework changes only. Linear, Kokoro, Claude ACP provider
packaging, adapter packages, browser/tool dependencies, and fleet binaries belong
in `mekenthompson/hermes-fleet`. SOUL, skills, MCP definitions, cron definitions,
and model selection belong in profile configuration. Tokens, OAuth state,
sessions, memories, and private data never belong in this repository or an image.

A custom core image must be built from the complete clean checkout at one pushed
commit. Do not build an older image, copy selected `gateway/`, `hermes_cli/`,
`agent/`, `providers/`, or `tools/` files over it, and label the result as a full
core build. OCI revision and the house core-commit label must both equal the source
checkout commit.

Building a core image does not authorize a fleet capability build, profile
enablement, Compose change, or rollout. Those are separate reviewed stages.
