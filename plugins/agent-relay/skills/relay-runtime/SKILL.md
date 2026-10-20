---
name: relay-runtime
description: Internal thin-launcher and collection contract for Claude agent-relay launchers.
user-invocable: false
---

# Agent Relay launcher contract

The launcher starts one durable job with the bundled `scripts/relay-job.py`. It does not perform the task, wait for the backend, inspect the target repository, or add its own analysis.

## Launch

- Make exactly one Bash call: `${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py run --backend <opencode|codex> [OpenCode route options] [options] --stdin`.
- Require the backend from the user's explicit choice. OpenCode also requires `--route sub|api`; `--zen` or `--go` is valid only for `sub`. Codex rejects OpenCode route and gateway flags.
- Pass the user's task text through a single-quoted heredoc so backticks, dollar signs, quotes, and punctuation remain literal. Choose a unique delimiter if needed.
- Set `RELAY_JOB_HOST=claude`; the manager records host conversation identity separately from the selected backend session.
- Preserve the user's task, directory, permission choice, model, effort, and any stated verification requirement verbatim. Use an absolute `--dir` for a named repository.
- Read-only is the default. Add `--write` only when edits are authorized. Forward model and effort only when supplied.
- Return the launch output, including job ID, selected backend, resolved state root, and exact `wait`, `result`, and `cancel` commands. Do not wait, monitor, or run cleanup from the launcher.

## Collection and continuation

- The printed Claude `wait --claude-waiter` command records its exact command and local waiter state. Start it through Bash with `run_in_background: true` and no timeout from the originating conversation; its completion is observed locally. Per job, the relay cannot observe whether Claude re-invoked the originating conversation; re-invocation was verified once in a live check.
- After the waiter exits, run the exact printed `result` command and present its output. If the waiter was not running, including after a session restart, use the printed manual collection commands, which include the absolute script path and explicit state root.
- Continue only through `resume JOB_ID` with new task text. The manager selects the exact recorded backend session, inherits the backend and applicable route/workspace/model/effort, and resets permissions to read-only unless `--write` is restated.
