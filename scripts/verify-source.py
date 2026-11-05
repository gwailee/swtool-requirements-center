#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Verify the standalone public inventory offline; never traverse runtime data."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
METADATA = frozenset({'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
                     'CODE_OF_CONDUCT.md', 'THIRD_PARTY_NOTICES.md', '.gitignore',
                     '.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/feature.yml',
                     '.github/ISSUE_TEMPLATE/config.yml', '.github/PULL_REQUEST_TEMPLATE.md',
                     '.github/workflows/ci.yml', '.github/workflows/release.yml',
                     'requirements-dev.txt', 'scripts/verify-source.py', 'scripts/build-source.py', 'tests/test_http.py'})


def read(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or str(path) != name or '\\' in name:
        raise ValueError('Unsafe inventory path')
    if name not in METADATA and (any(part.startswith('.') or part.lower() in {'data', 'server_data', 'uploads', 'cache', '__pycache__', 'secrets', 'credentials'} for part in path.parts)
                                 or any(word in path.name.lower() for word in ('.env', 'secret', 'credential')) and path.name != 'service.env.example'):
        raise ValueError('Private inventory path')
    if path.suffix.lower() in {'.pyc', '.db', '.sqlite', '.sqlite3', '.log', '.zip', '.pem', '.key'}:
        raise ValueError('Runtime inventory path')
    target = ROOT
    for component in path.parts:
        target = target / component
        if target.is_symlink(): raise ValueError('Symlink inventory path')
    if not target.is_file(): raise ValueError('Missing inventory file: ' + name)
    if target.stat().st_size > 16 * 1024 * 1024: raise ValueError('Inventory file too large')
    return target.read_bytes()


def verified_files():
    manifest_bytes = read('SOURCE_MANIFEST.json')
    manifest = json.loads(manifest_bytes)
    if (manifest.get('format') != 1 or not isinstance(manifest.get('files'), list)
        or not isinstance(manifest.get('public_metadata'), list)
        or len(manifest['files']) + len(manifest['public_metadata']) > 512):
        raise ValueError('Invalid source manifest')
    metadata_names = [entry['path'] for entry in manifest['public_metadata']]
    if set(metadata_names) != METADATA - {'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md'} or len(metadata_names) != len(set(metadata_names)):
        raise ValueError('Public metadata inventory differs from explicit allowlist')
    entries = {'SOURCE_MANIFEST.json': manifest_bytes}
    for entry in manifest['files'] + manifest.get('public_metadata', []):
        name = entry['path']
        if name.startswith('standalone/requirements-center/'):
            raise ValueError('Legacy snapshot must not enter public release')
        if name in entries: raise ValueError('Duplicate manifest member')
        if entry in manifest.get('public_metadata', []) and name not in METADATA:
            raise ValueError('Unknown public metadata')
        payload = read(name)
        if hashlib.sha256(payload).hexdigest() != entry['sha256'] or len(payload) != entry['size']:
            raise ValueError('Inventory hash mismatch: ' + name)
        if 'resource' in entry:
            if (PurePosixPath(name).parent != PurePosixPath('web/vendor/katex/fonts')
                or PurePosixPath(name).suffix not in {'.ttf', '.woff', '.woff2'}
                or entry['resource'] != 'resources/fonts/' + PurePosixPath(name).name + '.b64'
                or entry['resource'] in entries):
                raise ValueError('Unapproved binary resource')
            encoded = read(entry['resource'])
            if len(encoded) != entry['resource_size'] or hashlib.sha256(encoded).hexdigest() != entry['resource_sha256']:
                raise ValueError('Encoded font hash mismatch')
            if base64.b64decode(encoded.replace(b'\n', b''), validate=True) != payload:
                raise ValueError('Font resource mismatch')
            entries[entry['resource']] = encoded
        entries[name] = payload
    if sum(map(len, entries.values())) > 32 * 1024 * 1024: raise ValueError('Source inventory too large')
    return entries


if __name__ == '__main__':
    files = verified_files()
    print(json.dumps({'verified_files': len(files)}, sort_keys=True))
