---
description: Check readiness for one selected agent-relay backend.
argument-hint: '[opencode|codex]'
allowed-tools: Bash
---

Use the backend explicitly requested by the user or supplied as the argument. If neither identifies a backend, ask whether to check `opencode` or `codex`; never run both checks. Run only the selected bundled readiness script: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/oc-setup.sh" check` for OpenCode, or `bash "${CLAUDE_PLUGIN_ROOT}/scripts/codex-setup.sh" check` for Codex. Report the script output and what it checked without claiming broader readiness. The OpenCode-only `link` and `unlink` options are available only when the user explicitly asks to manage those shortcuts.
