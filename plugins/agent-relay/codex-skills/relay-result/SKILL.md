---
name: relay-result
description: Retrieve the captured output from an agent-relay job.
---

Use the explicit job ID supplied by the user or recorded in the conversation:

```bash
RELAY_JOB_HOST=codex python3 "<plugin-root>/scripts/relay-job.py" result "<job-id>" [--stderr|--events]
```

Return the selected artifact verbatim. The default selects the final response when available, else partial stdout with a label on stderr. `--stderr` selects diagnostics and `--events` selects the event artifact. OpenCode text output does not produce a separate structured event stream. Use the state root from the handoff for collection. Do not relaunch the task or claim partial output is a completed result.

