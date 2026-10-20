# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenCode CLI wrapper adapter."""

import os
import re
import tempfile

from . import Adapter, ExitInfo, Invocation, Outcome


SESSION_RE = re.compile(r'^\[relay-session\]\s+id=([^\s]+)')
HOST_CONTEXT_KEYS = (
    'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA',
    'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'RELAY_JOB_HOST',
    'RELAY_JOB_STATE_DIR', 'RELAY_JOB_CONVERSATION_ID',
)


class OpenCodeAdapter(Adapter):
    name = 'opencode'
    DEFAULT_IDLE_TIMEOUT = 600.0
    DEFAULT_MAX_RESUMES = 3
    RETRY_INSTRUCTION = (
        'Continue the previous task from where you stopped. '
        'If it is already complete, briefly confirm that and stop.'
    )

    def validate(self, options):
        route = options.get('route')
        if route not in ('sub', 'api'):
            raise ValueError('--route is required with --backend opencode')
        if route == 'api' and options.get('gateway'):
            raise ValueError('--zen and --go are only valid with --route sub')
        if options.get('gateway') not in (None, 'zen', 'go'):
            raise ValueError('unknown OpenCode gateway: {}'.format(options.get('gateway')))
        return dict(options)

    def _invocation(self, job, artifacts, resume_session_id=None):
        self.validate(job.get('options', {}))
        route = job.get('route')
        if route == 'sub':
            wrapper = os.environ.get('OC_RELAY_SUB_SCRIPT') or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'oc-sub.sh',
            )
        else:
            wrapper = os.environ.get('OC_RELAY_API_SCRIPT') or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                'oc-api.sh',
            )
        args = ['--dir', job['cwd'], '--quiet']
        if job.get('write'):
            args.append('--write')
        if job.get('model'):
            args.extend(('--model', job['model']))
        if job.get('effort'):
            args.extend(('--effort', job['effort']))
        gateway = job.get('gateway')
        if route == 'sub' and gateway:
            args.append('--' + gateway)
        if resume_session_id:
            args.extend(('--session', resume_session_id))
        elif job.get('label') or job.get('options', {}).get('title'):
            title = job.get('options', {}).get('title') or job.get('label')
            title = '{} [relay:{}]'.format(title[:64], job['id'])
            args.extend(('--title', title))
        args.extend(('--task-file', artifacts['task']))
        overrides = {
            'OC_RELAY_SESSION_FILE': artifacts['session_id'],
            'OC_RELAY_SINGLE_ATTEMPT': '1',
        }
        return Invocation([wrapper] + args, job['cwd'], overrides, None)

    def prepare(self, job, artifacts, resume_session_id=None):
        return self._invocation(job, artifacts, resume_session_id)

    def observe_session(self, artifacts):
        session_path = artifacts.get('session_id')
        if session_path:
            try:
                with open(session_path, 'r', encoding='utf-8') as stream:
                    value = stream.read().strip()
                if value:
                    return value
            except OSError:
                pass
        path = artifacts.get('stdout')
        if path:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        if not line.endswith('\n'):
                            continue
                        match = SESSION_RE.match(line.strip())
                        if match:
                            return match.group(1)
            except OSError:
                pass
        return None

    def idle_timeout(self, job):
        value = os.environ.get('OC_IDLE_TIMEOUT')
        if value is None:
            return self.DEFAULT_IDLE_TIMEOUT
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return self.DEFAULT_IDLE_TIMEOUT
        return timeout if timeout > 0 else self.DEFAULT_IDLE_TIMEOUT

    def next_attempt(self, job, artifacts, attempt_index, attempt_result):
        retryable = attempt_result.get('stalled') or attempt_result.get('truncated')
        if not retryable or int(attempt_index) > self._max_resumes():
            return None
        session_id = attempt_result.get('session_id') or self.observe_session(artifacts)
        if not session_id:
            return None
        artifact_dir = artifacts.get('dir') or os.path.dirname(artifacts['task'])
        retry_path = os.path.join(
            artifact_dir, 'retry-task-{}.txt'.format(int(attempt_index) + 1),
        )
        self._write_retry_instruction(retry_path)
        retry_artifacts = dict(artifacts, task=retry_path)
        return self._invocation(job, retry_artifacts, session_id)

    def _write_retry_instruction(self, path):
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix='.relay-retry-', dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(self.RETRY_INSTRUCTION.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            flags = getattr(os, 'O_DIRECTORY', 0) | os.O_RDONLY
            directory_fd = os.open(directory, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _max_resumes(self):
        value = os.environ.get('OC_MAX_RESUMES')
        if value is None:
            return self.DEFAULT_MAX_RESUMES
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return self.DEFAULT_MAX_RESUMES

    def classify(self, exit_info, artifacts, attempt_result=None, attempt_history=None):
        session_id = self.observe_session(artifacts)
        attempt_result = attempt_result or {}
        if exit_info.spawn_error:
            return Outcome('failed', 'startup', session_id, False)
        if (attempt_result.get('stalled') or attempt_result.get('truncated')) and not session_id:
            return Outcome('failed', 'session_identity_unknown', None, False)
        if attempt_result.get('truncated'):
            return Outcome('failed', 'truncated', session_id, False)
        if attempt_result.get('stalled'):
            return Outcome('failed', 'stalled', session_id, False)
        if exit_info.exit_code == 0 and exit_info.exit_signal is None:
            if not session_id:
                return Outcome('failed', 'session_identity_unknown', None, False)
            if attempt_result.get('finish_reason') != 'stop':
                return Outcome('done', 'completion_unverified', session_id, False)
            return Outcome('done', None, session_id, False)
        if exit_info.exit_code == 75:
            reason = 'opencode_retry_exhausted'
        elif exit_info.exit_signal:
            reason = 'signal_{}'.format(exit_info.exit_signal)
        elif exit_info.exit_code is None:
            reason = 'missing_exit_status'
        else:
            reason = 'exit_{}'.format(exit_info.exit_code)
        return Outcome('failed', reason, session_id, False)

