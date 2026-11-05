# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Trusted administrator-installed source plugins, atomically activated without restart."""
from contextlib import contextmanager
import fcntl
import importlib.util
import ipaddress
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
import uuid
import zipfile
from io import BytesIO

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, UploadFile, File
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

LOG = logging.getLogger(__name__)
MAX_ZIP = 20 * 1024 * 1024
MAX_FILES = 500
MAX_EXPANDED = 60 * 1024 * 1024


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


def safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('插件路径无效')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or ':' in value:
        raise ValueError('插件路径越界')
    return path


class PluginRuntime:
    def __init__(self, host):
        self.host = host
        self.standalone = bool(os.getenv('SWTOOL_REQUIREMENTS_STANDALONE'))
        self.root = host.DATA_DIR / 'plugins' if self.standalone else host.ROOT / 'plugins' / '.installed'
        self.lock = threading.RLock()
        self.cache = {}

    @contextmanager
    def mutation(self):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".lock").open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try: yield
                finally: fcntl.flock(stream, fcntl.LOCK_UN)

    def state(self):
        try:
            state = json.loads((self.root / 'state.json').read_text())
            return {name: item for name, item in state.items() if not self.standalone or name == 'requirements-center'}
        except FileNotFoundError:
            return {}

    def save(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.root, prefix='.state-')
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.root / 'state.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def active(self, name):
        item = self.state().get(name)
        return item['active'] if item else None

    def view(self, name, item):
        manifest = item['manifest']
        return {**{k: manifest.get(k, '') for k in ('name', 'displayName', 'version', 'description')},
                'active': item['active'], 'installed': True, 'hotReload': True,
                'frontendUrl': f'/extensions/{name}/assets/{manifest["frontendEntry"]}' if manifest.get('frontendEntry') else None}

    def items(self, active_only=False):
        return [self.view(name, item) for name, item in self.state().items() if not active_only or item['active']]

    def prepare(self, directory, manifest, revision):
        application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        if self.standalone:
            if manifest.get('name') != 'requirements-center':
                raise ValueError('独立发行仅支持需求中心')
            router = APIRouter()
            router.routes.extend(route for route in self.host.app.routes
                                 if getattr(route, 'path', '').startswith(('/api/tickets', '/api/requirements/')))
            def authenticate(user=Depends(self.host.current_user)):
                return user
            application.include_router(router, dependencies=[Depends(authenticate)])
            return application
        if manifest.get('providerEntry'):
            entry, symbol = manifest['providerEntry'].split(':')
            module_name = '_swtool_plugin_' + revision
            spec = importlib.util.spec_from_file_location(module_name, directory / entry,
                                                         submodule_search_locations=[str(directory)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                router = getattr(module, symbol)
                if not isinstance(router, APIRouter):
                    raise ValueError('Python 入口必须导出 APIRouter')
                if router.on_startup or router.on_shutdown or type(router.lifespan_context).__name__ != '_DefaultLifespan':
                    raise ValueError('热加载插件不能使用 lifespan/startup/shutdown，请使用请求级依赖')
                def authenticate(request: Request, user=Depends(self.host.current_user)):
                    request.state.user = dict(user)
                application.include_router(router, dependencies=[Depends(authenticate)])
            except BaseException:
                for key in list(sys.modules):
                    if key == module_name or key.startswith(module_name + '.'):
                        sys.modules.pop(key, None)
                raise
        return application

    def install(self, payload):
        if self.standalone:
            try:
                with zipfile.ZipFile(BytesIO(payload)) as supplied, zipfile.ZipFile(BytesIO(build_requirements_plugin(self.host.ROOT))) as approved:
                    entries = supplied.infolist()
                    if len(payload) > MAX_ZIP or len(entries) != len(approved.infolist()) or {entry.filename for entry in entries} != set(approved.namelist()):
                        raise ValueError('只接受本发行提供的需求中心插件包')
                    for entry in entries:
                        expected = approved.getinfo(entry.filename)
                        if entry.file_size != expected.file_size or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError('插件成员不符合本发行白名单')
                        with supplied.open(entry) as stream:
                            content = stream.read(expected.file_size + 1)
                        if content != approved.read(expected):
                            raise ValueError('插件源码不符合本发行核验版本')
            except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
                raise HTTPException(400, '插件包核验失败：' + str(exc)[:180]) from exc
        with self.mutation():
            self.root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=self.root, prefix='.upload-'))
            destination = None
            try:
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    entries = archive.infolist()
                    if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_EXPANDED:
                        raise ValueError('插件最多 500 个文件，解压后不超过 60MB')
                    names = set()
                    for entry in entries:
                        path = safe_path(entry.filename)
                        normalized = str(path)
                        if normalized in names or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                            raise ValueError('插件包含重复路径、符号链接或加密文件')
                        names.add(normalized)
                        if entry.is_dir(): continue
                        target = staging / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(entry))
                manifest = json.loads((staging / 'plugin.json').read_text())
                if not isinstance(manifest, dict): raise ValueError('plugin.json 必须是对象')
                name = manifest.get('name', '')
                if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,63}', name):
                    raise ValueError('插件名称须为 2–64 位小写字母、数字和连字符')
                if not isinstance(manifest.get('version'), str) or not manifest['version'].strip():
                    raise ValueError('缺少插件版本')
                for field in ('displayName', 'description'):
                    if field in manifest and not isinstance(manifest[field], str): raise ValueError('插件描述必须是文本')
                if not manifest.get('providerEntry') and not manifest.get('frontendEntry'):
                    raise ValueError('至少需要一个 Python 或前端入口')
                if manifest.get('providerEntry'):
                    provider = manifest['providerEntry']
                    if not isinstance(provider, str) or provider.count(':') != 1: raise ValueError('Python 入口格式为 backend.py:router')
                    filename, symbol = provider.split(':')
                    if not filename.endswith('.py') or not symbol.isidentifier(): raise ValueError('Python 入口无效')
                    if not (staging / safe_path(filename)).is_file(): raise ValueError('Python 入口文件不存在')
                if manifest.get('frontendEntry') and not (staging / safe_path(manifest['frontendEntry'])).is_file():
                    raise ValueError('前端入口不存在')
                revision = uuid.uuid4().hex
                destination = self.root / name / revision
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(destination)
                application = self.prepare(destination, manifest, revision)
                state = self.state()
                item = {'manifest': manifest, 'revision': revision, 'active': True}
                state[name] = item
                self.save(state)
                self.cache[(name, revision)] = application
                return self.view(name, item)
            except (Exception, SystemExit) as exc:
                if destination: shutil.rmtree(destination, ignore_errors=True)
                LOG.warning('Plugin installation rejected: %s', type(exc).__name__)
                raise HTTPException(400, '插件安装失败，请检查包结构、清单和 Python 入口：' + str(exc)[:240]) from exc
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def toggle(self, name, active):
        with self.mutation():
            state = self.state()
            if name not in state: raise HTTPException(404, '插件不存在')
            if active:
                item = state[name]
                try:
                    application = self.prepare(self.root / name / item['revision'], item['manifest'], item['revision'])
                except (Exception, SystemExit) as exc:
                    raise HTTPException(400, '插件激活失败，请检查 Python 入口和依赖') from exc
                self.cache[(name, item['revision'])] = application
            state[name]['active'] = active
            self.save(state)
            return self.view(name, state[name])

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return
        application = None
        try:
            root_path = scope.get('root_path', '')
            relative = scope['path'][len(root_path):].lstrip('/')
            name, _, rest = relative.partition('/')
            if self.standalone and name != 'requirements-center':
                raise HTTPException(404, '独立发行仅提供需求中心')
            item = self.state().get(name)
            if not item or not item['active']: raise HTTPException(404, '插件未激活或不存在')
            directory = self.root / name / item['revision']
            if rest == 'api' or rest.startswith('api/'):
                key = (name, item['revision'])
                def load():
                    with self.lock:
                        if key not in self.cache: self.cache[key] = self.prepare(directory, item['manifest'], item['revision'])
                        return self.cache[key]
                application = await run_in_threadpool(load)
                child_scope = dict(scope, root_path=root_path + '/' + name + '/api')
            else:
                if not rest.startswith('assets/'): raise HTTPException(404, '插件路径不存在')
                path = directory / safe_path(rest[len('assets/'):])
                # Python sources are never static downloads; static assets must not contain secrets.
                if path.suffix.lower() not in {'.html', '.htm', '.js', '.css', '.json', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf', '.map', '.md'} or not path.is_file():
                    raise HTTPException(404, '资源不存在')
                response = FileResponse(path, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        except ValueError:
            response = JSONResponse({'detail': '路径无效'}, status_code=400)
        except Exception:
            LOG.exception('Plugin loading failed')
            response = JSONResponse({'detail': '插件加载失败，请管理员检查插件源码'}, status_code=503)
        if application is not None:
            await application(child_scope, receive, send)
        else:
            await response(scope, receive, send)


def install_routes(host):
    runtime = PluginRuntime(host)
    host.plugin_runtime = runtime
    def admin(user=Depends(host.current_user)):
        if 'admin' not in host.roles_of(user): raise HTTPException(403, '仅管理员可管理插件')
        return user

    @host.app.post('/admin/plugins/upload', status_code=201)
    async def upload(file: UploadFile = File(...), user=Depends(admin)):
        try:
            payload = await file.read(MAX_ZIP + 1)
            if len(payload) > MAX_ZIP: raise HTTPException(413, '插件 ZIP 不超过 20MB')
            result = await run_in_threadpool(runtime.install, payload)
            return result
        finally:
            await file.close()

    @host.app.get('/api/plugins')
    def available(user=Depends(host.current_user)):
        return {'items': runtime.items(active_only=True)}

    @host.app.get('/admin/plugins/requirements-center/package')
    def package(request: Request, user=Depends(admin)):
        payload = build_requirements_plugin(host.ROOT)
        import json
        con = host.db()
        try:
            host.audit(con, user['id'], 'requirements_zip_download', details=json.dumps({
                'kind': 'plugin', 'ip': _client_ip(request), 'bytes': len(payload)
            }))
            con.commit()
        finally:
            con.close()
        return Response(payload, media_type='application/zip', headers={
            'Content-Disposition': 'attachment; filename="requirements-center-plugin.zip"', 'Cache-Control': 'no-store'})

    host.app.mount('/extensions', runtime, name='extensions')


def build_requirements_plugin(root):
    """Portable plugin sources; business storage stays with the compatible host."""
    from export_source import build_source_archive
    root = Path(root)
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        manifest = json.loads((root / 'plugins/requirements-center/plugin.json').read_text())
        manifest.update(providerEntry='backend.py:router', frontendEntry='web/index.html')
        archive.writestr('plugin.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('backend.py', (root / 'plugins/requirements-center/backend.py').read_bytes())
        archive.writestr('cli_driver.py', (root / 'plugins/requirements-center/cli_driver.py').read_bytes())
        with zipfile.ZipFile(BytesIO(build_source_archive())) as source_archive:
            resources = [(name, source_archive.read(name)) for name in source_archive.namelist() if name.startswith('web/')]
        for relative, data in resources:
            if relative.endswith('.html'):
                base = '/extensions/requirements-center/assets/web/'
                text = data.decode().replace('href="/"', 'href="__PLUGIN_HOME__"')
                for attr in ('src', 'href'):
                    text = text.replace(attr+'="/', attr+'="'+base)
                data = text.replace("__PLUGIN_HOME__", base+"index.html").encode()
            archive.writestr(relative, data)
    return output.getvalue()
