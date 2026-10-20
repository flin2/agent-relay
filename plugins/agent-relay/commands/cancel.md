---
description: Cancel a running agent-relay job.
argument-hint: '[job-id]'
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`RELAY_JOB_HOST=claude python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py" cancel $ARGUMENTS`

Present the command's actual result. If no job ID was supplied, ask for one or suggest `/agent-relay:status`.

