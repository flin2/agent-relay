# Security

## Execution permissions

Jobs default to backend-specific read-only modes. Codex uses its sandbox (`--sandbox read-only` for new jobs and `sandbox_mode="read-only"` for resumed jobs), which enforces read-only access. OpenCode selects the `plan` agent, which avoids edit tools but still permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. `--write` enables edits for that job only, and a continuation returns to the backend's read-only mode unless that follow-up explicitly includes `--write`.

The relay does not turn a read-only job into a write job based on a parent session's settings. Review the task scope and workspace before enabling writes.

## Cancellation

Cancelling an OpenCode job stops OpenCode and its relay-managed process group. Tool commands OpenCode starts in their own session, such as long builds, can survive and must be stopped manually. Codex tool commands run outside the relay's managed group; they were observed to stop when Codex was cancelled, so child cleanup relies on Codex itself and a hard kill of Codex could leave them.

## Local state

The state root defaults to `$HOME/.local/state/agent-relay`, or `$XDG_STATE_HOME/agent-relay` when set. The global `--state-dir PATH` option overrides `RELAY_JOB_STATE_DIR` and requires an absolute path. Keep state on local storage outside shared or synchronized folders.

The relay creates the state root and jobs directory with mode 0700, the SQLite database with mode 0600, and job artifacts with user-only permissions. State contains task text, backend output, session identifiers, host routing metadata, and result artifacts. Protect and prune it as local user data.

## Notifications and host data

The Codex queue notification payload contains the relay job ID, terminal state, resolved state root, and exact result command. It does not include task text or credentials. Delivery status is recorded as sent, failed, uncertain, or pending; sent indicates the Codex CLI returned success. Receipt and automatic re-invocation were confirmed once for an idle thread.

Claude background waiters record local registration and completion. Per job, the relay cannot observe whether Claude re-invoked the originating conversation; re-invocation was verified once in a live check. Use the printed manual collection commands if a waiter is missing.

Before starting a backend CLI, the relay removes inherited `RELAY_JOB_*` variables and the host routing variables `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PLUGIN_DATA`, `PLUGIN_DATA`, `CODEX_THREAD_ID`, and `CODEX_SESSION_ID`. It preserves backend configuration and authentication environment variables required by the installed CLI.

## Reporting

Report vulnerabilities privately to the maintainer. Omit credentials and private task content from public issues.
