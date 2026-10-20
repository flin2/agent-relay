# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex CLI adapter for the shared agent-relay lifecycle."""

import json
import os

from . import Adapter, Invocation, Outcome


_RESUME_SESSION_KEY = '_codex_resume_session_id'

# Event shapes remain isolated here until installed event behavior is verified.
EVENT_SHAPES = {
    'thread.started': ('session', 'thread_id'),
    'turn.started': ('turn_started', None),
    'turn.completed': ('turn_completed', None),
    'turn.failed': ('turn_failed', 'error'),
    'error': ('diagnostic', 'error'),
}


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=True)


def _event_message(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = value.get('message')
        if isinstance(message, str) and message:
            return message
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _interpret_event(event):
    """Return the normalized event kind, session/message fields, or a diagnostic."""
    if not isinstance(event, dict):
        return None, 'event must be a JSON object'
    event_type = event.get('type')
    if not isinstance(event_type, str) or not event_type:
        return None, 'event has no string type'
    if event_type.startswith('item.'):
        return {'kind': 'item'}, None
    shape = EVENT_SHAPES.get(event_type)
    if shape is None:
        return {'kind': 'unknown'}, None
    kind, field = shape
    value = event.get(field) if field else None
    if kind == 'session':
        if not isinstance(value, str) or not value:
            return None, 'thread.started has no nonempty thread_id'
        return {'kind': kind, 'session_id': value}, None
    if kind == 'turn_failed':
        if field not in event or value is None:
            return None, 'turn.failed has no error'
        return {'kind': kind, 'message': _event_message(value)}, None
    if kind == 'diagnostic':
        message = _event_message(value) if value is not None else _event_message(event)
        return {'kind': kind, 'message': message}, None
    return {'kind': kind}, None


def _scan_events(artifacts):
    path = artifacts.get('stdout')
    result = {
        'complete_lines': 0,
        'events': [],
        'session_ids': [],
        'malformed': [],
        'diagnostics': [],
        'truncated_tail': False,
    }
    if not path:
        return result
    try:
        stream = open(path, 'rb')
    except OSError:
        return result
    with stream:
        for line_number, raw_line in enumerate(stream, 1):
            if not raw_line.endswith(b'\n'):
                result['truncated_tail'] = bool(raw_line)
                continue
            result['complete_lines'] += 1
            raw_line = raw_line[:-1]
            if raw_line.endswith(b'\r'):
                raw_line = raw_line[:-1]
            try:
                decoded = raw_line.decode('utf-8')
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                result['malformed'].append((line_number, str(exc)))
                continue
            event, problem = _interpret_event(payload)
            if problem:
                result['malformed'].append((line_number, problem))
                continue
            event['index'] = len(result['events'])
            result['events'].append(event)
            if event['kind'] == 'session':
                result['session_ids'].append(event['session_id'])
            elif event['kind'] == 'diagnostic':
                result['diagnostics'].append(event['message'])
    return result


def _record_diagnostics(artifacts, scan):
    diagnostics = [
        'malformed Codex JSONL event on line {}: {}'.format(line, reason)
        for line, reason in scan['malformed']
    ]
    diagnostics.extend('Codex error event: {}'.format(message) for message in scan['diagnostics'])
    path = artifacts.get('stderr')
    if not diagnostics or not path or artifacts.get('_codex_diagnostics_written'):
        return
    artifacts['_codex_diagnostics_written'] = True
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            for diagnostic in diagnostics:
                stream.write(diagnostic + '\n')
    except OSError:
        pass


class CodexAdapter(Adapter):
    name = 'codex'

    def validate(self, options):
        if options.get('route') or options.get('gateway'):
            raise ValueError(
                'OpenCode-only flags --route, --zen, and --go cannot be used with --backend codex'
            )
        return dict(options)

    def prepare(self, job, artifacts, resume_session_id=None):
        self.validate(job.get('options') or {})
        response_path = artifacts['response']
        model = job.get('model')
        effort = job.get('effort')
        sandbox = 'workspace-write' if job.get('write') else 'read-only'
        argv = ['codex', 'exec']
        cwd = job['cwd']
        if resume_session_id:
            cwd = job.get('workspace') or job['cwd']
            argv.extend((
                'resume', '--json', '--output-last-message', response_path,
                '--skip-git-repo-check', '-c',
                'sandbox_mode={}'.format(_toml_string(sandbox)),
                '-c', 'approval_policy="never"',
            ))
            if model:
                argv.extend(('--model', model))
            if effort:
                argv.extend(('-c', 'model_reasoning_effort={}'.format(_toml_string(effort))))
            argv.extend((resume_session_id, '-'))
            artifacts[_RESUME_SESSION_KEY] = resume_session_id
        else:
            argv.extend((
                '--json', '--output-last-message', response_path,
                '--cd', cwd, '--sandbox', sandbox,
                '--skip-git-repo-check', '-c', 'approval_policy="never"',
            ))
            if model:
                argv.extend(('--model', model))
            if effort:
                argv.extend(('-c', 'model_reasoning_effort={}'.format(_toml_string(effort))))
            argv.append('-')
        return Invocation(argv, cwd, {}, artifacts['task'])

    def observe_session(self, artifacts):
        scan = _scan_events(artifacts)
        session_ids = scan['session_ids']
        requested_session = artifacts.get(_RESUME_SESSION_KEY)
        if not requested_session and len(set(session_ids)) > 1:
            return None
        # The supervisor claims every newly observed identity. Do not offer a resumed
        # thread's mismatched identity to that claim path; classify() reports it directly.
        if requested_session and any(session_id != requested_session for session_id in session_ids):
            return None
        return session_ids[0] if session_ids else None

    def has_conflicting_session_identity(self, artifacts):
        scan = _scan_events(artifacts)
        return (
            not artifacts.get(_RESUME_SESSION_KEY)
            and len(set(scan['session_ids'])) > 1
        )

    def classify(self, exit_info, artifacts, attempt_result=None, attempt_history=None):
        scan = _scan_events(artifacts)
        _record_diagnostics(artifacts, scan)
        session_ids = scan['session_ids']
        requested_session = artifacts.get(_RESUME_SESSION_KEY)
        ambiguous_fresh_identity = not requested_session and len(set(session_ids)) > 1
        outcome_session = requested_session or (
            session_ids[0] if session_ids and not ambiguous_fresh_identity else None
        )
        if exit_info.spawn_error:
            return Outcome('failed', 'startup', outcome_session, False)
        if ambiguous_fresh_identity:
            return Outcome(
                'failed', 'ambiguous_session_identity', None, False,
                clear_session_id=True,
            )
        if scan['truncated_tail']:
            return Outcome('failed', 'truncated_stream', outcome_session, False)
        if scan['complete_lines'] == 0:
            return Outcome('failed', 'startup', requested_session, False)
        if scan['malformed']:
            return Outcome('failed', 'malformed_stream', outcome_session, False)
        if requested_session and any(session_id != requested_session for session_id in session_ids):
            return Outcome('failed', 'resumed_session_mismatch', requested_session, False)

        events = scan['events']
        turn_starts = [event['index'] for event in events if event['kind'] == 'turn_started']
        last_turn_start = turn_starts[-1] if turn_starts else -1
        turn_failures = [
            event for event in events
            if event['kind'] == 'turn_failed' and event['index'] > last_turn_start
        ]
        if turn_failures:
            return Outcome(
                'failed', 'turn_failed: {}'.format(turn_failures[-1]['message']),
                outcome_session, False,
            )
        if exit_info.exit_signal is not None or exit_info.exit_code != 0:
            status = (
                'signal_{}'.format(exit_info.exit_signal)
                if exit_info.exit_signal is not None else
                str(exit_info.exit_code) if exit_info.exit_code is not None else 'unknown'
            )
            return Outcome(
                'failed', 'nonzero_exit: {}'.format(status),
                outcome_session, False,
            )
        if not requested_session and not session_ids:
            return Outcome('failed', 'missing_session_identity', None, False)
        terminal_events = [
            event for event in events
            if event['kind'] == 'turn_completed' and event['index'] > last_turn_start
        ]
        if not turn_starts or not terminal_events:
            return Outcome(
                'failed', 'missing_terminal_event',
                outcome_session, False,
            )
        response = artifacts.get('response')
        response_written = False
        if response:
            try:
                response_written = os.path.isfile(response) and os.path.getsize(response) > 0
            except OSError:
                response_written = False
        return Outcome(
            'done', None, outcome_session, response_written,
        )
