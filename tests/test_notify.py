#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex queue notification transaction, delivery, and fallback tests."""

import contextlib
import io
import json
import os
import pathlib
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
sys.path.insert(0, str(SCRIPTS))

from relay import cli, lifecycle, store


class CodexNotificationTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if scratch:
            pathlib.Path(scratch).mkdir(parents=True, exist_ok=True)
        self.work = tempfile.TemporaryDirectory(prefix='relay-notify-', dir=scratch)
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)
        self.state = self.root / 'state'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.db = store.Store(str(self.state))
        self.environment = {
            'PATH': os.environ.get('PATH', ''),
            'RELAY_JOB_HOST': 'codex',
            'RELAY_JOB_STATE_DIR': str(self.state),
            'RELAY_JOB_CONVERSATION_ID': 'origin-thread',
            'RELAY_JOB_PRIVATE_TEST': 'must-not-be-exported',
            'CODEX_HOME': str(self.root / 'codex-home'),
            'OPENAI_API_KEY': 'fixture-provider-key',
        }

    def make_job(self, *, host='codex', conversation_id='origin-thread', task='private task text',
                 state='running', session_id=None):
        job_id = str(uuid.uuid4())
        artifact_dir = pathlib.Path(lifecycle._job_dir(str(self.state), job_id))
        artifact_dir.mkdir(parents=True, mode=0o700)
        paths = lifecycle.artifact_paths(str(artifact_dir))
        pathlib.Path(paths['task']).write_text(task, encoding='utf-8')
        pathlib.Path(paths['response']).write_text('collectable final result\n', encoding='utf-8')
        now = store.now_utc()
        with self.db.transaction() as conn:
            self.db.insert_job(conn, {
                'id': job_id,
                'state': state,
                'host': host,
                'host_conversation_id': conversation_id,
                'host_conversation_key': '{}:{}'.format(host, conversation_id) if conversation_id else None,
                'backend': 'codex',
                'workspace': str(self.workspace),
                'cwd': str(self.workspace),
                'supervisor_token': 'fixture-supervisor-token' if state == 'running' else None,
                'backend_session_id': session_id,
                'created_at': now,
                'updated_at': now,
                'artifact_dir': str(artifact_dir),
                'task_path': paths['task'],
                'stdout_path': paths['stdout'],
                'stderr_path': paths['stderr'],
                'events_path': paths['events'],
                'response_path': paths['response'],
                'task_ready': int(state == 'running'),
                'options': {'backend': 'codex'},
            })
            if session_id:
                self.assertTrue(self.db.claim_session(conn, job_id, 'codex', session_id))
        return job_id

    @contextlib.contextmanager
    def hold_lock_in_process(self, path):
        ready = self.root / 'lock-ready'
        release = self.root / 'lock-release'
        code = (
            'import fcntl, os, pathlib, sys, time\n'
            'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n'
            'fcntl.flock(fd, fcntl.LOCK_EX)\n'
            'pathlib.Path(sys.argv[2]).write_text("ready")\n'
            'while not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.01)\n'
        )
        process = subprocess.Popen(
            [sys.executable, '-c', code, str(path), str(ready), str(release)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            self.fail('lock holder did not become ready; exited {}'.format(process.returncode))
        try:
            yield
        finally:
            release.touch()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)

    def terminal(self, job_id, state='done'):
        return lifecycle._mark_terminal(
            self.db, job_id, state, reason=None if state == 'done' else 'fixture failure',
            expected_state='running', expected_token='fixture-supervisor-token',
        )

    def call_terminal_with(self, job_id, result):
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
                with mock.patch.object(lifecycle.subprocess, 'run', return_value=result) as send:
                    self.assertTrue(self.terminal(job_id))
        return send

    def test_success_records_exact_argv_and_task_free_payload_and_status(self):
        job_id = self.make_job(task='DO NOT SEND THIS PRIVATE TASK')
        result = subprocess.CompletedProcess(['codex'], 0, '', '')
        send = self.call_terminal_with(job_id, result)
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        result_command = lifecycle.collection_command(str(self.state), job_id, 'result')
        expected_payload = (
            'agent-relay job {} reached terminal state done.\n'
            'State root: {}\nResult command: {}'
        ).format(job_id, self.state, result_command)
        self.assertEqual(args[0], [
            'codex', 'queue', '--thread', 'origin-thread', '--message', expected_payload,
        ])
        self.assertNotIn('DO NOT SEND THIS PRIVATE TASK', args[0][-1])
        self.assertEqual(kwargs['timeout'], 30.0)
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIs(kwargs['stdout'], subprocess.DEVNULL)
        self.assertIs(kwargs['stderr'], subprocess.PIPE)
        self.assertTrue(kwargs['text'])
        for key in (
            'RELAY_JOB_HOST', 'RELAY_JOB_STATE_DIR', 'RELAY_JOB_CONVERSATION_ID',
            'RELAY_JOB_PRIVATE_TEST', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
            'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
        ):
            self.assertNotIn(key, kwargs['env'])
        self.assertEqual(kwargs['env']['CODEX_HOME'], self.environment['CODEX_HOME'])
        self.assertEqual(kwargs['env']['OPENAI_API_KEY'], 'fixture-provider-key')
        row = self.db.notifications(job_id)[0]
        self.assertEqual((row['transport'], row['state'], row['attempts']),
                         ('codex-queue', 'sent', 1))

        with mock.patch.dict(os.environ, self.environment, clear=True):
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                self.assertEqual(cli.cmd_status([job_id, '--json']), 0)
            single = json.loads(json_output.getvalue())
            self.assertEqual(single['notification_state'], 'codex-queue=sent')
            self.assertEqual(single['notifications'][0]['state'], 'sent')

            table_output = io.StringIO()
            with contextlib.redirect_stdout(table_output):
                self.assertEqual(cli.cmd_status(['--all']), 0)
            self.assertIn('NOTIFICATION', table_output.getvalue())
            self.assertIn('codex-queue=sent', table_output.getvalue())

            list_output = io.StringIO()
            with contextlib.redirect_stdout(list_output):
                self.assertEqual(cli.cmd_status(['--all', '--json']), 0)
            listed = json.loads(list_output.getvalue())
            self.assertEqual(listed[0]['notifications'][0]['state'], 'sent')

    def test_failed_push_keeps_execution_result_collectable_and_does_not_retry(self):
        job_id = self.make_job(task='sensitive task text')
        result = subprocess.CompletedProcess(['codex'], 17, '', 'queue unavailable')
        send = self.call_terminal_with(job_id, result)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.db.get_job(job_id)['state'], 'done')
        notification = self.db.notifications(job_id)[0]
        self.assertEqual(notification['state'], 'failed')
        self.assertIn('queue unavailable', notification['last_error'])
        job = self.db.get_job(job_id)
        with open(job['response_path'], 'r', encoding='utf-8') as stream:
            self.assertEqual(stream.read(), 'collectable final result\n')

        with mock.patch.dict(os.environ, self.environment, clear=True):
            with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
                with mock.patch.object(lifecycle.subprocess, 'run') as duplicate_send:
                    self.assertFalse(self.terminal(job_id))
        duplicate_send.assert_not_called()
        self.assertEqual(len(self.db.notifications(job_id)), 1)

    def test_timeout_records_uncertain_without_changing_execution_outcome(self):
        job_id = self.make_job()
        timeout = subprocess.TimeoutExpired(['codex', 'queue'], 30)
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
                with mock.patch.object(lifecycle.subprocess, 'run', side_effect=timeout) as send:
                    self.assertTrue(self.terminal(job_id))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.db.get_job(job_id)['state'], 'done')
        notification = self.db.notifications(job_id)[0]
        self.assertEqual(notification['state'], 'uncertain')
        self.assertIn('timed out', notification['last_error'])

    def test_ineligible_jobs_create_no_notification_row_or_send(self):
        cases = (
            ('claude', 'claude-session', '/fixture/bin/codex'),
            ('codex', None, '/fixture/bin/codex'),
            ('codex', 'origin-thread', None),
        )
        for host, conversation_id, codex_path in cases:
            with self.subTest(host=host, conversation_id=conversation_id, codex_path=codex_path):
                job_id = self.make_job(host=host, conversation_id=conversation_id)
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    with mock.patch.object(lifecycle.shutil, 'which', return_value=codex_path):
                        with mock.patch.object(lifecycle.subprocess, 'run') as send:
                            self.assertTrue(self.terminal(job_id))
                send.assert_not_called()
                self.assertEqual(self.db.notifications(job_id), [])

    def test_launcher_supervisor_spawn_failure_attempts_codex_notification(self):
        fields = {
            'host': 'codex', 'host_conversation_id': 'origin-thread',
            'host_conversation_key': 'codex:origin-thread', 'backend': 'codex',
            'workspace': str(self.workspace), 'cwd': str(self.workspace),
            'options': {'backend': 'codex'}, 'write': False,
        }
        result = subprocess.CompletedProcess(['codex'], 0, '', '')
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
                with mock.patch.object(
                    lifecycle.subprocess, 'Popen', side_effect=OSError('fixture spawn failure'),
                ):
                    with mock.patch.object(lifecycle.subprocess, 'run', return_value=result) as send:
                        with self.assertRaisesRegex(ValueError, 'cannot start job supervisor'):
                            lifecycle.launch(str(self.state), fields, 'startup failure task\n')
        job = self.db.list_jobs()[0]
        self.assertEqual(job['state'], 'failed')
        send.assert_called_once()
        notification = self.db.notifications(job['id'])[0]
        self.assertEqual((notification['state'], notification['attempts']), ('sent', 1))

    def test_reconciliation_created_codex_outcome_is_settled_and_prunable(self):
        job_id = self.make_job(state='created')
        with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
            with mock.patch.object(lifecycle.subprocess, 'run') as send:
                recovered = lifecycle.reconcile_job(self.db, job_id)
                self.assertEqual(recovered['state'], 'failed')
                self.assertEqual(recovered['failure_reason'],
                                 'startup: launcher stopped before supervisor registration')
                notification = self.db.notifications(job_id)[0]
                self.assertEqual((notification['transport'], notification['state']),
                                 ('codex-queue', 'uncertain'))
                self.assertEqual(notification['attempts'], 0)
                self.assertIn('without a notification sender', notification['last_error'])
                self.assertEqual(lifecycle.reconcile_job(self.db, job_id)['state'], 'failed')
                send.assert_not_called()

        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, ended_at='2000-01-01T00:00:00.000000Z')
        self.assertTrue(lifecycle.prune_job(self.db, job_id, time.time() + 1))
        pruned = self.db.get_job(job_id)
        self.assertEqual(pruned['prune_state'], 'pruned')
        self.assertFalse(pathlib.Path(pruned['response_path']).exists())
        self.assertEqual(self.db.notifications(job_id)[0]['state'], 'uncertain')

    def test_pending_codex_notification_settles_only_after_supervisor_exits(self):
        job_id = self.make_job(session_id='fixture-session')
        with mock.patch.object(lifecycle.shutil, 'which', return_value='/fixture/bin/codex'):
            with mock.patch.object(lifecycle, '_attempt_codex_notification'):
                self.assertTrue(self.terminal(job_id))
        before = self.db.get_job(job_id)
        with contextlib.closing(self.db.connect()) as conn:
            claims_before = [dict(row) for row in conn.execute(
                'SELECT * FROM session_claims ORDER BY backend,backend_session_id',
            ).fetchall()]
        self.assertEqual(self.db.notifications(job_id)[0]['state'], 'pending')

        supervisor_lock = lifecycle.artifact_paths(before)['supervisor_lock']
        with self.hold_lock_in_process(supervisor_lock):
            protected = lifecycle.reconcile_job(self.db, job_id)
            self.assertEqual(protected['state'], 'done')
            self.assertEqual(self.db.notifications(job_id)[0]['state'], 'pending')

        with mock.patch.object(lifecycle.subprocess, 'run') as duplicate_send:
            settled = lifecycle.reconcile_job(self.db, job_id)
            repeated = lifecycle.reconcile_job(self.db, job_id)
            duplicate_send.assert_not_called()
        notification = self.db.notifications(job_id)[0]
        self.assertEqual(notification['state'], 'uncertain')
        self.assertEqual(notification['attempts'], 1)
        self.assertIn('supervisor exited', notification['last_error'])
        self.assertEqual(settled['state'], 'done')
        self.assertEqual(repeated['state'], 'done')
        self.assertEqual(settled['backend_session_id'], before['backend_session_id'])
        with contextlib.closing(self.db.connect()) as conn:
            claims_after = [dict(row) for row in conn.execute(
                'SELECT * FROM session_claims ORDER BY backend,backend_session_id',
            ).fetchall()]
        self.assertEqual(claims_after, claims_before)

        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, ended_at='2000-01-01T00:00:00.000000Z')
        self.assertTrue(lifecycle.prune_job(self.db, job_id, time.time() + 1))


if __name__ == '__main__':
    unittest.main()
