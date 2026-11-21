# September 6 upstream reconciliation (follow-up b)

This follow-up imports the Nous source snapshot
`12871bd01ede1c6eaf3bf456066d1e010034185f` on top of the fork base
`60bda97902164b31b43d90beddccc1c8636ce506` (the reviewed KEN-330
reconciliation, PR #25, plus KEN-461, PR #26).

The previous imported source snapshot is
`245e48008fa814b3251f50755eb656bd9fb86cb1` (see
`docs/upstream-sync-2026-09-06.md`). The source delta for this import is
exactly `git diff 245e48008f..12871bd01e`: 21 upstream commits touching 39
files (desktop spawn-priority/status-bar work, tui-gateway reattach
refactors and orphan-timer fence, an SSH probe isolation fix, the openrouter
per-model `provider_routing.models.<id>` feature, a sessions compaction
ordering fix, and three contributor mappings).

## Method

The branch carries a real merge commit of `12871bd01e` (both parents retained
for audit). Because fork `main` is a squash lineage, git's graph merge base
(`18a76be124`) is far older than the true source baseline, so the merge
reported 38 conflicts, most of them in files upstream did not change after
`245e48008f`. Resolution rule, applied per file:

1. **Upstream unchanged since `245e48008f`** (21 files) — keep the fork's
   reviewed reconciliation (`--ours`). These are the house-seam files:
   `gateway/run.py`, `gateway/run_shutdown.py`, `gateway/run_startup.py`,
   `gateway/run_turn_runner.py`, `hermes_cli/auth.py`,
   `hermes_cli/dashboard_auth/base.py`, `hermes_cli/dashboard_auth/middleware.py`,
   `hermes_cli/plugins.py`, `hermes_cli/runtime_provider.py`,
   `hermes_cli/runtime_provider_backends.py`, `hermes_cli/web_routers/status.py`,
   `hermes_cli/web_server.py`, `hermes_state_messages.py` (see 2),
   `plugins/dashboard_auth/_shared.py`, `plugins/dashboard_auth/nous/__init__.py`,
   `tools/registry.py`, and the fork tests
   `tests/agent/test_copilot_acp_client.py`,
   `tests/e2e/test_relay_native_openai_stream.py`,
   `tests/hermes_cli/test_external_process_provider_seam.py`,
   `tests/hermes_cli/test_local_quickstart.py`,
   `tests/plugins/dashboard_auth/test_opaque_bearer_not_unreachable.py`,
   `web/src/pages/SessionsPage.test.tsx`. No fork test was dropped.
2. **Fork identical to `245e48008f`, upstream changed** (16 files) — take
   upstream (`--theirs`): `agent/chat_completion_helpers.py`,
   `agent/prompt_builder.py`, `agent/turn_recovery.py`,
   `apps/desktop/electron/pool-spawn-coordinator.ts` (+ test),
   `apps/desktop/electron/preload.ts`, `apps/desktop/src/store/statusbar-prefs.ts`,
   `hermes_state_messages.py`, `plugins/model-providers/openrouter/__init__.py`,
   `tests/agent/test_prompt_builder.py`, `tools/environments/ssh.py`,
   `tools/terminal_tool_backends.py`, `tui_gateway/methods_prompt.py`,
   `tui_gateway/methods_session.py`, `tui_gateway/server.py`,
   `tui_gateway/session_lifecycle.py`.
3. **Both changed** (1 file) — `apps/desktop/electron/main.ts`. The fork's
   KEN-461 change (persistent remote gateway session tokens: ticket minting
   with a configured session token, `resolveRemoteWsAuthTransport`,
   `mintConfiguredTokenTicket`) and upstream's foreground spawn-priority
   change touch disjoint regions. The upstream patch for this file was
   applied verbatim on top of the fork's version.

Verification of the resolution: for every one of the 39 files in the source
delta, the diff `60bda97902..HEAD` is line-for-line identical to the diff
`245e48008f..12871bd01e`; the file list is identical too. The only residual
difference between `HEAD` and upstream `12871bd01e` within the delta files is
the KEN-461 delta in `main.ts` (103 insertions, 13 deletions), unchanged
from PR #26. House seams outside the delta (profile-local service
dispatch/lifecycle, host-derived tool invocation context, shared session
state and task-card identity, provider resolution, OAuth error
classification, fork image/provenance workflows) are untouched.

## Contributor attribution

Author emails in the 21 imported commits: `teknium1`, `hermes-seaeye[bot]`,
`kshitijk4poor` and `bounce12340` (id+login noreply, auto-resolved by the
CI gate), `cursoragent@cursor.com` (skipped by the gate), and
`Collin.Snitchler@pm.me`, `bear@bearhuddleston.dev`,
`ruichenzhou@outlook.com` (all mapped under `contributors/emails/`, two of
them added by the imported commits themselves).
`scripts/audit_pr_attribution.py --fix` added nothing. It reports
`Leanolf+212960991@users.noreply.github.com` because its local regex is
stricter than CI's; that author is pre-snapshot history (commit
`aef409343d`, already inside `245e48008f`) and is skipped by CI's
`+…@users.noreply.github.com` rule, as on PR #25.

## Tests run

Environment: `uv sync --locked --python 3.11 --extra all --extra dev ...`
(the CI `tests.yml` invocation), Python 3.11; `npm ci --ignore-scripts` at the
workspace root (0 vulnerabilities reported by npm audit for this lock).

- Python: the fork-specific gateway tests (`tests/gateway` files referencing
  `run_turn`, `run_plugin_services`, `turn_context`, `task_card`,
  `dispatch`: 166 files), `tests/hermes_cli/test_dashboard_auth_*`,
  `tests/plugins/dashboard_auth`, `tests/test_tui_gateway_server.py`,
  `tests/ci`, `tests/tui_gateway`, `tests/hermes_state`, and every test file
  touched by the 21 commits (`tests/agent/test_per_model_provider_routing.py`,
  `tests/agent/test_prompt_builder.py`,
  `tests/hermes_state/test_get_messages_include_compacted.py`,
  `tests/tools/test_ssh_environment.py`,
  `tests/tui_gateway/test_ws_orphan_races.py`):
  **5567 passed, 2 failed, 41 skipped, 1 xfailed** (500 s).
  - `tests/test_tui_gateway_server.py::test_model_options_preserves_canonical_custom_row_after_agent_init`
    fails identically on fork base `60bda97902` and on pure upstream
    `12871bd01e` in this workstation environment (also with an empty
    `HERMES_HOME`); it passed in the fork's CI on PR #25. Pre-existing,
    environment-specific, not introduced here.
  - `tests/tui_gateway/test_serve_exit_flush.py::test_sigterm_flushes_populated_session_into_state_db`
    passed in the first (`-x`) run and 6/6 on three isolated reruns; it failed
    once while the suite shared the host with `npm ci` and vitest. The file
    is unchanged upstream since `245e48008f`. Treated as load-sensitive.
- `uv run ruff check` on all changed Python files: all checks passed.
- Desktop (`apps/desktop`): `npm run check:lint` (tsc x3 + eslint) passed,
  0 errors, 124 pre-existing warnings. Vitest, electron project: the touched
  `backend-dial-claim`, `pool-spawn-coordinator` tests plus the KEN-461
  seam tests `connection-config`, `gateway-ticket-transport`,
  `remote-ws-headers`: **170 passed** (5 files). Vitest, ui project: the
  touched `statusbar-visibility`, `profile-routing`,
  `gateway-spawn-priority`, `statusbar-prefs` tests: **69 passed** (4 files).
- `ui-tui`: `npm run typecheck` and `npm run lint` passed (0 errors).
- `web`: house `src/pages/SessionsPage.test.tsx` passed (1/1).
- Not run here: the full desktop suites (`test:desktop:all`, platforms),
  Rust/installer tests, e2e/OS-specific CI jobs, image verifiers and plugin
  doctor; those remain CI and release gates.

## Boundaries

Protected `main` requires linear history; this lands by reviewed squash. The
merge commit on the branch identifies the imported source even when the
parent relationship is not retained on `main`. Source tests, protected-main
acceptance, image publication and deployment remain separate gates; this
record is not image or deployment evidence.
