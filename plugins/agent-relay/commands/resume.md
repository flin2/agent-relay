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

The new job uses the parent's recorded backend session, backend, OpenCode route when applicable, workspace, and model/effort unless explicitly overridden. Permission resets to the backend's read-only mode: Codex uses `sandbox_mode="read-only"`, while OpenCode selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when the user authorizes edits. Preserve the new task text and any stated verification requirement verbatim. For a detached launch, start the printed `wait` command through Bash with `run_in_background: true` and no timeout from this originating conversation; when it exits, run the printed `result` command. If the waiter is absent after a session restart, use the printed manual `wait`, `result`, and `cancel` commands. For an explicit inline continuation, pass `--wait`, keep the stderr handoff for recovery if collection is interrupted, and return the captured stdout response.

