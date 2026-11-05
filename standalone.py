# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Native Python/PostgreSQL entry point; no image/ML services are exposed."""
import os
import secrets
from pathlib import Path
os.environ['SWTOOL_REQUIREMENTS_STANDALONE'] = '1'
if not os.environ.get('SWTOOL_DATABASE_URL', '').startswith(('postgresql://', 'postgres://')):
    raise RuntimeError('必须配置 SWTOOL_DATABASE_URL；本独立版本仅支持 PostgreSQL')
data = Path(os.environ.setdefault('SWTOOL_DATA_DIR', str(Path(__file__).parent / 'data')))
data.mkdir(parents=True, exist_ok=True, mode=0o700)
if not os.environ.get('SWTOOL_SECRET'):
    secret_file = data / '.secret'
    try:
        descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(secrets.token_urlsafe(48))
            stream.flush()
            os.fsync(stream.fileno())
    os.environ['SWTOOL_SECRET'] = secret_file.read_text().strip()
    if not os.environ['SWTOOL_SECRET']:
        raise RuntimeError('加密主密钥文件为空，请检查初始化是否完成')
from server.app import app
# Shared handlers/schema implement the same protocol; only requirements/auth routes ship as services.
exact = {'/auth/register', '/auth/login', '/me', '/admin/users', '/issues/export/zip', '/issues/import/zip',
         '/admin/plugins/upload', '/admin/plugins/requirements-center/package', '/extensions',
         '/api/plugins', '/admin/plugins', '/admin/plugins/{plugin_name}', '/admin/users/{user_id}/status', '/admin/users/{user_id}/password', '/api/plugins/requirements-center'}
exact.update({'/api/tickets', '/api/tickets/{ticket_id}', '/api/tickets/{ticket_id}/comments', '/api/tickets/{ticket_id}/comments/{comment_id}',
              '/api/tickets/{ticket_id}/attachments', '/api/tickets/{ticket_id}/attachments/{attachment_id}',
              '/api/requirements/source', '/api/requirements/releases', '/api/requirements/identity', '/api/requirements/cases', '/tools/releases',
              '/api/requirements/key', '/api/requirements/key/rotate', '/api/requirements/key/secret',
              '/api/requirements/connections', '/api/requirements/connections/{ident}',
              '/api/requirements/connections/{ident}/proxy/{path:path}'})
exact.update({'/api/requirements/driver', '/api/requirements/driver/logs',
              '/api/requirements/driver/interject', '/api/requirements/driver/ssh-targets',
              '/api/requirements/driver/ssh-targets/{target_id}',
              '/api/requirements/driver/host-key', '/api/requirements/driver/{action}'})
app.router.routes[:] = [route for route in app.router.routes if
    getattr(route, 'path', None) in exact or
    getattr(route, 'name', None) == 'web']
app.title = 'SWTool 需求中心'
