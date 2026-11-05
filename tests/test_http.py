# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Real standalone uvicorn/PostgreSQL tests; requires a dedicated test database."""
import os
import importlib.util
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

import httpx
import psycopg
from psycopg import sql
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def service(tmp_path_factory):
    url = os.environ.get('SWTOOL_TEST_DATABASE_URL', '')
    if not url.startswith(('postgresql://', 'postgres://')):
        pytest.fail('SWTOOL_TEST_DATABASE_URL must explicitly name a dedicated PostgreSQL test database')
    parts = urlsplit(url)
    if 'test' not in parts.path.lower():
        pytest.fail('Dedicated database name must contain test; production databases are forbidden')
    query = dict(parse_qsl(parts.query))
    if 'options' in query: pytest.fail('Test URL options must not override isolated schema')
    schema = 'swtool_http_test_' + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    query['options'] = '-csearch_path=' + schema
    isolated = urlunsplit(parts._replace(query=urlencode(query)))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    folder = tmp_path_factory.mktemp('standalone-http')
    env = {**os.environ, 'SWTOOL_DATABASE_URL': isolated, 'SWTOOL_DATA_DIR': str(folder / 'data'),
           'SWTOOL_SECRET': secrets.token_urlsafe(48), 'PYTHONPATH': str(ROOT)}
    log = (folder / 'server.log').open('w+')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'standalone:app', '--host', '127.0.0.1', '--port', str(port)],
                               cwd=ROOT, env=env, stdout=log, stderr=log)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20, trust_env=False)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None: pytest.fail('Standalone uvicorn exited before readiness; inspect isolated server log')
            try:
                if client.get('/api/plugins/requirements-center').status_code == 200: break
            except httpx.TransportError: pass
            time.sleep(0.1)
        else: pytest.fail('Standalone uvicorn readiness timeout')
        yield client
    finally:
        client.close()
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        log.close()
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_admin_parent_order_and_driver_permissions(service):
    client = service
    password = secrets.token_urlsafe(20)
    first = client.post('/auth/register', json={'username': 'test-admin', 'password': password})
    assert first.status_code == 200
    assert 'admin' in first.json()['roles']
    login = client.post('/auth/login', json={'username': 'test-admin', 'password': password})
    assert login.status_code == 200
    admin = {'Authorization': 'Bearer ' + login.json()['access_token']}
    assert client.get('/api/requirements/identity', headers=admin).status_code == 200
    assert client.get('/api/requirements/driver', headers=admin).status_code == 200
    parent = client.post('/api/tickets', headers=admin, json={'title': 'Parent', 'description': 'test'} )
    assert parent.status_code == 201
    parent_id = parent.json()['id']
    child = client.post('/api/tickets', headers=admin, json={'title': 'Child', 'description': 'test', 'parent_ticket_id': parent_id})
    assert child.status_code == 201
    child_id = child.json()['id']
    assert child.json()['parent_ticket']['id'] == parent_id
    assert client.get('/api/tickets/' + parent_id, headers=admin).json()['child_tickets'][0]['id'] == child_id
    before = child.json()['updated_at']
    moved = client.patch('/api/tickets/' + child_id, headers=admin, json={'move_before': parent_id})
    assert moved.status_code == 200 and moved.json()['updated_at'] == before
    registered = client.post('/auth/register', json={'username': 'test-user', 'password': password})
    user_id = registered.json()['user_id']
    assert client.patch(f'/admin/users/{user_id}/status', headers=admin, json={'status': 'active'}).status_code == 200
    normal_login = client.post('/auth/login', json={'username': 'test-user', 'password': password})
    normal = {'Authorization': 'Bearer ' + normal_login.json()['access_token']}
    assert client.get('/api/tickets/' + parent_id, headers=normal).status_code in {403, 404}
    assert client.post('/api/tickets', headers=normal, json={'title': 'Hidden parent', 'parent_ticket_id': parent_id}).status_code == 404
    assert client.patch('/api/tickets/' + child_id, headers=normal, json={'move_before': parent_id}).status_code == 403
    assert client.get('/api/requirements/driver', headers=normal).status_code == 403
    assert client.post('/api/requirements/driver/start', headers=normal, json={}).status_code == 403
    readonly = client.post('/api/requirements/key/rotate', headers=normal, json={'scope': 'tickets:read'})
    assert readonly.status_code == 200
    read_key = {'Authorization': 'Bearer ' + readonly.json()['key']}
    assert client.get('/api/requirements/identity', headers=read_key).status_code == 200
    assert client.post('/api/tickets', headers=read_key, json={'title': 'Forbidden'}).status_code == 403
    assert client.get('/api/requirements/driver', headers=read_key).status_code in {401, 403}
    assert client.post('/api/requirements/driver/start', headers=read_key, json={}).status_code == 403
    write = client.post('/api/requirements/key/rotate', headers=normal, json={'scope': 'tickets:write'})
    write_key = {'Authorization': 'Bearer ' + write.json()['key']}
    assert client.patch('/api/tickets/' + child_id, headers=write_key, json={'status': 'resolved'}).status_code == 403
    assert client.get('/api/requirements/driver', headers=write_key).status_code in {401, 403}
    assert client.post('/api/requirements/driver/start', headers=write_key, json={}).status_code == 403


@pytest.mark.parametrize('field', ['status', 'from_status', 'to_status'])
def test_standalone_archive_pending_status_and_unknown_rejection(field):
    specification = importlib.util.spec_from_file_location(
        'standalone_archive_status_test', ROOT / 'server/requirements_archive.py')
    archive = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(archive)
    tables = {name: [] for name in archive.TABLES}
    tables['users'] = [{'id': 1, 'username': 'fixture-user', 'created_at': 1.0}]
    issue = {'id': 'fixture-ticket', 'title': 'Pending', 'description': '', 'labels': '[]',
             'case_id': None, 'status': 'pending', 'author_id': 1, 'created_at': 1.0,
             'updated_at': 1.0, 'expected_resolution_at': None, 'resolved_at': None,
             'reproduction_steps': '', 'expected_result': ''}
    event = {'id': 1, 'issue_id': issue['id'], 'from_status': 'pending',
             'to_status': 'pending', 'changed_by': 1, 'changed_at': 1.0}
    tables['issues'] = [issue]
    tables['issue_status_events'] = [event]
    files = {name + '.csv': {} for name in archive.TABLES}
    archive._validate(tables, files)
    (issue if field == 'status' else event)[field] = 'unknown-status'
    with pytest.raises(ValueError, match='CSV'):
        archive._validate(tables, files)

