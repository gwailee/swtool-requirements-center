#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Build an offline, inventoried source ZIP and matching SHA256 file."""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('verify_source', Path(__file__).with_name('verify-source.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = module.verified_files()
    output = args.output.absolute()
    checksum = output.with_name(output.name + '.sha256')
    if output.exists() or output.is_symlink() or output.suffix != '.zip':
        raise ValueError('Output must be a new .zip file')
    if checksum.exists() or checksum.is_symlink():
        raise ValueError('Checksum output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(member, payload)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with checksum.open('x') as stream: stream.write(digest + '  ' + output.name + '\n')
    print(str(output))


if __name__ == '__main__': main()
