---
name: relay-prompting
description: Internal guidance for shaping tasks before forwarding them through agent-relay backends (OpenCode routes, Codex, and Claude).
user-invocable: false
---

# Forwarding agent-relay tasks

The receiving backend has no access to the host conversation. Forward the user's task text and stated constraints verbatim; preserve the directory, permission choice, model, effort, and verification request. Do not inspect the repository or add analysis when acting as a launcher.

- Keep reviews and diagnoses read-only; request `--write` only for an authorized implementation or fix.
- Do not rewrite the task into a new objective. Pass repository instructions and verification requirements exactly as the user stated them.
- A read-only Claude job does not load repository `CLAUDE.md` or user settings, so include in the task text any instruction the backend must follow.
- Use an absolute working directory when the user names a repository. The backend can read files inside that workspace.
- Continue an existing job through `resume JOB_ID` with the new task text verbatim. Do not select a session by recency.

