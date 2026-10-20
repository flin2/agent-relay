# Changelog

## 0.1.0 — 2026-10-20

- Stage 1 packages the OpenCode subscription and direct API wrappers, model and session helpers, and the independent Claude Code and Codex plugin surfaces.
- Stage 2 replaces file-backed status with a schema-versioned SQLite WAL lifecycle store, detached supervisor and process-group ownership, confirmed cancellation, crash reconciliation, and explicit artifact pruning.
- Stage 3 adds the Codex JSONL execution adapter, current-turn terminal classification, explicit read-only and write modes, and exact-session continuation.
- Stage 4 adds backend selection across Claude commands and Codex skills, persistent manual collection commands, Codex queue delivery records, and Claude waiter registration and completion records.
- OpenCode idle and truncation retries run as separate supervisor-managed attempts; wrappers perform one invocation when called directly.
- macOS guards observe held leader exits with kqueue and report the final reap status.
- Stage 5 now preserves kqueue exit events delivered during registration, checks each shell file independently, and retains Codex write-denial checks when JSONL command events are available.
- Stage 5 live checks identify cancellation fixtures by a unique host-visible command token, classify OpenCode tool children that escape the managed group as a known gap, and skip Codex 0.163 permission enforcement when JSONL contains no command items.
- Stage 5 follow-up matches the cancellation fixture by both its unique token and the `sleep` command name, and reports successful OpenCode plan-agent shell writes as a known gap before removing the probe file.
- State, output, event, and notification outcomes remain separate so a successful task result does not depend on callback delivery.
- Stage 5 added the cross-platform CI matrix and opt-in live-check script; live checks ran on Linux against Codex CLI 0.163.0 and OpenCode's subscription route, with both passing authorized edits, WAL/status concurrency, and collection after launcher exit.
- CI passed on Ubuntu 22.04/Python 3.8, Ubuntu/Python 3.12, and macOS/Python 3.12. Codex event shape and idle-thread queue delivery were verified, including receipt confirmation; Claude background-waiter re-invocation was verified once. Per job, the relay cannot observe whether Claude re-invoked the originating conversation.
- Accepted limitations remain: OpenCode read-only uses an advisory `plan` agent unless OpenCode permissions deny shell; OpenCode tool commands can survive cancellation in their own sessions; Codex tool-child cleanup relies on Codex itself.
- Codex jobs whose fresh run reports conflicting thread identities clear the stored identity and cannot be continued, including older records that still store one.
- Only busy or locked SQLite errors are retried; read-only, corrupt, or I/O storage failures end the job's retry path while process-group cleanup proceeds and the reason is logged.
- Notification rows abandoned by an exited supervisor or Claude waiter settle as uncertain, and reconciliation-created outcomes record uncertain delivery directly, so pruning is no longer blocked by stale pending rows.
- The OpenCode API and subscription wrappers share one runner, `oc-run.sh`, without changing their options or exit status.
