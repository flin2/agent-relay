#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# oc-setup — readiness check + PATH symlink management for the agent-relay plugin.
# Invoked by the /agent-relay:setup slash command and may also be run by hand.
set -euo pipefail

usage() {
    cat <<'EOF'
oc-setup — OpenCode readiness check + optional wrapper PATH links for agent-relay.

Usage:
    oc-setup [check]          print a readiness report (default)
    oc-setup link [DIR]       symlink oc-sub and oc-api into a PATH bin dir (optional; for direct shell use)
    oc-setup unlink [DIR]     remove those two symlinks
    oc-setup -h, --help       print this help and exit

Bin dir resolution for link/unlink (in order): an explicit positional DIR
argument; else $OC_BIN_DIR if set; else $HOME/.local/bin if it exists;
else $HOME/bin.

This checks OpenCode only. Codex readiness is checked by codex-setup.
Relay jobs use relay-job.py and a shared state root; select the backend
explicitly, and select an OpenCode route when starting OpenCode work.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
api_script="$SCRIPT_DIR/oc-api.sh"
sub_script="$SCRIPT_DIR/oc-sub.sh"

resolve_bin_dir() {
    if [ $# -gt 0 ]; then
        echo "$1"; return
    fi
    if [ -n "${OC_BIN_DIR-}" ]; then
        echo "$OC_BIN_DIR"; return
    fi
    if [ -d "$HOME/.local/bin" ]; then
        echo "$HOME/.local/bin"; return
    fi
    echo "$HOME/bin"
}

do_check() {
    local ready=1
    if command -v opencode >/dev/null 2>&1; then
        echo "opencode: found ($(command -v opencode))"
        echo "version: $(opencode --version 2>/dev/null || echo 'unknown')"
        echo ""
        echo "auth credentials:"
        local auth_out
        auth_out=$(opencode auth list 2>&1 || true)
        if [ -n "$auth_out" ]; then
            local stripped
            stripped=$(echo "$auth_out" | sed $'s/\x1b\\[[0-9;]*[A-Za-z]//g')
            echo "$stripped"
            if echo "$stripped" | grep -q "0 credentials"; then
                echo "advice: run 'opencode auth login' to add credentials"
                ready=0
            fi
        else
            echo "(no output from opencode auth list)"
            ready=0
        fi
    else
        echo "opencode: NOT FOUND"
        echo "install options:"
        echo "  brew install sst/tap/opencode"
        echo "  npm i -g opencode-ai"
        echo "  curl -fsSL https://opencode.ai/install | bash"
        ready=0
    fi

    echo ""
    echo "direct-CLI shortcuts (optional, not needed by the plugin — the host CLI runs the"
    echo "backends via scripts/relay-job.py by absolute path; link only for your own shell use):"
    local names=("oc-sub" "oc-api")
    local targets=("$sub_script" "$api_script")
    for i in "${!names[@]}"; do
        local name="${names[$i]}"
        local target="${targets[$i]}"
        if command -v "$name" >/dev/null 2>&1; then
            # Canonicalize the resolved path portably. GNU `readlink -f` is unavailable on
            # macOS/BSD, so resolve via python3 (already a dependency, as in the wrappers),
            # falling back to the unresolved path.
            local resolved path
            path="$(command -v "$name")"
            if command -v python3 >/dev/null 2>&1; then
                resolved="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path" 2>/dev/null || echo "$path")"
            else
                resolved="$path"
            fi
            if [ "$resolved" = "$target" ]; then
                echo "  $name: linked ($resolved)"
            else
                echo "  $name: on PATH but resolves elsewhere ($resolved)"
            fi
        else
            echo "  $name: not linked (optional)"
        fi
    done

    echo ""
    if [ "$ready" -eq 1 ]; then
        echo "READY"
    else
        echo "NOT READY"
    fi
}

do_link() {
    local dir
    dir="$(resolve_bin_dir "$@")"
    mkdir -p "$dir"
    ln -sfn "$sub_script" "$dir/oc-sub"
    echo "linked: $dir/oc-sub -> $sub_script"
    ln -sfn "$api_script" "$dir/oc-api"
    echo "linked: $dir/oc-api -> $api_script"
    case ":${PATH}:" in
        *:"${dir}":*) ;;
        *) echo "warning: $dir is not on PATH — add it to your PATH to use oc-sub/oc-api directly" ;;
    esac
}

do_unlink() {
    local dir
    dir="$(resolve_bin_dir "$@")"
    for link in oc-sub oc-api; do
        local linkpath="$dir/$link"
        if [ -L "$linkpath" ]; then
            local target
            target="$(readlink "$linkpath")"
            local is_ours=0
            for backend in "$api_script" "$sub_script"; do
                if [ "$target" = "$backend" ]; then
                    is_ours=1; break
                fi
            done
            if [ "$is_ours" -eq 1 ]; then
                rm "$linkpath"
                echo "removed: $linkpath"
            else
                echo "skipped: $linkpath points elsewhere ($target)"
            fi
        elif [ -e "$linkpath" ]; then
            echo "skipped: $linkpath is a regular file (not a symlink)"
        else
            echo "skipped: $linkpath does not exist"
        fi
    done
}

case "${1:-check}" in
    check)       do_check ;;
    link)        shift; do_link "$@" ;;
    unlink)      shift; do_unlink "$@" ;;
    -h|--help)   usage; exit 0 ;;
    *)           echo "error: unknown subcommand: ${1:-}" >&2
                 usage >&2
                 exit 2 ;;
esac
