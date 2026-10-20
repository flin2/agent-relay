---
name: relay-setup
description: Check readiness for one selected agent-relay backend in Codex.
---

# Check backend readiness

Use the backend explicitly requested by the user or supplied as `opencode` or `codex`. If neither identifies a backend, ask which one to check; never run both checks. Resolve the installed plugin root two directories above this skill folder and run only its corresponding readiness script:

```bash
bash "<plugin-root>/scripts/oc-setup.sh" check
```

For Codex, run `bash "<plugin-root>/scripts/codex-setup.sh" check` instead.

- Present the selected script's actual output and a short readiness interpretation.
- OpenCode readiness checks OpenCode and its provider setup; Codex readiness checks the Codex CLI. Report missing setup without installing anything.
- OpenCode's optional PATH shortcuts are only needed when the user explicitly asks to link or unlink them.

