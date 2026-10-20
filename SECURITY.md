# Security

## Execution permissions

Jobs default to backend-specific read-only modes. Codex uses its sandbox (`--sandbox read-only` for new jobs and `sandbox_mode="read-only"` for resumed jobs), which enforces read-only access. OpenCode selects the `plan` agent, which avoids edit tools but still permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. `--write` enables edits for that job only, and a continuation returns to the backend's read-only mode unless that follow-up explicitly includes `--write`.

Claude read-only jobs use `--safe-mode --restricted --strict-mcp-config` and limit tools to Read, Grep, and Glob. Anything that would prompt is denied.

- Bash is limited to a prefix-matched allow list of read-only Git commands: `status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`, `merge-base`, `blame`, and `branch --show-current`.
- Deny rules block options that write files or run external programs (`--output`, `--ext-diff`, `--textconv`, `--open-files-in-pager`), redirection, pipes, and command chaining or substitution.

This is application-level enforcement inside Claude Code, not an OS sandbox. Subscription-route live checks on 2026-10-20 confirmed denials for a shell write (`touch`), `git diff --output`, and output redirection on fresh and resumed read-only jobs; other deny rules are not separately verified. Read-only jobs remove inherited Git helper variables such as `GIT_EXTERNAL_DIFF`, but helpers defined in Git configuration files (for example `diff.external` or a textconv driver) can still run programs when an allowed Git command uses them.

`--write` selects `acceptEdits` and loads the user's full Claude configuration, including hooks, plugins, and MCP servers, so a write job runs whatever those configure.

The relay does not turn a read-only job into a write job based on a parent session's settings. Review the task scope and workspace before enabling writes.

## Claude routes and credentials

- Route `sub` uses the Claude subscription login and removes `ANTHROPIC_API_KEY` from the backend environment; route `api` uses `ANTHROPIC_API_KEY` and removes `CLAUDE_CODE_OAUTH_TOKEN`.
- Both routes remove gateway and third-party provider overrides from the launched process environment.
- A job fails with `route_mismatch` when Claude reports a credential source that differs from its selected route, for example an `apiKeyHelper` in user settings on the `sub` route.
- The job checks the reported credential source and, when reported, the provider, which must be first-party. The 2026-10-20 subscription-route check reported a subscription credential and a first-party provider.
- Write jobs load the full user configuration, so a settings-supplied endpoint override (for example `ANTHROPIC_BASE_URL` in a settings `env` block) or managed settings is not detected. Read-only jobs ignore user settings files.
- The API-key route has not been verified by live runs.

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
