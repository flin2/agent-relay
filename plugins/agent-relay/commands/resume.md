---
description: Continue a completed agent-relay job in its exact recorded backend session.
argument-hint: '<job-id> [--model <alias|id>] [--effort <value>] [--write]'
allowed-tools: Bash(python3:*)
---

Ask for a parent job ID and the new task text if either is missing. Then create a linked job with:

```bash
RELAY_JOB_HOST=claude python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py" resume "<job-id>" [--model <value>] [--effort <value>] [--write] --stdin <<'RELAY_TASK_EOF'
<new task text>
RELAY_TASK_EOF
```

The new job uses the parent's recorded backend session, backend, route when applicable (the OpenCode route, or the Claude route recorded on the parent), workspace, and model/effort unless explicitly overridden. Claude resumes the exact recorded session with `--resume` from the recorded workspace and keeps the recorded route; a parent whose stream reported a different session identity cannot be resumed. Permission resets to the backend's read-only mode: Codex uses `sandbox_mode="read-only"`, OpenCode selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them, and Claude runs with `--safe-mode --restricted --strict-mcp-config` and its read-only tool allow list. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when the user authorizes edits; for Claude, restate it on the resume call. Preserve the new task text and any stated verification requirement verbatim. For a detached launch, start the printed `wait` command through Bash with `run_in_background: true` and no timeout from this originating conversation; when it exits, run the printed `result` command. If the waiter is absent after a session restart, use the printed manual `wait`, `result`, and `cancel` commands. For an explicit inline continuation, pass `--wait`, keep the stderr handoff for recovery if collection is interrupted, and return the captured stdout response.

