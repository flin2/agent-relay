---
description: Challenge an approach and its assumptions through the selected agent-relay backend.
argument-hint: '[--backend opencode|codex] [--route sub|api] [--model <id>] [--effort <value>] [--base <ref>] [focus ...]'
allowed-tools: Bash(python3:*), Bash(git:*), Read
---

Require an explicit backend from the user's request or `--backend opencode|codex`; ask if missing. This host-level adversarial-review template gathers the requested local change scope and runs `RELAY_JOB_HOST=claude python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py" run --backend <backend> --wait --dir <repo> --stdin` as a read-only job. For OpenCode, also require `--route sub|api`; forward `--zen` or `--go` only with `sub`. Never pass `--write` or act on reported issues.

1. Inspect the target repository status and gather the scope: with `--base <ref>`, use `git diff <ref>...HEAD --stat` and `git diff <ref>...HEAD`; otherwise use `git status --short --untracked-files=all`, `git diff`, `git diff --cached`, and read new files. Stop if there are no changes.
2. Read `${CLAUDE_PLUGIN_ROOT}/prompts/adversarial-review.md`; fill `{{TARGET_LABEL}}`, `{{SCOPE_INSTRUCTION}}`, and `{{USER_FOCUS}}`. With no focus, use `(none — challenge the change broadly)`. Preserve the user's focus and any stated verification requirement verbatim.
3. Launch through `${CLAUDE_PLUGIN_ROOT}/scripts/relay-job.py` with explicit `--backend`, `--dir`, and `--stdin`; include `--route` only for OpenCode. Pass supplied model and effort unchanged and use a single-quoted heredoc for the review task.
4. Return the captured output verbatim. If readiness fails, point to `/agent-relay:setup <backend>`.

