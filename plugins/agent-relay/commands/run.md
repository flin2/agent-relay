---
description: Launch the user's task through an explicitly selected agent-relay backend.
argument-hint: '[--backend opencode|codex] [--route sub|api] [--model <id>] [--effort <value>] [--write] [--dir <repo>] [task]'
allowed-tools: Agent, Bash(python3:*)
---

Require an explicit backend from the user's request or `--backend opencode|codex`; ask if it is missing. Use the matching thin launcher agent: `relay-opencode-sub` or `relay-opencode-api` for OpenCode, and `relay-codex` for Codex. OpenCode requires `--route sub|api`; `--zen` and `--go` apply only to the `sub` route. Do not pass OpenCode route or gateway options to Codex.

Forward the user's task text, directory, permission choice, model, effort, and any stated verification requirement verbatim. Add no analysis and do not inspect the target repository. The launcher makes one detached `run --backend ...` call with task text on stdin, using a single-quoted heredoc. Codex read-only is enforced by its sandbox (`--sandbox read-only`); OpenCode read-only selects the `plan` agent, which avoids edit tools but permits shell writes unless OpenCode permissions deny them. Configure OpenCode to deny shell for the plan agent when enforcement is required. Add `--write` only when the user authorizes edits.

For a detached launch, read the handoff and start its exact `wait` command through Bash with `run_in_background: true` and no timeout from this originating conversation. Keep the returned Bash session associated with this conversation; when it exits, read the exact `result` command and present the captured result. If the waiter is absent after a session restart, use the printed manual `wait`, `result`, and `cancel` commands with the printed state root. For an explicit inline request, pass `--wait`, keep the stderr handoff for recovery if collection is interrupted, and return the captured stdout response.

