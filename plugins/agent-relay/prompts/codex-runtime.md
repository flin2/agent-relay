# Agent Relay Codex runtime

Use this reference when a Codex skill launches or manages an agent-relay job.

## Paths and state

- Resolve the installed plugin root from a skill under `codex-skills/<name>/` by going two directories up.
- Use absolute paths to bundled scripts. Do not assume `CLAUDE_PLUGIN_ROOT` or a sibling checkout exists in Codex.
- Prefix manager calls with `RELAY_JOB_HOST=codex`. The manager records the host conversation separately from the selected backend session.
- State root precedence is the global `--state-dir PATH` option, then `RELAY_JOB_STATE_DIR`, `XDG_STATE_HOME/agent-relay`, and `HOME/.local/state/agent-relay`. The option overrides the environment variable; the root must be absolute and is shared by both hosts.
- Use the same state root for launch, status, wait, result, resume, and cancel. Launch handoffs print the resolved root and exact collection commands.

## Forwarding

- Require the user's explicit backend choice and pass `--backend opencode|codex` on every new `run` command. OpenCode also requires `--route sub|api`; use `--zen` or `--go` only with `sub`. Codex rejects OpenCode route and gateway flags.
- Preserve the user's task text, directory, permission choice, model, effort, and any stated verification requirement verbatim. Do not inspect the repository or add analysis in the launcher skill.
- Read-only is the default. Add `--write` only when the user authorizes edits. Pass task text through a single-quoted heredoc or task file, never interpolated shell text.
- Review operations are host-level read-only templates over `run --wait`; include the selected backend explicitly and return the captured result.

## Collection and continuation

- Codex notification is attempted with `codex queue --thread <originating-thread-id> --message <payload>` only when the supervisor has an originating thread ID and `codex` on `PATH`. The notification state is separate from execution state.
- The payload contains the job ID, terminal state, resolved state root, and exact result command. It contains no task text or credentials. Delivery can be pending, sent, failed, or uncertain; do not treat `sent` as evidence that the task result is correct.
- The manual route is always available: use the exact printed `wait`, then `result` commands. If queue notification is absent or fails, run those commands as printed; each starts with `python3 <absolute script> --state-dir <state root>`, so collection does not depend on `RELAY_JOB_STATE_DIR` in the environment.
- Wait exit codes are 0 done, 1 unknown job or usage error, 2 failed, 3 cancelled, 4 crashed, 5 timeout, 6 ownership unresolved, and 7 storage unavailable or contended.
- Continue only with `resume JOB_ID` and new task text. The manager uses the parent's exact recorded backend session and inherits backend, applicable route, workspace, model, and effort. Permission resets to read-only unless `--write` is restated.
