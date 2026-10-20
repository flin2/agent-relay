---
name: relay-runtime
description: Internal thin-launcher and collection contract for Claude agent-relay launchers.
user-invocable: false
---

# Agent Relay launcher contract

The launcher starts one durable job with the bundled `scripts/relay-job.py`. It does not perform the task, wait for the backend, inspect the target repository, or add its own analysis.

## Launch

- Make exactly one Bash call: `${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py run --backend <opencode|codex|claude> [route options] [options] --stdin`.
- Require the backend from the user's explicit choice. OpenCode also requires `--route sub|api`; `--zen` or `--go` is valid only for `sub`. Codex rejects route and gateway flags.
  - Claude takes an optional `--route sub|api`. When it is omitted, the launcher picks `sub` if a Claude subscription login is detected, otherwise `api` if `ANTHROPIC_API_KEY` is set, and otherwise fails before launch.
  - Claude rejects `--zen` and `--go`.
- Pass the user's task text through a single-quoted heredoc so backticks, dollar signs, quotes, and punctuation remain literal. Choose a unique delimiter if needed.
- Set `RELAY_JOB_HOST=claude`; the manager records host conversation identity separately from the selected backend session.
- Preserve the user's task, directory, permission choice, model, effort, and any stated verification requirement verbatim. Use an absolute `--dir` for a named repository.
- Read-only is the default. Add `--write` only when edits are authorized. Forward model and effort only when supplied; Claude `--effort` accepts only `low`, `medium`, `high`, `xhigh`, or `max`.
- Claude read-only runs with `--safe-mode --restricted --strict-mcp-config`, tools limited to Read, Grep, and Glob, and Bash limited to a prefix-matched allow list of read-only Git commands with deny rules for file-writing and external-program options; anything that would prompt is denied.
  - User plugins, hooks, skills, MCP servers, user settings, and repository `CLAUDE.md` are not loaded; only built-in plugins load. This is application-level enforcement inside Claude Code, not an OS sandbox; live checks covered only the write attempts listed in the plugin guide.
  - Claude `--write` selects `acceptEdits`, loads the user's full Claude configuration, and denies shell commands not already allowed by the user's settings.
- Return the launch output, including job ID, selected backend, resolved state root, and exact `wait`, `result`, and `cancel` commands. Do not wait, monitor, or run cleanup from the launcher.

## Collection and continuation

- The printed Claude `wait --claude-waiter` command records its exact command and local waiter state. Start it through Bash with `run_in_background: true` and no timeout from the originating conversation; its completion is observed locally. Per job, the relay cannot observe whether Claude re-invoked the originating conversation; re-invocation was verified once in a live check.
- After the waiter exits, run the exact printed `result` command and present its output. If the waiter was not running, including after a session restart, use the printed manual collection commands, which include the absolute script path and explicit state root.
- Continue only through `resume JOB_ID` with new task text. The manager selects the exact recorded backend session, inherits the backend and applicable route/workspace/model/effort, and resets permissions to read-only unless `--write` is restated. A Claude job whose stream reported a different session identity cannot be resumed.
- A Claude job whose reported credential source differs from its selected route ends with `route_mismatch`; report that as the job's failure rather than retrying on another route.
