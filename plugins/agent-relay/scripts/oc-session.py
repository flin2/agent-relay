#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""
oc-session — per-repo "latest thread" pointer for the agent-relay wrappers.

The wrappers (oc-sub.sh / oc-api.sh) call this after each `opencode run` to:
  * record         — remember the opencode session the run created/continued, as
                     the repo's "latest thread", in last-session.json under the
                     shared per-repo state dir;
  * resolve-latest — print that recorded session id (verified to still exist), so
                     `--resume-latest` can continue exactly that thread.
  * find           — print the id of the session a run created, identified by its title and creation time rather than "newest in --dir", which belongs to whichever concurrent job touched the repo last.
  * last-finish    — print the finish reason of the session's last assistant turn
                     (e.g. "length" when the model was cut off at its output-token
                     cap). The runner uses this to tell a truncated turn apart from a
                     genuinely complete one, so a run that opencode exits 0 on but that
                     ended mid-task gets nudged to continue rather than reported as done.
  * last-model     — print the `provider/model` id the session's last assistant turn
                     ran on, so a run that left the model to opencode's configured
                     default can still name the model it actually used.

State layout matches relay-job.py: $STATE_ROOT/sessions/<slug>-<hash>/last-session.json, where STATE_ROOT is RELAY_JOB_STATE_DIR, XDG_STATE_HOME/agent-relay, or HOME/.local/state/agent-relay, and <slug>-<hash> is derived from the git toplevel of --dir.
Stdlib only. Every operation is best-effort: failures print nothing actionable and exit non-zero, never disturbing the wrapper's own exit code.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile


def _host():
    # RELAY_JOB_HOST (claude|codex) names the launching host explicitly. Both hosts export their own
    # variables into child shells, so a CLI started inside the other (e.g. Codex run from a Claude
    # Code session) inherits a foreign session id and plugin-data dir; the Codex skills set
    # RELAY_JOB_HOST=codex so those inherited Claude values are ignored. Unset: infer from the
    # environment, Claude first.
    host = os.environ.get('RELAY_JOB_HOST', '').strip().lower()
    if host in ('claude', 'codex'):
        return host
    if os.environ.get('CLAUDE_CODE_SESSION_ID'):
        return 'claude'
    if os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID'):
        return 'codex'
    return None


def _resolve_state_root():
    explicit = os.environ.get('RELAY_JOB_STATE_DIR')
    if explicit:
        root = explicit
    elif os.environ.get('XDG_STATE_HOME'):
        root = os.path.join(os.environ['XDG_STATE_HOME'], 'agent-relay')
    else:
        home = os.environ.get('HOME') or os.path.expanduser('~')
        root = os.path.join(home, '.local', 'state', 'agent-relay')
    if not os.path.isabs(root):
        return ''
    return os.path.realpath(root)


def _resolve_state_dir(target_dir):
    # Keep backend session pointers isolated under the shared agent-relay state root.
    root = _resolve_state_root()
    repo = os.path.abspath(target_dir)
    try:
        workspace_root = subprocess.check_output(
            ['git', '-C', repo, 'rev-parse', '--show-toplevel'],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        workspace_root = repo
    real = os.path.realpath(workspace_root)
    bname = os.path.basename(real)
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', bname).strip('-')
    if not slug:
        slug = 'workspace'
    h = hashlib.sha256(real.encode()).hexdigest()[:16]
    return os.path.join(root, 'sessions', f'{slug}-{h}')


def _pointer_path(target_dir):
    return os.path.join(_resolve_state_dir(target_dir), 'last-session.json')


def _list_sessions(cwd, limit=10):
    # Capture through a file, not a pipe: opencode exits before flushing a piped stdout past its first 8192 bytes, which truncates the JSON of a longer listing.
    try:
        with tempfile.TemporaryFile('w+') as fh:
            subprocess.run(
                ['opencode', 'session', 'list', '--format', 'json', '-n', str(limit)],
                cwd=cwd, stdout=fh, stderr=subprocess.DEVNULL, timeout=20, check=True,
            )
            fh.seek(0)
            out = fh.read()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _opencode_db_path():
    # opencode's local store; honours XDG_DATA_HOME, else the ~/.local/share default.
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    db = os.path.join(base, 'opencode', 'opencode.db')
    return db if os.path.exists(db) else None


def _latest_session_id_for_dir(real_target):
    matches = [
        s for s in _list_sessions(real_target)
        if s.get('directory') and os.path.realpath(s['directory']) == real_target
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s.get('updated', 0)).get('id')


def cmd_record(args):
    target = args.dir or os.getcwd()
    real_target = os.path.realpath(target)
    sessions = _list_sessions(real_target)
    chosen = None
    if args.known_session:
        for s in sessions:
            if s.get('id') == args.known_session:
                chosen = s
                break
        if chosen is None:
            chosen = {'id': args.known_session, 'title': args.title or ''}
    else:
        # The session our run just created/continued is the most-recently-updated
        # one whose directory is this repo.
        matches = [
            s for s in sessions
            if s.get('directory') and os.path.realpath(s['directory']) == real_target
        ]
        if matches:
            chosen = max(matches, key=lambda s: s.get('updated', 0))
    if not chosen or not chosen.get('id'):
        return 1
    pointer = {
        'id': chosen['id'],
        'title': chosen.get('title') or args.title or '',
        'model': args.model or '',
        'agent': args.agent or '',
        'backend': args.backend or '',
        'dir': real_target,
        'ended': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    path = _pointer_path(target)
    state_root = _resolve_state_root()
    sessions_root = os.path.join(state_root, 'sessions')
    os.makedirs(state_root, mode=0o700, exist_ok=True)
    os.chmod(state_root, 0o700)
    os.makedirs(sessions_root, mode=0o700, exist_ok=True)
    os.chmod(sessions_root, 0o700)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    tmp = tempfile.NamedTemporaryFile('w', dir=os.path.dirname(path), delete=False)
    json.dump(pointer, tmp)
    tmp.close()
    os.replace(tmp.name, path)
    if args.print_id:
        # The wrapper uses this to surface the session id on a trailer line so a
        # follow-up can continue exactly this thread (--session <id>).
        sys.stdout.write(chosen['id'])
    return 0


def cmd_find(args):
    # The wrappers title every fresh session, so a run's own session is the one in this directory with that exact title, created no earlier than the run started. Several jobs can share one --dir, so the newest session there is not necessarily ours; when the match is missing or ambiguous, print nothing rather than another job's id.
    real_target = os.path.realpath(args.dir or os.getcwd())
    matches = [
        s for s in _list_sessions(real_target, limit=50)
        if s.get('id') and s.get('title') == args.title
        and s.get('directory') and os.path.realpath(s['directory']) == real_target
        and (s.get('created') or 0) >= args.since_ms
    ]
    if len(matches) != 1:
        return 1
    sys.stdout.write(matches[0]['id'])
    return 0


def cmd_resolve_latest(args):
    target = args.dir or os.getcwd()
    path = _pointer_path(target)
    try:
        with open(path) as f:
            pointer = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 1
    sid = pointer.get('id')
    if not sid:
        return 1
    if args.verify:
        sessions = _list_sessions(os.path.realpath(target))
        if sessions and not any(s.get('id') == sid for s in sessions):
            return 1
    sys.stdout.write(sid)
    return 0


def _last_assistant_message(args):
    # Return the session's most recent assistant message, read straight from opencode's
    # local store (opencode exposes no CLI for message internals), or None.
    #
    # Best-effort and conservative: any uncertainty — no sqlite3, no db, the session
    # is not found, the schema differs, or a row is unreadable — yields None.
    target = args.dir or os.getcwd()
    real_target = os.path.realpath(target)
    sid = args.session or _latest_session_id_for_dir(real_target)
    if not sid:
        return None
    db = _opencode_db_path()
    if not db:
        return None
    try:
        import sqlite3
    except ImportError:
        return None
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        cur = con.execute(
            'SELECT data FROM message WHERE session_id = ? '
            'ORDER BY time_created DESC, id DESC LIMIT 50',
            (sid,),
        )
        for (data,) in cur:
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get('role') == 'assistant':
                return msg
        return None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def cmd_last_finish(args):
    # Print the finish reason of the session's most recent assistant message. The
    # runner reads this to tell a turn that opencode cut off at the model's
    # output-token cap (reason "length") apart from a genuine completion. Any
    # uncertainty prints nothing and exits non-zero, so the caller treats the run as
    # complete and never resumes a healthy session by mistake.
    msg = _last_assistant_message(args)
    reason = msg.get('finish') if msg else None
    if isinstance(reason, str) and reason:
        sys.stdout.write(reason)
        return 0
    return 1


def cmd_last_model(args):
    # Print the `provider/model` id the session's most recent assistant turn ran on.
    # The wrappers use this to name the model opencode picked when no -m was passed
    # (its configured default). Prints nothing and exits non-zero when unknown.
    msg = _last_assistant_message(args)
    if not msg:
        return 1
    provider, model = msg.get('providerID'), msg.get('modelID')
    if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
        return 1
    sys.stdout.write(f'{provider}/{model}')
    return 0


def main():
    p = argparse.ArgumentParser(prog='oc-session.py', description='Per-repo opencode latest-thread pointer.')
    sub = p.add_subparsers(dest='cmd', required=True)

    pr = sub.add_parser('record', help="Record the repo's latest opencode session.")
    pr.add_argument('--dir', default=None)
    pr.add_argument('--backend', default=None)
    pr.add_argument('--model', default=None)
    pr.add_argument('--agent', default=None)
    pr.add_argument('--title', default=None)
    pr.add_argument('--known-session', default=None, dest='known_session')
    pr.add_argument('--print-id', dest='print_id', action='store_true',
                    help="On success, print the recorded session id to stdout.")

    pl = sub.add_parser('resolve-latest', help="Print the repo's recorded latest session id.")
    pl.add_argument('--dir', default=None)
    pl.add_argument('--no-verify', dest='verify', action='store_false')
    pl.set_defaults(verify=True)

    pf = sub.add_parser('find', help="Print the id of the session a run created (by title and creation time).")
    pf.add_argument('--dir', default=None)
    pf.add_argument('--title', required=True)
    pf.add_argument('--since-ms', dest='since_ms', type=int, default=0,
                    help="Ignore sessions created before this epoch time in milliseconds.")

    lf = sub.add_parser('last-finish', help="Print the finish reason of the session's last assistant turn.")
    lf.add_argument('--dir', default=None)
    lf.add_argument('--session', default=None,
                    help="Session id to inspect; defaults to the repo's latest session for --dir.")

    lm = sub.add_parser('last-model', help="Print the provider/model id of the session's last assistant turn.")
    lm.add_argument('--dir', default=None)
    lm.add_argument('--session', default=None,
                    help="Session id to inspect; defaults to the repo's latest session for --dir.")

    args = p.parse_args()
    if args.cmd == 'record':
        sys.exit(cmd_record(args))
    elif args.cmd == 'find':
        sys.exit(cmd_find(args))
    elif args.cmd == 'resolve-latest':
        sys.exit(cmd_resolve_latest(args))
    elif args.cmd == 'last-finish':
        sys.exit(cmd_last_finish(args))
    elif args.cmd == 'last-model':
        sys.exit(cmd_last_model(args))
    else:
        p.print_help()
        sys.exit(2)


if __name__ == '__main__':
    main()
