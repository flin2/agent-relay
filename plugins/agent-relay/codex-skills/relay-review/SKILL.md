---
name: relay-review
description: Run an evidence-based, read-only review of local changes through the selected backend.
---

# Review local changes

Read the Codex runtime guidance in `../../prompts/codex-runtime.md`. Require the user's explicit backend choice; ask if missing. Never pass `--write` or apply findings.

1. Select the scope: for `--base <ref>`, inspect `git diff <ref>...HEAD --stat` and `git diff <ref>...HEAD`; otherwise inspect status, working and staged diffs, and new files. Stop if the scope is empty.
2. Read `../../prompts/review.md` and fill `{{TARGET_LABEL}}`, `{{SCOPE_INSTRUCTION}}`, and `{{USER_FOCUS}}`, preserving the user's focus and any stated verification requirement verbatim.
3. Launch through `<plugin-root>/scripts/relay-job.py run --backend <codex|opencode|claude> --wait --dir <workspace> --stdin`, with `RELAY_JOB_HOST=codex` and a single-quoted heredoc. Require `--route sub|api` for OpenCode; for Claude the route is optional and `--zen` or `--go` are rejected. Forward model and effort unchanged when supplied.
4. Return the captured output verbatim. Do not act on findings.

