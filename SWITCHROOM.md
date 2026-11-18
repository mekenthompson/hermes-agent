# Switchroom and this fork

`switchroom/hermes-agent` is the Hermes Agent fork that [Switchroom](https://switchroom.ai) builds on. Hermes Agent itself is made by [Nous Research](https://github.com/NousResearch/hermes-agent), and almost everything in this repository is their work. This fork adds a small set of add-ons and packages the result as the agent image the Switchroom distribution uses. Switchroom is not affiliated with or endorsed by Nous Research.

## The flow

```text
NousResearch/hermes-agent          upstream Hermes Agent, unchanged
        │  regular upstream syncs (merge commits)
        ▼
switchroom/hermes-agent            this fork: upstream + Switchroom add-ons
        │  Fork Agent Image workflow (scan, provenance, SBOM)
        ▼
ghcr.io/switchroom/hermes-agent    the agent image, pinned by digest
        │  Release Handoff opens a pin PR
        ▼
switchroom/switchroom              the Switchroom distribution: a digest-pinned
                                   child image, sidecars, plugins and contracts
```

1. **Upstream.** Nous Research develops Hermes Agent in `NousResearch/hermes-agent`.
2. **This fork.** Upstream code, plus the add-ons below. The fork keeps its own CI on GitHub's standard hosted runners and turns off upstream product workflows it does not ship (desktop installers, the docs site, and similar).
3. **Agent image.** A dispatched run of `.github/workflows/fork-agent-image.yml` on `main` builds the complete runtime from the root `Dockerfile`, checks it, scans it for critical vulnerabilities, and publishes `ghcr.io/switchroom/hermes-agent:sha-<commit>` with build-provenance and SBOM attestations. There is no `latest` tag. Consumers pin the digest. Details: [`website/docs/developer-guide/fork-agent-image.md`](website/docs/developer-guide/fork-agent-image.md).
4. **Distribution.** `.github/workflows/release-handoff.yml` hands the published digest to [`switchroom/switchroom`](https://github.com/switchroom/switchroom) as an auto-merging pin PR, gated by that repository's own checks. Switchroom then builds its child image on top of that exact digest.

Publishing an agent image does not deploy anything. Each step is a separate, reviewed change.

## What the add-ons are

The add-ons are the reliability and operations work a long-running, one-container-per-agent deployment needs. They fall into a few groups:

- **Long-running work.** Kanban worker recovery across gateway restarts, respawn and block-loop guards, durable human checkpoints, and clarification routing for background tasks.
- **Messaging delivery.** Gateway fixes for restart drains, redelivery, completion delivery and notification transport, plus Slack and Telegram delivery fixes (thread handling, voice replies, per-topic voice mode).
- **Approvals and safety.** Approval flows that keep a turn's command authority, smart approval of computer-use actions, and a sandboxed local Chromium for managed profiles.
- **Bot Screen and Desktop.** Screen handoff and clipboard copy-out for an agent's own desktop session.
- **Image and release pipeline.** The Fork Agent Image workflow, the Release Handoff, and fork CI tuned to cheap hosted runners.

The canonical list is the history. With a remote named `upstream` pointing at Nous, `git log --no-merges upstream/main..main` shows every commit this fork has on top of upstream.

## Upstream syncs

- The fork regularly merges Nous `main` into this fork's `main` through a reviewed pull request. It lands as a merge commit, never a squash, so upstream stays an ancestor and its history stays intact.
- Conflicts are resolved in favour of upstream unless an add-on depends on the fork's behaviour. An add-on that upstream has since fixed is dropped rather than kept.
- A sync merges only on green CI for the exact head. A new agent image is published from the merged `main`, and Switchroom pins it like any other release.
- Generic fixes should go upstream. The aim is a small, shrinking set of add-ons.

## For agents working on this fork

Read this before changing anything here. Upstream's [`AGENTS.md`](AGENTS.md) still applies to the code itself.

- **The chain.** `NousResearch/hermes-agent` → this fork → `ghcr.io/switchroom/hermes-agent` (published by `fork-agent-image.yml`, pinned by digest) → `switchroom/switchroom` (pin PR from `release-handoff.yml`, then its Fleet child image) → a private overlay's Stage A pin PR → a private candidate image → a host-run rollout. Each arrow is a separate reviewed change. Merging here deploys nothing.
- **Upstream syncs** land as a reviewed merge commit of Nous `main`, never a squash or rebase, so the Nous parent stays in history. Keep every add-on unless upstream now does the same job. Resolve each conflict on purpose and say why in the PR. Merge only on green CI for the exact head, then publish a new image from the merged `main`. Do not start a sync unless one has been asked for.
- **Where add-ons live.** There is no patch directory. Add-ons are ordinary commits on `main` on top of upstream; `git log --no-merges upstream/main..main` lists them. Keep them small and in the area they change, and prefer a plugin or skill over a core edit so syncs stay cheap. Fork-only workflows (`fork-agent-image.yml`, `release-handoff.yml`) are gated on `github.repository == 'switchroom/hermes-agent'`.
- **Upstream files.** Keep edits to upstream-owned files such as `README.md` and `AGENTS.md` to a line or two at the top, so syncs rarely conflict. Fork context belongs here.
- **GitHub access.** The `switchroom` organization rejects fine-grained personal access tokens with a lifetime over 366 days. A 403 from a long-lived token on `switchroom/*` is that policy, not a broken login. Use an org-approved token for this org.
- **Old name.** This fork moved from `mekenthompson/hermes-agent` on 2026-11-18, and GitHub redirects the old URL. Never create a repository at the old name, because that breaks the redirect. Images already published under `ghcr.io/mekenthompson/hermes-agent` stay valid by digest. Never delete or retag them.

## Where issues go

- **Hermes Agent bugs and features** that reproduce on upstream: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and its contribution guide.
- **Switchroom distribution, packaging or this fork's add-ons**: [switchroom/switchroom](https://github.com/switchroom/switchroom). Switchroom is in early access, and [switchroom.ai](https://switchroom.ai) has the current contact route.
- **Security**: never in a public issue. For upstream Hermes, follow [`SECURITY.md`](SECURITY.md). For Switchroom, follow the [`switchroom/switchroom` security policy](https://github.com/switchroom/switchroom/blob/main/SECURITY.md).
