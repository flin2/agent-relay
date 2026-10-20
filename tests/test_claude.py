#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude adapter argv, route, environment, stream, and lifecycle tests."""

import itertools
import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'plugins/agent-relay/scripts'
JOB_PATH = SCRIPTS / 'relay-job.py'
sys.path.insert(0, str(SCRIPTS))

from relay import cli, lifecycle, store
from relay.adapters import ExitInfo, get_adapter
from relay.adapters import claude as claude_module
from relay.adapters.claude import ClaudeAdapter


BASE_ARGV = ['-p', '--output-format', 'stream-json', '--verbose', '--permission-prompts', 'none']
READ_ONLY_GIT_RULES = (
    'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)', 'Bash(git show:*)',
    'Bash(git rev-parse:*)', 'Bash(git ls-files:*)', 'Bash(git merge-base:*)',
    'Bash(git branch --show-current)', 'Bash(git blame:*)',
)
READ_ONLY_GIT_DENY_RULES = (
    'Bash(git *--output*)', 'Bash(git *--ext-diff*)', 'Bash(git *--textconv*)',
    'Bash(git *--no-index*)', 'Bash(git *--open-files-in-pager*)', 'Bash(git *>*)',
    'Bash(git *|*)', 'Bash(git *;*)', 'Bash(git *&*)', 'Bash(git *$(*)', 'Bash(git *`*)',
)
GIT_HELPER_ENV = (
    'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'PAGER', 'GIT_EDITOR', 'GIT_SSH', 'GIT_SSH_COMMAND',
    'GIT_ASKPASS', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
)
READ_ONLY_ARGV = [
    '--safe-mode', '--restricted', '--strict-mcp-config',
    '--tools', 'Read,Grep,Glob,Bash',
    '--allowedTools', ','.join(READ_ONLY_GIT_RULES),
    '--disallowedTools', ','.join(READ_ONLY_GIT_DENY_RULES),
]
FORBIDDEN_FLAGS = (
    '--continue', '--fork-session', '--no-session-persistence', '--background',
    '--dangerously-skip-permissions', '--bare',
)
RESPONSE_TEXT = 'claude fixture final response\n'
# Names the stub snapshots from its own environment; each one is checked by a test.
ENV_NAMES = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_PID', 'CLAUDE_EFFORT',
    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_EXECPATH',
    'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_SAFE_MODE', 'CLAUDE_CODE_SIMPLE', 'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
    'CODEX_HOME', 'OPENAI_API_KEY', 'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID',
    'RELAY_JOB_STATE_DIR', 'RELAY_JOB_CUSTOM', 'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'PAGER',
    'GIT_CONFIG_PARAMETERS',
)
# Variables stripped from the inherited environment so a host Claude or Codex session
# cannot influence the fixtures.
STRIPPED_PREFIXES = ('CLAUDE', 'ANTHROPIC_', 'RELAY_JOB_', 'CODEX_', 'PLUGIN_DATA')


CLAUDE_STUB = r'''#!__PYTHON__
import json
import os
import pathlib
import sys
import time

ENV_NAMES = __ENV_NAMES__
RESPONSE = 'claude fixture final response\n'
READ_ONLY_TOKENS = (
    '--safe-mode', '--restricted', '--strict-mcp-config', '--tools', 'Read,Grep,Glob,Bash',
    '--allowedTools', '--disallowedTools',
)
FORBIDDEN = (
    '--continue', '--fork-session', '--no-session-persistence', '--background',
    '--dangerously-skip-permissions', '--bare', '--route', '--zen', '--go',
)


def snapshot_env():
    return {name: os.environ.get(name, '<unset>') for name in ENV_NAMES}


def fail(message):
    sys.stderr.write('Claude fixture argv contract failure: ' + message + '\n')
    sys.stderr.flush()
    raise SystemExit(91)


def emit(event):
    sys.stdout.write(json.dumps(event, separators=(',', ':')) + '\n')
    sys.stdout.flush()


args = sys.argv[1:]
if args[:1] == ['auth']:
    capture = os.environ.get('CLAUDE_STUB_AUTH_CAPTURE')
    if capture:
        pathlib.Path(capture).write_text(
            json.dumps({'argv': args, 'env': snapshot_env()}, sort_keys=True), encoding='utf-8',
        )
    if args != ['auth', 'status', '--json']:
        fail('unexpected auth invocation: {!r}'.format(args))
    mode = os.environ.get('CLAUDE_STUB_AUTH', 'logged-out')
    if mode == 'bad-json':
        print('{"loggedIn": ')
    elif mode == 'list':
        print('[]')
    elif mode == 'garbage-exit':
        print('not json')
        raise SystemExit(3)
    elif mode == 'sub':
        print(json.dumps({
            'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
            'email': 'fixture@example.invalid', 'orgName': 'fixture-org',
        }))
    elif mode == 'api-only':
        print(json.dumps({'loggedIn': False, 'authMethod': 'api_key', 'apiProvider': 'firstParty'}))
    else:
        print(json.dumps({'loggedIn': False, 'authMethod': 'none', 'apiProvider': 'firstParty'}))
    raise SystemExit(0)

if any(arg in FORBIDDEN or arg.startswith('--dangerously-') for arg in args):
    fail('forbidden option in {!r}'.format(args))
if args[:len(__BASE_ARGV__)] != __BASE_ARGV__:
    fail('expected stream-json prefix, got {!r}'.format(args))
session_flags = [flag for flag in ('--session-id', '--resume') if flag in args]
if len(session_flags) != 1 or args.index(session_flags[0]) + 1 >= len(args):
    fail('expected exactly one session flag with a value in {!r}'.format(args))
flag = session_flags[0]
session_id = args[args.index(flag) + 1]
if '--permission-mode' in args:
    index = args.index('--permission-mode')
    if args[index + 1:index + 2] != ['acceptEdits']:
        fail('write run must use acceptEdits in {!r}'.format(args))
    if '--disallowedTools' in args or '--allowedTools' in args:
        fail('write run must not carry read-only tool rules in {!r}'.format(args))
    mode = 'write'
else:
    missing = [token for token in READ_ONLY_TOKENS if token not in args]
    if missing:
        fail('read-only run is missing {!r}'.format(missing))
    mode = 'read-only'

task = sys.stdin.read()
capture_path = os.environ.get('CLAUDE_STUB_CAPTURE')
if capture_path:
    pathlib.Path(capture_path).write_text(json.dumps({
        'argv': args, 'cwd': os.getcwd(), 'mode': mode, 'flag': flag,
        'session_id': session_id, 'stdin': task, 'env': snapshot_env(),
        'relay_job_keys': sorted(key for key in os.environ if key.startswith('RELAY_JOB_')),
    }, sort_keys=True), encoding='utf-8')

scenario = os.environ.get('CLAUDE_STUB_SCENARIO', 'success')
source = os.environ.get('CLAUDE_STUB_API_KEY_SOURCE')
if source is None:
    source = 'ANTHROPIC_API_KEY' if os.environ.get('ANTHROPIC_API_KEY') else 'none'
foreign = os.environ.get('CLAUDE_STUB_FOREIGN_SESSION', 'foreign-claude-session')


def init(identity, api_source=None):
    return {
        'type': 'system', 'subtype': 'init', 'session_id': identity,
        'apiKeySource': api_source or source, 'permissionMode': 'default',
        'tools': ['Read', 'Grep', 'Glob', 'Bash'],
    }


def assistant(identity, text='working'):
    return {
        'type': 'assistant', 'session_id': identity,
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]},
    }


def rate_limit(identity):
    return {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}, 'session_id': identity}


def result(identity, text=RESPONSE, subtype='success', is_error=False, terminal_reason='completed',
           provider='firstParty'):
    return {
        'type': 'result', 'subtype': subtype, 'is_error': is_error, 'result': text,
        'terminal_reason': terminal_reason, 'session_id': identity, 'api_error_status': None,
        'modelUsage': {'fixture-model': {'provider': provider, 'inputTokens': 1}},
    }


if scenario == 'success':
    emit(init(session_id))
    emit(rate_limit(session_id))
    emit({'type': 'future_event', 'payload': 'ignored'})
    emit(assistant(session_id))
    emit(result(session_id))
elif scenario == 'error_result':
    emit(init(session_id))
    emit(assistant(session_id))
    emit(result(session_id, 'fixture error text', 'error_max_turns', True, 'max_turns'))
elif scenario == 'nonzero':
    emit(init(session_id))
    emit(assistant(session_id))
    emit(result(session_id))
    raise SystemExit(7)
elif scenario == 'missing_result':
    emit(init(session_id))
    emit(assistant(session_id))
elif scenario == 'malformed':
    emit(init(session_id))
    sys.stdout.write('{not-json}\n')
    sys.stdout.flush()
    emit(result(session_id))
elif scenario == 'truncated':
    emit(init(session_id))
    sys.stdout.write('{"type":"result","subtype":"succ')
    sys.stdout.flush()
elif scenario == 'delayed':
    delay = float(os.environ.get('CLAUDE_STUB_DELAY', '1.0'))
    emit(init(session_id))
    time.sleep(delay)
    emit(assistant(session_id))
    time.sleep(delay)
    emit(result(session_id))
elif scenario == 'high_volume':
    emit(init(session_id))
    padding = 'x' * 2048
    for _ in range(int(os.environ.get('CLAUDE_STUB_LINES', '1500'))):
        emit(assistant(session_id, padding))
    emit(result(session_id))
elif scenario == 'identity_mismatch':
    emit(init(foreign))
    emit(assistant(foreign))
    emit(result(foreign))
elif scenario == 'late_conflict':
    emit(init(session_id))
    emit(assistant(session_id))
    barrier = pathlib.Path(os.environ.get('CLAUDE_STUB_BARRIER', '/nonexistent-barrier'))
    deadline = time.monotonic() + 15
    while not barrier.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not barrier.exists():
        fail('test did not release the late-conflict barrier')
    emit(assistant(foreign))
    if os.environ.get('CLAUDE_STUB_HOLD') == '1':
        while True:
            time.sleep(1)
    emit(result(session_id))
elif scenario == 'apikey_source':
    emit(init(session_id, 'ANTHROPIC_API_KEY'))
    emit(assistant(session_id))
    emit(result(session_id))
elif scenario == 'no_init':
    emit(assistant(session_id))
    emit(result(session_id))
elif scenario == 'error_then_success':
    emit(init(session_id))
    emit(assistant(session_id))
    emit(result(session_id, 'fixture error text', 'error_max_turns', True, 'max_turns'))
    emit(result(session_id))
elif scenario == 'success_then_assistant':
    emit(init(session_id))
    emit(result(session_id))
    emit(assistant(session_id, 'work after completion'))
elif scenario == 'nonstring_result':
    emit(init(session_id))
    event = result(session_id)
    event['result'] = 42
    emit(event)
elif scenario == 'result_without_session':
    emit(init(session_id))
    event = result(session_id)
    del event['session_id']
    emit(event)
elif scenario == 'malformed_foreign':
    emit(init(session_id))
    emit({'type': [1], 'session_id': foreign})
    emit(result(session_id))
elif scenario == 'bedrock_provider':
    emit(init(session_id))
    emit(assistant(session_id))
    emit(result(session_id, provider='bedrock'))
elif scenario == 'truncated_foreign':
    emit(init(session_id))
    emit(assistant(session_id))
    sys.stdout.write(json.dumps(assistant(foreign)))
    sys.stdout.flush()
elif scenario == 'mixed_init':
    emit(init(session_id, 'none'))
    emit(init(session_id, 'ANTHROPIC_API_KEY'))
    emit(assistant(session_id))
    emit(result(session_id))
elif scenario == 'init_missing_source':
    emit(init(session_id))
    late = init(session_id)
    del late['apiKeySource']
    emit(late)
    emit(assistant(session_id))
    emit(result(session_id))
elif scenario == 'max_turns_success':
    emit(init(session_id))
    emit(assistant(session_id))
    emit(result(session_id, RESPONSE, 'success', False, 'max_turns'))
elif scenario == 'slow':
    emit(init(session_id))
    emit(assistant(session_id))
    while True:
        time.sleep(1)
else:
    fail('unknown scenario: ' + scenario)
raise SystemExit(0)
'''.replace('__PYTHON__', sys.executable).replace(
    '__ENV_NAMES__', repr(ENV_NAMES),
).replace('__BASE_ARGV__', repr(BASE_ARGV))


SESSION = 'session-expected'


def init_event(session=SESSION, source='none'):
    return {'type': 'system', 'subtype': 'init', 'session_id': session, 'apiKeySource': source}


def result_event(session=SESSION, **fields):
    event = {
        'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'unit answer',
        'session_id': session, 'terminal_reason': 'completed',
    }
    event.update(fields)
    return event


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        self.work = tempfile.TemporaryDirectory(prefix='relay-claude-unit-', dir=scratch)
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)

    def job(self, route, options=None, **fields):
        cwd = str(self.root / 'cwd')
        base = {'backend': 'claude', 'route': route, 'cwd': cwd, 'workspace': cwd}
        base.update(options or {})
        job = {'cwd': cwd, 'workspace': cwd, 'route': route, 'write': False, 'options': base}
        job.update(fields)
        return job

    def artifacts(self):
        return {
            'task': str(self.root / 'task.txt'),
            'response': str(self.root / 'response.txt'),
            'stdout': str(self.root / 'stdout.log'),
            'stderr': str(self.root / 'stderr.log'),
        }

    def test_registered_adapter_builds_exact_fresh_resumed_and_write_argv(self):
        adapter = get_adapter('claude')
        self.assertIsInstance(adapter, ClaudeAdapter)
        options = adapter.validate({
            'backend': 'claude', 'route': 'sub', 'cwd': str(self.root / 'cwd'),
            'model': 'model with spaces', 'effort': 'high',
        })
        session = options['assigned_session_id']
        job = self.job('sub', options, model='model with spaces', effort='high')

        fresh = adapter.prepare(job, self.artifacts())
        self.assertEqual(fresh.argv, ['claude'] + BASE_ARGV + [
            '--session-id', session,
        ] + READ_ONLY_ARGV + ['--model', 'model with spaces', '--effort', 'high'])
        self.assertEqual(fresh.cwd, str(self.root / 'cwd'))
        self.assertEqual(fresh.env_overrides, {})
        self.assertEqual(fresh.stdin_path, str(self.root / 'task.txt'))
        self.assertIn('CLAUDECODE', fresh.env_remove)
        self.assertIn('ANTHROPIC_API_KEY', fresh.env_remove)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', fresh.env_remove)

        resumed_artifacts = self.artifacts()
        resumed_job = self.job('sub', dict(options, parent_job_id='parent-fixture'),
                               model='model with spaces', effort='high', parent_job_id='parent-fixture')
        resumed = adapter.prepare(resumed_job, resumed_artifacts, 'session-exact')
        self.assertEqual(resumed.argv, ['claude'] + BASE_ARGV + [
            '--resume', 'session-exact',
        ] + READ_ONLY_ARGV + ['--model', 'model with spaces', '--effort', 'high'])
        self.assertEqual(resumed.cwd, str(self.root / 'cwd'))
        self.assertEqual(resumed_artifacts['_claude_expected_session_id'], 'session-exact')
        self.assertEqual(resumed_artifacts['_claude_route'], 'sub')

        write_artifacts = self.artifacts()
        write_job = self.job('api', options, write=True)
        write = adapter.prepare(write_job, write_artifacts)
        self.assertEqual(write.argv, ['claude'] + BASE_ARGV + [
            '--session-id', session, '--permission-mode', 'acceptEdits',
        ])
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN', write.env_remove)
        self.assertNotIn('ANTHROPIC_API_KEY', write.env_remove)

    def test_forbidden_flags_never_appear_in_any_argv_shape(self):
        adapter = ClaudeAdapter()
        for route in ('sub', 'api'):
            for write in (False, True):
                for resume in (None, 'session-resume'):
                    options = adapter.validate({'route': route, 'cwd': str(self.root)})
                    job = self.job(route, options, write=write)
                    with self.subTest(route=route, write=write, resume=resume):
                        argv = adapter.prepare(job, self.artifacts(), resume).argv
                        for flag in FORBIDDEN_FLAGS:
                            self.assertNotIn(flag, argv)
                        self.assertFalse(any(arg.startswith('--dangerously-') for arg in argv))

    def test_validate_rejects_bad_controls_and_keeps_assigned_session_stable(self):
        adapter = ClaudeAdapter()
        with self.assertRaisesRegex(ValueError, 'OpenCode-only flags --zen and --go'):
            adapter.validate({'gateway': 'zen'})
        with self.assertRaisesRegex(ValueError, 'Claude route must be sub or api'):
            adapter.validate({'route': 'codex'})
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            self.assertEqual(adapter.validate({'effort': effort})['effort'], effort)
        with self.assertRaisesRegex(ValueError, 'low, medium, high, xhigh, max'):
            adapter.validate({'effort': 'ultra'})

        first = adapter.validate({'route': 'sub'})
        self.assertTrue(first['assigned_session_id'])
        self.assertEqual(adapter.validate(first)['assigned_session_id'], first['assigned_session_id'])
        resumed = adapter.validate({'route': 'sub', 'parent_job_id': 'parent-fixture'})
        self.assertNotIn('assigned_session_id', resumed)

    def test_expected_identity_is_recovered_from_invocation_file_for_fresh_artifacts(self):
        invocation = self.root / 'invocation.json'
        invocation.write_text(json.dumps({'argv': ['claude', '-p', '--resume', 'resume-id']}), encoding='utf-8')
        stdout = self.root / 'stdout.log'
        stdout.write_text(
            json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'resume-id'}) + '\n'
            + json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'other-id'}) + '\n',
            encoding='utf-8',
        )
        artifacts = {'stdout': str(stdout), 'invocation': str(invocation)}
        adapter = ClaudeAdapter()
        self.assertTrue(adapter.has_conflicting_session_identity(artifacts))
        self.assertIsNone(adapter.observe_session(artifacts))

        stdout.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'resume-id'}) + '\n')
        self.assertFalse(adapter.has_conflicting_session_identity(artifacts))
        self.assertEqual(adapter.observe_session(artifacts), 'resume-id')

    def test_unknown_events_are_tolerated_and_success_writes_private_response(self):
        artifacts = self.artifacts()
        lines = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'session-unit', 'apiKeySource': 'none'},
            {'type': 'rate_limit_event', 'session_id': 'session-unit'},
            {'type': 'stream_event', 'event': {}, 'session_id': 'session-unit'},
            {'type': 'future.thing', 'payload': 'ignored'},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'unit answer',
             'session_id': 'session-unit', 'terminal_reason': 'completed'},
        ]
        pathlib.Path(artifacts['stdout']).write_text(
            ''.join(json.dumps(line) + '\n' for line in lines), encoding='utf-8',
        )
        adapter = ClaudeAdapter()
        adapter.prepare(self.job('sub', {'route': 'sub'}), artifacts, 'session-unit')
        outcome = adapter.classify(ExitInfo(exit_code=0), artifacts)
        self.assertEqual((outcome.state, outcome.reason, outcome.session_id), ('done', None, 'session-unit'))
        self.assertTrue(outcome.final_response_written)
        self.assertEqual(pathlib.Path(artifacts['response']).read_text(encoding='utf-8'), 'unit answer')
        self.assertEqual(stat.S_IMODE(os.stat(artifacts['response']).st_mode), 0o600)

    def test_empty_success_result_is_done_without_a_written_response(self):
        artifacts = self.artifacts()
        adapter = ClaudeAdapter()
        adapter.prepare(self.job('sub', {'route': 'sub'}), artifacts, 'session-empty')
        pathlib.Path(artifacts['stdout']).write_text(
            json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'session-empty',
                        'apiKeySource': 'none'}) + '\n'
            + json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '',
                          'session_id': 'session-empty'}) + '\n',
            encoding='utf-8',
        )
        outcome = adapter.classify(ExitInfo(exit_code=0), artifacts)
        self.assertEqual(outcome.state, 'done')
        self.assertFalse(outcome.final_response_written)

    def classify_stream(self, lines, *, route='sub', expected=SESSION, artifacts=None):
        artifacts = artifacts or self.artifacts()
        artifacts['_claude_expected_session_id'] = expected
        artifacts['_claude_route'] = route
        text = ''.join(
            line if isinstance(line, str) else json.dumps(line) + '\n' for line in lines
        )
        pathlib.Path(artifacts['stdout']).write_text(text, encoding='utf-8')
        return ClaudeAdapter().classify(ExitInfo(exit_code=0), artifacts), artifacts

    def test_read_only_argv_carries_the_deny_list_and_no_git_grep(self):
        adapter = ClaudeAdapter()
        options = adapter.validate({'route': 'sub', 'cwd': str(self.root / 'cwd')})
        read_only = adapter.prepare(self.job('sub', options), self.artifacts(), 'session-deny').argv
        index = read_only.index('--disallowedTools')
        self.assertEqual(read_only.index('--allowedTools') + 2, index)
        self.assertEqual(read_only[index + 1], ','.join(READ_ONLY_GIT_DENY_RULES))
        self.assertFalse(any('git grep' in arg for arg in read_only))
        write = adapter.prepare(self.job('api', options, write=True), self.artifacts(), None).argv
        self.assertNotIn('--disallowedTools', write)
        self.assertNotIn('--allowedTools', write)
        self.assertFalse(any('git grep' in arg for arg in write))

    def test_first_error_result_wins_over_a_later_success(self):
        outcome, _ = self.classify_stream([
            init_event(),
            result_event(subtype='error_max_turns', is_error=True, terminal_reason='max_turns'),
            result_event(),
        ])
        self.assertEqual((outcome.state, outcome.reason), ('failed', 'result_error: error_max_turns (max_turns)'))

    def test_work_after_the_terminal_result_is_missing_terminal_evidence(self):
        outcome, _ = self.classify_stream([
            init_event(), result_event(), {'type': 'assistant', 'session_id': SESSION, 'message': {}},
        ])
        self.assertEqual((outcome.state, outcome.reason), ('failed', 'missing_terminal_event'))
        allowed, _ = self.classify_stream([
            init_event(), result_event(),
            {'type': 'rate_limit_event', 'session_id': SESSION},
            {'type': 'future.thing'},
        ])
        self.assertEqual(allowed.state, 'done')

    def test_non_string_result_and_result_without_session_are_malformed(self):
        outcome, artifacts = self.classify_stream([init_event(), result_event(result=42)])
        self.assertEqual((outcome.state, outcome.reason), ('failed', 'malformed_stream'))
        self.assertIn('result event has a non-string result', pathlib.Path(artifacts['stderr']).read_text())
        event = result_event()
        del event['session_id']
        outcome, artifacts = self.classify_stream([init_event(), event])
        self.assertEqual((outcome.state, outcome.reason), ('failed', 'malformed_stream'))
        self.assertIn('result event has no string session_id', pathlib.Path(artifacts['stderr']).read_text())

    def test_malformed_line_with_foreign_identity_is_still_a_conflict(self):
        outcome, artifacts = self.classify_stream([
            init_event(), '{"type":[1],"session_id":"session-foreign"}\n', result_event(),
        ])
        self.assertEqual(outcome.reason, 'session_identity_mismatch')
        self.assertTrue(outcome.clear_session_id)
        self.assertIsNone(outcome.session_id)
        adapter = ClaudeAdapter()
        self.assertTrue(adapter.has_conflicting_session_identity(artifacts))
        self.assertIsNone(adapter.observe_session(artifacts))

    def test_non_first_party_provider_is_a_route_mismatch_and_absent_evidence_is_allowed(self):
        outcome, _ = self.classify_stream([
            init_event(source='ANTHROPIC_API_KEY'),
            result_event(modelUsage={'fixture-model': {'provider': 'bedrock'}}),
        ], route='api')
        self.assertEqual(
            (outcome.state, outcome.reason),
            ('failed', 'route_mismatch: expected firstParty provider, backend reported bedrock'),
        )
        for model_usage in (None, 'not-a-dict', {'fixture-model': 'not-a-dict'}, {'fixture-model': {}}):
            with self.subTest(model_usage=model_usage):
                outcome, _ = self.classify_stream([
                    init_event(source='ANTHROPIC_API_KEY'), result_event(modelUsage=model_usage),
                ], route='api')
                self.assertEqual(outcome.state, 'done')

    def test_response_write_failure_is_reported_and_keeps_the_session(self):
        artifacts = self.artifacts()
        pathlib.Path(artifacts['response']).mkdir()
        outcome, artifacts = self.classify_stream(
            [init_event(), result_event()], artifacts=artifacts,
        )
        self.assertEqual(
            (outcome.state, outcome.reason, outcome.session_id, outcome.final_response_written),
            ('failed', 'response_write_failed', SESSION, False),
        )
        self.assertFalse(pathlib.Path(artifacts['response'] + '.tmp').exists())

    def test_failed_replace_keeps_the_previous_response_and_removes_the_temporary_file(self):
        artifacts = self.artifacts()
        pathlib.Path(artifacts['response']).write_text('previous answer', encoding='utf-8')
        with mock.patch.object(os, 'replace', side_effect=OSError('fixture replace failure')):
            outcome, artifacts = self.classify_stream(
                [init_event(), result_event(result='new answer')], artifacts=artifacts,
            )
        self.assertEqual(outcome.reason, 'response_write_failed')
        self.assertEqual(pathlib.Path(artifacts['response']).read_text(encoding='utf-8'), 'previous answer')
        self.assertFalse(pathlib.Path(artifacts['response'] + '.tmp').exists())

    def test_read_only_jobs_drop_git_helper_environment_and_write_jobs_keep_it(self):
        adapter = ClaudeAdapter()
        options = adapter.validate({'route': 'sub', 'cwd': str(self.root / 'cwd')})
        read_only = adapter.prepare(self.job('sub', options), self.artifacts(), 'session-git').env_remove
        write = adapter.prepare(self.job('sub', options, write=True), self.artifacts(), None).env_remove
        for name in GIT_HELPER_ENV:
            with self.subTest(name=name):
                self.assertIn(name, read_only)
                self.assertNotIn(name, write)

    def test_truncated_tail_identity_is_a_conflict_but_partial_text_is_not(self):
        outcome, artifacts = self.classify_stream([
            init_event(), '{"type":"assistant","session_id":"session-foreign"}',
        ])
        self.assertEqual((outcome.reason, outcome.clear_session_id), ('session_identity_mismatch', True))
        self.assertTrue(ClaudeAdapter().has_conflicting_session_identity(artifacts))
        outcome, _ = self.classify_stream([init_event(), '{"type":"assistant","session_id":"session-expec'])
        self.assertEqual((outcome.reason, outcome.session_id), ('truncated_stream', SESSION))

    def test_every_init_event_must_report_a_permitted_credential_source(self):
        missing = init_event()
        del missing['apiKeySource']
        cases = (
            ([init_event(source='none'), init_event(source='ANTHROPIC_API_KEY')],
             'route_mismatch: expected sub, backend reported ANTHROPIC_API_KEY'),
            ([init_event(source='none'), missing],
             'route_mismatch: expected sub, backend reported missing'),
            ([missing], 'credential_source_unreported'),
        )
        for inits, reason in cases:
            with self.subTest(reason=reason):
                outcome, _ = self.classify_stream(inits + [result_event()])
                self.assertEqual((outcome.state, outcome.reason), ('failed', reason))

    def test_turn_and_budget_stops_fail_even_when_the_subtype_is_success(self):
        for reason in ('max_turns', 'Budget_Exceeded', 'max_tokens'):
            with self.subTest(reason=reason):
                outcome, _ = self.classify_stream([init_event(), result_event(terminal_reason=reason)])
                self.assertEqual(
                    (outcome.state, outcome.reason),
                    ('failed', 'result_error: success ({})'.format(reason)),
                )
        outcome, _ = self.classify_stream([init_event(), result_event(terminal_reason='completed')])
        self.assertEqual(outcome.state, 'done')

    def login_state(self, *, stdout=b'', returncode=0, side_effect=None):
        with mock.patch.object(claude_module.subprocess, 'run') as run:
            if side_effect is not None:
                run.side_effect = side_effect
            else:
                run.return_value = subprocess.CompletedProcess(['claude'], returncode, stdout=stdout)
            return claude_module._login_state(str(self.root))

    def test_login_state_is_sub_none_or_unavailable(self):
        def status(**fields):
            return json.dumps(fields).encode('utf-8')

        self.assertEqual(self.login_state(stdout=status(
            loggedIn=True, authMethod='claude.ai', apiProvider='firstParty')), 'sub')
        self.assertEqual(self.login_state(stdout=status(
            loggedIn=True, authMethod='claude.ai', apiProvider=None)), 'sub')
        self.assertEqual(self.login_state(stdout=status(
            loggedIn=False, authMethod='none')), 'none')
        self.assertEqual(self.login_state(stdout=status(
            loggedIn=True, authMethod='claude.ai', apiProvider='bedrock')), 'none')
        self.assertEqual(self.login_state(stdout=status(loggedIn=False), returncode=1), 'none')
        for name, kwargs in (
            ('non-json', {'stdout': b'not json'}),
            ('non-object', {'stdout': b'[]'}),
            ('invalid utf-8', {'stdout': b'\xff\xfe'}),
            ('nonzero exit with unparseable output', {'stdout': b'oops', 'returncode': 3}),
            ('timeout', {'side_effect': subprocess.TimeoutExpired(['claude'], 30)}),
            ('missing binary', {'side_effect': FileNotFoundError('claude')}),
        ):
            with self.subTest(name):
                self.assertIsNone(self.login_state(**kwargs))

    def test_auth_probe_environment_drops_relay_and_host_context(self):
        leaked = {
            'PATH': '/fixture/bin', 'CLAUDE_CODE_SESSION_ID': 'foreign-claude-session',
            'CODEX_THREAD_ID': 'foreign-thread', 'CODEX_SESSION_ID': 'foreign-session',
            'RELAY_JOB_CONVERSATION_ID': 'foreign-conversation', 'RELAY_JOB_STATE_DIR': '/foreign',
            'CLAUDE_PLUGIN_DATA': '/foreign', 'CLAUDECODE': '1', 'ANTHROPIC_API_KEY': 'fixture-api-key',
        }
        with mock.patch.dict(os.environ, leaked):
            environment = claude_module._auth_environment()
        for name in leaked:
            if name != 'PATH':
                self.assertNotIn(name, environment)
        self.assertEqual(environment['PATH'], '/fixture/bin')

    def test_environment_removal_sets_match_the_route_policy(self):
        adapter = ClaudeAdapter()
        sub = adapter.prepare(self.job('sub', {'route': 'sub'}), self.artifacts(), 'session-sub').env_remove
        api = adapter.prepare(self.job('api', {'route': 'api'}), self.artifacts(), 'session-api').env_remove
        for name in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ATTENDED',
                     'CLAUDE_CODE_MESSAGING_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
            self.assertIn(name, sub)
            self.assertIn(name, api)
        self.assertIn('ANTHROPIC_API_KEY', sub)
        self.assertNotIn('ANTHROPIC_API_KEY', api)
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN', api)
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', sub)
        self.assertIsInstance(sub, tuple)

    def test_prepare_refuses_a_job_without_a_resolved_route(self):
        with self.assertRaisesRegex(ValueError, 'no resolved billing route'):
            ClaudeAdapter().prepare(self.job(None, {}), self.artifacts())


class ClaudeLifecycleTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if scratch:
            pathlib.Path(scratch).mkdir(parents=True, exist_ok=True)
        self.work = tempfile.TemporaryDirectory(prefix='relay-claude-', dir=scratch)
        self.root = pathlib.Path(self.work.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.state = self.root / 'state'
        self.tmp_dir = self.root / 'tmp'
        self.tmp_dir.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin_dir = self.root / 'bin'
        self.bin_dir.mkdir()
        self.captures = self.root / 'captures'
        self.captures.mkdir()
        self.claude = self.bin_dir / 'claude'
        self.claude.write_text(CLAUDE_STUB, encoding='utf-8')
        self.claude.chmod(0o700)
        self.base_env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(STRIPPED_PREFIXES)
        }
        self.base_env.update({
            'HOME': str(self.home),
            'XDG_STATE_HOME': str(self.root / 'xdg'),
            'RELAY_JOB_STATE_DIR': str(self.state),
            'RELAY_JOB_HOST': 'claude',
            'RELAY_JOB_CONVERSATION_ID': 'claude-tests',
            'CLAUDE_STUB_AUTH': 'sub',
            'CLAUDE_STUB_AUTH_CAPTURE': str(self.root / 'auth-capture.json'),
            'TMPDIR': str(self.tmp_dir),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(self.bin_dir) + os.pathsep + os.environ.get('PATH', ''),
        })
        self.db = store.Store(str(self.state))
        self.pending = set()
        self.capture_paths = {}
        self.launch_stderr = {}
        self.counter = itertools.count(1)
        self.addCleanup(self.cleanup_work)

    def cleanup_work(self):
        failures = []
        for job_id in sorted(self.pending):
            try:
                job = self.db.get_job(job_id)
                if job and job['state'] not in ('done', 'failed', 'cancelled', 'crashed', 'ownership_unresolved'):
                    self.run_cli('cancel', job_id, '--timeout', '8', timeout=12)
                result = self.run_cli('wait', job_id, '--timeout', '10', '--interval', '0.05', timeout=12)
                current = self.db.get_job(job_id)
                if current and current['state'] not in ('done', 'failed', 'cancelled', 'crashed'):
                    failures.append('{} ended in {}'.format(job_id, current['state']))
                if result.returncode == 5:
                    failures.append('{} did not stop during cleanup'.format(job_id))
            except Exception as exc:
                failures.append('{} cleanup error: {}'.format(job_id, exc))
        # Wait for each supervisor to release its lock and exit before deleting its state root.
        for job_id in sorted(self.pending):
            job = self.db.get_job(job_id)
            if not job:
                continue
            lock = lifecycle.artifact_paths(job)['supervisor_lock']
            deadline = time.monotonic() + 40
            while (lifecycle._lock_state(lock) == lifecycle.LOCK_HELD
                   and time.monotonic() < deadline):
                time.sleep(0.05)
            if lifecycle._lock_state(lock) == lifecycle.LOCK_HELD:
                failures.append('{} supervisor still held its lock during cleanup'.format(job_id))
            pid = job.get('supervisor_pid')
            while pid and time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.05)
        for attempt in range(5):
            try:
                self.work.cleanup()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        if failures:
            raise AssertionError('Claude lifecycle cleanup failed: {}'.format('; '.join(failures)))

    def run_cli(self, *args, env=None, input_text=None, timeout=20):
        return subprocess.run(
            [sys.executable, str(JOB_PATH)] + list(args),
            cwd=str(self.workspace), env=env or self.base_env,
            input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )

    def environment(self, scenario, updates=None):
        capture = self.captures / 'capture-{}.json'.format(next(self.counter))
        env = dict(self.base_env, CLAUDE_STUB_SCENARIO=scenario, CLAUDE_STUB_CAPTURE=str(capture))
        env.update(updates or {})
        return env

    def track(self, job_id, env, result):
        self.pending.add(job_id)
        self.capture_paths[job_id] = env['CLAUDE_STUB_CAPTURE']
        self.launch_stderr[job_id] = result.stderr
        return job_id

    def launch(self, scenario='success', *, route='sub', write=False, model=None, effort=None,
               env_updates=None, task='fixture task\n', directory=None):
        env = self.environment(scenario, env_updates)
        args = ['run', '--backend', 'claude', '--dir', str(directory or self.workspace)]
        if route:
            args.extend(('--route', route))
        if model is not None:
            args.extend(('--model', model))
        if effort is not None:
            args.extend(('--effort', effort))
        if write:
            args.append('--write')
        args.append('--stdin')
        result = self.run_cli(*args, env=env, input_text=task)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.track(result.stdout.strip(), env, result)

    def resume(self, parent_id, *, scenario='success', env_updates=None, task='follow-up task\n',
               model=None, effort=None, write=False):
        env = self.environment(scenario, env_updates)
        args = ['resume', parent_id]
        if model is not None:
            args.extend(('--model', model))
        if effort is not None:
            args.extend(('--effort', effort))
        if write:
            args.append('--write')
        args.append('--stdin')
        result = self.run_cli(*args, env=env, input_text=task)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.track(result.stdout.strip(), env, result)

    def wait(self, job_id, timeout=12):
        result = self.run_cli(
            'wait', job_id, '--timeout', str(timeout), '--interval', '0.05', timeout=timeout + 5,
        )
        job = self.db.get_job(job_id)
        if job and job['state'] in ('done', 'failed', 'cancelled', 'crashed'):
            self.pending.discard(job_id)
        return result, job

    def assert_failed(self, job_id, reason):
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 2, '{} job={!r}'.format(waited.stderr, job))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['failure_reason'], reason, 'job={!r}'.format(job))
        return job

    def capture(self, job_id):
        return json.loads(pathlib.Path(self.capture_paths[job_id]).read_text(encoding='utf-8'))

    def auth_capture(self):
        return json.loads((self.root / 'auth-capture.json').read_text(encoding='utf-8'))

    def assigned_session(self, job_id):
        return self.db.get_job(job_id)['options']['assigned_session_id']

    def claim_for(self, job_id):
        with self.db.transaction() as conn:
            return conn.execute(
                'SELECT backend_session_id FROM session_claims WHERE job_id=?', (job_id,),
            ).fetchone()

    def wait_for_claim(self, job_id, session_id):
        deadline = time.monotonic() + 10
        claim = None
        while time.monotonic() < deadline:
            claim = self.claim_for(job_id)
            if claim:
                break
            time.sleep(0.01)
        self.assertIsNotNone(claim, 'supervisor did not claim the first session identity')
        self.assertEqual(claim['backend_session_id'], session_id)

    def wait_for_stdout(self, job_id, text, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stdout = pathlib.Path(self.db.get_job(job_id)['stdout_path']).read_text(encoding='utf-8')
            if text in stdout:
                return
            time.sleep(0.01)
        self.fail('Claude fixture did not emit {!r}'.format(text))

    def assert_resume_refused(self, job_id):
        before = len(self.db.list_jobs())
        refused = self.run_cli('resume', job_id, '--stdin', input_text='follow-up task')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('ambiguous backend session identity', refused.stderr)
        self.assertEqual(len(self.db.list_jobs()), before)

    def test_fresh_read_only_run_records_exact_argv_route_and_final_response(self):
        job_id = self.launch('success', route='sub', task='fixture task text\n')
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['route'], 'sub')
        self.assertFalse(job['options']['route_defaulted'])
        self.assertEqual(job['backend_session_id'], self.assigned_session(job_id))
        capture = self.capture(job_id)
        self.assertEqual(capture['argv'], BASE_ARGV + [
            '--session-id', self.assigned_session(job_id),
        ] + READ_ONLY_ARGV)
        self.assertEqual(capture['mode'], 'read-only')
        self.assertEqual(capture['cwd'], str(self.workspace))
        self.assertEqual(capture['stdin'], 'fixture task text\n')
        self.assertIn('(backend=claude, route=sub)', self.launch_stderr[job_id])
        result = self.run_cli('result', job_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, RESPONSE_TEXT)

    def test_fresh_write_run_uses_accept_edits_model_and_effort_on_api_route(self):
        job_id = self.launch(
            'success', route='api', write=True, model='claude-fixture-model', effort='high',
            env_updates={'ANTHROPIC_API_KEY': 'fixture-api-key'},
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertTrue(job['write'])
        capture = self.capture(job_id)
        self.assertEqual(capture['argv'], BASE_ARGV + [
            '--session-id', self.assigned_session(job_id), '--permission-mode', 'acceptEdits',
            '--model', 'claude-fixture-model', '--effort', 'high',
        ])
        self.assertEqual(capture['mode'], 'write')
        self.assertEqual(capture['env']['ANTHROPIC_API_KEY'], 'fixture-api-key')

    def test_resume_inherits_session_model_effort_and_resets_write_permission(self):
        parent = self.launch(
            'success', route='sub', write=True, model='claude-fixture-model', effort='high',
        )
        parent_wait, parent_job = self.wait(parent)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        child = self.resume(parent)
        child_wait, child_job = self.wait(child)
        self.assertEqual(child_wait.returncode, 0, child_wait.stderr)
        capture = self.capture(child)
        self.assertEqual(capture['argv'], BASE_ARGV + [
            '--resume', parent_job['backend_session_id'],
        ] + READ_ONLY_ARGV + ['--model', 'claude-fixture-model', '--effort', 'high'])
        self.assertEqual(capture['cwd'], str(self.workspace))
        self.assertEqual(capture['session_id'], parent_job['backend_session_id'])
        self.assertEqual(child_job['parent_job_id'], parent)
        self.assertEqual(child_job['backend_session_id'], parent_job['backend_session_id'])
        self.assertEqual((child_job['model'], child_job['effort']), ('claude-fixture-model', 'high'))
        self.assertFalse(child_job['write'])

    def test_resume_write_flag_is_an_explicit_permission_grant(self):
        parent = self.launch('success', route='sub')
        parent_wait, _ = self.wait(parent)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        child = self.resume(parent, write=True)
        child_wait, child_job = self.wait(child)
        self.assertEqual(child_wait.returncode, 0, child_wait.stderr)
        argv = self.capture(child)['argv']
        self.assertIn('--permission-mode', argv)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        self.assertNotIn('--safe-mode', argv)
        self.assertTrue(child_job['write'])

    def test_invalid_effort_and_zen_are_rejected_before_any_job_is_created(self):
        before = len(self.db.list_jobs())
        effort = self.run_cli(
            'run', '--backend', 'claude', '--route', 'sub', '--effort', 'ultra', '--stdin',
            input_text='task\n',
        )
        self.assertEqual(effort.returncode, 2)
        self.assertIn('Claude effort must be one of: low, medium, high, xhigh, max', effort.stderr)
        zen = self.run_cli('run', '--backend', 'claude', '--zen', '--stdin', input_text='task\n')
        self.assertEqual(zen.returncode, 2)
        self.assertIn('OpenCode-only flags --zen and --go cannot be used with --backend claude', zen.stderr)
        self.assertEqual(len(self.db.list_jobs()), before)

    def test_route_defaults_to_subscription_login_and_keeps_api_key_out_of_both_processes(self):
        job_id = self.launch(
            'success', route=None,
            env_updates={'ANTHROPIC_API_KEY': 'fixture-api-key', 'CLAUDE_STUB_AUTH': 'sub'},
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(job['route'], 'sub')
        self.assertTrue(job['options']['route_defaulted'])
        self.assertIn('launched detached: {} (backend=claude, route=sub (default))'.format(job_id),
                      self.launch_stderr[job_id])
        self.assertEqual(self.auth_capture()['env']['ANTHROPIC_API_KEY'], '<unset>')
        self.assertEqual(self.auth_capture()['env']['CLAUDECODE'], '<unset>')
        self.assertEqual(self.capture(job_id)['env']['ANTHROPIC_API_KEY'], '<unset>')

    def test_route_defaults_to_api_key_for_api_only_login_and_fails_on_unreadable_status(self):
        job_id = self.launch(
            'success', route=None,
            env_updates={
                'CLAUDE_STUB_AUTH': 'api-only',
                'ANTHROPIC_API_KEY': 'fixture-api-key',
                'CLAUDE_CODE_OAUTH_TOKEN': 'fixture-oauth',
            },
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual((job['route'], job['options']['route_defaulted']), ('api', True))
        self.assertIn('(backend=claude, route=api (default))', self.launch_stderr[job_id])
        capture = self.capture(job_id)
        self.assertEqual(capture['env']['ANTHROPIC_API_KEY'], 'fixture-api-key')
        self.assertEqual(capture['env']['CLAUDE_CODE_OAUTH_TOKEN'], '<unset>')

    def test_unreadable_login_status_fails_before_launch(self):
        empty = self.root / 'empty-bin'
        empty.mkdir(exist_ok=True)
        cases = (
            ('bad-json', dict(self.base_env, CLAUDE_STUB_AUTH='bad-json', ANTHROPIC_API_KEY='fixture-api-key')),
            ('non-object', dict(self.base_env, CLAUDE_STUB_AUTH='list', ANTHROPIC_API_KEY='fixture-api-key')),
            ('nonzero exit', dict(self.base_env, CLAUDE_STUB_AUTH='garbage-exit', ANTHROPIC_API_KEY='fixture-api-key')),
            ('missing binary', dict(self.base_env, PATH=str(empty), ANTHROPIC_API_KEY='fixture-api-key')),
        )
        for name, env in cases:
            with self.subTest(name):
                before = len(self.db.list_jobs())
                result = self.run_cli('run', '--backend', 'claude', '--stdin', env=env, input_text='task\n')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('cannot read Claude login status to select a billing route', result.stderr)
                self.assertEqual(len(self.db.list_jobs()), before)
        explicit = self.run_cli(
            'run', '--backend', 'claude', '--route', 'sub', '--stdin',
            env=dict(self.base_env, CLAUDE_STUB_AUTH='bad-json'), input_text='task\n',
        )
        self.assertEqual(explicit.returncode, 2)
        self.assertIn('--route sub requires a Claude subscription login', explicit.stderr)

    def test_auth_probe_environment_lacks_host_and_relay_context(self):
        job_id = self.launch('success', route=None, env_updates={
            'CLAUDE_CODE_SESSION_ID': 'foreign-claude-session',
            'CODEX_THREAD_ID': 'foreign-codex-thread',
            'CODEX_SESSION_ID': 'foreign-codex-session',
            'RELAY_JOB_CUSTOM': 'must-not-reach-probe',
        })
        waited, _ = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        env = self.auth_capture()['env']
        for name in (
            'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
            'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_HOST', 'RELAY_JOB_STATE_DIR', 'RELAY_JOB_CUSTOM',
        ):
            self.assertEqual(env[name], '<unset>', name)

    def test_read_only_job_drops_git_helper_environment_and_write_job_keeps_it(self):
        helpers = {'GIT_EXTERNAL_DIFF': 'fixture-diff', 'GIT_PAGER': 'fixture-pager', 'PAGER': 'fixture-pager'}
        read_only = self.launch('success', route='sub', env_updates=helpers)
        waited, _ = self.wait(read_only)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        env = self.capture(read_only)['env']
        self.assertEqual({name: env[name] for name in helpers}, {name: '<unset>' for name in helpers})
        write = self.launch('success', route='sub', write=True, env_updates=helpers)
        waited, _ = self.wait(write)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        env = self.capture(write)['env']
        self.assertEqual({name: env[name] for name in helpers}, helpers)

    def test_truncated_foreign_identity_fails_and_refuses_resume(self):
        job_id = self.launch(
            'truncated_foreign', env_updates={'CLAUDE_STUB_FOREIGN_SESSION': 'claude-truncated-foreign'},
        )
        job = self.assert_failed(job_id, 'session_identity_mismatch')
        self.assertIsNone(job['backend_session_id'])
        self.assertIsNone(self.claim_for(job_id))
        self.assert_resume_refused(job_id)

    def test_conflicting_init_sources_and_turn_stops_fail_the_job(self):
        self.assert_failed(
            self.launch('mixed_init', route='sub'),
            'route_mismatch: expected sub, backend reported ANTHROPIC_API_KEY',
        )
        self.assert_failed(
            self.launch('init_missing_source', route='sub'),
            'route_mismatch: expected sub, backend reported missing',
        )
        self.assert_failed(self.launch('max_turns_success'), 'result_error: success (max_turns)')

    def test_route_defaulting_and_explicit_sub_fail_without_a_billing_route(self):
        before = len(self.db.list_jobs())
        no_route = self.run_cli(
            'run', '--backend', 'claude', '--stdin', env=dict(
                self.base_env, CLAUDE_STUB_AUTH='logged-out'),
            input_text='task\n',
        )
        self.assertEqual(no_route.returncode, 2)
        self.assertIn('cannot select a Claude billing route', no_route.stderr)
        explicit = self.run_cli(
            'run', '--backend', 'claude', '--route', 'sub', '--stdin', env=dict(
                self.base_env, CLAUDE_STUB_AUTH='logged-out'),
            input_text='task\n',
        )
        self.assertEqual(explicit.returncode, 2)
        self.assertIn('--route sub requires a Claude subscription login (claude auth login)', explicit.stderr)
        self.assertEqual(len(self.db.list_jobs()), before)

    def test_explicit_api_route_does_not_run_the_auth_check(self):
        job_id = self.launch(
            'success', route='api',
            env_updates={'CLAUDE_STUB_AUTH': 'logged-out', 'ANTHROPIC_API_KEY': 'fixture-api-key'},
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertFalse(job['options']['route_defaulted'])
        self.assertFalse((self.root / 'auth-capture.json').exists())

    def test_sub_route_environment_strips_api_key_provider_overrides_and_host_context(self):
        job_id = self.launch(
            'success', route='sub',
            env_updates={
                'ANTHROPIC_API_KEY': 'fixture-api-key',
                'ANTHROPIC_AUTH_TOKEN': 'fixture-token',
                'ANTHROPIC_BASE_URL': 'https://fixture.invalid',
                'CLAUDE_CODE_USE_BEDROCK': '1',
                'CLAUDE_CODE_USE_VERTEX': '1',
                'CLAUDE_CODE_USE_FOUNDRY': '1',
                'CLAUDECODE': '1',
                'CLAUDE_CODE_ENTRYPOINT': 'cli',
                'CLAUDE_CODE_SESSION_ID': 'foreign-session',
                'CLAUDE_PLUGIN_DATA': '/fixture/claude-data',
                'PLUGIN_DATA': '/fixture/plugin-data',
                'CODEX_THREAD_ID': 'foreign-thread',
                'CLAUDE_CODE_OAUTH_TOKEN': 'fixture-oauth',
                'OPENAI_API_KEY': 'fixture-openai-key',
                'RELAY_JOB_CUSTOM': 'must-not-reach-backend',
            },
        )
        waited, _ = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        env = self.capture(job_id)['env']
        for name in (
            'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
            'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
            'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ID',
            'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA', 'CODEX_THREAD_ID', 'RELAY_JOB_HOST',
            'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR', 'RELAY_JOB_CUSTOM',
        ):
            self.assertEqual(env[name], '<unset>', name)
        self.assertEqual(self.capture(job_id)['relay_job_keys'], [])
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'fixture-oauth')
        self.assertEqual(env['OPENAI_API_KEY'], 'fixture-openai-key')
        auth_env = self.auth_capture()['env']
        self.assertEqual(auth_env['ANTHROPIC_API_KEY'], '<unset>')
        self.assertEqual(auth_env['CLAUDECODE'], '<unset>')

    def test_api_route_environment_strips_oauth_token_and_keeps_api_key(self):
        job_id = self.launch(
            'success', route='api',
            env_updates={
                'ANTHROPIC_API_KEY': 'fixture-api-key',
                'CLAUDE_CODE_OAUTH_TOKEN': 'fixture-oauth',
                'ANTHROPIC_AUTH_TOKEN': 'fixture-token',
                'ANTHROPIC_BASE_URL': 'https://fixture.invalid',
                'CLAUDE_CODE_USE_BEDROCK': '1',
                'CLAUDECODE': '1',
                'CLAUDE_CODE_SAFE_MODE': '1',
            },
        )
        waited, _ = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        env = self.capture(job_id)['env']
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'fixture-api-key')
        for name in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'CLAUDE_CODE_USE_BEDROCK', 'CLAUDECODE', 'CLAUDE_CODE_SAFE_MODE'):
            self.assertEqual(env[name], '<unset>', name)

    def test_error_result_reports_subtype_terminal_reason_and_diagnostic(self):
        job = self.assert_failed(self.launch('error_result'), 'result_error: error_max_turns (max_turns)')
        self.assertIn('Claude error result: fixture error text', pathlib.Path(job['stderr_path']).read_text())

    def test_nonzero_exit_after_a_successful_looking_result_is_a_failure(self):
        self.assert_failed(self.launch('nonzero'), 'nonzero_exit: 7')

    def test_missing_result_event_is_not_completion(self):
        self.assert_failed(self.launch('missing_result'), 'missing_terminal_event')

    def test_malformed_stream_is_classified_and_diagnosed(self):
        job = self.assert_failed(self.launch('malformed'), 'malformed_stream')
        self.assertIn('malformed Claude stream-json event on line 2', pathlib.Path(job['stderr_path']).read_text())

    def test_truncated_stream_is_classified(self):
        self.assert_failed(self.launch('truncated'), 'truncated_stream')

    def test_delayed_and_high_volume_output_are_collected_before_classification(self):
        delayed = self.launch('delayed', env_updates={'CLAUDE_STUB_DELAY': '0.3'})
        waited, job = self.wait(delayed)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(job['state'], 'done')
        bulk = self.launch('high_volume', env_updates={'CLAUDE_STUB_LINES': '1500'})
        waited, job = self.wait(bulk, timeout=20)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertGreater(len(pathlib.Path(job['stdout_path']).read_bytes()), 1000000)
        self.assertEqual(self.run_cli('result', bulk).stdout, RESPONSE_TEXT)

    def test_missing_executable_is_a_startup_failure(self):
        empty = self.root / 'empty-bin'
        empty.mkdir()
        env = self.environment('success', {
            'ANTHROPIC_API_KEY': 'fixture-api-key',
            'PATH': str(empty),
        })
        launch = self.run_cli(
            'run', '--backend', 'claude', '--route', 'api', '--stdin', env=env,
            input_text='task\n',
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        self.track(launch.stdout.strip(), env, launch)
        self.assert_failed(launch.stdout.strip(), 'startup')

    def test_cancellation_stops_the_claude_process_group(self):
        job_id = self.launch('slow')
        deadline = time.monotonic() + 6
        running = None
        while time.monotonic() < deadline:
            job = self.db.get_job(job_id)
            if job and job['state'] == 'running':
                running = job
                break
            time.sleep(0.05)
        self.assertIsNotNone(running, 'Claude job did not reach running before cancellation')
        result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=12)
        self.assertEqual(result.returncode, 3, result.stderr)
        job = self.db.get_job(job_id)
        self.assertEqual(job['state'], 'cancelled')
        self.pending.discard(job_id)
        self.assertTrue(self.db.attempts(job_id)[-1]['cleanup_confirmed'])

    def test_fresh_identity_mismatch_fails_and_clears_the_session(self):
        job = self.assert_failed(self.launch('identity_mismatch'), 'session_identity_mismatch')
        self.assertIsNone(job['backend_session_id'])
        self.assertIsNone(self.claim_for(job['id']))

    def test_resumed_identity_mismatch_fails_without_touching_the_parent(self):
        parent = self.launch('success')
        parent_wait, parent_job = self.wait(parent)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        child = self.resume(parent, scenario='identity_mismatch')
        job = self.assert_failed(child, 'session_identity_mismatch')
        self.assertIsNone(job['backend_session_id'])
        self.assertEqual(self.db.get_job(parent)['backend_session_id'], parent_job['backend_session_id'])

    def test_route_mismatch_and_unreported_credential_source_fail_closed(self):
        self.assert_failed(self.launch('apikey_source', route='sub'),
                           'route_mismatch: expected sub, backend reported ANTHROPIC_API_KEY')
        self.assert_failed(self.launch('no_init', route='sub'), 'credential_source_unreported')

    def test_malformed_event_with_foreign_identity_fails_and_refuses_resume(self):
        job_id = self.launch('malformed_foreign', env_updates={'CLAUDE_STUB_FOREIGN_SESSION': 'claude-malformed-foreign'})
        job = self.assert_failed(job_id, 'session_identity_mismatch')
        self.assertIsNone(job['backend_session_id'])
        self.assertIsNone(self.claim_for(job_id))
        self.assert_resume_refused(job_id)

    def test_non_first_party_provider_fails_the_api_route(self):
        job_id = self.launch(
            'bedrock_provider', route='api', env_updates={'ANTHROPIC_API_KEY': 'fixture-api-key'},
        )
        self.assert_failed(
            job_id, 'route_mismatch: expected firstParty provider, backend reported bedrock',
        )

    def test_late_conflict_clears_the_claim_and_refuses_resume(self):
        barrier = self.root / 'release-late-conflict'
        job_id = self.launch(
            'late_conflict',
            env_updates={'CLAUDE_STUB_BARRIER': str(barrier), 'CLAUDE_STUB_FOREIGN_SESSION': 'claude-late-foreign'},
        )
        self.wait_for_claim(job_id, self.assigned_session(job_id))
        barrier.touch()
        job = self.assert_failed(job_id, 'session_identity_mismatch')
        self.assertIsNone(job['backend_session_id'])
        self.assertIsNone(self.claim_for(job_id), 'session claim remained after confirmed cleanup')
        self.assert_resume_refused(job_id)

    def test_cancellation_after_late_conflict_also_refuses_resume(self):
        barrier = self.root / 'release-late-conflict-cancel'
        job_id = self.launch(
            'late_conflict',
            env_updates={
                'CLAUDE_STUB_BARRIER': str(barrier),
                'CLAUDE_STUB_HOLD': '1',
                'CLAUDE_STUB_FOREIGN_SESSION': 'claude-late-foreign',
            },
        )
        self.wait_for_claim(job_id, self.assigned_session(job_id))
        barrier.touch()
        self.wait_for_stdout(job_id, 'claude-late-foreign')
        self.assertIsNotNone(self.claim_for(job_id), 'session claim was released before cleanup completed')
        result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=12)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.pending.discard(job_id)
        job = self.db.get_job(job_id)
        self.assertEqual(job['state'], 'cancelled')
        self.assertIsNone(job['backend_session_id'])
        self.assertTrue(self.db.attempts(job_id)[-1]['cleanup_confirmed'])
        self.assertIsNone(self.claim_for(job_id), 'session claim remained after confirmed cleanup')
        self.assert_resume_refused(job_id)

    def test_subdirectory_launch_and_resume_both_run_in_the_recorded_cwd(self):
        nested = self.workspace / 'nested' / 'deeper'
        nested.mkdir(parents=True)
        recorded = os.path.realpath(str(nested))
        parent = self.launch('success', route='sub', directory=nested)
        parent_wait, parent_job = self.wait(parent)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        self.assertEqual(parent_job['cwd'], recorded)
        self.assertEqual(self.capture(parent)['cwd'], recorded)
        child = self.resume(parent)
        child_wait, child_job = self.wait(child)
        self.assertEqual(child_wait.returncode, 0, child_wait.stderr)
        self.assertEqual(child_job['cwd'], recorded)
        self.assertEqual(self.capture(child)['cwd'], recorded)

    def test_help_lists_the_claude_backend_and_its_route_default(self):
        run_help = subprocess.run(
            [sys.executable, str(JOB_PATH), 'run', '--help'], env=self.base_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertEqual(run_help.returncode, 0, run_help.stderr)
        self.assertIn('--backend opencode|codex|claude', run_help.stdout)
        self.assertIn('optional for claude', run_help.stdout)
        top_help = subprocess.run(
            [sys.executable, str(JOB_PATH), '--help'], env=self.base_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertIn('OpenCode, Codex, and Claude', top_help.stdout)
        self.assertIn('run --backend {opencode|codex|claude}', top_help.stdout)


if __name__ == '__main__':
    unittest.main()
