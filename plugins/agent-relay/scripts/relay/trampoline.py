# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hold a backend's process-group leader until relay ownership is durable."""

import os
import signal
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4 or argv[2] != '--':
        return 125
    try:
        gate_fd = int(argv[0])
        exec_error_fd = int(argv[1])
    except ValueError:
        return 125
    try:
        release = os.read(gate_fd, 1)
    finally:
        os.close(gate_fd)
    if release != b'R':
        os.close(exec_error_fd)
        return 125
    command = argv[3:]
    if not command:
        os.close(exec_error_fd)
        return 125
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_DFL)
    os.set_inheritable(exec_error_fd, False)
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        try:
            os.write(exec_error_fd, str(exc).encode('utf-8', 'replace'))
        finally:
            os.close(exec_error_fd)
        return 127


if __name__ == '__main__':
    sys.exit(main())
