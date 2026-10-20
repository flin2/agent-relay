# agent-relay

An independent plugin package for delegating bounded tasks from Claude Code or Codex through the OpenCode CLI, the Codex CLI, or the Claude Code CLI. Both hosts call the same detached job manager and can select any of the three backends.

OpenCode supports the subscription and direct API routes. Codex uses JSONL execution, current-turn terminal evidence, and continuation by the exact thread recorded in a parent job. New work requires an explicit backend; OpenCode also requires a route. Jobs default to backend-specific read-only modes, and resume returns to that mode unless the new job explicitly includes `--write`. Codex read-only is enforced by its sandbox (`--sandbox read-only` for new jobs and `sandbox_mode="read-only"` for resumed jobs). OpenCode selects the `plan` agent, which avoids edit tools but still permits shell writes unless OpenCode permissions deny them; configure OpenCode to deny shell for the plan agent when enforcement is required.

Claude jobs run `claude -p` under the same lifecycle. The route (`--route sub` or `--route api`) is optional: when it is omitted, the launcher picks one from the subscription login or `ANTHROPIC_API_KEY`, records it on the job, and fails before launch if neither is available. Claude jobs are read-only by default with `--safe-mode --restricted --strict-mcp-config`, Read, Grep, Glob, and a prefix-matched allow list of read-only Git commands; `--write` selects `acceptEdits`. Resume returns to read-only unless `--write` is restated.

See the [plugin guide](plugins/agent-relay/README.md) for host command names, examples, setup, collection, and backend details.

## State and commands

The state root resolves from global `--state-dir PATH`, then `RELAY_JOB_STATE_DIR`, `$XDG_STATE_HOME/agent-relay`, then `$HOME/.local/state/agent-relay`. The selected path must be absolute. Use a local directory outside Dropbox and shared network storage.

`status` reports jobs, with `status --json <job-id>` exposing attempts and notification state. `wait` collects a terminal outcome; `result` prints the response or labeled partial stdout; `cancel` requests and confirms cancellation. `prune --older-than DAYS` explicitly removes artifacts for old terminal jobs while retaining job metadata.

`wait` exit codes are 0 done, 1 unknown job or usage error, 2 failed, 3 cancelled, 4 crashed, 5 timeout, 6 ownership unresolved, and 7 storage unavailable or contended. A timeout leaves the job running.

SQLite stores lifecycle metadata in WAL mode. The default state root and job artifacts use user-only permissions. Select a different absolute root with the global `--state-dir PATH` option when needed.

Codex notifications are attempted through `codex queue` only when an originating thread is recorded; status separately records `sent`, `failed`, `uncertain`, or `pending`. A `sent` status reports CLI success and does not by itself verify delivery; receipt and automatic re-invocation were confirmed once for an idle thread. Claude waiters record registration and completion. Per job, the relay cannot observe whether Claude re-invoked the originating conversation; re-invocation was verified once in a live check. Use the printed manual `wait`, `result`, and `cancel` commands as the fallback.

## Validation

Run all deterministic suites and the package checks from the repository root:

    bash tests/smoke.sh
    python3 -m unittest discover -s tests -p 'test_lifecycle.py'
    python3 -m unittest discover -s tests -p 'test_codex.py'
    python3 -m unittest discover -s tests -p 'test_hosts.py'
    python3 -m unittest discover -s tests -p 'test_notify.py'
    python3 tests/validate-package.py

CI passed on Ubuntu 22.04/Python 3.8, Ubuntu/Python 3.12, and macOS/Python 3.12. Bash syntax passed in the matrix, and ShellCheck passed on Linux.

Live checks ran on Linux against Codex CLI 0.163.0 and OpenCode's subscription route. Both backends passed authorized edits, WAL/status concurrency, and collection after launcher exit; Codex event shape and delivery to an idle thread passed with user-confirmed receipt and automatic re-invocation. A Claude background-waiter re-invocation was verified once. Claude subscription-route live checks passed on 2026-10-20; the verified cases are listed in the [plugin guide](plugins/agent-relay/README.md#verification). Live runs are billable. Inspect the commands with `bash tests/live/live-check.sh --dry-run --backend codex` or `bash tests/live/live-check.sh --dry-run --backend opencode`. To repeat them manually, set `RELAY_LIVE_CHECKS=1` and select a backend. Add `--notify-thread UUID` only when testing delivery to an idle Codex thread you control.

## Known limitations

- Direct wrapper use runs one attempt; retry handling belongs to the relay supervisor.
- The Claude API-key route is not yet verified by live runs. Live runs are billable.
- From a sandboxed Codex session, launches need approval to run outside the sandbox to reach the shared state root and the network.
- Claude read-only mode is application-level enforcement inside Claude Code, not an OS sandbox. Live checks covered only the write attempts listed in the plugin guide; other deny rules are not separately verified.
  - Helpers defined in Git configuration files (for example `diff.external` or a textconv driver) can still run programs in read-only jobs, although inherited helper variables such as `GIT_EXTERNAL_DIFF` are removed.
- Claude billing-route checks read the reported credential source and, when reported, the provider, which must be first-party.
  - Write jobs load the full user configuration, so a settings-supplied endpoint override (for example `ANTHROPIC_BASE_URL` in a settings `env` block) or managed settings is not detected. Read-only jobs ignore user settings files.
- Cancellation covers the owned process group; OpenCode tool commands started in their own sessions can survive. Codex tool-child cleanup relies on Codex itself, and a hard kill of Codex could leave them.
- OpenCode model aliases now live at `RELAY_MODEL_CONFIG` or `$XDG_CONFIG_HOME/agent-relay/config.json` (default `$HOME/.config/agent-relay/config.json`); `OC_HELPER_CONFIG` remains a compatibility override.

## License

GPL-3.0-or-later; see [LICENSE](plugins/agent-relay/LICENSE). The public package is maintained independently from the source package it was adapted from.
