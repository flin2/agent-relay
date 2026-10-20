#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# claude-setup — Claude Code backend capability check for the agent-relay plugin.
set -u

usage() {
    cat <<'EOF'
claude-setup — check whether the Claude Code CLI supports agent-relay execution.

Usage:
    claude-setup [check]
    claude-setup -h, --help

This check requires only the Claude Code CLI. It runs version and help output only; it does not check OpenCode or Codex, log in, read credentials, or start a model task. Login status is read at launch time when no --route is given or when --route sub is requested. New jobs select the backend with relay-job.py run --backend claude and are read-only unless --write is supplied. Resume targets the recorded session and resets permission to read-only unless its new job includes --write.
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
if claude_path="$(command -v claude 2>/dev/null)"; then
    echo "claude: found ($claude_path)"
else
    echo "claude: NOT FOUND on PATH"
    echo "advice: install the Claude Code CLI and add it to PATH"
    echo "NOT READY"
    exit 1
fi

scratch="$(mktemp -d "${TMPDIR:-/tmp}/claude-setup.XXXXXX")" || {
    echo "error: cannot create a temporary directory" >&2
    exit 1
}
trap 'rm -rf "$scratch"' EXIT

# Output is written to files, not pipes: piped output from the Claude CLI can be truncated when it exits.
version_file="$scratch/version.txt"
if claude --version >"$version_file" 2>&1; then
    echo "claude --version: $(head -n 1 "$version_file")"
else
    echo "claude --version: FAILED"
    ready=0
fi

help_file="$scratch/help.txt"
if claude --help >"$help_file" 2>&1; then
    fresh_help="$(cat "$help_file")"
    for capability in --session-id --permission-prompts --restricted --safe-mode \
        --strict-mcp-config --effort stream-json --resume; do
        case "$fresh_help" in
            *"$capability"*) echo "claude --help: supports $capability" ;;
            *) echo "claude --help: MISSING required option $capability"; ready=0 ;;
        esac
    done
else
    echo "claude --help: FAILED"
    ready=0
fi

echo "note: login status is read at launch when no --route is given or --route sub is requested; this check did not read it"

if [ "$ready" -eq 1 ]; then
    echo "READY"
    exit 0
fi
echo "NOT READY"
exit 1
