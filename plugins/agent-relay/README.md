# agent-relay

A self-contained plugin for delegating bounded tasks from Claude Code or Codex through either the OpenCode CLI or the Codex CLI. Both backends use the same detached SQLite job lifecycle.

## Supported execution

- OpenCode jobs select the subscription or direct API route with `--route sub` or `--route api`; `--zen` and `--go` apply only to the subscription route.
- Codex jobs use `codex exec --json`, pass task text through stdin, require current-turn terminal evidence, and continue the exact thread recorded by the parent job.
- New jobs require an explicit `--backend opencode|codex`; OpenCode also requires an explicit route.
- Jobs default to backend-specific read-only modes. Codex read-only is enforced by its sandbox; OpenCode selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. Use `--write` only for authorized edits; a continuation returns to the backend's read-only mode unless `--write` is repeated.
- OpenCode relay jobs apply the adapter's idle timeout and separate-session retry policy. Each wrapper runs one invocation when called directly, outside the relay.
- Claude commands and launcher agents, and Codex skills, can select either backend.

## Setup

Install the plugin in Claude Code or Codex. For OpenCode jobs, install and configure the OpenCode CLI, then run the OpenCode setup check. For Codex jobs, run `bash "<plugin-root>/scripts/codex-setup.sh"`; it checks Codex availability and required help options without requiring OpenCode or running a task.

## Usage

The job manager requires a backend and, for OpenCode, a route:

    python3 "<plugin-root>/scripts/relay-job.py" run --backend opencode --route sub --dir /path/to/repo --label "Review changes" --stdin

Provide the task on stdin. Use `--route api` for a direct provider API. Use `--zen` or `--go` only with `--route sub`. OpenCode accepts model aliases and full provider/model IDs; set `RELAY_MODEL_CONFIG` or create `$XDG_CONFIG_HOME/agent-relay/config.json` to define aliases.

Codex jobs use `codex exec --json` and are read-only by default. The relay passes `--sandbox read-only` for new jobs and sets `sandbox_mode="read-only"` for resumed jobs, so Codex's sandbox enforces the mode:

    python3 "<plugin-root>/scripts/relay-job.py" run --backend codex --dir /path/to/repo --stdin

Add `--write` only for an authorized edit. `--model` and `--effort` map to Codex model and reasoning-effort controls. Codex 0.163 JSONL contains no command-execution items, so the event stream alone does not expose sandbox enforcement; authorized rollout logs confirmed writes failed in fresh and resumed read-only jobs.

A detached launch prints the job ID, backend, resolved state root, and exact `wait`, `result`, and `cancel` commands to stderr. A launch returns after starting the detached supervisor; use `wait` and then `result` to collect the response after the launcher exits. `--wait` performs collection inline and prints the recovery commands first.

Continue a recorded session by relay job ID and provide new task text:

    python3 "<plugin-root>/scripts/relay-job.py" --state-dir "<state-root>" resume <job-id> --stdin

Resume inherits the parent's backend, route, workspace, model, and effort. Set a new model or effort explicitly when needed. Permission resets to the backend's read-only mode: Codex uses `sandbox_mode="read-only"`, while OpenCode selects the `plan` agent. Restate `--write` only when the follow-up is authorized to edit. A session can have only one active managed writer in the shared state root.

## Host commands

Claude commands are `/agent-relay:setup`, `/agent-relay:run`, `/agent-relay:review`, `/agent-relay:adversarial-review`, `/agent-relay:status`, `/agent-relay:result`, `/agent-relay:resume`, and `/agent-relay:cancel`. Claude launcher agents are `relay-opencode-sub`, `relay-opencode-api`, and `relay-codex`.

Codex skills are `$agent-relay:relay-setup`, `$agent-relay:relay-run`, `$agent-relay:relay-review`, `$agent-relay:relay-adversarial-review`, `$agent-relay:relay-status`, `$agent-relay:relay-result`, `$agent-relay:relay-resume`, and `$agent-relay:relay-cancel`. Setup and new work require an explicit backend choice; route options apply only to OpenCode.

## State and collection

The state root resolves in this order: global `--state-dir PATH`, `RELAY_JOB_STATE_DIR`, `$XDG_STATE_HOME/agent-relay`, then `$HOME/.local/state/agent-relay`. The selected path must be absolute. Use a local state directory outside Dropbox or shared network directories; host plugin-data variables do not select the relay root.

SQLite stores lifecycle metadata at `jobs.sqlite3` in WAL mode. Job artifacts live under `jobs/<uuid>/`. Task, output, event, response, and diagnostic artifacts are kept separately, and Codex's raw JSONL is retained in stdout.

`status --json <job-id>` reports the recorded job, attempts, and notification state. `result <job-id>` prints the final response when available, else labeled partial stdout. Use `result --stderr` for diagnostics and `result --events` for lifecycle events. `status --all` removes the current conversation filter; `status --dir PATH` filters by canonical workspace.

`prune --older-than DAYS` explicitly removes artifacts for old terminal jobs that have no active session claim or pending notification. Job metadata and continuation links remain.

`wait` exit codes are 0 done, 1 unknown job or usage error, 2 failed, 3 cancelled, 4 crashed, 5 timeout, 6 ownership unresolved, and 7 storage unavailable or contended. A wait timeout leaves the job running.

## Notifications

Codex-host jobs attempt one `codex queue` notification when an originating thread is recorded. Status records `sent`, `failed`, `uncertain`, or `pending` separately from execution outcome. `sent` means the CLI returned success; it does not by itself verify delivery. Receipt and automatic re-invocation were confirmed once for an idle thread.

Claude background waiters record local registration and completion, while waiter delivery status remains uncertain. Per job, the relay cannot observe whether Claude re-invoked the originating conversation; re-invocation was verified once in a live check. Use the printed manual `wait`, `result`, and `cancel` commands when a notification is missing or unavailable.

## Verification

CI passed on Ubuntu 22.04/Python 3.8, Ubuntu/Python 3.12, and macOS/Python 3.12. Bash syntax passed in the matrix, and ShellCheck passed on Linux.

Live checks ran on Linux against Codex CLI 0.163.0 and OpenCode's subscription route. Both backends passed authorized edits, WAL/status concurrency, and collection after launcher exit; Codex event shape and delivery to an idle thread passed with user-confirmed receipt and automatic re-invocation. Claude background-waiter re-invocation was verified once. To inspect or repeat bounded live checks after confirming the isolated workspace and prompts:

    RELAY_LIVE_CHECKS=1 bash tests/live/live-check.sh --backend codex
    RELAY_LIVE_CHECKS=1 bash tests/live/live-check.sh --backend opencode

To repeat the optional Codex queue check, add `--notify-thread UUID` for an idle thread you control. A CLI-reported `sent` result still requires confirming the message arrived in that thread.

## KNOWN-LIMITATIONS

- Authorized rollout logs confirmed that Codex read-only write attempts failed in fresh and resumed jobs, but Codex 0.163 JSONL contains no command-execution items and does not expose those results. OpenCode's live read-only check created a file with a shell command; its `plan` agent is advisory, so shell-write enforcement depends on OpenCode permissions denying shell for that agent.
- Cancelling an OpenCode job stops OpenCode and its managed process group, but tool commands it starts in their own session can survive. Codex tool commands run outside the relay's group and were observed to stop when Codex was cancelled; child cleanup relies on Codex itself, and a hard kill of Codex could leave them.
- Processes that deliberately leave the managed process group are outside the cancellation guarantee; unsupported platforms fail clearly rather than signaling sampled PIDs.
- Shell wrappers run one attempt when used directly. Retry handling belongs to the relay supervisor.
- OpenCode model aliases now use `RELAY_MODEL_CONFIG` or `$XDG_CONFIG_HOME/agent-relay/config.json` (default `$HOME/.config/agent-relay/config.json`). `OC_HELPER_CONFIG` remains a compatibility override.
- OpenCode's text wrapper does not produce a structured event stream, so its events artifact is empty.

## License

GPL-3.0-or-later; see `LICENSE`.
