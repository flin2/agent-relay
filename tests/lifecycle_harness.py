#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-only adapter and entry points for lifecycle fault injection."""

import json
import os
import pathlib
import signal
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'plugins/agent-relay/scripts'))

from relay import cli, lifecycle, store
from relay.adapters import Adapter, ExitInfo, Invocation, Outcome, register_adapter


class StubAdapter(Adapter):
    name = 'stub'

    def validate(self, options):
        return dict(options)

    def prepare(self, job, artifacts, resume_session_id=None):
        config = dict((job.get('options') or {}).get('stub') or {})
        return self._invocation(job, artifacts, config)

    def _invocation(self, job, artifacts, config):
        config.setdefault(
            'fixture_identity_file',
            str(pathlib.Path(artifacts['dir']) / 'fixture-backend-identities'),
        )
        if config.get('spawn_error'):
            argv = ['/agent-relay-test-missing-executable']
        elif config.get('argv'):
            argv = list(config['argv'])
        else:
            argv = [sys.executable, str(pathlib.Path(__file__).resolve()), 'backend', json.dumps(config)]
        return Invocation(argv, job['cwd'], dict(config.get('env') or {}), artifacts['task'])

    def observe_session(self, artifacts):
        path = artifacts.get('stdout')
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    if line.startswith('[test-session] id='):
                        return line.split('=', 1)[1].strip()
        except OSError:
            return None
        return None

    def idle_timeout(self, job):
        value = (job.get('options') or {}).get('stub', {}).get('idle_timeout')
        return float(value) if value is not None else None

    def next_attempt(self, job, artifacts, attempt_index, attempt_result):
        if not (attempt_result.get('stalled') or attempt_result.get('truncated')):
            return None
        config = dict((job.get('options') or {}).get('stub') or {})
        retry_plan = config.get('retry_attempts') or []
        position = int(attempt_index) - 1
        if position >= len(retry_plan):
            return None
        next_config = dict(config)
        next_config.pop('retry_attempts', None)
        next_config.update(retry_plan[position])
        return self._invocation(job, artifacts, next_config)

    def classify(self, exit_info, artifacts, attempt_result=None, attempt_history=None):
        session = self.observe_session(artifacts)
        attempt_result = attempt_result or {}
        if attempt_result.get('truncated'):
            return Outcome('failed', 'truncated', session, False)
        if attempt_result.get('stalled'):
            return Outcome('failed', 'stalled', session, False)
        if exit_info.exit_code == 0 and not exit_info.exit_signal:
            return Outcome('done', None, session, False)
        return Outcome('failed', 'stub_exit_{}'.format(exit_info.exit_code), session, False)


register_adapter(StubAdapter)


def _fault_at(target):
    def fault(point):
        if point == target:
            os._exit(81)
    return fault


def _fields(cwd, config, session_id=None, parent_id=None):
    now = store.now_utc()
    return {
        'host': 'claude', 'host_conversation_id': 'lifecycle-test',
        'host_conversation_key': 'claude:lifecycle-test', 'backend': 'stub',
        'route': None, 'gateway': None, 'workspace': cwd, 'cwd': cwd,
        'label': 'lifecycle fixture', 'model': None, 'effort': None, 'write': False,
        'parent_job_id': parent_id, 'backend_session_id': session_id,
        'created_at': now, 'updated_at': now,
        'options': {'backend': 'stub', 'stub': config},
    }


def _supervisor_command(job_id, fault=None):
    command = [sys.executable, str(pathlib.Path(__file__).resolve()), 'supervisor', job_id]
    if fault:
        command.extend(('--fault', fault))
    return command


def _fault_hook(target, ready_path=None, release_path=None):
    def fault(point):
        if point != target:
            return
        if ready_path and release_path:
            pathlib.Path(ready_path).write_text(point)
            while not pathlib.Path(release_path).exists():
                time.sleep(0.01)
            return
        os._exit(81)
    return fault


def _install_storage_fault(marker_path):
    if not marker_path:
        return
    original_connect = store.Store.connect

    def connect(instance):
        if pathlib.Path(marker_path).exists():
            raise store.StorageError(
                'state database error: injected read-only database failure',
            )
        return original_connect(instance)

    store.Store.connect = connect


def _storage_fault_hook(target, marker_path, previous):
    def fault(point):
        previous(point)
        if point == target:
            pathlib.Path(marker_path).write_text(point)
    return fault


def _supervisor_command_for_config(job_id, config):
    command = _supervisor_command(job_id, config.get('supervisor_fault'))
    for key, flag in (
        ('fault_ready', '--fault-ready'), ('fault_release', '--fault-release'),
        ('guard_fault', '--guard-fault'),
        ('guard_fault_ready', '--guard-fault-ready'),
        ('guard_fault_release', '--guard-fault-release'),
        ('guard_group_key_unavailable', '--group-key-unavailable'),
        ('storage_fault', '--storage-fault'),
        ('storage_fault_marker', '--storage-fault-marker'),
    ):
        if config.get(key):
            command.extend((flag, config[key]))
    return command


def backend(config_json):
    config = json.loads(config_json)
    if config.get('fixture_identity_file'):
        stat_path = pathlib.Path('/proc/self/stat')
        start_time = ''
        proc_group_key = os.getpgrp()
        try:
            stat_fields = stat_path.read_text().rsplit(')', 1)[1].split()
            start_time = stat_fields[19]
        except (OSError, IndexError):
            pass
        try:
            for line in pathlib.Path('/proc/self/status').read_text().splitlines():
                if line.startswith('NSpgid:'):
                    proc_group_key = int(line.partition(':')[2].split()[0])
                    break
        except (OSError, ValueError, IndexError):
            pass
        with open(config['fixture_identity_file'], 'a', encoding='ascii') as stream:
            stream.write('{} {} {} {}\n'.format(
                os.getpid(), os.getpgrp(), start_time or '-', proc_group_key,
            ))
            stream.flush()
    if config.get('start_file'):
        pathlib.Path(config['start_file']).write_text(str(os.getpid()))
    if config.get('zombie_child'):
        zombie = subprocess.Popen([sys.executable, '-c', 'pass'])
        if config.get('zombie_pid_file'):
            deadline = time.monotonic() + 5.0
            while True:
                result = subprocess.run(
                    ['ps', '-o', 'stat=', '-p', str(zombie.pid)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, check=False,
                )
                states = result.stdout.split()
                if result.returncode == 0 and states and states[0].startswith('Z'):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('zombie fixture child did not exit')
                time.sleep(0.01)
            pathlib.Path(config['zombie_pid_file']).write_text(str(zombie.pid))
    if config.get('ignore_term'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if config.get('session_delay'):
        time.sleep(float(config['session_delay']))
    if config.get('session_id'):
        sys.stdout.write('[test-session] id={}\n'.format(config['session_id']))
        sys.stdout.flush()
    if config.get('stdout'):
        sys.stdout.write(config['stdout'])
        sys.stdout.flush()
    if config.get('stderr'):
        sys.stderr.write(config['stderr'])
        sys.stderr.flush()
    if config.get('finish'):
        sys.stderr.write('[relay-completion] finish={}\n'.format(config['finish']))
        sys.stderr.flush()
    if config.get('stdout_repeat'):
        remaining = int(config['stdout_repeat'])
        while remaining:
            count = min(16384, remaining)
            sys.stdout.buffer.write(b'O' * count)
            remaining -= count
        sys.stdout.buffer.flush()
    if config.get('stderr_repeat'):
        remaining = int(config['stderr_repeat'])
        while remaining:
            count = min(16384, remaining)
            sys.stderr.buffer.write(b'E' * count)
            remaining -= count
        sys.stderr.buffer.flush()
    if config.get('echo_task', True):
        data = sys.stdin.buffer.read()
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    if config.get('child_sleep'):
        child_script = 'import time; '
        if config.get('child_ignore_term'):
            child_script += 'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        child_script += 'time.sleep({})'.format(config['child_sleep'])
        subprocess.Popen([sys.executable, '-c', child_script])
    if config.get('child_code'):
        subprocess.Popen([sys.executable, '-c', config['child_code']])
        if config.get('child_ready_file'):
            deadline = time.monotonic() + 5.0
            while not pathlib.Path(config['child_ready_file']).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('child fixture did not become ready')
                time.sleep(0.01)
    if config.get('delay'):
        time.sleep(float(config['delay']))
    if config.get('finish_file'):
        pathlib.Path(config['finish_file']).write_text('finished')
    return int(config.get('exit_code', 0))


def main(argv):
    if not argv:
        return 2
    command, args = argv[0], argv[1:]
    if command == 'backend':
        return backend(args[0])
    if command == 'supervisor':
        job_id = args[0]
        options = dict(zip(args[1::2], args[2::2]))
        fault = options.get('--fault')
        storage_fault = options.get('--storage-fault')
        storage_fault_marker = options.get('--storage-fault-marker')
        _install_storage_fault(storage_fault_marker)
        if fault:
            lifecycle._fault = _fault_hook(
                fault, options.get('--fault-ready'), options.get('--fault-release'),
            )
        if storage_fault:
            lifecycle._fault = _storage_fault_hook(
                storage_fault, storage_fault_marker, lifecycle._fault,
            )
        guard_builder = None
        if (options.get('--guard-fault') or options.get('--group-key-unavailable')
                or storage_fault):
            guard_fault = options.get('--guard-fault')
            guard_ready = options.get('--guard-fault-ready')
            guard_release = options.get('--guard-fault-release')

            def guard_builder(g_job_id, control_fd, status_fd, invocation_path):
                command = [
                    sys.executable, str(pathlib.Path(__file__).resolve()), 'guard', g_job_id,
                    str(control_fd), str(status_fd), invocation_path,
                ]
                if guard_fault:
                    command.extend((
                        '--fault', guard_fault, '--fault-ready', guard_ready,
                        '--fault-release', guard_release,
                    ))
                if options.get('--group-key-unavailable'):
                    command.extend(('--group-key-unavailable', '1'))
                if storage_fault:
                    command.extend((
                        '--storage-fault', storage_fault,
                        '--storage-fault-marker', storage_fault_marker,
                    ))
                return command
        lock_fd = int(options['--lock-fd']) if '--lock-fd' in options else None
        return lifecycle.supervise(
            lifecycle._resolve_state_root(), job_id, lock_fd,
            guard_command_builder=guard_builder,
        )
    if command == 'guard':
        job_id, control_fd, status_fd, invocation_path = args[:4]
        options = dict(zip(args[4::2], args[5::2]))
        storage_fault = options.get('--storage-fault')
        storage_fault_marker = options.get('--storage-fault-marker')
        _install_storage_fault(storage_fault_marker)
        if options.get('--fault'):
            lifecycle._fault = _fault_hook(
                options['--fault'], options.get('--fault-ready'), options.get('--fault-release'),
            )
        if storage_fault:
            lifecycle._fault = _storage_fault_hook(
                storage_fault, storage_fault_marker, lifecycle._fault,
            )
        if options.get('--group-key-unavailable'):
            lifecycle._proc_group_key_for_child = lambda *args, **kwargs: None
        return lifecycle.guard(job_id, int(control_fd), int(status_fd), invocation_path)
    if command == 'launch-fault':
        root, fault = args[0], args[1]
        lifecycle._fault = _fault_at(fault)
        cwd = args[2]
        config = json.loads(args[3])
        lifecycle.launch(
            root, _fields(cwd, config), 'test task\n',
            supervisor_command_builder=lambda job_id: _supervisor_command_for_config(job_id, config),
        )
        return 0
    if command == 'prune-fault':
        root, job_id, fault = args[0], args[1], args[2]
        lifecycle._fault = _fault_at(fault)
        db = store.Store(root)
        return 0 if lifecycle.prune_job(db, job_id, time.time() + 1) else 1
    if command == 'claude-waiter':
        job_id, ready, release = args[0], args[1], args[2]
        lifecycle._fault = _fault_hook(
            'claude_waiter_before_completion_record', ready, release,
        )
        return cli.cmd_wait([job_id, '--claude-waiter'])
    if command == 'lock-guard':
        root, job_id, ready = args[0], args[1], args[2]
        path = pathlib.Path(root) / 'jobs' / job_id / 'guard.lock'
        path.touch(mode=0o600, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        pathlib.Path(ready).write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
