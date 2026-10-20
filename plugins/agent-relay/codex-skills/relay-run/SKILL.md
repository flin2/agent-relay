---
name: relay-run
description: Delegate a diagnosis, review, or bounded task through an explicitly selected agent-relay backend.
---

# Delegate through an explicit backend

Use the installed package path and the shared job interface. Require the user's explicit choice of `--backend codex` or `--backend opencode`; ask if it is missing.

- With OpenCode, require `--route sub|api`; add `--go` or `--zen` only for `sub` when requested. Do not pass OpenCode options with Codex.
- Preserve the user's task text, directory, permission choice, model, effort, and any stated verification requirement verbatim. The launcher does not inspect the repository or add analysis.
- Codex read-only is enforced by its sandbox (`--sandbox read-only`). OpenCode read-only selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when the user authorizes edits. Pass task text on stdin through a single-quoted heredoc; do not interpolate it into shell text.
- Run `RELAY_JOB_HOST=codex python3 "<plugin-root>/scripts/relay-job.py" run --backend <codex|opencode> [OpenCode route options] --dir "<workspace>" --label "<short title>" --stdin`.
- Return the job ID, backend, state root, and exact wait, result, and cancel commands from the launch handoff. For a requested inline result, use `--wait`; keep its stderr handoff for recovery if collection is interrupted, while stdout remains the response.
- Codex queue notification is best effort. Use the printed manual `wait` and `result` commands if it is absent or fails. Do not relaunch a yielded job or continue through a latest-session lookup.

