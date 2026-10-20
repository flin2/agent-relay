---
name: relay-resume
description: Continue an agent-relay job in its recorded backend session.
---

Ask for a parent job ID and new task text if either is missing. Then run:

```bash
RELAY_JOB_HOST=codex python3 "<plugin-root>/scripts/relay-job.py" resume "<job-id>" [--model <value>] [--effort <value>] [--write] --stdin <<'RELAY_TASK_EOF'
<new task text>
RELAY_TASK_EOF
```

The manager inherits the backend, route when applicable (OpenCode or Claude), workspace, model, and effort from the parent unless model or effort is overridden. Claude resumes the exact recorded session with `--resume` from the recorded workspace; a parent whose stream reported a different session identity cannot be resumed. Permission resets to the backend's read-only mode: Codex uses `sandbox_mode="read-only"`, OpenCode selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them, and Claude runs with `--safe-mode --restricted --strict-mcp-config` and its read-only tool allow list. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when authorized; for Claude, restate it on the resume call. Preserve the new task text and any stated verification requirement verbatim. Return the handoff's job ID, backend, state root, and exact wait, result, and cancel commands. Codex queue notification is best effort; the printed manual commands remain available.

