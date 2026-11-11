# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Versioned full-site CSV/attachment archives for the requirements plugin."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile

from fastapi import Depends, File, Form, HTTPException, UploadFile
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

LIMIT = 10 * 1024 * 1024 * 1024
MAX_ATTACHMENT = 1024 * 1024 * 1024
TABLES = {
    'issues': 'id title description labels case_id status author_id created_at updated_at expected_resolution_at resolved_at reproduction_steps expected_result'.split(),
    'issue_comments': 'id issue_id author_id body created_at'.split(),
    'issue_status_events': 'id issue_id from_status to_status changed_by changed_at'.split(),
    'issue_attachments': 'id issue_id comment_id uploaded_by original_name mime_type size created_at archive_path sha256'.split(),
    'users': 'id username created_at'.split(),
}
_LOCK = threading.RLock()


def _csv_bytes(rows, fields):
    # JSON scalar cells preserve null vs empty text, types, all Unicode and
    # formula prefixes without executing spreadsheet formulas on opening CSV.
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([json.dumps(row[key], ensure_ascii=False, allow_nan=False) for key in fields])
    return out.getvalue().encode('utf-8-sig')


def _begin(host, con, writing=False):
    con.execute('BEGIN')
    if host.DB_URL and not writing:
        con.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')


def _directory_fd(path, create=False):
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stream_attachment(root, stored_path, expected_size, archive, entry):
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(stored_path))
    relative = path.relative_to(root)
    directory = _directory_fd(root / relative.parent)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size or not 0 <= expected_size <= MAX_ATTACHMENT:
                raise ValueError('附件类型或大小不符')
            digest = hashlib.sha256()
            actual = 0
            with archive.open(entry, 'w', force_zip64=True) as target:
                while chunk := stream.read(1024 * 1024):
                    actual += len(chunk)
                    if actual > expected_size:
                        raise ValueError('附件读取期间大小变化')
                    digest.update(chunk)
                    target.write(chunk)
        if actual != expected_size:
            raise ValueError('附件读取期间大小变化')
        return digest.hexdigest()
    finally:
        os.close(directory)


def export_archive(host, actor):
    fd, name = tempfile.mkstemp(prefix='swtool-requirements-', suffix='.zip')
    os.close(fd)
    con = host.db()
    try:
        with _LOCK, zipfile.ZipFile(name, 'w', zipfile.ZIP_DEFLATED) as archive:
            _begin(host, con)
            rows = {table: [dict(row) for row in con.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()]
                    for table in TABLES if table != 'users'}
            refs = {row[key] for table, key in [('issues', 'author_id'), ('issue_comments', 'author_id'),
                    ('issue_status_events', 'changed_by'), ('issue_attachments', 'uploaded_by')] for row in rows[table]}
            rows['users'] = []
            for ident in sorted(refs):
                rows['users'].append({'id': ident, 'username': f'source-user-{ident}', 'created_at': 0})
            manifest = {'format': 'swtool-requirements', 'version': 2, 'scope': 'all',
                        'cell_encoding': 'json-scalar', 'exported_at': time.time(), 'files': {}}
            total = 0
            root = host.DATA_DIR / 'requirements_attachments'
            for row in rows['issue_attachments']:
                total += row['size']
                if total > LIMIT: raise ValueError('归档解压总量不能超过 10 GiB')
                entry = 'attachments/' + hashlib.sha256(row['id'].encode()).hexdigest()
                row['archive_path'] = entry
                row['sha256'] = _stream_attachment(root, row.pop('stored_path'), row['size'], archive, entry)
                manifest['files'][entry] = {'size': row['size'], 'sha256': row['sha256']}
            for table, fields in TABLES.items():
                content = _csv_bytes(rows[table], fields)
                total += len(content)
                if len(content) > 32 * 1024 * 1024 or total > LIMIT: raise ValueError('CSV 或归档超过大小限制')
                entry = table + '.csv'
                archive.writestr(entry, content)
                manifest['files'][entry] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            if len(manifest['files']) + 1 > 20000: raise ValueError('归档条目不能超过 20000')
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False))
            con.commit()
        host.audit(con, actor['id'], 'requirements_archive_export', details=json.dumps({'issues': len(rows['issues'])}))
        con.commit()
        return name
    except Exception:
        con.rollback()
        Path(name).unlink(missing_ok=True)
        raise
    finally:
        con.close()


def _load(archive):
    entries = archive.infolist()
    names = [x.filename for x in entries]
    if len(entries) > 20000 or len(set(names)) != len(names): raise ValueError('归档条目重复或过多')
    if sum(x.file_size for x in entries) > LIMIT: raise ValueError('归档解压总量不能超过 10 GiB')
    for entry in entries:
        maximum = MAX_ATTACHMENT if entry.filename.startswith('attachments/') else 32 * 1024 * 1024
        if entry.file_size > maximum: raise ValueError('归档文件超过大小限制')
        path = PurePosixPath(entry.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                or str(path) != entry.filename or entry.is_dir()
                or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
            raise ValueError('归档包含非法路径、链接或加密条目')
    if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 8 * 1024 * 1024:
        raise ValueError('缺少有效归档清单')
    manifest = json.loads(archive.read('manifest.json'))
    if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in {
        'format': 'swtool-requirements', 'scope': 'all', 'cell_encoding': 'json-scalar'}.items()) or manifest.get('version') not in (1, 2):
        raise ValueError('不支持的需求归档版本')
    files = manifest.get('files')
    if not isinstance(files, dict) or set(names) != set(files) | {'manifest.json'}:
        raise ValueError('清单与归档文件不一致')
    actual_total = archive.getinfo('manifest.json').file_size
    for name, check in files.items():
        if not isinstance(check, dict): raise ValueError('无效校验清单')
        digest = hashlib.sha256()
        actual = 0
        with archive.open(name) as source:
            while chunk := source.read(1024 * 1024):
                actual += len(chunk)
                actual_total += len(chunk)
                if actual > archive.getinfo(name).file_size or actual_total > LIMIT:
                    raise ValueError('归档实际解压大小超限')
                digest.update(chunk)
        if actual != archive.getinfo(name).file_size:
            raise ValueError('归档实际大小不匹配')
        if archive.getinfo(name).file_size != check.get('size') or digest.hexdigest() != check.get('sha256'):
            raise ValueError(f'文件校验失败：{name}')
    tables = {}
    csv.field_size_limit(32 * 1024 * 1024)
    for table, fields in TABLES.items():
        if table == 'issues' and manifest['version'] == 1:
            fields = fields[:-2]
        name = table + '.csv'
        if name not in files or archive.getinfo(name).file_size > 32 * 1024 * 1024:
            raise ValueError(f'缺少或超限的 CSV：{name}')
        reader = csv.reader(io.StringIO(archive.read(name).decode('utf-8-sig'), newline=''))
        if next(reader, None) != fields: raise ValueError(f'CSV 表头不匹配：{name}')
        rows = []
        for cells in reader:
            if len(cells) != len(fields): raise ValueError(f'CSV 列数错误：{name}')
            row = {key: json.loads(value) for key, value in zip(fields, cells)}
            if table == 'issues':
                row.setdefault('reproduction_steps', '')
                row.setdefault('expected_result', '')
            rows.append(row)
        tables[table] = rows
    for row in tables['issues']:
        if row['status'] == 'closed': row['status'] = 'resolved'
    for row in tables['issue_status_events']:
        if row['from_status'] == 'closed': row['from_status'] = 'resolved'
        if row['to_status'] == 'closed': row['to_status'] = 'resolved'
    _validate(tables, files)
    issues = {row['id']: row for row in tables['issues']}
    for table, field in [('issue_comments', 'created_at'), ('issue_status_events', 'changed_at'), ('issue_attachments', 'created_at')]:
        for row in tables[table]:
            issue = issues[row['issue_id']]
            issue['updated_at'] = max(issue['updated_at'], row[field])
    return tables


def _validate(tables, files):
    def string(value): return isinstance(value, str)
    def integer(value): return type(value) is int and value > 0
    def timestamp(value): return type(value) in (int, float) and math.isfinite(value) and value >= 0
    def fail(): raise ValueError('CSV 字段类型、唯一性或关联不正确')
    indexes = {}
    for table, rows in tables.items():
        index = {}
        for row in rows:
            ident = row['id']
            if not (integer(ident) if table in {'users', 'issue_comments', 'issue_status_events'} else string(ident) and bool(ident)):
                fail()
            if ident in index: fail()
            index[ident] = row
        indexes[table] = index
    users, issues, comments = indexes['users'], indexes['issues'], indexes['issue_comments']
    if len({row['username'] for row in users.values() if string(row['username'])}) != len(users): fail()
    for row in users.values():
        if not string(row['username']) or not row['username'] or not timestamp(row['created_at']): fail()
    statuses = {'pending', 'open', 'in_progress', 'resolved', 'withdrawn'}
    for row in issues.values():
        if not all(string(row[k]) for k in ['title', 'description', 'labels', 'status', 'reproduction_steps', 'expected_result']) or not row['title'].strip(): fail()
        labels = json.loads(row['labels'])
        if not isinstance(labels, list) or not all(string(x) for x in labels): fail()
        if row['status'] not in statuses or not integer(row['author_id']) or row['author_id'] not in users: fail()
        if row['case_id'] is not None and not string(row['case_id']): fail()
        for key in ['created_at', 'updated_at', 'expected_resolution_at', 'resolved_at']:
            if row[key] is None and key in {'expected_resolution_at', 'resolved_at'}: continue
            if not timestamp(row[key]): fail()
    paths = set()
    for table, who, when in [('issue_comments', 'author_id', 'created_at'),
                            ('issue_status_events', 'changed_by', 'changed_at'),
                            ('issue_attachments', 'uploaded_by', 'created_at')]:
        for row in tables[table]:
            if not string(row['issue_id']) or row['issue_id'] not in issues or not integer(row[who]) or row[who] not in users or not timestamp(row[when]): fail()
            if table == 'issue_comments' and not string(row['body']): fail()
            if table == 'issue_status_events':
                if row['to_status'] not in statuses or row['from_status'] not in statuses | {None}: fail()
            if table == 'issue_attachments':
                if row['comment_id'] is not None and (not integer(row['comment_id']) or row['comment_id'] not in comments or comments[row['comment_id']]['issue_id'] != row['issue_id']): fail()
                if not all(string(row[k]) for k in ['original_name', 'mime_type', 'archive_path', 'sha256']): fail()
                entry = row['archive_path']
                if not entry.startswith('attachments/') or entry not in files or entry in paths: fail()
                if type(row['size']) is not int or not 0 <= row['size'] <= MAX_ATTACHMENT: fail()
                if files[entry]['size'] != row['size'] or files[entry]['sha256'] != row['sha256']: fail()
                paths.add(entry)
    if set(files) != paths | {table + '.csv' for table in TABLES}: fail()


def restore_archive(host, archive_path, actor, identity_map=None):
    created_paths = []
    replaced_paths = []
    con = None
    root_fd = None
    try:
        with _LOCK, zipfile.ZipFile(archive_path) as archive:
            tables = _load(archive)  # Validate the entire archive before modifying anything.
            children = {}
            for table in ['issue_comments', 'issue_status_events', 'issue_attachments']:
                groups = children[table] = {}
                for row in tables[table]: groups.setdefault(row['issue_id'], []).append(row)
            con = host.db()
            _begin(host, con, writing=True)
            user_map = {}
            identity_map = identity_map or {}
            if not isinstance(identity_map, dict):
                raise ValueError('必须显式提供 source 用户ID到已确认本地账号ID的映射')
            for row in tables['users']:
                mapped = identity_map.get(str(row['id']))
                if type(mapped) is not int:
                    raise ValueError('缺少已确认的 source 用户ID映射：' + str(row['id']))
                local = con.execute('SELECT id FROM users WHERE id=? AND status=?', (mapped, 'active')).fetchone()
                if local is None:
                    raise ValueError('映射目标必须是已确认且正常的本地账号')
                user_map[row['id']] = local['id']
            normalize_title = lambda value: ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
            result = {'created': 0, 'updated': 0, 'skipped': 0, 'total': len(tables['issues'])}
            root = host.DATA_DIR / 'requirements_attachments'
            root_fd = _directory_fd(root, create=True)
            existing = [dict(row) for row in con.execute('SELECT id,title,description,author_id,case_id,updated_at FROM issues').fetchall()]
            by_id = {row['id']: row for row in existing}
            for table, field in [('issue_comments', 'created_at'), ('issue_status_events', 'changed_at'), ('issue_attachments', 'created_at')]:
                for row in con.execute(f'SELECT issue_id,MAX({field}) AS latest FROM {table} GROUP BY issue_id').fetchall():
                    if row['issue_id'] in by_id:
                        item = by_id[row['issue_id']]
                        item['updated_at'] = max(item['updated_at'], row['latest'])
            for issue in tables['issues']:
                source_id = issue['id']
                local = by_id.get(source_id)
                if local is not None:
                    raise ValueError('需求ID已存在；请人工核实来源与归属后处理，不自动覆盖')
                if local is None:
                    key = (normalize_title(issue['title']), issue['case_id'])
                    title_matches = [row for row in existing
                                     if (normalize_title(row['title']), row['case_id']) == key
                                     and row['author_id'] == user_map[issue['author_id']]
                                     and (row['description'].startswith(issue['description']) or issue['description'].startswith(row['description']))]
                    if title_matches:
                        raise ValueError('疑似重复需求；请人工核实来源与归属，不按标题自动合并')
                ident = local['id'] if local else source_id
                if local and local['updated_at'] > issue['updated_at']:
                    result['skipped'] += 1
                    continue
                data = dict(issue)
                data['id'] = ident
                data['author_id'] = user_map[data['author_id']]
                if local:
                    fields = [k for k in TABLES['issues'] if k != 'id']
                    con.execute('UPDATE issues SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?', [data[k] for k in fields] + [ident])
                    replaced_paths.extend(Path(x['stored_path']) for x in con.execute(
                        'SELECT stored_path FROM issue_attachments WHERE issue_id=?', (ident,)).fetchall())
                    for table in ['issue_attachments', 'issue_comments', 'issue_status_events']:
                        con.execute(f'DELETE FROM {table} WHERE issue_id=?', (ident,))
                    result['updated'] += 1
                else:
                    fields = TABLES['issues']
                    con.execute('INSERT INTO issues('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')', [data[k] for k in fields])
                    result['created'] += 1
                comment_map = {}
                for row in children['issue_comments'].get(source_id, []):
                    new = con.execute('INSERT INTO issue_comments(issue_id,author_id,body,created_at) VALUES(?,?,?,?) RETURNING id',
                                      (ident, user_map[row['author_id']], row['body'], row['created_at'])).fetchone()
                    comment_map[row['id']] = new['id']
                for row in children['issue_status_events'].get(source_id, []):
                    con.execute('INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)',
                                (ident, row['from_status'], row['to_status'], user_map[row['changed_by']], row['changed_at']))
                for row in children['issue_attachments'].get(source_id, []):
                    attachment_id = row['id']
                    if con.execute('SELECT 1 FROM issue_attachments WHERE id=?', (attachment_id,)).fetchone():
                        attachment_id = secrets.token_hex(16)
                    # Random storage path: never trust IDs or paths from the package;
                    # rollback can remove new files without harming the old version.
                    target = root / ('archive-' + secrets.token_hex(24))
                    descriptor = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                    created_paths.append(target.name)
                    with os.fdopen(descriptor, 'wb') as out, archive.open(row['archive_path']) as source:
                        digest = hashlib.sha256()
                        actual = 0
                        while chunk := source.read(1024 * 1024):
                            actual += len(chunk)
                            if actual > row['size']: raise ValueError('附件恢复大小超限')
                            digest.update(chunk)
                            out.write(chunk)
                        if actual != row['size'] or digest.hexdigest() != row['sha256']:
                            raise ValueError('附件恢复校验失败')
                    con.execute('INSERT INTO issue_attachments(id,issue_id,comment_id,uploaded_by,original_name,stored_path,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                (attachment_id, ident, comment_map.get(row['comment_id']), user_map[row['uploaded_by']], row['original_name'], str(target), row['mime_type'], row['size'], row['created_at']))
            host.audit(con, actor['id'], 'requirements_archive_import', details=json.dumps(result))
            con.commit()
            return result
    except Exception as failure:
        if con is not None: con.rollback()
        cleanup_failures = 0
        for name in created_paths:
            try:
                os.unlink(name, dir_fd=root_fd)
            except OSError:
                cleanup_failures += 1
        if cleanup_failures:
            raise RuntimeError('数据库已回滚，但有 %s 个新附件清理失败，需管理员核查隔离数据目录' % cleanup_failures) from failure
        raise
    finally:
        if root_fd is not None: os.close(root_fd)
        if con is not None: con.close()


def install_routes(host):
    def administrator(user=Depends(host.current_issue_user)):
        host._requirements_guard()
        if 'admin' not in host.roles_of(user) or dict(user).get('_requirements_key') or dict(user).get('_ops_token'):
            raise HTTPException(403, '仅本机登录管理员可导入导出全站需求，不接受 Key')
        return user

    @host.app.get('/issues/export/zip')
    def export(user=Depends(administrator)):
        try:
            name = export_archive(host, user)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(name, media_type='application/zip', filename='swtool-requirements.zip',
                            headers={'Cache-Control': 'private, no-store'},
                            background=BackgroundTask(lambda: Path(name).unlink(missing_ok=True)))

    @host.app.post('/issues/import/zip')
    async def restore(file: UploadFile = File(...), identity_map: str = Form('{}'), user=Depends(administrator)):
        try:
            with tempfile.TemporaryDirectory(prefix='swtool-requirements-import-') as directory:
                path = Path(directory) / 'input.zip'
                size = 0
                with path.open('wb') as out:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > LIMIT + 64 * 1024 * 1024: raise HTTPException(413, '归档不能超过 10 GiB 加 ZIP 开销')
                        out.write(chunk)
                return await run_in_threadpool(restore_archive, host, path, user, json.loads(identity_map))
        except (ValueError, TypeError, KeyError, OverflowError, OSError, zipfile.BadZipFile, RuntimeError, csv.Error) as exc:
            raise HTTPException(400, '归档导入失败：' + str(exc)) from exc
        finally:
            await file.close()
