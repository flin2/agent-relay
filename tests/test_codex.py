#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex adapter argv, JSONL, continuation, and lifecycle tests."""

import json
import os
import pathlib
import shlex
import shutil
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
SETUP_PATH = SCRIPTS / 'codex-setup.sh'
sys.path.insert(0, str(SCRIPTS))

from relay import cli, lifecycle, store
from relay.adapters import ExitInfo, get_adapter
from relay.adapters.codex import CodexAdapter


CODEX_STUB = r'''#!__PYTHON__
import json
import os
import pathlib
import sys
import threading
import time


def fail(message):
    sys.stderr.write('Codex fixture argv contract failure: ' + message + '\n')
    sys.stderr.flush()
    raise SystemExit(91)


args = sys.argv[1:]
if args and args[0] == 'queue':
    capture = {
        'argv': args,
        'env': {
            key: os.environ.get(key, '<unset>')
            for key in (
                'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR',
                'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
                'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_HOME', 'OPENAI_API_KEY',
            )
        },
        'relay_job_keys': sorted(key for key in os.environ if key.startswith('RELAY_JOB_')),
    }
    capture_path = os.environ.get('RELAY_QUEUE_CAPTURE_FILE')
    if capture_path:
        pathlib.Path(capture_path).write_text(json.dumps(capture), encoding='utf-8')
    delay = float(os.environ.get('RELAY_QUEUE_SLEEP', '0'))
    if delay:
        time.sleep(delay)
    if os.environ.get('RELAY_QUEUE_FAIL_STDERR'):
        sys.stderr.write(os.environ['RELAY_QUEUE_FAIL_STDERR'])
    raise SystemExit(int(os.environ.get('RELAY_QUEUE_EXIT_CODE', '0')))
if args == ['exec', '--help']:
    print(os.environ.get('RELAY_CODEX_FRESH_HELP', '--json --output-last-message FILE'))
    raise SystemExit(int(os.environ.get('RELAY_CODEX_FRESH_STATUS', '0')))
if args == ['exec', 'resume', '--help']:
    if os.environ.get('RELAY_CODEX_RESUME_HELP_FAIL') == '1':
        raise SystemExit(1)
    print('Usage: codex exec resume SESSION_ID [PROMPT]')
    raise SystemExit(0)

forbidden = (
    '--ephemeral', '--last', '--add-dir', '--dangerously-bypass-approvals-and-sandbox',
    '--dangerously-bypass-hook-trust', '--route', '--zen', '--go',
)
if any(arg in forbidden or arg.startswith('--dangerously-') for arg in args):
    fail('forbidden option in {!r}'.format(args))
if args[:2] == ['exec', 'resume']:
    mode = 'resume'
elif args and args[0] == 'exec':
    mode = 'fresh'
else:
    fail('wrong command placement: {!r}'.format(args))

try:
    task_text = sys.stdin.buffer.read().decode('utf-8')
    config = json.loads(task_text)
except Exception as exc:
    fail('task stdin was not valid JSON configuration: {}'.format(exc))
if not isinstance(config, dict):
    fail('task stdin did not contain an object')
try:
    response_path = args[args.index('--output-last-message') + 1]
except (ValueError, IndexError):
    fail('missing --output-last-message path')

sandbox = 'workspace-write' if config.get('write') else 'read-only'
expected = ['exec']
if mode == 'resume':
    expected.extend([
        'resume', '--json', '--output-last-message', response_path,
        '--skip-git-repo-check', '-c', 'sandbox_mode=' + json.dumps(sandbox),
        '-c', 'approval_policy="never"',
    ])
else:
    expected.extend([
        '--json', '--output-last-message', response_path,
        '--cd', config['expected_cwd'], '--sandbox', sandbox,
        '--skip-git-repo-check', '-c', 'approval_policy="never"',
    ])
if config.get('model'):
    expected.extend(['--model', config['model']])
if config.get('effort'):
    expected.extend(['-c', 'model_reasoning_effort=' + json.dumps(config['effort'])])
if mode == 'resume':
    expected.extend([config['expected_session_id'], '-'])
else:
    expected.append('-')
if args != expected:
    fail('expected {!r}, got {!r}'.format(expected, args))
if os.getcwd() != config['expected_cwd']:
    fail('expected process cwd {!r}, got {!r}'.format(config['expected_cwd'], os.getcwd()))

capture = {
    'argv': args,
    'cwd': os.getcwd(),
    'task': task_text,
    'mode': mode,
    'backend_env': {
        key: os.environ.get(key, '<unset>')
        for key in (
            'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR',
            'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
            'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_HOME', 'OPENAI_API_KEY',
        )
    },
    'relay_job_keys': sorted(key for key in os.environ if key.startswith('RELAY_JOB_')),
}
pathlib.Path(response_path).with_name('codex-capture.json').write_text(
    json.dumps(capture, sort_keys=True), encoding='utf-8',
)

case = config.get('case', 'success')
session_id = config.get('reported_session_id', config.get('session_id', 'thread-fixture'))


def emit(event):
    sys.stdout.write(json.dumps(event, separators=(',', ':')) + '\n')
    sys.stdout.flush()


if case == 'startup':
    sys.stderr.write('fixture failed before emitting events\n')
    sys.stderr.flush()
    raise SystemExit(int(config.get('exit_code', 70)))
if case == 'quiet':
    time.sleep(float(config.get('quiet_seconds', 0.4)))
if case == 'delayed_conflict':
    emit({'type': 'thread.started', 'thread_id': session_id})
    barrier_path = pathlib.Path(config['barrier_path'])
    deadline = time.monotonic() + 15
    while not barrier_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not barrier_path.exists():
        fail('test did not release delayed-conflict barrier')
    emit({'type': 'thread.started', 'thread_id': config.get('second_session_id', 'thread-other')})
elif case != 'missing_identity':
    emit({'type': 'thread.started', 'thread_id': session_id})
if case not in ('startup',):
    emit({'type': 'turn.started'})

if case == 'delayed_conflict' and config.get('hold_after_conflict'):
    while True:
        time.sleep(1)

if case == 'turn_failed':
    emit({'type': 'turn.failed', 'error': {'message': config.get('error', 'fixture turn failed')}})
elif case == 'nonzero_exit':
    raise SystemExit(int(config.get('exit_code', 7)))
elif case in ('missing_terminal', 'cancel'):
    if case == 'cancel':
        while True:
            time.sleep(1)
elif case == 'stale_start':
    emit({'type': 'turn.completed'})
    emit({'type': 'turn.started'})
elif case == 'missing_identity':
    emit({'type': 'turn.completed'})
elif case == 'unknown_partial':
    emit({'type': 'item.started', 'item': {'id': 'ignored'}})
    emit({'type': 'future.event', 'payload': 'ignored'})
    emit({'type': 'turn.completed'})
    sys.stdout.write('{"type":"turn.failed"')
    sys.stdout.flush()
elif case == 'unknown_events':
    emit({'type': 'item.started', 'item': {'id': 'ignored'}})
    emit({'type': 'future.event', 'payload': 'ignored'})
elif case == 'ambiguous_identity':
    emit({'type': 'turn.completed'})
    emit({'type': 'thread.started', 'thread_id': config.get('second_session_id', 'thread-other')})
    emit({'type': 'turn.started'})
    emit({'type': 'turn.completed'})
elif case == 'malformed':
    emit({'type': 'turn.completed'})
    sys.stdout.write('{not-json}\n')
    sys.stdout.flush()
elif case == 'error_diagnostic':
    emit({'type': 'error', 'error': {'message': 'diagnostic context'}})
elif case == 'large':
    size = int(config.get('large_size', 1048576))
    payload = json.dumps({
        'type': 'item.completed', 'item': {'text': 'x' * size},
    }, separators=(',', ':')) + '\n'

    def write_stdout():
        sys.stdout.write(payload)
        sys.stdout.flush()

    def write_stderr():
        remaining = size
        while remaining:
            chunk = b'E' * min(65536, remaining)
            os.write(2, chunk)
            remaining -= len(chunk)

    writers = [threading.Thread(target=write_stdout), threading.Thread(target=write_stderr)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
elif case not in ('success', 'quiet', 'startup', 'mismatch', 'unknown_events', 'delayed_conflict'):
    fail('unknown fixture case: ' + case)

if case in ('success', 'quiet', 'large', 'mismatch', 'unknown_events', 'delayed_conflict'):
    emit({'type': 'turn.completed'})
if not config.get('omit_response'):
    pathlib.Path(response_path).write_text('fixture final response\n', encoding='utf-8')
raise SystemExit(int(config.get('exit_code', 0)))
'''.replace('__PYTHON__', sys.executable)


class CodexAdapterTests(unittest.TestCase):
    def test_registered_adapter_builds_exact_fresh_and_continuation_argv(self):
        adapter = get_adapter('codex')
        self.assertIsInstance(adapter, CodexAdapter)
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            root = pathlib.Path(directory)
            artifacts = {
                'task': str(root / 'task.txt'),
                'response': str(root / 'response.txt'),
                'stdout': str(root / 'stdout.log'),
            }
            job = {
                'cwd': str(root / 'cwd'), 'workspace': str(root / 'workspace'),
                'write': False, 'model': 'model with spaces', 'effort': 'high',
                'options': {'backend': 'codex'},
            }
            fresh = adapter.prepare(job, dict(artifacts))
            self.assertEqual(fresh.argv, [
                'codex', 'exec', '--json', '--output-last-message', artifacts['response'],
                '--cd', str(root / 'cwd'), '--sandbox', 'read-only',
                '--skip-git-repo-check', '-c', 'approval_policy="never"',
                '--model', 'model with spaces', '-c', 'model_reasoning_effort="high"', '-',
            ])
            self.assertEqual(fresh.stdin_path, artifacts['task'])
            self.assertEqual(fresh.cwd, str(root / 'cwd'))

            resumed_artifacts = dict(artifacts)
            resumed = adapter.prepare(job, resumed_artifacts, 'session-exact')
            self.assertEqual(resumed.argv, [
                'codex', 'exec', 'resume', '--json', '--output-last-message',
                artifacts['response'], '--skip-git-repo-check', '-c',
                'sandbox_mode="read-only"', '-c', 'approval_policy="never"',
                '--model', 'model with spaces', '-c', 'model_reasoning_effort="high"',
                'session-exact', '-',
            ])
            self.assertEqual(resumed.cwd, str(root / 'workspace'))
            self.assertEqual(resumed.stdin_path, artifacts['task'])
            self.assertEqual(resumed_artifacts['_codex_resume_session_id'], 'session-exact')

    def test_codex_rejects_opencode_controls_and_has_no_idle_or_retry_policy(self):
        adapter = CodexAdapter()
        for options in ({'route': 'sub'}, {'gateway': 'zen'}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, 'OpenCode-only flags'):
                    adapter.validate(options)
        self.assertIsNone(adapter.idle_timeout({}))
        self.assertIsNone(adapter.next_attempt({}, {}, 1, {'stalled': True}))


class CodexLifecycleTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if scratch:
            pathlib.Path(scratch).mkdir(parents=True, exist_ok=True)
        self.work = tempfile.TemporaryDirectory(prefix='relay-codex-', dir=scratch)
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
        self.codex = self.bin_dir / 'codex'
        self.codex.write_text(CODEX_STUB, encoding='utf-8')
        self.codex.chmod(0o700)
        self.queue_capture = self.root / 'codex-queue-capture.json'
        self.base_env = dict(os.environ)
        for key in list(self.base_env):
            if key.startswith('RELAY_JOB_') or key in (
                'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
                'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
            ):
                self.base_env.pop(key, None)
        self.base_env.update({
            'HOME': str(self.home),
            'XDG_STATE_HOME': str(self.root / 'xdg'),
            'RELAY_JOB_STATE_DIR': str(self.state),
            'RELAY_JOB_HOST': 'claude',
            'RELAY_JOB_CONVERSATION_ID': 'codex-tests',
            'RELAY_QUEUE_CAPTURE_FILE': str(self.queue_capture),
            'CODEX_HOME': str(self.root / 'codex-home'),
            'OPENAI_API_KEY': 'fixture-provider-key',
            'TMPDIR': str(self.tmp_dir),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(self.bin_dir) + os.pathsep + os.environ.get('PATH', ''),
        })
        self.db = store.Store(str(self.state))
        self.pending = set()
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
        # A terminal state is committed before the supervisor sends notifications, so wait for
        # each supervisor to release its lock before removing the state directory it writes to.
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
            # After unlocking, the supervisor still closes its SQLite connection, which can
            # checkpoint and remove WAL sidecars; wait for the process itself to exit.
            pid = job.get('supervisor_pid')
            while pid and time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
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
            raise AssertionError('Codex lifecycle cleanup failed: {}'.format('; '.join(failures)))

    def run_cli(self, *args, env=None, input_text=None, timeout=20):
        return subprocess.run(
            [sys.executable, str(JOB_PATH)] + list(args),
            cwd=str(self.workspace), env=env or self.base_env,
            input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )

    def task(self, case='success', **options):
        config = {
            'case': case, 'expected_cwd': str(self.workspace),
            'session_id': 'thread-fixture', 'write': False,
        }
        config.update(options)
        return json.dumps(config)

    def assert_inline_handoff(self, result, job_id):
        self.assertEqual(result.stdout, 'fixture final response\n')
        self.assertNotIn('RELAY_JOB_STATE_DIR=', result.stderr)
        self.assertIn('[agent-relay] launched inline: {} (backend=codex'.format(job_id), result.stderr)
        self.assertIn('[agent-relay] state root: {}'.format(self.state), result.stderr)
        self.assertIn('Inline collection is in progress', result.stderr)
        for operation in ('wait', 'result', 'cancel'):
            prefix = '[agent-relay] {}: '.format(operation)
            line = next(line for line in result.stderr.splitlines() if line.startswith(prefix))
            expected = 'python3 {} --state-dir {} {} {}'.format(
                shlex.quote(str(JOB_PATH.resolve())), shlex.quote(str(self.state.resolve())),
                operation, shlex.quote(job_id),
            )
            if operation == 'wait':
                expected += ' --claude-waiter'
            self.assertEqual(line[len(prefix):], expected)

    def test_run_wait_emits_recovery_handoff_before_clean_inline_response(self):
        result = self.run_cli(
            'run', '--backend', 'codex', '--dir', str(self.workspace), '--stdin', '--wait',
            input_text=self.task('success', session_id='inline-run-thread'),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.db.list_jobs()[0]
        self.assertEqual(job['state'], 'done')
        self.assert_inline_handoff(result, job['id'])

    def test_resume_wait_emits_recovery_handoff_before_clean_inline_response(self):
        parent_result = self.run_cli(
            'run', '--backend', 'codex', '--dir', str(self.workspace), '--stdin', '--wait',
            input_text=self.task('success', session_id='inline-resume-thread'),
        )
        self.assertEqual(parent_result.returncode, 0, parent_result.stderr)
        parent = self.db.list_jobs()[0]
        self.assertEqual(parent['state'], 'done')
        task = self.task(
            'success', expected_session_id='inline-resume-thread',
            reported_session_id='inline-resume-thread',
        )
        result = self.run_cli('resume', parent['id'], '--stdin', '--wait', input_text=task)
        self.assertEqual(result.returncode, 0, result.stderr)
        child = next(job for job in self.db.list_jobs() if job['id'] != parent['id'])
        self.assertEqual(child['state'], 'done')
        self.assert_inline_handoff(result, child['id'])

    def launch(self, case='success', *, session_id='thread-fixture', model=None,
               effort=None, write=False, config_overrides=None, env=None):
        expected = {
            'session_id': session_id, 'write': bool(write),
            'model': model, 'effort': effort,
        }
        expected.update(config_overrides or {})
        task = self.task(case, **expected)
        args = ['run', '--backend', 'codex', '--dir', str(self.workspace)]
        if model:
            args.extend(('--model', model))
        if effort:
            args.extend(('--effort', effort))
        if write:
            args.append('--write')
        args.append('--stdin')
        result = self.run_cli(*args, input_text=task, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        job_id = result.stdout.strip()
        self.pending.add(job_id)
        return job_id

    def resume(self, parent_id, *, case='success', session_id=None, model=None,
               effort=None, write=False, config_overrides=None):
        parent = self.db.get_job(parent_id)
        expected = {
            'expected_cwd': parent['workspace'],
            'expected_session_id': parent['backend_session_id'],
            'reported_session_id': session_id or parent['backend_session_id'],
            'session_id': parent['backend_session_id'],
            'write': bool(write),
            'model': model if model is not None else parent.get('model'),
            'effort': effort if effort is not None else parent.get('effort'),
        }
        expected.update(config_overrides or {})
        task = json.dumps(dict({'case': case}, **expected))
        args = ['resume', parent_id]
        if model is not None:
            args.extend(('--model', model))
        if effort is not None:
            args.extend(('--effort', effort))
        if write:
            args.append('--write')
        args.append('--stdin')
        result = self.run_cli(*args, input_text=task)
        self.assertEqual(result.returncode, 0, result.stderr)
        job_id = result.stdout.strip()
        self.pending.add(job_id)
        return job_id

    def wait(self, job_id, timeout=12):
        result = self.run_cli(
            'wait', job_id, '--timeout', str(timeout), '--interval', '0.05', timeout=timeout + 5,
        )
        job = self.db.get_job(job_id)
        if job and job['state'] in ('done', 'failed', 'cancelled', 'crashed'):
            self.pending.discard(job_id)
        return result, job

    def capture(self, job):
        path = pathlib.Path(job['artifact_dir']) / 'codex-capture.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def assert_failed(self, job_id, reason):
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 2, '{} job={!r}'.format(waited.stderr, job))
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['failure_reason'], reason, 'job={!r}'.format(job))
        return job

    def test_success_and_missing_response_falls_back_to_stdout(self):
        job_id = self.launch(
            session_id='thread-success', config_overrides={'omit_response': True},
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['backend_session_id'], 'thread-success')
        result = self.run_cli('result', job_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('turn.completed', result.stdout)
        self.assertIn('partial stdout', result.stderr)

    def test_backend_and_queue_environments_strip_parent_host_context(self):
        environment = dict(
            self.base_env, RELAY_JOB_HOST='codex',
            RELAY_JOB_CONVERSATION_ID='origin-thread-123',
            RELAY_JOB_CUSTOM='must-not-reach-backend',
            CLAUDE_CODE_SESSION_ID='foreign-claude-session',
            CLAUDE_PLUGIN_DATA='/fixture/claude-data',
            PLUGIN_DATA='/fixture/plugin-data',
            CODEX_THREAD_ID='foreign-thread',
            CODEX_SESSION_ID='foreign-session',
        )
        job_id = self.launch(session_id='backend-env-test', env=environment)
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual((job['host'], job['host_conversation_id']),
                         ('codex', 'origin-thread-123'))
        captured = self.capture(job)
        self.assertEqual(captured['relay_job_keys'], [])
        for key in (
            'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR',
            'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
            'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
        ):
            self.assertEqual(captured['backend_env'][key], '<unset>', key)
        self.assertEqual(captured['backend_env']['CODEX_HOME'], str(self.root / 'codex-home'))
        self.assertEqual(captured['backend_env']['OPENAI_API_KEY'], 'fixture-provider-key')

        deadline = time.monotonic() + 5
        while not self.queue_capture.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.queue_capture.is_file(), 'Codex queue stub was not called')
        queued = json.loads(self.queue_capture.read_text(encoding='utf-8'))
        self.assertEqual(queued['argv'][:3], ['queue', '--thread', 'origin-thread-123'])
        self.assertEqual(queued['relay_job_keys'], [])
        for key in (
            'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR',
            'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
            'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
        ):
            self.assertEqual(queued['env'][key], '<unset>', key)
        self.assertEqual(queued['env']['CODEX_HOME'], str(self.root / 'codex-home'))
        self.assertEqual(queued['env']['OPENAI_API_KEY'], 'fixture-provider-key')

    def test_turn_failure_nonzero_exit_startup_and_missing_terminal_are_specific(self):
        turn_failed = self.launch('turn_failed', session_id='thread-failed')
        self.assert_failed(turn_failed, 'turn_failed: fixture turn failed')

        nonzero = self.launch(
            'nonzero_exit', session_id='thread-nonzero',
            config_overrides={'exit_code': 7},
        )
        self.assert_failed(nonzero, 'nonzero_exit: 7')

        startup = self.launch(
            'startup', session_id='thread-startup', config_overrides={'exit_code': 70},
        )
        self.assert_failed(startup, 'startup')

        missing = self.launch('missing_terminal', session_id='thread-missing-terminal')
        self.assert_failed(missing, 'missing_terminal_event')

    def test_partial_trailing_line_unknown_events_and_malformed_lines(self):
        truncated = self.launch('unknown_partial', session_id='thread-partial')
        failed = self.assert_failed(truncated, 'truncated_stream')
        self.assertTrue(pathlib.Path(failed['stdout_path']).read_bytes().endswith(
            b'{"type":"turn.failed"',
        ))

        valid = self.launch('unknown_events', session_id='thread-unknown')
        waited, job = self.wait(valid)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(job['state'], 'done')

        malformed = self.launch('malformed', session_id='thread-malformed')
        failed = self.assert_failed(malformed, 'malformed_stream')
        self.assertIn('malformed Codex JSONL event', pathlib.Path(failed['stderr_path']).read_text())

    def test_missing_fresh_identity_and_turn_order_require_current_terminal_evidence(self):
        missing_identity = self.launch('missing_identity')
        self.assert_failed(missing_identity, 'missing_session_identity')

        stale_terminal = self.launch('stale_start', session_id='thread-stale')
        self.assert_failed(stale_terminal, 'missing_terminal_event')

    def test_split_line_is_ignored_live_then_classified_after_completion(self):
        scratch = os.environ.get('TMPDIR')
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            stdout = pathlib.Path(directory) / 'stdout.log'
            stdout.write_bytes(
                b'{"type":"thread.started","thread_id":"thread-split"}\n'
                b'{"type":"turn.started"}\n{"type":"turn.com',
            )
            artifacts = {'stdout': str(stdout)}
            adapter = CodexAdapter()
            self.assertEqual(adapter.observe_session(artifacts), 'thread-split')

            with stdout.open('ab') as stream:
                stream.write(b'pleted"}\n')
            self.assertEqual(adapter.observe_session(artifacts), 'thread-split')
            outcome = adapter.classify(ExitInfo(exit_code=0), artifacts)
            self.assertEqual(outcome.state, 'done')
            self.assertEqual(outcome.session_id, 'thread-split')

    def test_fresh_conflicting_session_identities_are_ambiguous_and_unresumable(self):
        job_id = self.launch(
            'ambiguous_identity', session_id='thread-first',
            config_overrides={'second_session_id': 'thread-second'},
        )
        job = self.assert_failed(job_id, 'ambiguous_session_identity')
        self.assertIsNone(job['backend_session_id'])

        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            stdout = pathlib.Path(directory) / 'stdout.log'
            stdout.write_bytes(
                b'{"type":"thread.started","thread_id":"thread-repeat"}\n'
                b'{"type":"thread.started","thread_id":"thread-repeat"}\n'
                b'{"type":"turn.started"}\n{"type":"turn.completed"}\n',
            )
            artifacts = {'stdout': str(stdout)}
            adapter = CodexAdapter()
            self.assertEqual(adapter.observe_session(artifacts), 'thread-repeat')
            outcome = adapter.classify(ExitInfo(exit_code=0), artifacts)
            self.assertEqual(outcome.state, 'done')
            self.assertEqual(outcome.session_id, 'thread-repeat')

        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            stdout = pathlib.Path(directory) / 'stdout.log'
            stdout.write_bytes(
                b'{"type":"thread.started","thread_id":"thread-first"}\n'
                b'{"type":"thread.started","thread_id":"thread-second"}\n'
                b'{"type":"turn.started"}\n{"type":"turn.completed"}\n',
            )
            artifacts = {'stdout': str(stdout)}
            adapter = CodexAdapter()
            self.assertIsNone(adapter.observe_session(artifacts))
            outcome = adapter.classify(ExitInfo(exit_code=0), artifacts)
            self.assertEqual(outcome.state, 'failed')
            self.assertEqual(outcome.reason, 'ambiguous_session_identity')
            self.assertIsNone(outcome.session_id)

    def test_delayed_identity_conflict_clears_claim_and_refuses_continuation(self):
        barrier = self.root / 'release-delayed-conflict'
        job_id = self.launch(
            'delayed_conflict', session_id='thread-delayed-first',
            config_overrides={
                'second_session_id': 'thread-delayed-second',
                'barrier_path': str(barrier),
            },
        )

        deadline = time.monotonic() + 10
        claim = None
        while time.monotonic() < deadline:
            with self.db.transaction() as conn:
                claim = conn.execute(
                    'SELECT backend_session_id FROM session_claims WHERE job_id=?',
                    (job_id,),
                ).fetchone()
            if claim:
                break
            time.sleep(0.01)
        self.assertIsNotNone(claim, 'supervisor did not claim the first session identity')
        self.assertEqual(claim['backend_session_id'], 'thread-delayed-first')
        barrier.touch()

        job = self.assert_failed(job_id, 'ambiguous_session_identity')
        self.assertIsNone(job['backend_session_id'])
        attempts = self.db.attempts(job_id)
        self.assertTrue(attempts[-1]['cleanup_confirmed'])
        with self.db.transaction() as conn:
            claim = conn.execute(
                'SELECT backend_session_id FROM session_claims WHERE job_id=?',
                (job_id,),
            ).fetchone()
        self.assertIsNone(claim, 'session claim remained after confirmed cleanup')

        before = len(self.db.list_jobs())
        refused = self.run_cli('resume', job_id, '--stdin', input_text='follow-up task')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('ambiguous backend session identity', refused.stderr)
        self.assertEqual(len(self.db.list_jobs()), before)

    def test_delayed_identity_conflict_clears_identity_when_cancelled(self):
        barrier = self.root / 'release-delayed-conflict-cancel'
        job_id = self.launch(
            'delayed_conflict', session_id='thread-cancel-first',
            config_overrides={
                'second_session_id': 'thread-cancel-second',
                'barrier_path': str(barrier), 'hold_after_conflict': True,
            },
        )

        deadline = time.monotonic() + 10
        claim = None
        while time.monotonic() < deadline:
            with self.db.transaction() as conn:
                claim = conn.execute(
                    'SELECT backend_session_id FROM session_claims WHERE job_id=?',
                    (job_id,),
                ).fetchone()
            if claim:
                break
            time.sleep(0.01)
        self.assertIsNotNone(claim, 'supervisor did not claim the first session identity')
        self.assertEqual(claim['backend_session_id'], 'thread-cancel-first')
        barrier.touch()

        job = self.db.get_job(job_id)
        deadline = time.monotonic() + 10
        conflict_seen = False
        while time.monotonic() < deadline:
            job = self.db.get_job(job_id)
            stdout = pathlib.Path(job['stdout_path']).read_text(encoding='utf-8')
            if 'thread-cancel-second' in stdout:
                conflict_seen = True
                break
            time.sleep(0.01)
        self.assertTrue(conflict_seen, 'Codex fixture did not emit the conflicting identity')
        with self.db.transaction() as conn:
            claim = conn.execute(
                'SELECT backend_session_id FROM session_claims WHERE job_id=?',
                (job_id,),
            ).fetchone()
        self.assertIsNotNone(claim, 'session claim was released before cleanup completed')

        result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=12)
        self.assertEqual(result.returncode, 3, result.stderr)
        job = self.db.get_job(job_id)
        self.pending.discard(job_id)
        self.assertEqual(job['state'], 'cancelled')
        self.assertIsNone(job['backend_session_id'])
        attempts = self.db.attempts(job_id)
        self.assertTrue(attempts[-1]['cleanup_confirmed'])
        with self.db.transaction() as conn:
            claim = conn.execute(
                'SELECT backend_session_id FROM session_claims WHERE job_id=?',
                (job_id,),
            ).fetchone()
        self.assertIsNone(claim, 'session claim remained after confirmed cleanup')

        before = len(self.db.list_jobs())
        refused = self.run_cli('resume', job_id, '--stdin', input_text='follow-up task')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('ambiguous backend session identity', refused.stderr)
        self.assertEqual(len(self.db.list_jobs()), before)

    def test_resume_refuses_legacy_ambiguous_parent_with_populated_identity(self):
        for failure_reason in (
                'ambiguous_session_identity',
                'ambiguous_session_identity; artifact_sync: x'):
            parent = {
                'id': 'parent-fixture', 'backend': 'codex', 'route': None, 'gateway': None,
                'cwd': str(self.workspace), 'workspace': str(self.workspace),
                'model': None, 'effort': None, 'write': True,
                'state': 'failed', 'failure_reason': failure_reason,
                'backend_session_id': 'stale-thread-identity',
            }
            with self.subTest(failure_reason=failure_reason):
                with mock.patch.dict(os.environ, self.base_env, clear=True):
                    with mock.patch.object(cli.lifecycle, 'reconcile_job', return_value=parent):
                        with mock.patch.object(cli.lifecycle, 'launch') as launch:
                            with self.assertRaisesRegex(ValueError, 'ambiguous backend session identity'):
                                cli.cmd_resume(['parent-fixture', 'follow-up task'])
                            launch.assert_not_called()

    def test_resume_checks_stored_codex_events_for_conflicting_identity(self):
        def parent_with_events(name, state, failure_reason, session_ids):
            artifact_dir = self.root / name
            artifact_dir.mkdir()
            parent = {
                'id': 'parent-fixture', 'backend': 'codex', 'route': None, 'gateway': None,
                'cwd': str(self.workspace), 'workspace': str(self.workspace),
                'model': None, 'effort': None, 'write': True,
                'state': state, 'failure_reason': failure_reason,
                'backend_session_id': 'thread-a', 'artifact_dir': str(artifact_dir),
            }
            stdout = pathlib.Path(lifecycle._job_artifacts(parent)['stdout'])
            stdout.write_text(''.join(
                json.dumps({'type': 'thread.started', 'thread_id': session_id}) + '\n'
                for session_id in session_ids
            ), encoding='utf-8')
            return parent

        for state, failure_reason in (
                ('crashed', 'storage: permanent error'),
                ('failed', 'nonzero_exit: 7')):
            parent = parent_with_events(
                'conflicting-' + state, state, failure_reason, ('thread-a', 'thread-b'),
            )
            with self.subTest(state=state):
                with mock.patch.dict(os.environ, self.base_env, clear=True):
                    with mock.patch.object(cli.lifecycle, 'reconcile_job', return_value=parent):
                        with mock.patch.object(cli.lifecycle, 'launch') as launch:
                            with self.assertRaisesRegex(ValueError, 'ambiguous backend session identity'):
                                cli.cmd_resume(['parent-fixture', 'follow-up task'])
                            launch.assert_not_called()

        parent = parent_with_events(
            'repeated-identity', 'failed', 'nonzero_exit: 7', ('thread-a', 'thread-a'),
        )
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            with mock.patch.object(cli.lifecycle, 'reconcile_job', return_value=parent):
                with mock.patch.object(cli.lifecycle, 'launch', return_value='child-fixture') as launch:
                    with mock.patch.object(cli, '_emit_handoff'):
                        with mock.patch('builtins.print'):
                            self.assertEqual(
                                cli.cmd_resume(['parent-fixture', 'follow-up task']), 0,
                            )
                    launch.assert_called_once()

    def test_top_level_error_is_diagnostic_and_does_not_complete_a_turn(self):
        job_id = self.launch('error_diagnostic', session_id='thread-error')
        job = self.assert_failed(job_id, 'missing_terminal_event')
        self.assertIn('diagnostic context', pathlib.Path(job['stderr_path']).read_text())

    def test_large_simultaneous_stdout_and_stderr_are_drained(self):
        job_id = self.launch(
            'large', session_id='thread-large',
            config_overrides={'large_size': 1048576},
        )
        waited, job = self.wait(job_id, timeout=20)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertGreater(len(pathlib.Path(job['stdout_path']).read_bytes()), 1048576)
        self.assertGreaterEqual(len(pathlib.Path(job['stderr_path']).read_bytes()), 1048576)

    def test_quiet_codex_run_is_not_idle_stopped_or_retried(self):
        job_id = self.launch(
            'quiet', session_id='thread-quiet',
            config_overrides={'quiet_seconds': 0.45},
        )
        waited, job = self.wait(job_id)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        attempts = self.db.attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(attempts[0]['stalled'])

    def test_shared_lifecycle_cancellation_stops_the_codex_process_group(self):
        job_id = self.launch('cancel', session_id='thread-cancel')
        deadline = time.monotonic() + 6
        running = None
        while time.monotonic() < deadline:
            job = self.db.get_job(job_id)
            if job and job['state'] == 'running':
                running = job
                break
            time.sleep(0.05)
        self.assertIsNotNone(running, 'Codex job did not reach running before cancellation')
        result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=12)
        self.assertEqual(result.returncode, 3, result.stderr)
        job = self.db.get_job(job_id)
        self.assertEqual(job['state'], 'cancelled')
        self.pending.discard(job_id)
        attempts = self.db.attempts(job_id)
        self.assertTrue(attempts[-1]['cleanup_confirmed'])

    def test_resume_uses_exact_session_inherited_model_effort_workspace_and_read_only_reset(self):
        parent_id = self.launch(
            session_id='thread-resume-exact', model='model-parent', effort='high', write=True,
        )
        parent_wait, parent = self.wait(parent_id)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        child_id = self.resume(parent_id)
        child_wait, child = self.wait(child_id)
        self.assertEqual(child_wait.returncode, 0, child_wait.stderr)
        capture = self.capture(child)
        self.assertEqual(capture['cwd'], parent['workspace'])
        self.assertEqual(capture['argv'][-2:], ['thread-resume-exact', '-'])
        self.assertIn('sandbox_mode="read-only"', capture['argv'])
        self.assertIn('model_reasoning_effort="high"', capture['argv'])
        self.assertEqual(child['parent_job_id'], parent_id)
        self.assertEqual(child['model'], 'model-parent')
        self.assertEqual(child['effort'], 'high')
        self.assertFalse(child['write'])

    def test_resume_explicit_model_effort_and_write_overrides(self):
        parent_id = self.launch(
            session_id='thread-resume-override', model='model-parent', effort='high',
        )
        parent_wait, _ = self.wait(parent_id)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        child_id = self.resume(
            parent_id, model='model-override', effort='low', write=True,
        )
        child_wait, child = self.wait(child_id)
        self.assertEqual(child_wait.returncode, 0, child_wait.stderr)
        args = self.capture(child)['argv']
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertIn('--model', args)
        self.assertEqual(args[args.index('--model') + 1], 'model-override')
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertTrue(child['write'])

    def test_resume_identity_mismatch_fails_and_old_terminal_events_are_isolated(self):
        parent_id = self.launch(session_id='thread-parent-stale')
        parent_wait, parent = self.wait(parent_id)
        self.assertEqual(parent_wait.returncode, 0, parent_wait.stderr)
        parent_stdout = pathlib.Path(parent['stdout_path']).read_text()
        self.assertIn('turn.completed', parent_stdout)

        no_terminal = self.resume(
            parent_id, case='missing_terminal',
        )
        child = self.assert_failed(no_terminal, 'missing_terminal_event')
        child_stdout = pathlib.Path(child['stdout_path']).read_text()
        self.assertNotIn('turn.completed', child_stdout)
        self.assertNotEqual(child['stdout_path'], parent['stdout_path'])

        mismatch = self.resume(
            parent_id, case='mismatch', session_id='thread-other-session',
        )
        self.assert_failed(mismatch, 'resumed_session_mismatch')

    def test_concurrent_jobs_keep_distinct_session_identities(self):
        launch_specs = (
            ('thread-concurrent-a', 'task-a'),
            ('thread-concurrent-b', 'task-b'),
        )
        jobs = []
        errors = []

        def launch_one(session_id, label):
            try:
                job_id = self.launch(
                    'success', session_id=session_id,
                    config_overrides={'fixture_label': label},
                )
                jobs.append((job_id, session_id, label))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=launch_one, args=spec) for spec in launch_specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors, errors)
        self.assertEqual(len(jobs), 2)
        for job_id, session_id, label in jobs:
            waited, job = self.wait(job_id)
            self.assertEqual(waited.returncode, 0, waited.stderr)
            self.assertEqual(job['backend_session_id'], session_id)
            self.assertEqual(json.loads(self.capture(job)['task'])['fixture_label'], label)
        self.assertEqual(len({self.db.get_job(job_id)['backend_session_id'] for job_id, _, _ in jobs}), 2)

    def test_resume_refuses_active_unresolved_and_sessionless_parents(self):
        cases = (
            ({'state': 'running', 'backend_session_id': 'session-active'}, 'cannot resume active parent job'),
            ({'state': 'ownership_unresolved', 'backend_session_id': 'session-uncertain'},
             'cannot resume active parent job'),
            ({'state': 'failed', 'backend_session_id': None}, 'has no recorded backend session id'),
        )
        for updates, message in cases:
            parent = {
                'id': 'parent-fixture', 'backend': 'codex', 'route': None, 'gateway': None,
                'cwd': str(self.workspace), 'workspace': str(self.workspace),
                'model': None, 'effort': None, 'write': True,
                'artifact_dir': str(self.root / 'parent-fixture'),
            }
            parent.update(updates)
            with self.subTest(state=parent['state']):
                with mock.patch.dict(os.environ, self.base_env, clear=True):
                    with mock.patch.object(cli.lifecycle, 'reconcile_job', return_value=parent):
                        with mock.patch.object(cli.lifecycle, 'launch') as launch:
                            with self.assertRaisesRegex(ValueError, message):
                                cli.cmd_resume(['parent-fixture', 'follow-up task'])
                            launch.assert_not_called()

    def test_cli_rejects_opencode_only_flags_for_codex(self):
        for flag_args in (('--route', 'sub'), ('--zen',), ('--go',)):
            with self.subTest(flag_args=flag_args):
                result = self.run_cli(
                    'run', '--backend', 'codex', *flag_args, '--stdin', input_text='{}',
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('OpenCode-only flags', result.stderr)


class CodexSetupTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if scratch:
            pathlib.Path(scratch).mkdir(parents=True, exist_ok=True)
        self.work = tempfile.TemporaryDirectory(prefix='relay-codex-setup-', dir=scratch)
        self.root = pathlib.Path(self.work.name)
        self.bin_dir = self.root / 'bin'
        self.bin_dir.mkdir()
        self.codex = self.bin_dir / 'codex'
        self.codex.write_text(CODEX_STUB, encoding='utf-8')
        self.codex.chmod(0o700)

    def tearDown(self):
        self.work.cleanup()

    def run_setup(self, env_overrides=None, include_codex=True):
        path = str(self.bin_dir) if include_codex else str(self.root / 'empty-bin')
        env = {
            'PATH': path,
            'HOME': str(self.root),
            'RELAY_CODEX_FRESH_HELP': '--json --output-last-message FILE',
        }
        env.update(env_overrides or {})
        return subprocess.run(
            [shutil.which('bash') or '/bin/bash', str(SETUP_PATH)], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=5,
        )

    def test_setup_checks_codex_without_opencode(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('codex: found', result.stdout)
        self.assertIn('codex exec: supports --json', result.stdout)
        self.assertIn('codex exec: supports --output-last-message', result.stdout)
        self.assertIn('codex exec resume --help: available', result.stdout)
        self.assertIn('READY', result.stdout)

    def test_setup_reports_missing_capabilities_and_missing_cli(self):
        missing_options = self.run_setup({'RELAY_CODEX_FRESH_HELP': '--model MODEL'})
        self.assertNotEqual(missing_options.returncode, 0)
        self.assertIn('MISSING required option --json', missing_options.stdout)
        self.assertIn('MISSING required option --output-last-message', missing_options.stdout)

        missing_resume = self.run_setup({'RELAY_CODEX_RESUME_HELP_FAIL': '1'})
        self.assertNotEqual(missing_resume.returncode, 0)
        self.assertIn('codex exec resume --help: FAILED', missing_resume.stdout)

        missing_cli = self.run_setup(include_codex=False)
        self.assertNotEqual(missing_cli.returncode, 0)
        self.assertIn('codex: NOT FOUND on PATH', missing_cli.stdout)


if __name__ == '__main__':
    unittest.main()
