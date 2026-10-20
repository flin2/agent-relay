# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code CLI adapter for the shared agent-relay lifecycle."""

import json
import os
import subprocess
import uuid

from . import Adapter, Invocation, Outcome


_EXPECTED_SESSION_KEY = '_claude_expected_session_id'
_ROUTE_KEY = '_claude_route'
_DIAGNOSTICS_KEY = '_claude_diagnostics_written'
AUTH_STATUS_TIMEOUT = 30.0
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
READ_ONLY_TOOLS = 'Read,Grep,Glob,Bash'
READ_ONLY_GIT = (
    'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git show:*)',
    'Bash(git rev-parse:*)', 'Bash(git ls-files:*)', 'Bash(git merge-base:*)',
    'Bash(git branch --show-current)', 'Bash(git blame:*)',
)
# Deny rules take precedence over allow rules and close Git options that write files or run
# external programs. Enforcement is verified only by Stage 4.2 live checks.
READ_ONLY_GIT_DENY = (
    'Bash(git *--output*)', 'Bash(git *--ext-diff*)', 'Bash(git *--textconv*)',
    'Bash(git *--no-index*)', 'Bash(git *--open-files-in-pager*)', 'Bash(git *>*)',
    'Bash(git *|*)', 'Bash(git *;*)', 'Bash(git *&*)', 'Bash(git *$(*)', 'Bash(git *`*)',
)
# Inherited variables that make read-only Git commands run external programs or inject config.
# Git config files can still define helpers; that limitation is documented, not solved here.
READ_ONLY_GIT_ENV_REMOVALS = (
    'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'PAGER', 'GIT_EDITOR', 'GIT_SSH', 'GIT_SSH_COMMAND',
    'GIT_ASKPASS', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
)
# A success result with one of these terminal reasons stopped at a limit, not at completion.
TURN_OR_BUDGET_STOPS = ('max_turns', 'budget', 'max_tokens')

# Stream-json shapes from a live probe of claude 2.1.304. Unknown event types are tolerated.
# API-key source values are provisional until verified against installed CLI builds.
EVENT_SHAPES = {
    'system': 'system',
    'result': 'result',
    'assistant': 'message',
    'user': 'message',
    'rate_limit_event': 'message',
    'stream_event': 'message',
}
# Permitted apiKeySource values per route; provisional like EVENT_SHAPES.
CREDENTIAL_SOURCES = {
    'sub': frozenset(('none',)),
    'api': frozenset(('ANTHROPIC_API_KEY', 'apiKeyHelper')),
}

# Host and provider variables that must not reach a relay-launched Claude process.
HOST_MARKERS = (
    'CLAUDECODE',  # set by the host session; marks the child as nested
    'CLAUDE_CODE_ENTRYPOINT',  # host launch surface (CLI, IDE, SDK)
    'CLAUDE_CODE_CHILD_SESSION',  # host child-session flag
    'CLAUDE_PID',  # host process id; stale inside the job
    'CLAUDE_EFFORT',  # host effort; the job takes --effort from its own options
    'CLAUDE_CODE_SESSION_ATTENDED',  # host attendance flag
    'CLAUDE_CODE_EXECPATH',  # host executable path
    'CLAUDE_CODE_MESSAGING_SOCKET',  # host messaging channel
    'CLAUDE_CODE_MESSAGING_TOKEN',  # host messaging credential
    'CLAUDE_CODE_SAFE_MODE',  # host mode toggle; the job sets its own permissions
    'CLAUDE_CODE_SIMPLE',  # host mode toggle; the job sets its own tools
)
PROVIDER_OVERRIDES = (
    'ANTHROPIC_AUTH_TOKEN',  # alternate bearer credential could bypass the chosen route
    'ANTHROPIC_BASE_URL',  # would redirect requests away from the billed endpoint
    'CLAUDE_CODE_USE_BEDROCK',  # would switch the job to another provider
    'CLAUDE_CODE_USE_VERTEX',  # would switch the job to another provider
    'CLAUDE_CODE_USE_FOUNDRY',  # would switch the job to another provider
)
SUB_ROUTE_REMOVALS = (
    'ANTHROPIC_API_KEY',  # an API key would bill the job instead of the subscription login
)
API_ROUTE_REMOVALS = (
    'CLAUDE_CODE_OAUTH_TOKEN',  # a subscription token would bill the job instead of the API key
)


def _env_remove(route):
    if route == 'sub':
        return HOST_MARKERS + PROVIDER_OVERRIDES + SUB_ROUTE_REMOVALS
    return HOST_MARKERS + PROVIDER_OVERRIDES + API_ROUTE_REMOVALS


def _interpret_event(event):
    """Return the normalized event, or a diagnostic when the event is malformed."""
    if not isinstance(event, dict):
        return None, 'event must be a JSON object'
    event_type = event.get('type')
    if not isinstance(event_type, str) or not event_type:
        return None, 'event has no string type'
    session_id = event.get('session_id')
    normalized = {
        'kind': EVENT_SHAPES.get(event_type, 'unknown'),
        'raw_type': event_type,
        'session_id': session_id if isinstance(session_id, str) else None,
    }
    if normalized['kind'] == 'system' and event.get('subtype') == 'init':
        source = event.get('apiKeySource')
        normalized['kind'] = 'init'
        normalized['api_key_source'] = source if isinstance(source, str) else None
    elif normalized['kind'] == 'result':
        is_error = event.get('is_error')
        subtype = event.get('subtype')
        if not isinstance(is_error, bool):
            return None, 'result event has no boolean is_error'
        if not isinstance(subtype, str):
            return None, 'result event has no string subtype'
        if not isinstance(session_id, str):
            return None, 'result event has no string session_id'
        text = event.get('result')
        # An absent (or null) result is treated as empty; any other non-string is malformed.
        if text is not None and not isinstance(text, str):
            return None, 'result event has a non-string result'
        terminal_reason = event.get('terminal_reason')
        if not isinstance(terminal_reason, str) or not terminal_reason:
            terminal_reason = None
        normalized.update({
            'is_error': is_error,
            'subtype': subtype,
            'text': text or '',
            'terminal_reason': terminal_reason,
            'api_error_status': event.get('api_error_status'),
            'providers': _model_providers(event.get('modelUsage')),
        })
    return normalized, None


def _model_providers(model_usage):
    # Optional evidence: the probe's result carries modelUsage[<model>].provider; absence is allowed.
    if not isinstance(model_usage, dict):
        return []
    return [
        usage['provider'] for usage in model_usage.values()
        if isinstance(usage, dict) and isinstance(usage.get('provider'), str)
    ]


def _work_after_last_result(events):
    result_indexes = [event['index'] for event in events if event['kind'] == 'result']
    if not result_indexes:
        return False
    last = result_indexes[-1]
    return any(
        event['raw_type'] in ('assistant', 'user') and event['index'] > last
        for event in events
    )


def _decode_object(raw_line):
    """Return the JSON object encoded by raw_line, or None when it is not one."""
    try:
        payload = json.loads(raw_line.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _scan_events(artifacts):
    path = artifacts.get('stdout')
    result = {
        'complete_lines': 0,
        'events': [],
        'session_ids': [],
        'malformed': [],
        'truncated_tail': False,
        'api_key_sources': [],
        'results': [],
        'providers': [],
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
                # A complete JSON object missing only its newline still carries identity evidence.
                tail = _decode_object(raw_line)
                if tail is not None and isinstance(tail.get('session_id'), str):
                    result['session_ids'].append(tail['session_id'])
                continue
            result['complete_lines'] += 1
            raw_line = raw_line[:-1]
            if raw_line.endswith(b'\r'):
                raw_line = raw_line[:-1]
            try:
                payload = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                result['malformed'].append((line_number, str(exc)))
                continue
            # Identity is collected before validation so a malformed event still counts for conflicts.
            if isinstance(payload, dict) and isinstance(payload.get('session_id'), str):
                result['session_ids'].append(payload['session_id'])
            event, problem = _interpret_event(payload)
            if problem:
                result['malformed'].append((line_number, problem))
                continue
            event['index'] = len(result['events'])
            result['events'].append(event)
            if event['kind'] == 'init':
                result['api_key_sources'].append(event['api_key_source'])
            elif event['kind'] == 'result':
                result['results'].append(event)
                result['providers'].extend(event['providers'])
    return result


def _record_diagnostics(artifacts, scan):
    diagnostics = [
        'malformed Claude stream-json event on line {}: {}'.format(line, reason)
        for line, reason in scan['malformed']
    ]
    diagnostics.extend(
        'Claude error result: {}'.format(event['text'])
        for event in scan['results'] if event['is_error'] and event['text']
    )
    path = artifacts.get('stderr')
    if not diagnostics or not path or artifacts.get(_DIAGNOSTICS_KEY):
        return
    artifacts[_DIAGNOSTICS_KEY] = True
    try:
        with open(path, 'a', encoding='utf-8') as stream:
            for diagnostic in diagnostics:
                stream.write(diagnostic + '\n')
    except OSError:
        pass


def _expected_session_id(artifacts):
    expected = artifacts.get(_EXPECTED_SESSION_KEY)
    if expected:
        return expected
    # cmd_resume passes fresh artifacts for the parent, so recover the identity from argv.
    path = artifacts.get('invocation')
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            argv = json.load(stream).get('argv')
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(argv, list):
        return None
    for flag in ('--session-id', '--resume'):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
    return None


def _agreed_session(expected, session_ids):
    if expected and session_ids and all(session_id == expected for session_id in session_ids):
        return expected
    return None


def _write_response(path, text):
    """Replace path atomically with text; return (written, nonempty)."""
    if not path:
        return True, False
    temporary = path + '.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False, False
    return True, bool(text)


def _auth_environment():
    # Lazy import: lifecycle imports this package at load time, so a module-level import would cycle.
    from .. import lifecycle

    removed = set(_env_remove('sub')) | set(lifecycle.HOST_CONTEXT_KEYS)
    return {
        key: value for key, value in os.environ.items()
        if key not in removed and not key.startswith('RELAY_JOB_')
    }


def _login_state(cwd):
    """Return 'sub' for a subscription login, 'none' for any other readable state, or None."""
    try:
        completed = subprocess.run(
            ['claude', 'auth', 'status', '--json'], cwd=cwd, env=_auth_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=AUTH_STATUS_TIMEOUT,
        )
        payload = json.loads(completed.stdout.decode('utf-8'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(payload, dict):
        return None
    # Only the login state is read; the other auth fields (email, org) are never stored.
    if (payload.get('loggedIn') is True and payload.get('authMethod') == 'claude.ai'
            and payload.get('apiProvider') in (None, 'firstParty')):
        return 'sub'
    return 'none'


class ClaudeAdapter(Adapter):
    name = 'claude'

    def validate(self, options):
        if options.get('gateway'):
            raise ValueError('OpenCode-only flags --zen and --go cannot be used with --backend claude')
        route = options.get('route')
        if route not in (None, 'sub', 'api'):
            raise ValueError('Claude route must be sub or api: {}'.format(route))
        effort = options.get('effort')
        if effort is not None and effort not in EFFORTS:
            raise ValueError('Claude effort must be one of: {}'.format(', '.join(EFFORTS)))
        validated = dict(options)
        # validate runs again in the supervisor, so an assigned id must survive re-validation.
        if not options.get('parent_job_id') and not options.get('assigned_session_id'):
            validated['assigned_session_id'] = str(uuid.uuid4())
        return validated

    def resolve_launch(self, options):
        resolved = dict(options)
        cwd = resolved.get('cwd')
        route = resolved.get('route')
        if route:
            resolved['route_defaulted'] = False
            if route == 'sub' and _login_state(cwd) != 'sub':
                raise ValueError(
                    '--route sub requires a Claude subscription login (claude auth login)'
                )
            return resolved
        state = _login_state(cwd)
        if state is None:
            raise ValueError(
                'cannot read Claude login status to select a billing route; '
                'pass --route sub or --route api'
            )
        if state == 'sub':
            resolved['route'] = 'sub'
        elif os.environ.get('ANTHROPIC_API_KEY'):
            resolved['route'] = 'api'
        else:
            raise ValueError(
                'cannot select a Claude billing route: no subscription login was detected '
                'and ANTHROPIC_API_KEY is not set; pass --route sub or --route api'
            )
        resolved['route_defaulted'] = True
        return resolved

    def prepare(self, job, artifacts, resume_session_id=None):
        options = self.validate(job.get('options') or {})
        route = job.get('route') or options.get('route')
        if route not in CREDENTIAL_SOURCES:
            raise ValueError('Claude job has no resolved billing route')
        argv = [
            'claude', '-p', '--output-format', 'stream-json', '--verbose',
            '--permission-prompts', 'none',
        ]
        if resume_session_id:
            expected = resume_session_id
            argv.extend(('--resume', resume_session_id))
        else:
            expected = options.get('assigned_session_id')
            if not expected:
                raise ValueError('Claude fresh job has no assigned session id')
            argv.extend(('--session-id', expected))
        if job.get('write'):
            argv.extend(('--permission-mode', 'acceptEdits'))
        else:
            argv.extend((
                '--safe-mode', '--restricted', '--strict-mcp-config',
                '--tools', READ_ONLY_TOOLS, '--allowedTools', ','.join(READ_ONLY_GIT),
                '--disallowedTools', ','.join(READ_ONLY_GIT_DENY),
            ))
        if job.get('model'):
            argv.extend(('--model', job['model']))
        if job.get('effort'):
            argv.extend(('--effort', job['effort']))
        artifacts[_EXPECTED_SESSION_KEY] = expected
        artifacts[_ROUTE_KEY] = route
        env_remove = _env_remove(route)
        if not job.get('write'):
            env_remove += READ_ONLY_GIT_ENV_REMOVALS
        # The working directory is the recorded cwd for fresh and resumed runs alike, since
        # Claude keys its session store by directory.
        return Invocation(argv, job['cwd'], {}, artifacts['task'], env_remove)

    def observe_session(self, artifacts):
        scan = _scan_events(artifacts)
        return _agreed_session(_expected_session_id(artifacts), scan['session_ids'])

    def has_conflicting_session_identity(self, artifacts):
        expected = _expected_session_id(artifacts)
        if not expected:
            return False
        scan = _scan_events(artifacts)
        return any(session_id != expected for session_id in scan['session_ids'])

    def classify(self, exit_info, artifacts, attempt_result=None, attempt_history=None):
        scan = _scan_events(artifacts)
        _record_diagnostics(artifacts, scan)
        expected = _expected_session_id(artifacts)
        session_ids = scan['session_ids']
        if exit_info.spawn_error:
            return Outcome('failed', 'startup', None, False)
        if expected is not None and any(session_id != expected for session_id in session_ids):
            return Outcome('failed', 'session_identity_mismatch', None, False, clear_session_id=True)
        session = _agreed_session(expected, session_ids)
        if scan['truncated_tail']:
            return Outcome('failed', 'truncated_stream', session, False)
        if scan['complete_lines'] == 0:
            return Outcome('failed', 'startup', session, False)
        if scan['malformed']:
            return Outcome('failed', 'malformed_stream', session, False)
        route = artifacts.get(_ROUTE_KEY)
        if route in CREDENTIAL_SOURCES:
            sources = scan['api_key_sources']
            if all(source is None for source in sources):
                return Outcome('failed', 'credential_source_unreported', session, False)
            for source in sources:
                if source not in CREDENTIAL_SOURCES[route]:
                    reported = source if source is not None else 'missing'
                    return Outcome(
                        'failed', 'route_mismatch: expected {}, backend reported {}'.format(route, reported),
                        session, False,
                    )
            for provider in scan['providers']:
                if provider != 'firstParty':
                    reason = 'route_mismatch: expected firstParty provider, backend reported {}'
                    return Outcome('failed', reason.format(provider), session, False)
        results = scan['results']
        errors = [event for event in results if event['is_error'] or event['subtype'] != 'success']
        if errors:
            first = errors[0]
            reason = 'result_error: {}'.format(first['subtype'])
            if first['terminal_reason']:
                reason += ' ({})'.format(first['terminal_reason'])
            return Outcome('failed', reason, session, False)
        for event in results:
            reason = event['terminal_reason'] or ''
            if any(marker in reason.lower() for marker in TURN_OR_BUDGET_STOPS):
                return Outcome(
                    'failed', 'result_error: {} ({})'.format(event['subtype'], reason), session, False,
                )
        if exit_info.exit_signal is not None or exit_info.exit_code != 0:
            status = (
                'signal_{}'.format(exit_info.exit_signal)
                if exit_info.exit_signal is not None else
                str(exit_info.exit_code) if exit_info.exit_code is not None else 'unknown'
            )
            return Outcome('failed', 'nonzero_exit: {}'.format(status), session, False)
        if not session_ids:
            return Outcome('failed', 'missing_session_identity', None, False)
        if not results or _work_after_last_result(scan['events']):
            return Outcome('failed', 'missing_terminal_event', session, False)
        written, nonempty = _write_response(artifacts.get('response'), results[-1]['text'])
        if not written:
            # The session stays resumable: the stream itself was complete and consistent.
            return Outcome('failed', 'response_write_failed', expected, False)
        return Outcome('done', None, expected, nonempty)
