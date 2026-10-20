# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend adapter registry and shared invocation/result values."""

from dataclasses import dataclass


@dataclass
class Invocation:
    argv: list
    cwd: str
    env_overrides: dict
    stdin_path: str = None


@dataclass
class ExitInfo:
    exit_code: int = None
    exit_signal: int = None
    spawn_error: str = None


@dataclass
class Outcome:
    state: str
    reason: str = None
    session_id: str = None
    final_response_written: bool = False
    clear_session_id: bool = False

    def __post_init__(self):
        if self.state not in ('done', 'failed'):
            raise ValueError('adapter outcome state must be done or failed')


class Adapter:
    name = None

    def validate(self, options):
        return dict(options)

    def prepare(self, job, artifacts, resume_session_id=None):
        raise NotImplementedError

    def observe_session(self, artifacts):
        return None

    def has_conflicting_session_identity(self, artifacts):
        return False

    def idle_timeout(self, job):
        return None

    def next_attempt(self, job, artifacts, attempt_index, attempt_result):
        return None

    def classify(self, exit_info, artifacts, attempt_result=None, attempt_history=None):
        raise NotImplementedError


_REGISTRY = {}


def register_adapter(adapter):
    """Register an adapter class; production registers only built-in adapters."""
    instance = adapter() if isinstance(adapter, type) else adapter
    if not instance.name:
        raise ValueError('adapter must define a name')
    _REGISTRY[instance.name] = instance
    return instance


def get_adapter(name):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError('backend adapter is not registered: {}'.format(name))


def registered_adapters():
    return dict(_REGISTRY)


from .opencode import OpenCodeAdapter
from .codex import CodexAdapter

register_adapter(OpenCodeAdapter)
register_adapter(CodexAdapter)

