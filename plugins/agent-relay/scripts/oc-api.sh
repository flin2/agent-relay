#!/usr/bin/env bash
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
# oc-api runs a direct provider API through OpenCode for one-shot development or review work.
# Bare model names use deepseek/; any configured provider can be selected with --model provider/model.
set -euo pipefail

OC_BACKEND_KIND=api
OC_DEFAULT_PROVIDER=deepseek
OC_SCRIPT_NAME=oc-api
OC_ROUTE_DESCRIPTION="direct provider API route"
OC_MODEL_SHORTCUTS="Built-in shortcuts: pro and flash/fast use the newest DeepSeek family match; v4-pro, v4-flash, reasoner, and chat are fixed aliases."

if command -v python3 >/dev/null 2>&1; then
    SELF_DIR="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${BASH_SOURCE[0]}")"
else
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

. "$SELF_DIR/oc-run.sh"
oc_wrapper_main "$@"
