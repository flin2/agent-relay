#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""
oc-model — resolve a --model value to a full `provider/model` id for the agent-relay wrappers.

Resolution order (first match wins):
  1. a full id (it contains a `/`) is used as-is;
  2. a shortcut defined in the user's config file, for this backend;
  3. a built-in model-family shortcut, resolved to the NEWEST version of that family that
     `opencode models <provider>` lists right now (e.g. `glm` -> the highest `glm-X.Y`);
  4. a built-in fixed shortcut (an explicitly versioned or legacy name);
  5. anything else is a bare model name, prefixed with the active provider.

Built-in family shortcuts never name a specific version, so they keep working as providers
retire old models. When a family has no match in the catalog the resolver fails loudly
rather than guessing.

Config file: $RELAY_MODEL_CONFIG, with $OC_HELPER_CONFIG retained as a compatibility override, else $XDG_CONFIG_HOME/agent-relay/config.json (default ~/.config/agent-relay/config.json). Optional. Shape:

    {
      "aliases": {
        "sub": { "glm": "glm-5.3", "fast": "opencode-go/deepseek-v4-flash" },
        "api": { "pro": "deepseek/deepseek-v4-pro" }
      }
    }

A value containing `/` is a full id; a bare value is prefixed with the active provider
(`opencode/` or `opencode-go/` for `sub`, so a config shortcut follows --zen / --go).
Stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys

VERSION = r'(\d+(?:\.\d+)*)'

# Family shortcuts: resolved to the newest catalog entry matching the pattern. Patterns are
# anchored so variants such as `glm-5.3-flash` or `deepseek-v4-flash-vision-exp` never match.
FAMILIES = {
    'sub': {
        'glm':      r'glm-' + VERSION,
        'kimi':     r'kimi-k' + VERSION,
        'minimax':  r'minimax-m' + VERSION,
        'ds-pro':   r'deepseek-v' + VERSION + r'-pro',
        'ds-flash': r'deepseek-v' + VERSION + r'-flash',
    },
    'api': {
        'pro':   r'deepseek-v' + VERSION + r'-pro',
        'flash': r'deepseek-v' + VERSION + r'-flash',
        'fast':  r'deepseek-v' + VERSION + r'-flash',
    },
}

# Fixed shortcuts: names that deliberately pin a version (or a legacy model).
FIXED = {
    'sub': {},
    'api': {
        'v4-pro':   'deepseek-v4-pro',
        'v4-flash': 'deepseek-v4-flash',
        'reasoner': 'deepseek-reasoner',
        'chat':     'deepseek-chat',
    },
}


def _fail(msg):
    sys.stderr.write(f'error: {msg}\n')
    sys.exit(2)


def _config_path():
    explicit = os.environ.get('RELAY_MODEL_CONFIG') or os.environ.get('OC_HELPER_CONFIG')
    if explicit:
        return explicit
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'agent-relay', 'config.json')


def _config_aliases(backend):
    path = _config_path()
    if not os.path.exists(path):
        return {}
    # A config the user wrote must be honoured or rejected loudly, never silently skipped.
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _fail(f'cannot read model config {path}: {e}')
    sections = cfg.get('aliases', {}) if isinstance(cfg, dict) else None
    aliases = sections.get(backend, {}) if isinstance(sections, dict) else None
    if not isinstance(aliases, dict) or not all(isinstance(v, str) and v for v in aliases.values()):
        _fail(f'{path}: "aliases.{backend}" must map shortcut names to model ids')
    return aliases


def _catalog(provider):
    try:
        out = subprocess.run(['opencode', 'models', provider], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        _fail(f'cannot list models with `opencode models {provider}`: {e}')
    prefix = provider + '/'
    return [line.strip()[len(prefix):] for line in out.stdout.splitlines() if line.strip().startswith(prefix)]


def _newest(provider, pattern, alias):
    rx = re.compile('^' + pattern + '$')
    found = []
    for name in _catalog(provider):
        m = rx.match(name)
        if m:
            found.append((tuple(int(p) for p in m.group(1).split('.')), name))
    if not found:
        _fail(f'no model for shortcut "{alias}" in `opencode models {provider}` '
              f'(is the provider connected?); pass a full provider/model id, '
              f'or map "{alias}" in {_config_path()}')
    return max(found)[1]


def resolve(backend, provider, value):
    if '/' in value:
        return value
    configured = _config_aliases(backend).get(value)
    if configured:
        return configured if '/' in configured else f'{provider}/{configured}'
    if value in FAMILIES[backend]:
        return f'{provider}/{_newest(provider, FAMILIES[backend][value], value)}'
    if value in FIXED[backend]:
        return f'{provider}/{FIXED[backend][value]}'
    return f'{provider}/{value}'


def main():
    p = argparse.ArgumentParser(prog='oc-model.py', description='Resolve a --model value to a provider/model id.')
    sub = p.add_subparsers(dest='cmd', required=True)
    pr = sub.add_parser('resolve', help='Print the full provider/model id for a --model value.')
    pr.add_argument('--backend', choices=sorted(FAMILIES), required=True)
    pr.add_argument('--provider', required=True, help='Active provider prefix, e.g. opencode, opencode-go, deepseek.')
    pr.add_argument('value')
    args = p.parse_args()
    sys.stdout.write(resolve(args.backend, args.provider, args.value))
    return 0


if __name__ == '__main__':
    sys.exit(main())
