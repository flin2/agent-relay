---
description: Show agent-relay jobs for this conversation or inspect one job, including notification state.
argument-hint: '[job-id] [--all] [--dir PATH] [--json]'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`RELAY_JOB_HOST=claude python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py" status $ARGUMENTS`

Present the actual output. By default it lists this conversation's jobs across the shared state root; `--all` removes the conversation filter and `--dir` filters by canonical workspace. The notification column or field reports delivery state separately from execution state; a sent notification does not certify the result. For an unknown ID, say so and suggest listing jobs.

