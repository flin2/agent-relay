#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# codex-setup — Codex backend capability check for the agent-relay plugin.
set -u

usage() {
    cat <<'EOF'
codex-setup — check whether the Codex CLI supports agent-relay execution.

Usage:
    codex-setup [check]
    codex-setup -h, --help

This check requires only the Codex CLI. It does not check OpenCode or run a model task.
New jobs select the backend with relay-job.py run --backend codex and are read-only
unless --write is supplied. Resume targets a recorded thread and resets permission
to read-only unless its new job includes --write.
EOF
}

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "check" ] && [ "$1" != "-h" ] && [ "$1" != "--help" ]; }; then
    echo "error: unknown command: ${1:-}" >&2
    usage >&2
    exit 2
fi
if [ "$#" -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
    usage
    exit 0
fi

ready=1
if codex_path="$(command -v codex 2>/dev/null)"; then
    echo "codex: found ($codex_path)"
else
    echo "codex: NOT FOUND on PATH"
    echo "advice: install the Codex CLI and add it to PATH"
    echo "NOT READY"
    exit 1
fi

if fresh_help="$(codex exec --help 2>&1)"; then
    for capability in --json --output-last-message; do
        case "$fresh_help" in
            *"$capability"*) echo "codex exec: supports $capability" ;;
            *) echo "codex exec: MISSING required option $capability"; ready=0 ;;
        esac
    done
else
    echo "codex exec --help: FAILED"
    ready=0
fi

if codex exec resume --help >/dev/null 2>&1; then
    echo "codex exec resume --help: available"
else
    echo "codex exec resume --help: FAILED"
    ready=0
fi

if [ "$ready" -eq 1 ]; then
    echo "READY"
    exit 0
fi
echo "NOT READY"
exit 1
