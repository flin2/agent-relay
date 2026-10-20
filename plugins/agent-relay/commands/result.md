---
description: Print a captured agent-relay job result.
argument-hint: '[job-id] [--stderr|--events]'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`RELAY_JOB_HOST=claude python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py" result $ARGUMENTS`

Present the selected artifact. If no job ID was supplied, ask for one or suggest `/agent-relay:status`. The default selects the final response when available, else partial stdout with a label on stderr. `--stderr` selects diagnostics and `--events` selects the event artifact. OpenCode text output does not produce a separate structured event stream.

