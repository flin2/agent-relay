#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# Cross-platform smoke checks for the agent-relay OpenCode scripts; no real provider calls.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SCRIPTS="$ROOT/plugins/agent-relay/scripts"
WORK="$(mktemp -d)"
cid=""
cleanup_fixture_job() {
    [ -n "$cid" ] && [ -n "${job:-}" ] || return 0
    python3 "$job" cancel "$cid" --timeout 8 >/dev/null 2>&1 || true
    python3 "$job" wait "$cid" --timeout 8 >/dev/null 2>&1 || true
    state="$(python3 "$job" status "$cid" --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", ""))' 2>/dev/null || true)"
    case "$state" in
        done|failed|cancelled|crashed) return 0 ;;
        *) echo "FAIL  surviving relay fixture $cid (state: ${state:-unknown})" >&2; return 1 ;;
    esac
}
cleanup() {
    cleanup_status=0
    cleanup_fixture_job || cleanup_status=1
    python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$WORK"
    [ "$cleanup_status" -eq 0 ] || exit 1
}
trap cleanup EXIT

fail=0
ok() { printf 'PASS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }
expect_has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" ;; esac; }
expect_absent() { case "$2" in *"$3"*) bad "$1" ;; *) ok "$1" ;; esac; }
expect_nonempty() { if [ -n "$2" ]; then ok "$1"; else bad "$1"; fi; }
expect_argv() {
  local label="$1" args_file="$2"
  shift 2
  if python3 - "$args_file" "$@" <<'PY'
import os
from pathlib import Path
import sys

actual = Path(sys.argv[1]).read_bytes().split(b'\0')
if actual and actual[-1] == b'':
    actual.pop()
actual = [os.fsdecode(arg) for arg in actual]
if actual != sys.argv[2:]:
    print('actual:   {!r}'.format(actual), file=sys.stderr)
    print('expected: {!r}'.format(sys.argv[2:]), file=sys.stderr)
    sys.exit(1)
PY
  then
    ok "$label"
  else
    bad "$label"
  fi
}

command -v python3 >/dev/null 2>&1 || { echo "python3 required" >&2; exit 2; }
command -v bash >/dev/null 2>&1 || { echo "bash required" >&2; exit 2; }

export TMPDIR="$WORK/tmp"
export HOME="$WORK/home"
export XDG_STATE_HOME="$WORK/xdg"
export RELAY_JOB_STATE_DIR="$WORK/state"
export RELAY_JOB_HOST=claude
export RELAY_JOB_CONVERSATION_ID=smoke-conversation
export RELAY_MODEL_CONFIG="$WORK/agent-relay-config.json"
export OC_WATCH_INTERVAL=1
export OC_IDLE_TIMEOUT=600
export OC_SKIP_PERMISSIONS=0
export OC_RELAY_SINGLE_ATTEMPT=0
export CLAUDE_PLUGIN_DATA="$WORK/ignored-claude-data"
export PLUGIN_DATA="$WORK/ignored-plugin-data"
mkdir -p "$TMPDIR" "$HOME"

REPO="$WORK/repo"; mkdir -p "$REPO"
BIN="$WORK/bin"; mkdir -p "$BIN"
cat > "$BIN/opencode" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  auth) echo "stub-auth: ${*:2}"; exit 0 ;;
  run)
        printf '%s\0' "$@" > "$OC_STUB_ARGS_FILE"
        printf '%s\n' "${OC_RELAY_SINGLE_ATTEMPT:-0}" > "$OC_STUB_MODE_FILE"
        if [ "${OC_STUB_SLEEP:-0}" = "1" ]; then
          printf 'started\n' > "$OC_STUB_STARTED_FILE"
          trap 'printf "term\n" > "$OC_STUB_STOPPED_FILE"; exit 143' TERM
          sleep 30
        fi
        model=""; previous=""; title=""
        for arg in "$@"; do
          [ "$previous" = "-m" ] && model="$model${model:+,}$arg"
          [ "$previous" = "--title" ] && title="$arg"
          previous="$arg"
        done
        [ -n "$title" ] && printf '%s\n' "$title" > "$OC_STUB_TITLE_FILE"
        printf 'STUB_OK \033[31mcolored\033[0m line\nSTUB_MODEL=%s;\n' "${model:-none}"
        [ "${OC_STUB_STDERR:-0}" = "1" ] && printf 'STUB_STDERR\n' >&2
        exit "${OC_STUB_EXIT:-0}" ;;
  models)
        if [ "${2:-}" = "deepseek" ]; then
          for model in deepseek-v4-pro deepseek-v4-flash; do printf '%s/%s\n' "$2" "$model"; done
        else
          for model in glm-5.9 glm-5.10 glm-5.10-flash glm-5 kimi-k3 kimi-k2.7-code deepseek-v4-pro deepseek-v4-flash; do
            printf '%s/%s\n' "${2:-}" "$model"
          done
        fi
        exit 0 ;;
  session)
        if [ "${2:-}" = "list" ] && [ "${OC_STUB_SESSIONS:-0}" = "1" ]; then
          now="$(python3 -c 'import time; print(int(time.time() * 1000))')"
          if [ -n "${OC_STUB_TITLE:-}" ]; then
            title="$OC_STUB_TITLE"
          elif [ -r "${OC_STUB_TITLE_FILE:-}" ]; then
            title="$(cat "$OC_STUB_TITLE_FILE")"
          fi
          printf '[{"id":"ses_mine","title":"%s","directory":"%s","created":%s,"updated":%s},{"id":"ses_other","title":"other job","directory":"%s","created":%s,"updated":%s}]\n' \
            "$title" "$PWD" "$now" "$now" "$PWD" "$now" "$((now + 1000))"
        fi
        exit 0 ;;
  debug)
        [ "${2:-}" = "config" ] && printf '{"model": "%s"}\n' "${OC_STUB_CFG_MODEL:-}"
        exit 0 ;;
  *)
        echo "stub-opencode: $*"
        exit 0 ;;
esac
STUB
chmod +x "$BIN/opencode"
export PATH="$BIN:$PATH"
export OC_STUB_TITLE_FILE="$WORK/stub-title.txt"
export OC_STUB_STARTED_FILE="$WORK/stub-opencode.started"
export OC_STUB_STOPPED_FILE="$WORK/stub-opencode.stopped"
export OC_STUB_ARGS_FILE="$WORK/stub-opencode.args"
export OC_STUB_MODE_FILE="$WORK/stub-opencode.mode"

esc="$(printf '\033')"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet "hello sub")"
expect_has "subscription wrapper forwards stub output" "$out" "STUB_OK"
expect_absent "quiet wrapper strips ANSI escapes" "$out" "$esc"

out="$(bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet "hello api")"
expect_has "direct API wrapper forwards stub output" "$out" "STUB_OK"

out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet "m")"
expect_has "subscription wrapper without model leaves default selection to OpenCode" "$out" "STUB_MODEL=none;"
out="$(bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet "m")"
expect_has "API wrapper without model leaves default selection to OpenCode" "$out" "STUB_MODEL=none;"
out="$(bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet --model pro "m")"
expect_has "API built-in alias resolves the newest DeepSeek family model" "$out" "STUB_MODEL=deepseek/deepseek-v4-pro;"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --zen --model kimi "m")"
expect_has "explicit Zen gateway keeps the Zen provider prefix" "$out" "STUB_MODEL=opencode/kimi-k3;"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model glm --go "m")"
expect_has "model family resolves latest version on Go" "$out" "STUB_MODEL=opencode-go/glm-5.10;"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model kimi "m")"
expect_has "model family skips suffixed variants" "$out" "STUB_MODEL=opencode/kimi-k3;"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model some-model "m")"
expect_has "bare subscription model gets provider prefix" "$out" "STUB_MODEL=opencode/some-model;"
printf '{"aliases": {"api": {"mine": "openai/gpt-5.4"}, "sub": {"glm": "glm-5.9", "mine": "opencode-go/kimi-k3"}}}\n' > "$RELAY_MODEL_CONFIG"
out="$(bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet --model mine "m")"
expect_has "API config alias supports a full provider ID" "$out" "STUB_MODEL=openai/gpt-5.4;"
rc=0
bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model minimax "m" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then ok "missing model catalog match refuses"; else bad "missing model catalog match refuses"; fi
printf '{"aliases": {"sub": {"glm": "glm-5.9", "mine": "opencode-go/kimi-k3"}}}\n' > "$RELAY_MODEL_CONFIG"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --go --model glm "m")"
expect_has "config alias overrides built-in under selected gateway" "$out" "STUB_MODEL=opencode-go/glm-5.9;"
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model mine "m")"
expect_has "config alias supports a full provider ID" "$out" "STUB_MODEL=opencode-go/kimi-k3;"
printf '{"aliases": {"sub": {"glm": "glm-5.9"}}}\n' > "$WORK/compat-model-config.json"
out="$(RELAY_MODEL_CONFIG='' OC_HELPER_CONFIG="$WORK/compat-model-config.json" bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model glm "m")"
expect_has "legacy OpenCode model-config override remains supported" "$out" "STUB_MODEL=opencode/glm-5.9;"
printf '{not json\n' > "$RELAY_MODEL_CONFIG"
rc=0
bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --model glm "m" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then ok "malformed model config refuses"; else bad "malformed model config refuses"; fi
python3 -c 'import os; os.unlink(os.environ["RELAY_MODEL_CONFIG"])'
out="$(OC_STUB_CFG_MODEL=opencode/kimi-k3 bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --go "m")"
expect_has "Go gateway moves configured default model" "$out" "STUB_MODEL=opencode-go/kimi-k3;"
rc=0
out="$(bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --go "m" 2>&1)" || rc=$?
if [ "$rc" -eq 2 ]; then ok "Go gateway without a configured default refuses"; else bad "Go gateway without a configured default refuses"; fi

literal_task='literal $HOME `printf not-run` "quotes" and \ backslash'
literal_out="$WORK/literal-task.out"
printf '%s' "$literal_task" | bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --write --model glm --effort high --session ses_exact --stdin > "$literal_out"
expect_has "literal stdin task reaches OpenCode" "$(cat "$literal_out")" "STUB_OK"
expect_argv "model, effort, exact session, and literal task use exact arguments" "$OC_STUB_ARGS_FILE" \
  run --agent build --format default --dir "$REPO" -m opencode/glm-5.10 --variant high -s ses_exact "$literal_task"

OC_SKIP_PERMISSIONS=1 bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet --model v4-pro --session ses_permission "permissions" >/dev/null
expect_argv "dangerous permission bypass is opt-in and forwarded exactly" "$OC_STUB_ARGS_FILE" \
  run --agent plan --format default --dir "$REPO" -m deepseek/deepseek-v4-pro --dangerously-skip-permissions -s ses_permission permissions

stream_out="$WORK/standalone.stdout"
stream_err="$WORK/standalone.stderr"
OC_STUB_STDERR=1 bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet --session ses_stream "stream check" > "$stream_out" 2> "$stream_err"
expect_has "standalone stdout keeps OpenCode output and session trailer" "$(cat "$stream_out")" "STUB_OK"
expect_absent "standalone stdout excludes diagnostics" "$(cat "$stream_out")" "STUB_STDERR"
expect_has "standalone stderr keeps OpenCode diagnostics" "$(cat "$stream_err")" "STUB_STDERR"
expect_absent "standalone call omits managed completion status" "$(cat "$stream_err")" "[relay-completion]"
rc=0
OC_STUB_EXIT=19 bash "$SCRIPTS/oc-api.sh" --dir "$REPO" --quiet --session ses_nonzero "nonzero" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 19 ]; then ok "standalone wrapper propagates OpenCode exit status"; else bad "standalone wrapper propagates OpenCode exit status"; fi

job="$SCRIPTS/relay-job.py"
rc=0
error="$(python3 "$job" run --backend codex --route sub --stdin </dev/null 2>&1)" || rc=$?
if [ "$rc" -ne 0 ]; then
  case "$error" in *"OpenCode-only flags"*) ok "Codex rejects OpenCode-only flags" ;; *) bad "Codex rejects OpenCode-only flags" ;; esac
else
  bad "Codex rejects OpenCode-only flags"
fi
rc=0
python3 "$job" run --backend opencode --stdin </dev/null >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then ok "OpenCode route is required"; else bad "OpenCode route is required"; fi
rc=0
python3 "$job" run --backend opencode --route api --go --stdin </dev/null >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then ok "subscription gateway flags reject the API route"; else bad "subscription gateway flags reject the API route"; fi

out="$(OC_STUB_SESSIONS=1 OC_STUB_TITLE='own session task' bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet "own session task")"
expect_has "wrapper session trailer names its own session" "$out" "[relay-session] id=ses_mine"
expect_absent "wrapper session trailer ignores another session" "$out" "ses_other"
expect_absent "standalone wrapper omits relay completion trailer" "$out" "[relay-completion]"
out="$(OC_STUB_SESSIONS=1 bash "$SCRIPTS/oc-sub.sh" --dir "$REPO" --quiet --resume-latest "latest session task")"
expect_has "latest-session shortcut resumes the recorded session" "$out" "[relay-session] id=ses_mine"
expect_argv "latest-session shortcut passes an exact session argument" "$OC_STUB_ARGS_FILE" \
  run --agent plan --format default --dir "$REPO" -s ses_mine "latest session task"

out="$(OC_STUB_SESSIONS=1 python3 "$job" run --backend opencode --route sub --wait --dir "$REPO" --label "relay title smoke" --stdin <<'RELAY_TASK_EOF'
foreground job
RELAY_TASK_EOF
)"
expect_has "foreground manager job streams wrapper output" "$out" "STUB_OK"
expect_has "manager records exact backend session" "$out" "[relay-session] id=ses_mine"
title="$(cat "$OC_STUB_TITLE_FILE")"
if printf '%s' "$title" | python3 -c 'import re,sys; sys.exit(0 if re.search(r"\[relay:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\]",sys.stdin.read()) else 1)'; then
  ok "fresh OpenCode title includes relay job identity"
else
  bad "fresh OpenCode title includes relay job identity"
fi

task_file="$WORK/background-task.txt"
printf 'background job\n' > "$task_file"
jid="$(OC_STUB_SESSIONS=1 OC_STUB_STDERR=1 python3 "$job" run --backend opencode --route sub --dir "$REPO" --task-file "$task_file" 2>/dev/null)"
expect_nonempty "detached run prints a job ID" "$jid"
python3 "$job" wait "$jid" --timeout 60 --interval 0.05 >/dev/null
status="$(python3 "$job" status "$jid" 2>/dev/null)"
expect_has "detached job reaches done" "$status" "done"
result="$(python3 "$job" result "$jid" 2>/dev/null)"
expect_has "detached result includes wrapper output" "$result" "STUB_OK"
stderr_result="$(python3 "$job" result "$jid" --stderr 2>/dev/null)"
expect_absent "stderr selector keeps stdout out of diagnostics" "$stderr_result" "STUB_OK"
events_result="$(python3 "$job" result "$jid" --events 2>/dev/null)"
expect_absent "events selector stays separate for OpenCode text output" "$events_result" "STUB_OK"
json_status="$(python3 "$job" status "$jid" --json)"
expect_has "job metadata records OpenCode backend and route" "$json_status" '"backend": "opencode"'
expect_has "job metadata records distinct host conversation fields" "$json_status" '"host_conversation_key": "claude:smoke-conversation"'
expect_has "managed job stores the discovered backend session" "$json_status" '"backend_session_id": "ses_mine"'
expect_has "managed wrapper receives the one-attempt marker" "$(cat "$OC_STUB_MODE_FILE")" "1"
managed_stderr="$(python3 "$job" result "$jid" --stderr 2>/dev/null)"
expect_has "managed stderr preserves OpenCode diagnostics" "$managed_stderr" "STUB_STDERR"
expect_has "managed stderr reports completion to the supervisor" "$managed_stderr" "[relay-completion] finish="

export OC_STUB_SLEEP=1
cid="$(python3 "$job" run --backend opencode --route sub --dir "$REPO" --label "cancel smoke" --stdin 2>/dev/null <<'RELAY_TASK_EOF'
sleepy
RELAY_TASK_EOF
)"
unset OC_STUB_SLEEP
i=0
while [ "$i" -lt 100 ]; do
  [ -r "$OC_STUB_STARTED_FILE" ] && break
  cstat="$(python3 "$job" status "$cid" 2>/dev/null || true)"
  case "$cstat" in *'Status:                cancelled'*|*'Status:                crashed'*|*'Status:                failed'*|*'Status:                done'*) break ;; esac
  i=$((i + 1))
  sleep 0.1
done
if [ -r "$OC_STUB_STARTED_FILE" ]; then ok "cancel fixture starts OpenCode before cancellation"; else bad "cancel fixture starts OpenCode before cancellation"; fi
python3 "$job" cancel "$cid" >/dev/null 2>&1 || true
i=0
cstat=""
while [ "$i" -lt 20 ]; do
  cstat="$(python3 "$job" status "$cid" 2>/dev/null || true)"
  case "$cstat" in *cancelled*) break ;; esac
  i=$((i + 1))
  sleep 1
done
expect_has "cancel marks the job cancelled" "$cstat" "cancelled"
rc=0
python3 "$job" wait "$cid" --interval 0.05 >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 3 ]; then ok "wait returns the cancelled exit code"; else bad "wait returns the cancelled exit code"; fi
if [ -r "$OC_STUB_STOPPED_FILE" ]; then
  ok "cancel delivers TERM to the OpenCode fixture"
else
  bad "cancel delivers TERM to the OpenCode fixture"
fi

setup_output="$(bash "$SCRIPTS/oc-setup.sh" check 2>&1 || true)"
expect_nonempty "setup readiness check runs with stub OpenCode" "$setup_output"

echo
if [ "$fail" -eq 0 ]; then
  echo "smoke: all checks passed"
else
  echo "smoke: FAILURES above" >&2
fi
exit "$fail"

