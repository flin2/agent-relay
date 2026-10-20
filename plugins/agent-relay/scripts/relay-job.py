#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin command-line entry point for the agent-relay lifecycle package."""

import sys

from relay import cli
from relay.store import StorageError


# Compatibility exports for the small host-identity test surface. These helpers all
# query SQLite; none reads or writes a file-based status record.
_host = cli._host
_conversation_id = cli._conversation_id
_conversation_key = cli._conversation_key
_resolve_state_root = cli._resolve_state_root
_ensure_state_root = cli._ensure_state_root
_job_dir = cli._job_dir
_make_job_id = cli._make_job_id
_read_meta = cli._read_meta
_write_meta = cli._write_meta


def main(argv=None):
    try:
        return cli.main(argv)
    except StorageError as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        return 7
    except (OSError, ValueError) as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
