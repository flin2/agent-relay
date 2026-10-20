#!/usr/bin/env python3
# Copyright (C) 2026 Feng Lin
# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate package metadata and the public/private repository boundary."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / 'plugins/agent-relay'
PRIVATE_PATHS = ('_private', 'CLAUDE.md')


def main():
    errors = []
    versions = []
    for host, catalog in (
        ('claude', '.claude-plugin/marketplace.json'),
        ('codex', '.agents/plugins/marketplace.json'),
    ):
        manifest = json.loads((PLUGIN / ('.' + host + '-plugin/plugin.json')).read_text())
        marketplace = json.loads((ROOT / catalog).read_text())
        if manifest['name'] != 'agent-relay' or marketplace['name'] != 'agent-relay':
            errors.append(host + ': unexpected package name')
        entries = marketplace['plugins']
        if len(entries) != 1 or entries[0]['name'] != 'agent-relay':
            errors.append(host + ': expected exactly one agent-relay entry')
            continue
        entry = entries[0]
        source = entry['source']
        source_path = source if isinstance(source, str) else source['path']
        if source_path != './plugins/agent-relay':
            errors.append(host + ': unexpected marketplace source')
        versions.extend((manifest['version'], entry['version']))
        if 'metadata' in marketplace:
            versions.append(marketplace['metadata']['version'])
        if host == 'codex':
            if manifest.get('skills') != './codex-skills/':
                errors.append('codex: keep discovery separate from Claude skills')
            if not (PLUGIN / 'codex-skills').is_dir():
                errors.append('codex: skill directory is missing')
    changelog = (PLUGIN / 'CHANGELOG.md').read_text()
    header = re.search(r'^## ([0-9]+\.[0-9]+\.[0-9]+) — ', changelog, re.M)
    versions.append(header.group(1) if header else None)
    if None in versions or len(set(versions)) != 1:
        errors.append('version mismatch: ' + repr(versions))
    if (ROOT / 'LICENSE').read_bytes() != (PLUGIN / 'LICENSE').read_bytes():
        errors.append('root and bundled licenses differ')

    required_files = (
        'scripts/relay-job.py',
        'scripts/relay/__init__.py',
        'scripts/relay/store.py',
        'scripts/relay/lifecycle.py',
        'scripts/relay/cli.py',
        'scripts/relay/adapters/__init__.py',
        'scripts/relay/adapters/opencode.py',
        'scripts/relay/adapters/codex.py',
        'scripts/oc-run.sh',
        'scripts/oc-sub.sh',
        'scripts/oc-api.sh',
        'scripts/oc-model.py',
        'scripts/oc-session.py',
        'scripts/oc-setup.sh',
        'scripts/claude-setup.sh',
        'commands/setup.md',
        'commands/run.md',
        'commands/review.md',
        'commands/adversarial-review.md',
        'commands/status.md',
        'commands/result.md',
        'commands/resume.md',
        'commands/cancel.md',
        'agents/relay-opencode-sub.md',
        'agents/relay-opencode-api.md',
        'agents/relay-codex.md',
        'agents/relay-claude.md',
        'skills/relay-runtime/SKILL.md',
        'skills/relay-prompting/SKILL.md',
        'prompts/review.md',
        'prompts/adversarial-review.md',
        'prompts/codex-runtime.md',
        'assets/icon.svg',
    )
    for relative in required_files:
        if not (PLUGIN / relative).is_file():
            errors.append('missing package file: ' + relative)
    claude_setup = PLUGIN / 'scripts/claude-setup.sh'
    if claude_setup.is_file() and not os.access(str(claude_setup), os.X_OK):
        errors.append('scripts/claude-setup.sh must be executable')
    for relative in ('test_lifecycle.py', 'test_notify.py', 'lifecycle_harness.py'):
        if not (ROOT / 'tests' / relative).is_file():
            errors.append('missing lifecycle test file: ' + relative)
    for skill in (
        'setup', 'run', 'review', 'adversarial-review',
        'status', 'result', 'resume', 'cancel',
    ):
        if not (PLUGIN / 'codex-skills' / ('relay-' + skill) / 'SKILL.md').is_file():
            errors.append('missing Codex skill: relay-' + skill)

    host_docs = []
    for directory in ('commands', 'agents', 'skills', 'codex-skills'):
        host_docs.extend((PLUGIN / directory).rglob('*.md'))
    reference_pattern = re.compile(r'(?P<path>(?:scripts|\.\./\.\./prompts)/[A-Za-z0-9._/-]+)')
    for doc in host_docs:
        content = doc.read_text(encoding='utf-8')
        for match in reference_pattern.finditer(content):
            reference = match.group('path')
            target = (PLUGIN / reference) if reference.startswith('scripts/') else (doc.parent / reference)
            if not target.is_file():
                errors.append('{} references missing package file {}'.format(
                    doc.relative_to(PLUGIN), reference,
                ))
        for line_number, line in enumerate(content.splitlines(), 1):
            if 'relay-job.py' in line and re.search(r'\brun\b', line) and '--backend' not in line:
                errors.append('{}:{} invokes run without --backend'.format(
                    doc.relative_to(PLUGIN), line_number,
                ))

    legacy_package_name = 'opencode' + '-' + 'helper'
    legacy_manager_name = 'oc' + '-' + 'job.py'
    legacy_environment_prefix = 'OC' + '_' + 'JOB_'
    forbidden_references = (
        legacy_package_name,
        legacy_manager_name,
        legacy_environment_prefix,
        legacy_package_name + '/state',
    )
    excluded_dirs = {'_archive', 'archive', '_dev', 'dev', '__pycache__'}
    for directory, child_dirs, filenames in os.walk(PLUGIN):
        child_dirs[:] = [name for name in child_dirs if name not in excluded_dirs]
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink() or not path.is_file():
                continue
            try:
                content = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for reference in forbidden_references:
                if reference in content:
                    errors.append('{} references {}'.format(path.relative_to(PLUGIN), reference))

    readme = (PLUGIN / 'README.md').read_text(encoding='utf-8')
    if 'KNOWN-LIMITATIONS' not in readme:
        errors.append('README must include a known-limitations section')
    for manifest_path in (PLUGIN / '.claude-plugin/plugin.json', PLUGIN / '.codex-plugin/plugin.json'):
        description = json.loads(manifest_path.read_text(encoding='utf-8'))['description'].casefold()
        if 'opencode' not in description or 'codex' not in description:
            errors.append(manifest_path.relative_to(PLUGIN).as_posix() + ': describe both OpenCode and Codex backends')

    # Backend-listing surfaces must name all three backends; this is a presence check on an explicit list, so keep the list in step with files that enumerate backends, and it does not verify the wording of each rule.
    backend_surfaces = (
        ROOT / 'README.md',
        ROOT / 'SECURITY.md',
        ROOT / '.claude-plugin/marketplace.json',
        PLUGIN / 'README.md',
        PLUGIN / '.claude-plugin/plugin.json',
        PLUGIN / '.codex-plugin/plugin.json',
        PLUGIN / 'commands/run.md',
        PLUGIN / 'commands/review.md',
        PLUGIN / 'commands/adversarial-review.md',
        PLUGIN / 'commands/resume.md',
        PLUGIN / 'commands/setup.md',
        PLUGIN / 'codex-skills/relay-run/SKILL.md',
        PLUGIN / 'codex-skills/relay-review/SKILL.md',
        PLUGIN / 'codex-skills/relay-resume/SKILL.md',
        PLUGIN / 'codex-skills/relay-setup/SKILL.md',
        PLUGIN / 'codex-skills/relay-result/SKILL.md',
        PLUGIN / 'skills/relay-runtime/SKILL.md',
        PLUGIN / 'prompts/codex-runtime.md',
    )
    backend_terms = (
        ('OpenCode', r'\bopencode\b'),
        ('Codex', r'\bcodex\b'),
        ('Claude', r'\bclaude\b'),
    )
    for surface in backend_surfaces:
        text = surface.read_text(encoding='utf-8')
        missing = [name for name, pattern in backend_terms if not re.search(pattern, text, re.IGNORECASE)]
        if missing:
            errors.append('{}: backend surface does not name {}'.format(
                surface.relative_to(ROOT).as_posix(), ', '.join(missing),
            ))

    public = os.environ.get('GITHUB_REF_NAME') == 'main' or os.environ.get('GITHUB_BASE_REF') == 'main'
    if public:
        for private in PRIVATE_PATHS:
            if (ROOT / private).exists():
                errors.append(private + ' present in public tree')
            history = subprocess.check_output(
                ['git', 'log', '--format=%H', 'HEAD', '--', private], cwd=ROOT, text=True,
            )
            if history.strip():
                errors.append(private + ' present in public history')
    if errors:
        print('\n'.join('FAIL: ' + error for error in errors))
        return 1
    print('OK: package metadata, version ' + versions[0] + ', and public boundary')
    return 0


if __name__ == '__main__':
    sys.exit(main())
