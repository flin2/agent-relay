# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite lifecycle storage for agent-relay jobs."""

import contextlib
import datetime
import json
import os
import sqlite3


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_TOKEN_UNSET = object()


class StorageError(RuntimeError):
    """A storage operation failed or exceeded its bounded busy budget."""


class StorageContentionError(StorageError):
    """A storage operation failed because SQLite was busy or locked."""


class SchemaVersionError(StorageError):
    """The state database uses an unsupported schema version."""


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def resolve_state_root(environ=None):
    environ = os.environ if environ is None else environ
    explicit = environ.get('RELAY_JOB_STATE_DIR')
    if explicit:
        root = explicit
    elif environ.get('XDG_STATE_HOME'):
        root = os.path.join(environ['XDG_STATE_HOME'], 'agent-relay')
    else:
        home = environ.get('HOME') or os.path.expanduser('~')
        root = os.path.join(home, '.local', 'state', 'agent-relay')
    if not os.path.isabs(root):
        raise ValueError('state root must be an absolute path: {}'.format(root))
    return os.path.realpath(root)


def ensure_state_root(root):
    if not os.path.isabs(root):
        raise ValueError('state root must be an absolute path: {}'.format(root))
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        jobs = os.path.join(root, 'jobs')
        os.makedirs(jobs, mode=0o700, exist_ok=True)
        os.chmod(jobs, 0o700)
    except OSError as exc:
        raise StorageError('state root unavailable: {}'.format(exc))
    return os.path.realpath(root)


def _schema_sql():
    return (
        '''CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            failure_reason TEXT,
            ownership_evidence TEXT,
            host TEXT,
            host_conversation_id TEXT,
            host_conversation_key TEXT,
            backend TEXT NOT NULL,
            route TEXT,
            gateway TEXT,
            workspace TEXT NOT NULL,
            cwd TEXT NOT NULL,
            label TEXT,
            model TEXT,
            effort TEXT,
            write INTEGER NOT NULL DEFAULT 0,
            parent_job_id TEXT REFERENCES jobs(id),
            backend_session_id TEXT,
            cancel_requested_at TEXT,
            cancel_confirmed_at TEXT,
            supervisor_token TEXT,
            supervisor_pid INTEGER,
            supervisor_started_at TEXT,
            guard_pid INTEGER,
            group_pgid INTEGER,
            group_proc_key INTEGER,
            exit_code INTEGER,
            exit_signal INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            artifact_dir TEXT NOT NULL,
            task_path TEXT NOT NULL,
            stdout_path TEXT NOT NULL,
            stderr_path TEXT NOT NULL,
            events_path TEXT NOT NULL,
            response_path TEXT NOT NULL,
            task_ready INTEGER NOT NULL DEFAULT 0,
            prune_state TEXT NOT NULL DEFAULT 'available',
            prune_token TEXT,
            options TEXT NOT NULL DEFAULT '{}'
        )''',
        '''CREATE INDEX jobs_created_idx ON jobs(created_at DESC)''',
        '''CREATE INDEX jobs_state_idx ON jobs(state)''',
        '''CREATE TABLE session_claims (
            backend TEXT NOT NULL,
            backend_session_id TEXT NOT NULL,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            created_at TEXT NOT NULL,
            PRIMARY KEY (backend, backend_session_id),
            UNIQUE (job_id)
        )''',
        '''CREATE TABLE notifications (
            job_id TEXT NOT NULL REFERENCES jobs(id),
            transport TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','sent','failed','uncertain')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (job_id, transport)
        )''',
        '''CREATE TABLE attempts (
            job_id TEXT NOT NULL REFERENCES jobs(id),
            attempt_index INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            released_at TEXT,
            not_started INTEGER NOT NULL DEFAULT 0,
            cleanup_confirmed INTEGER NOT NULL DEFAULT 0,
            ended_at TEXT,
            exit_code INTEGER,
            exit_signal INTEGER,
            stalled INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0,
            cancel_signalled INTEGER NOT NULL DEFAULT 0,
            stop_signalled INTEGER NOT NULL DEFAULT 0,
            kill_sent INTEGER NOT NULL DEFAULT 0,
            group_pgid INTEGER,
            group_proc_key INTEGER,
            guard_pid INTEGER,
            finish_reason TEXT,
            stop_reason TEXT,
            spawn_error TEXT,
            stdout_start INTEGER NOT NULL DEFAULT 0,
            stderr_start INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, attempt_index)
        )''',
    )


class Store:
    """Open short-lived, process-local SQLite connections for one state root."""

    def __init__(self, root, initialize=True):
        self.root = ensure_state_root(root)
        self.path = os.path.join(self.root, 'jobs.sqlite3')
        self.jobs_dir = os.path.join(self.root, 'jobs')
        if initialize:
            self.initialize()

    def _connect(self, enable_wal=False):
        conn = None
        try:
            conn = sqlite3.connect(
                self.path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout={}'.format(BUSY_TIMEOUT_MS))
            conn.execute('PRAGMA synchronous=FULL')
            pragma = 'PRAGMA journal_mode=WAL' if enable_wal else 'PRAGMA journal_mode'
            mode = conn.execute(pragma).fetchone()[0]
            if str(mode).lower() != 'wal':
                conn.close()
                raise StorageError('SQLite WAL mode unavailable for {}'.format(self.path))
            os.chmod(self.path, 0o600)
            return conn
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if isinstance(exc, StorageError):
                raise
            raise self._storage_error(exc)

    def connect(self):
        return self._connect(enable_wal=False)

    def initialize(self):
        try:
            conn = self._connect(enable_wal=not os.path.exists(self.path))
        except StorageError as exc:
            if 'WAL mode unavailable' not in str(exc):
                raise
            conn = self._connect(enable_wal=True)
        try:
            with self.transaction(conn):
                version = int(conn.execute('PRAGMA user_version').fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise SchemaVersionError(
                        'state database schema version {} is newer than supported version {}; upgrade agent-relay'.format(
                            version, SCHEMA_VERSION,
                        )
                    )
                if version == SCHEMA_VERSION:
                    return
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if version != 0 or tables:
                    raise SchemaVersionError(
                        'state database schema version {} is unsupported; preserve the database and upgrade agent-relay'.format(
                            version,
                        )
                    )
                for statement in _schema_sql():
                    conn.execute(statement)
                conn.execute('PRAGMA user_version={}'.format(SCHEMA_VERSION))
            os.chmod(self.path, 0o600)
        except (sqlite3.Error, OSError) as exc:
            if isinstance(exc, StorageError):
                raise
            raise self._storage_error(exc)
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self, conn=None):
        owned = conn is None
        conn = self.connect() if conn is None else conn
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except (sqlite3.Error, OSError) as exc:
            try:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            if isinstance(exc, StorageError):
                raise
            raise self._storage_error(exc)
        except Exception:
            try:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            if owned:
                conn.close()

    @staticmethod
    def _storage_error(exc):
        message = str(exc)
        if isinstance(exc, sqlite3.OperationalError) and (
            'locked' in message.lower() or 'busy' in message.lower()
        ):
            return StorageContentionError(
                'state database is busy after {} ms: {}'.format(BUSY_TIMEOUT_MS, message),
            )
        return StorageError('state database error: {}'.format(message))

    def get_job(self, job_id, conn=None):
        owned = conn is None
        conn = self.connect() if conn is None else conn
        try:
            row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return self.row_to_dict(row)
        except sqlite3.Error as exc:
            raise self._storage_error(exc)
        finally:
            if owned:
                conn.close()

    def list_jobs(self, conn=None):
        owned = conn is None
        conn = self.connect() if conn is None else conn
        try:
            rows = conn.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
            return [self.row_to_dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise self._storage_error(exc)
        finally:
            if owned:
                conn.close()

    @staticmethod
    def row_to_dict(row):
        if row is None:
            return None
        job = dict(row)
        job['write'] = bool(job.get('write'))
        try:
            job['options'] = json.loads(job.get('options') or '{}')
        except (TypeError, ValueError):
            job['options'] = {}
        job['status'] = job.get('state')
        job['dir'] = job.get('cwd')
        job['started'] = job.get('created_at')
        job['ended'] = job.get('ended_at')
        job['pid'] = job.get('supervisor_pid')
        job['pgid'] = job.get('group_pgid')
        job['artifacts_pruned'] = job.get('prune_state') == 'pruned'
        job['job_dir'] = job.get('artifact_dir')
        job['out_log'] = job.get('stdout_path')
        return job

    def insert_job(self, conn, job):
        fields = (
            'id', 'state', 'host', 'host_conversation_id', 'host_conversation_key',
            'backend', 'route', 'gateway', 'workspace', 'cwd', 'label', 'model',
            'effort', 'write', 'parent_job_id', 'backend_session_id', 'created_at',
            'updated_at', 'ended_at', 'failure_reason', 'ownership_evidence',
            'cancel_requested_at', 'cancel_confirmed_at', 'supervisor_token',
            'supervisor_pid', 'supervisor_started_at', 'guard_pid', 'group_pgid',
            'group_proc_key', 'exit_code', 'exit_signal', 'artifact_dir', 'task_path', 'stdout_path',
            'stderr_path', 'events_path', 'response_path', 'task_ready', 'prune_state',
            'prune_token',
            'options',
        )
        values = []
        for field in fields:
            value = job.get(field)
            if field == 'options' and not isinstance(value, str):
                value = json.dumps(value or {}, sort_keys=True)
            elif field == 'write':
                value = int(bool(value))
            elif field == 'task_ready' and value is None:
                value = 0
            elif field == 'prune_state' and value is None:
                value = 'available'
            values.append(value)
        placeholders = ','.join('?' for _ in fields)
        try:
            conn.execute(
                'INSERT INTO jobs ({}) VALUES ({})'.format(','.join(fields), placeholders),
                values,
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def cas_state(self, conn, job_id, old_states, new_state, expected_token=_TOKEN_UNSET,
                  **updates):
        old_states = tuple(old_states)
        if not old_states:
            return False
        if 'options' in updates and not isinstance(updates['options'], str):
            updates['options'] = json.dumps(updates['options'] or {}, sort_keys=True)
        if 'write' in updates:
            updates['write'] = int(bool(updates['write']))
        now = now_utc()
        updates.setdefault('updated_at', now)
        assignments = ['state=?'] + ['{}=?'.format(key) for key in updates]
        parameters = [new_state] + list(updates.values()) + [job_id] + list(old_states)
        placeholders = ','.join('?' for _ in old_states)
        token_clause = ''
        if expected_token is not _TOKEN_UNSET:
            token_clause = ' AND supervisor_token IS ?'
            parameters.append(expected_token)
        try:
            cursor = conn.execute(
                'UPDATE jobs SET {} WHERE id=? AND state IN ({}){}'.format(
                    ','.join(assignments), placeholders, token_clause,
                ), parameters,
            )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def update_job(self, conn, job_id, **updates):
        if not updates:
            return
        updates.setdefault('updated_at', now_utc())
        if 'options' in updates and not isinstance(updates['options'], str):
            updates['options'] = json.dumps(updates['options'] or {}, sort_keys=True)
        if 'write' in updates:
            updates['write'] = int(bool(updates['write']))
        assignments = ','.join('{}=?'.format(key) for key in updates)
        try:
            conn.execute(
                'UPDATE jobs SET {} WHERE id=?'.format(assignments),
                list(updates.values()) + [job_id],
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def cas_prune(self, conn, job_id, expected_state, expected_token, new_state, new_token):
        try:
            cursor = conn.execute(
                '''UPDATE jobs SET prune_state=?,prune_token=?,updated_at=?
                   WHERE id=? AND prune_state=? AND prune_token IS ?''',
                (new_state, new_token, now_utc(), job_id, expected_state, expected_token),
            )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def claim_session(self, conn, job_id, backend, session_id):
        if not session_id:
            return True
        try:
            row = conn.execute(
                'SELECT job_id FROM session_claims WHERE backend=? AND backend_session_id=?',
                (backend, session_id),
            ).fetchone()
            if row:
                if row['job_id'] == job_id:
                    return True
                return False
            conn.execute(
                'INSERT INTO session_claims(backend,backend_session_id,job_id,created_at) VALUES(?,?,?,?)',
                (backend, session_id, job_id, now_utc()),
            )
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def release_claim(self, conn, job_id):
        try:
            conn.execute('DELETE FROM session_claims WHERE job_id=?', (job_id,))
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def insert_notification(self, conn, job_id, transport, state, last_error=None):
        if state not in ('pending', 'sent', 'failed', 'uncertain'):
            raise ValueError('invalid notification state: {}'.format(state))
        now = now_utc()
        try:
            conn.execute(
                '''INSERT OR IGNORE INTO notifications
                   (job_id,transport,state,attempts,last_error,created_at,updated_at)
                   VALUES(?,?,?,0,?,?,?)''',
                (job_id, transport, state, last_error, now, now),
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def update_notification(self, conn, job_id, transport, state, last_error=None,
                            attempts=None):
        if state not in ('pending', 'sent', 'failed', 'uncertain'):
            raise ValueError('invalid notification state: {}'.format(state))
        try:
            row = conn.execute(
                'SELECT attempts FROM notifications WHERE job_id=? AND transport=?',
                (job_id, transport),
            ).fetchone()
            if row is None:
                return False
            count = attempts if attempts is not None else row['attempts'] + 1
            conn.execute(
                '''UPDATE notifications SET state=?,attempts=?,last_error=?,updated_at=?
                   WHERE job_id=? AND transport=?''',
                (state, count, last_error, now_utc(), job_id, transport),
            )
            return True
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def upsert_notification(self, job_id, transport, state, last_error=None, attempts=None):
        if state not in ('pending', 'sent', 'failed', 'uncertain'):
            raise ValueError('invalid notification state: {}'.format(state))
        now = now_utc()
        with self.transaction() as conn:
            row = conn.execute(
                'SELECT attempts,created_at FROM notifications WHERE job_id=? AND transport=?',
                (job_id, transport),
            ).fetchone()
            count = attempts if attempts is not None else ((row['attempts'] + 1) if row else 0)
            created = row['created_at'] if row else now
            conn.execute(
                '''INSERT INTO notifications(job_id,transport,state,attempts,last_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(job_id,transport) DO UPDATE SET
                   state=excluded.state, attempts=excluded.attempts,
                   last_error=excluded.last_error, updated_at=excluded.updated_at''',
                (job_id, transport, state, count, last_error, created, now),
            )

    def notifications(self, job_id, conn=None):
        owned = conn is None
        conn = self.connect() if conn is None else conn
        try:
            rows = conn.execute(
                'SELECT * FROM notifications WHERE job_id=? ORDER BY transport', (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise self._storage_error(exc)
        finally:
            if owned:
                conn.close()

    def insert_attempt(self, conn, attempt):
        fields = (
            'job_id', 'attempt_index', 'started_at', 'released_at', 'not_started',
            'cleanup_confirmed', 'ended_at', 'exit_code',
            'exit_signal', 'stalled', 'truncated', 'cancel_signalled', 'stop_signalled',
            'kill_sent', 'group_pgid', 'guard_pid', 'finish_reason', 'stop_reason',
            'group_proc_key', 'spawn_error', 'stdout_start', 'stderr_start',
        )
        values = [attempt.get(field) for field in fields]
        for field in (
            'not_started', 'cleanup_confirmed', 'stalled', 'truncated',
            'cancel_signalled', 'stop_signalled', 'kill_sent',
        ):
            index = fields.index(field)
            values[index] = int(bool(values[index]))
        try:
            conn.execute(
                'INSERT INTO attempts ({}) VALUES ({})'.format(
                    ','.join(fields), ','.join('?' for _ in fields),
                ), values,
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def update_attempt(self, conn, job_id, attempt_index, **updates):
        if not updates:
            return
        for field in (
            'not_started', 'cleanup_confirmed', 'stalled', 'truncated',
            'cancel_signalled', 'stop_signalled', 'kill_sent',
        ):
            if field in updates:
                updates[field] = int(bool(updates[field]))
        assignments = ','.join('{}=?'.format(key) for key in updates)
        try:
            conn.execute(
                'UPDATE attempts SET {} WHERE job_id=? AND attempt_index=?'.format(assignments),
                list(updates.values()) + [job_id, attempt_index],
            )
        except sqlite3.Error as exc:
            raise self._storage_error(exc)

    def attempts(self, job_id, conn=None):
        owned = conn is None
        conn = self.connect() if conn is None else conn
        try:
            rows = conn.execute(
                'SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_index', (job_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise self._storage_error(exc)
        finally:
            if owned:
                conn.close()

