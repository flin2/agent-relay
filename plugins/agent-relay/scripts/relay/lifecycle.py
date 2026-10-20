# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Detached job launch, supervision, process ownership, recovery, and pruning."""

import datetime
import atexit
import concurrent.futures
import errno
import fcntl
import json
import os
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from . import store as store_module
from .adapters import ExitInfo, get_adapter


TERMINAL = frozenset(('done', 'failed', 'cancelled', 'crashed'))
LOCK_HELD = 'held'
LOCK_FREE = 'free'
LOCK_ERROR = 'error'
HOST_CONTEXT_KEYS = frozenset((
    'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
    'CODEX_THREAD_ID', 'CODEX_SESSION_ID',
))
CODEX_QUEUE_TRANSPORT = 'codex-queue'
CLAUDE_WAITER_TRANSPORT = 'claude-waiter'
CODEX_QUEUE_TIMEOUT = 30.0
# Group membership must not depend on the job's PATH, which may be empty or hostile.
PS_EXECUTABLE = '/bin/ps'
NOTIFICATION_ERROR_LIMIT = 1000
RECONCILIATION_NOTIFICATION_ERROR = (
    'Reconciliation recorded this terminal outcome without a notification sender; '
    'delivery is uncertain.'
)
ABANDONED_CODEX_NOTIFICATION_ERROR = (
    'The supervisor exited while the Codex queue notification was pending; '
    'delivery is uncertain.'
)
ABANDONED_CLAUDE_WAITER_ERROR = (
    'The Claude waiter exited before local completion was recorded; the relay cannot '
    'observe whether Claude re-invoked the originating conversation.'
)
_DETACHED_SUPERVISORS = []
_CANCELLATION_UNSUPPORTED = (
    'cancellation unsupported on this platform: neither waitid/WNOWAIT nor kqueue/NOTE_EXIT is available'
)


def _reap_detached_supervisors():
    global _DETACHED_SUPERVISORS
    remaining = []
    for process in _DETACHED_SUPERVISORS:
        if process.poll() is None:
            remaining.append(process)
    _DETACHED_SUPERVISORS = remaining


def _detach_supervisors_at_exit():
    for process in _DETACHED_SUPERVISORS:
        if process.poll() is None:
            # The launcher exits without waiting by design; let the OS reparent the
            # detached supervisor and suppress Popen's misleading live-child warning.
            process._child_created = False


atexit.register(_detach_supervisors_at_exit)


def _fault(point):
    """Test-only monkeypatch seam. Production builds leave this as a no-op."""


def _append_supervisor_log(paths, message):
    try:
        with open(paths['supervisor_log'], 'ab') as stream:
            stream.write(('{}\n'.format(message)).encode('utf-8', 'replace'))
    except OSError:
        pass


def _timestamp():
    return store_module.now_utc()


def _make_job_id():
    return str(uuid.uuid4())


def _host(environ=None):
    environ = os.environ if environ is None else environ
    explicit = environ.get('RELAY_JOB_HOST', '').strip().lower()
    if explicit in ('claude', 'codex'):
        return explicit
    if environ.get('CLAUDE_CODE_SESSION_ID'):
        return 'claude'
    if environ.get('CODEX_THREAD_ID') or environ.get('CODEX_SESSION_ID'):
        return 'codex'
    return None


def _conversation_id(host=None, environ=None):
    environ = os.environ if environ is None else environ
    explicit = environ.get('RELAY_JOB_CONVERSATION_ID', '').strip()
    if explicit:
        return explicit
    host = _host(environ) if host is None else host
    if host == 'claude':
        return environ.get('CLAUDE_CODE_SESSION_ID') or None
    if host == 'codex':
        return environ.get('CODEX_THREAD_ID') or environ.get('CODEX_SESSION_ID') or None
    return None


def _conversation_key(host=None, conversation_id=None, environ=None):
    host = _host(environ) if host is None else host
    conversation_id = _conversation_id(host, environ) if conversation_id is None else conversation_id
    if not conversation_id:
        return None
    return '{}:{}'.format(host or 'unknown', conversation_id)


def _resolve_state_root(environ=None):
    return store_module.resolve_state_root(environ)


def _ensure_state_root(root=None):
    return store_module.ensure_state_root(root or _resolve_state_root())


def _job_dir(state_root, job_id):
    if not job_id or os.path.basename(job_id) != job_id or job_id in ('.', '..'):
        raise ValueError('invalid job id: {}'.format(job_id))
    return os.path.join(state_root, 'jobs', job_id)


def artifact_paths(job_or_dir):
    directory = job_or_dir if isinstance(job_or_dir, str) else job_or_dir['artifact_dir']
    return {
        'dir': directory,
        'task': os.path.join(directory, 'task.txt'),
        'stdout': os.path.join(directory, 'stdout.log'),
        'stderr': os.path.join(directory, 'stderr.log'),
        'events': os.path.join(directory, 'events.jsonl'),
        'response': os.path.join(directory, 'final-response.txt'),
        'session_id': os.path.join(directory, 'backend-session-id'),
        'invocation': os.path.join(directory, 'invocation.json'),
        'supervisor_log': os.path.join(directory, 'supervisor.log'),
        'supervisor_lock': os.path.join(directory, 'supervisor.lock'),
        'guard_lock': os.path.join(directory, 'guard.lock'),
        'claude_waiter_lock': os.path.join(directory, 'claude-waiter.lock'),
    }


def _fsync_directory(path):
    flags = getattr(os, 'O_DIRECTORY', 0) | os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, data, mode=0o600):
    directory = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix='.relay-tmp-', dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            if isinstance(data, str):
                data = data.encode('utf-8')
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
        _fsync_directory(directory)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _write_private(path, data=b''):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if isinstance(data, str):
            data = data.encode('utf-8')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _sync_job_artifacts(paths):
    errors = []
    for key in ('stdout', 'stderr', 'events', 'response'):
        path = paths.get(key)
        if not path or not os.path.isfile(path):
            continue
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            errors.append('{}: {}'.format(key, exc))
    try:
        _fsync_directory(paths['dir'])
    except OSError as exc:
        errors.append('artifact directory: {}'.format(exc))
    return errors


def _lock_file(path, blocking=True):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    os.set_inheritable(fd, False)
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, operation)
    except (BlockingIOError, OSError) as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError) or getattr(exc, 'errno', None) in (11, 35, 36):
            return None
        raise
    return fd


def _lock_state(path):
    try:
        fd = _lock_file(path, blocking=False)
        if fd is None:
            return LOCK_HELD
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return LOCK_FREE
    except OSError:
        return LOCK_ERROR


def _preparation_lock(root):
    return os.path.join(root, 'preparation.lock')


def _allocate_artifact_dir(root):
    jobs = os.path.join(root, 'jobs')
    for _ in range(16):
        job_id = _make_job_id()
        path = _job_dir(root, job_id)
        try:
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
            return job_id, path
        except FileExistsError:
            continue
    raise OSError('could not allocate a unique job artifact directory under {}'.format(jobs))


def launch(root, fields, task_text, supervisor_command_builder=None):
    """Prepare a durable job, close SQLite, then start its detached supervisor."""
    root = _ensure_state_root(root)
    db = store_module.Store(root)
    prep_fd = _lock_file(_preparation_lock(root))
    if prep_fd is None:
        raise store_module.StorageError('could not acquire preparation lock')
    job_id = None
    artifact_dir = None
    supervisor_fd = None
    try:
        job_id, artifact_dir = _allocate_artifact_dir(root)
        paths = artifact_paths(artifact_dir)
        # Reserve supervisor ownership before the created row can be observed. The same
        # locked descriptor is inherited by the child, closing the launch/register gap.
        supervisor_fd = _lock_file(paths['supervisor_lock'], blocking=False)
        if supervisor_fd is None:
            raise store_module.StorageError('could not acquire the new supervisor lock')
        now = _timestamp()
        job = dict(fields)
        job.update({
            'id': job_id,
            'state': 'created',
            'created_at': now,
            'updated_at': now,
            'artifact_dir': artifact_dir,
            'task_path': paths['task'],
            'stdout_path': paths['stdout'],
            'stderr_path': paths['stderr'],
            'events_path': paths['events'],
            'response_path': paths['response'],
            'options': fields.get('options') or {},
            'write': bool(fields.get('write')),
        })
        conn = db.connect()
        try:
            with db.transaction(conn):
                db.insert_job(conn, job)
                if job.get('backend_session_id') and not db.claim_session(
                    conn, job_id, job['backend'], job['backend_session_id'],
                ):
                    raise ValueError(
                        'backend session {} is already claimed by another job'.format(
                            job['backend_session_id'],
                        )
                    )
        finally:
            conn.close()
        _fault('after_insert')
        write_atomic(paths['task'], task_text, 0o600)
        _fault('after_task_write')
        with db.transaction() as conn:
            db.update_job(conn, job_id, task_ready=1)
        if supervisor_command_builder:
            command = list(supervisor_command_builder(job_id))
        else:
            command = [
                sys.executable,
                os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'relay-job.py')),
                '_supervisor', job_id,
            ]
        command.extend(('--lock-fd', str(supervisor_fd)))
        log_fd = os.open(paths['supervisor_log'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(log_fd, 0o600)
        try:
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                    cwd=job.get('cwd') or None, start_new_session=True, close_fds=True,
                    pass_fds=(supervisor_fd,),
                )
            except OSError as exc:
                _fail_startup(
                    db, job_id, 'startup: supervisor spawn failed: {}'.format(exc),
                    notify=True,
                )
                raise ValueError('cannot start job supervisor: {}'.format(exc))
        finally:
            os.close(log_fd)
        os.close(supervisor_fd)
        supervisor_fd = None
        _reap_detached_supervisors()
        _DETACHED_SUPERVISORS.append(process)
        _fault('after_spawn')
        registration_deadline = time.monotonic() + 2.0
        while time.monotonic() < registration_deadline:
            try:
                registered = db.get_job(job_id)
            except store_module.StorageContentionError:
                time.sleep(0.05)
                continue
            if registered and registered['state'] != 'created':
                break
            time.sleep(0.02)
    except Exception:
        if job_id and artifact_dir:
            try:
                if not db.get_job(job_id) and os.path.isdir(artifact_dir):
                    shutil.rmtree(artifact_dir)
            except Exception:
                pass
        raise
    finally:
        if supervisor_fd is not None:
            try:
                os.close(supervisor_fd)
            except OSError:
                pass
        fcntl.flock(prep_fd, fcntl.LOCK_UN)
        os.close(prep_fd)
    return job_id


def _retry_storage(operation, initial_delay=0.05, max_delay=1.0):
    """Retry typed database contention forever with a capped exponential backoff."""
    delay = initial_delay
    while True:
        try:
            return operation()
        except store_module.StorageContentionError:
            time.sleep(delay)
            delay = min(max_delay, delay * 2)


def _insert_pending_notification(db, conn, job):
    if (job and job.get('host') == 'codex' and job.get('host_conversation_id')
            and shutil.which('codex')):
        db.insert_notification(conn, job['id'], CODEX_QUEUE_TRANSPORT, 'pending')


def _insert_reconciliation_notification(db, conn, job):
    if (job and job.get('host') == 'codex' and job.get('host_conversation_id')
            and shutil.which('codex')):
        db.insert_notification(
            conn, job['id'], CODEX_QUEUE_TRANSPORT, 'uncertain',
            last_error=RECONCILIATION_NOTIFICATION_ERROR,
        )


def _settle_abandoned_notification(db, job):
    if not job or job['state'] not in TERMINAL:
        return job
    if job.get('host') == 'codex':
        transport = CODEX_QUEUE_TRANSPORT
        lock_path = artifact_paths(job)['supervisor_lock']
        error = ABANDONED_CODEX_NOTIFICATION_ERROR
    elif job.get('host') == 'claude':
        transport = CLAUDE_WAITER_TRANSPORT
        lock_path = artifact_paths(job)['claude_waiter_lock']
        error = ABANDONED_CLAUDE_WAITER_ERROR
    else:
        return job
    row = next((item for item in db.notifications(job['id'])
                if item['transport'] == transport), None)
    if not row or row['state'] != 'pending':
        return job

    try:
        owner_fd = _lock_file(lock_path, blocking=False)
    except OSError:
        return db.get_job(job['id'])
    if owner_fd is None:
        return job
    try:
        with db.transaction() as conn:
            current = db.get_job(job['id'], conn)
            if not current or current['state'] not in TERMINAL:
                return current
            row = next((item for item in db.notifications(job['id'], conn)
                        if item['transport'] == transport), None)
            if not row or row['state'] != 'pending':
                return current
            db.update_notification(
                conn, job['id'], transport, 'uncertain', last_error=error, attempts=1,
            )
            if transport == CLAUDE_WAITER_TRANSPORT:
                options = dict(current.get('options') or {})
                waiter = dict(options.get('claude_waiter') or {})
                if waiter.get('state') == 'registered':
                    waiter.update({
                        'state': 'uncertain', 'ended_at': _timestamp(), 'error': error,
                    })
                    options['claude_waiter'] = waiter
                    db.update_job(conn, job['id'], options=options)
            return db.get_job(job['id'], conn)
    finally:
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_UN)
            os.close(owner_fd)
        except OSError:
            pass


def _record_codex_notification(db, job_id, state, error=None):
    try:
        db.upsert_notification(
            job_id, CODEX_QUEUE_TRANSPORT, state, last_error=error, attempts=1,
        )
    except Exception:
        # Notification persistence cannot replace or roll back the committed job outcome.
        pass


def _attempt_codex_notification(db, job_id):
    try:
        rows = db.notifications(job_id)
        row = next((item for item in rows if item['transport'] == CODEX_QUEUE_TRANSPORT), None)
        job = db.get_job(job_id)
    except Exception:
        return
    if not row or row['state'] != 'pending' or not job or job['state'] not in TERMINAL:
        return

    payload = 'agent-relay job {} reached terminal state {}.\nState root: {}\nResult command: {}'.format(
        job_id, job['state'], db.root,
        collection_command(db.root, job_id, 'result'),
    )
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('RELAY_JOB_') or key in HOST_CONTEXT_KEYS:
            env.pop(key, None)
    argv = [
        'codex', 'queue', '--thread', job['host_conversation_id'], '--message', payload,
    ]
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, env=env,
            timeout=CODEX_QUEUE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        _record_codex_notification(
            db, job_id, 'uncertain', 'codex queue timed out after {} seconds'.format(
                int(CODEX_QUEUE_TIMEOUT),
            ),
        )
        return
    except KeyboardInterrupt:
        _record_codex_notification(db, job_id, 'uncertain', 'supervisor interrupted during codex queue')
        return
    except OSError as exc:
        _record_codex_notification(db, job_id, 'failed', 'codex queue could not start: {}'.format(exc))
        return
    except Exception as exc:
        _record_codex_notification(db, job_id, 'uncertain', 'codex queue delivery outcome is uncertain: {}'.format(exc))
        return

    if completed.returncode == 0:
        _record_codex_notification(db, job_id, 'sent')
        return
    stderr = completed.stderr or ''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    excerpt = stderr.strip()[:NOTIFICATION_ERROR_LIMIT]
    error = 'codex queue exited {}'.format(completed.returncode)
    if excerpt:
        error += ': ' + excerpt
    _record_codex_notification(db, job_id, 'failed', error)


def _fail_startup(db, job_id, reason, expected=('created',), expected_token=None,
                  notify=False):
    transitioned = False
    with db.transaction() as conn:
        current = db.get_job(job_id, conn)
        transitioned = db.cas_state(
            conn, job_id, expected, 'failed', expected_token=expected_token,
            failure_reason=reason, ended_at=_timestamp(),
        )
        if transitioned:
            db.release_claim(conn, job_id)
            _insert_pending_notification(db, conn, current)
    if transitioned and notify:
        _attempt_codex_notification(db, job_id)
    return transitioned


def _entrypoint():
    return os.path.realpath(os.path.join(os.path.dirname(__file__), '..', 'relay-job.py'))


def collection_command(state_root, job_id, operation, host=None):
    if operation not in ('wait', 'result', 'cancel'):
        raise ValueError('unsupported collection operation: {}'.format(operation))
    # Start with python3 so Claude's Bash(python3:*) permission pattern matches it.
    command = 'python3 {} --state-dir {} {} {}'.format(
        shlex.quote(_entrypoint()), shlex.quote(os.path.realpath(state_root)),
        operation, shlex.quote(job_id),
    )
    if operation == 'wait' and host == 'claude':
        command += ' --claude-waiter'
    return command


def _send_status(fd, line):
    try:
        os.write(fd, (line + '\n').encode('utf-8', 'replace'))
    except OSError:
        pass


def _status_lines(fd, timeout=0.0, buffer=b''):
    lines = []
    readable, _, _ = select.select([fd], [], [], timeout)
    if not readable:
        return lines, buffer, False
    try:
        chunk = os.read(fd, 65536)
    except BlockingIOError:
        return lines, buffer, False
    if not chunk:
        if buffer:
            lines.append(buffer.decode('utf-8', 'replace'))
        return lines, b'', True
    buffer += chunk
    while b'\n' in buffer:
        line, buffer = buffer.split(b'\n', 1)
        lines.append(line.decode('utf-8', 'replace'))
    return lines, buffer, False


def _read_ready(fd, process, timeout=10.0):
    deadline = time.monotonic() + timeout
    buffer = b''
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        lines, buffer, eof = _status_lines(fd, min(remaining, 0.25), buffer)
        for line in lines:
            parts = line.split()
            if parts and parts[0] == 'READY' and len(parts) >= 3:
                return int(parts[1]), int(parts[2])
            if parts and parts[0] == 'GUARD_ERROR':
                raise RuntimeError('guard startup failed: {}'.format(line.partition(' ')[2]))
        if process.poll() is not None or eof:
            break
    raise RuntimeError('guard did not report readiness before timeout')


def _write_control(fd, message):
    try:
        os.write(fd, message)
        return True
    except OSError:
        return False


def _stat_fields(stat_text):
    """Return (state, process-group) from /proc/PID/stat, or (None, None)."""
    close = stat_text.rfind(')')
    if close < 0:
        return None, None
    fields = stat_text[close + 1:].split()
    if len(fields) < 3:
        return None, None
    try:
        return fields[0], int(fields[2])
    except ValueError:
        return fields[0], None


def _proc_status_fields(status_text):
    """Parse the procfs fields needed for namespace-correct group ownership."""
    state = None
    nspid = None
    nspgid = None
    seen = set()
    for line in status_text.splitlines():
        if line.startswith('State:'):
            if 'State' in seen:
                raise ValueError('duplicate State field')
            seen.add('State')
            value = line.partition(':')[2].strip()
            state = value[:1] if value else None
            if state not in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I'):
                raise ValueError('malformed State field')
        elif line.startswith('NSpid:'):
            if 'NSpid' in seen:
                raise ValueError('duplicate NSpid field')
            seen.add('NSpid')
            values = line.partition(':')[2].split()
            try:
                nspid = [int(value) for value in values]
            except ValueError:
                raise ValueError('malformed NSpid field')
            if not nspid or any(value <= 0 for value in nspid):
                raise ValueError('invalid NSpid field')
        elif line.startswith('NSpgid:'):
            if 'NSpgid' in seen:
                raise ValueError('duplicate NSpgid field')
            seen.add('NSpgid')
            values = line.partition(':')[2].split()
            try:
                nspgid = [int(value) for value in values]
            except ValueError:
                raise ValueError('malformed NSpgid field')
            # Kernel threads and groups led from an outer namespace report 0, which never matches a held group key.
            if not nspgid or any(value < 0 for value in nspgid):
                raise ValueError('invalid NSpgid field')
    if not state:
        raise ValueError('missing State field')
    return state, nspid, nspgid


def _proc_entry_disappeared(exc):
    return getattr(exc, 'errno', None) in (errno.ENOENT, errno.ESRCH)


def _proc_group_key_for_child(child_pid, proc_root='/proc', own_pid=None):
    """Resolve a held child group's key in the procfs mount's own numbering."""
    child_pid = int(child_pid)
    if not sys.platform.startswith('linux'):
        return child_pid
    own_pid = os.getpid() if own_pid is None else int(own_pid)
    try:
        proc_self_pid = os.path.basename(os.readlink(os.path.join(proc_root, 'self')))
    except OSError:
        return None
    matching_mount = proc_self_pid == str(own_pid)
    if matching_mount:
        process_dirs = [os.path.join(proc_root, str(child_pid))]
    else:
        try:
            own_namespace = os.readlink(os.path.join(proc_root, 'self', 'ns', 'pid'))
            entries = os.listdir(proc_root)
        except OSError:
            return None
        process_dirs = []
        for entry in entries:
            if not entry.isdigit():
                continue
            process_dir = os.path.join(proc_root, entry)
            try:
                namespace = os.readlink(os.path.join(process_dir, 'ns', 'pid'))
            except OSError as exc:
                if _proc_entry_disappeared(exc):
                    continue
                continue
            if namespace != own_namespace:
                continue
            try:
                with open(os.path.join(process_dir, 'status'), 'r', encoding='ascii', errors='replace') as stream:
                    _, nspid, _ = _proc_status_fields(stream.read())
            except (OSError, UnicodeError, ValueError):
                # An unreadable unrelated process does not hide a readable match because inner PIDs are unique in this namespace; if the leader itself is unreadable, no matching entry is collected and startup fails.
                continue
            if nspid and nspid[-1] == child_pid:
                process_dirs.append(process_dir)
    if len(process_dirs) != 1:
        return None
    process_dir = process_dirs[0]
    try:
        with open(os.path.join(process_dir, 'status'), 'r', encoding='ascii', errors='replace') as stream:
            _, _, nspgid = _proc_status_fields(stream.read())
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, OSError) and _proc_entry_disappeared(exc):
            return None
        return None
    if nspgid:
        return nspgid[0] if nspgid[0] > 0 else None
    if not matching_mount:
        return None
    try:
        with open(os.path.join(process_dir, 'stat'), 'r', encoding='ascii', errors='replace') as stream:
            _, stat_pgid = _stat_fields(stream.read())
    except (OSError, UnicodeError) as exc:
        if isinstance(exc, OSError) and _proc_entry_disappeared(exc):
            return None
        return None
    return stat_pgid if stat_pgid and stat_pgid > 0 else None


def _linux_proc_group_members(group_key, proc_root='/proc', own_pid=None):
    """Return procfs-numbered members, or None when any membership evidence is unknown."""
    own_pid = os.getpid() if own_pid is None else int(own_pid)
    try:
        proc_self_pid = os.path.basename(os.readlink(os.path.join(proc_root, 'self')))
    except OSError:
        return None
    matching_mount = proc_self_pid == str(own_pid)
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    members = []
    for entry in entries:
        if not entry.isdigit():
            continue
        process_dir = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(process_dir, 'status'), 'r', encoding='ascii', errors='replace') as stream:
                state, _, nspgid = _proc_status_fields(stream.read())
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, OSError) and _proc_entry_disappeared(exc):
                continue
            return None
        if state == 'Z':
            continue
        if nspgid:
            proc_group = nspgid[0]
        elif matching_mount:
            try:
                with open(os.path.join(process_dir, 'stat'), 'r', encoding='ascii', errors='replace') as stream:
                    stat_state, proc_group = _stat_fields(stream.read())
            except (OSError, UnicodeError) as exc:
                if isinstance(exc, OSError) and _proc_entry_disappeared(exc):
                    continue
                return None
            if not stat_state or proc_group is None:
                return None
        else:
            return None
        if proc_group == group_key:
            try:
                members.append(int(entry))
            except ValueError:
                return None
    return sorted(members)


def _ps_group_members(pgid):
    """Use ps only after confirming it sees this process with its real PID."""
    own_pid = os.getpid()
    try:
        own = subprocess.run(
            [PS_EXECUTABLE, '-o', 'pid=', '-p', str(own_pid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        own_fields = own.stdout.decode('ascii').split()
        if (own.returncode or own.stderr.strip() or len(own_fields) != 1
                or int(own_fields[0]) != own_pid):
            return None
        result = subprocess.run(
            [PS_EXECUTABLE, '-A', '-o', 'pid=', '-o', 'pgid=', '-o', 'stat='],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode or result.stderr.strip():
        return None
    try:
        output = result.stdout.decode('ascii')
    except UnicodeError:
        return None
    members = []
    saw_self = False
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return None
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            return None
        if pid <= 0 or group <= 0 or not fields[2]:
            return None
        if pid == own_pid:
            saw_self = True
        if group == pgid and 'Z' not in fields[2]:
            members.append(pid)
    return sorted(members) if saw_self else None


def _group_live_members(group_key):
    """Return live members, with None meaning namespace membership is unknown."""
    if not group_key or group_key <= 1:
        return None
    if sys.platform.startswith('linux'):
        return _linux_proc_group_members(group_key)
    if sys.platform == 'darwin':
        return _ps_group_members(group_key)
    return None


def _process_group_pids(pgid):
    """Compatibility name returning only live (non-zombie) process-group members."""
    return _group_live_members(pgid)


def _waitid_supported():
    return callable(getattr(os, 'waitid', None)) and all(
        hasattr(os, name) for name in ('P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT')
    )


def _kqueue_exit_observer_supported():
    return callable(getattr(select, 'kqueue', None)) and all(
        hasattr(select, name) for name in (
            'kevent', 'KQ_FILTER_PROC', 'KQ_NOTE_EXIT', 'KQ_EV_ADD', 'KQ_EV_ENABLE',
        )
    )


def _leader_exit_observation_supported():
    return _waitid_supported() or _kqueue_exit_observer_supported()


class _KqueueLeaderExitObserver:
    """Observe a held child's exit without reaping it, keeping its process group reserved."""

    def __init__(self, pid):
        self._queue = select.kqueue()
        self._observed = False
        try:
            event = select.kevent(
                pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT,
            )
            registered = self._queue.control([event], 1, 0)
            self._observed = any(
                getattr(returned, 'ident', None) == pid
                and getattr(returned, 'filter', None) == select.KQ_FILTER_PROC
                and (getattr(returned, 'fflags', 0) or 0) & select.KQ_NOTE_EXIT
                for returned in (registered or ())
            )
        except BaseException:
            self._queue.close()
            raise

    def poll(self):
        if not self._observed:
            self._observed = bool(self._queue.control([], 1, 0))
        return self._observed

    def close(self):
        self._queue.close()


def _leader_wait_status(pid, observer=None):
    if _waitid_supported():
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if info is None or not info.si_pid:
            return None
        if info.si_code == getattr(os, 'CLD_EXITED', -1):
            return {'exit_code': info.si_status, 'exit_signal': None}
        return {'exit_code': None, 'exit_signal': info.si_status}
    if observer is None:
        raise RuntimeError(_CANCELLATION_UNSUPPORTED)
    return {'observed': True} if observer.poll() else None


def _reap_leader(child):
    """Reap a previously observed leader and return its authoritative wait status."""
    while True:
        try:
            pid, status = os.waitpid(child.pid, 0)
            break
        except InterruptedError:
            continue
    if pid != child.pid:
        raise ChildProcessError('waitpid did not return the held leader')
    if os.WIFEXITED(status):
        child.returncode = os.WEXITSTATUS(status)
        return {'exit_code': child.returncode, 'exit_signal': None}
    if os.WIFSIGNALED(status):
        child.returncode = -os.WTERMSIG(status)
        return {'exit_code': None, 'exit_signal': os.WTERMSIG(status)}
    raise ChildProcessError('waitpid returned a non-terminal status for the held leader')


def _send_leader_status(status_fd, result):
    if result is None or 'exit_code' not in result:
        return
    if result.get('exit_code') is not None:
        _send_status(status_fd, 'LEADER_STATUS exit={}'.format(result['exit_code']))
    else:
        _send_status(status_fd, 'LEADER_STATUS signal={}'.format(result.get('exit_signal')))


def _drain_exec_error(fd):
    """Return a queued exec error from a non-blocking pipe, or '' without waiting."""
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        return ''
    return data.decode('utf-8', 'replace').replace('\n', ' ')


def _cleanup_owned_group(pgid, group_key, child, status_fd, grace=5.0, request_kind=None,
                         observer=None):
    """Signal only the held leader's group and bound cleanup when membership is unknown."""
    if child is None or not pgid or pgid <= 1:
        return None, False, False, False
    leader_status = _leader_wait_status(child.pid, observer)
    try:
        members = _group_live_members(group_key)
    except Exception:
        members = None
    term_sent = False
    kill_sent = False
    if members is None or members:
        leader_before_term = _leader_wait_status(child.pid, observer)
        try:
            os.killpg(pgid, signal.SIGTERM)
            term_sent = True
        except (ProcessLookupError, PermissionError):
            pass
        if term_sent and leader_before_term is None:
            if request_kind == 'cancel':
                _send_status(status_fd, 'CANCEL_SIGNALLED')
            elif request_kind == 'stop':
                _send_status(status_fd, 'STOP_SIGNALLED')
            elif request_kind == 'abort':
                _send_status(status_fd, 'ABORT_SIGNALLED')
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            status = _leader_wait_status(child.pid, observer)
            if status is not None:
                leader_status = status
            try:
                members = _group_live_members(group_key)
            except Exception:
                members = None
            if members == [] and leader_status is not None:
                break
            time.sleep(0.1)
    needs_kill = members is None or bool(members) or leader_status is None
    if needs_kill:
        # The unreaped leader keeps PGID reserved through this final group-wide decision.
        try:
            os.killpg(pgid, signal.SIGKILL)
            kill_sent = True
            _send_status(status_fd, 'KILL_SENT')
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = _leader_wait_status(child.pid, observer)
            if status is not None:
                leader_status = status
            try:
                members = _group_live_members(group_key)
            except Exception:
                members = None
            if members == [] and leader_status is not None:
                break
            time.sleep(0.05)
    confirmed_empty = members == [] and leader_status is not None
    if leader_status is not None:
        # Never issue another signal after this reap because a KILL decision is the last point at which the saved group identity can be used safely if membership is unknown.
        leader_status = _reap_leader(child)
    return leader_status, term_sent, kill_sent, confirmed_empty


class GuardStartupError(RuntimeError):
    def __init__(self, message, safe, process=None, guard_pid=None, guard_session_pgid=None):
        super(GuardStartupError, self).__init__(message)
        self.safe = safe
        self.process = process
        self.guard_pid = guard_pid
        self.guard_session_pgid = guard_session_pgid


def guard(job_id, control_fd, status_fd, invocation_path):
    """Hold a trampoline leader until the supervisor has committed its group identity."""
    root = _resolve_state_root()
    job_dir = _job_dir(root, job_id)
    paths = artifact_paths(job_dir)
    lock_fd = None
    child = None
    leader_observer = None
    group_pgid = None
    group_proc_key = None
    invocation = None
    attempt_index = None
    gate_write = None
    exec_error_read = None
    released = False
    cleanup_finished = False
    signal_request = {'requested': False}

    def done(confirmed_empty):
        nonlocal cleanup_finished
        _send_status(status_fd, 'GUARD_DONE {}'.format(
            'empty' if confirmed_empty else 'unconfirmed',
        ))
        cleanup_finished = True

    def record_guard_storage(operation):
        try:
            return _retry_storage(operation)
        except store_module.StorageError as exc:
            _send_status(status_fd, 'GUARD_ERROR storage error: {}'.format(
                str(exc).replace('\n', ' '),
            ))
            return None

    def read_barrier_byte():
        while not signal_request['requested']:
            readable, _, _ = select.select([control_fd], [], [], 0.1)
            if readable:
                return os.read(control_fd, 1)
        return None

    def stop_held_group(request_cancel=False, supervisor_lost=False):
        nonlocal gate_write, child, group_pgid, group_proc_key
        if request_cancel:
            _send_status(status_fd, 'PRE_RELEASE_CANCEL')
        elif supervisor_lost:
            _send_status(status_fd, 'SUPERVISOR_LOST')
        else:
            _send_status(status_fd, 'PRE_RELEASE')
        if gate_write is not None:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = None
        if child is None:
            if attempt_index is not None:
                record_guard_storage(lambda: _mark_attempt_not_started(
                    store_module.Store(root), job_id, int(attempt_index), True,
                    None if request_cancel else (
                        'supervisor_lost_before_execution_release' if supervisor_lost else None
                    ),
                ))
            done(True)
            return 0
        leader_status, _, _, confirmed = _cleanup_owned_group(
            group_pgid, group_proc_key, child, status_fd, grace=0.5,
            observer=leader_observer,
        )
        _send_leader_status(status_fd, leader_status)
        if attempt_index is not None:
            record_guard_storage(lambda: _mark_attempt_not_started(
                store_module.Store(root), job_id, int(attempt_index), confirmed,
                None if request_cancel else (
                    'supervisor_lost_before_execution_release' if supervisor_lost else None
                ),
            ))
        done(confirmed)
        if leader_status and leader_status.get('exit_code') is not None:
            return leader_status['exit_code']
        return 0

    def fail_held_trampoline(reason):
        nonlocal gate_write, child
        if gate_write is not None:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = None
        _send_status(status_fd, 'SPAWN_ERROR {}'.format(str(reason).replace('\n', ' ')))
        leader_status = None
        confirmed = False
        if child is not None and group_pgid:
            # The trampoline has not crossed its exec barrier and has no children, so its direct-child status reserves the group while this final KILL is sent.
            try:
                os.killpg(group_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if leader_observer is None and not _waitid_supported():
                # The trampoline is still held at the exec barrier and has no descendants, so reap it after the final group signal.
                leader_status = _reap_leader(child)
                confirmed = True
            else:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    leader_status = _leader_wait_status(child.pid, leader_observer)
                    if leader_status is not None:
                        break
                    time.sleep(0.02)
                if leader_status is not None:
                    leader_status = _reap_leader(child)
                    confirmed = True
        _send_leader_status(status_fd, leader_status)
        if attempt_index is not None:
            record_guard_storage(lambda: _mark_attempt_not_started(
                store_module.Store(root), job_id, int(attempt_index), confirmed, str(reason),
            ))
        done(confirmed)
        return 127

    try:
        if not _leader_exit_observation_supported():
            raise RuntimeError(_CANCELLATION_UNSUPPORTED)
        lock_fd = _lock_file(paths['guard_lock'], blocking=False)
        if lock_fd is None:
            _send_status(status_fd, 'GUARD_ERROR duplicate guard')
            return 70

        def request_cleanup(signum, frame):
            signal_request['requested'] = True

        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, request_cleanup)
        with open(invocation_path, 'r', encoding='utf-8') as stream:
            invocation = json.load(stream)
        attempt_index = invocation.get('attempt_index')
        pid = os.getpid()
        _send_status(status_fd, 'READY {} {}'.format(pid, os.getpgrp()))
        first = read_barrier_byte()
        if first != b'R':
            return stop_held_group(request_cancel=(first == b'C'),
                                   supervisor_lost=(first == b''))
        try:
            env = os.environ.copy()
            for key in list(env):
                if key.startswith('RELAY_JOB_') or key in HOST_CONTEXT_KEYS:
                    env.pop(key, None)
            for key, value in (invocation.get('env_overrides') or {}).items():
                if key.startswith('RELAY_JOB_') or key in HOST_CONTEXT_KEYS:
                    continue
                env[key] = str(value)
            for key in invocation.get('env_remove') or ():
                env.pop(key, None)
            stdin_stream = open(invocation['stdin_path'], 'rb') if invocation.get('stdin_path') else None
            stdout_fd = os.open(paths['stdout'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            stderr_fd = os.open(paths['stderr'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            events_fd = os.open(paths['events'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            for fd in (stdout_fd, stderr_fd, events_fd):
                os.fchmod(fd, 0o600)
            gate_read, gate_write = os.pipe()
            exec_error_read, exec_error_write = os.pipe()
            os.set_inheritable(gate_read, True)
            os.set_inheritable(exec_error_write, True)
            trampoline = os.path.join(os.path.dirname(__file__), 'trampoline.py')
            command = [
                sys.executable, trampoline, str(gate_read), str(exec_error_write), '--',
            ]
            command.extend(invocation['argv'])
            try:
                child = subprocess.Popen(
                    command, stdin=stdin_stream or subprocess.DEVNULL,
                    stdout=stdout_fd, stderr=stderr_fd, cwd=invocation.get('cwd') or None,
                    env=env, close_fds=True, pass_fds=(gate_read, exec_error_write),
                    start_new_session=True,
                )
                group_pgid = child.pid
                if not _waitid_supported():
                    # Register while the trampoline is blocked at its exec barrier, before reporting the group or allowing release.
                    leader_observer = _KqueueLeaderExitObserver(child.pid)
                os.close(exec_error_write)
                os.set_blocking(exec_error_read, False)
            finally:
                os.close(gate_read)
                if stdin_stream:
                    stdin_stream.close()
                os.close(stdout_fd)
                os.close(stderr_fd)
                os.close(events_fd)
        except OSError as exc:
            if child is not None:
                return fail_held_trampoline(
                    'could not register leader exit observer: {}'.format(exc),
                )
            if exec_error_read is not None:
                os.close(exec_error_read)
                exec_error_read = None
            _send_status(status_fd, 'SPAWN_ERROR {}'.format(str(exc).replace('\n', ' ')))
            if attempt_index is not None:
                record_guard_storage(lambda: _mark_attempt_not_started(
                    store_module.Store(root), job_id, int(attempt_index), True,
                    'backend trampoline spawn failed: {}'.format(exc),
                ))
            done(True)
            return 127
        except Exception as exc:
            if child is not None:
                return fail_held_trampoline(
                    'could not register leader exit observer: {}'.format(exc),
                )
            if exec_error_read is not None:
                os.close(exec_error_read)
                exec_error_read = None
            _send_status(status_fd, 'SPAWN_ERROR {}'.format(str(exc).replace('\n', ' ')))
            if attempt_index is not None:
                record_guard_storage(lambda: _mark_attempt_not_started(
                    store_module.Store(root), job_id, int(attempt_index), True,
                    'backend trampoline spawn failed: {}'.format(exc),
                ))
            done(True)
            return 127

        group_proc_key = _proc_group_key_for_child(child.pid)
        if not group_proc_key:
            return fail_held_trampoline(
                'could not establish the backend process group key in procfs',
            )
        _send_status(status_fd, 'BACKEND_SPAWNED {} {} {}'.format(
            child.pid, group_pgid, group_proc_key,
        ))
        command = read_barrier_byte()
        if command != b'E':
            return stop_held_group(request_cancel=(command == b'C'),
                                   supervisor_lost=(command == b''))
        attempt_index = invocation.get('attempt_index')
        release_db = None
        if attempt_index is not None:
            _fault('before_release_marker_commit')
            try:
                release_db = store_module.Store(root)
                _retry_storage(lambda: _mark_attempt_released(
                    release_db, job_id, int(attempt_index), _timestamp(),
                ))
            except store_module.StorageError as exc:
                return fail_held_trampoline(
                    'startup: could not commit backend release marker: {}'.format(exc),
                )
        # The supervisor holds its pipe open for the attempt; if it disappeared while the release marker was being committed, close the gate instead of executing.
        os.set_blocking(control_fd, False)
        readable, _, _ = select.select([control_fd], [], [], 0)
        if readable:
            queued = os.read(control_fd, 1)
            if not queued:
                if release_db is not None:
                    try:
                        _retry_storage(lambda: _clear_attempt_release(
                            release_db, job_id, int(attempt_index),
                        ))
                    except store_module.StorageError as exc:
                        return fail_held_trampoline(
                            'startup: could not clear backend release marker: {}'.format(exc),
                        )
                return stop_held_group(supervisor_lost=True)
            if queued == b'C':
                if release_db is not None:
                    try:
                        _retry_storage(lambda: _clear_attempt_release(
                            release_db, job_id, int(attempt_index),
                        ))
                    except store_module.StorageError as exc:
                        return fail_held_trampoline(
                            'startup: could not clear backend release marker: {}'.format(exc),
                        )
                return stop_held_group(request_cancel=True)
            # S/A are only valid after release. Preserve any such command for the loop.
            pending_command = queued
        else:
            pending_command = None
        os.set_blocking(control_fd, True)
        try:
            os.write(gate_write, b'R')
            released = True
        finally:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = None
        _send_status(status_fd, 'BACKEND_RELEASED {} {}'.format(child.pid, group_pgid))
        os.set_blocking(control_fd, False)
        while True:
            leader_status = _leader_wait_status(child.pid, leader_observer)
            if leader_status is not None:
                _send_status(status_fd, 'LEADER_EXIT_OBSERVED')
                _fault('guard_after_leader_exit')
            if leader_status is not None or signal_request['requested']:
                if leader_status is not None and exec_error_read is not None:
                    # The trampoline writes any exec failure before it exits, so an observed
                    # exit already has its message queued.
                    error = _drain_exec_error(exec_error_read)
                    os.close(exec_error_read)
                    exec_error_read = None
                    if error:
                        _send_status(status_fd, 'SPAWN_ERROR {}'.format(error))
                leader_status, _, _, confirmed = _cleanup_owned_group(
                    group_pgid, group_proc_key, child, status_fd,
                    observer=leader_observer,
                )
                _send_leader_status(status_fd, leader_status)
                done(confirmed)
                if leader_status.get('exit_code') is not None:
                    return leader_status['exit_code']
                return 128 + (leader_status.get('exit_signal') or 0)
            if pending_command is not None:
                command, pending_command = pending_command, None
            else:
                readers = [control_fd]
                if exec_error_read is not None:
                    readers.append(exec_error_read)
                readable, _, _ = select.select(readers, [], [], 0.1)
                if exec_error_read is not None and exec_error_read in readable:
                    error = os.read(exec_error_read, 65536)
                    if error:
                        _send_status(status_fd, 'SPAWN_ERROR {}'.format(
                            error.decode('utf-8', 'replace').replace('\n', ' '),
                        ))
                        leader_status, _, _, confirmed = _cleanup_owned_group(
                            group_pgid, group_proc_key, child, status_fd,
                            request_kind='abort', observer=leader_observer,
                        )
                        _send_leader_status(status_fd, leader_status)
                        done(confirmed)
                        if leader_status and leader_status.get('exit_code') is not None:
                            return leader_status['exit_code']
                        return 127
                    error_fd = exec_error_read
                    os.close(error_fd)
                    exec_error_read = None
                    readable = [fd for fd in readable if fd != error_fd]
                if control_fd not in readable:
                    continue
                try:
                    command = os.read(control_fd, 1)
                except BlockingIOError:
                    continue
            if not command:
                _send_status(status_fd, 'SUPERVISOR_LOST')
                leader_status, _, _, confirmed = _cleanup_owned_group(
                    group_pgid, group_proc_key, child, status_fd,
                    observer=leader_observer,
                )
                _send_leader_status(status_fd, leader_status)
                done(confirmed)
                return 0
            if command in (b'C', b'S', b'A'):
                leader_status = _leader_wait_status(child.pid, leader_observer)
                if leader_status is not None:
                    _send_status(status_fd, 'LEADER_EXIT_OBSERVED')
                request_kind = {b'C': 'cancel', b'S': 'stop', b'A': 'abort'}[command]
                leader_status, _, _, confirmed = _cleanup_owned_group(
                    group_pgid, group_proc_key, child, status_fd,
                    request_kind=request_kind, observer=leader_observer,
                )
                _send_leader_status(status_fd, leader_status)
                done(confirmed)
                if leader_status.get('exit_code') is not None:
                    return leader_status['exit_code']
                return 128 + (leader_status.get('exit_signal') or 0)
    except BaseException as exc:
        reason = str(exc).replace('\n', ' ')
        if not released:
            _send_status(status_fd, 'SPAWN_ERROR startup: {}'.format(reason))
        _send_status(status_fd, 'GUARD_ERROR {}'.format(reason))
        if not released and gate_write is not None:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = None
        if not cleanup_finished and child is not None and child.returncode is None:
            try:
                if not released and not group_proc_key:
                    return fail_held_trampoline('guard failed before process group registration')
                leader_status, _, _, confirmed = _cleanup_owned_group(
                    group_pgid, group_proc_key, child, status_fd,
                    observer=leader_observer,
                )
                _send_leader_status(status_fd, leader_status)
                if not released and attempt_index is not None:
                    record_guard_storage(lambda: _mark_attempt_not_started(
                        store_module.Store(root), job_id, int(attempt_index), confirmed,
                        'guard_error_before_execution_release',
                    ))
                done(confirmed)
            except Exception:
                pass
        return 70
    finally:
        if gate_write is not None:
            try:
                os.close(gate_write)
            except OSError:
                pass
            gate_write = None
        if not cleanup_finished and child is not None and child.returncode is None:
            try:
                if not released and not group_proc_key:
                    fail_held_trampoline('guard failed before process group registration')
                else:
                    leader_status, _, _, confirmed = _cleanup_owned_group(
                        group_pgid, group_proc_key, child, status_fd,
                        observer=leader_observer,
                    )
                    _send_leader_status(status_fd, leader_status)
                    done(confirmed)
            except Exception:
                pass
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
        if leader_observer is not None:
            try:
                leader_observer.close()
            except OSError:
                pass
        for fd in (control_fd, status_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        if exec_error_read is not None:
            try:
                os.close(exec_error_read)
            except OSError:
                pass


def _mark_terminal(db, job_id, state, reason=None, exit_code=None, exit_signal=None,
                   session_id=None, cancelled=False, expected_state='running',
                   expected_token=None, clear_session_id=False):
    def commit():
        ended = _timestamp()
        with db.transaction() as conn:
            current = db.get_job(job_id, conn)
            if clear_session_id:
                chosen_session = None
            elif session_id is None:
                chosen_session = current.get('backend_session_id') if current else None
            else:
                chosen_session = session_id
            updates = {
                'failure_reason': reason,
                'exit_code': exit_code,
                'exit_signal': exit_signal,
                'backend_session_id': chosen_session,
                'ended_at': ended,
            }
            if cancelled:
                updates['cancel_confirmed_at'] = ended
            transitioned = db.cas_state(
                conn, job_id, (expected_state,), state,
                expected_token=expected_token, **updates,
            )
            if transitioned:
                db.release_claim(conn, job_id)
                _insert_pending_notification(db, conn, current)
            return transitioned

    # The supervisor retains its lifetime lock while a busy store recovers. A terminal
    # write is never converted into a different execution outcome because SQLite was busy.
    transitioned = _retry_storage(commit)
    if transitioned:
        _attempt_codex_notification(db, job_id)
    return transitioned


def _spawn_guard(job, paths, invocation, command_builder=None):
    write_atomic(paths['invocation'], json.dumps(invocation, sort_keys=True), 0o600)
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    os.set_inheritable(control_read, True)
    os.set_inheritable(status_write, True)
    log_fd = os.open(paths['supervisor_log'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(log_fd, 0o600)
    try:
        if command_builder:
            command = command_builder(job['id'], control_read, status_write, paths['invocation'])
        else:
            command = [
                sys.executable, _entrypoint(), '_guard', job['id'],
                str(control_read), str(status_write), paths['invocation'],
            ]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
            cwd=job.get('cwd') or None, start_new_session=True, close_fds=True,
            pass_fds=(control_read, status_write),
        )
    except Exception:
        for fd in (control_read, control_write, status_read, status_write):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        os.close(log_fd)
    os.close(control_read)
    os.close(status_write)
    os.set_blocking(status_read, False)
    try:
        guard_pid, guard_session_pgid = _read_ready(status_read, process)
        if guard_pid != process.pid or guard_session_pgid != process.pid:
            raise RuntimeError('guard readiness identity does not match its detached process group')
    except Exception as exc:
        os.close(control_write)
        safe = False
        try:
            process.wait(timeout=7)
            group_members = _process_group_pids(process.pid)
            safe = group_members == [] and _lock_state(paths['guard_lock']) == LOCK_FREE
        except subprocess.TimeoutExpired:
            # The guard is the only process authorized to clean its group. Control EOF
            # asks it to do so; never signal its saved PID here.
            try:
                process.wait(timeout=6)
                group_members = _process_group_pids(process.pid)
                safe = group_members == [] and _lock_state(paths['guard_lock']) == LOCK_FREE
            except subprocess.TimeoutExpired:
                safe = False
        except Exception:
            safe = False
        os.close(status_read)
        raise GuardStartupError(
            'guard did not establish readiness: {}'.format(exc), safe,
            process if not safe else None, process.pid, process.pid,
        )
    return process, control_write, status_read, guard_pid, guard_session_pgid


def _read_available_status(fd, buffer, timeout=0.0):
    lines, buffer, _ = _status_lines(fd, timeout, buffer)
    return lines, buffer


def _job_artifacts(job):
    paths = artifact_paths(job)
    return {
        'task': paths['task'], 'stdout': paths['stdout'], 'stderr': paths['stderr'],
        'events': paths['events'], 'response': paths['response'],
        'session_id': paths['session_id'], 'invocation': paths['invocation'],
        'dir': paths['dir'],
    }


def _inherited_supervisor_lock(path, lock_fd):
    if lock_fd is None:
        return _lock_file(path, blocking=False)
    try:
        expected = os.stat(path)
        actual = os.fstat(lock_fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            os.close(lock_fd)
            return None
        os.set_inheritable(lock_fd, False)
        return lock_fd
    except OSError:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        return None


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _attempt_finish_reason(path, start):
    try:
        with open(path, 'rb') as stream:
            stream.seek(start)
            lines = stream.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return 'unknown'
    result = 'unknown'
    for line in lines:
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == '[relay-completion]' and parts[1].startswith('finish='):
            value = parts[1].split('=', 1)[1]
            if value in ('stop', 'length', 'unknown'):
                result = value
    return result


def _mark_unresolved(db, job_id, expected_state, expected_token, reason, evidence):
    def commit():
        with db.transaction() as conn:
            return db.cas_state(
                conn, job_id, (expected_state,), 'ownership_unresolved',
                expected_token=expected_token, failure_reason=reason,
                ownership_evidence=json.dumps(evidence, sort_keys=True),
            )
    return _retry_storage(commit)


def _store_guard_identity(db, job_id, guard_pid):
    with db.transaction() as conn:
        db.update_job(conn, job_id, guard_pid=guard_pid)


def _claim_session(db, job_id, backend, session_id):
    with db.transaction() as conn:
        won = db.claim_session(conn, job_id, backend, session_id)
        if won:
            db.update_job(conn, job_id, backend_session_id=session_id)
        return won


def _store_backend_group(db, job_id, attempt_index, guard_pid, group_pgid, group_proc_key):
    with db.transaction() as conn:
        db.update_job(
            conn, job_id, guard_pid=guard_pid, group_pgid=group_pgid,
            group_proc_key=group_proc_key,
        )
        db.update_attempt(
            conn, job_id, attempt_index, guard_pid=guard_pid,
            group_pgid=group_pgid, group_proc_key=group_proc_key,
        )


def _begin_attempt(db, job_id, attempt):
    with db.transaction() as conn:
        db.insert_attempt(conn, attempt)
        # Do not let a previous attempt's group identity stand in for this attempt.
        db.update_job(conn, job_id, guard_pid=None, group_pgid=None, group_proc_key=None)


def _mark_attempt_released(db, job_id, attempt_index, released_at):
    with db.transaction() as conn:
        db.update_attempt(
            conn, job_id, attempt_index, released_at=released_at,
        )


def _clear_attempt_release(db, job_id, attempt_index):
    with db.transaction() as conn:
        db.update_attempt(
            conn, job_id, attempt_index, released_at=None,
        )


def _mark_attempt_not_started(db, job_id, attempt_index, cleanup_confirmed,
                              reason=None):
    with db.transaction() as conn:
        db.update_attempt(
            conn, job_id, attempt_index, not_started=True,
            cleanup_confirmed=cleanup_confirmed, ended_at=_timestamp(),
            spawn_error=reason,
        )


def supervise(root, job_id, supervisor_lock_fd=None, guard_command_builder=None):
    """Run sequential guarded attempts and commit only evidence-backed outcomes."""
    root = _ensure_state_root(root)
    job_dir = _job_dir(root, job_id)
    paths = artifact_paths(job_dir)
    supervisor_fd = _inherited_supervisor_lock(paths['supervisor_lock'], supervisor_lock_fd)
    if supervisor_fd is None:
        # Duplicate or invalid supervisors leave the job row untouched.
        return 0
    db = store_module.Store(root)
    guard_process = None
    control_fd = None
    status_fd = None
    guard_pid = None
    guard_session_pgid = None
    group_pgid = None
    group_proc_key = None
    current_state = 'created'
    token = None
    status_buffer = b''
    status_items = []
    attempt_history = []
    session_id = None
    clear_session_id = False
    final_state = None
    final_reason = None
    exit_code = None
    exit_signal = None
    write_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    write_pool_closed = False
    try:
        job = _retry_storage(lambda: db.get_job(job_id))
        if not job or not job.get('task_ready'):
            if job:
                _retry_storage(lambda: _fail_startup(
                    db, job_id, 'startup: task artifact was not ready', expected=('created',),
                    notify=True,
                ))
            return 1
        _fault('before_supervisor_registration')
        token = secrets.token_hex(16)

        def register():
            with db.transaction() as conn:
                return db.cas_state(
                    conn, job_id, ('created',), 'launching', expected_token=None,
                    supervisor_token=token, supervisor_pid=os.getpid(),
                    supervisor_started_at=_timestamp(),
                )

        if not _retry_storage(register):
            return 0
        current_state = 'launching'
        _fault('after_supervisor_registration')
        job = _retry_storage(lambda: db.get_job(job_id))
        adapter = get_adapter(job['backend'])
        job['options'] = adapter.validate(job.get('options') or {})
        artifacts = _job_artifacts(job)
        session_id = job.get('backend_session_id')
        invocation_value = adapter.prepare(
            job, artifacts,
            session_id if job.get('parent_job_id') else None,
        )
        invocation = {
            'argv': list(invocation_value.argv),
            'cwd': invocation_value.cwd,
            'env_overrides': dict(invocation_value.env_overrides or {}),
            'env_remove': list(invocation_value.env_remove or ()),
            'stdin_path': invocation_value.stdin_path,
        }
        attempt_index = 1
        idle_timeout = adapter.idle_timeout(job)
        while True:
            attempt_started_at = _timestamp()
            attempt = {
                'job_id': job_id, 'attempt_index': attempt_index,
                'started_at': attempt_started_at,
                'stdout_start': _file_size(paths['stdout']),
                'stderr_start': _file_size(paths['stderr']),
            }

            _retry_storage(lambda: _begin_attempt(db, job_id, attempt))
            guard_process = None
            control_fd = None
            status_fd = None
            guard_pid = None
            guard_session_pgid = None
            group_pgid = None
            group_proc_key = None
            attempt['cancel_signalled'] = False
            attempt['stop_signalled'] = False
            attempt['kill_sent'] = False
            attempt['guard_done'] = False
            attempt['cleanup_confirmed'] = False
            attempt['pre_release_cancel_ack'] = False
            attempt['leader_status'] = None
            attempt['spawn_error'] = None
            attempt['pre_release_cancel'] = False
            cancel_wanted = False
            cancel_command_sent = False
            stop_command_sent = False
            abort_command_sent = False
            session_claim_conflict = False
            pending_session_id = None
            claim_future = None
            claim_inflight_id = None
            claim_retry_at = 0.0
            claim_retry_delay = 0.05
            cancel_future = None
            cancel_retry_at = 0.0
            cancel_retry_delay = 0.1
            last_output_size = attempt['stdout_start'] + attempt['stderr_start']
            last_output_growth = None

            def handle_status_line(line):
                nonlocal group_pgid, group_proc_key, cancel_wanted, cancel_command_sent
                nonlocal control_fd, last_output_growth, last_output_size
                parts = line.split(maxsplit=1)
                if not parts:
                    return
                key = parts[0]
                value = parts[1] if len(parts) > 1 else ''
                if key == 'BACKEND_SPAWNED':
                    ids = value.split()
                    if len(ids) != 3:
                        attempt['spawn_error'] = 'guard reported an invalid backend identity'
                        if control_fd is not None:
                            _write_control(control_fd, b'A')
                        return
                    attempt['backend_pid'] = int(ids[0])
                    group_pgid = int(ids[1])
                    group_proc_key = int(ids[2])
                    attempt['group_pgid'] = group_pgid
                    attempt['group_proc_key'] = group_proc_key
                    # The group identity and this attempt's identity commit together while the trampoline is held and has not executed the backend.
                    _fault('before_backend_group_commit')
                    _retry_storage(lambda: _store_backend_group(
                        db, job_id, attempt_index, guard_pid, group_pgid, group_proc_key,
                    ))
                    attempt['group_recorded'] = True
                    _fault('after_backend_group_commit')
                    latest = _retry_storage(lambda: db.get_job(job_id))
                    if latest and latest.get('cancel_requested_at'):
                        cancel_wanted = True
                        attempt['pre_release_cancel'] = True
                        cancel_command_sent = True
                        if not _write_control(control_fd, b'C'):
                            attempt['spawn_error'] = 'guard control pipe closed before execution release'
                    elif not _write_control(control_fd, b'E'):
                        attempt['spawn_error'] = 'guard execution barrier could not be released'
                        if control_fd is not None:
                            try:
                                os.close(control_fd)
                            except OSError:
                                pass
                            control_fd = None
                elif key == 'BACKEND_RELEASED':
                    attempt['backend_released'] = True
                    attempt['released_at'] = attempt.get('released_at') or _timestamp()
                    last_output_size = _file_size(paths['stdout']) + _file_size(paths['stderr'])
                    last_output_growth = time.monotonic()
                    _fault('after_backend_release')
                elif key == 'LEADER_EXIT_OBSERVED':
                    attempt['leader_exit_observed_before_signal'] = True
                elif key == 'LEADER_STATUS':
                    status_fields = dict(
                        item.split('=', 1) for item in value.split() if '=' in item
                    )
                    if 'exit' in status_fields:
                        attempt['leader_status'] = {
                            'exit_code': int(status_fields['exit']), 'exit_signal': None,
                        }
                    elif 'signal' in status_fields:
                        attempt['leader_status'] = {
                            'exit_code': None, 'exit_signal': int(status_fields['signal']),
                        }
                elif key == 'CANCEL_SIGNALLED':
                    attempt['cancel_signalled'] = True
                elif key == 'STOP_SIGNALLED':
                    attempt['stop_signalled'] = True
                elif key == 'KILL_SENT':
                    attempt['kill_sent'] = True
                elif key == 'GUARD_DONE':
                    attempt['guard_done'] = True
                    attempt['cleanup_confirmed'] = value == 'empty'
                elif key == 'PRE_RELEASE_CANCEL':
                    attempt['pre_release_cancel'] = True
                    attempt['pre_release_cancel_ack'] = True
                    attempt['not_started'] = True
                elif key == 'SPAWN_ERROR':
                    attempt['spawn_error'] = value or 'backend spawn failed'
                    _append_supervisor_log(paths, 'guard startup: {}'.format(value or 'backend spawn failed'))
                elif key == 'GUARD_ERROR':
                    attempt['guard_error'] = value or 'guard error'
                    _append_supervisor_log(paths, 'guard error: {}'.format(value or 'guard error'))

            try:
                (guard_process, control_fd, status_fd, guard_pid,
                 guard_session_pgid) = _spawn_guard(
                    job, paths, dict(invocation, attempt_index=attempt_index),
                    command_builder=guard_command_builder,
                )
                attempt['guard_pid'] = guard_pid
            except GuardStartupError as exc:
                guard_process = exc.process
                guard_pid = exc.guard_pid
                guard_session_pgid = exc.guard_session_pgid
                attempt['guard_pid'] = guard_pid
                attempt['spawn_error'] = 'guard startup: {}'.format(exc)
                attempt['ended_at'] = _timestamp()
                _retry_storage(lambda: self_update_attempt(
                    db, job_id, attempt_index, attempt,
                ))
                if exc.safe:
                    if current_state == 'launching':
                        _retry_storage(lambda: _fail_startup(
                            db, job_id, 'startup: {}'.format(exc),
                            expected=('launching',), expected_token=token,
                            notify=True,
                        ))
                    else:
                        _mark_terminal(
                            db, job_id, 'failed', 'startup: {}'.format(exc),
                            session_id=session_id, expected_state=current_state,
                            expected_token=token,
                        )
                else:
                    _mark_unresolved(
                        db, job_id, current_state, token, 'guard_readiness_unresolved',
                        {'guard_pid': guard_pid, 'guard_session_pgid': guard_session_pgid,
                         'manual_recovery': 'verify backend group membership and guard lock; reconciliation never signals processes'},
                    )
                    if guard_process is not None:
                        guard_process._child_created = False
                return 1
            except OSError as exc:
                attempt['spawn_error'] = 'guard spawn: {}'.format(exc)
                attempt['ended_at'] = _timestamp()
                _retry_storage(lambda: self_update_attempt(
                    db, job_id, attempt_index, attempt,
                ))
                if current_state == 'launching':
                    _retry_storage(lambda: _fail_startup(
                        db, job_id, 'startup: cannot spawn guard: {}'.format(exc),
                        expected=('launching',), expected_token=token,
                        notify=True,
                    ))
                else:
                    _mark_terminal(
                        db, job_id, 'failed', 'startup: cannot spawn guard: {}'.format(exc),
                        session_id=session_id, expected_state=current_state,
                        expected_token=token,
                    )
                return 1

            if current_state == 'launching':
                def set_running():
                    with db.transaction() as conn:
                        return db.cas_state(
                            conn, job_id, ('launching',), 'running',
                            expected_token=token, guard_pid=guard_pid,
                        )
                transitioned = _retry_storage(set_running)
                if not transitioned:
                    _write_control(control_fd, b'C')
                    os.close(control_fd)
                    control_fd = None
                    guard_process.wait(timeout=7)
                    lines, status_buffer = _read_available_status(status_fd, status_buffer, 0.1)
                    status_items.extend(lines)
                    final_state, final_reason = 'crashed', 'startup_state_changed_before_barrier_release'
                    break
                current_state = 'running'
            else:
                _retry_storage(lambda: _store_guard_identity(
                    db, job_id, guard_pid,
                ))

            _fault('before_barrier_release')
            latest = _retry_storage(lambda: db.get_job(job_id))
            if latest and latest.get('cancel_requested_at'):
                cancel_wanted = True
                _write_control(control_fd, b'C')
                os.close(control_fd)
                control_fd = None
                attempt['pre_release_cancel'] = True
            elif not _write_control(control_fd, b'R'):
                attempt['spawn_error'] = 'guard start barrier could not be released'
                os.close(control_fd)
                control_fd = None
            else:
                _fault('after_barrier_release')

            if attempt['pre_release_cancel']:
                try:
                    guard_process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    pass
                lines, status_buffer = _read_available_status(status_fd, status_buffer, 0.1)
                status_items.extend(lines)
                for line in lines:
                    handle_status_line(line)
            elif attempt['spawn_error']:
                try:
                    guard_process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    pass
                lines, status_buffer = _read_available_status(status_fd, status_buffer, 0.1)
                status_items.extend(lines)
                for line in lines:
                    handle_status_line(line)
            else:
                next_cancel_poll = 0.0
                while True:
                    lines, status_buffer = _read_available_status(status_fd, status_buffer, 0)
                    status_items.extend(lines)
                    for line in lines:
                        handle_status_line(line)

                    now = time.monotonic()
                    output_size = _file_size(paths['stdout']) + _file_size(paths['stderr'])
                    if output_size > last_output_size:
                        last_output_size = output_size
                        last_output_growth = now

                    if now >= next_cancel_poll:
                        discovered = adapter.observe_session(artifacts)
                        if discovered and discovered != session_id:
                            pending_session_id = discovered
                        if (not cancel_wanted and not attempt['leader_status']
                                and cancel_future is None and now >= cancel_retry_at):
                            cancel_future = write_pool.submit(db.get_job, job_id)
                        next_cancel_poll = now + 1.0

                    if cancel_future is not None and cancel_future.done():
                        try:
                            current = cancel_future.result()
                        except store_module.StorageContentionError:
                            cancel_future = None
                            cancel_retry_at = now + cancel_retry_delay
                            cancel_retry_delay = min(1.0, cancel_retry_delay * 2)
                        else:
                            cancel_future = None
                            if current and current.get('cancel_requested_at'):
                                cancel_wanted = True
                            cancel_retry_delay = 0.1

                    if claim_future is not None and claim_future.done():
                        try:
                            won = claim_future.result()
                        except store_module.StorageContentionError:
                            claim_future = None
                            claim_inflight_id = None
                            claim_retry_at = now + claim_retry_delay
                            claim_retry_delay = min(1.0, claim_retry_delay * 2)
                        else:
                            claim_future = None
                            if won:
                                session_id = claim_inflight_id
                                if pending_session_id == claim_inflight_id:
                                    pending_session_id = None
                                claim_retry_delay = 0.05
                            else:
                                session_claim_conflict = True
                                attempt['spawn_error'] = 'session_claim_conflict'
                                if not abort_command_sent and control_fd is not None:
                                    abort_command_sent = _write_control(control_fd, b'A')
                            claim_inflight_id = None
                    if (pending_session_id and claim_future is None
                            and now >= claim_retry_at and not session_claim_conflict):
                        claim_value = pending_session_id
                        claim_future = write_pool.submit(
                            _claim_session, db, job_id, job['backend'], claim_value,
                        )
                        claim_inflight_id = claim_value

                    if cancel_wanted and not cancel_command_sent and control_fd is not None:
                        cancel_command_sent = _write_control(control_fd, b'C')
                    if (attempt.get('backend_released') and idle_timeout is not None
                            and last_output_growth is not None and not cancel_wanted
                            and not stop_command_sent and not session_claim_conflict
                            and now - last_output_growth >= idle_timeout
                            and control_fd is not None):
                        stop_command_sent = _write_control(control_fd, b'S')
                        if stop_command_sent:
                            attempt['stop_reason'] = 'idle_timeout'

                    if guard_process.poll() is not None:
                        lines, status_buffer = _read_available_status(status_fd, status_buffer, 0)
                        status_items.extend(lines)
                        # Process the final complete messages through the same protocol parser.
                        for line in lines:
                            handle_status_line(line)
                        break
                    time.sleep(0.05)

            if control_fd is not None:
                os.close(control_fd)
                control_fd = None
            if status_fd is not None:
                try:
                    guard_process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    pass
                lines, status_buffer = _read_available_status(status_fd, status_buffer, 0.1)
                status_items.extend(lines)
                for line in lines:
                    handle_status_line(line)
                os.close(status_fd)
                status_fd = None
            attempt['guard_exit_unconfirmed'] = bool(
                guard_process is not None and guard_process.poll() is None
            )
            attempt['guard_exit_code'] = (
                guard_process.returncode if guard_process is not None else None
            )

            # Finish asynchronous storage work after the guard is done. During the active
            # attempt, these writes stay off-thread so contention cannot stop guard polling,
            # cancellation checks, or output-growth monitoring.
            if claim_future is not None:
                while not claim_future.done():
                    time.sleep(0.05)
                try:
                    won = claim_future.result()
                except store_module.StorageContentionError:
                    won = _retry_storage(lambda: _claim_session(
                        db, job_id, job['backend'], claim_inflight_id,
                    ))
                if won:
                    session_id = claim_inflight_id
                    if pending_session_id == claim_inflight_id:
                        pending_session_id = None
                else:
                    session_claim_conflict = True
                    attempt['spawn_error'] = 'session_claim_conflict'
                claim_future = None
                claim_inflight_id = None
            # The last session observation may happen after a short attempt has already
            # exited. Claim that identity before asking the adapter about another attempt.
            final_observed_session = adapter.observe_session(artifacts)
            if (final_observed_session and final_observed_session != session_id
                    and not session_claim_conflict):
                pending_session_id = final_observed_session
            if pending_session_id and not session_claim_conflict:
                won = _retry_storage(lambda: _claim_session(
                    db, job_id, job['backend'], pending_session_id,
                ))
                if won:
                    session_id = pending_session_id
                    pending_session_id = None
                else:
                    session_claim_conflict = True
                    attempt['spawn_error'] = 'session_claim_conflict'
            attempt['ended_at'] = _timestamp()
            attempt['group_pgid'] = attempt.get('group_pgid') or group_pgid
            attempt['group_proc_key'] = attempt.get('group_proc_key') or group_proc_key
            attempt['finish_reason'] = _attempt_finish_reason(
                paths['stderr'], attempt['stderr_start'],
            )
            attempt['stalled'] = bool(attempt.get('stop_signalled'))
            attempt['truncated'] = attempt['finish_reason'] == 'length'
            leader_status = attempt.get('leader_status')
            if leader_status:
                exit_code = leader_status.get('exit_code')
                exit_signal = leader_status.get('exit_signal')
            else:
                exit_code = None
                exit_signal = None
            if attempt.get('group_proc_key'):
                try:
                    group_members = _group_live_members(attempt['group_proc_key'])
                    ps_error = None if group_members is not None else 'PID namespace membership is unknown'
                except Exception as exc:
                    group_members = None
                    ps_error = str(exc)
            else:
                group_members = []
                ps_error = None
            if attempt.get('released_at') and not attempt.get('group_proc_key'):
                ps_error = 'released attempt has no own recorded backend group identity'
            attempt['session_id'] = session_id or final_observed_session
            attempt['session_claim_conflict'] = session_claim_conflict
            attempt['group_members'] = group_members
            attempt['ps_error'] = ps_error
            _retry_storage(lambda: self_update_attempt(
                db, job_id, attempt_index, attempt,
            ))
            attempt_result = dict(attempt)
            attempt_history.append(attempt_result)

            if (ps_error or group_members is None or group_members
                    or attempt.get('guard_exit_unconfirmed')
                    or not attempt.get('guard_done')
                    or not attempt.get('cleanup_confirmed')):
                final_state = 'ownership_unresolved'
                final_reason = 'backend_group_cleanup_unconfirmed'
                break
            if attempt.get('pre_release_cancel'):
                if attempt.get('pre_release_cancel_ack') and attempt.get('not_started'):
                    final_state = 'cancelled'
                    final_reason = 'requested_before_backend_start'
                else:
                    final_state = 'crashed'
                    final_reason = 'cancel_unconfirmed_before_backend_start'
                break
            if attempt.get('spawn_error'):
                final_state = 'failed'
                final_reason = ('session_claim_conflict' if session_claim_conflict
                                else 'startup')
                break
            if (cancel_wanted and attempt.get('cancel_signalled')
                    and attempt.get('guard_done') and attempt.get('cleanup_confirmed')):
                final_state = 'cancelled'
                final_reason = 'requested'
                break
            if not leader_status:
                final_state = 'crashed'
                final_reason = 'guard_exit_without_leader_status'
                break

            latest = _retry_storage(lambda: db.get_job(job_id))
            if latest and latest.get('cancel_requested_at'):
                cancel_wanted = True
            if (cancel_wanted and attempt.get('cancel_signalled')
                    and attempt.get('guard_done') and attempt.get('cleanup_confirmed')):
                final_state = 'cancelled'
                final_reason = 'requested'
                break

            next_invocation = None
            if not cancel_wanted:
                next_invocation = adapter.next_attempt(
                    job, artifacts, attempt_index, attempt_result,
                )
            if next_invocation is not None:
                invocation = {
                    'argv': list(next_invocation.argv),
                    'cwd': next_invocation.cwd,
                    'env_overrides': dict(next_invocation.env_overrides or {}),
                    'env_remove': list(next_invocation.env_remove or ()),
                    'stdin_path': next_invocation.stdin_path,
                }
                attempt_index += 1
                continue

            outcome = adapter.classify(
                ExitInfo(exit_code, exit_signal, attempt.get('spawn_error')),
                artifacts, attempt_result, attempt_history[:-1],
            )
            final_state = outcome.state
            final_reason = outcome.reason
            if outcome.clear_session_id:
                clear_session_id = True
                session_id = None
            else:
                session_id = outcome.session_id or session_id
            break

        write_pool.shutdown(wait=True)
        write_pool_closed = True
        if final_state is None:
            final_state, final_reason = 'crashed', 'supervisor_incomplete'
        if (not clear_session_id
                and adapter.has_conflicting_session_identity(artifacts)):
            clear_session_id = True
            session_id = None
        if session_id is None and not clear_session_id:
            session_id = job.get('backend_session_id')
        sync_errors = _sync_job_artifacts(paths)
        if sync_errors and final_state not in ('ownership_unresolved', 'cancelled'):
            sync_reason = 'artifact_sync: {}'.format('; '.join(sync_errors))
            if final_state == 'done':
                final_state, final_reason = 'failed', sync_reason
            else:
                final_reason = '{}; {}'.format(final_reason, sync_reason) if final_reason else sync_reason

        if final_state == 'ownership_unresolved':
            latest = _retry_storage(lambda: db.get_job(job_id))
            observed_state = latest.get('state') if latest else current_state
            evidence = {
                'supervisor_pid': os.getpid(), 'guard_pid': guard_pid,
                'group_pgid': group_pgid or (attempt_history[-1].get('group_pgid') if attempt_history else None),
                'group_proc_key': group_proc_key or (attempt_history[-1].get('group_proc_key') if attempt_history else None),
                'group_members': attempt_history[-1].get('group_members', []) if attempt_history else [],
                'guard_status': status_items,
                'manual_recovery': 'verify every backend group member is stopped, then retry status; reconciliation never signals processes',
            }
            _mark_unresolved(
                db, job_id, observed_state, token, final_reason or 'execution_ownership_uncertain', evidence,
            )
        else:
            _fault('before_terminal_commit')
            _mark_terminal(
                db, job_id, final_state, final_reason, exit_code, exit_signal, session_id,
                cancelled=(final_state == 'cancelled'), expected_state=current_state,
                expected_token=token, clear_session_id=clear_session_id,
            )
        return 0 if final_state == 'done' else 1
    except BaseException as exc:
        # Permanent storage failures ask the guard to clean up through EOF; contention retries stay at their database boundary.
        if control_fd is not None:
            try:
                os.close(control_fd)
            except OSError:
                pass
            control_fd = None
        safe = guard_process is None
        if guard_process is not None:
            try:
                guard_process.wait(timeout=7)
                guard_free = _lock_state(paths['guard_lock']) == LOCK_FREE
                if group_proc_key:
                    live = _group_live_members(group_proc_key)
                    safe = live == [] and guard_free
                else:
                    attempts = db.attempts(job_id)
                    last_attempt = attempts[-1] if attempts else None
                    safe = bool(
                        guard_free and last_attempt
                        and last_attempt.get('not_started')
                        and last_attempt.get('cleanup_confirmed')
                        and not last_attempt.get('released_at')
                    )
            except Exception:
                safe = False
        try:
            if safe:
                if current_state == 'launching':
                    _retry_storage(lambda: _fail_startup(
                        db, job_id, 'supervisor_error: {}'.format(exc),
                        expected=('launching',), expected_token=token,
                        notify=True,
                    ))
                else:
                    _mark_terminal(
                        db, job_id, 'failed', 'supervisor_error: {}'.format(exc),
                        session_id=session_id, expected_state=current_state,
                        expected_token=token,
                    )
            else:
                latest = _retry_storage(lambda: db.get_job(job_id))
                _mark_unresolved(
                    db, job_id, latest.get('state') if latest else current_state, token,
                    'supervisor_error: {}'.format(exc),
                    {'supervisor_pid': os.getpid(), 'guard_pid': guard_pid,
                     'group_pgid': group_pgid, 'group_proc_key': group_proc_key,
                     'guard_session_pgid': guard_session_pgid},
                )
        except Exception:
            pass
        _append_supervisor_log(paths, 'supervisor error: {}'.format(exc))
        return 1
    finally:
        if control_fd is not None:
            try:
                os.close(control_fd)
            except OSError:
                pass
        if status_fd is not None:
            try:
                os.close(status_fd)
            except OSError:
                pass
        if not write_pool_closed:
            write_pool.shutdown(wait=True)
            write_pool_closed = True
        try:
            fcntl.flock(supervisor_fd, fcntl.LOCK_UN)
            os.close(supervisor_fd)
        except OSError:
            pass


def self_update_attempt(db, job_id, attempt_index, attempt):
    fields = (
        'ended_at', 'released_at', 'not_started', 'cleanup_confirmed',
        'exit_code', 'exit_signal', 'stalled', 'truncated',
        'cancel_signalled', 'stop_signalled', 'kill_sent', 'group_pgid',
        'group_proc_key', 'guard_pid', 'released_at', 'finish_reason', 'stop_reason',
        'spawn_error',
    )
    updates = {key: attempt[key] for key in fields if key in attempt}
    if attempt.get('leader_status'):
        updates['exit_code'] = attempt['leader_status'].get('exit_code')
        updates['exit_signal'] = attempt['leader_status'].get('exit_signal')
    updates['guard_pid'] = attempt.get('guard_pid')
    with db.transaction() as conn:
        db.update_attempt(conn, job_id, attempt_index, **updates)


def request_cancel(db, job_id):
    now = _timestamp()
    with db.transaction() as conn:
        row = db.get_job(job_id, conn)
        if not row:
            return None
        if row['state'] in TERMINAL:
            return row
        if row['state'] == 'ownership_unresolved':
            return row
        conn.execute(
            'UPDATE jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?),updated_at=? WHERE id=? AND state NOT IN (?,?,?,?)',
            (now, now, job_id, 'done', 'failed', 'cancelled', 'crashed'),
        )
    return db.get_job(job_id)


def _terminal_transition(db, job_id, state, reason, expected_state, expected_token,
                         release=True):
    with db.transaction() as conn:
        current = db.get_job(job_id, conn)
        if db.cas_state(conn, job_id, (expected_state,), state,
                        expected_token=expected_token,
                        failure_reason=reason, ended_at=_timestamp()):
            if release:
                db.release_claim(conn, job_id)
            _insert_reconciliation_notification(db, conn, current)


def _prune_lock_path(db, job_id):
    return os.path.join(db.jobs_dir, '.{}.prune.lock'.format(job_id))


def _finish_prune(db, job, token):
    with db.transaction() as conn:
        current = db.get_job(job['id'], conn)
        if (not current or current.get('prune_state') != 'pruning'
                or current.get('prune_token') != token):
            return False
    directory = job.get('artifact_dir')
    jobs_root = os.path.realpath(os.path.join(db.root, 'jobs'))
    real_directory = os.path.realpath(directory or '')
    if os.path.dirname(real_directory) != jobs_root:
        raise store_module.StorageError('recorded artifact path is outside the jobs directory')
    if os.path.exists(real_directory):
        try:
            shutil.rmtree(real_directory)
        except OSError as exc:
            raise store_module.StorageError('artifact deletion failed: {}'.format(exc))
    with db.transaction() as conn:
        return db.cas_prune(
            conn, job['id'], 'pruning', token, 'pruned', None,
        )


def _recover_interrupted_prune(db, job):
    try:
        lock_fd = _lock_file(_prune_lock_path(db, job['id']), blocking=False)
    except OSError:
        return db.get_job(job['id'])
    if lock_fd is None:
        return db.get_job(job['id'])
    try:
        new_token = secrets.token_hex(16)
        claimed = False
        with db.transaction() as conn:
            current = db.get_job(job['id'], conn)
            if current and current.get('prune_state') == 'pruning':
                claimed = db.cas_prune(
                    conn, job['id'], 'pruning', current.get('prune_token'),
                    'pruning', new_token,
                )
        if claimed:
            _finish_prune(db, db.get_job(job['id']), new_token)
        return db.get_job(job['id'])
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass


def reconcile_job(db, job_id):
    job = db.get_job(job_id)
    if not job:
        return None
    if job.get('prune_state') == 'pruning':
        job = _recover_interrupted_prune(db, job)
    if job['state'] in TERMINAL:
        return _settle_abandoned_notification(db, job)
    paths = artifact_paths(job)
    _fault('reconcile_after_observation')
    try:
        reconciliation_fd = _lock_file(paths['supervisor_lock'], blocking=False)
    except OSError:
        return db.get_job(job_id)
    if reconciliation_fd is None:
        return db.get_job(job_id)
    try:
        # Re-read under the lock. A launcher holds this lock before creating the row and
        # transfers it to the supervisor, so no new owner can register during this CAS.
        job = db.get_job(job_id)
        if not job or job['state'] in TERMINAL:
            return job
        observed_state = job['state']
        observed_token = job.get('supervisor_token')
        if observed_state == 'created' and not job.get('supervisor_pid'):
            prep_state = _lock_state(_preparation_lock(db.root))
            if prep_state == LOCK_HELD:
                return job
            if prep_state == LOCK_FREE:
                _terminal_transition(
                    db, job_id, 'failed',
                    'startup: launcher stopped before supervisor registration',
                    expected_state='created', expected_token=observed_token,
                )
                return _settle_abandoned_notification(db, db.get_job(job_id))
            with db.transaction() as conn:
                db.cas_state(
                    conn, job_id, (observed_state,), 'ownership_unresolved',
                    expected_token=observed_token,
                    failure_reason='preparation_lock_unreadable',
                    ownership_evidence='preparation lock could not be inspected',
                )
            return db.get_job(job_id)

        guard_lock = _lock_state(paths['guard_lock'])
        attempts = db.attempts(job_id)
        latest_attempt = attempts[-1] if attempts else None
        attempt_group_pgid = (
            latest_attempt.get('group_pgid') if latest_attempt else job.get('group_pgid')
        )
        attempt_group_key = (
            latest_attempt.get('group_proc_key') if latest_attempt
            else job.get('group_proc_key')
        )
        released_at = latest_attempt.get('released_at') if latest_attempt else None
        try:
            if attempt_group_key:
                group_members = _group_live_members(attempt_group_key)
            elif (latest_attempt and (latest_attempt.get('guard_pid') or job.get('guard_pid'))
                  and not latest_attempt.get('not_started')
                  and not latest_attempt.get('cleanup_confirmed')):
                group_members = None
            else:
                group_members = []
            ps_error = None if group_members is not None else 'PID namespace membership is unknown'
        except Exception as exc:
            group_members = None
            ps_error = str(exc)
        if released_at and not attempt_group_key:
            ps_error = 'released attempt has no own recorded backend group identity'
        if (latest_attempt and (latest_attempt.get('guard_pid') or job.get('guard_pid'))
                and not attempt_group_key and not latest_attempt.get('not_started')
                and not latest_attempt.get('cleanup_confirmed')):
            ps_error = 'attempt group identity was not committed and cleanup was not confirmed'
        if (guard_lock == LOCK_HELD or group_members is None or group_members
                or guard_lock == LOCK_ERROR or ps_error):
            evidence = {
                'supervisor_lock': 'held by reconciliation',
                'guard_lock': guard_lock,
                'supervisor_pid_diagnostic_only': job.get('supervisor_pid'),
                'guard_pid': latest_attempt.get('guard_pid') if latest_attempt else job.get('guard_pid'),
                'group_pgid': attempt_group_pgid,
                'group_proc_key': attempt_group_key,
                'attempt_index': latest_attempt.get('attempt_index') if latest_attempt else None,
                'attempt_released_at': released_at,
                'attempt_not_started': bool(latest_attempt and latest_attempt.get('not_started')),
                'group_members': group_members,
                'membership_error': ps_error,
                'manual_recovery': 'verify every recorded backend group member is stopped, then retry status; reconciliation never signals processes',
            }
            with db.transaction() as conn:
                db.cas_state(
                    conn, job_id, (observed_state,), 'ownership_unresolved',
                    expected_token=observed_token,
                    failure_reason='supervisor_stopped_with_uncertain_execution_ownership',
                    ownership_evidence=json.dumps(evidence, sort_keys=True),
                )
            return db.get_job(job_id)
        _terminal_transition(
            db, job_id, 'crashed', 'supervisor stopped; no owned process remains',
            expected_state=observed_state, expected_token=observed_token,
        )
        return _settle_abandoned_notification(db, db.get_job(job_id))
    finally:
        try:
            fcntl.flock(reconciliation_fd, fcntl.LOCK_UN)
            os.close(reconciliation_fd)
        except OSError:
            pass


def reconcile_all(db):
    for job in db.list_jobs():
        reconcile_job(db, job['id'])
    return db.list_jobs()


def wait_for_job(db, job_id, timeout=None, interval=0.25):
    started = time.monotonic()
    interval = max(0.05, interval)
    while True:
        _reap_detached_supervisors()
        job = reconcile_job(db, job_id)
        if not job:
            return None
        if job['state'] in TERMINAL or job['state'] == 'ownership_unresolved':
            return job
        if timeout is not None and time.monotonic() - started >= timeout:
            return 'timeout'
        time.sleep(min(interval, 0.5))


def prune_job(db, job_id, cutoff):
    job = reconcile_job(db, job_id)
    if not job or job['state'] not in TERMINAL or job.get('state') == 'ownership_unresolved':
        return False
    if job.get('prune_state') == 'pruned':
        return False
    ended = job.get('ended_at')
    if not ended:
        return False
    try:
        ended_timestamp = datetime.datetime.fromisoformat(ended.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return False
    try:
        lock_fd = _lock_file(_prune_lock_path(db, job_id), blocking=False)
    except OSError:
        return False
    if lock_fd is None:
        return False
    try:
        token = secrets.token_hex(16)
        claimed = False
        with db.transaction() as conn:
            claim = conn.execute(
                'SELECT 1 FROM session_claims WHERE job_id=?', (job_id,),
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM notifications WHERE job_id=? AND state='pending'", (job_id,),
            ).fetchone()
            current = db.get_job(job_id, conn)
            if (claim or pending or not current or current['state'] not in TERMINAL
                    or current.get('prune_state') != 'available'
                    or ended_timestamp >= cutoff):
                return False
            claimed = db.cas_prune(
                conn, job_id, 'available', current.get('prune_token'),
                'pruning', token,
            )
        if not claimed:
            return False
        _fault('between_prune_mark_and_delete')
        return _finish_prune(db, db.get_job(job_id), token)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass

