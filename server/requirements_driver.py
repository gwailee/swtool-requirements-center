# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Creator-session-only PostgreSQL adapter for the isolated SSH CLI driver."""
from __future__ import annotations
import base64
from contextlib import contextmanager, closing
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException

_LOCK = threading.RLock()
_RUNNERS = {}
_CENTER = 'standalone'
_BOOT = uuid.uuid4().hex


def load_driver(root):
    name = '_requirements_standalone_cli_driver'
    if name not in sys.modules:
        path = Path(root) / 'plugins/requirements-center/cli_driver.py'
        if not path.exists():
            path = Path(__file__).resolve().parents[2] / 'cli_driver.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def probe_host_key(target):
    addresses = sorted({info[4][0] for info in socket.getaddrinfo(target['hostname'], target['port'], type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('SSH 目标必须解析为公网地址，禁止访问内网或本机')
    address = addresses[0]
    result = subprocess.run(['ssh-keyscan', '-T', '5', '-p', str(target['port']), address],
                            capture_output=True, timeout=10, check=False)
    if len(result.stdout) > 16384:
        raise ValueError('SSH 主机公钥响应超限')
    for line in result.stdout.decode('ascii', errors='ignore').splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] not in {'ssh-ed25519', 'ecdsa-sha2-nistp256', 'ssh-rsa'}:
            continue
        try:
            fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(fields[2], validate=True)).digest()).decode().rstrip('=')
        except ValueError:
            continue
        if fingerprint == target['host_fingerprint']:
            return {'address': address, 'algorithm': fields[1], 'publicKey': fields[2], 'fingerprint': fingerprint}
    raise ValueError('SSH 主机指纹不匹配，请通过可信渠道核实后重新添加目标')


class PostgresStore:
    def __init__(self, host, driver):
        self.host, self.driver = host, driver
        self.pending_config = None

    @contextmanager
    def connection(self):
        with closing(self.host.db()) as con:
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def get(self, requirement_id):
        with self.connection() as con:
            row = con.execute('SELECT * FROM requirements_driver_state WHERE requirement_id=?', (requirement_id,)).fetchone()
            return dict(row) if row else None

    def put(self, requirement_id, owner_id, policy, state, expected_version=None):
        with self.connection() as con:
            values = (owner_id, json.dumps(policy.__dict__), json.dumps(state), state['status'], time.time(), _BOOT)
            config = json.dumps(self.pending_config) if self.pending_config is not None else None
            if expected_version == 0:
                row = con.execute("INSERT INTO requirements_driver_state(requirement_id,owner_id,policy,state,status,updated,boot,version,config) VALUES(?,?,?,?,?,?,?,1,COALESCE(?,'{}')) ON CONFLICT DO NOTHING RETURNING version", (requirement_id, *values, config)).fetchone()
            else:
                row = con.execute('UPDATE requirements_driver_state SET owner_id=?,policy=?,state=?,status=?,updated=?,boot=?,config=COALESCE(?,config),version=version+1 WHERE requirement_id=? AND version=? RETURNING version', (*values, config, requirement_id, expected_version)).fetchone()
            if not row:
                raise self.driver.DriverError('version_conflict', '驱动状态已变化，请刷新')
            return row['version']

    def config(self, value=None):
        with self.connection() as con:
            if value is not None:
                con.execute('UPDATE requirements_driver_state SET config=? WHERE requirement_id=?', (json.dumps(value), _CENTER))
            else:
                row = con.execute('SELECT config FROM requirements_driver_state WHERE requirement_id=?', (_CENTER,)).fetchone()
                return json.loads(row['config']) if row else None

    def acquire(self):
        con = self.host.db()
        row = con.execute("SELECT pg_try_advisory_lock(hashtext('requirements-driver-standalone')) AS acquired").fetchone()
        if not row['acquired']:
            con.close()
            return None
        con.commit()
        return con

    def event(self, requirement_id, event):
        with self.connection() as con:
            con.execute('INSERT INTO requirements_driver_events VALUES(?,?,?,?) ON CONFLICT DO NOTHING', (event.get('event_id') or uuid.uuid4().hex, requirement_id, time.time(), json.dumps(event)))

    def events(self, requirement_id):
        with self.connection() as con:
            rows = con.execute('SELECT id,at,event FROM requirements_driver_events WHERE requirement_id=? ORDER BY at DESC,id DESC LIMIT 100', (requirement_id,)).fetchall()
            events = []
            for row in rows:
                event_id, at, event = (row['id'], row['at'], row['event']) if hasattr(row, 'keys') else row
                if isinstance(event, str):
                    event = json.loads(event)
                events.append(dict(event, storage_at=at, storage_event_id=event_id))
            return events[::-1]


class TicketConnector:
    def __init__(self, store, owner_id):
        self.store, self.owner_id = store, owner_id

    def list_tickets(self, requirement_id):
        with self.store.connection() as con:
            rows = con.execute("SELECT i.* FROM issues i LEFT JOIN requirements_driver_leases l ON l.issue_id=i.id WHERE i.status='open' AND COALESCE(l.quarantined,FALSE)=FALSE ORDER BY i.created_at,i.id LIMIT 200").fetchall()
            result = []
            for row in rows:
                labels = json.loads(row['labels'])
                if any(str(label).strip().lower() in {'待补充', '待补充信息', 'needs-info', 'needs_info'} for label in labels):
                    continue
                result.append(self.store.driver.Ticket(row['id'], row['status'], str(row['updated_at']), row['title'], row['description'], str(row['created_at'])))
            return result

    def claim(self, requirement_id, ticket, lease_id):
        with self.store.connection() as con:
            row = con.execute("SELECT updated_at,labels FROM issues WHERE id=? AND status='open' AND updated_at=? FOR UPDATE", (ticket.id, float(ticket.version))).fetchone()
            if not row:
                return None
            labels = json.loads(row['labels'])
            if any(str(label).strip().lower() in {'待补充', '待补充信息', 'needs-info', 'needs_info'} for label in labels):
                return None
            row = con.execute('INSERT INTO requirements_driver_leases(issue_id,lease_id,expires,quarantined) VALUES(?,?,?,FALSE) ON CONFLICT(issue_id) DO UPDATE SET lease_id=EXCLUDED.lease_id,expires=EXCLUDED.expires WHERE requirements_driver_leases.expires < ? AND requirements_driver_leases.quarantined=FALSE RETURNING issue_id', (ticket.id, lease_id, time.time()+90, time.time())).fetchone()
            return ticket if row else None

    def renew(self, requirement_id, ticket, lease_id):
        with self.store.connection() as con:
            result = con.execute("UPDATE requirements_driver_leases SET expires=? WHERE issue_id=? AND lease_id=? AND expires>? AND quarantined=FALSE AND EXISTS(SELECT 1 FROM issues WHERE id=? AND status='open' AND updated_at=?)", (time.time()+90, ticket.id, lease_id, time.time(), ticket.id, float(ticket.version)))
            return result.rowcount == 1

    def close(self, requirement_id, ticket, lease_id, result):
        with self.store.connection() as con:
            now = time.time()
            row = con.execute("UPDATE issues SET status='resolved',resolved_at=?,updated_at=? WHERE id=? AND status='open' AND updated_at=? AND EXISTS(SELECT 1 FROM requirements_driver_leases WHERE issue_id=? AND lease_id=? AND expires>? AND quarantined=FALSE) RETURNING id", (now, now, ticket.id, float(ticket.version), ticket.id, lease_id, now)).fetchone()
            if not row:
                return None
            con.execute('INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)', (ticket.id, 'open', 'resolved', self.owner_id, now))
            if getattr(result, 'execution_mode', None) == 'ssh-user':
                comment = 'CLI 工单完成回执已核验：' + result.summary[:1000]
            else:
                comment = 'CLI 验收通过并已推送：' + result.summary[:1000]
            con.execute('INSERT INTO issue_comments(issue_id,author_id,body,created_at) VALUES(?,?,?,?)', (ticket.id, self.owner_id, comment, now))
            return str(now)

    def release(self, requirement_id, ticket, lease_id):
        with self.store.connection() as con:
            con.execute('DELETE FROM requirements_driver_leases WHERE issue_id=? AND lease_id=? AND quarantined=FALSE', (ticket.id, lease_id))

    def quarantine(self, requirement_id, ticket, lease_id):
        with self.store.connection() as con:
            con.execute('UPDATE requirements_driver_leases SET quarantined=TRUE WHERE issue_id=? AND lease_id=?', (ticket.id, lease_id))

    def milestone(self, requirement_id, event_id, event):
        self.store.event(requirement_id, dict(event, event_id=event_id, milestone=True))


class Verified:
    def verify(self, policy, requirement_id, ticket, result):
        if (getattr(policy, 'tool', None) == 'swtool'
                and getattr(policy, 'target_kind', None) == 'ssh'
                and getattr(result, 'execution_mode', None) == 'ssh-user'):
            return result.exit_code == 0 and result.verified is True and result.pushed is False
        return result.exit_code == 0 and result.verified is True and result.pushed is True


def install_routes(host):
    from server.requirements_driver_ssh import SshDriverTransport, RunnerDiagnostic, ERRORS
    driver = load_driver(host.ROOT)
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'swtool:requirements-driver-ssh:v1\0'+host.SECRET).digest()))
    store = PostgresStore(host, driver)
    with store.connection() as con:
        con.execute('CREATE TABLE IF NOT EXISTS requirements_driver_ssh_targets(id TEXT PRIMARY KEY,owner_id BIGINT NOT NULL,name TEXT NOT NULL,hostname TEXT NOT NULL,port INTEGER NOT NULL,username TEXT NOT NULL,host_fingerprint TEXT NOT NULL,credential_kind TEXT NOT NULL,encrypted_credential TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS requirements_driver_center_owner(center_id TEXT PRIMARY KEY,owner_id BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS requirements_driver_state(requirement_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,version BIGINT NOT NULL,status TEXT NOT NULL,policy TEXT NOT NULL,state TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,boot TEXT NOT NULL,config TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS requirements_driver_events(id TEXT PRIMARY KEY,requirement_id TEXT NOT NULL,at DOUBLE PRECISION NOT NULL,event TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS requirements_driver_leases(issue_id TEXT PRIMARY KEY,lease_id TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL,quarantined BOOLEAN NOT NULL DEFAULT FALSE)')
        creator = con.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()
        if creator:
            con.execute('INSERT INTO requirements_driver_center_owner(center_id,owner_id,created_at) VALUES(?,?,?) ON CONFLICT(center_id) DO NOTHING', (_CENTER,creator['id'],time.time()))

    def owner(user=Depends(host.current_user)):
        with store.connection() as con:
            creator = con.execute('SELECT owner_id FROM requirements_driver_center_owner WHERE center_id=?', (_CENTER,)).fetchone()
        if user.get('_ops_token') or user.get('role') == 'guest' or not creator or (user.get('role') not in {'admin', 'superadmin'} and user['id'] != creator['owner_id']):
            raise HTTPException(403, '只有需求中心创建者登录会话可驱动 CLI')
        return user

    def target(ident, user):
        with store.connection() as con:
            row = con.execute('SELECT * FROM requirements_driver_ssh_targets WHERE id=? AND owner_id=?', (ident, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'SSH 目标不存在，请自行添加 SSH')
        return dict(row)

    def transport(config, user):
        if config.get('target_kind', 'ssh') == 'local':
            if os.environ.get('SWTOOL_REQUIREMENTS_LOCAL_DRIVER_ENABLED') != '1':
                raise ValueError('管理员尚未启用本机隔离 runner')
            from server.requirements_driver_local import LocalDriverTransport
            config['owner_id'] = str(user['id'])
            config['target_fingerprint'] = 'local:' + str(os.uname().nodename)
            return LocalDriverTransport(config)
        if config.get('target_kind', 'ssh') != 'ssh':
            raise ValueError('执行目标类型无效')
        saved = target(str(config.get('ssh_target_id') or ''), user)
        key = config.get('host_key')
        if not isinstance(key, dict) or config.get('host_key_confirmed') is not True or probe_host_key(saved) != key:
            raise ValueError('请重新检测并明确确认 SSH 主机指纹')
        credential = cipher.decrypt(saved['encrypted_credential'].encode()).decode()
        config['owner_id'] = str(user['id'])
        config['target_fingerprint'] = str(saved['host_fingerprint'])
        saved_target = SimpleNamespace(owner_id=str(saved['owner_id']), session_id=str(saved['id']),
                                       target=SimpleNamespace(host=saved['hostname'], port=saved['port'], username=saved['username']))
        return SshDriverTransport(saved_target, credential if saved['credential_kind']=='password' else '', config, key,
                                  private_key=credential if saved['credential_kind']=='private_key' else None)

    @host.app.get('/api/requirements/driver')
    def status(user=Depends(owner)):
        with store.connection() as con:
            rows = con.execute('SELECT id,name,hostname,port,username,host_fingerprint,credential_kind FROM requirements_driver_ssh_targets WHERE owner_id=? ORDER BY created_at,id', (user['id'],)).fetchall()
        row = store.get(_CENTER)
        state = dict(json.loads(row['state']), version=row['version']) if row else None
        # The durable config is authoritative.  A restarted service reports the
        # persisted state; resume performs a server-side status query on the same
        # tmux session before attaching, so the browser is never used as state.
        targets = [dict(row) for row in rows]
        if os.environ.get('SWTOOL_REQUIREMENTS_LOCAL_DRIVER_ENABLED') == '1':
            targets.insert(0, {'id': 'local', 'kind': 'local', 'name': '本机隔离 runner（独立中心服务所在机器）'})
        return {'driver': state, 'events': store.events(_CENTER), 'targets': targets, 'config': store.config(), 'default_completion_prompt_template': driver.DEFAULT_COMPLETION_PROMPT_TEMPLATE, 'default_startup_prompt_template': driver.DEFAULT_STARTUP_PROMPT_TEMPLATE,
                'billing': '独立需求中心本机与 SSH 执行均不收取 SWTool SSH 费用。', 'reason': '仅处理待解决工单；每 60 秒检测。本机指独立中心服务所在机器，须管理员显式启用隔离 runner；未配置时请自行添加 SSH。'}

    @host.app.get('/api/requirements/driver/logs')
    def logs(cursor: int = 0, run_id: str = '', user=Depends(owner)):
        if cursor < 0:
            raise HTTPException(400, '日志游标无效')
        config = dict(store.config() or {})
        ticket = config.get('current_ticket_id') or config.get('last_ticket_id')
        if not ticket:
            return {'run_id': '', 'cursor': 0, 'text': '', 'reset': True, 'has_more': False}
        if not config.get('current_ticket_id'):
            for name in ('run_generation', 'run_session_id', 'owner_id', 'target_fingerprint'):
                if config.get('last_' + name) is not None:
                    config[name] = config['last_' + name]
        try:
            return transport(config, user).logs(_CENTER, config['run_session_id'], ticket, cursor, run_id)
        except Exception:
            raise HTTPException(502, '运行日志暂不可用，请稍后重试') from None

    @host.app.post('/api/requirements/driver/interject')
    def interject(body: dict, user=Depends(owner)):
        interjection_id, text = body.get('interjection_id'), body.get('text')
        if (not isinstance(interjection_id, str) or not re.fullmatch(r'[0-9a-f]{32}', interjection_id)
                or not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8192):
            raise HTTPException(400, '插话内容或请求编号无效')
        config = dict(store.config() or {})
        row = store.get(_CENTER)
        ticket = str(config.get('current_ticket_id') or '')
        run_session_id = str(config.get('run_session_id') or '')
        if (not row or str(row['owner_id']) != str(user['id']) or str(config.get('owner_id') or '') != str(user['id'])
                or not ticket or not run_session_id or row.get('status') not in {'running', 'retry_wait'}):
            raise HTTPException(409, '当前没有可插话的活动工单会话')
        try:
            return transport(config, user).interject(_CENTER, run_session_id, ticket, interjection_id, text)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code in {'interjection_invalid', 'interjection_queue_full', 'interjection_unavailable'}:
                raise HTTPException(409, detail={'code': code, 'error': str(exc)}) from None
            raise HTTPException(502, '插话未能送达当前会话，请稍后重试') from None

    @host.app.post('/api/requirements/driver/ssh-targets')
    def add_target(body: dict, user=Depends(owner)):
        fields = {key:str(body.get(key) or '').strip() for key in ('name','hostname','username','host_fingerprint','credential_kind')}
        credential = str(body.get('credential') or '')
        try:
            port = int(body.get('port',22))
        except (ValueError,TypeError):
            port = 0
        if (not fields['name'] or len(fields['name'])>80 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}',fields['hostname'])
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',fields['username']) or not 1<=port<=65535
            or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}',fields['host_fingerprint'])
            or fields['credential_kind'] not in {'password','private_key'} or not 0<len(credential)<=65536):
            raise HTTPException(400,'SSH 主机、端口、用户名、SHA256 指纹或凭据无效')
        with store.connection() as con:
            ident = uuid.uuid4().hex
            con.execute('INSERT INTO requirements_driver_ssh_targets VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,user['id'],fields['name'],fields['hostname'],port,fields['username'],fields['host_fingerprint'],fields['credential_kind'],cipher.encrypt(credential.encode()).decode(),time.time()))
        return {'id':ident}

    @host.app.delete('/api/requirements/driver/ssh-targets/{target_id}')
    def delete_target(target_id: str, user=Depends(owner)):
        with _LOCK:
            config, row = store.config(), store.get(_CENTER)
            if row and config.get('ssh_target_id') == target_id:
                raise HTTPException(409,'该目标仍绑定持久驱动状态，不能删除停止或回撤所需凭据')
            with store.connection() as con:
                con.execute('DELETE FROM requirements_driver_ssh_targets WHERE id=? AND owner_id=?',(target_id,user['id']))
        return {'ok':True}

    @host.app.post('/api/requirements/driver/host-key')
    def host_key(body: dict, user=Depends(owner)):
        try:
            return {'host_key':probe_host_key(target(str(body.get('ssh_target_id') or ''),user))}
        except (ValueError,OSError,subprocess.TimeoutExpired):
            raise HTTPException(400,'主机公钥检测失败或与已保存指纹不匹配') from None

    @host.app.post('/api/requirements/driver/{action}')
    def action(action: str, body: dict, user=Depends(owner)):
        if action not in {'start','stop','resume','rollback'}:
            raise HTTPException(404,'操作不存在')
        if action == 'start':
            workspace = body.get('workspace', '')
            if not isinstance(workspace, str) or (workspace and not workspace.startswith('/')) or any(char in workspace for char in ('\0', '\r', '\n')):
                raise HTTPException(400, detail={'code': 'workspace_invalid', 'error': '项目目录须为空或远端绝对路径，且不能包含换行或空字符'})
        actor = str(user['id'])
        try:
            with _LOCK:
                row = store.get(_CENTER)
                saved_config = store.config()
                if action in {'stop','rollback'}:
                    if not row:
                        return {'driver':{'status':'stopped'}}
                    supervisor = _RUNNERS.get(_CENTER)
                    remote = supervisor.transport if supervisor else transport(saved_config,user)
                    if action == 'rollback':
                        if row['status'] != 'stopped' or body.get('confirmed') is not True:
                            raise ValueError('请先停止驱动并明确确认回撤与 expected_head')
                        rollback_lock = store.acquire()
                        if rollback_lock is None:
                            raise driver.DriverError('driver_locked','驱动尚未完全停止，请等待工作线程退出')
                        try:
                            receipt = remote.rollback(_CENTER,str(body.get('commit') or ''),str(body.get('expected_head') or ''))
                            store.event(_CENTER,{'type':'rollback','actor':actor,'commit':receipt.get('commit'),'reverted':receipt.get('reverted')})
                            return {'rollback':receipt}
                        finally:
                            rollback_lock.close()
                    supervisor = supervisor or driver.ContinuousSupervisor(store,TicketConnector(store,user['id']),remote,Verified())
                    return {'driver':supervisor.stop(_CENTER,actor)}
                session_lock = store.acquire()
                if session_lock is None:
                    raise driver.DriverError('driver_locked','其他服务实例仍在工作，请先停止并等待退出')
                lock_transferred = False
                try:
                    row = store.get(_CENTER)
                    saved_config = store.config()
                    if action == 'resume' and row and row['boot'] != _BOOT and row['status'] in {'running','retry_wait'}:
                        old_config = dict(saved_config or {})
                        if not old_config:
                            raise driver.DriverError('paused_restart','旧执行代次配置缺失，需人工检查')
                        old_remote = transport(old_config,user)
                        try:
                            current_ticket_id = str(old_config.get('current_ticket_id') or '')
                            if current_ticket_id:
                                status_fn = getattr(old_remote, 'status', None)
                                if not callable(status_fn) or not old_config.get('run_session_id'):
                                    raise RuntimeError('session_identity_missing')
                                remote_status = status_fn(_CENTER, old_config['run_session_id'], current_ticket_id)
                                if remote_status.get('active') is not True and remote_status.get('resumable') is not True and remote_status.get('status') != 'completed':
                                    raise RuntimeError('remote_session_gone')
                        except Exception as exc:
                            old_state = json.loads(row['state'])
                            old_state.update(status='paused_error',last_error_code='termination_unconfirmed')
                            store.put(_CENTER,row['owner_id'],driver.DriverPolicy(**json.loads(row['policy'])),old_state,int(row['version']))
                            raise driver.DriverError('termination_unconfirmed','旧执行代次尚未确认停止，禁止恢复') from exc
                        old_state = json.loads(row['state'])
                        old_state.update(status='paused_restart',last_error_code=None)
                        store.put(_CENTER,row['owner_id'],driver.DriverPolicy(**json.loads(row['policy'])),old_state,int(row['version']))
                        row = store.get(_CENTER)
                    config = {key:body[key] for key in ('tool','target_kind','ssh_target_id','workspace','host_key','host_key_confirmed','push_authorized','close_authorized','max_runtime_seconds','startup_prompt_template','completion_prompt_template') if key in body} if action=='start' else dict(saved_config or {})
                    if action == 'start':
                        config.update({key: value for key, value in (saved_config or {}).items() if key.startswith('last_')})
                    if action == 'resume' and 'max_runtime_seconds' in body:
                        config['max_runtime_seconds'] = body['max_runtime_seconds']
                    if action == 'resume' and 'completion_prompt_template' in body:
                        config['completion_prompt_template'] = body['completion_prompt_template']
                    if action == 'resume' and 'startup_prompt_template' in body:
                        config['startup_prompt_template'] = body['startup_prompt_template']
                    config.setdefault('completion_prompt_template', driver.DEFAULT_COMPLETION_PROMPT_TEMPLATE)
                    config.setdefault('startup_prompt_template', driver.DEFAULT_STARTUP_PROMPT_TEMPLATE)
                    if config.get('push_authorized') is not True or config.get('close_authorized') is not True:
                        raise ValueError('必须由创建者明确授权 Git push 和验收后关闭工单')
                    if action == 'start' or not config.get('run_generation'):
                        config['run_generation'] = uuid.uuid4().hex
                    if action == 'start' or not config.get('run_session_id'):
                        config['run_session_id'] = uuid.uuid4().hex
                    config['owner_id'] = actor
                    policy = driver.DriverPolicy(actor,str(config.get('tool') or ''),str(config.get('ssh_target_id') or ''),swtool_host=False,
                                                 target_kind=str(config.get('target_kind') or 'ssh'),max_runtime_seconds=config.get('max_runtime_seconds', 0),
                                                 startup_prompt_template=config['startup_prompt_template'],
                                                 completion_prompt_template=config['completion_prompt_template'])
                    driver.validate_policy(policy, actor)
                    remote = transport(config,user)
                    remote.preflight(_CENTER)
                    supervisor = driver.ContinuousSupervisor(store,TicketConnector(store,user['id']),remote,Verified())
                    try:
                        store.pending_config = config
                        result = supervisor.enable(_CENTER,policy,actor) if action=='start' else supervisor.resume(_CENTER,actor,policy)
                    finally:
                        store.pending_config = None
                    _RUNNERS[_CENTER] = supervisor
                    def run():
                        try:
                            supervisor.run(_CENTER)
                        except Exception:
                            current = store.get(_CENTER)
                            if current and current['status'] in {'running','retry_wait'}:
                                state = json.loads(current['state'])
                                state.update(status='paused_error', last_error_code='driver_internal_error')
                                try:
                                    store.put(_CENTER,actor,policy,state,current['version'])
                                except driver.DriverError:
                                    pass
                        finally:
                            session_lock.close()
                            with _LOCK:
                                if _RUNNERS.get(_CENTER) is supervisor:
                                    _RUNNERS.pop(_CENTER,None)
                    try:
                        threading.Thread(target=run,daemon=True,name='requirements-standalone-driver').start()
                    except Exception:
                        if _RUNNERS.get(_CENTER) is supervisor:
                            _RUNNERS.pop(_CENTER,None)
                        raise
                    lock_transferred = True
                    return {'driver':result}
                finally:
                    if not lock_transferred:
                        session_lock.close()
        except driver.DriverError as exc:
            raise HTTPException(409,detail={'code':exc.code,'error':exc.message}) from None
        except RuntimeError as exc:
            if isinstance(exc, RunnerDiagnostic) and exc.code in ERRORS:
                detail = {'code': exc.code, 'error': ERRORS[exc.code][1]}
                if exc.code in {'cli_api_disconnected', 'cli_api_process_exited'} and exc.detail:
                    detail['detail'] = exc.detail
                raise HTTPException(503, detail=detail) from None
            if 'runner_isolation_unavailable' in str(exc) or 'runner_policy_rejected' in str(exc):
                raise HTTPException(503,'远端受信隔离 runner 或 CLI 未预置，不能启动；请管理员完成部署后重试') from None
            raise HTTPException(503,'远端执行器未确认安全状态，不能继续；请检查远端状态后人工恢复') from None
        except (ValueError,RuntimeError,OSError,subprocess.TimeoutExpired):
            raise HTTPException(400,'驱动操作失败：请核实主机指纹、受信 runner 部署和远端状态后重试') from None
