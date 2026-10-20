---
name: relay-status
description: Show jobs for the current Codex conversation or inspect a job, including notification state.
---

Run from the target repository using the installed package path:

```bash
RELAY_JOB_HOST=codex python3 "<plugin-root>/scripts/relay-job.py" status [job-id] [--all] [--dir PATH] [--json]
```

The default list is filtered to the current host conversation across the shared state root. `--all` removes that filter; `--dir` filters by canonical workspace. Present the actual output. The notification field is separate from execution state; `sent` does not certify the result.

