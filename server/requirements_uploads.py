"""Bounded attachment chunks; filesystem sessions require no production DDL."""
import base64
import binascii
import fcntl
import json
import os
import secrets
import shutil
from starlette.concurrency import run_in_threadpool
import time
from contextlib import contextmanager
from pathlib import Path
from fastapi import Depends, HTTPException, Request

MAX_SIZE = 1024 ** 3
CHUNK_SIZE = 1024 ** 2
TTL = 86400


def install_routes(host):
    def root():
        path = host.DATA_DIR / 'requirements_attachments' / '.sessions'
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def access(ticket, user, comment=None):
        host._requirements_guard()
        con = host.db()
        try:
            row = con.execute('SELECT * FROM issues WHERE id=?', (ticket,)).fetchone()
            if not row: raise HTTPException(404, '需求不存在')
            manager = host._requirements_center_manager(user, con)
            if comment is None:
                if not manager and row['author_id'] != user['id']:
                    raise HTTPException(403, '无权上传该需求附件')
            else:
                reply = con.execute('SELECT id,author_id FROM issue_comments WHERE id=? AND issue_id=?', (comment, ticket)).fetchone()
                if not reply: raise HTTPException(404, '回复不存在')
                if not manager and reply['author_id'] != user['id']:
                    raise HTTPException(403, '无权上传该回复附件')
        finally:
            con.close()

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 2 * CHUNK_SIZE: raise HTTPException(413, '上传分片请求过大')
            data.extend(chunk)
        try:
            result = json.loads(data)
            if not isinstance(result, dict): raise ValueError()
            return result
        except (ValueError, UnicodeError): raise HTTPException(400, '上传参数无效')

    @contextmanager
    def session(ident, ticket, user):
        if len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident): raise HTTPException(404, '上传会话不存在')
        path = root() / ident
        try: lock = open(path / 'lock', 'r+')
        except FileNotFoundError: raise HTTPException(404, '上传会话不存在')
        with lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads((path / 'meta.json').read_text())
            if state['ticket_id'] != ticket or state['owner'] != str(user['id']): raise HTTPException(403, '无权访问上传会话')
            access(ticket, user, state['comment_id'])
            if state['expires'] < time.time():
                (path / 'data').unlink(missing_ok=True)
                raise HTTPException(410, '上传会话已过期，请重新选择附件')
            yield path, state

    def save(path, state):
        temp = path / 'meta.tmp'
        temp.write_text(json.dumps(state))
        os.replace(temp, path / 'meta.json')

    @host.app.post('/api/tickets/{ticket_id}/attachment-uploads', status_code=201)
    async def init(ticket_id: str, request: Request, user=Depends(host.current_issue_user)):
        data = await body(request)
        return await run_in_threadpool(begin, ticket_id, data, user)

    def begin(ticket_id, data, user):
        comment = data.get('comment_id')
        if comment is not None and (type(comment) is not int or comment < 1): raise HTTPException(400, '回复编号无效')
        access(ticket_id, user, comment)
        size, mime = data.get('size'), str(data.get('mime_type') or 'application/octet-stream').lower()
        if type(size) is not int or not 0 <= size <= MAX_SIZE: raise HTTPException(413, '单个附件不能超过 1 GiB')
        mime = {'audio/x-wav':'audio/wav','audio/wave':'audio/wav','audio/x-flac':'audio/flac','audio/x-m4a':'audio/mp4','video/avi':'video/x-msvideo','application/x-zip-compressed':'application/zip'}.get(mime, mime)
        if mime not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'audio/mpeg', 'audio/wav', 'video/x-msvideo', 'audio/ogg', 'audio/flac', 'audio/aac', 'audio/mp4', 'video/quicktime', 'video/3gpp', 'video/mp4', 'video/webm', 'application/pdf', 'text/plain', 'text/csv', 'application/zip', 'application/json', 'application/octet-stream'}:
            raise HTTPException(400, '附件格式不受支持')
        # Bound pending disk reservations for each actor. Expired data is reclaimed.
        with open(root() / '.admission', 'a+') as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            active = 0; count = 0; reserved = 0; remaining = 0
            for entry in root().iterdir():
                if not entry.is_dir() or entry.is_symlink(): continue
                try:
                    old = json.loads((entry / 'meta.json').read_text())
                    if old['expires'] < time.time():
                        with open(entry / 'lock', 'r+') as lock:
                            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError: pass
                            else:
                                old = json.loads((entry / 'meta.json').read_text())
                                if old['expires'] < time.time():
                                    target = host.DATA_DIR / 'requirements_attachments' / old['ticket_id'] / entry.name
                                    con = host.db()
                                    try:
                                        exists = con.execute('SELECT 1 FROM issue_attachments WHERE id=?', (entry.name,)).fetchone()
                                    finally: con.close()
                                    if not exists: target.unlink(missing_ok=True)
                                    shutil.rmtree(entry); continue
                    count += 1
                    if not old.get('result'):
                        reserved += old['size']; remaining += max(0, old['size'] - old['received'])
                        if old['owner'] == str(user['id']): active += 1
                except (OSError, ValueError, KeyError, TypeError):
                    raise HTTPException(503, '上传会话状态暂不可用，请稍后重试') from None
            if count >= 10000: raise HTTPException(429, '上传会话繁忙，请稍后重试')
            if active >= 10: raise HTTPException(429, '同时上传附件过多，请先完成或取消已有上传')
            if reserved + size > 10 * MAX_SIZE: raise HTTPException(429, '上传预留总容量超过 10 GiB')
            if shutil.disk_usage(root()).free < remaining + size + MAX_SIZE:
                raise HTTPException(507, '附件磁盘空间不足，请保留至少 1 GiB 可用空间')
            ident = secrets.token_hex(16); path = root() / ident; path.mkdir(mode=0o700)
            (path / 'lock').touch(mode=0o600); (path / 'data').touch(mode=0o600)
            state = dict(ticket_id=ticket_id, owner=str(user['id']), comment_id=comment, size=size,
                         mime_type=mime, name=Path(str(data.get('name') or '附件')).name[:180], received=0,
                         expires=time.time()+TTL)
            save(path, state)
        return {'upload_id': ident, 'chunk_size': CHUNK_SIZE, 'received': 0}

    @host.app.post('/api/tickets/{ticket_id}/attachment-uploads/{upload_id}/chunks')
    async def chunk(ticket_id: str, upload_id: str, request: Request, user=Depends(host.current_issue_user)):
        data = await body(request)
        return await run_in_threadpool(write_chunk, ticket_id, upload_id, data, user)

    def write_chunk(ticket_id, upload_id, data, user):
        with session(upload_id, ticket_id, user) as (path, state):
            if state.get('result'): raise HTTPException(409, '附件上传已完成')
            try: raw = base64.b64decode(data.get('contentBase64', ''), validate=True)
            except (ValueError, TypeError, binascii.Error): raise HTTPException(400, '分片编码无效')
            offset = data.get('offset')
            if type(offset) is not int or offset < 0: raise HTTPException(409, '分片位置不匹配')
            if not raw or len(raw) > CHUNK_SIZE or offset + len(raw) > state['size']:
                raise HTTPException(413, '分片超过附件大小限制')
            if offset < state['received']:
                if offset + len(raw) > state['received']: raise HTTPException(409, '分片位置不匹配')
                with open(path / 'data', 'rb') as stream:
                    stream.seek(offset)
                    if stream.read(len(raw)) != raw: raise HTTPException(409, '重复分片内容不匹配')
                return {'received': state['received']}
            if offset != state['received']: raise HTTPException(409, '分片位置不匹配')
            with open(path / 'data', 'r+b') as stream:
                stream.seek(offset); stream.write(raw); stream.truncate(offset + len(raw))
            state['received'] += len(raw); save(path, state)
            return {'received': state['received']}

    @host.app.post('/api/tickets/{ticket_id}/attachment-uploads/{upload_id}/complete')
    def complete(ticket_id: str, upload_id: str, user=Depends(host.current_issue_user)):
        with session(upload_id, ticket_id, user) as (path, state):
            if state.get('result'): return state['result']
            if state['received'] != state['size']: raise HTTPException(409, '附件尚未上传完成')
            con = host.db(); target = host.DATA_DIR / 'requirements_attachments' / ticket_id / upload_id
            try:
                ticket = con.execute('SELECT * FROM issues WHERE id=? FOR KEY SHARE', (ticket_id,)).fetchone()
                if not ticket: raise HTTPException(404, '需求不存在')
                manager = host._requirements_center_manager(user, con)
                if state['comment_id'] is None:
                    if not manager and ticket['author_id'] != user['id']: raise HTTPException(403, '无权上传该需求附件')
                else:
                    reply = con.execute('SELECT id,author_id FROM issue_comments WHERE id=? AND issue_id=? FOR KEY SHARE', (state['comment_id'], ticket_id)).fetchone()
                    if not reply: raise HTTPException(404, '回复不存在')
                    if not manager and reply['author_id'] != user['id']: raise HTTPException(403, '无权上传该回复附件')
                existing = con.execute('SELECT * FROM issue_attachments WHERE id=? AND issue_id=?', (upload_id,ticket_id)).fetchone()
                if existing:
                    result = host._attachment_item(existing)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if (path / 'data').exists(): os.replace(path / 'data', target)
                    if not target.is_file() or target.stat().st_size != state['size']: raise HTTPException(409, '附件内容不完整')
                    now = time.time()
                    con.execute('INSERT INTO issue_attachments(id,issue_id,comment_id,uploaded_by,original_name,stored_path,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?,?,?)', (upload_id,ticket_id,state['comment_id'],user['id'],state['name'],str(target),state['mime_type'],state['size'],now))
                    con.execute('UPDATE issues SET updated_at=? WHERE id=?', (now,ticket_id)); con.commit()
                    result = host._attachment_item(con.execute('SELECT * FROM issue_attachments WHERE id=?', (upload_id,)).fetchone())
                state['result'] = result; save(path, state); return result
            finally: con.close()

    @host.app.delete('/api/tickets/{ticket_id}/attachment-uploads/{upload_id}')
    def cancel(ticket_id: str, upload_id: str, user=Depends(host.current_issue_user)):
        with session(upload_id, ticket_id, user) as (path, state):
            if not state.get('result'):
                (path / 'data').unlink(missing_ok=True)
                con = host.db()
                try:
                    exists = con.execute('SELECT 1 FROM issue_attachments WHERE id=?', (upload_id,)).fetchone()
                finally: con.close()
                if not exists: (host.DATA_DIR / 'requirements_attachments' / ticket_id / upload_id).unlink(missing_ok=True)
                state['expires'] = 0; save(path, state)
            return {'cancelled': True}
