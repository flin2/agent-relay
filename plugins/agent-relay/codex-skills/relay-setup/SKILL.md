---
name: relay-setup
description: Check readiness for one selected agent-relay backend in Codex.
---

# Check backend readiness

Use the backend explicitly requested by the user or supplied as `opencode`, `codex`, or `claude`. If none identifies a backend, ask which one to check; never run more than one check. Resolve the installed plugin root two directories above this skill folder and run only its corresponding readiness script:

```bash
bash "<plugin-root>/scripts/oc-setup.sh" check
```

For Codex, run `bash "<plugin-root>/scripts/codex-setup.sh" check` instead; for Claude, run `bash "<plugin-root>/scripts/claude-setup.sh" check` instead.

- Present the selected script's actual output and a short readiness interpretation.
- OpenCode readiness checks OpenCode and its provider setup; Codex readiness checks the Codex CLI. Report missing setup without installing anything.
- Claude readiness checks the Claude Code CLI's version and help output only; it does not log in or start a task. Login status is read at launch when no route is given or when route `sub` is requested.
- OpenCode's optional PATH shortcuts are only needed when the user explicitly asks to link or unlink them.

