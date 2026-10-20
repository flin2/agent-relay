# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line interface for the shared agent-relay lifecycle."""

import argparse
import datetime
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time

from . import store as store_module
from . import lifecycle
from .adapters import get_adapter


SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
WAIT_CODES = {
    'done': 0, 'failed': 2, 'cancelled': 3, 'crashed': 4,
    'ownership_unresolved': 6,
}
CLAUDE_REINVOCATION_NOTE = (
    'the relay cannot observe whether Claude re-invoked the originating conversation'
)


class _WaitArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, '{}: error: {}\n'.format(self.prog, message))


def _arg_parser(prog, description):
    parser_type = _WaitArgumentParser if prog.endswith(' wait') else argparse.ArgumentParser
    return parser_type(prog=prog, description=description, allow_abbrev=False)


def _current_conversation():
    host = lifecycle._host()
    conversation_id = lifecycle._conversation_id(host)
    return host, conversation_id, lifecycle._conversation_key(host, conversation_id)


def _canonical_workspace(target_dir):
    real_target = os.path.realpath(os.path.abspath(target_dir))
    try:
        top = subprocess.check_output(
            ['git', '-C', real_target, 'rev-parse', '--show-toplevel'],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        top = real_target
    return os.path.realpath(top)


def _read_task(args):
    sources = int(bool(getattr(args, 'stdin', False))) + int(bool(getattr(args, 'task_file', None)))
    words = getattr(args, 'task_words', [])
    if words:
        sources += 1
    if sources != 1:
        raise ValueError('pass task text in exactly one way: positional text, --task-file, or --stdin')
    if args.stdin:
        task = sys.stdin.read()
    elif args.task_file:
        try:
            with open(args.task_file, 'r', encoding='utf-8') as stream:
                task = stream.read()
        except OSError as exc:
            raise ValueError('cannot read --task-file {}: {}'.format(args.task_file, exc))
    else:
        task = ' '.join(words)
    if not task.strip():
        raise ValueError('task text is empty')
    return task


def _collection_command(state_root, job_id, operation, host=None):
    if operation not in ('wait', 'result', 'cancel'):
        raise ValueError('unsupported collection operation: {}'.format(operation))
    entrypoint = os.path.realpath(os.path.join(SCRIPT_DIR, 'relay-job.py'))
    command = 'python3 {} --state-dir {} {} {}'.format(
        shlex.quote(entrypoint), shlex.quote(os.path.realpath(state_root)),
        operation, shlex.quote(job_id),
    )
    if operation == 'wait' and host == 'claude':
        command += ' --claude-waiter'
    return command


def _emit_handoff(job_id, job, state_root, inline=False):
    wait_command = _collection_command(state_root, job_id, 'wait', host=job.get('host'))
    result_command = _collection_command(state_root, job_id, 'result')
    cancel_command = _collection_command(state_root, job_id, 'cancel')
    sys.stderr.write(
        '[agent-relay] launched {}: {} (backend={}, route={})\n'.format(
            'inline' if inline else 'detached', job_id, job.get('backend'),
            job.get('route') or '(none)',
        )
        + '[agent-relay] state root: {}\n'.format(state_root)
        + '[agent-relay] wait: {}\n'.format(wait_command)
        + '[agent-relay] result: {}\n'.format(result_command)
        + '[agent-relay] cancel: {}\n'.format(cancel_command)
    )
    if inline:
        sys.stderr.write(
            '[agent-relay] Inline collection is in progress. If interrupted, recover this '
            'job with the exact wait, result, and cancel commands above.\n'
        )
    elif job.get('host') == 'claude':
        sys.stderr.write(
            '[agent-relay] Claude: start the wait command as a BACKGROUND Bash call '
            '(run_in_background, no timeout) from this conversation. The waiter records '
            'local completion; {}. If that waiter is absent after a session restart, '
            'use the manual commands above.\n'.format(CLAUDE_REINVOCATION_NOTE)
        )
    elif job.get('host') == 'codex':
        sys.stderr.write(
            '[agent-relay] Codex: a queue notification is attempted when eligible; use '
            'the manual commands above if it is absent or fails.\n'
        )


def _job_fields(options, parent=None):
    host, conversation_id, conversation_key = _current_conversation()
    now = store_module.now_utc()
    fields = {
        'host': host,
        'host_conversation_id': conversation_id,
        'host_conversation_key': conversation_key,
        'backend': options['backend'],
        'route': options.get('route'),
        'gateway': options.get('gateway'),
        'workspace': options['workspace'],
        'cwd': options['cwd'],
        'label': options.get('label'),
        'model': options.get('model'),
        'effort': options.get('effort'),
        'write': bool(options.get('write')),
        'parent_job_id': options.get('parent_job_id'),
        'backend_session_id': options.get('backend_session_id'),
        'created_at': now,
        'updated_at': now,
        'options': options,
    }
    if parent:
        fields['backend'] = parent['backend']
    return fields


def _run_wait(job_id, db, inline=False, timeout=None, interval=0.25):
    try:
        job = lifecycle.wait_for_job(db, job_id, timeout=timeout, interval=interval)
    except KeyboardInterrupt:
        requested = lifecycle.request_cancel(db, job_id)
        if not requested:
            print('error: unknown job id: {}'.format(job_id), file=sys.stderr)
            return 1
        if requested['state'] in lifecycle.TERMINAL:
            job = requested
        else:
            job = lifecycle.wait_for_job(db, job_id, timeout=7.0, interval=0.1)
        if job == 'timeout' or not job or job['state'] not in lifecycle.TERMINAL:
            print('Cancellation was requested but is not confirmed; job id: {}'.format(job_id), file=sys.stderr)
            return 6
    if not job:
        print('error: unknown job id: {}'.format(job_id), file=sys.stderr)
        return 1
    if job == 'timeout':
        print('Job {} is still running (wait timed out).'.format(job_id))
        return 5
    if inline:
        _print_result(job, 'stdout')
        if job.get('stderr_path'):
            _copy_artifact(job['stderr_path'], sys.stderr.buffer, ignore_missing=True)
    if job['state'] == 'done':
        if not inline:
            print('Job {} completed.'.format(job_id))
        return 0
    if not inline:
        print('Job {} ended with state {}{}.'.format(
            job_id, job['state'],
            ' ({})'.format(job.get('failure_reason')) if job.get('failure_reason') else '',
        ))
    else:
        print('Job {} ended with state {}{}.'.format(
            job_id, job['state'],
            ' ({})'.format(job.get('failure_reason')) if job.get('failure_reason') else '',
        ), file=sys.stderr)
    return WAIT_CODES.get(job['state'], 1)


def cmd_run(raw_args):
    parser = _arg_parser('relay-job.py run', 'Launch a detached OpenCode or Codex adapter job.')
    parser.add_argument('--backend', required=True, choices=('opencode', 'codex'))
    parser.add_argument('--route', choices=('sub', 'api'))
    parser.add_argument('--zen', action='store_true')
    parser.add_argument('--go', action='store_true')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--background', action='store_true')
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument('--stdin', action='store_true')
    task_group.add_argument('--task-file')
    parser.add_argument('--dir', default=os.getcwd())
    parser.add_argument('--label')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('task_words', nargs='*')
    args = parser.parse_args(raw_args)
    if args.wait and args.background:
        raise ValueError('choose only one of --wait or --background')
    if args.backend == 'codex' and (args.route or args.zen or args.go):
        raise ValueError('OpenCode-only flags --route, --zen, and --go cannot be used with --backend codex')
    if args.zen and args.go:
        raise ValueError('choose only one of --zen or --go')
    gateway = 'go' if args.go else ('zen' if args.zen else None)
    adapter = get_adapter(args.backend)
    task_text = _read_task(args)
    target_dir = os.path.realpath(os.path.abspath(args.dir))
    if not os.path.isdir(target_dir):
        raise ValueError('working directory does not exist: {}'.format(args.dir))
    options = {
        'backend': args.backend,
        'route': args.route,
        'gateway': gateway,
        'cwd': target_dir,
        'workspace': _canonical_workspace(target_dir),
        'label': args.label,
        'model': args.model,
        'effort': args.effort,
        'write': args.write,
        'title': _session_title(args.label, task_text) if args.backend == 'opencode' else None,
    }
    options = adapter.validate(options)
    root = lifecycle._ensure_state_root(lifecycle._resolve_state_root())
    job = _job_fields(options)
    job_id = lifecycle.launch(root, job, task_text)
    _emit_handoff(job_id, job, root, inline=args.wait)
    if args.wait:
        db = store_module.Store(root)
        return _run_wait(job_id, db, inline=True)
    print(job_id)
    return 0


def _session_title(label, task_text):
    base = (label or '').strip()
    if not base:
        base = next((line.strip() for line in task_text.splitlines() if line.strip()), 'OpenCode task')
    # The relay job UUID is added after allocation by the wrapper's title policy below.
    return base[:64]


def cmd_resume(raw_args):
    parser = _arg_parser('relay-job.py resume', 'Continue the exact backend session recorded by a parent job.')
    parser.add_argument('parent_id')
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument('--stdin', action='store_true')
    task_group.add_argument('--task-file')
    parser.add_argument('--label')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('task_words', nargs='*')
    args = parser.parse_args(raw_args)
    if args.wait and args.background:
        raise ValueError('choose only one of --wait or --background')
    task_text = _read_task(args)
    root = lifecycle._ensure_state_root(lifecycle._resolve_state_root())
    db = store_module.Store(root)
    parent = lifecycle.reconcile_job(db, args.parent_id)
    if not parent:
        raise ValueError('unknown parent job id: {}'.format(args.parent_id))
    if parent['state'] in ('created', 'launching', 'running', 'ownership_unresolved'):
        raise ValueError('cannot resume active parent job {} (status: {})'.format(args.parent_id, parent['state']))
    failure_component = (parent.get('failure_reason') or '').split(';', 1)[0].strip()
    if failure_component == 'ambiguous_session_identity':
        raise ValueError(
            'cannot resume parent job {} with ambiguous backend session identity'.format(
                args.parent_id,
            ),
        )
    adapter = get_adapter(parent['backend'])
    if adapter.has_conflicting_session_identity(lifecycle._job_artifacts(parent)):
        raise ValueError(
            'cannot resume parent job {} with ambiguous backend session identity'.format(
                args.parent_id,
            ),
        )
    session_id = parent.get('backend_session_id')
    if not session_id:
        raise ValueError('parent job {} has no recorded backend session id'.format(args.parent_id))
    target_dir = parent.get('cwd')
    workspace = parent.get('workspace') or target_dir
    if not target_dir or not os.path.isdir(target_dir) or not workspace or not os.path.isdir(workspace):
        raise ValueError('parent job workspace is missing: {}'.format(workspace or target_dir or '(unknown)'))
    options = {
        'backend': parent['backend'], 'route': parent.get('route'),
        'gateway': parent.get('gateway'), 'cwd': target_dir, 'workspace': workspace,
        'label': args.label or 'resume {}'.format(args.parent_id),
        'model': args.model if args.model is not None else parent.get('model'),
        'effort': args.effort if args.effort is not None else parent.get('effort'),
        'write': args.write, 'parent_job_id': args.parent_id,
        'backend_session_id': session_id,
        'title': None,
    }
    options = adapter.validate(options)
    job = _job_fields(options, parent=parent)
    job_id = lifecycle.launch(root, job, task_text)
    _emit_handoff(job_id, job, root, inline=args.wait)
    if args.wait:
        return _run_wait(job_id, db, inline=True)
    print(job_id)
    return 0


def _iso_timestamp(timestamp):
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _decorate_job(job, db=None):
    if not job:
        return None
    decorated = dict(job)
    decorated['status'] = job['state']
    decorated['started'] = job.get('created_at')
    decorated['ended'] = job.get('ended_at')
    decorated['pid'] = job.get('supervisor_pid')
    decorated['pgid'] = job.get('group_pgid')
    decorated['artifacts_pruned'] = job.get('prune_state') == 'pruned'
    if db is not None:
        decorated['notifications'] = db.notifications(job['id'])
        notification_states = []
        for item in decorated['notifications']:
            state = '{}={}'.format(item['transport'], item['state'])
            if item['transport'] == 'claude-waiter':
                state += ' ({})'.format(CLAUDE_REINVOCATION_NOTE)
            notification_states.append(state)
        decorated['notification_state'] = ', '.join(notification_states) or 'none (manual collection)'
    started = job.get('created_at')
    try:
        decorated['started_ts'] = datetime.datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp() if started else 0
    except (ValueError, TypeError):
        decorated['started_ts'] = 0
    return decorated


def _list_jobs(db, show_all=False, directory=None):
    _, _, conversation_key = _current_conversation()
    canonical_dir = _canonical_workspace(directory) if directory else None
    jobs = []
    for raw_job in lifecycle.reconcile_all(db):
        job = _decorate_job(raw_job, db)
        if not show_all and conversation_key and job.get('host_conversation_key') != conversation_key:
            continue
        if canonical_dir and job.get('workspace') != canonical_dir:
            continue
        jobs.append(job)
    jobs.sort(key=lambda item: item.get('started_ts') or 0, reverse=True)
    if not show_all:
        jobs = jobs[:20]
    return jobs


def cmd_status(raw_args):
    parser = _arg_parser('relay-job.py status', 'Show one job or list jobs for this conversation.')
    parser.add_argument('job_id', nargs='?')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--dir')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(raw_args)
    db = store_module.Store(lifecycle._resolve_state_root())
    if args.job_id:
        job = _decorate_job(lifecycle.reconcile_job(db, args.job_id), db)
        if not job or (args.dir and job.get('workspace') != _canonical_workspace(args.dir)):
            print('error: unknown job id: {}'.format(args.job_id), file=sys.stderr)
            return 1
        if args.json:
            job['attempts'] = db.attempts(job['id'])
            json.dump(job, sys.stdout, sort_keys=True)
            sys.stdout.write('\n')
            return 0
        _print_job(job)
        return 0
    jobs = _list_jobs(db, args.all, args.dir)
    if args.json:
        json.dump(jobs, sys.stdout, sort_keys=True)
        sys.stdout.write('\n')
        return 0
    _print_job_table(jobs, args.all)
    return 0


def _print_job(job):
    print('Job: {}'.format(job['id']))
    print('  Host:                  {}'.format(job.get('host') or '(unknown)'))
    print('  Host conversation:     {}'.format(job.get('host_conversation_id') or '(unknown)'))
    print('  Host conversation key: {}'.format(job.get('host_conversation_key') or '(unknown)'))
    print('  Backend:               {}'.format(job.get('backend')))
    print('  Route:                 {}'.format(job.get('route') or '(none)'))
    print('  Backend session:       {}'.format(job.get('backend_session_id') or '(not recorded)'))
    print('  Parent job:            {}'.format(job.get('parent_job_id') or '(none)'))
    print('  Model:                 {}'.format(job.get('model') or '(backend default)'))
    print('  Effort:                {}'.format(job.get('effort') or '(default)'))
    print('  Write:                 {}'.format('yes' if job.get('write') else 'no'))
    print('  Status:                {}'.format(job['state']))
    print('  Notification:          {}'.format(job.get('notification_state') or 'not shown'))
    claude_waiter = (job.get('options') or {}).get('claude_waiter')
    if claude_waiter:
        print('  Claude waiter:         {} ({})'.format(
            claude_waiter.get('state'), CLAUDE_REINVOCATION_NOTE,
        ))
        print('  Claude wait command:   {}'.format(claude_waiter.get('command')))
    print('  Working directory:     {}'.format(job.get('cwd')))
    print('  Workspace:             {}'.format(job.get('workspace')))
    print('  Started:               {}'.format(job.get('created_at')))
    print('  Ended:                 {}'.format(job.get('ended_at') or '(still running)'))
    print('  Exit:                  {}'.format(job.get('exit_code') if job.get('exit_code') is not None else '(N/A)'))
    print('  Supervisor PID:        {}'.format(job.get('supervisor_pid') or '(N/A; diagnostic only)'))
    print('  Guard PID:             {}'.format(job.get('guard_pid') or '(N/A)'))
    print('  Backend group PGID:    {}'.format(job.get('group_pgid') or '(N/A)'))
    print('  Label:                 {}'.format(job.get('label') or '(none)'))
    if job.get('failure_reason'):
        print('  Failure:               {}'.format(job['failure_reason']))
    if job.get('prune_state') == 'pruned':
        print('  Artifacts:             pruned')
    if job.get('state') == 'ownership_unresolved':
        print('  Ownership evidence:    {}'.format(job.get('ownership_evidence') or '(not recorded)'))
        print('  Manual recovery:       Verify each recorded group member is stopped; reconciliation never signals processes.')
    print('  Output:                {}'.format(job.get('stdout_path')))


def _print_job_table(jobs, show_all):
    if not jobs:
        print('No jobs found.')
        return
    print('{:<36} {:<10} {:<8} {:<20} {:<20} {}'.format(
        'ID', 'BACKEND', 'ROUTE', 'STATUS', 'NOTIFICATION', 'LABEL',
    ))
    print('-' * 126)
    for job in jobs:
        print('{:<36} {:<10} {:<8} {:<20} {:<20} {}'.format(
            job['id'], job.get('backend') or '?', job.get('route') or '-', job['state'],
            job.get('notification_state', 'not shown'),
            (job.get('label') or job.get('created_at') or '')[:40],
        ))
    if not show_all:
        print('(current conversation jobs; pass --all for all conversations)')


def cmd_list(raw_args):
    parser = _arg_parser('relay-job.py list', 'List jobs.')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--dir')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(raw_args)
    forwarded = []
    if args.all:
        forwarded.append('--all')
    if args.dir:
        forwarded.extend(('--dir', args.dir))
    if args.json:
        forwarded.append('--json')
    return cmd_status(forwarded)


def _copy_artifact(path, destination, ignore_missing=False, tail=None):
    try:
        with open(path, 'rb') as stream:
            if tail is not None:
                data = stream.read()
                lines = data.splitlines(keepends=True)
                data = b''.join(lines[-tail:])
                destination.write(data)
            else:
                shutil = __import__('shutil')
                shutil.copyfileobj(stream, destination)
        return True
    except OSError:
        if not ignore_missing:
            return False
        return False


def _result_artifact(job, selection='stdout'):
    if job.get('prune_state') == 'pruned' or not os.path.isdir(job['artifact_dir']):
        return None, 'pruned', 'Output artifacts were pruned for job {}.'.format(job['id'])
    if selection == 'stdout' and os.path.isfile(job.get('response_path') or ''):
        path = job['response_path']
        message = 'final response'
    else:
        key = {'stdout': 'stdout_path', 'stderr': 'stderr_path', 'events': 'events_path'}[selection]
        path = job.get(key)
        message = 'partial stdout' if selection == 'stdout' else selection
    if not path or not os.path.isfile(path):
        return None, 'unavailable', '{} artifact is unavailable for job {}.'.format(selection, job['id'])
    return path, message, None


def _print_result(job, selection='stdout', tail=None, output=None):
    if output is None:
        output = sys.stdout.buffer
    path, label, error = _result_artifact(job, selection)
    if error:
        print(error, file=sys.stderr)
        return False
    if selection == 'stdout' and label != 'final response':
        print('No final response artifact; showing {}.'.format(label), file=sys.stderr)
    return _copy_artifact(path, output, tail=tail)


def cmd_result(raw_args):
    parser = _arg_parser('relay-job.py result', 'Print a final response or an artifact stream.')
    parser.add_argument('job_id')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--stderr', action='store_true')
    selection.add_argument('--events', action='store_true')
    parser.add_argument('--tail', type=int)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(raw_args)
    if args.tail is not None and args.tail < 0:
        raise ValueError('--tail must be nonnegative')
    db = store_module.Store(lifecycle._resolve_state_root())
    job = _decorate_job(lifecycle.reconcile_job(db, args.job_id))
    if not job:
        print('error: unknown job id: {}'.format(args.job_id), file=sys.stderr)
        return 1
    selected = 'stderr' if args.stderr else ('events' if args.events else 'stdout')
    if args.json:
        path, label, error = _result_artifact(job, selected)
        content = ''
        availability = 'pruned' if label == 'pruned' else ('unavailable' if error else 'available')
        if path:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                    content = stream.read()
            except OSError:
                availability = 'unavailable'
                error = '{} artifact is unavailable for job {}.'.format(selected, job['id'])
        if error:
            print(error, file=sys.stderr)
        json.dump({'id': job['id'], 'state': job['state'], 'output': content,
                   'selection': selected, 'label': label,
                   'availability': availability}, sys.stdout, sort_keys=True)
        sys.stdout.write('\n')
        return 1 if availability != 'available' else 0
    success = _print_result(job, selected, args.tail)
    return 0 if success else 1


def cmd_wait(raw_args):
    parser = _arg_parser('relay-job.py wait', 'Wait for a job to reach a terminal state.')
    parser.add_argument('job_id')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--interval', type=float, default=0.25)
    parser.add_argument('--claude-waiter', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(raw_args)
    if args.timeout is not None and args.timeout < 0:
        raise ValueError('--timeout must be nonnegative')
    if args.interval <= 0:
        raise ValueError('--interval must be positive')
    db = store_module.Store(lifecycle._resolve_state_root())
    if not args.claude_waiter:
        return _run_wait(args.job_id, db, inline=False, timeout=args.timeout, interval=args.interval)
    job = db.get_job(args.job_id)
    if not job:
        return _run_wait(args.job_id, db, inline=False, timeout=args.timeout, interval=args.interval)
    if job.get('host') != 'claude':
        raise ValueError('--claude-waiter is only valid for Claude host jobs')
    command = _collection_command(db.root, args.job_id, 'wait', host='claude')
    waiter_fd = lifecycle._lock_file(
        lifecycle.artifact_paths(job)['claude_waiter_lock'], blocking=True,
    )
    try:
        _record_claude_waiter(db, args.job_id, 'registered', command=command)
        try:
            exit_code = _run_wait(
                args.job_id, db, inline=False, timeout=args.timeout, interval=args.interval,
            )
        except BaseException as exc:
            _record_claude_waiter(db, args.job_id, 'uncertain', error=str(exc))
            raise
        lifecycle._fault('claude_waiter_before_completion_record')
        _record_claude_waiter(db, args.job_id, 'completed', exit_code=exit_code)
        return exit_code
    finally:
        try:
            fcntl.flock(waiter_fd, fcntl.LOCK_UN)
            os.close(waiter_fd)
        except OSError:
            pass


def _record_claude_waiter(db, job_id, state, command=None, exit_code=None, error=None):
    now = store_module.now_utc()
    with db.transaction() as conn:
        job = db.get_job(job_id, conn)
        if not job:
            raise ValueError('unknown job id: {}'.format(job_id))
        if job.get('host') != 'claude':
            raise ValueError('--claude-waiter is only valid for Claude host jobs')
        options = dict(job.get('options') or {})
        waiter = dict(options.get('claude_waiter') or {})
        if state == 'registered':
            waiter.update({
                'command': command,
                'state': 'registered',
                'pid': os.getpid(),
                'registered_at': now,
                'exit_code': None,
                'ended_at': None,
                'error': None,
            })
            db.insert_notification(conn, job_id, 'claude-waiter', 'pending')
            db.update_notification(
                conn, job_id, 'claude-waiter', 'pending', last_error=None, attempts=0,
            )
        else:
            waiter.update({
                'state': state,
                'exit_code': exit_code,
                'ended_at': now,
                'error': error,
            })
            db.update_notification(
                conn, job_id, 'claude-waiter', 'uncertain',
                last_error=error or CLAUDE_REINVOCATION_NOTE, attempts=1,
            )
        options['claude_waiter'] = waiter
        db.update_job(conn, job_id, options=options)


def cmd_cancel(raw_args):
    parser = _arg_parser('relay-job.py cancel', 'Request cancellation and wait for confirmation.')
    parser.add_argument('job_id')
    parser.add_argument('--timeout', type=float, default=7.0)
    args = parser.parse_args(raw_args)
    if args.timeout < 0:
        raise ValueError('--timeout must be nonnegative')
    db = store_module.Store(lifecycle._resolve_state_root())
    job = lifecycle.reconcile_job(db, args.job_id)
    if not job:
        print('error: unknown job id: {}'.format(args.job_id), file=sys.stderr)
        return 1
    if job['state'] in lifecycle.TERMINAL:
        print('Job {} already {}. Nothing to cancel.'.format(args.job_id, job['state']))
        return WAIT_CODES.get(job['state'], 1)
    if job['state'] == 'ownership_unresolved':
        print('Job {} ownership is unresolved; no process was signalled.'.format(args.job_id), file=sys.stderr)
        return 6
    lifecycle.request_cancel(db, args.job_id)
    result = lifecycle.wait_for_job(db, args.job_id, timeout=args.timeout, interval=0.1)
    if result == 'timeout' or not result or result['state'] not in lifecycle.TERMINAL:
        print('Cancellation requested but not confirmed; job id: {}'.format(args.job_id), file=sys.stderr)
        return 6
    if result['state'] == 'cancelled':
        print('Job {} cancelled; the owned process group has exited.'.format(args.job_id))
        return 3
    print('Job {} completed as {} before cancellation took effect.'.format(args.job_id, result['state']))
    return WAIT_CODES.get(result['state'], 1)


def cmd_prune(raw_args):
    parser = _arg_parser('relay-job.py prune', 'Prune artifacts for old terminal jobs; metadata is retained.')
    parser.add_argument('--older-than', type=float, required=True, metavar='DAYS')
    args = parser.parse_args(raw_args)
    if args.older_than < 0:
        raise ValueError('--older-than must be nonnegative')
    db = store_module.Store(lifecycle._resolve_state_root())
    jobs = lifecycle.reconcile_all(db)
    cutoff = time.time() - args.older_than * 86400
    count = sum(1 for job in jobs if lifecycle.prune_job(db, job['id'], cutoff))
    print('Pruned artifacts for {} job(s); job records were retained.'.format(count))
    return 0


def _compat_read_meta(job_dir):
    root = os.path.dirname(os.path.dirname(os.path.realpath(job_dir)))
    db = store_module.Store(root)
    row = db.get_job(os.path.basename(job_dir))
    if not row:
        raise FileNotFoundError(job_dir)
    return row


def _compat_write_meta(job_dir, job):
    root = os.path.dirname(os.path.dirname(os.path.realpath(job_dir)))
    db = store_module.Store(root)
    allowed = (
        'state', 'failure_reason', 'ownership_evidence', 'host', 'host_conversation_id',
        'host_conversation_key', 'backend', 'route', 'gateway', 'workspace', 'cwd',
        'label', 'model', 'effort', 'write', 'parent_job_id', 'backend_session_id',
        'cancel_requested_at', 'cancel_confirmed_at', 'supervisor_token', 'supervisor_pid',
        'supervisor_started_at', 'guard_pid', 'group_pgid', 'exit_code', 'exit_signal',
        'ended_at', 'task_ready', 'prune_state', 'options',
    )
    updates = {key: job[key] for key in allowed if key in job}
    if 'status' in job and 'state' not in updates:
        updates['state'] = job['status']
    if 'dir' in job and 'cwd' not in updates:
        updates['cwd'] = job['dir']
    job_id = os.path.basename(job_dir)
    if 'state' in updates:
        state = updates.pop('state')
        with db.transaction() as conn:
            current = db.get_job(job_id, conn)
            if current:
                transitioned = db.cas_state(
                    conn, job_id, (current['state'],), state,
                    expected_token=current.get('supervisor_token'), **updates,
                )
                if transitioned and state in lifecycle.TERMINAL:
                    db.release_claim(conn, job_id)
    else:
        with db.transaction() as conn:
            db.update_job(conn, job_id, **updates)


def _print_help():
    print('''relay-job.py — shared SQLite job manager for OpenCode and Codex adapters.

Usage:
    relay-job.py [--state-dir PATH] run --backend {opencode|codex} [--route {sub|api}] [options] (task text | --task-file PATH | --stdin)
    relay-job.py [--state-dir PATH] resume JOB_ID [options] (task text | --task-file PATH | --stdin)
    relay-job.py [--state-dir PATH] status [JOB_ID] [--all] [--dir PATH] [--json]
    relay-job.py [--state-dir PATH] result JOB_ID [--stderr|--events]
    relay-job.py [--state-dir PATH] wait JOB_ID [--timeout SECONDS] [--interval SECONDS]
    relay-job.py [--state-dir PATH] list [--all] [--dir PATH] [--json]
    relay-job.py [--state-dir PATH] cancel JOB_ID [--timeout SECONDS]
    relay-job.py [--state-dir PATH] prune --older-than DAYS

Global options:
    --state-dir PATH              absolute state root; overrides RELAY_JOB_STATE_DIR

Both the OpenCode and Codex backends use the shared detached lifecycle.'''.rstrip())


def _print_run_help():
    print('''relay-job.py run — launch a detached adapter job.

Required:
    --backend opencode|codex
    --route sub|api                 required for --backend opencode

Shared options:
    --stdin | --task-file PATH     provide task text without shell quoting
    --dir PATH                     working directory (default: current directory)
    --label TEXT                   label shown in job listings
    --model MODEL                  backend model or model ID
    --effort VALUE                 backend effort setting where supported
    --write                        allow edits (default: read-only)
    --wait | --background          wait for the detached job or return its ID

OpenCode subscription route options:
    --zen | --go                   select a subscription gateway with route sub'''.rstrip())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    state_dir = None
    if argv and argv[0] == '--state-dir':
        if len(argv) < 2:
            raise ValueError('--state-dir requires an absolute PATH')
        state_dir, argv = argv[1], argv[2:]
    elif argv and argv[0].startswith('--state-dir='):
        state_dir, argv = argv[0].split('=', 1)[1], argv[1:]

    if state_dir is not None:
        if not state_dir or not os.path.isabs(state_dir):
            raise ValueError('state root must be an absolute path: {}'.format(state_dir))
        state_dir = store_module.resolve_state_root({'RELAY_JOB_STATE_DIR': state_dir})
        was_set = 'RELAY_JOB_STATE_DIR' in os.environ
        previous_state_dir = os.environ.get('RELAY_JOB_STATE_DIR')
        os.environ['RELAY_JOB_STATE_DIR'] = state_dir
        try:
            return _dispatch(argv)
        finally:
            if was_set:
                os.environ['RELAY_JOB_STATE_DIR'] = previous_state_dir
            else:
                os.environ.pop('RELAY_JOB_STATE_DIR', None)
    return _dispatch(argv)


def _dispatch(argv):
    if not argv or argv[0] in ('-h', '--help'):
        _print_help()
        return 0
    command, args = argv[0], argv[1:]
    if command == 'run':
        if args and args[0] in ('-h', '--help'):
            _print_run_help()
            return 0
        return cmd_run(args)
    if command == 'resume':
        return cmd_resume(args)
    if command == '_supervisor':
        parser = _arg_parser('relay-job.py _supervisor', 'Internal supervisor entry point.')
        parser.add_argument('job_id')
        parser.add_argument('--lock-fd', type=int)
        parsed = parser.parse_args(args)
        return lifecycle.supervise(
            lifecycle._resolve_state_root(), parsed.job_id, parsed.lock_fd,
        )
    if command == '_guard':
        parser = _arg_parser('relay-job.py _guard', 'Internal guard entry point.')
        parser.add_argument('job_id')
        parser.add_argument('control_fd', type=int)
        parser.add_argument('status_fd', type=int)
        parser.add_argument('invocation_path')
        parsed = parser.parse_args(args)
        return lifecycle.guard(parsed.job_id, parsed.control_fd, parsed.status_fd, parsed.invocation_path)
    if command == 'status':
        return cmd_status(args)
    if command == 'list':
        return cmd_list(args)
    if command == 'result':
        return cmd_result(args)
    if command == 'wait':
        return cmd_wait(args)
    if command == 'cancel':
        return cmd_cancel(args)
    if command == 'prune':
        return cmd_prune(args)
    print('error: unknown subcommand: {}'.format(command), file=sys.stderr)
    _print_help()
    return 1


# Kept as data helpers for the existing host regression tests; lifecycle metadata stays in
# SQLite, and these aliases never read or write a file-based status store.
_host = lifecycle._host
_conversation_id = lifecycle._conversation_id
_conversation_key = lifecycle._conversation_key
_resolve_state_root = lifecycle._resolve_state_root
_ensure_state_root = lifecycle._ensure_state_root
_job_dir = lifecycle._job_dir
_make_job_id = lifecycle._make_job_id
_read_meta = _compat_read_meta
_write_meta = _compat_write_meta

