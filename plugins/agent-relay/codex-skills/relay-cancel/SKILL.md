---
name: relay-cancel
description: Cancel a specific running agent-relay job when requested.
---

Identify the requested job by its explicit ID or the conversation's recorded handoff. Run:

```bash
RELAY_JOB_HOST=codex python3 "<plugin-root>/scripts/relay-job.py" cancel "<job-id>"
```

Present the actual result. If cancelling from a handoff, use its exact `cancel` command and state root. Leave any backend edits in place.

