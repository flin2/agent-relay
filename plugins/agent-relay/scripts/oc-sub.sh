#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# oc-sub runs an OpenCode subscription model through OpenCode for one-shot development or review work.
# OpenCode Zen is the default gateway; use --go for OpenCode Go.
set -euo pipefail

OC_BACKEND_KIND=sub
OC_DEFAULT_PROVIDER=opencode
OC_SCRIPT_NAME=oc-sub
OC_ROUTE_DESCRIPTION="subscription model"
OC_MODEL_SHORTCUTS="Built-in shortcuts: glm, kimi, minimax, ds-pro, and ds-flash resolve the newest family match on the active gateway."

if command -v python3 >/dev/null 2>&1; then
    SELF_DIR="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${BASH_SOURCE[0]}")"
else
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

. "$SELF_DIR/oc-run.sh"
oc_wrapper_main "$@"
