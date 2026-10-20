---
name: relay-adversarial-review
description: Challenge the assumptions and shipping readiness of local changes through the selected backend.
---

# Challenge local changes

Follow `../relay-review/SKILL.md` for scope selection, backend choice, routing, execution, and collection, with these differences:

- Use `../../prompts/adversarial-review.md`.
- With no user focus, set `{{USER_FOCUS}}` to `(none — challenge the change broadly)`.
- Preserve the challenge to the approach, design, assumptions, and shipping readiness.

This workflow is read-only. Never pass `--write` or act on reported issues.

