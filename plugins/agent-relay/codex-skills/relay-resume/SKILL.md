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

The manager inherits the backend, OpenCode route when applicable, workspace, model, and effort from the parent unless model or effort is overridden. Permission resets to the backend's read-only mode: Codex uses `sandbox_mode="read-only"`, while OpenCode selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when authorized. Preserve the new task text and any stated verification requirement verbatim. Return the handoff's job ID, backend, state root, and exact wait, result, and cancel commands. Codex queue notification is best effort; the printed manual commands remain available.

