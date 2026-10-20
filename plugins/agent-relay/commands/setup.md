---
description: Check readiness for one selected agent-relay backend.
argument-hint: '[opencode|codex|claude]'
allowed-tools: Bash
---

Use the backend explicitly requested by the user or supplied as the argument. If neither identifies a backend, ask whether to check `opencode`, `codex`, or `claude`; never run more than one check. Run only the selected bundled readiness script: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/oc-setup.sh" check` for OpenCode, `bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-setup.sh" check` for Codex, or `bash "${CLAUDE_PLUGIN_ROOT}/scripts/claude-setup.sh" check` for Claude. Report the script output and what it checked without claiming broader readiness. The Claude check runs version and help output only; it does not log in, read credentials, or start a task, and login status is read at launch when no `--route` is given or when `--route sub` is requested. The OpenCode-only `link` and `unlink` options are available only when the user explicitly asks to manage those shortcuts.
