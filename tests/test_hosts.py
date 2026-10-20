# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise host identity, shared storage, routing, and exact-session continuation."""

import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'plugins/agent-relay/scripts'
JOB_PATH = SCRIPTS / 'relay-job.py'
sys.path.insert(0, str(SCRIPTS))


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JOB = load_helper('relay-job')
SESSION = load_helper('oc-session')


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='relay-hosts-')
        self.addCleanup(self.cleanup_work)
        self.root = pathlib.Path(self.work.name)
        self.repo_a = self.root / 'repo-a'
        self.repo_b = self.root / 'repo-b'
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        self.state = self.root / 'state'
        self.args_file = self.root / 'backend-args.txt'
        self.env_file = self.root / 'backend-env.txt'
        self.backend = self.root / 'stub-backend.sh'
        self.bin_dir = self.root / 'bin'
        self.bin_dir.mkdir()
        self.queue_file = self.root / 'codex-queue-args.txt'
        codex_stub = self.bin_dir / 'codex'
        codex_stub.write_text(
            '#!/bin/sh\n'
            ': > "${RELAY_STUB_QUEUE_FILE:?}"\n'
            'for arg in "$@"; do printf "ARG=%s\\n" "$arg" >> "$RELAY_STUB_QUEUE_FILE"; done\n'
            'exit 0\n'
        )
        codex_stub.chmod(0o700)
        self.backend.write_text(
            '#!/bin/sh\n'
            'args_file="${RELAY_STUB_ARGS_FILE:?}"\n'
            ': > "$args_file"\n'
            'task_file=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  printf "ARG=%s\\n" "$1" >> "$args_file"\n'
            '  if [ "$1" = "--task-file" ]; then shift; task_file="$1"; printf "ARG=%s\\n" "$1" >> "$args_file"; fi\n'
            '  shift\n'
            'done\n'
            'if [ -n "${RELAY_STUB_ENV_FILE:-}" ]; then\n'
            '  : > "$RELAY_STUB_ENV_FILE"\n'
            '  printf "RELAY_JOB_HOST=%s\\n" "${RELAY_JOB_HOST-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "RELAY_JOB_CONVERSATION_ID=%s\\n" "${RELAY_JOB_CONVERSATION_ID-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "RELAY_JOB_STATE_DIR=%s\\n" "${RELAY_JOB_STATE_DIR-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "RELAY_JOB_ENV_LEAK_TEST=%s\\n" "${RELAY_JOB_ENV_LEAK_TEST-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "CLAUDE_CODE_SESSION_ID=%s\\n" "${CLAUDE_CODE_SESSION_ID-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "CLAUDE_PLUGIN_DATA=%s\\n" "${CLAUDE_PLUGIN_DATA-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "PLUGIN_DATA=%s\\n" "${PLUGIN_DATA-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "CODEX_THREAD_ID=%s\\n" "${CODEX_THREAD_ID-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "CODEX_SESSION_ID=%s\\n" "${CODEX_SESSION_ID-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "CODEX_HOME=%s\\n" "${CODEX_HOME-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            '  printf "OPENAI_API_KEY=%s\\n" "${OPENAI_API_KEY-<unset>}" >> "$RELAY_STUB_ENV_FILE"\n'
            'fi\n'
            'if [ "${RELAY_STUB_SLEEP:-0}" = "1" ]; then sleep 10; fi\n'
            'if [ "${RELAY_STUB_NO_SESSION:-0}" != "1" ]; then\n'
            '  session_id="${RELAY_STUB_SESSION_ID:-ses-$(basename "$(dirname "$OC_RELAY_SESSION_FILE")")}"\n'
            '  (umask 077; printf "%s\\n" "$session_id" > "$OC_RELAY_SESSION_FILE")\n'
            'fi\n'
            'if [ -n "$task_file" ]; then cat "$task_file"; fi\n'
            'exit "${RELAY_STUB_EXIT:-0}"\n'
        )
        self.backend.chmod(0o700)
        self.base_env = dict(os.environ)
        for key in list(self.base_env):
            if key.startswith('RELAY_JOB_') or key in (
                'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
                'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
            ):
                self.base_env.pop(key, None)
        self.base_env.update({
            'HOME': str(self.root / 'home'),
            'XDG_STATE_HOME': str(self.root / 'xdg'),
            'RELAY_JOB_STATE_DIR': str(self.state),
            'RELAY_STUB_ARGS_FILE': str(self.args_file),
            'RELAY_STUB_ENV_FILE': str(self.env_file),
            'RELAY_STUB_QUEUE_FILE': str(self.queue_file),
            'CODEX_HOME': str(self.root / 'codex-home'),
            'OPENAI_API_KEY': 'fixture-provider-key',
            'PATH': str(self.bin_dir) + os.pathsep + os.environ.get('PATH', ''),
            'OC_RELAY_SUB_SCRIPT': str(self.backend),
            'OC_RELAY_API_SCRIPT': str(self.backend),
            'TMPDIR': str(self.root / 'tmp'),
        })
        pathlib.Path(self.base_env['HOME']).mkdir()
        pathlib.Path(self.base_env['TMPDIR']).mkdir()
        self.pending = set()

    def cleanup_work(self):
        for job_id in list(getattr(self, 'pending', set())):
            subprocess.run(
                [sys.executable, str(JOB_PATH), 'cancel', job_id],
                cwd=str(self.repo_a), env=self.base_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        self.work.cleanup()

    def run_command(self, *args, cwd=None, env=None, task=None, timeout=20):
        return subprocess.run(
            [sys.executable, str(JOB_PATH)] + list(args),
            cwd=str(cwd or self.repo_a), env=env or self.base_env,
            input=task, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )

    def launch_and_collect(self, environment, task, repo=None, route='sub', options=()):
        result = self.run_command(
            'run', '--backend', 'opencode', '--route', route, '--dir', str(repo or self.repo_a),
            *options, '--stdin', env=environment, task=task,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        job_id = result.stdout.strip()
        self.assertEqual(str(uuid.UUID(job_id)), job_id)
        self.pending.add(job_id)
        wait_args = ['wait', job_id, '--timeout', '10', '--interval', '0.05']
        if environment.get('RELAY_JOB_HOST') == 'claude':
            wait_args.append('--claude-waiter')
        waited = self.run_command(*wait_args, env=environment)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        started = time.monotonic()
        waited_again = self.run_command('wait', job_id, env=environment, timeout=5)
        self.assertEqual(waited_again.returncode, 0, waited_again.stderr)
        self.assertLess(time.monotonic() - started, 2.0)
        self.pending.remove(job_id)
        output = self.run_command('result', job_id, env=environment)
        self.assertEqual(output.returncode, 0, output.stderr)
        return job_id, result.stderr, output.stdout

    def read_meta(self, job_id):
        return JOB._read_meta(JOB._job_dir(str(self.state), job_id))

    def test_state_root_precedence_absolute_path_and_private_permissions(self):
        old_state_key = 'OC' + '_JOB_STATE_DIR'
        for environment, expected in (
            ({'RELAY_JOB_STATE_DIR': str(self.root / 'explicit'),
              'XDG_STATE_HOME': str(self.root / 'xdg')}, str(self.root / 'explicit')),
            ({'XDG_STATE_HOME': str(self.root / 'xdg')}, str(self.root / 'xdg' / 'agent-relay')),
            ({'HOME': str(self.root / 'home')}, str(self.root / 'home' / '.local/state/agent-relay')),
            ({'RELAY_JOB_STATE_DIR': str(self.root / 'relay'),
              'CLAUDE_PLUGIN_DATA': str(self.root / 'claude-data'),
              'PLUGIN_DATA': str(self.root / 'plugin-data')}, str(self.root / 'relay')),
            ({old_state_key: str(self.root / 'old-state'),
              'HOME': str(self.root / 'home')},
             str(self.root / 'home' / '.local/state/agent-relay')),
        ):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(JOB._resolve_state_root(), os.path.realpath(expected))
                self.assertEqual(SESSION._resolve_state_root(), os.path.realpath(expected))
        with mock.patch.dict(os.environ, {'RELAY_JOB_STATE_DIR': 'relative/state'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'absolute path'):
                JOB._resolve_state_root()
        with mock.patch.dict(os.environ, {'RELAY_JOB_STATE_DIR': str(self.state)}, clear=True):
            root = pathlib.Path(JOB._ensure_state_root())
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / 'jobs').stat().st_mode & 0o777, 0o700)

        alternate = self.root / 'explicit-flag-state'
        conflicting = dict(self.base_env, RELAY_JOB_STATE_DIR=str(self.root / 'environment-state'))
        result = self.run_command('--state-dir', str(alternate), 'status', '--all', '--json', env=conflicting)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((alternate / 'jobs.sqlite3').is_file())
        self.assertFalse((self.root / 'environment-state').exists())
        invalid = self.run_command('--state-dir', 'relative/state', 'status', env=self.base_env)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn('state root must be an absolute path', invalid.stderr)

    def test_explicit_host_and_conversation_controls_override_foreign_host_values(self):
        old_conversation_key = 'OC' + '_JOB_SESSION_ID'
        cases = (
            ({'RELAY_JOB_HOST': 'codex', 'RELAY_JOB_CONVERSATION_ID': 'explicit',
              'CLAUDE_CODE_SESSION_ID': 'foreign', 'CODEX_THREAD_ID': 'thread'},
             ('codex', 'explicit', 'codex:explicit')),
            ({'RELAY_JOB_HOST': 'codex', 'CLAUDE_CODE_SESSION_ID': 'foreign',
              'CODEX_THREAD_ID': 'thread'},
             ('codex', 'thread', 'codex:thread')),
            ({'RELAY_JOB_HOST': 'claude', 'CLAUDE_CODE_SESSION_ID': 'claude',
              'CODEX_THREAD_ID': 'foreign'},
             ('claude', 'claude', 'claude:claude')),
            ({'CODEX_THREAD_ID': 'thread', 'CODEX_SESSION_ID': 'session'},
             ('codex', 'thread', 'codex:thread')),
            ({'CODEX_SESSION_ID': 'session'}, ('codex', 'session', 'codex:session')),
            ({old_conversation_key: 'old', 'HOME': str(self.root / 'home')},
             (None, None, None)),
        )
        for environment, expected in cases:
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True):
                host = JOB._host()
                conversation = JOB._conversation_id(host)
                key = JOB._conversation_key(host, conversation)
                self.assertEqual((host, conversation, key), expected)

    def test_jobs_are_conversation_scoped_across_state_root_and_workspace_filtered(self):
        env_a = dict(self.base_env, RELAY_JOB_HOST='claude', RELAY_JOB_CONVERSATION_ID='conversation-a')
        env_b = dict(self.base_env, RELAY_JOB_HOST='codex', RELAY_JOB_CONVERSATION_ID='conversation-b')
        job_a, handoff_a, output_a = self.launch_and_collect(env_a, 'task in repo a\n', repo=self.repo_a)
        self.assertFalse(self.queue_file.exists(), 'Claude collection must not use supervisor-side queue send')
        job_b, handoff_b, output_b = self.launch_and_collect(
            env_b, 'task in repo b\n', repo=self.repo_b, route='api')
        self.assertIn('state root: ' + str(self.state), handoff_a)
        self.assertIn('backend=opencode', handoff_a)
        self.assertIn('run_in_background', handoff_a)
        self.assertIn(
            'the relay cannot observe whether Claude re-invoked the originating conversation',
            handoff_a,
        )
        self.assertIn('Codex: a queue notification is attempted', handoff_b)
        self.assertNotIn('RELAY_JOB_STATE_DIR=', handoff_a)
        self.assertNotIn('RELAY_JOB_STATE_DIR=', handoff_b)
        self.assertIn('python3 {} --state-dir {}'.format(
            shlex.quote(str(JOB_PATH.resolve())), shlex.quote(str(self.state.resolve())),
        ), handoff_a)
        self.assertIn('--claude-waiter', handoff_a)
        for operation in (' wait ', ' result ', ' cancel '):
            self.assertIn(operation, handoff_a)
        self.assertEqual(output_a, 'task in repo a\n')
        self.assertEqual(output_b, 'task in repo b\n')
        listing_a = self.run_command('status', '--json', env=env_a)
        listing_b = self.run_command('list', '--json', env=env_b)
        self.assertEqual([job['id'] for job in json.loads(listing_a.stdout)], [job_a])
        self.assertEqual([job['id'] for job in json.loads(listing_b.stdout)], [job_b])
        listing_a_job = json.loads(listing_a.stdout)[0]
        self.assertEqual(listing_a_job['notifications'][0]['state'], 'uncertain')
        self.assertIn(
            'the relay cannot observe whether Claude re-invoked the originating conversation',
            listing_a_job['notification_state'],
        )
        self.assertEqual(listing_a_job['options']['claude_waiter']['state'], 'completed')
        self.assertEqual(json.loads(listing_b.stdout)[0]['notifications'][0]['transport'], 'codex-queue')
        all_jobs = self.run_command('status', '--all', '--json', env=env_b)
        self.assertEqual({job['id'] for job in json.loads(all_jobs.stdout)}, {job_a, job_b})
        workspace_jobs = self.run_command('status', '--all', '--dir', str(self.repo_a), '--json', env=env_b)
        self.assertEqual([job['id'] for job in json.loads(workspace_jobs.stdout)], [job_a])
        meta_a = self.read_meta(job_a)
        meta_b = self.read_meta(job_b)
        self.assertEqual((meta_a['backend'], meta_a['route']), ('opencode', 'sub'))
        claude_waiter = meta_a['options']['claude_waiter']
        self.assertEqual(claude_waiter['state'], 'completed')
        self.assertIn('--claude-waiter', claude_waiter['command'])
        self.assertEqual((meta_b['backend'], meta_b['route']), ('opencode', 'api'))
        self.assertEqual(meta_b['host_conversation_key'], 'codex:conversation-b')
        self.assertFalse((self.root / 'claude-data').exists())
        job_dir = pathlib.Path(JOB._job_dir(str(self.state), job_a))
        self.assertEqual(job_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / 'jobs.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertEqual((job_dir / 'task.txt').stat().st_mode & 0o777, 0o600)
        self.assertEqual((job_dir / 'stdout.log').stat().st_mode & 0o777, 0o600)
        self.assertFalse((job_dir / 'meta.json').exists())

    def test_printed_collection_commands_work_without_state_dir_environment(self):
        environment = dict(
            self.base_env, RELAY_JOB_HOST='claude',
            RELAY_JOB_CONVERSATION_ID='handoff-command-conversation',
        )
        launch = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=environment, task='collect through printed commands\n',
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        job_id = launch.stdout.strip()
        self.pending.add(job_id)
        self.assertNotIn('RELAY_JOB_STATE_DIR=', launch.stderr)
        commands = {}
        for operation in ('wait', 'result', 'cancel'):
            prefix = '[agent-relay] {}: '.format(operation)
            line = next(line for line in launch.stderr.splitlines() if line.startswith(prefix))
            commands[operation] = line[len(prefix):]
        expected_prefix = 'python3 {} --state-dir {}'.format(
            shlex.quote(str(JOB_PATH.resolve())), shlex.quote(str(self.state.resolve())),
        )
        self.assertEqual(set(commands), {'wait', 'result', 'cancel'})
        for command in commands.values():
            self.assertTrue(command.startswith(expected_prefix), command)

        clean_environment = dict(environment)
        clean_environment.pop('RELAY_JOB_STATE_DIR')
        self.assertNotIn('RELAY_JOB_STATE_DIR', clean_environment)
        waited = subprocess.run(
            commands['wait'], shell=True, cwd=str(self.repo_a), env=clean_environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertEqual(waited.returncode, 0, waited.stderr)
        output = subprocess.run(
            commands['result'], shell=True, cwd=str(self.repo_a), env=clean_environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, 'collect through printed commands\n')
        cancelled = subprocess.run(
            commands['cancel'], shell=True, cwd=str(self.repo_a), env=clean_environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.pending.remove(job_id)

        status = self.run_command('--state-dir', str(self.state), 'status', job_id, env=clean_environment)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(
            'completed (the relay cannot observe whether Claude re-invoked the originating conversation)',
            status.stdout,
        )

    def test_opencode_backend_environment_strips_host_context_and_keeps_backend_config(self):
        environment = dict(
            self.base_env, RELAY_JOB_HOST='codex',
            RELAY_JOB_CONVERSATION_ID='explicit-origin-thread',
            RELAY_JOB_ENV_LEAK_TEST='must-not-reach-backend',
            CLAUDE_CODE_SESSION_ID='foreign-claude-session',
            CLAUDE_PLUGIN_DATA='/fixture/claude-data',
            PLUGIN_DATA='/fixture/plugin-data',
            CODEX_THREAD_ID='foreign-codex-thread',
            CODEX_SESSION_ID='foreign-codex-session',
        )
        job_id, _, _ = self.launch_and_collect(environment, 'preserve this task\n')
        metadata = self.read_meta(job_id)
        self.assertEqual((metadata['host'], metadata['host_conversation_id']),
                         ('codex', 'explicit-origin-thread'))
        captured = dict(line.split('=', 1) for line in self.env_file.read_text().splitlines())
        for key in (
            'RELAY_JOB_HOST', 'RELAY_JOB_CONVERSATION_ID', 'RELAY_JOB_STATE_DIR',
            'RELAY_JOB_ENV_LEAK_TEST', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA',
            'PLUGIN_DATA', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
        ):
            self.assertEqual(captured[key], '<unset>', key)
        self.assertEqual(captured['CODEX_HOME'], str(self.root / 'codex-home'))
        self.assertEqual(captured['OPENAI_API_KEY'], 'fixture-provider-key')

    def test_manual_claude_collection_works_when_background_waiter_is_absent(self):
        environment = dict(
            self.base_env, RELAY_JOB_HOST='claude',
            RELAY_JOB_CONVERSATION_ID='manual-claude-conversation',
        )
        launch = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=environment, task='manual fallback task\n',
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        job_id = launch.stdout.strip()
        self.pending.add(job_id)
        waited = self.run_command('wait', job_id, env=environment)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.pending.remove(job_id)
        status = self.run_command('status', job_id, '--json', env=environment)
        self.assertEqual(status.returncode, 0, status.stderr)
        job = json.loads(status.stdout)
        self.assertEqual(job['notifications'], [])
        self.assertEqual(job['notification_state'], 'none (manual collection)')
        output = self.run_command('result', job_id, env=environment)
        self.assertEqual(output.stdout, 'manual fallback task\n')

    def test_route_validation_and_public_session_flags(self):
        cases = (
            (('run', '--backend', 'codex', '--route', 'sub', '--stdin'), 'OpenCode-only flags'),
            (('run', '--backend', 'opencode', '--stdin'), '--route is required'),
            (('run', '--backend', 'opencode', '--route', 'api', '--go', '--stdin'),
             'only valid with --route sub'),
            (('run', '--backend', 'opencode', '--route', 'codex', '--stdin'), 'invalid choice'),
            (('run', '--backend', 'opencode', '--route', 'sub', '--session', 'native', '--stdin'),
             'unrecognized arguments'),
            (('run', '--backend', 'opencode', '--route', 'sub', '--resume-latest', '--stdin'),
             'unrecognized arguments'),
        )
        for command, message in cases:
            with self.subTest(command=command):
                result = self.run_command(*command, task='task\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_resume_links_exact_session_inherits_route_model_effort_and_resets_permission(self):
        parent_env = dict(
            self.base_env, RELAY_JOB_HOST='claude',
            RELAY_JOB_CONVERSATION_ID='parent-conversation',
            RELAY_STUB_SESSION_ID='ses-parent-exact',
        )
        parent, _, _ = self.launch_and_collect(
            parent_env, 'parent task\n', repo=self.repo_a, route='sub',
            options=('--model', 'glm', '--effort', 'high', '--write'),
        )
        parent_meta = self.read_meta(parent)
        self.assertEqual(parent_meta['backend_session_id'], 'ses-parent-exact')
        resume_env = dict(
            self.base_env, RELAY_JOB_HOST='codex',
            RELAY_JOB_CONVERSATION_ID='new-conversation',
            RELAY_STUB_SESSION_ID='ses-parent-exact',
        )
        resumed = self.run_command('resume', parent, '--stdin', env=resume_env, task='follow up\n')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        child = resumed.stdout.strip()
        self.pending.add(child)
        waited = self.run_command('wait', child, '--timeout', '10', '--interval', '0.05', env=resume_env)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.pending.remove(child)
        child_meta = self.read_meta(child)
        self.assertEqual(child_meta['parent_job_id'], parent)
        self.assertEqual(child_meta['backend'], parent_meta['backend'])
        self.assertEqual(child_meta['route'], parent_meta['route'])
        self.assertEqual(child_meta['dir'], parent_meta['dir'])
        self.assertEqual(child_meta['workspace'], parent_meta['workspace'])
        self.assertEqual(child_meta['backend_session_id'], 'ses-parent-exact')
        self.assertEqual(child_meta['model'], 'glm')
        self.assertEqual(child_meta['effort'], 'high')
        self.assertFalse(child_meta['write'])
        self.assertEqual(child_meta['host'], 'codex')
        self.assertEqual(child_meta['host_conversation_id'], 'new-conversation')
        self.assertEqual(child_meta['host_conversation_key'], 'codex:new-conversation')
        args_text = self.args_file.read_text()
        self.assertIn('ARG=--session\nARG=ses-parent-exact\n', args_text)
        self.assertIn('ARG=--model\nARG=glm\n', args_text)
        self.assertIn('ARG=--effort\nARG=high\n', args_text)
        self.assertNotIn('ARG=--write\n', args_text)

    def test_resume_model_effort_override_and_explicit_write(self):
        parent_env = dict(self.base_env, RELAY_JOB_HOST='claude',
                          RELAY_JOB_CONVERSATION_ID='parent',
                          RELAY_STUB_SESSION_ID='ses-override')
        parent, _, _ = self.launch_and_collect(
            parent_env, 'parent\n', options=('--model', 'glm', '--effort', 'high'))
        child_env = dict(self.base_env, RELAY_JOB_HOST='claude',
                         RELAY_JOB_CONVERSATION_ID='follow-up',
                         RELAY_STUB_SESSION_ID='ses-override')
        resumed = self.run_command(
            'resume', parent, '--model', 'provider/model-x', '--effort', 'low',
            '--write', '--stdin', env=child_env, task='authorized edit\n')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        child = resumed.stdout.strip()
        self.pending.add(child)
        waited = self.run_command('wait', child, '--timeout', '10', '--interval', '0.05', env=child_env)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.pending.remove(child)
        meta = self.read_meta(child)
        self.assertEqual(meta['model'], 'provider/model-x')
        self.assertEqual(meta['effort'], 'low')
        self.assertTrue(meta['write'])

    def test_resume_rejects_active_parent_missing_session_and_workspace(self):
        active_env = dict(self.base_env, RELAY_JOB_HOST='claude',
                          RELAY_JOB_CONVERSATION_ID='active',
                          RELAY_STUB_SLEEP='1')
        launch = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=active_env, task='slow\n')
        self.assertEqual(launch.returncode, 0, launch.stderr)
        active = launch.stdout.strip()
        self.pending.add(active)
        resumed = self.run_command('resume', active, '--stdin', env=active_env, task='new task\n')
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn('cannot resume active parent job', resumed.stderr)
        self.run_command('cancel', active, env=active_env, timeout=15)
        self.pending.remove(active)

        no_session_env = dict(self.base_env, RELAY_STUB_NO_SESSION='1')
        no_session_launch = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--dir', str(self.repo_a),
            '--stdin', env=no_session_env, task='no session\n',
        )
        self.assertEqual(no_session_launch.returncode, 0, no_session_launch.stderr)
        no_session = no_session_launch.stdout.strip()
        self.pending.add(no_session)
        no_session_wait = self.run_command(
            'wait', no_session, '--timeout', '10', '--interval', '0.05', env=no_session_env,
        )
        self.assertEqual(no_session_wait.returncode, 2, no_session_wait.stderr)
        self.pending.remove(no_session)
        result = self.run_command(
            'resume', no_session, '--stdin', env=no_session_env, task='follow-up\n',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('has no recorded backend session id', result.stderr)

        parent_env = dict(self.base_env, RELAY_STUB_SESSION_ID='ses-missing-workspace')
        parent, _, _ = self.launch_and_collect(parent_env, 'has session\n')
        meta = self.read_meta(parent)
        meta['dir'] = str(self.root / 'missing')
        meta['workspace'] = str(self.root / 'missing')
        JOB._write_meta(JOB._job_dir(str(self.state), parent), meta)
        result = self.run_command('resume', parent, '--stdin', task='follow-up\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('workspace is missing', result.stderr)

    def test_wait_exit_codes(self):
        missing = self.run_command('wait', 'relay-unknown')
        self.assertEqual(missing.returncode, 1)
        usage = self.run_command('wait')
        self.assertEqual(usage.returncode, 1)

        failed_env = dict(self.base_env, RELAY_STUB_EXIT='7')
        failed_launch = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=failed_env, task='expected failure\n')
        self.assertEqual(failed_launch.returncode, 0, failed_launch.stderr)
        failed = failed_launch.stdout.strip()
        self.pending.add(failed)
        failed_wait = self.run_command(
            'wait', failed, '--timeout', '10', '--interval', '0.05', env=failed_env)
        self.assertEqual(failed_wait.returncode, 2)
        failed_again = self.run_command('wait', failed, env=failed_env, timeout=5)
        self.assertEqual(failed_again.returncode, 2)
        self.pending.remove(failed)

        slow_env = dict(self.base_env, RELAY_STUB_SLEEP='1')
        launched = self.run_command(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=slow_env, task='timeout then cancel\n')
        self.assertEqual(launched.returncode, 0, launched.stderr)
        slow = launched.stdout.strip()
        self.pending.add(slow)
        timed_out = self.run_command(
            'wait', slow, '--timeout', '0.01', '--interval', '0.01', env=slow_env)
        self.assertEqual(timed_out.returncode, 5)
        self.run_command('cancel', slow, env=slow_env, timeout=15)
        cancelled = self.run_command('wait', slow, env=slow_env)
        self.assertEqual(cancelled.returncode, 3)
        self.pending.remove(slow)

        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait()
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            state_root = JOB._ensure_state_root()
        crashed_id = JOB._make_job_id()
        crashed_dir = pathlib.Path(JOB._job_dir(str(self.state), crashed_id))
        crashed_dir.mkdir(mode=0o700, parents=True)
        sys.path.insert(0, str(SCRIPTS))
        from relay import lifecycle
        from relay.store import Store
        db = Store(str(state_root))
        paths = lifecycle.artifact_paths(str(crashed_dir))
        now = '2026-01-01T00:00:00.000000Z'
        with db.transaction() as conn:
            db.insert_job(conn, {
                'id': crashed_id, 'state': 'running', 'host': 'claude',
                'host_conversation_id': 'test', 'host_conversation_key': 'claude:test',
                'backend': 'opencode', 'route': 'sub', 'cwd': str(self.repo_a),
                'workspace': str(self.repo_a), 'supervisor_pid': child.pid,
                'guard_pid': child.pid, 'group_pgid': child.pid, 'created_at': now,
                'updated_at': now, 'artifact_dir': str(crashed_dir),
                'task_path': paths['task'], 'stdout_path': paths['stdout'],
                'stderr_path': paths['stderr'], 'events_path': paths['events'],
                'response_path': paths['response'], 'options': {},
            })
        crashed = self.run_command('wait', crashed_id)
        self.assertEqual(crashed.returncode, 4)


if __name__ == '__main__':
    unittest.main()

