#!/usr/bin/env bash
# shellcheck shell=bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# oc-run.sh — shared parsing, session handling, and one OpenCode invocation for oc-sub.sh and oc-api.sh.
#
# OpenCode headless runs need supervisor-managed recovery for two failure modes because a process exit does not always mean the task finished. A silent stall can leave work half-done: the model or gateway may stop producing output, or a headless permission prompt may wait for a person who is not there.
#
# A reasoning turn can also exhaust its output budget and finish with reason "length". OpenCode may end such a turn with no text or tool call and still exit 0, which makes truncation look like success. The relay retries only after it identifies this job's own session; it never guesses from the newest session in a shared working directory.
#
# The relay supervisor owns those retries, each as a separate guarded attempt with its own process group. Keeping the wrapper to one invocation avoids signaling sampled PIDs that may have been reused. Standalone wrapper use also runs one invocation; `OC_IDLE_TIMEOUT` and `OC_MAX_RESUMES` are interpreted by the OpenCode adapter.

oc_usage() {
    cat <<EOF
$OC_SCRIPT_NAME — run an OpenCode $OC_ROUTE_DESCRIPTION for one-shot development or review work.

Usage:
    $OC_SCRIPT_NAME [flags] "<task text>"
    $OC_SCRIPT_NAME [flags] --stdin <<'EOF' ... EOF

Task input:
    "<task text>"      positional arguments
    --stdin            read literal task text from stdin
    --task-file PATH   read literal task text from a file

Mode:
    --write            allow edits and command execution with the build agent
    (omit)             read-only review or diagnosis with the plan agent

Model:
    --model M          use an alias or provider/model id; bare names use $OC_DEFAULT_PROVIDER/
    $OC_MODEL_SHORTCUTS
    (omit)             let OpenCode use its configured default model
    Aliases may be set in the aliases section of ~/.config/agent-relay/config.json or
    \$OC_HELPER_CONFIG. Otherwise family shortcuts resolve against the live model catalog.
EOF
    if [ "$OC_BACKEND_KIND" = "sub" ]; then
        cat <<'EOF'

Provider:
    --zen              use OpenCode Zen (provider prefix opencode/, the default)
    --go               use OpenCode Go (provider prefix opencode-go/)
    Without --model, either flag moves OpenCode's configured subscription default to the selected gateway.
EOF
    fi
    cat <<EOF

Effort:
    --effort V         pass V to opencode run --variant

Working directory:
    --dir PATH         set OpenCode's working directory (default: current directory)

Session:
    --resume           continue the last OpenCode session
    --resume-latest    continue the latest thread recorded by this wrapper for --dir
    --session ID       continue an exact OpenCode session

Title:
    --title TEXT       set the title for a new session; otherwise a short task summary is used

Output:
    --json             emit raw JSON events
    --quiet            strip ANSI decorations and collapse repeated blank lines

Permissions:
    OC_SKIP_PERMISSIONS=1 opts into --dangerously-skip-permissions; it is off by default.
    Prefer allow-listing needed external paths in OpenCode's permission.external_directory config.

Managed execution:
    The relay supervisor owns idle and truncation retries. A direct wrapper call runs one
    OpenCode invocation. OC_IDLE_TIMEOUT defaults to 600 seconds and OC_MAX_RESUMES to 3.
    Output is raised to 128000 tokens by default, bounded by the model's own output limit;
    OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX overrides the wrapper default.
EOF
    if [ "$OC_BACKEND_KIND" = "api" ]; then
        cat <<'EOF'

Examples:
    oc-api "review the staged diff"
    oc-api --model pro "review the staged diff"
    oc-api --model openai/gpt-5.4 --effort high "summarize this repository"
EOF
    else
        cat <<'EOF'

Examples:
    oc-sub "review the staged diff"
    oc-sub --model glm "review the staged diff"
    oc-sub --go --model kimi "review the staged diff with Go billing"
    oc-sub --model kimi --write "implement the requested change"
EOF
    fi
}

oc_resolve_model() {
    python3 "$SELF_DIR/oc-model.py" resolve --backend "$1" --provider "$2" "$3"
}

oc_own_session() {
    [ -z "$known_session" ] && [ -n "$run_title" ] || return 0
    [ -f "$SELF_DIR/oc-session.py" ] && command -v python3 >/dev/null 2>&1 || return 0
    known_session="$(python3 "$SELF_DIR/oc-session.py" find --dir "$dir" --title "$run_title" --since-ms "$((run_started_ms - 2000))" 2>/dev/null || true)"
}

oc_session_observe() {
    oc_own_session
    [ -n "$known_session" ] && [ -n "${OC_RELAY_SESSION_FILE:-}" ] || return 0
    local tmp="${OC_RELAY_SESSION_FILE}.tmp.$$"
    (umask 077; printf '%s\n' "$known_session" > "$tmp") || return 0
    mv -f "$tmp" "$OC_RELAY_SESSION_FILE" 2>/dev/null || rm -f "$tmp"
}

oc_completion_reason() {
    [ -f "$SELF_DIR/oc-session.py" ] && command -v python3 >/dev/null 2>&1 || { printf 'unknown'; return; }
    local reason=""
    oc_own_session
    [ -n "$known_session" ] || { printf 'unknown'; return; }
    reason="$(python3 "$SELF_DIR/oc-session.py" last-finish --dir "$dir" --session "$known_session" 2>/dev/null || true)"
    case "$reason" in
        length) printf 'length' ;;
        '') printf 'unknown' ;;
        *) printf 'stop' ;;
    esac
}

oc__run_once() {
    OC_ONCE_RC=0
    local session_tmp="" session_done="" session_pid="" oc_pid=""

    if declare -F oc_session_observe >/dev/null 2>&1 \
        && [ -n "${OC_RELAY_SESSION_FILE:-}" ]; then
        session_tmp="$(mktemp -d "${TMPDIR:-/tmp}/oc-session-observe.XXXXXX" 2>/dev/null || true)"
        if [ -n "$session_tmp" ]; then
            session_done="$session_tmp/done"
            (
                while [ ! -f "$session_done" ]; do
                    oc_session_observe || true
                    sleep 1
                done
                oc_session_observe || true
            ) &
            session_pid=$!
        fi
    fi

    if [ "$quiet" -eq 1 ]; then
        (
            opencode "$@" | sed $'s/\x1b\[[0-9;]*[A-Za-z]//g' | cat -s
            exit "${PIPESTATUS[0]}"
        ) &
    else
        opencode "$@" &
    fi
    oc_pid=$!
    if wait "$oc_pid" 2>/dev/null; then
        OC_ONCE_RC=0
    else
        OC_ONCE_RC=$?
    fi

    if [ -n "$session_done" ]; then
        : > "$session_done"
    fi
    if [ -n "$session_pid" ]; then
        wait "$session_pid" 2>/dev/null || true
    fi
    [ -n "$session_tmp" ] && rm -rf "$session_tmp"
    return 0
}

oc_run_single() {
    trap 'exit 143' TERM
    trap 'exit 130' INT
    trap 'exit 129' HUP
    oc__run_once "$@"
    OC_RC="$OC_ONCE_RC"
    trap - TERM INT HUP
}

oc_wrapper_main() {
    local backend_kind="${OC_BACKEND_KIND:?}"
    local prefix="${OC_DEFAULT_PROVIDER:?}"
    local prefix_set=0
    local model_arg="" agent="plan" fmt="default" dir="$PWD" variant=""
    local task_file="" read_stdin=0 quiet=0 title="" title_set=0
    local resume_latest=0 session_id="" sources=0 model="" sid=""
    local resuming=0 run_title="" run_started_ms="" can_guess=0
    local cfg_model="" session_id_out="" rc=0
    local -a resume=() msg=() args=()
    local known_session=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --write)     agent="build" ;;
            --zen|--go)
                [ "$backend_kind" = "sub" ] || { echo "error: unknown option: $1 (see --help)" >&2; exit 2; }
                if [ "$1" = "--go" ]; then prefix="opencode-go"; else prefix="opencode"; fi
                prefix_set=1 ;;
            --model)     shift; [ $# -gt 0 ] || { echo "error: --model needs a value" >&2; exit 2; }
                         model_arg="$1" ;;
            --effort|--variant)
                         shift; [ $# -gt 0 ] || { echo "error: --effort needs a value" >&2; exit 2; }; variant="$1" ;;
            --dir)       shift; [ $# -gt 0 ] || { echo "error: --dir needs a value" >&2; exit 2; }; dir="$1" ;;
            --resume)    resume=(-c) ;;
            --session)   shift; [ $# -gt 0 ] || { echo "error: --session needs a value" >&2; exit 2; }; resume=(-s "$1"); session_id="$1" ;;
            --resume-latest) resume_latest=1 ;;
            --title)     shift; [ $# -gt 0 ] || { echo "error: --title needs a value" >&2; exit 2; }; title="$1"; title_set=1 ;;
            --json)      fmt="json" ;;
            --stdin)     read_stdin=1 ;;
            --quiet)     quiet=1 ;;
            --task-file) shift; [ $# -gt 0 ] || { echo "error: --task-file needs a value" >&2; exit 2; }; task_file="$1" ;;
            -h|--help)   oc_usage; exit 0 ;;
            --)          shift; while [ $# -gt 0 ]; do msg+=("$1"); shift; done; break ;;
            -*)          echo "error: unknown option: $1 (see --help)" >&2; exit 2 ;;
            *)           msg+=("$1") ;;
        esac
        shift
    done

    [ -n "$task_file" ] && sources=$((sources + 1))
    [ "$read_stdin" -eq 1 ] && sources=$((sources + 1))
    [ "${#msg[@]}" -gt 0 ] && sources=$((sources + 1))
    if [ "$sources" -gt 1 ]; then
        echo "error: pass the task in only one way (positional text, --task-file, or --stdin)" >&2
        exit 2
    fi

    if [ -n "$task_file" ]; then
        [ -r "$task_file" ] || { echo "error: --task-file not readable: $task_file" >&2; exit 2; }
        msg=("$(cat -- "$task_file")")
    elif [ "$read_stdin" -eq 1 ]; then
        msg=("$(cat)")
    fi

    if [ "${#msg[@]}" -eq 0 ] || [ -z "${msg[0]}" ]; then
        echo "error: no task text given (see --help)" >&2
        exit 2
    fi

    command -v opencode >/dev/null 2>&1 || { echo "error: opencode CLI not found on PATH" >&2; exit 1; }
    if [ -n "$model_arg" ]; then
        model="$(oc_resolve_model "$backend_kind" "$prefix" "$model_arg")" || exit 2
    fi

    if [ "$backend_kind" = "sub" ] && [ -z "$model" ] && [ "$prefix_set" -eq 1 ]; then
        cfg_model="$(opencode debug config 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model") or "")' 2>/dev/null || true)"
        case "$cfg_model" in
            opencode/*|opencode-go/*) model="$prefix/${cfg_model#*/}" ;;
            "") echo "error: --zen/--go without --model needs a default \"model\" in your opencode config; set one or pass --model" >&2; exit 2 ;;
            *)  echo "error: OpenCode's default model ($cfg_model) is not on a subscription gateway; pass --model" >&2; exit 2 ;;
        esac
    fi

    export OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX="${OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX:-128000}"
    known_session="$session_id"
    if [ "$resume_latest" -eq 1 ] && [ "${#resume[@]}" -eq 0 ]; then
        sid=""
        if [ -f "$SELF_DIR/oc-session.py" ] && command -v python3 >/dev/null 2>&1; then
            sid="$(python3 "$SELF_DIR/oc-session.py" resolve-latest --dir "$dir" 2>/dev/null || true)"
        fi
        if [ -n "$sid" ]; then
            resume=(-s "$sid"); known_session="$sid"
        else
            resume=(-c)
        fi
    fi

    [ "${#resume[@]}" -gt 0 ] && resuming=1
    args=(run --agent "$agent" --format "$fmt" --dir "$dir")
    [ -n "$model" ] && args+=(-m "$model")
    [ "${OC_SKIP_PERMISSIONS:-0}" = "1" ] && args+=(--dangerously-skip-permissions)
    [ -n "$variant" ] && args+=(--variant "$variant")
    [ "${#resume[@]}" -gt 0 ] && args+=("${resume[@]}")

    if [ "$title_set" -eq 1 ]; then
        args+=(--title "$title")
        [ "$resuming" -eq 0 ] && run_title="$title"
    elif [ "$resuming" -eq 0 ]; then
        local synth=""
        synth="$(printf '%s\n' "${msg[0]}" | grep -m1 '[^[:space:]]' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' || true)"
        synth="${synth:0:72}"
        if [ -n "$synth" ]; then args+=(--title "$synth"); run_title="$synth"; fi
    fi
    args+=("${msg[@]}")

    run_started_ms="$(python3 -c 'import time; print(int(time.time() * 1000))' 2>/dev/null || echo 0)"
    oc_run_single "${args[@]}"
    rc="$OC_RC"

    oc_own_session
    [ -z "$run_title" ] && can_guess=1
    if [ -z "$model" ] && [ -f "$SELF_DIR/oc-session.py" ] && command -v python3 >/dev/null 2>&1; then
        if [ -n "$known_session" ]; then
            model="$(python3 "$SELF_DIR/oc-session.py" last-model --dir "$dir" --session "$known_session" 2>/dev/null || true)"
        elif [ "$can_guess" -eq 1 ]; then
            model="$(python3 "$SELF_DIR/oc-session.py" last-model --dir "$dir" 2>/dev/null || true)"
        fi
        [ -n "$model" ] || model="(opencode default)"
    fi

    session_id_out=""
    if [ -f "$SELF_DIR/oc-session.py" ] && command -v python3 >/dev/null 2>&1; then
        if [ -n "$known_session" ]; then
            session_id_out="$(python3 "$SELF_DIR/oc-session.py" record --dir "$dir" --backend "$backend_kind" --model "$model" --agent "$agent" --known-session "$known_session" --print-id 2>/dev/null || true)"
        elif [ "$can_guess" -eq 1 ]; then
            session_id_out="$(python3 "$SELF_DIR/oc-session.py" record --dir "$dir" --backend "$backend_kind" --model "$model" --agent "$agent" --print-id 2>/dev/null || true)"
        fi
    fi

    if [ -n "$session_id_out" ] && [ "$fmt" != "json" ]; then
        printf '\n[relay-session] id=%s backend=%s model=%s dir=%s\n' "$session_id_out" "$backend_kind" "$model" "$dir"
    fi
    if [ "${OC_RELAY_SINGLE_ATTEMPT:-0}" = "1" ]; then
        printf '[relay-completion] finish=%s\n' "$(oc_completion_reason)" >&2
    fi
    exit "$rc"
}
