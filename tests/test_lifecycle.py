#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic lifecycle, storage, and process-ownership tests."""

import fcntl
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'plugins/agent-relay/scripts'
JOB_PATH = SCRIPTS / 'relay-job.py'
HARNESS = pathlib.Path(__file__).resolve().parent / 'lifecycle_harness.py'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HARNESS.parent))

from relay import lifecycle, store
from relay.adapters import ExitInfo, Outcome
from relay.adapters.opencode import OpenCodeAdapter
import lifecycle_harness


class LifecycleTests(unittest.TestCase):
    def test_adapter_outcomes_are_limited_to_done_or_failed(self):
        self.assertEqual(Outcome('done').state, 'done')
        self.assertEqual(Outcome('failed').state, 'failed')
        with self.assertRaises(ValueError):
            Outcome('cancelled')

    def test_kqueue_observer_preserves_exit_returned_during_registration(self):
        pid = 4321
        filter_proc = 1
        note_exit = 2
        registration = object()
        returned_exit = mock.Mock(ident=pid, filter=filter_proc, fflags=note_exit)
        queue = mock.Mock()
        queue.control.return_value = [returned_exit]
        with mock.patch.object(lifecycle.select, 'kqueue', return_value=queue, create=True), \
                mock.patch.object(lifecycle.select, 'kevent', return_value=registration, create=True), \
                mock.patch.object(lifecycle.select, 'KQ_FILTER_PROC', filter_proc, create=True), \
                mock.patch.object(lifecycle.select, 'KQ_NOTE_EXIT', note_exit, create=True), \
                mock.patch.object(lifecycle.select, 'KQ_EV_ADD', 4, create=True), \
                mock.patch.object(lifecycle.select, 'KQ_EV_ENABLE', 8, create=True):
            observer = lifecycle._KqueueLeaderExitObserver(pid)

        self.assertTrue(observer.poll())
        queue.control.assert_called_once_with([registration], 1, 0)
        observer.close()
        queue.close.assert_called_once_with()

    def test_opencode_d10_requires_session_and_rejects_length(self):
        adapter = OpenCodeAdapter()
        with tempfile.TemporaryDirectory() as directory:
            session_path = pathlib.Path(directory) / 'session-id'
            artifacts = {'session_id': str(session_path)}
            session_path.write_text('session-current\n')
            complete = adapter.classify(
                ExitInfo(0), artifacts, {'finish_reason': 'stop'}, [],
            )
            self.assertEqual((complete.state, complete.reason, complete.session_id),
                             ('done', None, 'session-current'))
            unverified = adapter.classify(ExitInfo(0), artifacts, {'finish_reason': 'unknown'}, [])
            self.assertEqual((unverified.state, unverified.reason), ('done', 'completion_unverified'))
            truncated = adapter.classify(ExitInfo(0), artifacts, {'truncated': True}, [])
            self.assertEqual((truncated.state, truncated.reason), ('failed', 'truncated'))
            session_path.unlink()
            missing = adapter.classify(ExitInfo(0), artifacts, {'finish_reason': 'stop'}, [])
            self.assertEqual((missing.state, missing.reason), ('failed', 'session_identity_unknown'))
            unidentified_stall = adapter.classify(ExitInfo(143), artifacts, {'stalled': True}, [])
            self.assertEqual((unidentified_stall.state, unidentified_stall.reason),
                             ('failed', 'session_identity_unknown'))

    def test_opencode_retry_uses_a_separate_atomic_continuation_task(self):
        adapter = OpenCodeAdapter()
        task = self.root / 'original-task.txt'
        task.write_text('Perform the original side-effecting task exactly once')
        artifacts = {
            'task': str(task), 'dir': str(self.root),
            'session_id': str(self.root / 'backend-session-id'),
        }
        job = {
            'id': 'retry-fixture', 'route': 'sub', 'cwd': str(self.cwd_a),
            'options': {'route': 'sub'}, 'write': False,
        }
        with mock.patch.dict(os.environ, {'OC_MAX_RESUMES': '3'}, clear=True):
            invocation = adapter.next_attempt(
                job, artifacts, 1,
                {'truncated': True, 'session_id': 'exact-session-123'},
            )
        task_path = pathlib.Path(
            invocation.argv[invocation.argv.index('--task-file') + 1],
        )
        self.assertNotEqual(task_path, task)
        self.assertEqual(
            task_path.read_text(),
            'Continue the previous task from where you stopped. '
            'If it is already complete, briefly confirm that and stop.',
        )
        self.assertEqual(task.read_text(), 'Perform the original side-effecting task exactly once')
        self.assertEqual(task_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(invocation.stdin_path, None)
        self.assertEqual(invocation.argv[invocation.argv.index('--session') + 1], 'exact-session-123')

        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        capture = self.root / 'opencode-argv.json'
        opencode = bin_dir / 'opencode'
        opencode.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'if len(sys.argv) > 1 and sys.argv[1] == "run":\n'
            '  open(os.environ["CAPTURE"], "w").write(json.dumps(sys.argv[1:]))\n'
            '  print("TASK:" + sys.argv[-1])\n'
            'elif len(sys.argv) > 1 and sys.argv[1] == "session":\n'
            '  print("[]")\n'
        )
        opencode.chmod(0o700)
        env = dict(os.environ)
        env.update(invocation.env_overrides)
        env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
        env['CAPTURE'] = str(capture)
        env['TMPDIR'] = str(self.tmp_dir)
        result = subprocess.run(
            invocation.argv, cwd=invocation.cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        actual_argv = json.loads(capture.read_text())
        self.assertIn('-s', actual_argv, repr(actual_argv))
        self.assertEqual(actual_argv[actual_argv.index('-s') + 1], 'exact-session-123')
        self.assertEqual(actual_argv[-1], task_path.read_text())
        self.assertIn('TASK:' + task_path.read_text(), result.stdout)

    def _make_proc_fixture(self, proc_root, self_pid, self_namespace, processes):
        proc_root.mkdir()
        os.symlink(str(self_pid), proc_root / 'self')
        self_ns = proc_root / str(self_pid) / 'ns'
        self_ns.mkdir(parents=True)
        os.symlink(self_namespace, self_ns / 'pid')
        for pid, namespace, status, stat in processes:
            process_ns = proc_root / str(pid) / 'ns'
            process_ns.mkdir(parents=True, exist_ok=True)
            pid_namespace_link = process_ns / 'pid'
            if not os.path.lexists(str(pid_namespace_link)):
                os.symlink(namespace, pid_namespace_link)
            (proc_root / str(pid) / 'status').write_text(status)
            (proc_root / str(pid) / 'stat').write_text(stat)

    def test_linux_proc_membership_uses_procfs_group_key_and_ignores_zombies(self):
        proc_root = self.root / 'proc-matching'
        self._make_proc_fixture(proc_root, 100, 'pid:[same]', [
            (100, 'pid:[same]', 'State:\tS (sleeping)\nNSpid:\t100\nNSpgid:\t9 77\n', '100 (self) S 1 77 77'),
            (101, 'pid:[same]', 'State:\tZ (zombie)\nNSpid:\t101\nNSpgid:\t9 77\n', '101 (z) Z 1 77 77'),
            (102, 'pid:[other]', 'State:\tS (sleeping)\nNSpid:\t102\nNSpgid:\t10 77\n', '102 (other) S 1 77 77'),
            (103, 'pid:[other]', 'State:\tS (sleeping)\nNSpid:\t103\nNSpgid:\t9 77\n', '103 (hidden-link) S 1 77 77'),
        ])
        real_readlink = os.readlink
        def unreadable_namespace_link(path, *args, **kwargs):
            if str(path) == str(proc_root / '103/ns/pid'):
                raise PermissionError(13, 'Permission denied', str(path))
            return real_readlink(path, *args, **kwargs)
        with mock.patch.object(os, 'readlink', side_effect=unreadable_namespace_link):
            members = lifecycle._linux_proc_group_members(9, str(proc_root), own_pid=100)
        self.assertEqual(members, [100, 103])

    def test_linux_proc_membership_parser_handles_foreign_proc_mount_safely(self):
        proc_root = self.root / 'proc-foreign'
        self._make_proc_fixture(proc_root, 400, 'pid:[inner]', [
            (400, 'pid:[inner]', 'State:\tS (sleeping)\nNSpid:\t3 400\nNSpgid:\t10 0\n', '400 (self) S 1 81 81'),
            (401, 'pid:[other]', 'State:\tS (sleeping)\nNSpid:\t401\nNSpgid:\t11 81\n', '401 (other) S 1 81 81'),
        ])
        self.assertEqual(
            lifecycle._linux_proc_group_members(10, str(proc_root), own_pid=2),
            [400],
        )

    def test_linux_proc_membership_is_unknown_without_nspgid_on_foreign_mount(self):
        proc_root = self.root / 'proc-foreign-no-nspgid'
        self._make_proc_fixture(proc_root, 400, 'pid:[inner]', [
            (400, 'pid:[inner]', 'State:\tS (sleeping)\nNSpid:\t2 400\n', '400 (self) S 1 81 81'),
        ])
        self.assertIsNone(
            lifecycle._linux_proc_group_members(10, str(proc_root), own_pid=2),
        )

    def test_linux_proc_membership_falls_back_to_stat_only_for_matching_mount(self):
        proc_root = self.root / 'proc-stat-fallback'
        self._make_proc_fixture(proc_root, 200, 'pid:[same]', [
            (200, 'pid:[same]', 'State:\tS (sleeping)\nNSpid:\t200\n', '200 (fallback) S 1 93 93 0'),
        ])
        self.assertEqual(
            lifecycle._linux_proc_group_members(93, str(proc_root), own_pid=200),
            [200],
        )

    def test_linux_proc_membership_is_unknown_on_status_permission_or_parse_errors(self):
        proc_root = self.root / 'proc-unreadable-status'
        self._make_proc_fixture(proc_root, 100, 'pid:[same]', [
            (100, 'pid:[same]', 'State:\tS (sleeping)\nNSpgid:\t9 77\n', '100 (self) S 1 77 77'),
            (200, 'pid:[same]', 'State:\tS (sleeping)\nNSpgid:\t9 77\n', '200 (other) S 1 77 77'),
        ])
        builtin_open = open
        def unreadable_status(path, *args, **kwargs):
            if str(path) == str(proc_root / '200/status'):
                raise PermissionError(13, 'Permission denied', str(path))
            return builtin_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=unreadable_status):
            self.assertIsNone(
                lifecycle._linux_proc_group_members(9, str(proc_root), own_pid=100),
            )
        (proc_root / '200/status').write_text('State:\tS (sleeping)\nNSpgid:\tbroken\n')
        self.assertIsNone(
            lifecycle._linux_proc_group_members(9, str(proc_root), own_pid=100),
        )
        # Kernel threads report a zero group on every real host; they are non-members, not unknown.
        (proc_root / '200/status').write_text('State:\tS (sleeping)\nNSpgid:\t0 77\n')
        self.assertEqual(
            lifecycle._linux_proc_group_members(9, str(proc_root), own_pid=100), [100],
        )
        (proc_root / '200/status').write_bytes(b'Name:\tw\xc3\xa4chter\nState:\tS (sleeping)\nNSpgid:\t9 77\n')
        self.assertEqual(
            lifecycle._linux_proc_group_members(9, str(proc_root), own_pid=100), [100, 200],
        )

    def test_kqueue_style_observer_orders_exit_before_cancel_signal_and_reaps_status(self):
        class Observer(object):
            def __init__(self):
                self.results = iter((False, True, True))

            def poll(self):
                return next(self.results)

        observer = Observer()
        child = mock.Mock(pid=4242, returncode=None)
        with mock.patch.object(lifecycle, '_waitid_supported', return_value=False):
            with mock.patch.object(
                    lifecycle, '_group_live_members', side_effect=([4242], []),
            ):
                with mock.patch.object(lifecycle.os, 'killpg') as killpg:
                    with mock.patch.object(
                            lifecycle.os, 'waitpid', return_value=(4242, 0),
                    ):
                        with mock.patch.object(lifecycle, '_send_status') as send_status:
                            status, term_sent, kill_sent, confirmed = lifecycle._cleanup_owned_group(
                                4242, 'proc-key', child, 99, request_kind='cancel',
                                observer=observer,
                            )
        self.assertEqual(status, {'exit_code': 0, 'exit_signal': None})
        self.assertTrue(term_sent)
        self.assertFalse(kill_sent)
        self.assertTrue(confirmed)
        killpg.assert_called_once_with(4242, signal.SIGTERM)
        self.assertNotIn(
            mock.call(99, 'CANCEL_SIGNALLED'), send_status.call_args_list,
        )
        self.assertEqual(child.returncode, 0)

    def test_unknown_group_membership_keeps_reconciliation_claim_until_absence_is_proven(self):
        process = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True,
        )
        self.background.append(process)
        job_id = self._insert_running_row('membership-unknown-session', process.pid)
        with mock.patch.object(lifecycle, '_group_live_members', return_value=None):
            unresolved = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(unresolved['state'], 'ownership_unresolved')
        with self.db.connect() as conn:
            claim = conn.execute(
                'SELECT job_id FROM session_claims WHERE backend_session_id=?',
                ('membership-unknown-session',),
            ).fetchone()
        self.assertEqual(claim['job_id'], job_id)
        process.terminate()
        process.wait(timeout=3)
        self.background.remove(process)
        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute(
                'SELECT 1 FROM session_claims WHERE job_id=?', (job_id,),
            ).fetchone())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'procfs group-key derivation is Linux-specific')
    def test_proc_group_key_uses_matching_and_foreign_procfs_numbering(self):
        matching = self.root / 'proc-key-matching'
        self._make_proc_fixture(matching, 200, 'pid:[same]', [
            (200, 'pid:[same]', 'State:\tS\nNSpid:\t200\nNSpgid:\t93 4\n', '200 (leader) S 1 93 93 0'),
        ])
        self.assertEqual(lifecycle._proc_group_key_for_child(200, str(matching), 200), 93)

        foreign = self.root / 'proc-key-foreign'
        self._make_proc_fixture(foreign, 400, 'pid:[inner]', [
            (400, 'pid:[inner]', 'State:\tS\nNSpid:\t10 2\nNSpgid:\t12 0\n', '400 (leader) S 1 3 3'),
        ])
        self.assertEqual(lifecycle._proc_group_key_for_child(2, str(foreign), 2), 12)

    @unittest.skipUnless(
        sys.platform.startswith('linux'),
        'synthetic foreign procfs numbering requires Linux NSpid/NSpgid status fields',
    )
    def test_foreign_proc_group_key_skips_unreadable_unrelated_status(self):
        proc_root = self.root / 'proc-key-foreign-unreadable'
        self._make_proc_fixture(proc_root, 400, 'pid:[inner]', [
            (400, 'pid:[inner]', 'State:\tS\nNSpid:\t3 1\nNSpgid:\t10 1\n', '400 (self) S 1 1 1'),
            (401, 'pid:[inner]', 'State:\tS\nNSpid:\t4 9\nNSpgid:\t11 9\n', '401 (unrelated) S 1 9 9'),
            (402, 'pid:[inner]', 'State:\tS\nNSpid:\t5 2\nNSpgid:\t12 2\n', '402 (leader) S 1 2 2'),
        ])
        builtin_open = open

        def unreadable_unrelated_status(path, *args, **kwargs):
            if str(path) == str(proc_root / '401/status'):
                raise PermissionError(13, 'Permission denied', str(path))
            return builtin_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=unreadable_unrelated_status):
            self.assertEqual(
                lifecycle._proc_group_key_for_child(2, str(proc_root), own_pid=2), 12,
            )

    @unittest.skipUnless(sys.platform.startswith('linux'), 'PID namespace integration requires Linux')
    def test_pid_namespace_cancellation_without_mount_proc(self):
        unshare = shutil.which('unshare')
        if not unshare:
            self.skipTest('unshare is not installed; cannot create an unprivileged PID namespace')
        probe = subprocess.run(
            [unshare, '-Ufp', '--fork', 'true'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
        )
        if probe.returncode:
            reason = probe.stderr.strip() or 'unshare exited {}'.format(probe.returncode)
            self.skipTest('unshare -Ufp --fork is unavailable for this user: {}'.format(reason))

        code = r'''
import os, pathlib, sys, time
sys.path.insert(0, os.environ['RELAY_TEST_SCRIPTS'])
sys.path.insert(0, os.environ['RELAY_TESTS_DIR'])
import lifecycle_harness
from relay import lifecycle, store
from unittest import mock
root = pathlib.Path(os.environ['RELAY_TEST_ROOT'])
state = root / 'state'
cwd = root / 'workspace'
cwd.mkdir(parents=True)
home = root / 'home'
home.mkdir()
started = root / 'backend-started'
config = {'start_file': str(started), 'delay': 30, 'echo_task': False}
env = dict(os.environ)
env.update({'HOME': str(home), 'RELAY_JOB_STATE_DIR': str(state),
            'RELAY_JOB_HOST': 'claude', 'RELAY_JOB_CONVERSATION_ID': 'pid-namespace-test',
            'PYTHONDONTWRITEBYTECODE': '1'})
fields = lifecycle_harness._fields(str(cwd), config)
with mock.patch.dict(os.environ, env, clear=True):
    job_id = lifecycle.launch(
        str(state), fields, 'namespace cancellation\n',
        lambda value: lifecycle_harness._supervisor_command_for_config(value, config),
    )
pathlib.Path(os.environ['RELAY_TEST_ROOT'], 'job-id').write_text(job_id)
deadline = time.monotonic() + 8
while not started.exists() and time.monotonic() < deadline:
    time.sleep(0.03)
if not started.exists():
    db = store.Store(str(state))
    raise RuntimeError('backend did not start in the new PID namespace: job={!r}, attempts={!r}'.format(
        db.get_job(job_id), db.attempts(job_id),
    ))
proc_self = os.readlink('/proc/self')
pid_matches_proc = proc_self == str(os.getpid())
row = store.Store(str(state)).get_job(job_id)
lifecycle.request_cancel(store.Store(str(state)), job_id)
result = lifecycle.wait_for_job(store.Store(str(state)), job_id, timeout=10, interval=0.05)
members = lifecycle._group_live_members(row['group_proc_key'])
if result == 'timeout' or result['state'] != 'cancelled' or members != []:
    raise RuntimeError('namespace cancellation failed: state={!r}, members={!r}'.format(
        result if result == 'timeout' else result['state'], members))
print('state={} group_members={} proc_self={} pid={} proc_pid_matches={}'.format(
    result['state'], members, proc_self, os.getpid(), pid_matches_proc))
'''
        env = dict(self.base_env)
        env.update({
            'RELAY_TEST_SCRIPTS': str(SCRIPTS),
            'RELAY_TESTS_DIR': str(HARNESS.parent),
            'RELAY_TEST_ROOT': str(self.root / 'pid-namespace-run'),
        })
        pathlib.Path(env['RELAY_TEST_ROOT']).mkdir()
        process = subprocess.Popen(
            [unshare, '-Ufp', '--fork', sys.executable, '-c', code],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            job_file = pathlib.Path(env['RELAY_TEST_ROOT']) / 'job-id'
            if job_file.exists():
                job_id = job_file.read_text().strip()
                namespace_db = store.Store(str(pathlib.Path(env['RELAY_TEST_ROOT']) / 'state'))
                lifecycle.request_cancel(namespace_db, job_id)
                cleanup_result = lifecycle.wait_for_job(
                    namespace_db, job_id, timeout=12, interval=0.1,
                )
                if cleanup_result == 'timeout' or cleanup_result.get('state') == 'ownership_unresolved':
                    self.fail('surviving PID namespace fixture {}: {}'.format(job_id, cleanup_result))
            self.fail('PID namespace cancellation timed out: {} {}'.format(stdout, stderr))
        if process.returncode:
            job_file = pathlib.Path(env['RELAY_TEST_ROOT']) / 'job-id'
            if job_file.exists():
                job_id = job_file.read_text().strip()
                namespace_db = store.Store(str(pathlib.Path(env['RELAY_TEST_ROOT']) / 'state'))
                lifecycle.request_cancel(namespace_db, job_id)
                cleanup_result = lifecycle.wait_for_job(
                    namespace_db, job_id, timeout=12, interval=0.1,
                )
                if cleanup_result == 'timeout' or cleanup_result.get('state') == 'ownership_unresolved':
                    self.fail('surviving PID namespace fixture {}: {}'.format(job_id, cleanup_result))
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn('state=cancelled group_members=[]', stdout)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='relay-lifecycle-')
        self.addCleanup(self.cleanup_work)
        self.root = pathlib.Path(self.work.name)
        self.state = self.root / 'state'
        self.cwd_a = self.root / 'workspace-a'
        self.cwd_b = self.root / 'workspace-b'
        self.cwd_a.mkdir()
        self.cwd_b.mkdir()
        self.tmp_dir = self.root / 'tmp'
        self.tmp_dir.mkdir()
        self.base_env = dict(os.environ)
        for key in list(self.base_env):
            if key.startswith('RELAY_JOB_') or key in (
                'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
                'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
            ):
                self.base_env.pop(key, None)
        self.base_env.update({
            'HOME': str(self.root / 'home'),
            'XDG_STATE_HOME': str(self.root / 'xdg'),
            'RELAY_JOB_STATE_DIR': str(self.state),
            'TMPDIR': str(self.tmp_dir),
            'PYTHONDONTWRITEBYTECODE': '1',
            'RELAY_JOB_HOST': 'claude',
            'RELAY_JOB_CONVERSATION_ID': 'lifecycle-test',
        })
        pathlib.Path(self.base_env['HOME']).mkdir()
        self.db = store.Store(str(self.state))
        self.pending = set()
        self.background = []

    def cleanup_work(self):
        failures = []
        job_ids = set(getattr(self, 'pending', set()))
        try:
            jobs = self.db.list_jobs()
            job_ids.update(job['id'] for job in jobs if job['state'] not in lifecycle.TERMINAL)
        except Exception as exc:
            failures.append('could not list lifecycle fixtures: {}'.format(exc))
            jobs = []
        collectors = []
        for job_id in sorted(job_ids):
            try:
                db = store.Store(str(self.state))
                row = db.get_job(job_id)
                if row and row['state'] != 'ownership_unresolved':
                    lifecycle.request_cancel(db, job_id)
            except Exception as exc:
                failures.append('job {} cancellation request raised {}'.format(job_id, exc))
        for process in getattr(self, 'background', []):
            if process.poll() is None:
                command = process.args if isinstance(process.args, (list, tuple)) else [process.args]
                if any('relay-job.py' in str(part) for part in command):
                    collectors.append(process)
                    continue
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        failures.append('test-owned fixture process {} did not exit'.format(process.pid))
        for job_id in sorted(job_ids):
            try:
                db = store.Store(str(self.state))
                result = lifecycle.wait_for_job(db, job_id, timeout=12, interval=0.1)
                if result == 'timeout' or result.get('state') == 'ownership_unresolved':
                    failures.append('job {} ended as {}'.format(
                        job_id, 'timeout' if result == 'timeout' else result['state'],
                    ))
            except Exception as exc:
                failures.append('job {} cleanup raised {}'.format(job_id, exc))
        for process in collectors:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    failures.append('relay-job collector {} remained after owner cancellation'.format(process.pid))
        for process in list(getattr(lifecycle, '_DETACHED_SUPERVISORS', [])):
            try:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    failures.append('supervisor process {} is still running'.format(process.pid))
            except Exception as exc:
                failures.append('supervisor process cleanup raised {}'.format(exc))
        if failures:
            raise AssertionError('surviving lifecycle fixture(s): {}'.format('; '.join(failures)))
        self.work.cleanup()

    def _fields(self, cwd=None, config=None, **kwargs):
        fields = lifecycle_harness._fields(str(cwd or self.cwd_a), config or {}, **kwargs)
        fields['host'] = kwargs.pop('host', fields['host']) if 'host' in kwargs else fields['host']
        return fields

    def launch(self, config=None, cwd=None, host='claude', conversation='lifecycle-test',
               session_id=None, parent_id=None, fault=None):
        config = dict(config or {})
        if fault:
            config['supervisor_fault'] = fault
        fields = lifecycle_harness._fields(
            str(cwd or self.cwd_a), config, session_id=session_id, parent_id=parent_id,
        )
        fields['host'] = host
        fields['host_conversation_id'] = conversation
        fields['host_conversation_key'] = '{}:{}'.format(host, conversation)
        command = lambda job_id: lifecycle_harness._supervisor_command_for_config(job_id, config)
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            job_id = lifecycle.launch(str(self.state), fields, 'task body\n', command)
        self.pending.add(job_id)
        return job_id

    def wait(self, job_id, timeout=10):
        result = lifecycle.wait_for_job(store.Store(str(self.state)), job_id,
                                        timeout=timeout, interval=0.05)
        if result == 'timeout':
            return result
        if result and result.get('state') in lifecycle.TERMINAL:
            self.pending.discard(job_id)
        return result

    def job(self, job_id):
        return store.Store(str(self.state)).get_job(job_id)

    def run_cli(self, *args, env=None, timeout=15, cwd=None, input_text=None):
        return subprocess.run(
            [sys.executable, str(JOB_PATH)] + list(args), cwd=str(cwd or self.cwd_a),
            env=env or self.base_env, input=input_text, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=timeout,
        )

    def assert_group_gone(self, job_id):
        job = self.job(job_id)
        records = self._fixture_process_records(job_id)
        if not records:
            attempts = self.db.attempts(job_id)
            if not attempts:
                self.assertIn(job['state'], ('failed', 'crashed'))
                self.assertIsNone(job.get('group_pgid'))
                self.assertIsNone(job.get('group_proc_key'))
                return
            group_key = attempts[-1].get('group_proc_key')
            if group_key:
                members = lifecycle._group_live_members(group_key)
                self.assertIsNotNone(members, 'cannot prove recorded attempt group is empty')
                self.assertEqual(members, [], 'recorded attempt group still has members: {}'.format(members))
                return
            self.assertTrue(
                attempts[-1].get('cleanup_confirmed') or attempts[-1].get('not_started'),
                'no fixture identity or confirmed cleanup is recorded',
            )
            return
        for pid, pgid, start_time, group_key in records:
            self.assertGreater(pid, 1)
            self.assertGreater(pgid, 1)
            self.assertTrue(start_time or sys.platform == 'darwin')
            members = lifecycle._group_live_members(group_key)
            self.assertIsNotNone(
                members, 'cannot prove membership for fixture process group key {}'.format(group_key),
            )
            self.assertEqual(
                members, [], 'fixture process group key {} still has members: {}'.format(group_key, members),
            )

    def _fixture_process_records(self, job_id):
        job = self.job(job_id)
        identity_path = pathlib.Path(job['artifact_dir']) / 'fixture-backend-identities'
        records = []
        if identity_path.is_file():
            for line in identity_path.read_text().splitlines():
                fields = line.split()
                if len(fields) >= 4 and all(fields[index].isdigit() for index in (0, 1, 3)):
                    records.append((int(fields[0]), int(fields[1]), fields[2], int(fields[3])))
        return records

    def _proc_group_key_for_pid(self, pid):
        if sys.platform == 'darwin':
            return os.getpgid(pid)
        try:
            proc_self_pid = os.path.basename(os.readlink('/proc/self'))
            matching_mount = proc_self_pid == str(os.getpid())
            if matching_mount:
                candidates = ['/proc/{}'.format(pid)]
            else:
                namespace = os.readlink('/proc/self/ns/pid')
                candidates = []
                for entry in os.listdir('/proc'):
                    if not entry.isdigit():
                        continue
                    directory = pathlib.Path('/proc') / entry
                    try:
                        if os.readlink(str(directory / 'ns/pid')) != namespace:
                            continue
                        status = (directory / 'status').read_text()
                        nspid = next(line.split(':', 1)[1].split()
                                     for line in status.splitlines() if line.startswith('NSpid:'))
                        if int(nspid[-1]) == pid:
                            candidates.append(str(directory))
                    except (OSError, ValueError, IndexError, StopIteration):
                        continue
            if len(candidates) != 1:
                return None
            status = pathlib.Path(candidates[0], 'status').read_text()
            values = next(line.split(':', 1)[1].split()
                          for line in status.splitlines() if line.startswith('NSpgid:'))
            return int(values[0])
        except (OSError, ValueError, IndexError, StopIteration):
            return None

    def _proc_entry_for_namespace_pid(self, pid):
        try:
            own_pid = os.getpid()
            matching_mount = os.path.basename(os.readlink('/proc/self')) == str(own_pid)
            if matching_mount:
                candidates = [pathlib.Path('/proc') / str(pid)]
            else:
                namespace = os.readlink('/proc/self/ns/pid')
                candidates = []
                for entry in os.listdir('/proc'):
                    if not entry.isdigit():
                        continue
                    directory = pathlib.Path('/proc') / entry
                    try:
                        if os.readlink(str(directory / 'ns/pid')) != namespace:
                            continue
                        lines = (directory / 'status').read_text().splitlines()
                        nspid = next(line.split(':', 1)[1].split()
                                     for line in lines if line.startswith('NSpid:'))
                        if int(nspid[-1]) == pid:
                            candidates.append(directory)
                    except (OSError, ValueError, IndexError, StopIteration):
                        continue
            if len(candidates) != 1:
                return None
            directory = candidates[0]
            lines = (directory / 'status').read_text().splitlines()
            state = next(line.split(':', 1)[1].strip()[0]
                         for line in lines if line.startswith('State:'))
            nspgid = next(line.split(':', 1)[1].split()
                          for line in lines if line.startswith('NSpgid:'))
            return int(directory.name), state, int(nspgid[0])
        except (OSError, ValueError, IndexError, StopIteration):
            return None

    def _wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return None

    def test_launch_detaches_and_keeps_artifacts_and_streams_separate(self):
        job_id = self.launch({
            'stdout': 'OUT ONLY\n', 'stderr': 'ERR ONLY\n', 'session_id': 'session-one',
            'delay': 0.2,
        })
        result = self.wait(job_id)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['backend_session_id'], 'session-one')
        job_dir = pathlib.Path(result['artifact_dir'])
        self.assertEqual(job_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(pathlib.Path(result['task_path']).stat().st_mode & 0o777, 0o600)
        self.assertIn('OUT ONLY', pathlib.Path(result['stdout_path']).read_text())
        self.assertIn('task body', pathlib.Path(result['stdout_path']).read_text())
        self.assertIn('ERR ONLY', pathlib.Path(result['stderr_path']).read_text())
        connection = self.db.connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE jobs SET label=label WHERE id=?', (job_id,))
            connection.execute('COMMIT')
            self.assertTrue((self.state / 'jobs.sqlite3-wal').exists() or (self.state / 'jobs.sqlite3-shm').exists())
        finally:
            connection.close()
        self.assertFalse((job_dir / 'meta.json').exists())
        self.assert_group_gone(job_id)
        stdout = self.run_cli('result', job_id)
        stderr = self.run_cli('result', job_id, '--stderr')
        self.assertIn('OUT ONLY', stdout.stdout)
        self.assertIn('ERR ONLY', stderr.stdout)

    def test_result_json_uses_partial_stdout_and_reports_unavailable_or_pruned(self):
        job_id = self.launch({'stdout': 'captured partial output\n', 'echo_task': False})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        partial = self.run_cli('result', job_id, '--json')
        self.assertEqual(partial.returncode, 0, partial.stderr)
        result = json.loads(partial.stdout)
        self.assertEqual(result['output'], 'captured partial output\n')
        self.assertEqual(result['label'], 'partial stdout')
        self.assertEqual(result['availability'], 'available')

        pathlib.Path(self.job(job_id)['stdout_path']).unlink()
        unavailable = self.run_cli('result', job_id, '--json')
        self.assertEqual(unavailable.returncode, 1)
        self.assertEqual(json.loads(unavailable.stdout)['availability'], 'unavailable')
        self.assertIn('unavailable', unavailable.stderr)

        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, ended_at='2000-01-01T00:00:00.000000Z')
        self.assertTrue(lifecycle.prune_job(self.db, job_id, time.time() + 1))
        pruned = self.run_cli('result', job_id, '--json')
        self.assertEqual(pruned.returncode, 1)
        self.assertEqual(json.loads(pruned.stdout)['availability'], 'pruned')
        self.assertIn('pruned', pruned.stderr)

    def test_startup_failure_and_quiet_success_have_distinct_outcomes(self):
        failed = self.launch({'spawn_error': True, 'echo_task': False})
        result = self.wait(failed)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['failure_reason'], 'startup')
        self.assert_group_gone(failed)
        quiet = self.launch({'echo_task': False, 'delay': 0.15})
        result = self.wait(quiet)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(pathlib.Path(result['stdout_path']).read_bytes(), b'')
        self.assert_group_gone(quiet)

    def test_wait_timeout_leaves_quiet_job_running_until_cancelled(self):
        job_id = self.launch({'echo_task': False, 'delay': 30})
        running = self._wait_until(
            lambda: self.job(job_id) if self.job(job_id)['state'] == 'running' else None,
            timeout=10,
        )
        self.assertIsNotNone(running, 'job did not reach running before the wait timeout')
        timed_out = lifecycle.wait_for_job(store.Store(str(self.state)), job_id,
                                           timeout=0.12, interval=0.03)
        self.assertEqual(timed_out, 'timeout')
        self.assertEqual(self.job(job_id)['state'], 'running')
        lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'cancelled')
        self.assertIsNotNone(result['cancel_confirmed_at'])
        self.assert_group_gone(job_id)

    def test_natural_completion_wins_when_cancellation_arrives_during_group_cleanup(self):
        marker = self.root / 'backend-finished'
        job_id = self.launch({
            'echo_task': False, 'delay': 0.1, 'finish_file': str(marker),
            'child_sleep': 0.8, 'child_ignore_term': True,
        })
        self.assertTrue(self._wait_until(marker.exists, timeout=3))
        time.sleep(0.25)
        requested = lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        self.assertIsNotNone(requested['cancel_requested_at'])
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done')
        self.assertIsNotNone(result['cancel_requested_at'])
        self.assertIsNone(result['cancel_confirmed_at'])
        self.assert_group_gone(job_id)

    def test_natural_exit_before_cancel_cleanup_is_not_confirmed_as_cancellation(self):
        ready = self.root / 'leader-exited'
        release = self.root / 'release-guard'
        job_id = self.launch({
            'session_id': 'natural-race-session', 'echo_task': False,
            'guard_fault': 'guard_after_leader_exit',
            'guard_fault_ready': str(ready), 'guard_fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=5), 'guard did not reach leader-exit barrier')
        requested = lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        self.assertIsNotNone(requested['cancel_requested_at'])
        pathlib.Path(release).write_text('continue cleanup')
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done')
        self.assertIsNotNone(result['cancel_requested_at'])
        self.assertIsNone(result['cancel_confirmed_at'])
        attempt = store.Store(str(self.state)).attempts(job_id)[0]
        self.assertFalse(attempt['cancel_signalled'])
        self.assertEqual(attempt['exit_code'], 0)
        self.assert_group_gone(job_id)

    def test_confirmed_cancel_before_backend_release_is_cancelled(self):
        ready = self.root / 'barrier-ready'
        release = self.root / 'barrier-release'
        job_id = self.launch({
            'echo_task': False, 'supervisor_fault': 'before_barrier_release',
            'fault_ready': str(ready), 'fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=5))
        running = self._wait_until(
            lambda: self.job(job_id) if self.job(job_id)['state'] == 'running' else None,
            timeout=5,
        )
        self.assertIsNotNone(running, 'supervisor did not commit running before the barrier')
        requested = lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        self.assertIsNotNone(requested['cancel_requested_at'])
        release.write_text('continue')
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['failure_reason'], 'requested_before_backend_start')
        self.assertIsNotNone(result['cancel_confirmed_at'])
        attempt = store.Store(str(self.state)).attempts(job_id)[0]
        self.assertEqual(lifecycle._lock_state(
            lifecycle.artifact_paths(result)['guard_lock'],
        ), lifecycle.LOCK_FREE)
        self.assertFalse(attempt['cancel_signalled'])
        self.assertIsNone(attempt['exit_code'])
        self.assertIsNone(attempt['group_pgid'])
        self.assertTrue(attempt['not_started'])
        self.assertTrue(attempt['cleanup_confirmed'])
        self.assert_group_gone(job_id)

    def test_unavailable_procfs_group_key_fails_held_trampoline_startup(self):
        started = self.root / 'must-not-exec-without-proc-key'
        job_id = self.launch({
            'start_file': str(started), 'echo_task': False,
            'guard_group_key_unavailable': '1',
        })
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['failure_reason'], 'startup')
        self.assertFalse(started.exists())
        attempt = self.db.attempts(job_id)[0]
        self.assertTrue(attempt['not_started'])
        self.assertTrue(attempt['cleanup_confirmed'])
        self.assertIsNone(attempt['released_at'])
        self.assert_group_gone(job_id)

    def test_reconciliation_of_cancel_request_with_empty_group_is_crashed_not_cancelled(self):
        job_id = self._insert_running_row('empty-group-session', 0)
        requested = lifecycle.request_cancel(self.db, job_id)
        self.assertIsNotNone(requested['cancel_requested_at'])
        recovered = lifecycle.reconcile_job(store.Store(str(self.state)), job_id)
        self.assertEqual(recovered['state'], 'crashed')
        self.assertIsNotNone(recovered['cancel_requested_at'])
        self.assertIsNone(recovered['cancel_confirmed_at'])
        with self.db.connect() as connection:
            self.assertIsNone(connection.execute(
                'SELECT 1 FROM session_claims WHERE job_id=?', (job_id,),
            ).fetchone())

    def test_concurrent_jobs_keep_host_workspace_and_output_identity(self):
        a = self.launch({'stdout': 'A\n'}, self.cwd_a, 'claude', 'conversation-a')
        b = self.launch({'stdout': 'B\n'}, self.cwd_b, 'codex', 'conversation-b')
        self.assertEqual(self.wait(a)['state'], 'done')
        self.assertEqual(self.wait(b)['state'], 'done')
        ja, jb = self.job(a), self.job(b)
        self.assertEqual((ja['host'], ja['host_conversation_key']), ('claude', 'claude:conversation-a'))
        self.assertEqual((jb['host'], jb['host_conversation_key']), ('codex', 'codex:conversation-b'))
        self.assertEqual(ja['workspace'], str(self.cwd_a))
        self.assertEqual(jb['workspace'], str(self.cwd_b))
        self.assertIn('A', pathlib.Path(ja['stdout_path']).read_text())
        self.assertIn('B', pathlib.Path(jb['stdout_path']).read_text())

    def test_only_one_competing_continuation_claims_a_backend_session(self):
        parent = self.launch({'session_id': 'shared-session'})
        self.assertEqual(self.wait(parent)['state'], 'done')
        barrier = threading.Barrier(3)
        outcomes = []
        lock = threading.Lock()

        def compete(index):
            fields = lifecycle_harness._fields(
                str(self.cwd_a), {'delay': 0.8, 'echo_task': False},
                session_id='shared-session', parent_id=parent,
            )
            fields['host_conversation_id'] = 'writer-{}'.format(index)
            fields['host_conversation_key'] = 'claude:writer-{}'.format(index)
            try:
                barrier.wait()
                job_id = lifecycle.launch(
                    str(self.state), fields, 'continue\n',
                    lambda value: lifecycle_harness._supervisor_command(value),
                )
                with lock:
                    outcomes.append(('ok', job_id))
                    self.pending.add(job_id)
            except ValueError as exc:
                with lock:
                    outcomes.append(('claimed', str(exc)))

        threads = [threading.Thread(target=compete, args=(i,)) for i in (1, 2)]
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=8)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([item[0] for item in outcomes].count('ok'), 1, outcomes)
        self.assertEqual([item[0] for item in outcomes].count('claimed'), 1, outcomes)
        child = next(item[1] for item in outcomes if item[0] == 'ok')
        self.assertEqual(self.wait(child, timeout=8)['state'], 'done')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM session_claims').fetchone()[0], 0)

    def test_late_session_discovery_claims_before_retry_and_conflict_starts_no_group(self):
        owner = self._insert_running_row('late-session', 0)
        retry_started = self.root / 'late-retry-started'
        job_id = self.launch({
            'session_delay': 0.2, 'session_id': 'late-session',
            'finish': 'length', 'echo_task': False,
            'retry_attempts': [{
                'session_delay': 0, 'finish': 'stop',
                'start_file': str(retry_started), 'delay': 2,
            }],
        })
        result = self.wait(job_id, timeout=10)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['failure_reason'], 'session_claim_conflict')
        self.assertFalse(retry_started.exists(), 'conflicting session was invoked a second time')
        attempts = self.db.attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]['truncated'])
        with self.db.connect() as conn:
            claim = conn.execute(
                'SELECT job_id FROM session_claims WHERE backend_session_id=?',
                ('late-session',),
            ).fetchone()
        self.assertEqual(claim['job_id'], owner)
        self.assert_group_gone(job_id)

    def test_terminal_and_notification_transactions_preserve_both_updates(self):
        ready = self.root / 'terminal-ready'
        release = self.root / 'terminal-release'
        job_id = self.launch({
            'delay': 0.1, 'echo_task': False, 'supervisor_fault': 'before_terminal_commit',
            'fault_ready': str(ready), 'fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=5), 'terminal boundary was not reached')
        # The supervisor is blocked immediately before its terminal transaction. Commit a
        # notification row in that interval, then release the terminal writer.
        store.Store(str(self.state)).upsert_notification(job_id, 'test', 'pending')
        store.Store(str(self.state)).upsert_notification(job_id, 'test', 'sent')
        pathlib.Path(release).write_text('release')
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done')
        note = store.Store(str(self.state)).notifications(job_id)[0]
        self.assertEqual(note['state'], 'sent')
        self.assertEqual(note['attempts'], 1)
        self.assertLessEqual(note['updated_at'], result['ended_at'])

    def test_faults_after_insert_and_task_write_reconcile_as_startup_failure(self):
        for fault in ('after_insert', 'after_task_write'):
            result = subprocess.run(
                [sys.executable, str(HARNESS), 'launch-fault', str(self.state), fault,
                 str(self.cwd_a), json.dumps({'echo_task': False})],
                cwd=str(self.cwd_a), env=self.base_env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 81, result.stderr)
            rows = store.Store(str(self.state)).list_jobs()
            job = next(item for item in rows if item['state'] == 'created')
            if fault == 'after_insert':
                self.assertEqual(job['task_ready'], 0)
            else:
                self.assertTrue(pathlib.Path(job['task_path']).is_file())
                self.assertEqual(job['task_ready'], 0)
            recovered = lifecycle.reconcile_job(store.Store(str(self.state)), job['id'])
            self.assertEqual(recovered['state'], 'failed')
            self.assertEqual(recovered['failure_reason'], 'startup: launcher stopped before supervisor registration')

    def test_launcher_exit_after_spawn_does_not_replace_supervisor_registration(self):
        ready = self.root / 'registration-ready'
        release = self.root / 'registration-release'
        config = {
            'delay': 0.6, 'echo_task': False,
            'supervisor_fault': 'before_supervisor_registration',
            'fault_ready': str(ready), 'fault_release': str(release),
        }
        result = subprocess.run(
            [sys.executable, str(HARNESS), 'launch-fault', str(self.state), 'after_spawn',
             str(self.cwd_a), json.dumps(config)],
            cwd=str(self.cwd_a), env=self.base_env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 81, result.stderr)
        self.assertTrue(self._wait_until(ready.exists, timeout=5), 'supervisor did not block before registration')
        job = store.Store(str(self.state)).list_jobs()[0]
        self.assertEqual(job['state'], 'created')
        self.assertEqual(lifecycle._lock_state(
            lifecycle.artifact_paths(job)['supervisor_lock'],
        ), lifecycle.LOCK_HELD)

        observed = threading.Event()
        allow_reconcile = threading.Event()
        reconciled = []

        def reconcile_barrier(point):
            if point == 'reconcile_after_observation':
                observed.set()
                allow_reconcile.wait(timeout=5)

        def reconcile_in_thread():
            reconciled.append(lifecycle.reconcile_job(store.Store(str(self.state)), job['id']))

        with mock.patch.object(lifecycle, '_fault', side_effect=reconcile_barrier):
            thread = threading.Thread(target=reconcile_in_thread)
            thread.start()
            self.assertTrue(observed.wait(timeout=3), 'reconciliation did not pause after observing created')
            pathlib.Path(release).write_text('register')
            running = self._wait_until(
                lambda: self.job(job['id']) if self.job(job['id'])['state'] == 'running' else None,
                timeout=5,
            )
            self.assertIsNotNone(running, 'supervisor never registered running')
            allow_reconcile.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(reconciled[0]['state'], 'running')
        self.pending.add(job['id'])
        result = lifecycle.wait_for_job(store.Store(str(self.state)), job['id'], timeout=8, interval=0.05)
        self.assertEqual(result['state'], 'done')
        self.assertIsNotNone(result['supervisor_token'])
        self.assert_group_gone(job['id'])

    def test_supervisor_faults_before_release_after_release_and_before_commit_are_recoverable(self):
        cases = (
            ('after_supervisor_registration', {'echo_task': False}, False),
            ('before_barrier_release', {'echo_task': False}, False),
            ('after_barrier_release', {'delay': 10, 'echo_task': False}, False),
            ('after_backend_release', {'delay': 10, 'echo_task': False}, True),
            ('before_terminal_commit', {'stdout': 'saved output\n'}, True),
        )
        for fault, config, backend_may_start in cases:
            job_id = self.launch(config, fault=fault)
            def recover_when_crashed():
                current = lifecycle.reconcile_job(store.Store(str(self.state)), job_id)
                return current if current and current['state'] == 'crashed' else None
            recovered = self._wait_until(recover_when_crashed, timeout=12)
            self.assertIsNotNone(recovered, fault)
            self.assertEqual(recovered['state'], 'crashed', fault)
            if fault == 'before_terminal_commit':
                self.assertIn('saved output', pathlib.Path(recovered['stdout_path']).read_text())
            if not backend_may_start:
                stdout_path = pathlib.Path(recovered['stdout_path'])
                self.assertFalse(stdout_path.exists() and stdout_path.read_bytes())
            self.assert_group_gone(job_id)

    def test_supervisor_death_before_exec_release_cleans_held_trampoline(self):
        ready = self.root / 'group-committed-ready'
        release = self.root / 'group-committed-release'
        backend_started = self.root / 'backend-started-before-release'
        job_id = self.launch({
            'start_file': str(backend_started), 'delay': 10, 'echo_task': False,
            'supervisor_fault': 'after_backend_group_commit',
            'fault_ready': str(ready), 'fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=6))
        job = self.job(job_id)
        attempt = self.db.attempts(job_id)[0]
        self.assertIsNotNone(attempt['group_pgid'])
        self.assertIsNone(attempt['released_at'])
        self.assertFalse(backend_started.exists())
        os.kill(job['supervisor_pid'], signal.SIGKILL)
        self.assertTrue(self._wait_until(
            lambda: (
                lifecycle._lock_state(lifecycle.artifact_paths(job)['guard_lock']) == lifecycle.LOCK_FREE
                and lifecycle._lock_state(lifecycle.artifact_paths(job)['supervisor_lock']) == lifecycle.LOCK_FREE
            ),
            timeout=5,
        ))
        attempt = self.db.attempts(job_id)[0]
        self.assertTrue(attempt['not_started'])
        self.assertTrue(attempt['cleanup_confirmed'])
        self.assertIsNone(attempt['released_at'])
        self.assertFalse(backend_started.exists())
        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        self.assert_group_gone(job_id)

    def test_reconciliation_uses_current_attempt_identity_and_keeps_claim(self):
        backend_started = self.root / 'identity-backend-started'
        job_id = self.launch({
            'start_file': str(backend_started), 'delay': 30, 'echo_task': False,
        }, session_id='identity-session')
        self.assertTrue(self._wait_until(backend_started.exists, timeout=6))
        job = self.job(job_id)
        attempts = self.db.attempts(job_id)
        fixture_pid, attempt_group, _, attempt_key = self._fixture_process_records(job_id)[0]
        self.assertEqual(int(backend_started.read_text()), fixture_pid)
        self.assertEqual(attempts[0]['group_pgid'], attempt_group)
        self.assertEqual(attempts[0]['group_proc_key'], attempt_key)
        self.assertIsNotNone(attempts[0]['released_at'])
        self.assertEqual(job['group_pgid'], attempt_group)
        self.assertEqual(job['group_proc_key'], attempt_key)
        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, group_pgid=None, group_proc_key=None)
        self.assertEqual(
            lifecycle._lock_state(lifecycle.artifact_paths(job)['guard_lock']), lifecycle.LOCK_HELD,
        )
        os.kill(job['guard_pid'], signal.SIGSTOP)
        try:
            self.assertTrue(self._wait_until(
                # macOS ps appends flags to the state letter (for example, Ts).
                lambda: (self._process_state(job['guard_pid']) or '')[:1] in ('T', 't'), timeout=3,
            ), 'guard did not stop for the reconciliation probe')
            os.kill(job['supervisor_pid'], signal.SIGKILL)
            self.assertTrue(self._wait_until(
                lambda: lifecycle._lock_state(
                    lifecycle.artifact_paths(job)['supervisor_lock'],
                ) == lifecycle.LOCK_FREE,
                timeout=5,
            ))
            recovered = lifecycle.reconcile_job(self.db, job_id)
            self.assertEqual(recovered['state'], 'ownership_unresolved')
            with self.db.connect() as conn:
                claim = conn.execute(
                    'SELECT job_id FROM session_claims WHERE backend_session_id=?',
                    ('identity-session',),
                ).fetchone()
            self.assertEqual(claim['job_id'], job_id)
            members = lifecycle._group_live_members(attempt_key)
            self.assertIsNotNone(members)
            self.assertTrue(members)
        finally:
            os.kill(job['guard_pid'], signal.SIGCONT)
        self.assertTrue(self._wait_until(
            lambda: lifecycle._lock_state(lifecycle.artifact_paths(job)['guard_lock']) == lifecycle.LOCK_FREE,
            timeout=8,
        ))
        self.assert_group_gone(job_id)
        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute(
                'SELECT 1 FROM session_claims WHERE job_id=?', (job_id,),
            ).fetchone())

    @staticmethod
    def _process_state(pid):
        try:
            fields = pathlib.Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()
            return fields[0]
        except (OSError, IndexError):
            result = subprocess.run(
                ['ps', '-o', 'stat=', '-p', str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            fields = result.stdout.split()
            return fields[0] if result.returncode == 0 and fields else None

    def _insert_running_row(self, session_id, pgid, cwd=None, group_proc_key=None):
        if group_proc_key is None and pgid and pgid > 1:
            group_proc_key = self._proc_group_key_for_pid(pgid)
        job_id = str(uuid.uuid4())
        directory = self.state / 'jobs' / job_id
        directory.mkdir(mode=0o700)
        paths = lifecycle.artifact_paths(str(directory))
        now = store.now_utc()
        row = {
            'id': job_id, 'state': 'running', 'host': 'claude',
            'host_conversation_id': 'recovery', 'host_conversation_key': 'claude:recovery',
            'backend': 'stub', 'workspace': str(cwd or self.cwd_a), 'cwd': str(cwd or self.cwd_a),
            'label': 'recovery', 'write': False, 'backend_session_id': session_id,
            'created_at': now, 'updated_at': now, 'artifact_dir': str(directory),
            'task_path': paths['task'], 'stdout_path': paths['stdout'],
            'stderr_path': paths['stderr'], 'events_path': paths['events'],
            'response_path': paths['response'], 'options': {'stub': {}},
            'guard_pid': 99999999, 'group_pgid': pgid,
            'group_proc_key': group_proc_key, 'supervisor_pid': 99999999,
        }
        with self.db.transaction() as conn:
            self.db.insert_job(conn, row)
            self.db.claim_session(conn, job_id, 'stub', session_id)
        return job_id

    def test_supervisor_dead_with_surviving_guard_evidence_is_unresolved_and_keeps_claim(self):
        rogue = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True,
        )
        self.background.append(rogue)
        job_id = self._insert_running_row('held-session', rogue.pid)
        ready = self.root / 'lock-ready'
        holder = subprocess.Popen(
            [sys.executable, str(HARNESS), 'lock-guard', str(self.state), job_id, str(ready)],
            env=self.base_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.background.append(holder)
        self.assertTrue(self._wait_until(ready.exists, timeout=3))
        recovered = lifecycle.reconcile_job(store.Store(str(self.state)), job_id)
        self.assertEqual(recovered['state'], 'ownership_unresolved')
        self.assertIn('manual_recovery', json.loads(recovered['ownership_evidence']))
        self.assertIsNone(rogue.poll())
        fields = lifecycle_harness._fields(str(self.cwd_a), {}, session_id='held-session', parent_id=job_id)
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                lifecycle.launch(
                    str(self.state), fields, 'must not start\n',
                    lambda value: lifecycle_harness._supervisor_command(value),
                )
        self.assertIsNone(rogue.poll())

    def test_stale_recorded_process_group_is_never_signalled(self):
        unrelated = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True,
        )
        self.background.append(unrelated)
        job_id = self._insert_running_row('stale-session', unrelated.pid)
        recovered = lifecycle.reconcile_job(store.Store(str(self.state)), job_id)
        self.assertEqual(recovered['state'], 'ownership_unresolved')
        self.assertIsNone(unrelated.poll())
        evidence = json.loads(recovered['ownership_evidence'])
        self.assertEqual(evidence['group_proc_key'], self._proc_group_key_for_pid(unrelated.pid))
        self.assertTrue(evidence['group_members'])

    def test_nondumpable_descendant_stays_owned_until_group_cleanup_is_confirmed(self):
        if not sys.platform.startswith('linux'):
            self.skipTest('PR_SET_DUMPABLE is Linux-specific')
        ready = self.root / 'nondumpable-ready'
        heartbeat = self.root / 'nondumpable-heartbeat'
        stop = self.root / 'nondumpable-stop'
        child_code = (
            'import ctypes, os, pathlib, signal, time\n'
            'assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'ready = pathlib.Path({!r})\n'
            'heartbeat = pathlib.Path({!r})\n'
            'stop = pathlib.Path({!r})\n'
            'heartbeat.write_text(str(time.monotonic()))\n'
            'ready.write_text(str(os.getpid()))\n'
            'while not stop.exists():\n'
            '    heartbeat.write_text(str(time.monotonic()))\n'
            '    time.sleep(0.04)\n'
        ).format(str(ready), str(heartbeat), str(stop))
        job_id = self.launch({
            'session_id': 'nondumpable-descendant-session', 'child_code': child_code,
            'child_ready_file': str(ready), 'echo_task': False,
        })
        try:
            self.assertTrue(self._wait_until(ready.exists, timeout=5))
            beat_before = heartbeat.read_text()
            observed = lifecycle.wait_for_job(
                store.Store(str(self.state)), job_id, timeout=0.5, interval=0.05,
            )
            self.assertEqual(observed, 'timeout', 'job completed while its hidden child was alive')
            claim = self._wait_until(lambda: self._claim_for_session(
                'nondumpable-descendant-session',
            ), timeout=2)
            self.assertIsNotNone(claim, 'session claim was released while the child was alive')
            self.assertEqual(claim['job_id'], job_id)
            running = self._wait_until(
                lambda: self.job(job_id) if self.job(job_id)['state'] == 'running' else None,
                timeout=5,
            )
            self.assertIsNotNone(running, 'job did not remain running while the child was alive')
            time.sleep(0.15)
            self.assertNotEqual(heartbeat.read_text(), beat_before)
            result = self.wait(job_id, timeout=10)
            self.assertEqual(result['state'], 'done', result)
            self.assertTrue(self.db.attempts(job_id)[0]['cleanup_confirmed'])
            with self.db.connect() as conn:
                self.assertIsNone(conn.execute(
                    'SELECT 1 FROM session_claims WHERE job_id=?', (job_id,),
                ).fetchone())
            beat_after = heartbeat.read_text()
            time.sleep(0.15)
            self.assertEqual(heartbeat.read_text(), beat_after)
        finally:
            stop.touch()

    def test_split_stdout_session_marker_is_claimed_only_after_complete_line(self):
        wrapper = self.root / 'split-session-wrapper'
        release = self.root / 'split-session-release'
        wrapper_source = (
            '#!/usr/bin/env python3\n'
            'import sys, time\n'
            'sys.stdout.write("[relay-session] id=ses_"); sys.stdout.flush(); time.sleep(0.2)\n'
            'sys.stdout.write("complete backend=go\\n"); sys.stdout.flush()\n'
            'sys.stderr.write("[relay-completion] finish=stop\\n"); sys.stderr.flush()\n'
            'while not __import__("os").path.exists({!r}): time.sleep(0.02)\n'
        )
        wrapper.write_text(wrapper_source.format(str(release)))
        wrapper.chmod(0o700)
        env = dict(self.base_env, OC_RELAY_SUB_SCRIPT=str(wrapper))
        launched = self.run_cli(
            'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
            env=env, input_text='split session marker regression\n',
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        job_id = launched.stdout.strip()
        self.pending.add(job_id)
        try:
            job = self.job(job_id)
            self.assertTrue(self._wait_until(
                lambda: '[relay-session] id=ses_' in pathlib.Path(job['stdout_path']).read_text()
                if pathlib.Path(job['stdout_path']).exists() else False,
                timeout=3,
            ), 'partial session marker was not written')
            self.assertIsNone(OpenCodeAdapter().observe_session({
                'session_id': lifecycle.artifact_paths(job)['session_id'],
                'stdout': job['stdout_path'],
            }))
            self.assertIsNone(self._claim_for_session('ses_'))
            claim = self._wait_until(lambda: self._claim_for_session('ses_complete'), timeout=4)
            self.assertIsNotNone(claim, 'complete marker was not claimed while the wrapper was alive')
            self.assertEqual(claim['job_id'], job_id)
            self.assertEqual(self.job(job_id)['backend_session_id'], 'ses_complete')
        finally:
            release.touch()
        result = lifecycle.wait_for_job(store.Store(str(self.state)), job_id, timeout=8)
        self.assertNotEqual(result, 'timeout')
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(result['backend_session_id'], 'ses_complete')
        self.assert_group_gone(job_id)

    def _claim_for_session(self, session_id):
        conn = self.db.connect()
        try:
            row = conn.execute(
                'SELECT job_id,backend_session_id FROM session_claims WHERE backend_session_id=?',
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def test_cancellation_stops_only_the_owned_group(self):
        caller_pid = os.getpid()
        job_id = self.launch({'delay': 12, 'echo_task': False})
        started = self._wait_until(lambda: self.job(job_id) if self.job(job_id)['state'] == 'running' else None)
        self.assertIsNotNone(started)
        self.assertNotEqual(started['group_pgid'], os.getpgrp())
        result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=10)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.job(job_id)['state'], 'cancelled')
        os.kill(caller_pid, 0)
        self.assert_group_gone(job_id)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS ps membership and cancellation check')
    def test_darwin_ps_membership_path_and_cancellation(self):
        job_id = self.launch({'delay': 30, 'echo_task': False})
        started = self._wait_until(
            lambda: self.job(job_id) if self.job(job_id)['group_pgid'] else None,
        )
        self.assertIsNotNone(started, 'backend process group was not recorded')
        group_key = started['group_proc_key']
        self.assertIsNotNone(group_key)
        with mock.patch.object(
                lifecycle, '_ps_group_members', wraps=lifecycle._ps_group_members,
        ) as ps_members:
            members = lifecycle._group_live_members(group_key)
            self.assertIsNotNone(members, 'ps could not establish group membership')
            self.assertTrue(members, 'live backend group is missing from ps')
            self.assertGreater(ps_members.call_count, 0)

            result = self.run_cli('cancel', job_id, '--timeout', '8', timeout=10)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(self.job(job_id)['state'], 'cancelled')
            self.assertEqual(lifecycle._group_live_members(group_key), [])
            self.assertGreater(ps_members.call_count, 1)
        self.assert_group_gone(job_id)

    def test_idle_stop_creates_a_fresh_attempt_then_succeeds(self):
        job_id = self.launch({
            'session_id': 'idle-retry-session', 'idle_timeout': 0.3,
            'delay': 4, 'echo_task': False,
            'retry_attempts': [{
                'session_id': 'idle-retry-session', 'stdout': 'retry completed\n',
                'finish': 'stop', 'delay': 0.1, 'echo_task': False,
            }],
        })
        result = self.wait(job_id, timeout=10)
        self.assertEqual(result['state'], 'done', result)
        attempts = store.Store(str(self.state)).attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(attempts[0]['stalled'])
        self.assertTrue(attempts[0]['stop_signalled'])
        self.assertFalse(attempts[0]['cancel_signalled'])
        self.assertFalse(attempts[1]['stalled'])
        self.assertNotEqual(attempts[0]['group_pgid'], attempts[1]['group_pgid'])
        self.assertIn('retry completed', pathlib.Path(result['stdout_path']).read_text())
        self.assert_group_gone(job_id)

    def test_idle_interval_starts_after_delayed_backend_release(self):
        ready = self.root / 'idle-release-ready'
        release = self.root / 'idle-release-go'
        started = self.root / 'idle-backend-started'
        finished = self.root / 'idle-backend-finished'
        job_id = self.launch({
            'idle_timeout': 0.4, 'delay': 0.15, 'echo_task': False,
            'start_file': str(started), 'finish_file': str(finished),
            'supervisor_fault': 'after_backend_group_commit',
            'fault_ready': str(ready), 'fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=5))
        time.sleep(0.7)
        release.write_text('release')
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done', result)
        self.assertTrue(started.exists(), 'backend did not start after barrier release')
        self.assertTrue(finished.exists(), 'backend did not receive the full idle interval')
        attempt = self.db.attempts(job_id)[0]
        self.assertFalse(attempt['stop_signalled'])
        self.assert_group_gone(job_id)

    def test_truncated_attempts_retry_then_exhaust_as_failed(self):
        job_id = self.launch({
            'session_id': 'truncated-session', 'finish': 'length', 'echo_task': False,
            'retry_attempts': [
                {'session_id': 'truncated-session', 'finish': 'length', 'echo_task': False},
                {'session_id': 'truncated-session', 'finish': 'length', 'echo_task': False},
            ],
        })
        result = self.wait(job_id, timeout=10)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['failure_reason'], 'truncated')
        attempts = store.Store(str(self.state)).attempts(job_id)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(item['truncated'] for item in attempts))
        self.assertEqual([item['attempt_index'] for item in attempts], [1, 2, 3])
        self.assert_group_gone(job_id)

    def test_cancellation_during_retry_attempt_stops_without_another_attempt(self):
        retry_started = self.root / 'retry-started'
        job_id = self.launch({
            'session_id': 'cancel-retry-session', 'finish': 'length', 'echo_task': False,
            'retry_attempts': [{
                'session_id': 'cancel-retry-session', 'start_file': str(retry_started),
                'delay': 30, 'echo_task': False,
            }, {'session_id': 'cancel-retry-session', 'stdout': 'must not run',
                'echo_task': False}],
        })
        self.assertTrue(self._wait_until(retry_started.exists, timeout=6))
        lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        result = self.wait(job_id, timeout=10)
        self.assertEqual(result['state'], 'cancelled')
        attempts = store.Store(str(self.state)).attempts(job_id)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(attempts[1]['cancel_signalled'])
        self.assertFalse(attempts[1]['stop_signalled'])
        self.assert_group_gone(job_id)

    def test_bash_term_trap_runs_before_any_kill_escalation(self):
        started = self.root / 'bash-started'
        marker = self.root / 'term-marker'
        script = (
            'trap \'printf trapped > "$TERM_MARKER"; exit 23\' TERM; '
            'printf started > "$TERM_STARTED"; while :; do sleep 0.1; done'
        )
        job_id = self.launch({
            'argv': ['/bin/bash', '-c', script],
            'env': {'TERM_MARKER': str(marker), 'TERM_STARTED': str(started)},
            'echo_task': False,
        })
        self.assertTrue(self._wait_until(started.exists, timeout=5))
        lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'cancelled')
        self.assertTrue(marker.exists(), 'Bash did not receive TERM with its default disposition')
        attempt = store.Store(str(self.state)).attempts(job_id)[0]
        self.assertTrue(attempt['cancel_signalled'])
        self.assertFalse(attempt['kill_sent'], 'TERM trap should finish before KILL escalation')
        self.assertEqual(attempt['exit_code'], 23)
        self.assert_group_gone(job_id)

    def test_term_trap_exit_zero_after_effective_cancel_is_cancelled(self):
        started = self.root / 'bash-zero-started'
        marker = self.root / 'term-zero-marker'
        script = (
            'trap \'printf trapped > "$TERM_MARKER"; exit 0\' TERM; '
            'printf started > "$TERM_STARTED"; while :; do sleep 0.1; done'
        )
        job_id = self.launch({
            'argv': ['/bin/bash', '-c', script],
            'env': {'TERM_MARKER': str(marker), 'TERM_STARTED': str(started)},
            'echo_task': False,
        })
        self.assertTrue(self._wait_until(started.exists, timeout=5))
        lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'cancelled')
        self.assertIsNotNone(result['cancel_confirmed_at'])
        self.assertTrue(marker.exists())
        attempt = self.db.attempts(job_id)[0]
        self.assertTrue(attempt['cancel_signalled'])
        self.assertEqual(attempt['exit_code'], 0)
        self.assertFalse(attempt['kill_sent'])
        self.assert_group_gone(job_id)

    def test_claim_conflict_waits_for_guard_group_cleanup_before_failure(self):
        owner = self._insert_running_row('conflicting-live-session', 0)
        backend_started = self.root / 'conflict-backend-started'
        job_id = self.launch({
            'session_id': 'conflicting-live-session', 'session_delay': 0.1,
            'child_sleep': 30, 'child_ignore_term': True,
            'delay': 30, 'start_file': str(backend_started), 'echo_task': False,
        })
        self.assertTrue(self._wait_until(backend_started.exists, timeout=5))
        result = self.wait(job_id, timeout=12)
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['failure_reason'], 'session_claim_conflict')
        attempt = self.db.attempts(job_id)[0]
        self.assertTrue(attempt['kill_sent'], 'TERM-ignoring group was not escalated before failure')
        self.assertTrue(attempt['cleanup_confirmed'])
        self.assertEqual(lifecycle._lock_state(
            lifecycle.artifact_paths(result)['guard_lock'],
        ), lifecycle.LOCK_FREE)
        with self.db.connect() as conn:
            claim = conn.execute(
                'SELECT job_id FROM session_claims WHERE backend_session_id=?',
                ('conflicting-live-session',),
            ).fetchone()
        self.assertEqual(claim['job_id'], owner)
        self.assert_group_gone(job_id)

    def test_group_emptiness_ignores_zombies_and_package_has_no_per_pid_signal(self):
        zombie_file = self.root / 'zombie-pid'
        job_id = self.launch({
            'session_id': 'zombie-session', 'zombie_child': True,
            'zombie_pid_file': str(zombie_file), 'delay': 5, 'echo_task': False,
        })
        self.assertTrue(self._wait_until(zombie_file.exists, timeout=5))
        job = self._wait_until(lambda: self.job(job_id) if self.job(job_id)['group_pgid'] else None)
        self.assertIsNotNone(job)
        zombie_pid = int(zombie_file.read_text())
        if sys.platform.startswith('linux'):
            zombie_entry = self._proc_entry_for_namespace_pid(zombie_pid)
            self.assertIsNotNone(zombie_entry, 'could not locate the held zombie in procfs')
            zombie_proc_pid, zombie_state, zombie_group_key = zombie_entry
        else:
            stat = subprocess.check_output(
                ['ps', '-o', 'stat=', '-p', str(zombie_pid)], text=True,
            ).strip()
            zombie_proc_pid, zombie_state, zombie_group_key = zombie_pid, stat[0], job['group_proc_key']
        self.assertEqual(zombie_state, 'Z')
        self.assertEqual(zombie_group_key, job['group_proc_key'])
        members = lifecycle._group_live_members(job['group_proc_key'])
        self.assertNotIn(zombie_proc_pid, members)
        self.assertTrue(members, 'live backend leader should remain in the group')
        lifecycle.request_cancel(store.Store(str(self.state)), job_id)
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'cancelled')
        self.assert_group_gone(job_id)

        package = SCRIPTS / 'relay'
        excluded_dirs = {'_archive', 'archive', '_dev', 'dev', '__pycache__'}
        for directory, child_dirs, filenames in os.walk(package):
            child_dirs[:] = [name for name in child_dirs if name not in excluded_dirs]
            for filename in filenames:
                if not filename.endswith('.py'):
                    continue
                path = pathlib.Path(directory) / filename
                if path.is_symlink():
                    continue
                self.assertNotIn('os.kill(', path.read_text(), str(path))
        self.assertIn('os.killpg(pgid, signal.SIGTERM)',
                      (package / 'lifecycle.py').read_text())

    def test_concurrent_large_stdout_and_stderr_streams_remain_separate(self):
        stdout_size = 240000
        stderr_size = 240000
        job_id = self.launch({
            'stdout_repeat': stdout_size, 'stderr_repeat': stderr_size, 'echo_task': False,
        })
        result = self.wait(job_id, timeout=10)
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(len(pathlib.Path(result['stdout_path']).read_bytes()), stdout_size)
        self.assertEqual(len(pathlib.Path(result['stderr_path']).read_bytes()), stderr_size)
        self.assertEqual(pathlib.Path(result['stdout_path']).read_bytes()[:1], b'O')
        self.assertEqual(pathlib.Path(result['stderr_path']).read_bytes()[:1], b'E')
        self.assert_group_gone(job_id)

    def test_notification_and_terminal_updates_serialize_without_lost_rows(self):
        job_id = self.launch({'delay': 0.15, 'echo_task': False})
        barrier = threading.Barrier(3)
        errors = []

        def terminal_writer():
            barrier.wait()
            try:
                self.wait(job_id)
            except Exception as exc:
                errors.append(exc)

        def notification_writer():
            barrier.wait()
            try:
                store.Store(str(self.state)).upsert_notification(job_id, 'transport', 'pending')
                store.Store(str(self.state)).upsert_notification(job_id, 'transport', 'sent')
            except Exception as exc:
                errors.append(exc)

        one = threading.Thread(target=terminal_writer)
        two = threading.Thread(target=notification_writer)
        one.start(); two.start(); barrier.wait()
        one.join(timeout=8); two.join(timeout=8)
        self.assertFalse(one.is_alive() or two.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.job(job_id)['state'], 'done')
        self.assertEqual(store.Store(str(self.state)).notifications(job_id)[0]['state'], 'sent')

    def test_claude_waiter_settlement_respects_live_lock_and_reregistration(self):
        job_id = self.launch({'stdout': 'waiter result\n'})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        with self.db.transaction() as conn:
            job = self.db.get_job(job_id, conn)
            options = dict(job.get('options') or {})
            options['claude_waiter'] = {
                'state': 'registered', 'command': 'python3 relay-job.py wait {}'.format(job_id),
            }
            self.db.update_job(conn, job_id, options=options)
            self.db.insert_notification(conn, job_id, 'claude-waiter', 'pending')

        abandoned = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(abandoned['state'], 'done')
        notification = self.db.notifications(job_id)[0]
        self.assertEqual(notification['state'], 'uncertain')
        self.assertIn('Claude waiter exited', notification['last_error'])
        self.assertEqual(abandoned['options']['claude_waiter']['state'], 'uncertain')

        ready = self.root / 'claude-waiter-ready'
        release = self.root / 'claude-waiter-release'
        process = subprocess.Popen(
            [sys.executable, str(HARNESS), 'claude-waiter', job_id, str(ready), str(release)],
            cwd=str(self.cwd_a), env=self.base_env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.background.append(process)
        try:
            self.assertTrue(self._wait_until(ready.exists, timeout=5),
                            'Claude waiter did not reach its completion barrier')
            registered = self.db.get_job(job_id)
            notification = self.db.notifications(job_id)[0]
            self.assertEqual(registered['options']['claude_waiter']['state'], 'registered')
            self.assertEqual(notification['state'], 'pending')
            self.assertEqual(notification['attempts'], 0)
            self.assertIsNone(notification['last_error'])
            waiter_lock = lifecycle.artifact_paths(registered)['claude_waiter_lock']
            self.assertEqual(lifecycle._lock_state(waiter_lock), lifecycle.LOCK_HELD)

            protected = lifecycle.reconcile_job(self.db, job_id)
            self.assertEqual(protected['state'], 'done')
            self.assertEqual(self.db.notifications(job_id)[0]['state'], 'pending')
        finally:
            release.touch()
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                self.fail('Claude waiter remained after releasing its completion barrier')
        self.assertEqual(process.returncode, 0, stderr or stdout)
        completed = self.db.get_job(job_id)
        notification = self.db.notifications(job_id)[0]
        self.assertEqual(completed['state'], 'done')
        self.assertEqual(completed['options']['claude_waiter']['state'], 'completed')
        self.assertEqual(notification['state'], 'uncertain')
        self.assertEqual(notification['attempts'], 1)
        self.assertIn('relay cannot observe whether Claude re-invoked the originating conversation',
                      notification['last_error'])

    def test_interrupted_prune_is_finished_and_metadata_is_retained(self):
        job_id = self.launch({'stdout': 'collectable\n'})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, ended_at='2000-01-01T00:00:00.000000Z')
        result = subprocess.run(
            [sys.executable, str(HARNESS), 'prune-fault', str(self.state), job_id,
             'between_prune_mark_and_delete'], env=self.base_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 81, result.stderr)
        self.assertEqual(self.job(job_id)['prune_state'], 'pruning')
        status = self.run_cli('status', job_id, '--json')
        self.assertEqual(status.returncode, 0, status.stderr)
        job = json.loads(status.stdout)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['prune_state'], 'pruned')
        self.assertFalse(pathlib.Path(job['artifact_dir']).exists())
        output = self.run_cli('result', job_id)
        self.assertEqual(output.returncode, 1)
        self.assertIn('pruned', output.stderr)

    def test_concurrent_pruners_have_one_token_owner_and_delete_once(self):
        job_id = self.launch({'stdout': 'prune once\n'})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        with self.db.transaction() as conn:
            self.db.update_job(conn, job_id, ended_at='2000-01-01T00:00:00.000000Z')
        started = threading.Event()
        release = threading.Event()
        deleted = []
        original_rmtree = shutil.rmtree

        def hold_first_delete(path):
            deleted.append(path)
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError('prune test did not release its delete barrier')
            return original_rmtree(path)

        outcomes = []
        def prune():
            outcomes.append(lifecycle.prune_job(self.db, job_id, time.time() + 1))

        with mock.patch.object(lifecycle.shutil, 'rmtree', side_effect=hold_first_delete):
            first = threading.Thread(target=prune)
            first.start()
            self.assertTrue(started.wait(timeout=3), 'first pruner did not own deletion')
            second_result = lifecycle.prune_job(self.db, job_id, time.time() + 1)
            release.set()
            first.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertEqual(outcomes, [True])
        self.assertFalse(second_result)
        self.assertEqual(len(deleted), 1)
        pruned = self.job(job_id)
        self.assertEqual(pruned['prune_state'], 'pruned')
        self.assertIsNone(pruned['prune_token'])
        self.assertFalse(pathlib.Path(pruned['artifact_dir']).exists())

    def test_storage_busy_returns_exit_seven_without_changing_existing_job(self):
        job_id = self.launch({'echo_task': False})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        before = self.job(job_id)['state']
        connection = self.db.connect()
        connection.execute('BEGIN IMMEDIATE')
        try:
            result = self.run_cli(
                'run', '--backend', 'opencode', '--route', 'sub', '--stdin',
                env=self.base_env, timeout=12, input_text='blocked insert\n',
            )
        finally:
            connection.execute('ROLLBACK')
            connection.close()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('busy', result.stderr.lower())
        self.assertEqual(self.job(job_id)['state'], before)
        self.assertEqual(len(self.db.list_jobs()), 1)

    def test_storage_error_classifies_only_busy_or_locked_as_contention(self):
        for message in ('database is locked', 'database is busy'):
            with self.subTest(message=message):
                error = store.Store._storage_error(sqlite3.OperationalError(message))
                self.assertIsInstance(error, store.StorageContentionError)

        for message in (
                'attempt to write a readonly database', 'database disk image is malformed',
                'disk I/O error'):
            with self.subTest(message=message):
                error = store.Store._storage_error(sqlite3.OperationalError(message))
                self.assertIs(type(error), store.StorageError)

    def test_storage_retry_does_not_retry_permanent_failure(self):
        def fail_permanently():
            raise store.StorageError('state database error: injected read-only failure')

        with mock.patch.object(lifecycle.time, 'sleep') as sleep:
            with self.assertRaisesRegex(store.StorageError, 'injected read-only failure'):
                lifecycle._retry_storage(fail_permanently)
        sleep.assert_not_called()

    def test_storage_permanent_failure_returns_cli_exit_seven(self):
        corrupt_state = self.root / 'corrupt-state'
        corrupt_state.mkdir()
        (corrupt_state / 'jobs.sqlite3').write_bytes(b'not a SQLite database')
        env = dict(self.base_env, RELAY_JOB_STATE_DIR=str(corrupt_state))
        result = self.run_cli('status', '--all', env=env)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('state database error', result.stderr)

    def test_permanent_storage_failure_during_active_attempt_cleans_backend(self):
        backend_started = self.root / 'storage-active-backend-started'
        fault_marker = self.root / 'storage-active-fault'
        job_id = self.launch({
            'start_file': str(backend_started), 'delay': 30, 'echo_task': False,
            'storage_fault': 'after_backend_release',
            'storage_fault_marker': str(fault_marker),
        })
        self.assertTrue(self._wait_until(backend_started.exists, timeout=5))
        job = self.job(job_id)
        paths = lifecycle.artifact_paths(job)
        self.assertTrue(self._wait_until(
            lambda: (
                fault_marker.exists()
                and lifecycle._lock_state(paths['supervisor_lock']) == lifecycle.LOCK_FREE
                and lifecycle._lock_state(paths['guard_lock']) == lifecycle.LOCK_FREE
            ), timeout=8,
        ), 'permanent storage failure did not leave the supervisor and guard')
        attempt = self.db.attempts(job_id)[0]
        self.assertIsNotNone(attempt['group_proc_key'])
        self.assertEqual(lifecycle._group_live_members(attempt['group_proc_key']), [])
        log = pathlib.Path(paths['supervisor_log']).read_text()
        self.assertIn('injected read-only database failure', log)
        fault_marker.unlink()

        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        self.assertNotEqual(recovered['state'], 'done')
        self.assertEqual(len(self.db.attempts(job_id)), 1)
        self.assert_group_gone(job_id)
        self.pending.discard(job_id)

    def test_permanent_storage_failure_at_terminal_persistence_reconciles_crashed(self):
        fault_marker = self.root / 'storage-terminal-fault'
        job_id = self.launch({
            'delay': 0.1, 'echo_task': False,
            'storage_fault': 'before_terminal_commit',
            'storage_fault_marker': str(fault_marker),
        })
        job = self.job(job_id)
        paths = lifecycle.artifact_paths(job)
        self.assertTrue(self._wait_until(
            lambda: (
                fault_marker.exists()
                and lifecycle._lock_state(paths['supervisor_lock']) == lifecycle.LOCK_FREE
                and lifecycle._lock_state(paths['guard_lock']) == lifecycle.LOCK_FREE
            ), timeout=8,
        ), 'terminal storage failure did not leave the supervisor')
        self.assertEqual(self.job(job_id)['state'], 'running')
        log = pathlib.Path(paths['supervisor_log']).read_text()
        self.assertIn('injected read-only database failure', log)
        fault_marker.unlink()

        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        self.assertNotEqual(recovered['state'], 'done')
        self.assert_group_gone(job_id)
        self.pending.discard(job_id)

    def test_release_marker_storage_failure_stops_held_trampoline(self):
        backend_started = self.root / 'storage-release-backend-started'
        fault_marker = self.root / 'storage-release-fault'
        job_id = self.launch({
            'start_file': str(backend_started), 'delay': 30, 'echo_task': False,
            'storage_fault': 'before_release_marker_commit',
            'storage_fault_marker': str(fault_marker),
        })
        job = self.job(job_id)
        paths = lifecycle.artifact_paths(job)
        self.assertTrue(self._wait_until(
            lambda: (
                fault_marker.exists()
                and lifecycle._lock_state(paths['supervisor_lock']) == lifecycle.LOCK_FREE
                and lifecycle._lock_state(paths['guard_lock']) == lifecycle.LOCK_FREE
            ), timeout=8,
        ), 'release marker storage failure did not stop the held trampoline')
        self.assertFalse(backend_started.exists())
        log = pathlib.Path(paths['supervisor_log']).read_text()
        self.assertIn('could not commit backend release marker', log)
        self.assertIn('injected read-only database failure', log)
        fault_marker.unlink()

        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'crashed')
        self.assertNotEqual(recovered['state'], 'done')
        self.assert_group_gone(job_id)
        self.pending.discard(job_id)

    def test_uncommitted_group_identity_storage_failure_stays_unresolved(self):
        backend_started = self.root / 'storage-uncommitted-backend-started'
        fault_marker = self.root / 'storage-uncommitted-fault'
        job_id = self.launch({
            'start_file': str(backend_started), 'delay': 30, 'echo_task': False,
            'storage_fault': 'before_backend_group_commit',
            'storage_fault_marker': str(fault_marker),
        })
        job = self.job(job_id)
        paths = lifecycle.artifact_paths(job)
        self.assertTrue(self._wait_until(
            lambda: (
                fault_marker.exists()
                and lifecycle._lock_state(paths['supervisor_lock']) == lifecycle.LOCK_FREE
                and lifecycle._lock_state(paths['guard_lock']) == lifecycle.LOCK_FREE
            ), timeout=8,
        ), 'uncommitted identity failure did not leave the supervisor and guard')
        self.assertFalse(backend_started.exists())
        attempt = self.db.attempts(job_id)[0]
        self.assertIsNone(attempt['group_proc_key'])
        log = pathlib.Path(paths['supervisor_log']).read_text()
        self.assertIn('injected read-only database failure', log)
        fault_marker.unlink()

        recovered = lifecycle.reconcile_job(self.db, job_id)
        self.assertEqual(recovered['state'], 'ownership_unresolved')
        self.assertNotEqual(recovered['state'], 'done')

        # Record independently proven cleanup so fixture cleanup can finish.
        self.assertEqual(lifecycle._lock_state(paths['guard_lock']), lifecycle.LOCK_FREE)
        with self.db.transaction() as conn:
            self.db.update_attempt(
                conn, job_id, 1, not_started=True, cleanup_confirmed=True,
                ended_at=store.now_utc(), spawn_error='fixture confirmed held trampoline cleanup',
            )
        self.assertEqual(lifecycle.reconcile_job(self.db, job_id)['state'], 'crashed')
        self.pending.discard(job_id)

    def test_supervisor_retries_session_claim_contention_while_backend_keeps_running(self):
        job_id = self.launch({
            'session_id': 'contention-session', 'session_delay': 0.2,
            'delay': 9, 'echo_task': False,
        })
        started = self._wait_until(
            lambda: self.job(job_id) if self.job(job_id)['group_pgid'] else None,
            timeout=5,
        )
        self.assertIsNotNone(started)
        connection = self.db.connect()
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE jobs SET label=label WHERE id=?', (job_id,))
        try:
            # The supervisor's first session claim hits the standard five-second SQLite
            # busy budget, then retries it while the guard and backend remain live.
            time.sleep(6.5)
            self.assertTrue(lifecycle._group_live_members(started['group_proc_key']))
        finally:
            connection.execute('COMMIT')
            connection.close()
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(result['backend_session_id'], 'contention-session')
        self.assertIsNone(result['failure_reason'])
        self.assertEqual(len(store.Store(str(self.state)).attempts(job_id)), 1)
        self.assert_group_gone(job_id)

    def test_terminal_commit_retries_after_sqlite_busy(self):
        ready = self.root / 'terminal-busy-ready'
        release = self.root / 'terminal-busy-release'
        job_id = self.launch({
            'delay': 0.1, 'echo_task': False, 'supervisor_fault': 'before_terminal_commit',
            'fault_ready': str(ready), 'fault_release': str(release),
        })
        self.assertTrue(self._wait_until(ready.exists, timeout=5))
        connection = self.db.connect()
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE jobs SET label=label WHERE id=?', (job_id,))
        pathlib.Path(release).write_text('continue')
        try:
            time.sleep(5.3)
        finally:
            connection.execute('COMMIT')
            connection.close()
        result = self.wait(job_id, timeout=8)
        self.assertEqual(result['state'], 'done', result)
        self.assertNotIn('supervisor error', pathlib.Path(result['artifact_dir'], 'supervisor.log').read_text())
        self.assert_group_gone(job_id)

    def test_reads_overlap_a_short_writer_without_losing_job_state(self):
        job_id = self.launch({'echo_task': False})
        self.assertEqual(self.wait(job_id)['state'], 'done')
        ready = threading.Event()
        release = threading.Event()

        def short_writer():
            connection = self.db.connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('UPDATE jobs SET label=? WHERE id=?', ('writer', job_id))
                ready.set()
                release.wait(timeout=2)
                connection.execute('COMMIT')
            finally:
                connection.close()

        thread = threading.Thread(target=short_writer)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))
        started = time.monotonic()
        status = self.run_cli('status', job_id, '--json', timeout=3)
        elapsed = time.monotonic() - started
        release.set()
        thread.join(timeout=3)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertLess(elapsed, 3)
        self.assertEqual(json.loads(status.stdout)['state'], 'done')
        self.assertEqual(self.job(job_id)['state'], 'done')

    def test_newer_and_unsupported_older_schema_versions_are_refused(self):
        with self.db.connect() as connection:
            connection.execute('PRAGMA user_version=99')
        newer = self.run_cli('status', '--all')
        self.assertEqual(newer.returncode, 7)
        self.assertIn('newer than supported', newer.stderr)
        with sqlite3.connect(str(self.state / 'jobs.sqlite3')) as connection:
            connection.execute('PRAGMA user_version=0')
        older = self.run_cli('status', '--all')
        self.assertEqual(older.returncode, 7)
        self.assertIn('unsupported', older.stderr)

    def test_run_wait_ctrl_c_cancels_detached_work_without_killing_caller(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        pid_file = self.root / 'backend.pid'
        opencode = bin_dir / 'opencode'
        opencode.write_text(
            '#!/bin/sh\n'
            'if [ "${1:-}" = "run" ]; then pgid="$(ps -o pgid= -p $$ | tr -d " ")"; echo "$$ $pgid" > "$RELAY_TEST_BACKEND_PID"; sleep 60; exit 0; fi\n'
            'if [ "${1:-}" = "session" ]; then echo "[]"; exit 0; fi\n'
            'exit 0\n'
        )
        opencode.chmod(0o700)
        env = dict(self.base_env)
        env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
        env['RELAY_TEST_BACKEND_PID'] = str(pid_file)
        env['OC_IDLE_TIMEOUT'] = '600'
        process = subprocess.Popen(
            [sys.executable, str(JOB_PATH), 'run', '--backend', 'opencode', '--route', 'sub',
             '--wait', '--dir', str(self.cwd_a), '--stdin'],
            cwd=str(self.cwd_a), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.background.append(process)
        process.stdin.write('cancel this task\n')
        process.stdin.close()
        process.stdin = None
        self.assertTrue(self._wait_until(pid_file.exists, timeout=8))
        backend_pid, backend_pgid = [int(value) for value in pid_file.read_text().split()]
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=12)
        self.background.remove(process)
        self.assertEqual(process.returncode, 3, stderr)
        os.kill(os.getpid(), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(backend_pid, 0)
        rows = self.db.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'cancelled')
        self.assertEqual(backend_pgid, rows[0]['group_pgid'])
        self.assertNotEqual(rows[0]['group_pgid'], os.getpgrp())
        self.assertIn('cancelled', (stdout + stderr).lower())

    def test_schema_has_required_tables_and_wal_durability_pragmas(self):
        with self.db.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
            self.assertEqual(connection.execute('PRAGMA synchronous').fetchone()[0], 2)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )}
        self.assertTrue({'jobs', 'session_claims', 'notifications', 'attempts'}.issubset(tables))


if __name__ == '__main__':
    unittest.main()
