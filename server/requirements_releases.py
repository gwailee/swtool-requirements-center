# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Read-only release metadata for the standalone requirements center."""
import json

from fastapi import Depends, HTTPException


def install_routes(host):
    @host.app.get('/api/requirements/releases')
    def releases(user=Depends(host.current_user)):
        host._requirements_guard()
        if user['status'] != 'active':
            raise HTTPException(403, '账号不可用')
        try:
            document = json.loads((host.ROOT / 'release' / 'releases.json').read_text(encoding='utf-8'))
            current_version = document.get('current_version')
            entries = document.get('releases', [])
        except (OSError, ValueError, TypeError):
            raise HTTPException(503, '版本清单暂不可用') from None
        if not isinstance(entries, list) or not isinstance(current_version, str):
            raise HTTPException(503, '版本清单格式无效')
        fields = ('platform', 'version', 'released_at', 'features', 'notes', 'url', 'sha256', 'size', 'signature')
        return {'current_version': current_version, 'releases': [
            {key: entry.get(key) for key in fields} for entry in entries if isinstance(entry, dict)
        ]}
