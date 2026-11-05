# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Export only the explicitly declared standalone source distribution."""
import json
import ipaddress
from fastapi import Depends, HTTPException, Request
from starlette.responses import Response
from export_source import build_source_archive


def build_source(root=None):
    return build_source_archive()


def _client_ip(request):
    peer = request.client.host if request.client else ""
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"
    if not peer_address.is_loopback:
        return str(peer_address)
    forwarded = request.headers.get("X-Real-IP", "")
    try:
        return str(ipaddress.ip_address(forwarded.strip()))
    except ValueError:
        return str(peer_address)


def install_routes(host):
    @host.app.get('/api/requirements/source')
    def download(request: Request, user=Depends(host.current_user)):
        host._requirements_guard()
        if user['status'] != 'active':
            raise HTTPException(403, '账号不可用')
        try:
            payload = build_source_archive()
        except (OSError, ValueError):
            raise HTTPException(503, '源码发行文件不完整') from None
        con = host.db()
        try:
            host.audit(con, user['id'], 'requirements_zip_download', details=json.dumps({
                'kind': 'source', 'ip': _client_ip(request), 'bytes': len(payload)
            }))
            con.commit()
        finally:
            con.close()
        return Response(payload, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="requirements-center-source.zip"',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        })

    @host.app.get('/api/requirements/downloads')
    def downloads(user=Depends(host.current_user)):
        if 'admin' not in host.roles_of(user):
            raise HTTPException(403, '仅管理员可查看下载记录')
        con = host.db()
        try:
            rows = con.execute("SELECT details,created_at FROM audit_events WHERE action='requirements_zip_download' ORDER BY id DESC LIMIT 500").fetchall()
            count_rows = con.execute("SELECT COALESCE(details::jsonb ->> 'kind','source') AS kind,COUNT(*) AS count FROM audit_events WHERE action='requirements_zip_download' GROUP BY kind").fetchall()
            counts = {'source': 0, 'plugin': 0}
            for row in count_rows:
                if row['kind'] in counts: counts[row['kind']] = row['count']
            records = []
            for row in rows:
                try: details = json.loads(row['details'] or '{}')
                except (TypeError, ValueError): details = {}
                kind = details.get('kind') if details.get('kind') in counts else 'source'
                records.append({'kind': kind, 'ip': details.get('ip', ''), 'bytes': details.get('bytes', 0), 'created_at': row['created_at']})
        finally:
            con.close()
        return {'counts': counts, 'records': records}
