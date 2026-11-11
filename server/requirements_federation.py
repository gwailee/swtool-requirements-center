# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""User-scoped requirements keys and an allowlisted server-to-server gateway."""
from __future__ import annotations
import threading
import time
import base64
from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, Response

SCHEMA = """
CREATE TABLE IF NOT EXISTS requirements_keys(
 user_id BIGINT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
 suffix TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
 scope TEXT DEFAULT 'tickets:read' NOT NULL);
CREATE TABLE IF NOT EXISTS requirements_connections(
 id TEXT PRIMARY KEY, owner_id BIGINT NOT NULL, name TEXT NOT NULL, base_url TEXT NOT NULL,
 encrypted_key TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
"""
PREFIX = 'swtool_'
PROTOCOL = 'swtool-requirements/1'
SUPPORTED_PROTOCOLS = (PROTOCOL,)


def is_requirements_key(raw):
    return (isinstance(raw, str) and not raw.startswith('swtool_ops_')
            and (raw.startswith('swtool_req_')
                 or re.fullmatch(re.escape(PREFIX) + r'[A-Za-z0-9_-]{48}', raw) is not None))


def remote_write_allowed(identity):
    if not isinstance(identity, dict) or identity.get('scope') != 'tickets:write':
        return False
    capabilities = identity.get('capabilities')
    if isinstance(capabilities, dict):
        return capabilities.get('remote_write') is True
    roles = identity.get('roles')
    return (isinstance(roles, list) and any(str(role).lower() in {'admin', 'superadmin'} for role in roles)
            or str(identity.get('role', '')).lower() in {'admin', 'superadmin'})


@contextmanager
def database(host):
    con = host.db()
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def cipher(host):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'swtool:requirements:v1\0' + host.SECRET).digest()))


def authenticate(host, raw):
    con = host.db()
    try:
        row = con.execute('SELECT user_id,scope FROM requirements_keys WHERE token_hash=?',
                          (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
        user = con.execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone() if row else None
        if not user or user['status'] != 'active':
            raise HTTPException(401, '需求中心 Key 已失效或账号不可用')
        host._requirements_guard()
        identity = dict(user)
        if row['scope'] != 'tickets:write':
            identity.update(role='annotator', roles='["annotator"]')
        return identity | {'_requirements_key': True, '_requirements_scope': row['scope']}
    finally:
        con.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_addresses(hostname, port):
    addresses = [item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('互联目标必须是公网地址')
    return addresses


def normalize_url(value):
    value = str(value).strip().rstrip('/')
    try:
        p = urllib.parse.urlsplit(value)
        if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ('', '/api/v1')):
            raise ValueError
        port = p.port or (443 if p.scheme == 'https' else 80)
        public_addresses(p.hostname, port)
    except (ValueError, OSError):
        raise HTTPException(400, '请输入可访问的 HTTP(S) 中心根网址；不支持账号、查询参数或子路径') from None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, '', '', ''))


def allowed(method, path, scope='tickets:read'):
    if scope not in ('tickets:read', 'tickets:write'):
        return False
    if method == 'GET':
        return (path in ('api/requirements/identity', 'api/tickets')
                or bool(re.fullmatch(r'api/tickets/[A-Za-z0-9_-]+', path))
                or bool(re.fullmatch(r'api/tickets/[A-Za-z0-9_-]+/attachments/[A-Za-z0-9_-]+', path)))
    if scope != 'tickets:write':
        return False
    if re.fullmatch(r'api/tickets/[A-Za-z0-9_-]+/attachment-uploads(?:/[A-Za-z0-9_-]+(?:/(?:chunks|complete))?)?', path):
        return method in ('POST', 'DELETE')
    if method == 'POST':
        return path == 'api/tickets' or bool(re.fullmatch(r'api/tickets/[A-Za-z0-9_-]+/(comments|attachments)', path))
    return method in ('PATCH', 'DELETE') and bool(re.fullmatch(
        r'api/tickets/[A-Za-z0-9_-]+(?:/comments/[A-Za-z0-9_-]+)?', path
    ))


_ATTACHMENT_DOWNLOAD_SLOTS = threading.BoundedSemaphore(4)
_ATTACHMENT_OUTPUTS = set()
_ATTACHMENT_OUTPUT_LOCK = threading.Lock()


def cleanup_remote_output(output):
    try:
        output.unlink(missing_ok=True)
    finally:
        with _ATTACHMENT_OUTPUT_LOCK:
            held = str(output) in _ATTACHMENT_OUTPUTS
            _ATTACHMENT_OUTPUTS.discard(str(output))
        if held: _ATTACHMENT_DOWNLOAD_SLOTS.release()


class RemoteFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            cleanup_remote_output(Path(self.path))



def remote_request(base, key, method, path, query='', body=None, content_type=None):
    attachment = method == 'GET' and bool(re.fullmatch(r'api/tickets/[A-Za-z0-9_-]+/attachments/[A-Za-z0-9_-]+', path))
    if attachment and not _ATTACHMENT_DOWNLOAD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, '附件下载繁忙，请稍后重试')
    try:
        result = _remote_request(base, key, method, path, query, body, content_type,
                                 attachment=attachment, deadline=time.monotonic() + (300 if attachment else 60))
        if attachment:
            with _ATTACHMENT_OUTPUT_LOCK: _ATTACHMENT_OUTPUTS.add(str(result[0]))
        return result
    except BaseException:
        if attachment: _ATTACHMENT_DOWNLOAD_SLOTS.release()
        raise


def _remote_request(base, key, method, path, query='', body=None, content_type=None, *, attachment=False, deadline):
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0: raise HTTPException(504, '远端响应超过总时限')
        return min(60, left)

    base = normalize_url(base)
    parsed = urllib.parse.urlsplit(base)
    try:
        address = public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))[0]
    except (ValueError, OSError):
        raise HTTPException(400, '远端必须解析到公网地址') from None

    class PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)

    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            transport = socket.create_connection((address, self.port), self.timeout, self.source_address)
            try:
                self.sock = self._context.wrap_socket(transport, server_hostname=self.host)
            except BaseException:
                transport.close()
                raise

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(PinnedHTTP, request)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(PinnedHTTPS, request, context=self._context)

    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    if content_type:
        headers['Content-Type'] = content_type
    request = urllib.request.Request(base + '/api/v1/' + path + ('?' + query if query else ''),
                                     data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), HTTPHandler(), HTTPSHandler())
    try:
        response = opener.open(request, timeout=remaining())
    except urllib.error.HTTPError as exc:
        detail = '远端请求失败（HTTP %s）' % exc.code
        try:
            message = json.loads(exc.read(65536)).get('detail')
            if isinstance(message, str): detail += '：' + message[:500]
        except (ValueError, OSError): pass
        finally: exc.close()
        raise HTTPException(exc.code, detail) from None
    except (OSError, ValueError):
        raise HTTPException(502, '无法连接目标需求中心，请检查网址、网络和 HTTPS 证书') from None
    temp = tempfile.NamedTemporaryFile(prefix='swtool-remote-', delete=False)
    size = 0
    limit = (1024 ** 3 if attachment else 513 * 1024 * 1024 if path == 'issues/export/zip' else
             65536 if path == 'api/requirements/identity' else 12 * 1024 * 1024)
    try:
        with response, temp:
            while True:
                timeout = remaining()
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None: sock.settimeout(timeout)
                chunk = (getattr(response, 'read1', None) or response.read)(1024 * 1024)
                remaining()
                if not chunk: break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, '远端响应超过限制')
                temp.write(chunk)
        return Path(temp.name), response.status, response.headers.get('Content-Type', 'application/octet-stream')
    except BaseException:
        Path(temp.name).unlink(missing_ok=True)
        raise


def install_routes(host):
    app = host.app

    def local_user(user=Depends(host.current_user)):
        host._requirements_guard()
        if user['status'] != 'active': raise HTTPException(403, '账号不可用')
        return user

    def connection(con, ident, user):
        row = con.execute('SELECT * FROM requirements_connections WHERE id=? AND owner_id=?',
                          (ident, user['id'])).fetchone()
        if not row: raise HTTPException(404, '连接不存在')
        return row

    def decrypt(value):
        try: return cipher(host).decrypt(value.encode()).decode()
        except InvalidToken: raise HTTPException(503, '加密主密钥不匹配，请恢复主密钥或重新配置 Key') from None

    @app.get('/api/requirements/identity')
    def identity(user=Depends(host.current_issue_user)):
        host._requirements_guard()
        if dict(user).get('_ops_token'): raise HTTPException(403, '请使用个人需求中心 Key')
        if user['status'] != 'active': raise HTTPException(403, '账号不可用')
        scope = dict(user).get('_requirements_scope', 'tickets:write')
        return {'protocol': PROTOCOL, **host.user_view(user),
                'id': user['id'], 'scope': scope,
                'capabilities': {'remote_write': scope == 'tickets:write',
                                 'bearer_scopes': ['tickets:read', 'tickets:write']}}

    @app.get('/api/requirements/cases')
    def case_options(user=Depends(host.current_issue_user)):
        host._requirements_guard()
        raise HTTPException(501, '独立需求中心尚不支持病例选项')

    @app.get('/api/requirements/key')
    def key_info(user=Depends(local_user)):
        with database(host) as con:
            row = con.execute('SELECT suffix,created_at,scope FROM requirements_keys WHERE user_id=?', (user['id'],)).fetchone()
        return Response(json.dumps({'exists': bool(row), **(dict(row) if row else {})}),
                        media_type='application/json', headers={'Cache-Control': 'no-store'})

    @app.post('/api/requirements/key')
    @app.post('/api/requirements/key/rotate')
    def rotate(body: dict | None = None, user=Depends(local_user)):
        scope = (body or {}).get('scope', 'tickets:read')
        if scope not in ('tickets:read', 'tickets:write'):
            raise HTTPException(400, 'scope 必须为 tickets:read 或 tickets:write')
        raw = PREFIX + secrets.token_urlsafe(36)
        with database(host) as con:
            con.execute('INSERT INTO requirements_keys(user_id,token_hash,suffix,created_at,scope) VALUES(?,?,?,?,?) '
                        'ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash,suffix=excluded.suffix,created_at=excluded.created_at,scope=excluded.scope',
                        (user['id'], hashlib.sha256(raw.encode()).hexdigest(), raw[-6:], time.time(), scope))
            host.audit(con, user['id'], 'requirements_key_rotate')
        return Response(json.dumps({'key': raw, 'scope': scope}), media_type='application/json', headers={'Cache-Control': 'no-store'})

    @app.get('/api/requirements/key/secret')
    def reveal(user=Depends(local_user)):
        raise HTTPException(410, '个人 Key 仅生成时显示，遗失请轮换')

    @app.delete('/api/requirements/key')
    def revoke(user=Depends(local_user)):
        with database(host) as con:
            con.execute('DELETE FROM requirements_keys WHERE user_id=?', (user['id'],))
            host.audit(con, user['id'], 'requirements_key_revoke')
        return {'revoked': True}

    @app.get('/api/requirements/connections')
    def connections(user=Depends(local_user)):
        with database(host) as con:
            rows = con.execute('SELECT id,name,base_url,created_at FROM requirements_connections WHERE owner_id=? ORDER BY created_at', (user['id'],)).fetchall()
        return {'items': [dict(row) for row in rows]}

    @app.post('/api/requirements/connections', status_code=201)
    def add_connection(body: dict, user=Depends(local_user)):
        base = normalize_url(body.get('base_url', ''))
        key = str(body.get('key', '')).strip()
        name = str(body.get('name', '')).strip()
        if (not key.startswith(PREFIX)
                or len(key) > 200 or not name or len(name) > 80):
            raise HTTPException(400, '请填写连接名称和个人需求中心 Key')
        path, _, _ = remote_request(base, key, 'GET', 'api/requirements/identity')
        try:
            info = json.loads(path.read_bytes())
            if info.get('protocol') not in SUPPORTED_PROTOCOLS: raise ValueError
        except (ValueError, AttributeError): raise HTTPException(400, '目标不是兼容的需求中心') from None
        finally: path.unlink(missing_ok=True)
        ident = str(body.get('id') or secrets.token_hex(16))
        with database(host) as con:
            encrypted = cipher(host).encrypt(key.encode()).decode()
            existing = con.execute('SELECT id FROM requirements_connections WHERE id=? AND owner_id=?',
                                   (ident, user['id'])).fetchone()
            if existing:
                con.execute('UPDATE requirements_connections SET name=?,base_url=?,encrypted_key=? WHERE id=? AND owner_id=?',
                            (name, base, encrypted, ident, user['id']))
            elif body.get('id'):
                raise HTTPException(404, '连接不存在')
            else:
                con.execute('INSERT INTO requirements_connections(id,owner_id,name,base_url,encrypted_key,created_at) VALUES(?,?,?,?,?,?)',
                            (ident, user['id'], name, base, encrypted, time.time()))
        return {'id': ident, 'name': name, 'base_url': base, 'identity': info}

    @app.delete('/api/requirements/connections/{ident}')
    def delete_connection(ident: str, user=Depends(local_user)):
        with database(host) as con:
            connection(con, ident, user)
            con.execute('DELETE FROM requirements_connections WHERE id=?', (ident,))
        return {'deleted': True}

    @app.api_route('/api/requirements/connections/{ident}/proxy/{path:path}', methods=['GET', 'POST', 'PATCH', 'DELETE'])
    async def proxy(ident: str, path: str, request: Request, user=Depends(local_user)):
        if not allowed(request.method, path, 'tickets:write'): raise HTTPException(403, '该接口不允许通过需求中心互联调用')
        with database(host) as con: row = dict(connection(con, ident, user))
        key = decrypt(row['encrypted_key'])
        if request.method != 'GET':
            identity_path, _, _ = await run_in_threadpool(remote_request, row['base_url'], key, 'GET', 'api/requirements/identity')
            try:
                info = json.loads(identity_path.read_bytes())
                if (not isinstance(info, dict) or info.get('protocol') not in SUPPORTED_PROTOCOLS
                        or not remote_write_allowed(info)):
                    raise HTTPException(501, '远端未启用需求写入能力')
            except (ValueError, AttributeError):
                raise HTTPException(501, '远端未启用需求写入能力') from None
            finally:
                identity_path.unlink(missing_ok=True)
        # Multipart archives are spooled to disk, rather than retained in RAM.
        with tempfile.TemporaryFile() as body:
            count = 0
            async for chunk in request.stream():
                count += len(chunk)
                if count > (2 * 1024 * 1024 if '/attachment-uploads' in path else 513 * 1024 * 1024): raise HTTPException(413, '请求超过 513 MiB')
                body.write(chunk)
            body.seek(0)
            output, status, mime = await run_in_threadpool(remote_request, row['base_url'], key,
                request.method, path, request.url.query, body if count else None, request.headers.get('content-type'))
        # Attachment MIME types are preserved; caller chooses a safe download name.
        return RemoteFileResponse(output, status_code=status, media_type=mime,
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                                     'Content-Disposition': 'attachment'},
                            background=BackgroundTask(cleanup_remote_output, output))
