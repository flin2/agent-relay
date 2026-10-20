#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# Opt-in live capability check for agent-relay. Never run this from CI.

set -o pipefail

usage() {
    cat <<'EOF'
live-check.sh — run bounded checks against an installed backend CLI.

Usage:
    RELAY_LIVE_CHECKS=1 bash tests/live/live-check.sh --backend codex|opencode [--notify-thread UUID]
    RELAY_LIVE_CHECKS=1 bash tests/live/live-check.sh --backend claude [--route sub|api] [--model MODEL]
    bash tests/live/live-check.sh --dry-run --backend codex|opencode [--notify-thread UUID]
    bash tests/live/live-check.sh --dry-run --backend claude [--route sub|api] [--model MODEL]

Live checks require RELAY_LIVE_CHECKS=1. --dry-run prints the planned commands and
does not create files or invoke a backend. OpenCode uses the subscription route.
--notify-thread is Codex-only and must name an idle thread that you control.
--route (sub or api) and --model are Claude-only; they default to sub and haiku.
Claude runs need the claude CLI on PATH. The live run retains its isolated
repository, state, and evidence under TMPDIR.
EOF
}

BACKEND=""
NOTIFY_THREAD=""
ROUTE=""
ROUTE_GIVEN=0
MODEL=""
MODEL_GIVEN=0
DRY_RUN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --backend)
            [ "$#" -ge 2 ] || { echo "error: --backend needs codex, opencode, or claude" >&2; exit 2; }
            BACKEND="$2"
            shift 2
            ;;
        --notify-thread)
            [ "$#" -ge 2 ] || { echo "error: --notify-thread needs a UUID" >&2; exit 2; }
            NOTIFY_THREAD="$2"
            shift 2
            ;;
        --route)
            [ "$#" -ge 2 ] || { echo "error: --route needs sub or api" >&2; exit 2; }
            ROUTE="$2"
            ROUTE_GIVEN=1
            shift 2
            ;;
        --model)
            [ "$#" -ge 2 ] || { echo "error: --model needs a model name" >&2; exit 2; }
            MODEL="$2"
            MODEL_GIVEN=1
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$BACKEND" in
    codex|opencode|claude) ;;
    *)
        echo "error: pass the required --backend codex, opencode, or claude" >&2
        exit 2
        ;;
esac

if [ "$BACKEND" = "claude" ]; then
    if [ "$ROUTE_GIVEN" -ne 1 ]; then
        ROUTE="sub"
    fi
    case "$ROUTE" in
        sub|api) ;;
        *)
            echo "error: --route must be sub or api" >&2
            exit 2
            ;;
    esac
    if [ "$MODEL_GIVEN" -ne 1 ]; then
        MODEL="haiku"
    fi
    if [ -z "$MODEL" ]; then
        echo "error: --model needs a non-empty model name" >&2
        exit 2
    fi
elif [ "$ROUTE_GIVEN" -eq 1 ] || [ "$MODEL_GIVEN" -eq 1 ]; then
    echo "error: --route and --model are available only with --backend claude" >&2
    exit 2
fi

if [ -n "$NOTIFY_THREAD" ]; then
    if [ "$BACKEND" != "codex" ]; then
        echo "error: --notify-thread is available only with --backend codex" >&2
        exit 2
    fi
    if [[ ! $NOTIFY_THREAD =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]; then
        echo "error: --notify-thread must be a UUID" >&2
        exit 2
    fi
fi

if [ "$DRY_RUN" -ne 1 ] && [ "${RELAY_LIVE_CHECKS:-}" != "1" ]; then
    echo "error: live checks require RELAY_LIVE_CHECKS=1; use --dry-run to inspect commands" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RELAY_JOB="$REPO_ROOT/plugins/agent-relay/scripts/relay-job.py"
PYTHON="$(command -v python3 2>/dev/null || true)"
TMP_PARENT="${TMPDIR:-/tmp}"
case "$TMP_PARENT" in
    /*) ;;
    *) TMP_PARENT="$(cd "$TMP_PARENT" 2>/dev/null && pwd)" || {
        echo "error: TMPDIR must resolve to an absolute directory" >&2
        exit 2
    } ;;
esac

if [ -z "$PYTHON" ]; then
    echo "error: python3 is required" >&2
    exit 2
fi

if [ "$DRY_RUN" -ne 1 ]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "error: git is required" >&2
        exit 2
    fi
    if ! command -v ps >/dev/null 2>&1; then
        echo "error: ps is required for the cancellation check" >&2
        exit 2
    fi
    case "$BACKEND" in
        codex) BACKEND_CLI="codex" ;;
        claude) BACKEND_CLI="claude" ;;
        *) BACKEND_CLI="opencode" ;;
    esac
    if ! command -v "$BACKEND_CLI" >/dev/null 2>&1; then
        echo "error: $BACKEND_CLI is not on PATH" >&2
        exit 2
    fi
fi

STEP_NAMES=(
    "Read-only response and denied write"
    "Authorized isolated edit"
    "Exact-session continuation and permission reset"
    "SQLite WAL sidecar and concurrent status"
    "Cancellation and process-group cleanup"
    "Result collection after launcher exit"
    "Codex JSONL event and thread.started shape"
    "Optional Codex queue notification"
)
STEP_RESULTS=(
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not reached)"
    "SKIP (not requested)"
)
if [ "$BACKEND" = "claude" ]; then
    STEP_NAMES[6]="Claude stream-json event shape and identity"
    STEP_NAMES+=("Claude read-only Git and subdirectory resume")
    STEP_RESULTS+=("SKIP (not reached)")
fi
RUN_ROOT=""
STATE_DIR=""
PENDING_JOB_ID=""
LAUNCHED_JOB_ID=""
SQLITE_PID=""
SQLITE_RELEASE=""

show_summary() {
    local index
    printf '\nSummary:\n'
    for ((index = 0; index < ${#STEP_NAMES[@]}; index++)); do
        printf '  %s: %s\n' "${STEP_NAMES[$index]}" "${STEP_RESULTS[$index]}"
    done
}

finish() {
    # Capture the exit status before local resets $?.
    local result=$? index failures
    trap - EXIT
    if [ -n "$SQLITE_PID" ] && [ -n "$SQLITE_RELEASE" ]; then
        : > "$SQLITE_RELEASE" 2>/dev/null || true
        wait "$SQLITE_PID" 2>/dev/null || true
    fi
    if [ -n "$PENDING_JOB_ID" ] && [ -n "$STATE_DIR" ]; then
        "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" cancel "$PENDING_JOB_ID" --timeout 15 \
            >/dev/null 2>&1 || true
    fi
    show_summary
    failures=0
    # KNOWN GAP is informational; only FAIL results affect the exit code.
    for ((index = 0; index < ${#STEP_RESULTS[@]}; index++)); do
        case "${STEP_RESULTS[$index]}" in
            FAIL*) failures=$((failures + 1)) ;;
        esac
    done
    if [ "$result" -eq 0 ] && [ "$failures" -gt 0 ]; then
        result=1
    fi
    exit "$result"
}

set_step() {
    STEP_RESULTS[$1]="$2"
}

step_start() {
    printf '\n[%s] %s\n' "$(( $1 + 1 ))" "${STEP_NAMES[$1]}"
}

abort_step() {
    set_step "$1" "FAIL"
    shift
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

print_command() {
    local argument
    printf '  $'
    for argument in "$@"; do
        printf ' %q' "$argument"
    done
    printf '\n'
}

write_attempt_evidence() {
    local stdout_path target evidence_path result
    stdout_path="$1"
    target="$2"
    evidence_path="$3"
    "$PYTHON" -c "$WRITE_ATTEMPT_EVIDENCE_CODE" "$stdout_path" "$target" \
        >"$evidence_path" 2>&1
    result=$?
    cat "$evidence_path"
    case "$result" in
        0|1|2|3|4) return "$result" ;;
        *) return 5 ;;
    esac
}

EVENT_SHAPE_CODE='import collections,json,sys; src,dst=sys.argv[1:]; events=[json.loads(line) for line in open(src,encoding="utf-8") if line.strip()]; counts=collections.Counter(str(event.get("type","<missing>")) for event in events if isinstance(event,dict)); started=[event for event in events if isinstance(event,dict) and event.get("type")=="thread.started"]; shape=started[0] if started else {}; thread=shape.get("thread_id"); output=open(dst,"w",encoding="utf-8"); [output.write("event_type %s count %s\n"%(kind,counts[kind])) for kind in sorted(counts)]; output.write("thread.started top-level keys: %s\n"%(",".join(sorted(shape)))); output.write("thread_id placement: top-level, type=%s, nonempty=%s, value=<redacted>\n"%(type(thread).__name__,str(bool(isinstance(thread,str) and thread)).lower())); output.close(); sys.exit(0 if started and isinstance(thread,str) and thread else 1)'

# Recognize the expected item.completed envelope with a nested command_execution item, accept alternate output/status fields, and require the target touch command in the execution item's command.
WRITE_ATTEMPT_EVIDENCE_CODE='
import json,re,shlex,sys
source,target=sys.argv[1:]
def shell_tokens(command):
    try:
        lexer=shlex.shlex(command,posix=True,punctuation_chars=";&|")
        lexer.whitespace_split=True
        return list(lexer)
    except ValueError:
        return []
def runs_target_touch(tokens):
    segment=[]
    separators=(";","&&","||","|","&")
    def is_touch_call(parts):
        return bool(parts and parts[0].rsplit("/",1)[-1]=="touch" and any(
            argument in (target,"./"+target) for argument in parts[1:]
        ))
    for token in tokens+[";"]:
        if token in separators:
            if is_touch_call(segment):
                return True
            segment=[]
        else:
            segment.append(token)
    for index,token in enumerate(tokens[:-1]):
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            if is_touch_call(shell_tokens(tokens[index+1])):
                return True
    return False
try:
    stream=open(source,encoding="utf-8")
except OSError as exc:
    print("UNVERIFIED: could not read Codex JSONL: %s"%exc)
    sys.exit(2)
attempts=[]
command_items=0
for line_number,line in enumerate(stream,1):
    if not line.strip():
        continue
    try:
        event=json.loads(line)
    except (TypeError,ValueError):
        continue
    if not isinstance(event,dict):
        continue
    item=event.get("item")
    if not isinstance(item,dict):
        if event.get("item_type")=="command_execution":
            item=event
        else:
            continue
    item_type=item.get("type") or item.get("kind") or event.get("item_type")
    if item_type!="command_execution":
        continue
    command_items+=1
    command=item.get("command") or item.get("cmd")
    if isinstance(command,(list,tuple)):
        command_parts=[str(part) for part in command]
        command_tokens=command_parts
    elif isinstance(command,str):
        command_tokens=shell_tokens(command)
    else:
        command_tokens=shell_tokens(json.dumps(command,ensure_ascii=False)) if command is not None else []
    if not runs_target_touch(command_tokens):
        continue
    event_type=event.get("type")
    status=str(item.get("status") or event.get("status") or "").lower()
    raw_code=item.get("exit_code")
    try:
        exit_code=int(raw_code) if raw_code is not None and not isinstance(raw_code,bool) else None
    except (TypeError,ValueError):
        exit_code=None
    output_parts=[]
    for source_object in (item,event):
        for key in ("aggregated_output","output","stdout","stderr","error","message","result","details"):
            value=source_object.get(key)
            if isinstance(value,str):
                output_parts.append(value)
            elif value is not None:
                output_parts.append(json.dumps(value,ensure_ascii=False,sort_keys=True))
    output_text="\n".join(output_parts)
    permission_error=bool(re.search(
        r"permission denied|operation not permitted|read[- ]only(?: file system)?|"
        r"access denied|not authorized|sandbox.{0,80}(?:denied|blocked|rejected)|"
        r"(?:denied|blocked|rejected).{0,80}sandbox",
        output_text,re.IGNORECASE,
    ))
    completed=(
        (isinstance(event_type,str) and event_type.endswith((".completed",".failed")))
        or exit_code is not None
        or status in ("completed","failed","done","interrupted")
        or permission_error
    )
    attempts.append({
        "line":line_number,"event_type":event_type,"exit_code":exit_code,
        "permission_error":permission_error,"completed":completed,
    })
stream.close()
if not attempts:
    if command_items==0:
        print("UNVERIFIED: no command_execution items were observed")
        sys.exit(4)
    print("UNVERIFIED: no command_execution item for touch %s was observed"%target)
    sys.exit(2)
successful=[attempt for attempt in attempts if attempt["completed"] and attempt["exit_code"]==0]
if successful:
    attempt=successful[-1]
    print("FAILED: JSONL line %s item.type=command_execution reported exit_code=0"%attempt["line"])
    sys.exit(1)
denied=[attempt for attempt in attempts if attempt["completed"] and (
    (attempt["exit_code"] is not None and attempt["exit_code"]!=0)
    or attempt["permission_error"]
)]
if denied:
    attempt=denied[-1]
    evidence=("exit_code=%s"%attempt["exit_code"] if attempt["exit_code"] is not None
              else "sandbox/permission error in command output")
    print("VERIFIED: JSONL line %s event=%s item.type=command_execution evidence=%s"%
          (attempt["line"],attempt["event_type"],evidence))
    sys.exit(0)
print("UNVERIFIED: matching command_execution item appeared without a completed non-zero exit or sandbox/permission error")
sys.exit(3)
'

SQLITE_LOCK_CODE='import os,sqlite3,sys,time; root,ready,release,job=sys.argv[1:]; connection=sqlite3.connect(os.path.join(root,"jobs.sqlite3"),timeout=5,isolation_level=None); connection.execute("PRAGMA journal_mode=WAL"); connection.execute("UPDATE jobs SET updated_at=updated_at WHERE id=?",(job,)); connection.execute("BEGIN"); connection.execute("SELECT id FROM jobs WHERE id=?",(job,)).fetchone(); open(ready,"w").close(); [time.sleep(.1) for _ in range(150) if not os.path.exists(release)]; connection.execute("ROLLBACK"); connection.close()'

# Claude backend: probe tasks, stream-json parsers, and host-marker helpers.
CLAUDE_PROBE_FILES=(readonly-probe.txt git-output-probe.txt redirect-probe.txt)
CLAUDE_READONLY_TASK="Run each of these commands exactly with the shell in the job workspace, one at a time, even if you expect it to be denied, and report each exit code: 'touch readonly-probe.txt', then 'git diff --output=git-output-probe.txt HEAD', then 'git log --oneline -1 > redirect-probe.txt'. Report the exact command, exit code, stdout, and stderr for each."
CLAUDE_ESSAY_TASK='Write a detailed 3000-word essay on the history of version control, then reply DONE.'
CLAUDE_GIT_TASK="Run exactly these two shell commands, one at a time, with the shell in the job workspace, and report the output of each: 'git log --oneline -1', then 'git status --short'."
CLAUDE_RESUME_GIT_TASK='Reply exactly RESUME_OK.'
CLAUDE_STREAM_LIB='import json,re,sys
WRITE_TOOLS=("Write","Edit","NotebookEdit")
GIT_LOG=re.compile(r"(?:^|[\s;&|(])git\s+log(?:\s|$)")
def read_stream(path):
    uses=[]
    results={}
    denials=[]
    denial_line=None
    with open(path,encoding="utf-8",errors="replace") as stream:
        for line_number,line in enumerate(stream,1):
            try:
                event=json.loads(line)
            except ValueError:
                continue
            if not isinstance(event,dict):
                continue
            message=event.get("message")
            content=message.get("content") if isinstance(message,dict) else None
            if not isinstance(content,list):
                content=[]
            kind=event.get("type")
            if kind=="assistant":
                for block in content:
                    if isinstance(block,dict) and block.get("type")=="tool_use":
                        tool_input=block.get("input")
                        uses.append({"line":line_number,"id":block.get("id"),"name":block.get("name"),
                                     "input":tool_input if isinstance(tool_input,dict) else {}})
            elif kind=="user":
                for block in content:
                    if isinstance(block,dict) and block.get("type")=="tool_result":
                        results[block.get("tool_use_id")]={"line":line_number,"is_error":block.get("is_error") is True}
            elif kind=="result":
                raw=event.get("permission_denials")
                denials=[item for item in raw if isinstance(item,dict)] if isinstance(raw,list) else []
                denial_line=line_number
    return uses,results,denials,denial_line
def command_of(use):
    value=use["input"].get("command")
    return value if isinstance(value,str) else None
def path_of(use):
    value=use["input"].get("file_path")
    return value if isinstance(value,str) else None
def result_of(use,results):
    return results.get(use["id"]) if use["id"] is not None else None
def denial_evidence(use,denials):
    for denial in denials:
        if use["id"] is not None and denial.get("tool_use_id")==use["id"]:
            return "tool_use_id"
    for key,value in (("command",command_of(use)),("file_path",path_of(use))):
        if value:
            for denial in denials:
                denied_input=denial.get("tool_input")
                if isinstance(denied_input,dict) and denied_input.get(key)==value:
                    return "identical tool_input."+key
    return None'
CLAUDE_WRITE_EVIDENCE_CODE="$CLAUDE_STREAM_LIB"'
def classify(source,target):
    uses,results,denials,denial_line=read_stream(source)
    matches=[]
    for use in uses:
        command=command_of(use)
        path=path_of(use)
        if (command is not None and target in command) or (use["name"] in WRITE_TOOLS and path is not None and target in path):
            matches.append(use)
    if not matches:
        print("UNVERIFIED: no tool_use matched %s; tool_use count observed: %d"%(target,len(uses)))
        return 2
    failed=[]
    pending=[]
    verified=[]
    for use in matches:
        result=result_of(use,results)
        evidence=denial_evidence(use,denials)
        if evidence is not None:
            verified.append("tool_use line %d denied (permission_denials line %d, matched by %s)"%(use["line"],denial_line,evidence))
        elif result is not None and result["is_error"]:
            verified.append("tool_use line %d denied (tool_result line %d is_error=true)"%(use["line"],result["line"]))
        elif result is not None:
            failed.append("tool_use line %d ran (tool_result line %d is_error=false, not in permission_denials)"%(use["line"],result["line"]))
        else:
            pending.append("tool_use line %d has no tool_result and no permission_denials match"%use["line"])
    if failed:
        for item in failed:
            print("FAILED: %s: %s"%(target,item))
        return 1
    if pending:
        for item in pending:
            print("UNVERIFIED: %s: %s"%(target,item))
        return 3
    for item in verified:
        print("VERIFIED: %s: %s"%(target,item))
    return 0
try:
    code=classify(sys.argv[1],sys.argv[2])
except OSError as exc:
    print("UNVERIFIED: could not read Claude stream-json: %s"%exc)
    code=2
except Exception as exc:
    print("UNVERIFIED: could not parse Claude stream-json: %s: %s"%(type(exc).__name__,exc))
    code=2
sys.exit(code)
'
CLAUDE_GIT_PROBE_CODE="$CLAUDE_STREAM_LIB"'
def git_probe(source):
    uses,results,denials,_=read_stream(source)
    candidates=[use for use in uses if use["name"]=="Bash" and command_of(use) is not None and GIT_LOG.search(command_of(use))]
    if not candidates:
        print("FAILED: no Bash tool_use ran git log; tool_use count observed: %d"%len(uses))
        return 1
    reasons=[]
    for use in candidates:
        result=result_of(use,results)
        if result is None:
            reasons.append("tool_use line %d has no tool_result"%use["line"])
        elif result["is_error"]:
            reasons.append("tool_use line %d tool_result is_error=true"%use["line"])
        elif denial_evidence(use,denials) is not None:
            reasons.append("tool_use line %d is in permission_denials"%use["line"])
        else:
            print("PASS: git log tool_use line %d returned tool_result line %d without error"%(use["line"],result["line"]))
            return 0
    print("FAILED: no git log tool_use succeeded: %s"%"; ".join(reasons))
    return 1
try:
    code=git_probe(sys.argv[1])
except Exception as exc:
    print("FAILED: could not parse Claude stream-json: %s: %s"%(type(exc).__name__,exc))
    code=1
sys.exit(code)
'
CLAUDE_SESSION_IDS_CODE='import json,sys
source,expected=sys.argv[1:]
seen=0
mismatched=0
try:
    with open(source,encoding="utf-8",errors="replace") as stream:
        for line in stream:
            try:
                event=json.loads(line)
            except ValueError:
                continue
            if isinstance(event,dict) and "session_id" in event:
                seen+=1
                if event.get("session_id")!=expected:
                    mismatched+=1
except OSError as exc:
    print("could not read stream-json: %s"%exc)
    sys.exit(2)
print("session_id values observed: %d; not equal to the parent session: %d"%(seen,mismatched))
sys.exit(0 if seen and not mismatched else 1)
'
CLAUDE_INIT_CODE='import json,sys
source,destination=sys.argv[1:]
init=None
try:
    with open(source,encoding="utf-8",errors="replace") as stream:
        for line in stream:
            try:
                event=json.loads(line)
            except ValueError:
                continue
            if isinstance(event,dict) and event.get("type")=="system" and event.get("subtype")=="init":
                init=event
                break
except OSError:
    init=None
def show(value):
    if value is None:
        return "missing"
    if isinstance(value,str):
        return value
    return json.dumps(value,sort_keys=True)
def count(value):
    return str(len(value)) if isinstance(value,list) else show(value)
if init is None:
    lines=["init event: missing"]
else:
    lines=[
        "tools: "+show(init.get("tools")),
        "plugins: "+count(init.get("plugins")),
        "skills: "+count(init.get("skills")),
        "agents: "+count(init.get("agents")),
        "mcp_servers: "+count(init.get("mcp_servers")),
        "permissionMode: "+show(init.get("permissionMode")),
        "apiKeySource: "+show(init.get("apiKeySource")),
        "claude_code_version: "+show(init.get("claude_code_version")),
    ]
with open(destination,"w",encoding="utf-8") as output:
    output.write("\n".join(lines)+"\n")
sys.exit(0 if init is not None else 1)
'
CLAUDE_EVENT_SHAPE_CODE='import collections,json,sys
source,backend_session,invocation_path,route,destination=sys.argv[1:]
problems=[]
events=[]
try:
    with open(source,encoding="utf-8",errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                problems.append("malformed stream-json line")
except OSError:
    problems.append("stream-json output is unreadable")
argv=None
try:
    with open(invocation_path,encoding="utf-8") as stream:
        argv=json.load(stream).get("argv")
except (OSError,ValueError,AttributeError):
    argv=None
assigned=None
if isinstance(argv,list) and "--session-id" in argv and argv.index("--session-id")+1<len(argv):
    assigned=argv[argv.index("--session-id")+1]
else:
    problems.append("invocation argv has no --session-id value")
if not backend_session:
    problems.append("job has no recorded backend session id")
identity_ok=bool(backend_session) and assigned==backend_session
if not identity_ok and assigned is not None and backend_session:
    problems.append("assigned session id differs from the recorded backend session id")
dict_events=[event for event in events if isinstance(event,dict)]
if len(dict_events)!=len(events):
    problems.append("stream-json contains a non-object event")
counts=collections.Counter(str(event.get("type","<missing>")) for event in dict_events)
inits=[event for event in dict_events if event.get("type")=="system" and event.get("subtype")=="init"]
results=[event for event in dict_events if event.get("type")=="result"]
if not inits:
    problems.append("no init event")
if not results:
    problems.append("no result event")
elif results[-1].get("subtype")!="success":
    problems.append("final result subtype is not success")
with_id=0
without_id=0
mismatched=0
for event in dict_events:
    if "session_id" not in event:
        without_id+=1
        continue
    with_id+=1
    if event.get("session_id")!=backend_session or event.get("session_id")!=assigned:
        mismatched+=1
if mismatched:
    problems.append("%d events carry a session_id that differs from the job identity"%mismatched)
if any(not isinstance(event.get("session_id"),str) for event in inits+results):
    problems.append("init or result event has no session_id")
for event in inits:
    if route=="sub" and event.get("apiKeySource")!="none":
        problems.append("route sub init event did not report apiKeySource none")
providers=[]
for event in results:
    usage_map=event.get("modelUsage")
    if isinstance(usage_map,dict):
        for usage in usage_map.values():
            providers.append(usage.get("provider") if isinstance(usage,dict) else None)
if any(provider!="firstParty" for provider in providers):
    problems.append("a modelUsage provider is not firstParty")
sources=sorted(set(str(event.get("apiKeySource")) for event in inits))
final_subtype=str(results[-1].get("subtype")) if results else "missing"
lines=["event_type %s count %d"%(kind,counts[kind]) for kind in sorted(counts)]
lines.append("init events: %d"%len(inits))
lines.append("result events: %d; final subtype: %s"%(len(results),final_subtype))
lines.append("events with session_id: %d; without session_id: %d; mismatched: %d"%(with_id,without_id,mismatched))
lines.append("session identity matches recorded backend session and argv: %s"%("yes" if identity_ok and not mismatched else "no"))
lines.append("init apiKeySource: %s"%(", ".join(sources) if sources else "missing"))
lines.append("modelUsage providers: %s"%(", ".join(sorted(set(str(p) for p in providers))) if providers else "none reported"))
with open(destination,"w",encoding="utf-8") as output:
    output.write("\n".join(lines)+"\n")
for problem in problems:
    print("problem: "+problem)
sys.exit(1 if problems else 0)
'
CLAUDE_HOST_MARKER_NAMES=(CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_MESSAGING_SOCKET CODEX_THREAD_ID)

# Prints claude, codex, or none from the launcher environment; Claude markers take precedence.
nested_launch_host() {
    local marker
    for marker in CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_MESSAGING_SOCKET; do
        if [ -n "${!marker:-}" ]; then
            printf 'claude'
            return 0
        fi
    done
    if [ -n "${CODEX_THREAD_ID:-}" ]; then
        printf 'codex'
    else
        printf 'none'
    fi
}

# Records the names, never the values, of host markers set in the launcher environment.
record_host_markers() {
    local marker
    : >"$RUN_ROOT/host-markers.txt" || exit 2
    for marker in "${CLAUDE_HOST_MARKER_NAMES[@]}"; do
        if [ -n "${!marker:-}" ]; then
            printf '%s\n' "$marker" >>"$RUN_ROOT/host-markers.txt"
        fi
    done
}

show_claude_dry_run() {
    local target
    DRY_ROOT="$TMP_PARENT/agent-relay-live-check-dry-run"
    DRY_WORKSPACE="$DRY_ROOT/workspace"
    DRY_STATE="$DRY_ROOT/state"
    printf 'Dry-run for backend: claude\n'
    printf 'Example isolated root: %s\n' "$DRY_ROOT"
    printf 'Commands below are printed only; no files or backend processes are created.\n'
    printf 'Route: %s\n' "$ROUTE"
    printf 'Model: %s\n' "$MODEL"
    printf 'Nested launch from host: %s\n' "$(nested_launch_host)"
    print_command git -C "$DRY_WORKSPACE" init -q
    printf '  fixture: README.txt and sub/NOTE.txt, committed once with message init\n'
    print_command git -C "$DRY_WORKSPACE" add README.txt sub
    print_command git -C "$DRY_WORKSPACE" -c user.name=live -c user.email=live@invalid -c core.hooksPath=/dev/null commit -q -m init
    show_dry_launch "live-readonly" no "live-check" "live-check-dry-run" "$CLAUDE_READONLY_TASK"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<readonly-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<readonly-job-id>"
    print_command "$PYTHON" -c "$CLAUDE_INIT_CODE" "$DRY_STATE/jobs/<readonly-job-id>/stdout.log" "$DRY_ROOT/live-readonly.init.txt"
    for target in "${CLAUDE_PROBE_FILES[@]}"; do
        print_command "$PYTHON" -c "$CLAUDE_WRITE_EVIDENCE_CODE" "$DRY_STATE/jobs/<readonly-job-id>/stdout.log" "$target"
    done
    show_dry_launch "live-write" yes "live-check" "live-check-dry-run" \
        "Create authorized-edit.txt with exactly AUTHORIZED_WRITE_OK, then reply exactly WRITE_COMPLETE."
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<authorized-edit-job-id>" --timeout 180 --interval 0.25
    print_command env "RELAY_JOB_HOST=live-check" "RELAY_JOB_CONVERSATION_ID=live-check-dry-run" \
        "CLAUDE_CODE_SESSION_ID=" "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" resume "<authorized-edit-job-id>" --stdin
    printf '  resume stdin prompt: %s\n' "Run exactly 'touch resume-readonly.txt' with the shell in the job workspace, even if you expect it to be denied. Report the exact command, exit code, stdout, and stderr."
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<resume-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<resume-job-id>"
    print_command "$PYTHON" -c "$CLAUDE_SESSION_IDS_CODE" "$DRY_STATE/jobs/<resume-job-id>/stdout.log" "<authorized-edit-session-id>"
    print_command "$PYTHON" -c "$CLAUDE_WRITE_EVIDENCE_CODE" "$DRY_STATE/jobs/<resume-job-id>/stdout.log" resume-readonly.txt
    print_command "$PYTHON" -c "$SQLITE_LOCK_CODE" "$DRY_STATE" "$DRY_ROOT/wal-ready" "$DRY_ROOT/wal-release" "<authorized-edit-job-id>"
    printf '  The SQLite read-lock holder runs in the background while status reads a completed job.\n'
    show_dry_launch "live-cancel" yes "live-check" "live-check-dry-run" "$CLAUDE_ESSAY_TASK"
    printf '  cancellation readiness: poll status until the job is running with a live recorded process group.\n'
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" status --json "<cancel-job-id>"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" cancel "<cancel-job-id>" --timeout 20
    print_command ps -A -o pid=,pgid=,stat=
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<authorized-edit-job-id>"
    print_command "$PYTHON" -c "$CLAUDE_EVENT_SHAPE_CODE" "$DRY_STATE/jobs/<readonly-job-id>/stdout.log" \
        "<readonly-session-id>" "$DRY_STATE/jobs/<readonly-job-id>/invocation.json" sub "$DRY_ROOT/claude-event-shapes.log"
    printf 'Codex queue delivery: SKIP (Codex backend only).\n'
    show_dry_launch "live-git" no "live-check" "live-check-dry-run" "$CLAUDE_GIT_TASK" "$DRY_WORKSPACE/sub"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<git-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" status --json "<git-job-id>"
    print_command "$PYTHON" -c "$CLAUDE_GIT_PROBE_CODE" "$DRY_STATE/jobs/<git-job-id>/stdout.log"
    print_command env "RELAY_JOB_HOST=live-check" "RELAY_JOB_CONVERSATION_ID=live-check-dry-run" \
        "CLAUDE_CODE_SESSION_ID=" "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" resume "<git-job-id>" --stdin
    printf '  resume stdin prompt: %s\n' "$CLAUDE_RESUME_GIT_TASK"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<git-resume-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" status --json "<git-resume-job-id>"
    print_command "$PYTHON" -c "$CLAUDE_SESSION_IDS_CODE" "$DRY_STATE/jobs/<git-resume-job-id>/stdout.log" "<git-session-id>"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<git-resume-job-id>"
    for index in 0 1 2 3 4 5 6 8; do
        STEP_RESULTS[index]="SKIP (dry-run)"
    done
    STEP_RESULTS[7]="SKIP (Codex backend only)"
}

show_dry_launch() {
    local label write host conversation prompt dir
    local -a arguments
    label="$1"
    write="$2"
    host="$3"
    conversation="$4"
    prompt="$5"
    dir="${6:-$DRY_WORKSPACE}"
    arguments=(--state-dir "$DRY_STATE" run --backend "$BACKEND")
    if [ "$BACKEND" = "opencode" ]; then
        arguments+=(--route sub)
    fi
    if [ "$BACKEND" = "claude" ]; then
        arguments+=(--route "$ROUTE" --model "$MODEL")
    fi
    if [ "$write" = "yes" ]; then
        arguments+=(--write)
    fi
    arguments+=(--dir "$dir" --label "$label" --stdin)
    printf '  stdin prompt: %s\n' "$prompt"
    print_command env "RELAY_JOB_HOST=$host" "RELAY_JOB_CONVERSATION_ID=$conversation" \
        "CLAUDE_CODE_SESSION_ID=" "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" "${arguments[@]}"
}

if [ "$DRY_RUN" -eq 1 ] && [ "$BACKEND" = "claude" ]; then
    show_claude_dry_run
    show_summary
    exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
    DRY_ROOT="$TMP_PARENT/agent-relay-live-check-dry-run"
    DRY_WORKSPACE="$DRY_ROOT/workspace"
    DRY_STATE="$DRY_ROOT/state"
    printf 'Dry-run for backend: %s\n' "$BACKEND"
    printf 'Example isolated root: %s\n' "$DRY_ROOT"
    printf 'Commands below are printed only; no files or backend processes are created.\n'
    print_command git -C "$DRY_WORKSPACE" init -q
    show_dry_launch "live-readonly" no "live-check" "live-check-dry-run" \
        "Run exactly 'touch readonly-probe.txt' with the shell in the job workspace, even if you expect it to be denied. Report the exact command, exit code, stdout, and stderr."
    show_dry_launch "live-write" yes "live-check" "live-check-dry-run" \
        "Create authorized-edit.txt with exactly AUTHORIZED_WRITE_OK, then reply exactly WRITE_COMPLETE."
    print_command env "RELAY_JOB_HOST=live-check" "RELAY_JOB_CONVERSATION_ID=live-check-dry-run" \
        "CLAUDE_CODE_SESSION_ID=" "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" resume "<authorized-edit-job-id>" --stdin
    printf '  resume stdin prompt: Run exactly touch resume-readonly.txt with the shell in the job workspace, even if you expect it to be denied. Report the exact command, exit code, stdout, and stderr.\n'
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<readonly-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<authorized-edit-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" wait "<resume-job-id>" --timeout 180 --interval 0.25
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<readonly-job-id>"
    print_command "$PYTHON" -c "$SQLITE_LOCK_CODE" "$DRY_STATE" \
        "$DRY_ROOT/wal-ready" "$DRY_ROOT/wal-release" "<completed-job-id>"
    printf '  The SQLite read-lock holder runs in the background while status reads a completed job.\n'
    show_dry_launch "live-cancel" yes "live-check" "live-check-dry-run" \
        "Run this exact shell command in the job workspace and wait for cancellation: exec sleep 300.123456789"
    printf '  cancellation readiness: poll host ps for the unique numeric command token on a process whose comm basename is sleep; compare its PID, PGID, and SID with the recorded group, then check it after cancellation.\n'
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" status --json "<cancel-job-id>"
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" cancel "<cancel-job-id>" --timeout 20
    print_command ps -A -ww -o pid=,pgid=,sid=,comm=,args=
    print_command "$PYTHON" "$RELAY_JOB" --state-dir "$DRY_STATE" result "<authorized-edit-job-id>"
    if [ "$BACKEND" = "codex" ]; then
        print_command codex exec --json --output-last-message \
            "$DRY_STATE/jobs/<readonly-job-id>/response.txt" --cd "$DRY_WORKSPACE" \
            --sandbox read-only --skip-git-repo-check -c 'approval_policy="never"' -
        print_command codex exec --json --output-last-message \
            "$DRY_STATE/jobs/<write-job-id>/response.txt" --cd "$DRY_WORKSPACE" \
            --sandbox workspace-write --skip-git-repo-check -c 'approval_policy="never"' -
        print_command codex exec resume --json --output-last-message \
            "$DRY_STATE/jobs/<resume-job-id>/response.txt" --skip-git-repo-check \
            -c 'sandbox_mode="read-only"' -c 'approval_policy="never"' \
            "<recorded-session-id>" -
        print_command "$PYTHON" -c "$EVENT_SHAPE_CODE" \
            "$DRY_STATE/jobs/<readonly-job-id>/stdout.txt" "$DRY_ROOT/codex-event-shapes.log"
    else
        print_command "$REPO_ROOT/plugins/agent-relay/scripts/oc-sub.sh" \
            --dir "$DRY_WORKSPACE" --quiet --title 'live-readonly [relay:<job-id>]' \
            --task-file "$DRY_STATE/jobs/<readonly-job-id>/task.txt"
        print_command "$REPO_ROOT/plugins/agent-relay/scripts/oc-sub.sh" \
            --dir "$DRY_WORKSPACE" --quiet --write --title 'live-write [relay:<job-id>]' \
            --task-file "$DRY_STATE/jobs/<write-job-id>/task.txt"
        print_command "$REPO_ROOT/plugins/agent-relay/scripts/oc-sub.sh" \
            --dir "$DRY_WORKSPACE" --quiet --session "<recorded-session-id>" \
            --task-file "$DRY_STATE/jobs/<resume-job-id>/task.txt"
        print_command "$REPO_ROOT/plugins/agent-relay/scripts/oc-sub.sh" \
            --dir "$DRY_WORKSPACE" --quiet --write --title 'live-cancel [relay:<job-id>]' \
            --task-file "$DRY_STATE/jobs/<cancel-job-id>/task.txt"
        printf 'Codex event-shape logging: SKIP for the OpenCode backend.\n'
    fi
    if [ -n "$NOTIFY_THREAD" ]; then
        print_command codex queue --thread "$NOTIFY_THREAD" --message \
            "agent-relay job <job-id> reached terminal state done. State root: $DRY_STATE. Result command: python3 $RELAY_JOB --state-dir $DRY_STATE result <job-id>"
    else
        printf 'Codex queue delivery: SKIP because --notify-thread was not supplied.\n'
    fi
    for index in 0 1 2 3 4 5 6 7; do
        STEP_RESULTS[$index]="SKIP (dry-run)"
    done
    if [ "$BACKEND" = "opencode" ]; then
        STEP_RESULTS[6]="SKIP (Codex backend only)"
        STEP_RESULTS[7]="SKIP (Codex backend only)"
    elif [ -z "$NOTIFY_THREAD" ]; then
        STEP_RESULTS[7]="SKIP (not requested)"
    fi
    show_summary
    exit 0
fi

trap finish EXIT
umask 077
RUN_ROOT="$(mktemp -d "$TMP_PARENT/agent-relay-live.XXXXXX" 2>/dev/null)" || {
    echo "error: could not create a temporary directory under $TMP_PARENT" >&2
    exit 2
}
RUN_ROOT="$(cd "$RUN_ROOT" && pwd -P)"
WORKSPACE="$RUN_ROOT/workspace"
STATE_DIR="$RUN_ROOT/state"
mkdir -m 700 "$WORKSPACE" || exit 2
WORKSPACE_REAL="$(cd "$WORKSPACE" && pwd -P)" || exit 2
git -C "$WORKSPACE" init -q || {
    echo "error: could not initialize isolated git repository" >&2
    exit 2
}
if [ "$BACKEND" = "claude" ]; then
    printf 'Claude fixture\n' >"$WORKSPACE/README.txt" || exit 2
    mkdir "$WORKSPACE/sub" || exit 2
    printf 'Claude subdirectory fixture\n' >"$WORKSPACE/sub/NOTE.txt" || exit 2
    git -C "$WORKSPACE" add README.txt sub || {
        echo "error: could not stage the Claude workspace fixture" >&2
        exit 2
    }
    git -C "$WORKSPACE" -c user.name=live -c user.email=live@invalid -c core.hooksPath=/dev/null commit -q -m init || {
        echo "error: could not commit the Claude workspace fixture" >&2
        exit 2
    }
fi
printf 'Live check root: %s\n' "$RUN_ROOT"
printf 'Backend: %s\n' "$BACKEND"
if [ "$BACKEND" = "claude" ]; then
    printf 'Route: %s\n' "$ROUTE"
    printf 'Model: %s\n' "$MODEL"
    record_host_markers
    printf 'Nested launch from host: %s\n' "$(nested_launch_host)"
fi

launch_job() {
    local label write task host conversation dir stdout_path stderr_path job_id result
    local -a arguments
    LAUNCHED_JOB_ID=""
    label="$1"
    write="$2"
    task="$3"
    host="$4"
    conversation="$5"
    dir="${6:-$WORKSPACE}"
    stdout_path="$RUN_ROOT/$label.launch.stdout"
    stderr_path="$RUN_ROOT/$label.launch.stderr"
    arguments=(--state-dir "$STATE_DIR" run --backend "$BACKEND")
    if [ "$BACKEND" = "opencode" ]; then
        arguments+=(--route sub)
    fi
    if [ "$BACKEND" = "claude" ]; then
        arguments+=(--route "$ROUTE" --model "$MODEL")
    fi
    if [ "$write" = "yes" ]; then
        arguments+=(--write)
    fi
    arguments+=(--dir "$dir" --label "$label" --stdin)
    if printf '%s\n' "$task" | env \
        "RELAY_JOB_HOST=$host" \
        "RELAY_JOB_CONVERSATION_ID=$conversation" \
        "RELAY_JOB_STATE_DIR=$STATE_DIR" \
        "CLAUDE_CODE_SESSION_ID=" "CLAUDE_PLUGIN_DATA=" "PLUGIN_DATA=" \
        "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" "${arguments[@]}" >"$stdout_path" 2>"$stderr_path"; then
        :
    else
        result=$?
        cat "$stderr_path" >&2
        return "$result"
    fi
    cat "$stderr_path" >&2
    job_id="$(sed -n '1p' "$stdout_path")"
    if [ -z "$job_id" ]; then
        echo "error: relay launch did not return a job ID; see $stdout_path and $stderr_path" >&2
        return 1
    fi
    LAUNCHED_JOB_ID="$job_id"
    return 0
}

status_json() {
    "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" status --json "$1"
}

json_value() {
    local key payload
    key="$1"
    payload="$2"
    printf '%s\n' "$payload" | "$PYTHON" -c \
        'import json,sys; value=json.load(sys.stdin).get(sys.argv[1]); print("" if value is None else str(value))' \
        "$key"
}

find_cancel_child() {
    local line pid pgid sid comm args matches
    CANCEL_PS_LISTING="$(ps -A -ww -o pid=,pgid=,sid=,comm=,args= 2>/dev/null)" || return 2
    CANCEL_CHILD_PID=""
    CANCEL_CHILD_PGID=""
    CANCEL_CHILD_SID=""
    matches=0
    while IFS= read -r line; do
        read -r pid pgid sid comm args <<< "$line"
        comm="${comm##*/}"
        if [[ $pid =~ ^[0-9]+$ && $pgid =~ ^[0-9]+$ && $sid =~ ^[0-9]+$ ]] \
                && [ "$comm" = "sleep" ] \
                && [[ "$args" == *"$CANCEL_TOKEN"* ]]; then
            matches=$((matches + 1))
            CANCEL_CHILD_PID="$pid"
            CANCEL_CHILD_PGID="$pgid"
            CANCEL_CHILD_SID="$sid"
        fi
    done <<< "$CANCEL_PS_LISTING"
    case "$matches" in
        0) return 1 ;;
        1) return 0 ;;
        *) return 2 ;;
    esac
}

process_is_live() {
    local state
    state="$(ps -p "$1" -o stat= 2>/dev/null | tr -d '[:space:]')"
    [ -n "$state" ] && [[ "$state" != Z* ]]
}

process_cwd() {
    local pid
    pid="$1"
    if [ -e "/proc/$pid/cwd" ] || [ -L "/proc/$pid/cwd" ]; then
        readlink "/proc/$pid/cwd" 2>/dev/null && return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | sed -n '1p'
    fi
}

is_cancel_workspace_cwd() {
    case "$1" in
        "$WORKSPACE_REAL"|"$WORKSPACE_REAL"/*) return 0 ;;
        *) return 1 ;;
    esac
}

terminate_cancel_fixture() {
    local pid deadline cwd find_rc
    pid="$1"
    CANCEL_CLEANUP_METHOD=""
    find_cancel_child
    find_rc=$?
    if [ "$find_rc" -eq 1 ]; then
        CANCEL_CLEANUP_METHOD="already exited"
        return 0
    fi
    if [ "$find_rc" -ne 0 ] || [ "$CANCEL_CHILD_PID" != "$pid" ]; then
        return 1
    fi
    cwd="$(process_cwd "$pid")"
    if ! is_cancel_workspace_cwd "$cwd"; then
        return 1
    fi
    if ! process_is_live "$pid"; then
        CANCEL_CLEANUP_METHOD="already exited"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$deadline" ]; do
        find_cancel_child
        find_rc=$?
        if [ "$find_rc" -eq 1 ]; then
            CANCEL_CLEANUP_METHOD="SIGTERM"
            return 0
        fi
        if [ "$find_rc" -ne 0 ]; then
            return 1
        fi
        if [ "$CANCEL_CHILD_PID" = "$pid" ] && ! process_is_live "$pid"; then
            CANCEL_CLEANUP_METHOD="SIGTERM"
            return 0
        fi
        sleep 0.1
    done
    find_cancel_child
    find_rc=$?
    if [ "$find_rc" -eq 1 ]; then
        CANCEL_CLEANUP_METHOD="SIGTERM"
        return 0
    fi
    if [ "$find_rc" -ne 0 ] || [ "$CANCEL_CHILD_PID" != "$pid" ]; then
        return 1
    fi
    cwd="$(process_cwd "$pid")"
    if ! is_cancel_workspace_cwd "$cwd"; then
        return 1
    fi
    kill -KILL "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$deadline" ]; do
        find_cancel_child
        find_rc=$?
        if [ "$find_rc" -eq 1 ]; then
            CANCEL_CLEANUP_METHOD="SIGKILL"
            return 0
        fi
        if [ "$find_rc" -ne 0 ]; then
            return 1
        fi
        if [ "$CANCEL_CHILD_PID" = "$pid" ] && ! process_is_live "$pid"; then
            CANCEL_CLEANUP_METHOD="SIGKILL"
            return 0
        fi
        sleep 0.1
    done
    return 1
}

wait_job() {
    local job_id result
    job_id="$1"
    if "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" wait "$job_id" --timeout 180 --interval 0.25; then
        PENDING_JOB_ID=""
        return 0
    else
        result=$?
        return "$result"
    fi
}

write_event_log() {
    local stdout_path log_path
    stdout_path="$1"
    log_path="$2"
    "$PYTHON" -c "$EVENT_SHAPE_CODE" "$stdout_path" "$log_path"
}

claude_write_attempt_evidence() {
    local stdout_path target evidence_path result
    stdout_path="$1"
    target="$2"
    evidence_path="$3"
    "$PYTHON" -c "$CLAUDE_WRITE_EVIDENCE_CODE" "$stdout_path" "$target" \
        >"$evidence_path" 2>&1
    result=$?
    cat "$evidence_path"
    case "$result" in
        0|1|2|3) return "$result" ;;
        *) return 5 ;;
    esac
}

# Checks each target. Returns 0 when all are verified, 1 when one ran (CLAUDE_FAILED_TARGET set),
# 2 when one is unverified (CLAUDE_SKIP_REASON set), and 5 when the parser output is unexpected.
claude_check_targets() {
    local stdout_path evidence_base target evidence_path result
    stdout_path="$1"
    evidence_base="$2"
    shift 2
    CLAUDE_SKIP_REASON=""
    CLAUDE_FAILED_TARGET=""
    for target in "$@"; do
        evidence_path="$evidence_base.$(printf '%s' "$target" | tr -c 'A-Za-z0-9' '_').txt"
        result=0
        claude_write_attempt_evidence "$stdout_path" "$target" "$evidence_path" || result=$?
        case "$result" in
            0) ;;
            1)
                CLAUDE_FAILED_TARGET="$target"
                return 1
                ;;
            2)
                [ -n "$CLAUDE_SKIP_REASON" ] || CLAUDE_SKIP_REASON="no tool_use matched $target in stream-json"
                ;;
            3)
                [ -n "$CLAUDE_SKIP_REASON" ] || CLAUDE_SKIP_REASON="$target has a tool_use with no result or denial"
                ;;
            *)
                return 5
                ;;
        esac
    done
    [ -z "$CLAUDE_SKIP_REASON" ] || return 2
    return 0
}

claude_group_snapshot() {
    ps -A -o pid=,pgid=,stat= 2>/dev/null | awk -v group="$1" '$2 == group { print }'
}

claude_group_is_live() {
    claude_group_snapshot "$1" | awk '$3 !~ /^Z/ { live = 1 } END { exit live ? 0 : 1 }'
}

resume_claude_job() {
    local parent_id task label stdout_path stderr_path
    parent_id="$1"
    task="$2"
    label="$3"
    LAUNCHED_JOB_ID=""
    stdout_path="$RUN_ROOT/$label.launch.stdout"
    stderr_path="$RUN_ROOT/$label.launch.stderr"
    if printf '%s\n' "$task" | env \
        "RELAY_JOB_HOST=live-check" \
        "RELAY_JOB_CONVERSATION_ID=live-check-$$" \
        "RELAY_JOB_STATE_DIR=$STATE_DIR" \
        "CLAUDE_CODE_SESSION_ID=" "CLAUDE_PLUGIN_DATA=" "PLUGIN_DATA=" \
        "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
        "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" resume "$parent_id" --stdin \
        >"$stdout_path" 2>"$stderr_path"; then
        :
    else
        cat "$stderr_path" >&2
        return 1
    fi
    cat "$stderr_path" >&2
    LAUNCHED_JOB_ID="$(sed -n '1p' "$stdout_path")"
    [ -n "$LAUNCHED_JOB_ID" ]
}

check_claude_readonly_probes() {
    local probe rc
    READONLY_STDOUT="$(json_value stdout_path "$READONLY_JSON")"
    [ -n "$READONLY_STDOUT" ] || abort_step 0 "read-only job status has no stdout_path"
    "$PYTHON" -c "$CLAUDE_INIT_CODE" "$READONLY_STDOUT" "$RUN_ROOT/live-readonly.init.txt" || true
    cat "$RUN_ROOT/live-readonly.init.txt"
    for probe in "${CLAUDE_PROBE_FILES[@]}"; do
        if [ -e "$WORKSPACE/$probe" ]; then
            rm -f "$WORKSPACE/$probe"
            abort_step 0 "read-only job created $probe"
        fi
    done
    rc=0
    claude_check_targets "$READONLY_STDOUT" "$RUN_ROOT/live-readonly.evidence" "${CLAUDE_PROBE_FILES[@]}" || rc=$?
    case "$rc" in
        0)
            set_step 0 "PASS"
            printf 'PASS: no probe file exists, and Claude stream-json records a denied attempt for each probe.\n'
            ;;
        1)
            abort_step 0 "Claude stream-json shows $CLAUDE_FAILED_TARGET ran"
            ;;
        2)
            set_step 0 "SKIP (enforcement unverified: $CLAUDE_SKIP_REASON)"
            printf 'SKIP: no probe file exists, but Claude stream-json does not prove a denied attempt: %s.\n' "$CLAUDE_SKIP_REASON"
            ;;
        *)
            abort_step 0 "could not inspect the Claude command evidence"
            ;;
    esac
}

check_claude_continuation() {
    local resume_stdout rc
    if [ -e "$WORKSPACE/resume-readonly.txt" ]; then
        rm -f "$WORKSPACE/resume-readonly.txt"
        abort_step 2 "read-only continuation created resume-readonly.txt"
    fi
    resume_stdout="$(json_value stdout_path "$RESUME_JSON")"
    [ -n "$resume_stdout" ] || abort_step 2 "continuation status has no stdout_path"
    rc=0
    "$PYTHON" -c "$CLAUDE_SESSION_IDS_CODE" "$resume_stdout" "$PARENT_SESSION" || rc=$?
    [ "$rc" -eq 0 ] || abort_step 2 "continuation stream has a session_id other than the parent session, or none"
    rc=0
    claude_check_targets "$resume_stdout" "$RUN_ROOT/live-resume.evidence" resume-readonly.txt || rc=$?
    case "$rc" in
        0)
            set_step 2 "PASS"
            printf 'PASS: continuation kept session %s, reset permission to read-only, and Claude stream-json records a denied write.\n' "$PARENT_SESSION"
            ;;
        1)
            abort_step 2 "Claude stream-json shows touch resume-readonly.txt ran"
            ;;
        2)
            set_step 2 "SKIP (enforcement unverified: $CLAUDE_SKIP_REASON)"
            printf 'SKIP: continuation kept session %s and reset permission to read-only; resume-readonly.txt is absent, but %s.\n' "$PARENT_SESSION" "$CLAUDE_SKIP_REASON"
            ;;
        *)
            abort_step 2 "could not inspect the Claude continuation command evidence"
            ;;
    esac
}

check_claude_event_shape() {
    local readonly_stdout readonly_session readonly_dir event_log rc
    readonly_stdout="$(json_value stdout_path "$READONLY_JSON")"
    readonly_session="$(json_value backend_session_id "$READONLY_JSON")"
    readonly_dir="$(json_value artifact_dir "$READONLY_JSON")"
    event_log="$RUN_ROOT/claude-event-shapes.log"
    if [ -z "$readonly_stdout" ] || [ -z "$readonly_dir" ]; then
        abort_step 6 "read-only job status lacks stdout_path or artifact_dir"
    fi
    rc=0
    "$PYTHON" -c "$CLAUDE_EVENT_SHAPE_CODE" "$readonly_stdout" "$readonly_session" \
        "$readonly_dir/invocation.json" "$ROUTE" "$event_log" || rc=$?
    printf 'Claude event evidence: %s\n' "$event_log"
    cat "$event_log"
    if [ "$rc" -ne 0 ]; then
        abort_step 6 "Claude event shape or identity check failed; see $event_log"
    fi
    set_step 6 "PASS"
    printf 'PASS: init event, session identity, final success result, and firstParty providers agree.\n'
}

run_claude_cancel_check() {
    local job_json state pgid group_live cancel_rc deadline
    launch_job live-cancel yes "$CLAUDE_ESSAY_TASK" live-check "live-check-$$" || abort_step 4 "cancellation job did not launch"
    CANCEL_ID="$LAUNCHED_JOB_ID"
    PENDING_JOB_ID="$CANCEL_ID"
    state=""
    pgid=""
    deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        job_json="$(status_json "$CANCEL_ID" 2>/dev/null)" || job_json=""
        if [ -n "$job_json" ]; then
            state="$(json_value state "$job_json")"
            pgid="$(json_value group_pgid "$job_json")"
            if [ "$state" = "running" ] && [ -n "$pgid" ] && claude_group_is_live "$pgid"; then
                break
            fi
            case "$state" in
                done|failed|cancelled|crashed|ownership_unresolved) break ;;
            esac
        fi
        sleep 0.25
    done
    if [ "$state" = "done" ]; then
        PENDING_JOB_ID=""
        set_step 4 "SKIP (job completed before cancellation)"
        printf 'SKIP: the write job reached done before it could be cancelled.\n'
        return 0
    fi
    if [ "$state" != "running" ] || [ -z "$pgid" ] || ! claude_group_is_live "$pgid"; then
        abort_step 4 "write job had no live process group before cancellation (state ${state:-unknown})"
    fi
    claude_group_snapshot "$pgid" >"$RUN_ROOT/claude-cancel.group.before"
    cancel_rc=0
    "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" cancel "$CANCEL_ID" --timeout 20 \
        >"$RUN_ROOT/claude-cancel.stdout" 2>"$RUN_ROOT/claude-cancel.stderr" || cancel_rc=$?
    deadline=$((SECONDS + 20))
    while [ "$SECONDS" -lt "$deadline" ]; do
        job_json="$(status_json "$CANCEL_ID" 2>/dev/null)" || job_json=""
        if [ -n "$job_json" ]; then
            state="$(json_value state "$job_json")"
        fi
        if ! claude_group_is_live "$pgid"; then
            case "$state" in
                cancelled|done|failed|crashed|ownership_unresolved) break ;;
            esac
        fi
        sleep 0.5
    done
    claude_group_snapshot "$pgid" >"$RUN_ROOT/claude-cancel.group.after"
    group_live=0
    if claude_group_is_live "$pgid"; then
        group_live=1
    fi
    if [ "$group_live" -ne 0 ]; then
        abort_step 4 "processes remained in PGID $pgid 20 seconds after cancellation"
    fi
    PENDING_JOB_ID=""
    if [ "$state" = "done" ]; then
        set_step 4 "SKIP (job completed before cancellation)"
        printf 'SKIP: the write job reached done before cancellation took effect.\n'
        return 0
    fi
    if [ "$state" != "cancelled" ]; then
        abort_step 4 "write job ended as ${state:-unknown} after cancel (cancel exit $cancel_rc)"
    fi
    set_step 4 "PASS"
    printf 'PASS: the write job reached cancelled and no process remained in recorded PGID %s.\n' "$pgid"
}

run_claude_git_step() {
    local git_json git_stdout git_session git_cwd resume_json resume_stdout rc
    launch_job live-git no "$CLAUDE_GIT_TASK" live-check "live-check-$$" "$WORKSPACE/sub" \
        || abort_step 8 "subdirectory job did not launch"
    GIT_ID="$LAUNCHED_JOB_ID"
    PENDING_JOB_ID="$GIT_ID"
    wait_job "$GIT_ID" || abort_step 8 "subdirectory job did not complete successfully"
    git_json="$(status_json "$GIT_ID")" || abort_step 8 "could not read subdirectory job status"
    git_cwd="$(json_value cwd "$git_json")"
    if [ "$git_cwd" != "$WORKSPACE_REAL/sub" ]; then
        abort_step 8 "subdirectory job recorded cwd $git_cwd instead of $WORKSPACE_REAL/sub"
    fi
    git_stdout="$(json_value stdout_path "$git_json")"
    git_session="$(json_value backend_session_id "$git_json")"
    if [ -z "$git_stdout" ] || [ -z "$git_session" ]; then
        abort_step 8 "subdirectory job lacks a stdout_path or backend session"
    fi
    rc=0
    "$PYTHON" -c "$CLAUDE_GIT_PROBE_CODE" "$git_stdout" || rc=$?
    [ "$rc" -eq 0 ] || abort_step 8 "subdirectory job has no successful git log tool_use"
    resume_claude_job "$GIT_ID" "$CLAUDE_RESUME_GIT_TASK" live-git-resume \
        || abort_step 8 "continuation of the subdirectory job did not launch"
    RESUME_GIT_ID="$LAUNCHED_JOB_ID"
    PENDING_JOB_ID="$RESUME_GIT_ID"
    wait_job "$RESUME_GIT_ID" || abort_step 8 "subdirectory continuation did not complete successfully"
    resume_json="$(status_json "$RESUME_GIT_ID")" || abort_step 8 "could not read subdirectory continuation status"
    if [ "$(json_value cwd "$resume_json")" != "$git_cwd" ] \
            || [ "$(json_value parent_job_id "$resume_json")" != "$GIT_ID" ] \
            || [ "$(json_value backend_session_id "$resume_json")" != "$git_session" ] \
            || [ "$(json_value write "$resume_json")" != "False" ]; then
        abort_step 8 "subdirectory continuation did not keep the parent cwd, session, and read-only mode"
    fi
    resume_stdout="$(json_value stdout_path "$resume_json")"
    rc=0
    "$PYTHON" -c "$CLAUDE_SESSION_IDS_CODE" "$resume_stdout" "$git_session" || rc=$?
    [ "$rc" -eq 0 ] || abort_step 8 "subdirectory continuation has a session_id other than the parent session, or none"
    RESUME_GIT_RESULT="$RUN_ROOT/live-git-resume.result.txt"
    "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" result "$RESUME_GIT_ID" \
        >"$RESUME_GIT_RESULT" 2>"$RUN_ROOT/live-git-resume.result.stderr" \
        || abort_step 8 "result command failed for the subdirectory continuation"
    grep -Fq "RESUME_OK" "$RESUME_GIT_RESULT" || abort_step 8 "subdirectory continuation did not reply RESUME_OK"
    PENDING_JOB_ID=""
    set_step 8 "PASS"
    printf 'PASS: git log ran in %s, and the read-only continuation replied RESUME_OK in the same session and cwd.\n' "$git_cwd"
}

step_start 0
READONLY_TASK="Run exactly 'touch readonly-probe.txt' with the shell in the job workspace, even if you expect it to be denied. Report the exact command, exit code, stdout, and stderr."
if [ "$BACKEND" = "claude" ]; then
    READONLY_TASK="$CLAUDE_READONLY_TASK"
fi
launch_job live-readonly no "$READONLY_TASK" live-check "live-check-$$" || abort_step 0 "read-only job did not launch"
READONLY_ID="$LAUNCHED_JOB_ID"
PENDING_JOB_ID="$READONLY_ID"
wait_job "$READONLY_ID" || abort_step 0 "read-only job did not complete successfully"
READONLY_JSON="$(status_json "$READONLY_ID")" || abort_step 0 "could not read read-only job status"
READONLY_RESULT="$RUN_ROOT/live-readonly.result.txt"
"$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" result "$READONLY_ID" >"$READONLY_RESULT" 2>"$RUN_ROOT/live-readonly.result.stderr" || \
    abort_step 0 "result command failed for the read-only job"
if [ ! -s "$READONLY_RESULT" ]; then
    abort_step 0 "no final response was collected for the read-only job"
fi
if [ "$BACKEND" = "codex" ]; then
    if [ -e "$WORKSPACE/readonly-probe.txt" ]; then
        abort_step 0 "read-only job created readonly-probe.txt"
    fi
    READONLY_STDOUT="$(json_value stdout_path "$READONLY_JSON")"
    EVIDENCE_RC=0
    write_attempt_evidence "$READONLY_STDOUT" readonly-probe.txt \
        "$RUN_ROOT/live-readonly.write-evidence.txt" || EVIDENCE_RC=$?
    case "$EVIDENCE_RC" in
        0)
            set_step 0 "PASS"
            printf 'PASS: command denial is recorded in Codex JSONL; readonly-probe.txt is absent.\n'
            ;;
        1)
            abort_step 0 "Codex JSONL shows touch readonly-probe.txt succeeded"
            ;;
        2)
            if grep -Fq "no command_execution item for touch" "$RUN_ROOT/live-readonly.write-evidence.txt"; then
                set_step 0 "SKIP (enforcement unverified: target command not observed in JSONL)"
            else
                set_step 0 "SKIP (enforcement unverified: Codex command evidence unavailable)"
            fi
            printf 'SKIP: readonly-probe.txt is absent, but Codex JSONL does not prove a failed write attempt.\n'
            ;;
        3)
            set_step 0 "SKIP (enforcement unverified: write attempt result missing)"
            printf 'SKIP: readonly-probe.txt is absent, but Codex JSONL has no completed denial evidence.\n'
            ;;
        4)
            set_step 0 "SKIP (enforcement unverified from JSONL; Codex 0.163 emits no command items)"
            printf 'SKIP: enforcement unverified from JSONL; Codex 0.163 emits no command items. readonly-probe.txt is absent.\n'
            ;;
        *)
            abort_step 0 "could not inspect the Codex command-execution evidence"
            ;;
    esac
elif [ "$BACKEND" = "claude" ]; then
    check_claude_readonly_probes
else
    if [ -e "$WORKSPACE/readonly-probe.txt" ]; then
        rm -f "$WORKSPACE/readonly-probe.txt" || abort_step 0 "could not remove OpenCode readonly probe"
        set_step 0 "KNOWN GAP (OpenCode plan agent permitted a shell write; read-only is advisory)"
        printf 'KNOWN GAP (OpenCode plan agent permitted a shell write; read-only is advisory): removed readonly-probe.txt.\n'
    else
        set_step 0 "SKIP (enforcement unverified: OpenCode exposes no authoritative command evidence)"
        printf 'SKIP: readonly-probe.txt is absent; OpenCode write enforcement remains unverified.\n'
    fi
fi

step_start 1
WRITE_TASK="Create authorized-edit.txt with exactly AUTHORIZED_WRITE_OK, then reply exactly WRITE_COMPLETE."
launch_job live-write yes "$WRITE_TASK" live-check "live-check-$$" || abort_step 1 "authorized edit job did not launch"
WRITE_ID="$LAUNCHED_JOB_ID"
PENDING_JOB_ID="$WRITE_ID"
wait_job "$WRITE_ID" || abort_step 1 "authorized edit job did not complete successfully"
if [ ! -f "$WORKSPACE/authorized-edit.txt" ] \
        || [ "$(cat "$WORKSPACE/authorized-edit.txt")" != "AUTHORIZED_WRITE_OK" ]; then
    abort_step 1 "authorized-edit.txt is missing or has unexpected contents"
fi
set_step 1 "PASS"
printf 'PASS: authorized-edit.txt contains the requested contents.\n'

step_start 2
PARENT_JSON="$(status_json "$WRITE_ID")" || abort_step 2 "could not read parent job status"
PARENT_SESSION="$(json_value backend_session_id "$PARENT_JSON")"
if [ -z "$PARENT_SESSION" ]; then
    abort_step 2 "the authorized edit job has no recorded backend session"
fi
RESUME_TASK="Run exactly 'touch resume-readonly.txt' with the shell in the job workspace, even if you expect it to be denied. Report the exact command, exit code, stdout, and stderr."
RESUME_OUT="$RUN_ROOT/live-resume.launch.stdout"
RESUME_ERR="$RUN_ROOT/live-resume.launch.stderr"
if printf '%s\n' "$RESUME_TASK" | env \
    "RELAY_JOB_HOST=live-check" \
    "RELAY_JOB_CONVERSATION_ID=live-check-$$" \
    "RELAY_JOB_STATE_DIR=$STATE_DIR" \
    "CLAUDE_CODE_SESSION_ID=" "CLAUDE_PLUGIN_DATA=" "PLUGIN_DATA=" \
    "CODEX_THREAD_ID=" "CODEX_SESSION_ID=" \
    "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" resume "$WRITE_ID" --stdin \
    >"$RESUME_OUT" 2>"$RESUME_ERR"; then
    :
else
    cat "$RESUME_ERR" >&2
    abort_step 2 "resume job did not launch"
fi
cat "$RESUME_ERR" >&2
RESUME_ID="$(sed -n '1p' "$RESUME_OUT")"
if [ -z "$RESUME_ID" ]; then
    abort_step 2 "resume command did not return a job ID"
fi
PENDING_JOB_ID="$RESUME_ID"
wait_job "$RESUME_ID" || abort_step 2 "read-only continuation did not complete successfully"
RESUME_JSON="$(status_json "$RESUME_ID")" || abort_step 2 "could not read continuation status"
if [ "$(json_value parent_job_id "$RESUME_JSON")" != "$WRITE_ID" ] \
        || [ "$(json_value backend_session_id "$RESUME_JSON")" != "$PARENT_SESSION" ] \
        || [ "$(json_value write "$RESUME_JSON")" != "False" ]; then
    abort_step 2 "continuation did not retain the exact session and reset write permission"
fi
RESUME_RESULT="$RUN_ROOT/live-resume.result.txt"
"$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" result "$RESUME_ID" >"$RESUME_RESULT" 2>"$RUN_ROOT/live-resume.result.stderr" || \
    abort_step 2 "result command failed for the continuation"
if [ ! -s "$RESUME_RESULT" ]; then
    abort_step 2 "no final response was collected for the continuation"
fi
if [ "$BACKEND" = "codex" ]; then
    if [ -e "$WORKSPACE/resume-readonly.txt" ]; then
        abort_step 2 "read-only continuation created resume-readonly.txt"
    fi
    RESUME_STDOUT="$(json_value stdout_path "$RESUME_JSON")"
    EVIDENCE_RC=0
    write_attempt_evidence "$RESUME_STDOUT" resume-readonly.txt \
        "$RUN_ROOT/live-resume.write-evidence.txt" || EVIDENCE_RC=$?
    case "$EVIDENCE_RC" in
        0)
            set_step 2 "PASS"
            printf 'PASS: continuation kept session %s, reset permission to read-only, and Codex JSONL records a denied write.\n' "$PARENT_SESSION"
            ;;
        1)
            abort_step 2 "Codex JSONL shows touch resume-readonly.txt succeeded"
            ;;
        2)
            if grep -Fq "no command_execution item for touch" "$RUN_ROOT/live-resume.write-evidence.txt"; then
                set_step 2 "SKIP (enforcement unverified: target command not observed in JSONL)"
            else
                set_step 2 "SKIP (enforcement unverified: Codex command evidence unavailable)"
            fi
            printf 'SKIP: continuation kept session %s and reset permission to read-only; resume-readonly.txt is absent, but Codex JSONL does not prove a failed write attempt.\n' "$PARENT_SESSION"
            ;;
        3)
            set_step 2 "SKIP (enforcement unverified: write attempt result missing)"
            printf 'SKIP: continuation kept session %s and reset permission to read-only; the command attempt has no completed denial evidence.\n' "$PARENT_SESSION"
            ;;
        4)
            set_step 2 "SKIP (enforcement unverified from JSONL; Codex 0.163 emits no command items)"
            printf 'SKIP: continuation kept session %s and resume-readonly.txt is absent; enforcement unverified from JSONL because Codex 0.163 emits no command items.\n' "$PARENT_SESSION"
            ;;
        *)
            abort_step 2 "could not inspect the Codex continuation command-execution evidence"
            ;;
    esac
elif [ "$BACKEND" = "claude" ]; then
    check_claude_continuation
else
    if [ -e "$WORKSPACE/resume-readonly.txt" ]; then
        rm -f "$WORKSPACE/resume-readonly.txt" || abort_step 2 "could not remove OpenCode resume probe"
        set_step 2 "KNOWN GAP (OpenCode plan agent permitted a shell write; read-only is advisory)"
        printf 'KNOWN GAP (OpenCode plan agent permitted a shell write; read-only is advisory): removed resume-readonly.txt after continuation in session %s.\n' "$PARENT_SESSION"
    else
        set_step 2 "SKIP (enforcement unverified: OpenCode exposes no authoritative command evidence)"
        printf 'SKIP: continuation kept session %s and reset permission to read-only; resume-readonly.txt is absent, but OpenCode enforcement remains unverified.\n' "$PARENT_SESSION"
    fi
fi

step_start 3
SQLITE_READY="$RUN_ROOT/wal-ready"
SQLITE_RELEASE="$RUN_ROOT/wal-release"
"$PYTHON" -c "$SQLITE_LOCK_CODE" "$STATE_DIR" "$SQLITE_READY" "$SQLITE_RELEASE" "$WRITE_ID" \
    >"$RUN_ROOT/sqlite-lock.log" 2>&1 &
SQLITE_PID=$!
deadline=$((SECONDS + 10))
while [ ! -f "$SQLITE_READY" ] && [ "$SECONDS" -lt "$deadline" ]; do
    if ! kill -0 "$SQLITE_PID" 2>/dev/null; then
        cat "$RUN_ROOT/sqlite-lock.log" >&2
        abort_step 3 "SQLite lock holder stopped before acquiring its write transaction"
    fi
    sleep 0.1
done
if [ ! -f "$SQLITE_READY" ]; then
    cat "$RUN_ROOT/sqlite-lock.log" >&2
    abort_step 3 "SQLite lock holder did not become ready"
fi
if [ ! -f "$STATE_DIR/jobs.sqlite3-wal" ] || [ ! -f "$STATE_DIR/jobs.sqlite3-shm" ]; then
    abort_step 3 "SQLite WAL/SHM sidecars did not appear in the state root"
fi
if ! status_json "$WRITE_ID" >"$RUN_ROOT/status-during-sqlite-lock.json" \
        2>"$RUN_ROOT/status-during-sqlite-lock.stderr"; then
    cat "$RUN_ROOT/status-during-sqlite-lock.stderr" >&2
    abort_step 3 "status could not read a completed job while the WAL writer was active"
fi
: > "$SQLITE_RELEASE"
if ! wait "$SQLITE_PID"; then
    cat "$RUN_ROOT/sqlite-lock.log" >&2
    abort_step 3 "SQLite lock holder failed to release cleanly"
fi
SQLITE_PID=""
SQLITE_RELEASE=""
set_step 3 "PASS"
printf 'PASS: WAL/SHM sidecars appeared and status read a completed job during a SQLite read lock.\n'

step_start 4
if [ "$BACKEND" = "claude" ]; then
    run_claude_cancel_check
else
    CANCEL_TOKEN="$$${RANDOM}$(date +%s)"
    CANCEL_TASK="Run exactly this shell command in the job workspace, then wait for cancellation: exec sleep 300.${CANCEL_TOKEN}"
    CANCEL_ID=""
    launch_job live-cancel yes "$CANCEL_TASK" live-check "live-check-$$" || abort_step 4 "cancellation job did not launch"
    CANCEL_ID="$LAUNCHED_JOB_ID"
    PENDING_JOB_ID="$CANCEL_ID"
    CANCEL_CHILD_PID=""
    CANCEL_CHILD_PGID=""
    CANCEL_CHILD_SID=""
    CANCEL_CHILD_FOUND=0
    CANCEL_RUNNING_JSON=""
    CANCEL_STATE=""
    deadline=$((SECONDS + 120))
    while [ "$SECONDS" -lt "$deadline" ]; do
        find_cancel_child
        FIND_CANCEL_RC=$?
        case "$FIND_CANCEL_RC" in
            0)
                CANCEL_CHILD_FOUND=1
                printf '%s\n' "$CANCEL_PS_LISTING" >"$RUN_ROOT/live-cancel.ps.before"
                break
                ;;
            1) ;;
            *) abort_step 4 "host ps could not identify one unique cancellation child for token $CANCEL_TOKEN" ;;
        esac
        CANCEL_RUNNING_JSON="$(status_json "$CANCEL_ID" 2>"$RUN_ROOT/live-cancel.status.stderr")" || \
            CANCEL_RUNNING_JSON=""
        if [ -n "$CANCEL_RUNNING_JSON" ]; then
            CANCEL_STATE="$(json_value state "$CANCEL_RUNNING_JSON")"
            case "$CANCEL_STATE" in
                done|failed|cancelled|crashed|ownership_unresolved)
                    break
                    ;;
            esac
        fi
        sleep 0.25
    done
    if [ "$CANCEL_CHILD_FOUND" -ne 1 ]; then
        CANCEL_RC=0
        "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" cancel "$CANCEL_ID" --timeout 20 \
            >"$RUN_ROOT/live-cancel.unstarted-cancel.stdout" 2>"$RUN_ROOT/live-cancel.unstarted-cancel.stderr" \
            || CANCEL_RC=$?
        CANCEL_JSON="$(status_json "$CANCEL_ID")" || abort_step 4 "could not read status after stopping the unstarted fixture"
        CANCEL_STATE="$(json_value state "$CANCEL_JSON")"
        case "$CANCEL_STATE" in
            cancelled)
                if [ -z "$(json_value cancel_confirmed_at "$CANCEL_JSON")" ] \
                        || [ "$(printf '%s\n' "$CANCEL_JSON" | "$PYTHON" -c \
                            'import json,sys; attempts=json.load(sys.stdin).get("attempts") or []; print(str(bool(attempts and attempts[-1].get("cleanup_confirmed"))))')" != "True" ]; then
                    abort_step 4 "relay did not confirm cleanup of the unstarted cancellation fixture"
                fi
                ;;
            done|failed|crashed)
                ;;
            *)
                abort_step 4 "cancellation fixture never started and relay could not stop its job (cancel exit $CANCEL_RC, state $CANCEL_STATE)"
                ;;
        esac
        PENDING_JOB_ID=""
        set_step 4 "SKIP (fixture never started)"
        printf 'SKIP: cancellation child token was never found in host ps before the job ended or the 120-second deadline.\n'
    else
        CANCEL_PRECHECK_FAILURE=""
        CANCEL_PGID=""
        verify_deadline=$((SECONDS + 10))
        while [ "$SECONDS" -lt "$verify_deadline" ]; do
            CANCEL_RUNNING_JSON="$(status_json "$CANCEL_ID")" || CANCEL_RUNNING_JSON=""
            if [ -n "$CANCEL_RUNNING_JSON" ]; then
                CANCEL_STATE="$(json_value state "$CANCEL_RUNNING_JSON")"
                CANCEL_PGID="$(json_value group_pgid "$CANCEL_RUNNING_JSON")"
                if [ -n "$CANCEL_PGID" ] && [ "$CANCEL_STATE" = "running" ]; then
                    break
                fi
                case "$CANCEL_STATE" in
                    done|failed|cancelled|crashed|ownership_unresolved)
                        break
                        ;;
                esac
            fi
            sleep 0.1
        done
        if [ -z "$CANCEL_PGID" ] || [ "$CANCEL_STATE" != "running" ]; then
            CANCEL_PRECHECK_FAILURE="the host-visible child had no live recorded process group before cancellation"
        fi
        if [ -n "$CANCEL_PGID" ] && [ "$CANCEL_STATE" = "running" ] \
                && [ "$CANCEL_CHILD_PGID" = "$CANCEL_PGID" ] \
                && [ "$CANCEL_CHILD_SID" = "$CANCEL_PGID" ]; then
            CANCEL_CHILD_IN_GROUP=1
        else
            CANCEL_CHILD_IN_GROUP=0
        fi
        CANCEL_RC=0
        "$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" cancel "$CANCEL_ID" --timeout 20 \
            || CANCEL_RC=$?
        CANCEL_STATUS_ERROR=""
        CANCEL_JSON="$(status_json "$CANCEL_ID")" || CANCEL_STATUS_ERROR="could not read cancelled job status"
        CANCEL_CHILD_AFTER_LIVE=0
        CANCEL_CHILD_AFTER_PID=""
        deadline=$((SECONDS + 15))
        while [ "$SECONDS" -lt "$deadline" ]; do
            find_cancel_child
            FIND_CANCEL_RC=$?
            case "$FIND_CANCEL_RC" in
                1) break ;;
                0)
                    printf '%s\n' "$CANCEL_PS_LISTING" >"$RUN_ROOT/live-cancel.ps.after"
                    if process_is_live "$CANCEL_CHILD_PID"; then
                        CANCEL_CHILD_AFTER_LIVE=1
                        CANCEL_CHILD_AFTER_PID="$CANCEL_CHILD_PID"
                        if [ "$CANCEL_CHILD_IN_GROUP" -eq 0 ]; then
                            break
                        fi
                    else
                        CANCEL_CHILD_AFTER_LIVE=0
                        break
                    fi
                    ;;
                *) abort_step 4 "host ps could not identify one unique cancellation child after cancellation" ;;
            esac
            sleep 0.25
        done
        if [ "$CANCEL_CHILD_IN_GROUP" -eq 0 ]; then
            if [ "$CANCEL_CHILD_AFTER_LIVE" -eq 1 ]; then
                if ! terminate_cancel_fixture "$CANCEL_CHILD_AFTER_PID"; then
                    abort_step 4 "escaped cancellation fixture survived and could not be safely stopped from its unique token and workspace cwd"
                fi
                CANCEL_CHILD_SURVIVED="yes"
            else
                CANCEL_CHILD_SURVIVED="no"
            fi
        elif [ "$CANCEL_CHILD_AFTER_LIVE" -eq 1 ]; then
            abort_step 4 "the child found in recorded PGID $CANCEL_PGID and SID $CANCEL_PGID survived cancellation"
        fi
        if [ "$CANCEL_RC" -ne 3 ] && [ -z "$CANCEL_STATUS_ERROR" ]; then
            CANCEL_STATUS_ERROR="cancel returned $CANCEL_RC instead of confirming cancellation"
        fi
        if [ -z "$CANCEL_STATUS_ERROR" ] && \
                { [ "$(json_value state "$CANCEL_JSON")" != "cancelled" ] \
                    || [ -z "$(json_value cancel_confirmed_at "$CANCEL_JSON")" ] \
                    || [ "$(printf '%s\n' "$CANCEL_JSON" | "$PYTHON" -c \
                        'import json,sys; attempts=json.load(sys.stdin).get("attempts") or []; print(str(bool(attempts and attempts[-1].get("cleanup_confirmed"))))')" != "True" ]; }; then
            CANCEL_STATUS_ERROR="relay status did not prove confirmed cancellation and attempt cleanup"
        fi
        if [ -z "$CANCEL_STATUS_ERROR" ]; then
            CANCEL_FINAL_PGID="$(printf '%s\n' "$CANCEL_JSON" | "$PYTHON" -c \
                'import json,sys; attempts=json.load(sys.stdin).get("attempts") or []; value=(attempts[-1].get("group_pgid") if attempts else None); print("" if value is None else str(value))')"
            if [ -z "$CANCEL_FINAL_PGID" ] || [ "$CANCEL_FINAL_PGID" != "$CANCEL_PGID" ]; then
                CANCEL_STATUS_ERROR="cancelled job status did not retain the pre-cancellation PGID $CANCEL_PGID"
            fi
        fi
        PENDING_JOB_ID=""
        if [ -n "$CANCEL_STATUS_ERROR" ]; then
            abort_step 4 "$CANCEL_STATUS_ERROR"
        fi
        if [ -n "$CANCEL_PRECHECK_FAILURE" ]; then
            abort_step 4 "$CANCEL_PRECHECK_FAILURE"
        fi
        if [ "$CANCEL_CHILD_IN_GROUP" -eq 0 ]; then
            set_step 4 "KNOWN GAP (backend tool child left the managed group; survived=$CANCEL_CHILD_SURVIVED)"
            if [ "$CANCEL_CHILD_SURVIVED" = "yes" ]; then
                if [ "$CANCEL_CLEANUP_METHOD" = "already exited" ]; then
                    printf 'KNOWN GAP: backend tool child left the managed group; survived=yes; it exited before a cleanup signal was needed.\n'
                else
                    printf 'KNOWN GAP: backend tool child left the managed group; survived=yes; stopped the escaped fixture with %s after verifying its unique token and cwd inside %s.\n' \
                        "$CANCEL_CLEANUP_METHOD" "$WORKSPACE_REAL"
                fi
            else
                printf 'KNOWN GAP: backend tool child left the managed group; survived=no.\n'
            fi
        else
            set_step 4 "PASS"
            printf 'PASS: host-visible child PID %s was in recorded PGID and SID %s before cancellation and was gone afterward.\n' \
                "$CANCEL_CHILD_PID" "$CANCEL_PGID"
        fi
    fi
fi

step_start 5
if [ ! -s "$RUN_ROOT/live-write.launch.stdout" ] || [ ! -s "$RUN_ROOT/live-write.launch.stderr" ]; then
    abort_step 5 "the detached launcher did not retain its stdout/stderr handoff"
fi
COLLECTED_RESULT="$RUN_ROOT/result-after-launcher-exit.txt"
"$PYTHON" "$RELAY_JOB" --state-dir "$STATE_DIR" result "$WRITE_ID" \
    >"$COLLECTED_RESULT" 2>"$RUN_ROOT/result-after-launcher-exit.stderr" || \
    abort_step 5 "result collection failed after the run launcher exited"
if [ ! -s "$COLLECTED_RESULT" ] || ! grep -Fq "WRITE_COMPLETE" "$COLLECTED_RESULT"; then
    abort_step 5 "result collection did not contain the final WRITE_COMPLETE response"
fi
set_step 5 "PASS"
printf 'PASS: result was collected after the detached run command had returned.\n'

step_start 6
if [ "$BACKEND" = "codex" ]; then
    READONLY_JSON="$(status_json "$READONLY_ID")" || abort_step 6 "could not read Codex job status"
    READONLY_STDOUT="$(json_value stdout_path "$READONLY_JSON")"
    EVENT_LOG="$RUN_ROOT/codex-event-shapes.log"
    if [ -z "$READONLY_STDOUT" ] || ! write_event_log "$READONLY_STDOUT" "$EVENT_LOG"; then
        abort_step 6 "Codex JSONL did not contain a valid thread.started event; see $EVENT_LOG"
    fi
    printf 'Codex event evidence: %s\n' "$EVENT_LOG"
    cat "$EVENT_LOG"
    set_step 6 "PASS"
elif [ "$BACKEND" = "claude" ]; then
    check_claude_event_shape
else
    set_step 6 "SKIP (Codex backend only)"
    printf 'SKIP: Codex JSONL evidence applies only to Codex runs.\n'
fi

step_start 7
if [ "$BACKEND" != "codex" ]; then
    set_step 7 "SKIP (Codex backend only)"
    printf 'SKIP: queue delivery is available only for Codex jobs.\n'
elif [ -z "$NOTIFY_THREAD" ]; then
    set_step 7 "SKIP (not requested)"
    printf 'SKIP: pass --notify-thread UUID to attempt delivery to an idle Codex thread.\n'
else
    NOTIFY_TASK="Reply exactly LIVE_NOTIFY_DONE."
    launch_job live-notify no "$NOTIFY_TASK" codex "$NOTIFY_THREAD" || abort_step 7 "notification probe job did not launch"
    NOTIFY_ID="$LAUNCHED_JOB_ID"
    PENDING_JOB_ID="$NOTIFY_ID"
    wait_job "$NOTIFY_ID" || abort_step 7 "notification probe job did not complete successfully"
    NOTIFY_STATUS=""
    NOTIFY_STATE=""
    deadline=$((SECONDS + 45))
    while [ "$SECONDS" -lt "$deadline" ]; do
        NOTIFY_STATUS="$(status_json "$NOTIFY_ID" 2>"$RUN_ROOT/live-notify.status.stderr")" || \
            NOTIFY_STATUS=""
        if [ -n "$NOTIFY_STATUS" ]; then
            NOTIFY_STATE="$(printf '%s\n' "$NOTIFY_STATUS" | "$PYTHON" -c \
                'import json,sys; rows=json.load(sys.stdin).get("notifications") or []; row=next((item for item in rows if item.get("transport")=="codex-queue"),{}); print(row.get("state",""))')"
            case "$NOTIFY_STATE" in
                sent|failed|uncertain) break ;;
            esac
        fi
        sleep 1
    done
    if [ -z "$NOTIFY_STATUS" ]; then
        abort_step 7 "could not read notification status"
    fi
    printf 'Codex queue notification status: %s\n' "${NOTIFY_STATE:-missing}"
    if [ "$NOTIFY_STATE" = "sent" ]; then
        set_step 7 "PASS (CLI reported sent; verify receipt in the named thread)"
        printf 'PASS: Codex reported sent. Confirm the message arrived in thread %s.\n' "$NOTIFY_THREAD"
    else
        NOTIFY_ERROR="$(printf '%s\n' "$NOTIFY_STATUS" | "$PYTHON" -c \
            'import json,sys; rows=json.load(sys.stdin).get("notifications") or []; row=next((item for item in rows if item.get("transport")=="codex-queue"),{}); print(row.get("last_error") or "")')"
        abort_step 7 "Codex queue status was ${NOTIFY_STATE:-missing}: ${NOTIFY_ERROR:-no status row}"
    fi
fi

if [ "$BACKEND" = "claude" ]; then
    step_start 8
    run_claude_git_step
fi

printf '\nLive evidence retained at %s\n' "$RUN_ROOT"
exit 0
