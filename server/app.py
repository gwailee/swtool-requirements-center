# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""SWTool（SWTool）browser/server backend (minimal production-oriented skeleton).

The server keeps source cases server-side. Clients download frame metadata and
return annotation deltas only; uploads are streamed to disk and never loaded in
memory.
"""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import shlex
import shutil
import tempfile
import threading
import subprocess
import sys
import time
import zipfile
import zlib
import stat
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path
from typing import Annotated

from cryptography.fernet import Fernet, InvalidToken

try:
    import cv2
except ImportError:  # Video probing is optional; image-only cases still work.
    cv2 = None

try:
    import pydicom
    from pydicom.errors import InvalidDicomError
except ImportError:  # CT support is enabled when the optional decoder is installed.
    pydicom = None
InvalidDicomError = Exception

try:
    import SimpleITK as sitk
except ImportError:  # Required only for oblique CT patient-axis reconstruction.
    sitk = None

try:
    import nibabel as nib
except ImportError:
    nib = None

_DICOM_SCAN_CACHE: dict[str, tuple[float, tuple, list[dict]]] = {}
_DICOM_SCAN_CACHE_LOCK = threading.RLock()
_CT_MPR_CACHE: dict[str, tuple[float, tuple, object, dict]] = {}
_NIFTI_LABEL_MPR_CACHE: dict[str, tuple[float, tuple, object]] = {}
_CT_MPR_CACHE_LOCK = threading.RLock()
_NIFTI_SOURCE_CACHE = {}
_NIFTI_SOURCE_LOCK = threading.RLock()
_CT_MPR_BUILDING: dict[str, threading.Event] = {}
def _env(name: str, legacy_name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else os.getenv(legacy_name, default)


CT_MPR_CACHE_TTL_SECONDS = max(5, int(_env("SWTOOL_CT_MPR_CACHE_TTL_SECONDS", "WOMANSEG_CT_MPR_CACHE_TTL_SECONDS", "45")))
CT_MPR_CACHE_ENTRIES = max(1, int(_env("SWTOOL_CT_MPR_CACHE_ENTRIES", "WOMANSEG_CT_MPR_CACHE_ENTRIES", "1")))
CT_MPR_MAX_VOXELS = max(1_000_000, int(_env("SWTOOL_CT_MPR_MAX_VOXELS", "WOMANSEG_CT_MPR_MAX_VOXELS", "64000000")))
LOGGER = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

import psycopg
from psycopg import IntegrityError as PsycopgIntegrityError

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT


def _script_path(path: Path) -> Path:
    if path.is_file() or path.suffix != ".py":
        return path
    compiled = path.with_suffix(".pyc")
    return compiled if compiled.is_file() else path


DATA_DIR = Path(_env("SWTOOL_DATA_DIR", "WOMANSEG_DATA_DIR", str(ROOT / "server_data")))
CASES_DIR = Path(_env("SWTOOL_CASES_DIR", "WOMANSEG_CASES_DIR", str(DATA_DIR / "cases")))
ANNOTATION_DRAFTS_DIR = Path(_env("SWTOOL_ANNOTATION_DRAFTS_DIR", "WOMANSEG_ANNOTATION_DRAFTS_DIR", str(DATA_DIR / "annotation_drafts")))
DB_URL = _env("SWTOOL_DATABASE_URL", "WOMANSEG_DATABASE_URL").strip()
if not DB_URL.startswith(("postgresql://", "postgres://")):
    raise RuntimeError("必须配置 SWTOOL_DATABASE_URL PostgreSQL 连接，禁止本地数据库回退")
SECRET = _env("SWTOOL_SECRET", "WOMANSEG_SECRET").encode()
if len(SECRET) < 32 or SECRET == b"change-me-in-production":
    raise RuntimeError("必须配置至少32字节的随机 SWTOOL_SECRET，或通过 standalone 入口持久化生成")
OPS_TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(b"swtool:ops-token-encryption:v1\0" + SECRET).digest()
)
app = FastAPI(title="SWTool | SWTool", version="1.0")
WEB_DIR = ROOT / "web"
MAX_CASE_UPLOAD_BYTES = int(_env("SWTOOL_MAX_CASE_UPLOAD_BYTES", "WOMANSEG_MAX_CASE_UPLOAD_BYTES", str(40 * 1024 * 1024 * 1024)))
MAX_CASE_ARCHIVE_ENTRIES = int(_env("SWTOOL_MAX_CASE_ARCHIVE_ENTRIES", "WOMANSEG_MAX_CASE_ARCHIVE_ENTRIES", "200000"))
VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
FRAME_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
DICOM_SUFFIXES = {".dcm", ".ima", ".dicom"}
DANGEROUS_UPLOAD_SUFFIXES = {".exe", ".msi", ".bat", ".cmd", ".com", ".sh", ".bash", ".ps1", ".py", ".js", ".php", ".jar", ".dll", ".so"}

@app.middleware("http")
async def api_prefix(request, call_next):
    # Keep handlers readable while accepting the documented /api/v1 prefix.
    path = request.scope.get("path", "")
    if path.startswith("/api/v1"):
        request.scope["path"] = path[7:] or "/"
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    return response


class HybridRow(dict):
    """Row supporting both keyed and positional access patterns."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _pg_row_factory(cursor):
    """psycopg3 row_factory: build a row maker once per cursor."""
    # DDL/DML cursors have no result description; psycopg still invokes the
    # row factory in some paths, so return an empty row maker for those cursors.
    if cursor.description is None:
        return lambda values: HybridRow()
    names = [desc.name for desc in cursor.description]

    def make_row(values):
        return HybridRow(zip(names, values))

    return make_row


class PgConnection:
    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _sql(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        cur = self.raw.cursor(row_factory=_pg_row_factory)
        cur.execute(self._sql(sql), params)
        return cur

    def executescript(self, script):
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


DBIntegrityError = (PsycopgIntegrityError,)


_DB_SCHEMA_LOCK = threading.Lock()
_DB_INITIALIZED: set[tuple] = set()


def _database_identity(con, postgres: bool) -> tuple:
    return (os.getpid(), "postgres", DB_URL)


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = PgConnection(psycopg.connect(DB_URL))
    try:
        with _DB_SCHEMA_LOCK:
            identity = _database_identity(con, True)
            if identity not in _DB_INITIALIZED:
                _initialize_database_schema(con, True)
                _DB_INITIALIZED.add(identity)
        return con
    except Exception:
        try:
            con.rollback()
        finally:
            con.close()
        raise


def _initialize_database_schema(con, postgres: bool):
    from server.requirements_federation import SCHEMA as requirements_schema
    con.executescript(requirements_schema)
    if postgres:
        con.execute("ALTER TABLE requirements_keys ADD COLUMN IF NOT EXISTS scope TEXT DEFAULT 'tickets:read' NOT NULL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL, roles TEXT, custom_categories TEXT, category_admin BOOLEAN NOT NULL DEFAULT FALSE, training_permission BOOLEAN NOT NULL DEFAULT FALSE, status TEXT NOT NULL DEFAULT 'active', created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS global_categories(id SMALLINT PRIMARY KEY, categories TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS reviewer_map(annotator_id BIGINT PRIMARY KEY, reviewer_id BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, title TEXT, metadata TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'available', assigned_reviewer_id BIGINT, created_at DOUBLE PRECISION NOT NULL,
      assigned_annotator_id BIGINT);
    CREATE TABLE IF NOT EXISTS leases(case_id TEXT PRIMARY KEY, annotator_id BIGINT NOT NULL,
      token_hash TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS submissions(id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL, annotator_id BIGINT NOT NULL,
      reviewer_id BIGINT, version TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
      status TEXT NOT NULL, UNIQUE(case_id, version));
    CREATE TABLE IF NOT EXISTS audit_events(id BIGSERIAL PRIMARY KEY, user_id BIGINT, action TEXT NOT NULL,
      case_id TEXT, details TEXT, created_at DOUBLE PRECISION NOT NULL);
    CREATE INDEX IF NOT EXISTS audit_events_requirements_zip_download ON audit_events(id DESC)
      WHERE action='requirements_zip_download';
    CREATE TABLE IF NOT EXISTS upload_sessions(id TEXT PRIMARY KEY, case_id TEXT UNIQUE NOT NULL,
      admin_id BIGINT NOT NULL, status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
      assigned_annotator_id BIGINT, assigned_reviewer_id BIGINT);
    CREATE TABLE IF NOT EXISTS upload_files(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      name TEXT NOT NULL, size BIGINT NOT NULL, sha256 TEXT NOT NULL, chunk_size BIGINT NOT NULL,
      received BIGINT NOT NULL DEFAULT 0, path TEXT NOT NULL, UNIQUE(session_id,name));
    CREATE TABLE IF NOT EXISTS upload_chunks(file_id TEXT NOT NULL, "offset" BIGINT NOT NULL,
      size BIGINT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(file_id,"offset"));
    CREATE TABLE IF NOT EXISTS issues(id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,labels TEXT NOT NULL,case_id TEXT,status TEXT NOT NULL,author_id BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,expected_resolution_at DOUBLE PRECISION,resolved_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS issue_comments(id BIGSERIAL PRIMARY KEY,issue_id TEXT NOT NULL,author_id BIGINT NOT NULL,body TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL,edited_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS issue_status_events(id BIGSERIAL PRIMARY KEY,issue_id TEXT NOT NULL,from_status TEXT,to_status TEXT NOT NULL,changed_by BIGINT NOT NULL,changed_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS issue_attachments(id TEXT PRIMARY KEY,issue_id TEXT NOT NULL,comment_id BIGINT,uploaded_by BIGINT NOT NULL,original_name TEXT NOT NULL,stored_path TEXT NOT NULL,mime_type TEXT NOT NULL,size BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS dataset_versions(id TEXT PRIMARY KEY, source_submission_ids TEXT NOT NULL, root_path TEXT NOT NULL, status TEXT NOT NULL, sha256 TEXT, export_report TEXT, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS training_runs(id TEXT PRIMARY KEY, dataset_version TEXT NOT NULL, config_json TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, output_path TEXT NOT NULL, metrics_path TEXT, error TEXT, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS ctspine_training_nodes(id TEXT PRIMARY KEY,name TEXT NOT NULL,host TEXT NOT NULL,port INTEGER NOT NULL,username TEXT NOT NULL,key_path TEXT NOT NULL,host_key TEXT NOT NULL,work_dir TEXT NOT NULL,python_path TEXT NOT NULL,share_local TEXT NOT NULL,share_remote TEXT NOT NULL,enabled BOOLEAN NOT NULL DEFAULT TRUE,status TEXT NOT NULL DEFAULT 'unchecked',status_json TEXT NOT NULL DEFAULT '{}',updated_at DOUBLE PRECISION NOT NULL,updated_by BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS model_versions(id TEXT PRIMARY KEY, training_run TEXT NOT NULL, version TEXT NOT NULL, checkpoint_path TEXT NOT NULL, checkpoint_sha256 TEXT NOT NULL, status TEXT NOT NULL, metrics_json TEXT, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS ctspine_weight_registry(id TEXT PRIMARY KEY, name TEXT NOT NULL, source_url TEXT NOT NULL, local_path TEXT NOT NULL, sha256 TEXT NOT NULL, license TEXT NOT NULL, label_map_json TEXT NOT NULL, model_family TEXT NOT NULL, status TEXT NOT NULL, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS inference_runs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, model_version TEXT, prompt_source TEXT NOT NULL, status TEXT NOT NULL, output_path TEXT, error TEXT, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS evaluation_runs(id TEXT PRIMARY KEY, model_version TEXT NOT NULL, dataset_version TEXT NOT NULL, status TEXT NOT NULL, report_json TEXT, error TEXT, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS predictor_jobs(id TEXT PRIMARY KEY, model_name TEXT NOT NULL, case_id TEXT, dataset_id TEXT, input_path TEXT NOT NULL, report_path TEXT, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, metrics_json TEXT, error TEXT, cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS queue_preferences(user_id BIGINT PRIMARY KEY, queue_json TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
    CREATE TABLE IF NOT EXISTS annotation_projects(case_id TEXT NOT NULL,segment_id TEXT NOT NULL,payload_json TEXT NOT NULL,progress_json TEXT NOT NULL,updated_by BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL,PRIMARY KEY(case_id,segment_id));
    CREATE TABLE IF NOT EXISTS ops_tokens(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, encrypted_secret TEXT, created_by BIGINT NOT NULL, created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL, revoked_at DOUBLE PRECISION);
    CREATE TABLE IF NOT EXISTS plugin_settings(name TEXT PRIMARY KEY, active BOOLEAN NOT NULL DEFAULT TRUE, updated_at DOUBLE PRECISION NOT NULL, updated_by BIGINT);
    """)
    legacy_versions = con.execute("SELECT to_regclass('public.annotation_project_versions') AS name").fetchone()["name"]
    if legacy_versions:
        rows = con.execute("SELECT DISTINCT ON (case_id,segment_id) case_id,segment_id,payload_json,progress_json,created_by,created_at FROM annotation_project_versions ORDER BY case_id,segment_id,created_at DESC,id DESC").fetchall()
        for row in rows:
            con.execute("INSERT INTO annotation_projects(case_id,segment_id,payload_json,progress_json,updated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(case_id,segment_id) DO NOTHING", (row['case_id'],row['segment_id'],row['payload_json'],row['progress_json'],row['created_by'],row['created_at']))
        con.execute("DROP TABLE annotation_project_versions")
    con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
    con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS category_admin BOOLEAN NOT NULL DEFAULT FALSE")
    con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS training_permission BOOLEAN NOT NULL DEFAULT FALSE")
    con.execute("ALTER TABLE dataset_versions ADD COLUMN IF NOT EXISTS export_report TEXT")
    con.execute("ALTER TABLE ops_tokens ADD COLUMN IF NOT EXISTS encrypted_secret TEXT")
    con.execute("ALTER TABLE predictor_jobs ADD COLUMN IF NOT EXISTS guest_owner TEXT")
    con.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS expected_resolution_at DOUBLE PRECISION")
    con.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved_at DOUBLE PRECISION")
    con.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS manual_order BIGINT NULL")
    con.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS parent_ticket_id TEXT NULL")
    con.execute("ALTER TABLE issue_comments ADD COLUMN IF NOT EXISTS edited_at DOUBLE PRECISION")
    for column in ("reproduction_steps", "expected_result"):
        con.execute(f"ALTER TABLE issues ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
    _merge_closed_issue_status(con)
    con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) SELECT i.id,NULL,i.status,i.author_id,? FROM issues i WHERE NOT EXISTS (SELECT 1 FROM issue_status_events e WHERE e.issue_id=i.id)", (time.time(),))
    con.commit()
    return con

def _merge_closed_issue_status(con):
    """Retire the legacy closed state without changing original event times."""
    con.execute("UPDATE issues SET status='resolved',resolved_at=COALESCE(resolved_at,updated_at) WHERE status='closed'")
    con.execute("UPDATE issue_status_events SET from_status='resolved' WHERE from_status='closed'")
    con.execute("UPDATE issue_status_events SET to_status='resolved' WHERE to_status='closed'")


def audit(con, user_id, action, case_id=None, details=""):
    con.execute("INSERT INTO audit_events(user_id,action,case_id,details,created_at) VALUES(?,?,?,?,?)", (user_id, action, case_id, details, time.time()))

def roles_of(user):
    try: return set(json.loads(user["roles"] or "[]"))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError): return {user["role"]}

def user_view(user):
    return {"user_id": user["id"], "username": user["username"], "roles": sorted(roles_of(user)), "status": user["status"] if "status" in user.keys() else "active", "category_admin": bool(user["category_admin"]) if "category_admin" in user.keys() else False, "training_permission": bool(user["training_permission"]) if "training_permission" in user.keys() else False}

def anonymized_case(case_id):
    return "CASE-" + hashlib.sha256(str(case_id).encode()).hexdigest()[:8].upper()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return base64.urlsafe_b64encode(salt + digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode())
        return hmac.compare_digest(hash_password(password, raw[:16]), encoded)
    except Exception:
        return False


def token_for(user: HybridRow) -> str:
    # Normal user sessions are long lived. Access is still controlled by the
    # signed payload and the current user status checked below.
    payload = base64.urlsafe_b64encode(json.dumps({"uid": user["id"], "exp": None}).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def current_user(authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "需要登录")
    from server.requirements_federation import is_requirements_key
    if is_requirements_key(authorization.split(" ", 1)[1].strip()):
        raise HTTPException(403, "个人 Key 仅可访问授权的需求接口")
    try:
        payload, sig = authorization.split(" ", 1)[1].split(".")
        if not hmac.compare_digest(sig, hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        if not data.get("uid"):
            raise ValueError
    except Exception as exc:
        raise HTTPException(401, "登录令牌无效") from exc
    con = db(); user = con.execute("SELECT * FROM users WHERE id=?", (data["uid"],)).fetchone(); con.close()
    if not user:
        raise HTTPException(401, "用户不存在")
    if user["status"] != "active":
        raise HTTPException(403, "账号未激活或已停用")
    return user


def current_user_optional(authorization: Annotated[str | None, Header()] = None):
    """Return the authenticated user when present, otherwise allow guests."""
    if not authorization:
        return None
    return current_user(authorization)


_PREDICTOR_GUEST_TOKEN_DOMAIN = b"swtool:predictor-guest:v1\0"
_PREDICTOR_GUEST_TTL_SECONDS = 7 * 86400


def predictor_user(
    authorization: Annotated[str | None, Header()] = None,
    x_predictor_guest: Annotated[str | None, Header()] = None,
):
    """Guest credentials authorize only their own uploaded Predictor jobs."""
    if authorization:
        return current_user(authorization)
    try:
        if not x_predictor_guest or len(x_predictor_guest) > 1024:
            raise ValueError
        payload, signature = x_predictor_guest.split(".")
        expected = hmac.new(SECRET, _PREDICTOR_GUEST_TOKEN_DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        owner = data["sid"]
        if data["purpose"] != "predictor-guest" or data["exp"] <= time.time():
            raise ValueError
        if not isinstance(owner, str) or len(owner) != 64 or any(c not in "0123456789abcdef" for c in owner):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "游客推理会话无效，请重新提交") from exc
    return {"id": 0, "roles": "[]", "role": "guest", "guest_owner": owner}


@app.post("/predictor/guest-session")
def create_predictor_guest_session(response: Response):
    expires = int(time.time()) + _PREDICTOR_GUEST_TTL_SECONDS
    payload = base64.urlsafe_b64encode(json.dumps({
        "purpose": "predictor-guest", "sid": secrets.token_hex(32), "exp": expires,
    }).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET, _PREDICTOR_GUEST_TOKEN_DOMAIN + payload.encode(), hashlib.sha256).hexdigest()
    response.headers["Cache-Control"] = "no-store"
    return {"token": payload + "." + signature, "expires_at": expires}


def current_issue_user(request: Request, authorization: Annotated[str | None, Header()] = None):
    """Authenticate sessions or revocable tokens with current owner permissions.

    Operations tokens are deliberately accepted only by issue routes that use
    this dependency; all existing routes continue to use ``current_user``.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "需要登录")
    raw = authorization.split(" ", 1)[1].strip()
    from server.requirements_federation import allowed, authenticate, is_requirements_key
    if is_requirements_key(raw):
        user = authenticate(sys.modules[__name__], raw)
        if not allowed(request.method, request.url.path.removeprefix("/"), user['_requirements_scope']):
            raise HTTPException(403, "个人 Key 无权访问该需求接口")
        return user
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    con = db()
    # 运维令牌默认永久有效，生命周期仅由管理员撤销控制。
    op = con.execute(
        "SELECT * FROM ops_tokens WHERE token_hash=? AND revoked_at IS NULL",
        (token_hash,),
    ).fetchone()
    if op:
        user = con.execute("SELECT * FROM users WHERE id=?", (op["created_by"],)).fetchone()
        con.close()
        if user and user["status"] == "active":
            return dict(user) | {"_ops_token": True}
        raise HTTPException(401, "运维令牌所属账号已不可用")
    con.close()
    return current_user(authorization)


class Register(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)
    role: str = "annotator"


class Login(BaseModel):
    username: str
    password: str


class CaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=240, pattern=r"^[^/\\\x00-\x1f]+$")
    title: str = ""
    metadata: dict = {}
    assigned_annotator_id: int | None = None
    assigned_reviewer_id: int | None = None


class AssignmentIn(BaseModel):
    assigned_annotator_id: int
    assigned_reviewer_id: int


class DraftIn(BaseModel):
    payload: dict
    segment_id: str | None = Field(default=None, min_length=1, max_length=512)
    accept_geometry_warnings: bool = False
    metadata_only: bool = False


class HandoffIn(BaseModel):
    target_annotator_id: int
    draft: dict


class MappingIn(BaseModel):
    reviewer_id: int


class RolesIn(BaseModel):
    roles: list[str]

class CategoriesIn(BaseModel):
    categories: list[str] = Field(default_factory=list, max_length=30)

class CategoryPermissionIn(BaseModel):
    enabled: bool

class TrainingPermissionIn(BaseModel):
    enabled: bool

class PasswordResetIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)

class GlobalCategoriesIn(BaseModel):
    categories: list[str] = Field(default_factory=list, max_length=50)

class UserReviewIn(BaseModel):
    status: str


class QueuePreferencesIn(BaseModel):
    case_order: list[str] = Field(default_factory=list, max_length=1000)
    case_names: dict[str, str] = Field(default_factory=dict, max_length=1000)
    video_orders: dict[str, list[str]] = Field(default_factory=dict, max_length=1000)
    video_names: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=1000)


class UploadFileSpec(BaseModel):
    name: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    chunk_size: int = Field(default=8 * 1024 * 1024, ge=1024)

class UploadInit(BaseModel):
    case_id: str = Field(min_length=1, max_length=240, pattern=r"^[^/\\\x00-\x1f]+$")
    title: str = ""
    metadata: dict = {}
    files: list[UploadFileSpec]
    assigned_annotator_id: int | None = None
    assigned_reviewer_id: int | None = None


_VIDEO_FRAME_COUNT_CACHE: dict[tuple[str, int, int], int] = {}


def _probe_video_frame_count(video_path: Path) -> int:
    """Return a video's reported frame count without decoding the full file."""
    if cv2 is None:
        return 0
    try:
        stat = video_path.stat()
        cache_key = (str(video_path), stat.st_mtime_ns, stat.st_size)
        if cache_key in _VIDEO_FRAME_COUNT_CACHE:
            return _VIDEO_FRAME_COUNT_CACHE[cache_key]
    except OSError:
        cache_key = None
    capture = None
    try:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return 0
        value = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if value and value > 0:
            result = max(0, int(round(value)))
            if cache_key is not None:
                _VIDEO_FRAME_COUNT_CACHE[cache_key] = result
            return result
    except (OSError, ValueError, TypeError, cv2.error):
        return 0
    finally:
        if capture is not None:
            capture.release()
    if cache_key is not None:
        _VIDEO_FRAME_COUNT_CACHE[cache_key] = 0
    return 0


def _is_ct_batch_container(case_dir: Path) -> bool:
    """Identify conservative batch folders before recursive case indexing.

    A directory with explicit case metadata is always treated as a case root.
    Otherwise, two or more directly nested CT directories indicate a batch of
    patient folders; those children are indexed independently.
    """
    if (case_dir / "metadata.json").is_file() or (case_dir / "title.txt").is_file():
        return False
    try:
        # If the parent itself has slices, retain it as one nested-series case.
        if _scan_ct_series(case_dir):
            return False
        children = [child for child in case_dir.iterdir() if child.is_dir()]
    except OSError:
        return False
    independent = 0
    for child in children:
        if _scan_ct_series(child):
            independent += 1
            if independent >= 2:
                return True
    return False


def _case_directories() -> list[Path]:
    """Find case folders at any depth while leaving media files untouched."""
    if not CASES_DIR.is_dir():
        return []
    found = []
    try:
        candidates = sorted((p for p in CASES_DIR.rglob("*") if p.is_dir()), key=lambda p: str(p))
    except OSError:
        return found
    ct_roots: list[Path] = []
    for case_dir in candidates:
        # These first-level folders classify cases; recursive CT discovery
        # would otherwise collapse all their patients into one case.
        if case_dir.parent == CASES_DIR and case_dir.name in {"video", "ct", "mri"}:
            continue
        # A DICOM case may contain one directory per series.  Once its parent
        # case has been recognized, do not index those technical subfolders as
        # independent clinical cases.
        if any(root in case_dir.parents for root in ct_roots):
            continue
        if _is_ct_batch_container(case_dir):
            continue
        scanned = _scan_case_directory(case_dir)
        if not scanned:
            continue
        if scanned[1].get("imaging_type") in {"ct_dicom", "ct_nifti", "mri_nifti"}:
            ct_roots.append(case_dir)
        found.append(case_dir)
    return found


def _cleanup_stale_directory_cases(con, valid_dirs: set[str]) -> None:
    """Retain directory-backed rows and mark only confirmed missing sources."""
    rows = con.execute("SELECT id,status,metadata FROM cases").fetchall()
    con.commit()  # PostgreSQL SELECT also starts a transaction.
    stale = []
    try:
        root = CASES_DIR.resolve()
    except (OSError, ValueError):
        return
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if metadata.get("source") != "server_directory":
            continue
        relative = str(metadata.get("source_relative") or "")
        try:
            resolved = (CASES_DIR / relative).resolve()
            if not resolved.is_relative_to(root):
                continue
            # A directory can be present but unclassified by the current
            # scanner; preserve its index until a later scan recognizes it.
            try:
                resolved.stat()
            except FileNotFoundError:
                stale.append((row, metadata))
            except OSError:
                continue
        except (OSError, ValueError):
            continue
    # All filesystem path resolution above occurs outside a write transaction.
    for row, metadata in stale:
        if metadata.get("source_missing"):
            continue
        try:
            metadata["source_missing"] = True
            con.execute("UPDATE cases SET metadata=? WHERE id=? AND metadata=?",
                        (json.dumps(metadata, ensure_ascii=False), row["id"], row["metadata"]))
            con.commit()
        except Exception:
            con.rollback()
            raise


def _dicom_number(value, default: float = 0.0) -> float:
    """Read a scalar DICOM value without retaining identity-bearing fields."""
    try:
        if not isinstance(value, (str, bytes)) and hasattr(value, "__iter__"):
            value = value[0]
        return float(value)
    except (TypeError, ValueError, IndexError):
        return default


def _dicom_tree_signature(case_dir: Path) -> tuple:
    count = 0; total_size = 0; newest = 0
    try:
        for path in case_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".json", ".txt", ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".mp4", ".avi", ".mov", ".mkv", ".webm"} or path.name.lower().endswith((".nii", ".nii.gz")):
                continue
            stat = path.stat(); count += 1; total_size += stat.st_size; newest = max(newest, stat.st_mtime_ns)
    except OSError:
        return (0, 0, 0)
    return (count, total_size, newest)


def _scan_ct_series(case_dir: Path) -> list[dict]:
    """Discover CT slices under a case directory and return non-PHI metadata.

    Only technical tags are read.  File paths remain internal to the server and
    are later removed by ``_public_case_metadata`` before any API response.
    """
    if pydicom is None:
        return []
    signature = _dicom_tree_signature(case_dir)
    cache_key = str(case_dir.resolve())
    now = time.monotonic()
    with _DICOM_SCAN_CACHE_LOCK:
        cached = _DICOM_SCAN_CACHE.get(cache_key)
        if cached and cached[1] == signature and now - cached[0] < 15:
            return copy.deepcopy(cached[2])
    groups: dict[str, dict] = {}
    try:
        candidates = (path for path in case_dir.rglob("*") if path.is_file())
        for path in candidates:
            # Avoid re-reading ordinary web/video files during every automatic scan.
            if path.suffix.lower() in {".json", ".txt", ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".mp4", ".avi", ".mov", ".mkv", ".webm"} or path.name.lower().endswith((".nii", ".nii.gz")):
                continue
            try:
                dataset = pydicom.dcmread(
                    str(path), stop_before_pixels=True,
                    force=True,
                    specific_tags=["Modality", "SOPClassUID", "SeriesInstanceUID", "ImagePositionPatient", "ImageOrientationPatient", "InstanceNumber", "WindowCenter", "WindowWidth", "Rows", "Columns", "PixelSpacing", "SpacingBetweenSlices", "SliceThickness", "NumberOfFrames"],
                )
            except (InvalidDicomError, OSError, ValueError):
                continue
            if str(getattr(dataset, "Modality", "")).upper() != "CT":
                continue
            # Enhanced/multi-frame objects need a separate frame-of-reference
            # implementation.  Do not incorrectly treat one object as one slice.
            if int(_dicom_number(getattr(dataset, "NumberOfFrames", None), 1.0)) > 1:
                continue
            if str(getattr(dataset, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.2.1":
                continue
            series_uid = str(getattr(dataset, "SeriesInstanceUID", "") or "")
            if not series_uid:
                # A deterministic local id supports legacy exports lacking a UID.
                series_uid = "local-" + hashlib.sha256(str(path.parent.relative_to(case_dir)).encode()).hexdigest()[:16]
            position = getattr(dataset, "ImagePositionPatient", None)
            orientation = getattr(dataset, "ImageOrientationPatient", None)
            try:
                row = [float(orientation[i]) for i in range(3)]
                col = [float(orientation[i + 3]) for i in range(3)]
                normal = [row[1] * col[2] - row[2] * col[1], row[2] * col[0] - row[0] * col[2], row[0] * col[1] - row[1] * col[0]]
                position_key = sum(float(position[i]) * normal[i] for i in range(3))
            except (TypeError, ValueError, IndexError):
                position_key = _dicom_number(position[2] if position and len(position) > 2 else None)
            group = groups.setdefault(series_uid, {
                "id": series_uid,
                "modality": "CT",
                "window_center": _dicom_number(getattr(dataset, "WindowCenter", None), 40.0),
                "window_width": max(_dicom_number(getattr(dataset, "WindowWidth", None), 400.0), 1.0),
                "rows": int(_dicom_number(getattr(dataset, "Rows", None), 0)),
                "columns": int(_dicom_number(getattr(dataset, "Columns", None), 0)),
                "spacing_y": max(_dicom_number(getattr(dataset, "PixelSpacing", [1.0, 1.0]), 1.0), 0.01),
                "spacing_x": max(_dicom_number((getattr(dataset, "PixelSpacing", [1.0, 1.0]) or [1.0, 1.0])[1] if len(getattr(dataset, "PixelSpacing", []) or []) > 1 else None, 1.0), 0.01),
                "spacing_z": max(_dicom_number(getattr(dataset, "SpacingBetweenSlices", None), _dicom_number(getattr(dataset, "SliceThickness", None), 1.0)), 0.01),
                "_files": [],
            })
            try:
                relative = str(path.relative_to(case_dir))
            except ValueError:
                continue
            group["_files"].append({"path": relative, "position": position_key, "instance": _dicom_number(getattr(dataset, "InstanceNumber", None)), "name": path.name})
    except OSError:
        return []
    series = []
    for group in groups.values():
        group["_files"].sort(key=lambda item: (item["position"], item["instance"], item["name"]))
        positions = [float(item["position"]) for item in group["_files"] if isinstance(item.get("position"), (int, float))]
        if len(positions) > 1:
            distances = [abs(right - left) for left, right in zip(positions, positions[1:]) if abs(right - left) > 1e-6]
            if distances:
                distances.sort()
                group["spacing_z"] = distances[len(distances) // 2]
        for item in group["_files"]:
            item.pop("position", None)
        count = len(group["_files"])
        if count:
            group["slice_count"] = count
            group["frame_count"] = count
            group["total_frames"] = count
            series.append(group)
    series.sort(key=lambda item: item["id"])
    for index, item in enumerate(series, start=1):
        # SeriesDescription is free text and can contain PHI.  Use a stable,
        # clinical-neutral label instead of storing it at all.
        item["description"] = f"CT 序列 {index}"
    with _DICOM_SCAN_CACHE_LOCK:
        _DICOM_SCAN_CACHE[cache_key] = (now, signature, copy.deepcopy(series))
    return series


def _nifti_source_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not relative or Path(relative).is_absolute() or not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("invalid NIfTI source path")
    if not path.name.lower().endswith((".nii", ".nii.gz")):
        raise ValueError("unsupported NIfTI source")
    return path


def _nifti_geometry(image) -> dict:
    import numpy as np
    shape = tuple(int(value) for value in image.shape)
    affine = np.asarray(image.affine, dtype=np.float64)
    spatial_unit = image.header.get_xyzt_units()[0]
    unit_scale = {"mm": 1.0, "meter": 1000.0, "micron": 0.001, "unknown": 1.0}.get(spatial_unit)
    if unit_scale is None:
        raise ValueError("unsupported NIfTI spatial unit")
    affine = affine.copy()
    affine[:3, :] *= unit_scale
    if len(shape) != 3 or min(shape) <= 0 or math.prod(shape) > 500_000_000:
        raise ValueError("NIfTI requires a bounded 3D image")
    if not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-12:
        raise ValueError("invalid NIfTI affine")
    if not np.issubdtype(image.get_data_dtype(), np.number) or np.issubdtype(image.get_data_dtype(), np.complexfloating):
        raise ValueError("unsupported NIfTI pixels")
    orientation = nib.orientations.ornt_transform(
        nib.orientations.io_orientation(affine), nib.orientations.axcodes2ornt(("L", "P", "S")),
    )
    canonical_affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine @ nib.orientations.inv_ornt_aff(orientation, shape)
    axes = [int(np.flatnonzero(orientation[:, 0] == axis)[0]) for axis in range(3)]
    canonical_shape = tuple(shape[axis] for axis in axes)
    if canonical_shape[0] * canonical_shape[1] > 16_000_000:
        raise ValueError("NIfTI axial plane exceeds memory budget")
    spacing = np.linalg.norm(canonical_affine[:3, :3], axis=0)
    if not np.isfinite(spacing).all() or min(spacing) <= 0:
        raise ValueError("invalid NIfTI spacing")
    directions = canonical_affine[:3, :3] / spacing
    if not np.allclose(directions.T @ directions, np.eye(3), atol=1e-4):
        raise ValueError("sheared NIfTI affine is not supported")
    return {"orientation": orientation, "axes": axes, "shape": canonical_shape,
            "affine": canonical_affine, "spacing": spacing, "source_shape": shape,
            "source_affine_mm": affine, "source_spatial_unit": spatial_unit,
            "spatial_unit_assumed_mm": spatial_unit == "unknown"}


def _nifti_axial_pixels(image, geometry: dict, frame: int):
    """Read one scaled native plane, then permute/flip into LPS x/y/z indices."""
    import numpy as np
    x_axis, y_axis, z_axis = geometry["axes"]
    if frame < 0 or frame >= geometry["shape"][2]:
        raise HTTPException(404, "切片号超出范围")
    orientation = geometry["orientation"]
    native_frame = frame if orientation[z_axis, 1] > 0 else geometry["shape"][2] - 1 - frame
    selection = [slice(None)] * 3
    selection[z_axis] = native_frame
    # ArrayProxy slicing applies scl_slope/inter exactly once without get_fdata's full float64 copy.
    plane = np.asarray(image.dataobj[tuple(selection)], dtype=np.float32)
    remaining_axes = [axis for axis in range(3) if axis != z_axis]
    plane = plane.transpose(remaining_axes.index(y_axis), remaining_axes.index(x_axis))
    if orientation[y_axis, 1] < 0:
        plane = plane[::-1, :]
    if orientation[x_axis, 1] < 0:
        plane = plane[:, ::-1]
    return plane


def _scan_nifti_series(case_dir: Path, metadata: dict) -> list[dict]:
    """Index explicit original images, never paired labels or whole datasets."""
    if nib is None:
        return []
    import numpy as np
    labels = metadata.get("label_files", [])
    labels = [str(value) for value in labels] if isinstance(labels, list) else []
    if metadata.get("label_file"):
        labels.append(str(metadata["label_file"]))
    images = metadata.get("imaging_files", [])
    images = [str(value) for value in images] if isinstance(images, list) else []
    if metadata.get("image_file"):
        images.append(str(metadata["image_file"]))
    if not images:
        # Only direct image files may establish a legacy case root; nested files
        # cannot turn classification/batch directories into a single patient.
        images = [path.name for path in sorted(case_dir.iterdir()) if path.is_file()
                  and path.name.lower().endswith((".nii", ".nii.gz"))
                  and not any(token in path.name.lower() for token in ("_seg", "label", "mask", "truth"))]
        if len(images) != 1:
            return []
    type_value = str(metadata.get("imaging_type", "")).strip().lower()
    relative_parts = case_dir.relative_to(CASES_DIR).parts
    modality = "MR" if type_value in {"mri", "mr", "mri_nifti", "mri_dicom", "mr_dicom"} or relative_parts[0] == "mri" else "CT"
    result = []
    for relative in dict.fromkeys(images):
        try:
            path = _nifti_source_path(case_dir, relative)
            if any(path == _nifti_source_path(case_dir, label) for label in labels):
                return []
            image = nib.load(str(path), mmap=True)
            geometry = _nifti_geometry(image)
            columns, rows, depth = geometry["shape"]
            center, width = 40.0, 400.0
            if modality == "MR":
                pixels = _nifti_axial_pixels(image, geometry, depth // 2)
                finite = pixels[np.isfinite(pixels)]
                if not finite.size:
                    return []
                low, high = np.percentile(finite, [1, 99])
                center, width = float((low + high) / 2), max(1e-6, float(high - low))
            spacing_x, spacing_y, spacing_z = (float(value) for value in geometry["spacing"])
            result.append({"id": "nifti-" + hashlib.sha256(relative.encode()).hexdigest()[:16],
                           "description": f"{'MRI' if modality == 'MR' else 'CT'} 序列 {len(result) + 1}",
                           "modality": modality, "format": "nifti", "rows": rows, "columns": columns,
                           "slice_count": depth, "frame_count": depth, "total_frames": depth,
                           "spacing_x": spacing_x, "spacing_y": spacing_y, "spacing_z": spacing_z,
                           "window_center": center, "window_width": width,
                           "source_shape": list(geometry["source_shape"]),
                           "source_affine": np.asarray(image.affine).tolist(),
                           "source_affine_mm": geometry["source_affine_mm"].tolist(),
                           "source_spatial_unit": geometry["source_spatial_unit"],
                           "spatial_unit_assumed_mm": geometry["spatial_unit_assumed_mm"],
                           "frame_affine_lps": geometry["affine"].tolist(), "_image_file": relative})
        except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError):
            return []
    return result


_REFERENCE_LABEL_VALIDATION_CACHE = {}
_REFERENCE_LABEL_VALIDATION_LOCK = threading.RLock()


def _reference_label_stem(name: str) -> str:
    value = str(name).lower()
    value = value[:-7] if value.endswith(".nii.gz") else value[:-4] if value.endswith(".nii") else value
    for suffix in ("_segmentation", "_seg", "_label", "_mask", "_truth"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def _reference_label_for_series(image_name: str, labels: list[str]) -> str | None:
    paired = [label for label in labels if _reference_label_stem(label) == _reference_label_stem(image_name)]
    if len(paired) == 1:
        return paired[0]
    if len(labels) == 1:
        return labels[0]
    return None


def _has_valid_reference_label(case_id: str | None, metadata: dict) -> bool:
    import numpy as np

    if not case_id or metadata.get("imaging_type") not in {"ct_nifti", "mri_nifti"} or nib is None:
        return False
    root = _case_root(case_id, metadata)
    if root is None:
        return False
    labels = metadata.get("label_files", [])
    labels = [str(value) for value in labels] if isinstance(labels, list) else []
    if metadata.get("label_file"):
        labels.append(str(metadata["label_file"]))
    series = metadata.get("volume_series")
    if not isinstance(series, list) or not series:
        series = _scan_nifti_series(root, metadata)
    labels = list(dict.fromkeys(labels))
    for item in series:
        if not isinstance(item, dict) or not item.get("_image_file"):
            continue
        label = _reference_label_for_series(str(item["_image_file"]), labels)
        if label is None:
            continue
        try:
            label_path = _nifti_source_path(root, label)
            label_signature = (label_path.stat().st_size, label_path.stat().st_mtime_ns)
            label_image = nib.load(str(label_path), mmap=True, keep_file_open=True)
            label_geometry = _nifti_geometry(label_image)
            image_path = _nifti_source_path(root, str(item["_image_file"]))
            signature = (image_path.stat().st_size, image_path.stat().st_mtime_ns)
            cache_key = (str(image_path), signature, str(label_path), label_signature)
            with _REFERENCE_LABEL_VALIDATION_LOCK:
                cached = _REFERENCE_LABEL_VALIDATION_CACHE.get(cache_key)
            if cached is None:
                image = nib.load(str(image_path), mmap=True, keep_file_open=True)
                image_geometry = _nifti_geometry(image)
                cached = (
                    tuple(label_geometry["source_shape"]) == tuple(image_geometry["source_shape"])
                    and np.allclose(label_geometry["source_affine_mm"], image_geometry["source_affine_mm"], rtol=1e-6, atol=1e-5)
                    and tuple(label_geometry["axes"]) == tuple(image_geometry["axes"])
                    and np.array_equal(label_geometry["orientation"], image_geometry["orientation"])
                )
                with _REFERENCE_LABEL_VALIDATION_LOCK:
                    if len(_REFERENCE_LABEL_VALIDATION_CACHE) >= 4096:
                        _REFERENCE_LABEL_VALIDATION_CACHE.clear()
                    _REFERENCE_LABEL_VALIDATION_CACHE[cache_key] = bool(cached)
            if cached:
                return True
        except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError):
            continue
    return False


def _public_case_metadata(metadata: dict, case_id: str | None = None) -> dict:
    """Return client metadata without source paths, filenames, or PHI."""
    result = {
        "frame_count": metadata.get("frame_count", 0),
        "total_frames": metadata.get("total_frames", 0),
        "video_files": metadata.get("video_files", []),
        "video_metadata": metadata.get("video_metadata", {}),
        "imaging_type": metadata.get("imaging_type", "video"),
        "has_reference_label": _has_valid_reference_label(case_id, metadata),
    }
    for key in ("annotation_frames", "required_frames", "annotate_frames", "must_label_frames", "frame_step", "stride"):
        if key in metadata:
            result[key] = metadata[key]
    raw_series = metadata.get("dicom_series")
    if isinstance(raw_series, list):
        allowed = {"id", "description", "slice_count", "total_frames", "frame_count", "window_center", "window_width", "modality", "rows", "columns", "spacing_x", "spacing_y", "spacing_z"}
        safe_series = []
        for index, item in enumerate(raw_series, start=1):
            if not isinstance(item, dict):
                continue
            safe = {key: item[key] for key in allowed if key in item}
            safe["description"] = f"CT 序列 {index}"
            safe_series.append(safe)
        result["dicom_series"] = safe_series
    raw_volumes = metadata.get("volume_series")
    if isinstance(raw_volumes, list):
        allowed = {"id", "description", "modality", "format", "rows", "columns", "slice_count", "frame_count",
                   "total_frames", "spacing_x", "spacing_y", "spacing_z", "window_center", "window_width",
                   "source_shape", "source_affine", "source_affine_mm", "source_spatial_unit",
                   "spatial_unit_assumed_mm", "frame_affine_lps"}
        result["volume_series"] = [{key: value for key, value in item.items() if key in allowed}
                                   for item in raw_volumes if isinstance(item, dict)]
    return result


def _scan_case_directory(case_dir: Path):
    """Read lightweight metadata for a server-side case without moving media."""
    frames_dir = case_dir / "frames"
    videos = [p for p in case_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".avi", ".mov", ".mkv", ".webm"}]
    has_frame_files = frames_dir.is_dir() and any(p.is_file() for p in frames_dir.iterdir())
    dicom_series = _scan_ct_series(case_dir)
    metadata = {}
    metadata_file = case_dir / "metadata.json"
    if metadata_file.is_file():
        try:
            loaded = json.loads(metadata_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metadata.update(loaded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    volume_series = _scan_nifti_series(case_dir, metadata)
    if not has_frame_files and not videos and not dicom_series and not volume_series:
        return None
    title = str(metadata.pop("title", "") or "")
    title_file = case_dir / "title.txt"
    if not title and title_file.is_file():
        try:
            title = title_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            pass
    image_count = sum(1 for p in frames_dir.iterdir() if p.is_file()) if frames_dir.is_dir() else 0
    if volume_series:
        first_series = volume_series[0]
        metadata.update({"source": "server_directory", "source_relative": str(case_dir.relative_to(CASES_DIR)),
                         "has_frames": False, "frame_count": first_series["slice_count"],
                         "total_frames": first_series["slice_count"], "video_files": [], "video_metadata": {},
                         "imaging_type": "mri_nifti" if first_series["modality"] == "MR" else "ct_nifti",
                         "volume_series": volume_series})
        return title or case_dir.name, metadata
    if dicom_series:
        first_series = dicom_series[0]
        metadata.update({
            "source": "server_directory",
            "source_relative": str(case_dir.relative_to(CASES_DIR)),
            "has_frames": False,
            "frame_count": first_series["slice_count"],
            "total_frames": first_series["slice_count"],
            "video_files": [],
            "video_metadata": {},
            "imaging_type": "ct_dicom",
            "dicom_series": dicom_series,
        })
        return title or case_dir.name, metadata
    # A resumable copy can expose an MP4 before its container index (for
    # example the MP4 ``moov`` atom) has reached the destination.  Do not
    # register that directory yet: clients would otherwise receive a case
    # whose first frame consistently fails with a 409.  Image-only and DICOM
    # directories intentionally remain available when OpenCV is unavailable.
    video_frame_counts = {}
    if videos and cv2 is not None:
        video_frame_counts = {video.name: _probe_video_frame_count(video) for video in videos}
        if any(count <= 0 for count in video_frame_counts.values()):
            return None
    # Video-specific frame counts must be kept separately. A case can contain
    # several recordings with different lengths, and using the first video's
    # count for every recording makes the tail of shorter videos seek into
    # non-existent frames.
    old_video_metadata = metadata.get("video_metadata")
    if not isinstance(old_video_metadata, dict):
        old_video_metadata = {}
    video_metadata = {}
    for video in videos:
        previous = old_video_metadata.get(video.name)
        entry = dict(previous) if isinstance(previous, dict) else {}
        actual_count = video_frame_counts.get(video.name, _probe_video_frame_count(video))
        if actual_count > 0:
            entry["frame_count"] = actual_count
            entry["total_frames"] = actual_count
            for plan_key in ("annotation_frames", "required_frames", "annotate_frames", "must_label_frames"):
                plan = entry.get(plan_key)
                if isinstance(plan, list):
                    filtered = []
                    for value in plan:
                        try:
                            frame_number = int(value)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= frame_number < actual_count:
                            filtered.append(frame_number)
                    entry[plan_key] = filtered
        video_metadata[video.name] = entry

    # Keep legacy metadata as a fallback only when probing a video is not
    # possible. For normal video cases the case-level count follows the first
    # video for backward-compatible clients; per-video consumers use the map
    # above.
    declared_count = metadata.get("total_frames", metadata.get("frame_count", 0))
    try:
        declared_count = int(declared_count or 0)
    except (TypeError, ValueError):
        declared_count = 0
    frame_count = image_count
    if videos:
        first_count = video_metadata[videos[0].name].get("frame_count", 0)
        frame_count = int(first_count or declared_count or 0)
    elif frame_count <= 0:
        frame_count = declared_count
    metadata.update({
        "source": "server_directory",
        "source_relative": str(case_dir.relative_to(CASES_DIR)),
        "has_frames": frames_dir.is_dir(),
        "frame_count": frame_count,
        "total_frames": frame_count,
        "video_files": [p.name for p in videos],
        "video_metadata": video_metadata,
    })
    return title or case_dir.name, metadata


def _sync_case_directory():
    """Synchronize server-side case folders for normal case-list requests.

    This is intentionally lightweight and has no audit side effects. Existing
    workflow state and assignments are preserved; only title/metadata are
    refreshed from the folder. Any filesystem/database error is swallowed so
    a transient directory problem cannot prevent users from seeing cases
    already indexed in the database.
    """
    try:
        CASES_DIR.mkdir(parents=True, exist_ok=True)
        # Complete potentially slow DICOM/video traversal before any DB writes.
        case_dirs = _case_directories()
        valid_dirs = {str(p.relative_to(CASES_DIR)) for p in case_dirs}
        scanned_cases = []
        for case_dir in case_dirs:
            case_id = case_dir.name
            if (not case_id or case_id in {".", ".."} or "/" in case_id or "\\" in case_id
                    or any(ord(ch) < 32 for ch in case_id)):
                continue
            scanned = _scan_case_directory(case_dir)
            if scanned:
                scanned_cases.append((case_id, *scanned))
        con = db()
        try:
            _cleanup_stale_directory_cases(con, valid_dirs)
            for case_id, title, metadata in scanned_cases:
                existing = con.execute("SELECT title,metadata FROM cases WHERE id=?", (case_id,)).fetchone()
                con.commit()
                encoded = json.dumps(metadata, ensure_ascii=False)
                if existing:
                    try:
                        unchanged = existing["title"] == title and json.loads(existing["metadata"]) == metadata
                    except (TypeError, ValueError):
                        unchanged = False
                    if unchanged:
                        continue
                    con.execute("UPDATE cases SET title=?,metadata=? WHERE id=?", (title, encoded, case_id))
                else:
                    con.execute(
                        "INSERT INTO cases(id,title,metadata,status,created_at) VALUES(?,?,?,?,?)",
                        (case_id, title, encoded, "available", time.time()),
                    )
                con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    except Exception:
        # Directory scans are best-effort on the request path.
        return False
    return True


_CASE_SYNC_INTERVAL_SECONDS = 30.0
_CASE_SYNC_LOCK = threading.Lock()
_case_sync_running = False
_case_sync_last_scheduled_at = 0.0


def _schedule_case_directory_sync():
    """Run at most one best-effort directory scan per interval in background."""
    global _case_sync_running, _case_sync_last_scheduled_at
    now = time.monotonic()
    with _CASE_SYNC_LOCK:
        if _case_sync_running or now - _case_sync_last_scheduled_at < _CASE_SYNC_INTERVAL_SECONDS:
            return False
        _case_sync_running = True
        _case_sync_last_scheduled_at = now

    def run_sync():
        global _case_sync_running
        try:
            _sync_case_directory()
        except Exception:
            # A background directory problem must never affect API requests.
            pass
        finally:
            with _CASE_SYNC_LOCK:
                _case_sync_running = False

    threading.Thread(target=run_sync, name="swtool-case-directory-sync", daemon=True).start()
    return True


@app.on_event("startup")
def startup():
    con = db()
    if os.getenv("SWTOOL_REQUIREMENTS_STANDALONE"):
        con.close()
        return
    try:
        con.execute("UPDATE predictor_jobs SET status='failed', error='服务重启导致推理任务中断', updated_at=? WHERE status IN ('queued','running')", (time.time(),))
        interrupted = con.execute(
            "SELECT id,report_json FROM evaluation_runs WHERE status IN ('queued','running')"
        ).fetchall()
        for row in interrupted:
            try:
                report = json.loads(row["report_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                report = {}
            if not isinstance(report, dict):
                report = {}
            error = "服务重启导致评估任务中断"
            report["note"] = error
            con.execute(
                "UPDATE evaluation_runs SET status=?,report_json=?,error=? WHERE id=?",
                ("failed", json.dumps(report, ensure_ascii=False), error, row["id"]),
            )
        for row in con.execute("SELECT id,config_json,output_path FROM training_runs WHERE status IN ('preparing','queued','running')").fetchall():
            try: config = json.loads(row['config_json'] or '{}')
            except (TypeError, ValueError): config = {}
            if isinstance(config, dict) and config.get('background_preparation'):
                if config.get('task_kind') in {'sim_cbct', 'nnunet_mixed'}:
                    continue
                con.execute("UPDATE training_runs SET status='failed',error=?,updated_at=? WHERE id=?", ('服务重启导致后台训练任务中断，请重新创建；已保留任务和进度', time.time(), row['id']))
        con.commit()
    finally:
        con.close()
    if not os.getenv("SWTOOL_REQUIREMENTS_STANDALONE"):
        _schedule_case_directory_sync()


@app.post("/auth/register")
def register(body: Register):
    con = db()
    try:
        # Serialize first-account creation so only one concurrent registrant becomes administrator.
        con.execute("BEGIN")
        if DB_URL:
            con.execute("LOCK TABLE users IN EXCLUSIVE MODE")
        first = con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
        roles = ["admin"] if first else ["annotator"]
        status = "active" if first else "pending"
        cur = con.execute("INSERT INTO users(username,password_hash,role,roles,status,created_at) VALUES(?,?,?,?,?,?) RETURNING id", (body.username, hash_password(body.password), roles[0], json.dumps(roles), status, time.time()))
        new_id = cur.fetchone()[0]
        if first:
            con.execute("INSERT INTO requirements_driver_center_owner(center_id,owner_id,created_at) VALUES(?,?,?) ON CONFLICT(center_id) DO NOTHING", ("standalone", new_id, time.time()))
        con.commit(); row = con.execute("SELECT * FROM users WHERE id=?", (new_id,)).fetchone()
    except DBIntegrityError as exc:
        raise HTTPException(409, "用户名已存在") from exc
    finally: con.close()
    if row["status"] != "active":
        return user_view(row) | {"message": "注册成功，请通知管理员审核通过后再登录"}
    return user_view(row) | {"access_token": token_for(row)}


@app.post("/auth/login")
def login(body: Login):
    con = db(); row = con.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone(); con.close()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if row["status"] == "pending":
        raise HTTPException(403, "账号待管理员审核，请审核通过后再登录")
    if row["status"] == "rejected":
        raise HTTPException(403, "账号审核未通过，请联系管理员")
    return user_view(row) | {"access_token": token_for(row)}

def require_role(*roles):
    def dep(user=Depends(current_user)):
        actual = roles_of(user)
        if not actual.intersection(roles) and not ("admin" in actual and "annotator" in roles): raise HTTPException(403, "权限不足")
        return user
    return dep

def require_category_admin(user=Depends(current_user)):
    actual = roles_of(user)
    if "admin" not in actual and not bool(user["category_admin"] if "category_admin" in user.keys() else False):
        raise HTTPException(403, "没有全局分类管理权限")
    return user

def require_training_permission(user=Depends(current_user)):
    if user["status"] != "active":
        raise HTTPException(403, "用户尚未激活或已被拒绝")
    if "admin" not in roles_of(user) and not bool(user["training_permission"] if "training_permission" in user.keys() else False):
        raise HTTPException(403, "没有训练权限")
    return user


@app.get("/me")
def get_me(user=Depends(current_user)):
    if user["status"] != "active":
        raise HTTPException(403, "用户尚未激活或已被拒绝")
    return user_view(user)


@app.post("/admin/ops-tokens")
def create_ops_token(response: Response, body: dict | None = None, admin=Depends(require_role("admin"))):
    response.headers["Cache-Control"] = "no-store"
    now = time.time()
    token_id = secrets.token_hex(12)
    raw = "swtool_ops_" + secrets.token_urlsafe(32)
    con = db()
    try:
        encrypted_secret = Fernet(OPS_TOKEN_ENCRYPTION_KEY).encrypt(raw.encode()).decode()
        con.execute(
            "INSERT INTO ops_tokens(id,token_hash,encrypted_secret,created_by,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
            (token_id, hashlib.sha256(raw.encode()).hexdigest(), encrypted_secret, admin["id"], now, 0),
        )
        audit(con, admin["id"], "ops_token_created", details=json.dumps({"token_id": token_id, "permanent": True}))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"token_id": token_id, "token": raw, "expires_at": None, "permanent": True, "message": "永久令牌已加密保存，可在令牌列表中随时查看"}


@app.get("/admin/ops-tokens")
def list_ops_tokens(admin=Depends(require_role("admin"))):
    con = db(); rows = con.execute(
        "SELECT id,created_by,created_at,expires_at,revoked_at FROM ops_tokens ORDER BY created_at DESC"
    ).fetchall(); con.close()
    return [{"token_id": r["id"], "created_by": r["created_by"], "created_at": r["created_at"],
             "expires_at": None, "revoked_at": r["revoked_at"],
             "permanent": True,
             "status": "revoked" if r["revoked_at"] else "active"}
            for r in rows]


@app.get("/admin/ops-tokens/{token_id}/secret")
def reveal_ops_token(token_id: str, response: Response, admin=Depends(require_role("admin"))):
    response.headers["Cache-Control"] = "no-store"
    con = db()
    try:
        row = con.execute(
            "SELECT encrypted_secret,revoked_at FROM ops_tokens WHERE id=?", (token_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "运维令牌不存在")
        if row["revoked_at"] is not None:
            raise HTTPException(409, "已撤销的运维令牌不可查看")
        if not row["encrypted_secret"]:
            raise HTTPException(409, "该旧令牌未保存加密副本，请轮换令牌后再查看")
        try:
            raw = Fernet(OPS_TOKEN_ENCRYPTION_KEY).decrypt(row["encrypted_secret"].encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            LOGGER.error("Unable to decrypt ops token id=%s", token_id)
            raise HTTPException(500, "令牌密文无法解密，请轮换令牌") from exc
        audit(con, admin["id"], "ops_token_revealed", details=json.dumps({"token_id": token_id}))
        con.commit()
        return {"token_id": token_id, "token": raw}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.delete("/admin/ops-tokens/{token_id}")
def revoke_ops_token(token_id: str, admin=Depends(require_role("admin"))):
    con = db(); row = con.execute("SELECT id,revoked_at FROM ops_tokens WHERE id=?", (token_id,)).fetchone()
    if not row:
        con.close(); raise HTTPException(404, "运维令牌不存在")
    if row["revoked_at"] is None:
        con.execute("UPDATE ops_tokens SET revoked_at=? WHERE id=?", (time.time(), token_id))
        audit(con, admin["id"], "ops_token_revoked", details=json.dumps({"token_id": token_id}))
        con.commit()
    con.close(); return {"token_id": token_id, "status": "revoked"}

@app.get("/admin/users/pending")
def pending_users(admin=Depends(require_role("admin"))):
    con = db(); rows = con.execute("SELECT id,username,role,roles,status,created_at FROM users WHERE status='pending' ORDER BY created_at").fetchall(); con.close()
    return [user_view(r) | {"created_at": r["created_at"]} for r in rows]

@app.get("/admin/users")
def admin_users(admin=Depends(require_role("admin"))):
    con = db(); rows = con.execute("SELECT * FROM users ORDER BY username").fetchall(); con.close()
    return {"items": [user_view(row) for row in rows]}

@app.patch("/admin/users/{user_id}/status")
def review_user(user_id: int, body: UserReviewIn, admin=Depends(require_role("admin"))):
    if body.status not in {"active", "rejected"}:
        raise HTTPException(400, "审核状态无效")
    con = db(); row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        con.close(); raise HTTPException(404, "用户不存在")
    if row["status"] != "pending":
        con.close(); raise HTTPException(409, "该用户不在待审核状态")
    con.execute("UPDATE users SET status=? WHERE id=?", (body.status, user_id))
    audit(con, admin["id"], "user_registration_review", details=json.dumps({"target_user_id": user_id, "status": body.status}, ensure_ascii=False))
    con.commit(); updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone(); con.close()
    return user_view(updated)


@app.patch("/admin/users/{user_id}/roles")
def update_user_roles(user_id: int, body: RolesIn, admin=Depends(require_role("admin"))):
    allowed = {"admin", "annotator"}
    roles = list(dict.fromkeys(body.roles))
    if not roles or any(role not in allowed for role in roles):
        raise HTTPException(400, "角色无效")

    con = db()
    try:
        user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "用户不存在")
        before = sorted(roles_of(user))
        con.execute("UPDATE users SET role=?, roles=? WHERE id=?", (roles[0], json.dumps(roles), user_id))
        audit(con, admin["id"], "user_roles_update", details=json.dumps({
            "target_user_id": user_id,
            "before": before,
            "after": sorted(roles),
        }, ensure_ascii=False, sort_keys=True))
        con.commit()
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return user_view(updated)
    finally:
        con.close()


@app.patch("/admin/users/{user_id}/password")
def reset_user_password(user_id: int, body: PasswordResetIn, admin=Depends(require_role("admin"))):
    con = db()
    try:
        user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, "用户不存在")
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), user_id))
        audit(con, admin["id"], "user_password_reset", details=json.dumps({"target_user_id": user_id}, ensure_ascii=False))
        con.commit()
        updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return user_view(updated)
    finally:
        con.close()


@app.patch("/admin/users/{user_id}/categories")
def update_user_categories(user_id: int, body: CategoriesIn, admin=Depends(require_role("admin"))):
    raise HTTPException(410, "分类为全局定义，请使用全局分类权限授权接口")

@app.patch("/admin/users/{user_id}/category-permission")
def update_category_permission(user_id: int, body: CategoryPermissionIn, admin=Depends(require_role("admin"))):
    con=db(); user=con.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not user: con.close(); raise HTTPException(404,"用户不存在")
    con.execute("UPDATE users SET category_admin=? WHERE id=?",(body.enabled,user_id)); audit(con,admin["id"],"category_permission_update",details=json.dumps({"target_user_id":user_id,"enabled":body.enabled})); con.commit(); updated=con.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone(); con.close(); return user_view(updated)

@app.get("/categories")
def get_global_categories(user=Depends(current_user)):
    con = db(); row = con.execute("SELECT categories FROM global_categories WHERE id=1").fetchone()
    if not row:
        categories = ["子宫", "卵巢", "其他"]
        con.execute("INSERT INTO global_categories(id,categories,updated_at) VALUES(1,?,?)", (json.dumps(categories, ensure_ascii=False), time.time())); con.commit()
    else:
        try: categories = json.loads(row["categories"])
        except (TypeError, json.JSONDecodeError): categories = ["子宫", "卵巢", "其他"]
    con.close(); return {"categories": categories}

@app.patch("/admin/users/{user_id}/training-permission")
def update_training_permission(user_id: int, body: TrainingPermissionIn, admin=Depends(require_role("admin"))):
    con = db(); user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        con.close(); raise HTTPException(404, "用户不存在")
    con.execute("UPDATE users SET training_permission=? WHERE id=?", (body.enabled, user_id))
    audit(con, admin["id"], "training_permission_update", details=json.dumps({"target_user_id": user_id, "enabled": body.enabled}))
    con.commit(); updated = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone(); con.close()
    return user_view(updated)

@app.patch("/admin/categories")
def update_global_categories(body: GlobalCategoriesIn, admin=Depends(require_category_admin)):
    categories = list(dict.fromkeys(x.strip() for x in body.categories if isinstance(x, str) and x.strip()))
    if not categories or len(categories) > 50 or any(len(x) > 40 for x in categories): raise HTTPException(400, "全局分类不能为空，且最多50个、单个名称不超过40字")
    con = db(); payload = json.dumps(categories, ensure_ascii=False)
    con.execute("INSERT INTO global_categories(id,categories,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET categories=excluded.categories,updated_at=excluded.updated_at", (payload, time.time()))
    audit(con, admin["id"], "global_categories_update", details=payload); con.commit(); con.close(); return {"categories": categories}


@app.post("/admin/annotators/{annotator_id}/reviewer")
def map_reviewer(annotator_id: int, body: MappingIn, admin=Depends(require_role("admin"))):
    con = db(); a = con.execute("SELECT id,role,roles FROM users WHERE id=?", (annotator_id,)).fetchone(); r = con.execute("SELECT id,role,roles FROM users WHERE id=?", (body.reviewer_id,)).fetchone()
    if not a or "annotator" not in roles_of(a) or not r or not ({"annotator", "admin"} & roles_of(r)) or annotator_id == body.reviewer_id: raise HTTPException(400, "审核员必须是其他标注员")
    con.execute("INSERT INTO reviewer_map VALUES(?,?) ON CONFLICT(annotator_id) DO UPDATE SET reviewer_id=excluded.reviewer_id", (annotator_id, body.reviewer_id)); con.commit(); con.close(); return {"ok": True}


def _validate_assignment(con, annotator_id, reviewer_id):
    if annotator_id is None and reviewer_id is None:
        return
    if annotator_id is None or reviewer_id is None:
        raise HTTPException(400, "标注员和审核员需同时指定，或都留空由领取时分配")
    if annotator_id == reviewer_id:
        raise HTTPException(400, "标注员和审核员不能是同一人")
    a = con.execute("SELECT * FROM users WHERE id=?", (annotator_id,)).fetchone()
    r = con.execute("SELECT * FROM users WHERE id=?", (reviewer_id,)).fetchone()
    if not a or not ({"annotator", "admin"} & roles_of(a)) or not r or not ({"annotator", "admin"} & roles_of(r)):
        raise HTTPException(400, "用户角色不匹配")


@app.patch("/admin/cases/{case_id}/assignment")
def assign_case(case_id: str, body: AssignmentIn, admin=Depends(require_role("admin"))):
    con = db(); case = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case: con.close(); raise HTTPException(404, "案例不存在")
    if case["status"] not in {"available", "rejected"}:
        con.close(); raise HTTPException(409, "仅可分配 available/rejected 案例")
    _validate_assignment(con, body.assigned_annotator_id, body.assigned_reviewer_id)
    con.execute("UPDATE cases SET assigned_annotator_id=?,assigned_reviewer_id=? WHERE id=?", (body.assigned_annotator_id, body.assigned_reviewer_id, case_id))
    audit(con, admin["id"], "case_assignment", case_id, json.dumps(body.model_dump()))
    con.commit(); con.close()
    return {"case_id": case_id, "assigned_annotator_id": body.assigned_annotator_id, "assigned_reviewer_id": body.assigned_reviewer_id}


@app.post("/admin/cases/{case_id}/mark-annotated")
def mark_case_annotated(case_id: str, admin=Depends(require_role("admin"))):
    con = db()
    case = con.execute("SELECT status FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        con.close()
        raise HTTPException(404, "案例不存在")
    previous_status = case["status"]
    con.execute("UPDATE cases SET status='approved' WHERE id=?", (case_id,))
    con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
    con.execute("UPDATE submissions SET status='approved' WHERE id=(SELECT id FROM submissions WHERE case_id=? ORDER BY id DESC LIMIT 1)", (case_id,))
    audit(con, admin["id"], "admin_mark_annotated", case_id, json.dumps({"previous_status": previous_status}, ensure_ascii=False))
    con.commit(); con.close()
    return {"case_id": case_id, "status": "approved", "previous_status": previous_status}


@app.post("/admin/cases/{case_id}/mark-unannotated")
def mark_case_unannotated(case_id: str, admin=Depends(require_role("admin"))):
    con = db()
    case = con.execute("SELECT status FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        con.close()
        raise HTTPException(404, "案例不存在")
    previous_status = case["status"]
    if previous_status != "approved":
        con.close()
        raise HTTPException(409, "仅可将已标注案例移回未标注")
    con.execute("UPDATE cases SET status='available' WHERE id=?", (case_id,))
    con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
    audit(con, admin["id"], "admin_mark_unannotated", case_id, json.dumps({"previous_status": previous_status}, ensure_ascii=False))
    con.commit(); con.close()
    return {"case_id": case_id, "status": "available", "previous_status": previous_status}


@app.get("/admin/progress")
def admin_progress(admin=Depends(require_role("admin"))):
    con = db()
    users = con.execute("SELECT id,username,roles,category_admin FROM users ORDER BY id").fetchall()
    result = []
    for u in users:
        counts = {s: 0 for s in ("assigned", "in_progress", "submitted", "approved", "rejected")}
        owner = "assigned_annotator_id"
        status_map = {"assigned": "available", "in_progress": "annotation_in_progress", "submitted": "annotation_submitted", "approved": "approved", "rejected": "rejected"}
        for key, status in status_map.items():
            counts[key] = con.execute(f"SELECT COUNT(*) FROM cases WHERE ({owner}=? OR assigned_reviewer_id=?) AND status=?", (u["id"], u["id"], status)).fetchone()[0]
        result.append({"user": user_view(u), "counts": counts})
    overview = {s: con.execute("SELECT COUNT(*) FROM cases WHERE status=?", ("available" if s == "assigned" else "annotation_in_progress" if s == "in_progress" else "annotation_submitted" if s == "submitted" else s,)).fetchone()[0] for s in ("assigned", "in_progress", "submitted", "approved", "rejected")}
    recent = [dict(r) for r in con.execute("SELECT id,case_id,action,user_id,details,created_at FROM audit_events ORDER BY id DESC LIMIT 50").fetchall()]
    con.close()
    return {"users": result, "overview": overview, "recent_audit": recent}


@app.get("/admin/cases/source-root")
def case_source_root(admin=Depends(require_role("admin"))):
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    return {"path": str(CASES_DIR), "exists": CASES_DIR.is_dir()}


@app.post("/admin/cases/scan")
def scan_cases(admin=Depends(require_role("admin"))):
    """Index cases copied into SWTOOL_CASES_DIR; media remains untouched."""
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    con = db()
    created = updated = skipped = 0
    details = []
    try:
        for case_dir in _case_directories():
            case_id = case_dir.name
            if (not case_id or case_id in {".", ".."} or "/" in case_id or "\\" in case_id
                    or any(ord(ch) < 32 for ch in case_id)):
                skipped += 1
                continue
            scanned = _scan_case_directory(case_dir)
            if not scanned:
                skipped += 1
                continue
            title, metadata = scanned
            existing = con.execute("SELECT id,status,assigned_annotator_id,assigned_reviewer_id FROM cases WHERE id=?", (case_id,)).fetchone()
            if existing:
                con.execute("UPDATE cases SET title=?,metadata=? WHERE id=?", (title, json.dumps(metadata, ensure_ascii=False), case_id))
                updated += 1
            else:
                con.execute("INSERT INTO cases(id,title,metadata,status,created_at) VALUES(?,?,?,?,?)", (case_id, title, json.dumps(metadata, ensure_ascii=False), "available", time.time()))
                created += 1
            details.append({"case_id": case_id, "title": title, "frame_count": metadata.get("frame_count", 0), "video_files": metadata.get("video_files", [])})
        audit(con, admin["id"], "case_directory_scan", details=json.dumps({"created": created, "updated": updated, "skipped": skipped}, ensure_ascii=False))
        con.commit()
    finally:
        con.close()
    return {"source_root": str(CASES_DIR), "created": created, "updated": updated, "skipped": skipped, "cases": details}


@app.post("/cases")
def create_case(body: CaseIn, user=Depends(require_role("admin"))):
    con = db()
    try:
        _validate_assignment(con, body.assigned_annotator_id, body.assigned_reviewer_id)
        con.execute("INSERT INTO cases(id,title,metadata,status,assigned_reviewer_id,created_at,assigned_annotator_id) VALUES(?,?,?,?,?,?,?)", (body.case_id, body.title, json.dumps(body.metadata), "available", body.assigned_reviewer_id, time.time(), body.assigned_annotator_id)); con.commit()
    except DBIntegrityError as exc: raise HTTPException(409, "案例已存在") from exc
    finally: con.close()
    return {"case_id": body.case_id, "status": "available"}


@app.get("/cases")
def list_cases(user=Depends(current_user)):
    # Keep the task list current without making the user wait for a disk scan.
    _schedule_case_directory_sync()
    con = db(); roles = roles_of(user)
    # Keep the current user's active annotation/review work visible first,
    # then surface actionable rejected/available cases before the remainder.
    if "admin" in roles:
        priority_sql = "CASE WHEN status IN ('annotation_in_progress','annotation_submitted') THEN 0 WHEN status='rejected' THEN 1 WHEN status='available' THEN 2 ELSE 3 END"
        rows = con.execute("SELECT id,title,status,metadata,assigned_annotator_id,assigned_reviewer_id FROM cases ORDER BY " + priority_sql + " ASC, created_at DESC").fetchall()
    else:
        priority_sql = "CASE WHEN status='annotation_in_progress' AND assigned_annotator_id=? THEN 0 WHEN status='annotation_submitted' AND assigned_reviewer_id=? THEN 0 WHEN status='rejected' THEN 1 WHEN status='available' THEN 2 WHEN status='approved' THEN 3 ELSE 4 END"
        clauses, params = [], []
        if "annotator" in roles or "admin" in roles:
            clauses.append("((status IN ('available','rejected') AND (assigned_annotator_id IS NULL OR assigned_annotator_id=?)) OR assigned_annotator_id=? OR (status IN ('annotation_in_progress','rejected') AND assigned_annotator_id IS NOT NULL))")
            params.extend([user["id"], user["id"]])
        # 所有标注员都可以互相审核，审核任务按 assigned_reviewer_id 过滤。
        if "annotator" in roles or "admin" in roles: clauses.append("assigned_reviewer_id=?"); params.append(user["id"])
        # Keep completed cases available as reference material for users who
        # participated in the annotation or review workflow.
        if "annotator" in roles or "admin" in roles:
            clauses.append("status='approved' AND (assigned_annotator_id=? OR assigned_reviewer_id=?)")
            params.extend([user["id"], user["id"]])
        if clauses:
            params.extend([user["id"], user["id"]])
            rows = con.execute("SELECT id,title,status,metadata,assigned_annotator_id,assigned_reviewer_id FROM cases WHERE " + " OR ".join(clauses) + " ORDER BY " + priority_sql + " ASC, created_at DESC", params).fetchall()
        else:
            rows = []
    claimant_names = {row["id"]: row["username"] for row in con.execute("SELECT id,username FROM users").fetchall()}
    saved_progress = {}
    progress_rows = con.execute("SELECT case_id,segment_id,progress_json FROM annotation_projects ORDER BY updated_at DESC").fetchall()
    for row in progress_rows:
        saved_progress.setdefault(row['case_id'], {})[row['segment_id']] = json.loads(row['progress_json'])
    for case in rows:
        videos = json.loads(case['metadata'] or '{}').get('video_files') or []
        if videos:
            first = str(videos[0])
            latest = next((row for row in progress_rows if row['case_id'] == case['id'] and row['segment_id'] in {'default', first}), None)
            if latest:
                saved_progress[case['id']]['default'] = saved_progress[case['id']][first] = json.loads(latest['progress_json'])
    con.close()
    result=[]
    for r in rows:
        item=dict(r) | {"case_id": r["id"], "metadata": json.loads(r["metadata"])}
        original_metadata = item["metadata"]
        if "admin" not in roles:
            safe_video_metadata = {}
            raw_video_metadata = original_metadata.get("video_metadata")
            if isinstance(raw_video_metadata, dict):
                allowed_video_keys = {
                    "frame_count", "total_frames", "annotation_frames", "required_frames",
                    "annotate_frames", "must_label_frames", "frame_step", "stride",
                }
                for filename, details in raw_video_metadata.items():
                    if isinstance(details, dict):
                        safe_video_metadata[str(filename)] = {
                            key: value for key, value in details.items() if key in allowed_video_keys
                        }
            original_metadata = dict(original_metadata)
            original_metadata["video_metadata"] = safe_video_metadata
            item["anonymized_case_id"]=anonymized_case(r["id"]); item["title"]=item["anonymized_case_id"]
        item["metadata"] = _public_case_metadata(original_metadata, str(r["id"]))
        item["annotation_progress"] = saved_progress.get(r["id"], {})
        item["assigned_annotator_username"] = claimant_names.get(r["assigned_annotator_id"], "")
        item["annotation_locked"] = r["status"] in {"annotation_in_progress", "rejected"} and r["assigned_annotator_id"] is not None and r["assigned_annotator_id"] != user["id"]
        item["can_cancel_claim"] = r["status"] in {"annotation_in_progress", "rejected"} and r["assigned_annotator_id"] == user["id"]
        result.append(item)
    return result


def _visible_queue_cases(con, user):
    """Return the current user's visible cases without exposing other records."""
    roles = roles_of(user)
    if "admin" in roles:
        return con.execute("SELECT id,status,assigned_annotator_id,metadata FROM cases").fetchall()
    clauses, params = [], []
    if "annotator" in roles:
        clauses.append("((status IN ('available','rejected') AND (assigned_annotator_id IS NULL OR assigned_annotator_id=?)) OR assigned_annotator_id=?)")
        params.extend([user["id"], user["id"]])
        clauses.append("assigned_reviewer_id=?")
        params.append(user["id"])
        clauses.append("status='approved' AND (assigned_annotator_id=? OR assigned_reviewer_id=?)")
        params.extend([user["id"], user["id"]])
    if not clauses:
        return []
    return con.execute("SELECT id,status,assigned_annotator_id,metadata FROM cases WHERE " + " OR ".join(clauses), params).fetchall()


def _clean_queue_preferences(payload, rows, user_id):
    """Keep only visible IDs and rename only personally leased annotation work."""
    visible = {str(row["id"]): row for row in rows}
    def clean_ids(values, allowed):
        result, seen = [], set()
        for value in values if isinstance(values, list) else []:
            value = str(value).strip() if isinstance(value, str) else ""
            if value and value in allowed and value not in seen:
                result.append(value); seen.add(value)
        return result
    case_order = clean_ids(payload.case_order, visible)
    case_names, video_orders, video_names = {}, {}, {}
    for case_id, row in visible.items():
        try: metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError): metadata = {}
        videos = [str(v) for v in metadata.get("video_files", []) if isinstance(v, str)]
        for item in metadata.get("dicom_series", []) if isinstance(metadata.get("dicom_series"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str): videos.append(item["id"])
        for item in metadata.get("volume_series", []) if isinstance(metadata.get("volume_series"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str): videos.append(item["id"])
        allowed_videos = set(videos)
        order = clean_ids(payload.video_orders.get(case_id, []), allowed_videos)
        if order: video_orders[case_id] = order
        owned = row["assigned_annotator_id"] == user_id and row["status"] in {"annotation_in_progress", "rejected"}
        if not owned:
            continue
        name = payload.case_names.get(case_id)
        if isinstance(name, str) and len(name.strip()) <= 120 and name.strip():
            case_names[case_id] = name.strip()
        names = payload.video_names.get(case_id, {})
        if isinstance(names, dict):
            safe = {str(video): value.strip() for video, value in names.items()
                    if str(video) in allowed_videos and isinstance(value, str) and value.strip() and len(value.strip()) <= 120}
            if safe: video_names[case_id] = safe
    return {"case_order": case_order, "case_names": case_names, "video_orders": video_orders, "video_names": video_names}


@app.get("/queue-preferences")
def get_queue_preferences(user=Depends(current_user)):
    con = db()
    try:
        row = con.execute("SELECT queue_json FROM queue_preferences WHERE user_id=?", (user["id"],)).fetchone()
        if not row:
            return {"case_order": [], "case_names": {}, "video_orders": {}, "video_names": {}}
        try: raw = json.loads(row["queue_json"])
        except (TypeError, json.JSONDecodeError): raw = {}
        return {key: raw.get(key, default) for key, default in (("case_order", []), ("case_names", {}), ("video_orders", {}), ("video_names", {}))}
    finally:
        con.close()


@app.put("/queue-preferences")
def put_queue_preferences(body: QueuePreferencesIn, user=Depends(current_user)):
    con = db()
    try:
        clean = _clean_queue_preferences(body, _visible_queue_cases(con, user), user["id"])
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        con.execute("INSERT INTO queue_preferences(user_id,queue_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET queue_json=excluded.queue_json,updated_at=excluded.updated_at", (user["id"], encoded, time.time()))
        audit(con, user["id"], "queue_preferences_update", details=json.dumps({"case_order": len(clean["case_order"]), "renamed_cases": len(clean["case_names"]), "video_orders": len(clean["video_orders"]), "renamed_videos": sum(len(v) for v in clean["video_names"].values())}, ensure_ascii=False))
        con.commit()
        return clean
    finally:
        con.close()


@app.get("/reviewers")
def list_reviewers(search: str = "", user=Depends(current_user)):
    con = db()
    rows = con.execute(
        "SELECT id,username FROM users WHERE status='active' AND (roles LIKE ? OR role=?) AND id<>? AND username LIKE ? ORDER BY username LIMIT 20",
            ("%annotator%", "admin", user["id"], f"%{search.strip()}%"),
    ).fetchall()
    con.close()
    return [{"user_id": r["id"], "username": r["username"]} for r in rows]


@app.post("/cases/{case_id}/lease")
def lease(case_id: str, reviewer_id: int | None = None, user=Depends(require_role("annotator"))):
    con = db(); case = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case: raise HTTPException(404, "案例不存在")
    now = time.time()
    if case["assigned_annotator_id"] not in (None, user["id"]):
        con.close(); raise HTTPException(403, "该案例已分配给其他标注员")
    if case["status"] not in {"available", "annotation_in_progress", "rejected"}:
        con.close(); raise HTTPException(409, "案例当前状态不可领取")
    old = con.execute("SELECT * FROM leases WHERE case_id=?", (case_id,)).fetchone()
    if old and old["expires_at"] > now and old["annotator_id"] != user["id"]: con.close(); raise HTTPException(409, "案例正在被其他标注员占用")
    if reviewer_id is not None:
        reviewer = con.execute("SELECT id,roles,role,status FROM users WHERE id=?", (reviewer_id,)).fetchone()
        if not reviewer or reviewer_id == user["id"] or reviewer["status"] != "active" or not ({"annotator", "admin"} & roles_of(reviewer)):
            con.close(); raise HTTPException(400, "审核员无效，必须选择其他标注员")
        assigned_reviewer = reviewer_id
    else:
        existing = con.execute("SELECT id FROM users WHERE id=? AND status='active' AND (roles LIKE ? OR role=?)", (case["assigned_reviewer_id"], "%annotator%", "admin")).fetchone()
        reviewer = con.execute("SELECT u.id FROM reviewer_map m JOIN users u ON u.id=m.reviewer_id WHERE m.annotator_id=? AND u.status='active' AND (u.roles LIKE ? OR u.role=?)", (user["id"], "%annotator%", "admin")).fetchone()
        fallback = con.execute("SELECT id FROM users WHERE id<>? AND status='active' AND (roles LIKE ? OR role=?) ORDER BY RANDOM() LIMIT 1", (user["id"], "%annotator%", "admin")).fetchone()
        assigned_reviewer = (existing["id"] if existing and existing["id"] != user["id"] else None) or (reviewer["id"] if reviewer and reviewer["id"] != user["id"] else None) or (fallback["id"] if fallback else None)
    if assigned_reviewer is None:
        con.close(); raise HTTPException(409, "暂无可用审核员，请稍后重试")
    raw = secrets.token_urlsafe(32)
    reviewer_row = con.execute("SELECT username FROM users WHERE id=?", (assigned_reviewer,)).fetchone()
    draft_submission = con.execute("SELECT id FROM submissions WHERE case_id=? AND version='server-draft'", (case_id,)).fetchone()
    if not draft_submission:
        con.execute("INSERT INTO submissions(case_id,annotator_id,reviewer_id,version,sha256,path,status) VALUES(?,?,?,?,?,?,?)", (case_id, user["id"], assigned_reviewer, "server-draft", "", str(_segment_draft_dir(case_id)), "draft"))
    con.execute("INSERT INTO leases(case_id,annotator_id,token_hash,expires_at) VALUES(?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET annotator_id=excluded.annotator_id, token_hash=excluded.token_hash, expires_at=excluded.expires_at", (case_id, user["id"], hashlib.sha256(raw.encode()).hexdigest(), now + 3600)); con.execute("UPDATE cases SET status='annotation_in_progress',assigned_annotator_id=?,assigned_reviewer_id=? WHERE id=?", (user["id"], assigned_reviewer, case_id)); audit(con, user["id"], "lease", case_id); con.commit(); con.close()
    return {"case_id": case_id, "lease_token": raw, "expires_at": now + 3600, "reviewer": {"user_id": assigned_reviewer, "username": reviewer_row[0] if reviewer_row else ""}, "source": {"frames_url": f"/cases/{case_id}/frames", "metadata": _public_case_metadata(json.loads(case["metadata"] or "{}"), case_id)}}


def check_lease(con, case_id, user_id, token):
    row = con.execute("SELECT * FROM leases WHERE case_id=?", (case_id,)).fetchone()
    if not row or row["annotator_id"] != user_id or row["expires_at"] < time.time() or not hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest()): raise HTTPException(403, "租约无效或已过期")


def require_active_annotation_case(con, case_id: str, user_id: int, token: str):
    """Validate owner, active case state and lease while the caller holds its case lock."""
    now = time.time()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    case = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if case is None:
        raise HTTPException(404, "案例不存在")
    lease = con.execute("SELECT * FROM leases WHERE case_id=?", (case_id,)).fetchone()
    if not lease or lease["annotator_id"] != user_id or lease["expires_at"] < now or not hmac.compare_digest(lease["token_hash"], token_hash):
        raise HTTPException(403, "租约无效或已过期")
    if case["assigned_annotator_id"] != user_id or case["status"] not in {"annotation_in_progress", "rejected"}:
        raise HTTPException(409, "案例已提交、审核或不再由当前标注员处理")
    return case


def _active_draft_path(case_id: str) -> Path:
    con = db()
    try:
        case = con.execute("SELECT metadata FROM cases WHERE id=?", (case_id,)).fetchone()
    finally:
        con.close()
    if case is None:
        raise HTTPException(404, "案例不存在")
    try:
        metadata = json.loads(case["metadata"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    case_root = _case_root(case_id, metadata)
    if case_root is None:
        raise HTTPException(400, "病例目录无效")
    return case_root / "drafts" / "active_annotation.json"


def _segment_draft_dir(case_id: str) -> Path:
    return _annotation_case_dir(case_id) / "segments"


def _annotation_case_dir(case_id: str) -> Path:
    return ANNOTATION_DRAFTS_DIR / hashlib.sha256(case_id.encode("utf-8")).hexdigest()


def _segment_draft_path(case_id: str, segment_id: str) -> Path:
    return _segment_draft_dir(case_id) / (hashlib.sha256(segment_id.encode("utf-8")).hexdigest() + ".json")


def _read_segment_drafts(case_id: str) -> dict:
    legacy = _active_draft_path(case_id)
    segments = {}
    legacy_time = 0
    if legacy.is_file():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            segments = payload.get("segments") or {}
            legacy_time = legacy.stat().st_mtime_ns
            if not isinstance(segments, dict):
                raise ValueError("segments")
        except (OSError, UnicodeDecodeError, ValueError, AttributeError) as exc:
            raise HTTPException(409, "旧版服务器草稿不可读取") from exc
    directories = {_segment_draft_dir(case_id), legacy.parent / "segments"}
    latest = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            modified = path.stat().st_mtime_ns
            # A successful full transfer/save supersedes older segment files.
            if modified <= legacy_time:
                continue
            try:
                raw = path.read_bytes()
                if raw.startswith(b"\x1f\x8b"):
                    raw = gzip.decompress(raw)
                value = json.loads(raw.decode("utf-8"))
                source, segment = value['segment_id'], value['data']
                if not isinstance(source, str) or not isinstance(segment, dict):
                    raise ValueError("segment")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, OSError, EOFError, zlib.error) as exc:
                raise HTTPException(409, "服务器子任务草稿不可读取") from exc
            if modified > latest.get(source, (0, None))[0]:
                latest[source] = (modified, segment)
    segments.update({source: segment for source, (_, segment) in latest.items()})
    return segments


def _write_segment_draft(case_id: str, segment_id: str, segment: dict, preserve_previous: bool = True) -> tuple[Path, Path | None, bytes]:
    try:
        raw = json.dumps({"segment_id": segment_id, "data": segment}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "草稿必须是 JSON 对象") from exc
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "子任务草稿不能超过 20MB")
    target = _segment_draft_path(case_id, segment_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_suffix(".backup")
    if backup.exists():
        raise HTTPException(409, "上次草稿恢复尚未完成，请联系管理员")
    had_previous = target.is_file()
    try:
        if preserve_previous and had_previous:
            os.link(target, backup)
            _fsync_directory(target.parent)
        compressed = gzip.compress(raw, compresslevel=1, mtime=0)
        _write_bytes_atomically(target, compressed, ".segment_annotation.")
    except OSError as exc:
        if backup.is_file():
            os.replace(backup, target)
        elif not had_previous:
            target.unlink(missing_ok=True)
        raise HTTPException(503, "服务器子任务草稿不可写") from exc
    return target, backup if preserve_previous and backup.exists() else None, raw


def _write_active_draft(case_id: str, payload: dict):
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "草稿必须是 JSON 对象") from exc
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "草稿不能超过 20MB")
    target = _active_draft_path(case_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".active_annotation.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return raw
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _case_draft_lock(case_id: str):
    """Serialize all draft and submission changes for one case across workers."""
    lock_dir = _annotation_case_dir(case_id)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".active_annotation.lock"
    try:
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise HTTPException(503, "病例草稿当前不可操作，请稍后重试") from exc


def _fsync_directory(directory: Path):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_atomically(target: Path, raw: bytes, prefix: str):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _snapshot_active_draft(case_id: str) -> tuple[Path, Path | None]:
    """Persist the old draft so a failed database transaction can restore it."""
    target = _active_draft_path(case_id)
    backup = target.parent / ".active_annotation.backup"
    try:
        if backup.exists():
            raise HTTPException(409, "上次草稿恢复尚未完成，请联系管理员")
        if not target.is_file():
            return target, None
        _write_bytes_atomically(backup, target.read_bytes(), ".active_annotation.backup.")
        return target, backup
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(409, "服务端草稿不可读取，无法安全保存") from exc


def _restore_active_draft(snapshot: tuple[Path, Path | None]):
    """Restore the durable backup, retaining it when recovery itself fails."""
    target, backup = snapshot
    try:
        if backup is None:
            target.unlink(missing_ok=True)
        elif not backup.is_file():
            raise OSError("草稿备份不存在")
        else:
            os.replace(backup, target)
    except OSError as exc:
        LOGGER.exception("草稿补偿失败: %s", target)
        raise HTTPException(500, "草稿补偿失败，请联系管理员") from exc


def _discard_draft_backup(snapshot: tuple[Path, Path | None]):
    _, backup = snapshot
    if backup is None:
        return
    try:
        backup.unlink(missing_ok=True)
    except OSError:
        LOGGER.exception("已提交草稿的备份清理失败")


def _remove_active_draft(case_id: str):
    _active_draft_path(case_id).unlink(missing_ok=True)
    legacy_directory = _active_draft_path(case_id).parent / "segments"
    for directory in {legacy_directory, _segment_draft_dir(case_id)}:
        if directory.exists():
            shutil.rmtree(directory)


def _handoff_reviewer(con, previous_reviewer_id: int | None, target_annotator_id: int) -> int | None:
    if previous_reviewer_id is not None:
        existing = con.execute("SELECT id,roles,role,status FROM users WHERE id=?", (previous_reviewer_id,)).fetchone()
        if existing and existing["id"] != target_annotator_id and existing["status"] == "active" and ({"annotator", "admin"} & roles_of(existing)):
            return existing["id"]
    mapped = con.execute("SELECT u.id,u.roles,u.role,u.status FROM reviewer_map m JOIN users u ON u.id=m.reviewer_id WHERE m.annotator_id=? AND u.status='active'", (target_annotator_id,)).fetchone()
    if mapped and mapped["id"] != target_annotator_id and mapped["status"] == "active" and ({"annotator", "admin"} & roles_of(mapped)):
        return mapped["id"]
    fallback = con.execute("SELECT id FROM users WHERE id<>? AND status='active' AND (roles LIKE ? OR role=?) ORDER BY RANDOM() LIMIT 1", (target_annotator_id, "%annotator%", "admin")).fetchone()
    return fallback["id"] if fallback else None


@app.put("/cases/{case_id}/draft")
def put_draft(case_id: str, body: DraftIn, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    started = time.monotonic()
    try:
        payload_bytes = -1 if body.metadata_only else len(json.dumps({"segment_id": body.segment_id, "payload": body.payload}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        payload_bytes = -1
    con = db()
    try:
        with _case_draft_lock(case_id):
            case = require_active_annotation_case(con, case_id, user["id"], lease_token)
            if body.metadata_only:
                if body.segment_id is None:
                    raise HTTPException(400, "服务器草稿保存必须指定子任务")
                segments = body.payload.get("segments")
                if not isinstance(segments, dict) or set(segments) != {body.segment_id} or not isinstance(segments[body.segment_id], dict):
                    raise HTTPException(400, "单视频保存必须且只能包含指定视频段")
                registered = con.execute("SELECT id FROM submissions WHERE case_id=? AND version='server-draft'", (case_id,)).fetchone()
                target, backup, raw_segment = _write_segment_draft(case_id, body.segment_id, segments[body.segment_id], preserve_previous=not bool(registered))
                try:
                    if not registered:
                        con.execute("INSERT INTO submissions(case_id,annotator_id,reviewer_id,version,sha256,path,status) VALUES(?,?,?,?,?,?,?)", (case_id, user["id"], dict(case).get("assigned_reviewer_id"), "server-draft", hashlib.sha256(raw_segment).hexdigest(), str(target.parent), "draft"))
                        audit(con, user["id"], "annotation_draft_save", case_id, "server draft registered")
                        con.commit()
                except Exception:
                    con.rollback()
                    if backup is not None and backup.is_file():
                        os.replace(backup, target)
                    else:
                        target.unlink(missing_ok=True)
                    raise
                _discard_draft_backup((target, backup))
                elapsed = round((time.monotonic() - started) * 1000, 2)
                LOGGER.info("annotation_draft_save_success case_id=%s segment_id=%s user_id=%s bytes=%s duration_ms=%s", case_id, body.segment_id, user["id"], len(raw_segment), elapsed)
                return {"case_id": case_id, "saved": True, "duration_ms": elapsed}
            snapshot = _snapshot_active_draft(case_id)
            try:
                payload = body.payload
                if body.segment_id is not None:
                    segments = payload.get("segments")
                    if not isinstance(segments, dict) or set(segments) != {body.segment_id}:
                        raise HTTPException(400, "单视频保存必须且只能包含指定视频段")
                    path = _active_draft_path(case_id)
                    previous = json.loads(path.read_text()) if path.is_file() else {}
                    if not isinstance(previous, dict):
                        raise HTTPException(409, "既有草稿格式无效，请联系管理员恢复")
                    previous_segments = _read_segment_drafts(case_id)
                    if not isinstance(previous_segments, dict):
                        previous_segments = {}
                    payload = {**previous, **payload, "segments": {**previous_segments, **segments}}
                version = None
                if body.segment_id is not None and not body.metadata_only:
                    from server.annotation_project import geometry_warnings, progress
                    segment = segments[body.segment_id]
                    try:
                        warnings = geometry_warnings(segment)
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise HTTPException(400, str(exc)) from exc
                    if warnings and not body.accept_geometry_warnings:
                        _discard_draft_backup(snapshot)
                        LOGGER.info("annotation_draft_save_rejected case_id=%s segment_id=%s user_id=%s bytes=%s status=200 duration_ms=%s warnings=%s", case_id, body.segment_id, user["id"], payload_bytes, round((time.monotonic() - started) * 1000), len(warnings))
                        return {"case_id": case_id, "saved": False, "geometry_warnings": warnings}
                    metadata = json.loads(dict(case).get('metadata') or '{}')
                    source = body.segment_id
                    if source == 'default' and metadata.get('video_files'):
                        source = str(metadata['video_files'][0])
                    stats = progress(segment, metadata, source)
                    now = time.time()
                    project = {**segment, 'saved_by': user['id'], 'saved_at': now}
                    con.execute("INSERT INTO annotation_projects(case_id,segment_id,payload_json,progress_json,updated_by,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(case_id,segment_id) DO UPDATE SET payload_json=excluded.payload_json,progress_json=excluded.progress_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (case_id, body.segment_id, json.dumps(project, ensure_ascii=False), json.dumps(stats), user['id'], now))
                _write_active_draft(case_id, payload)
                audit(con, user["id"], "annotation_draft_save", case_id)
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    LOGGER.exception("草稿保存失败后的数据库回滚失败: %s", case_id)
                _restore_active_draft(snapshot)
                raise
            _discard_draft_backup(snapshot)
            LOGGER.info("annotation_draft_save_success case_id=%s segment_id=%s user_id=%s bytes=%s duration_ms=%s", case_id, body.segment_id or "", user["id"], payload_bytes, round((time.monotonic() - started) * 1000))
            return {"case_id": case_id, "saved": True, "duration_ms": round((time.monotonic() - started) * 1000, 2), **({"progress": stats} if body.segment_id is not None and not body.metadata_only else {})}
    except HTTPException as exc:
        LOGGER.warning("annotation_draft_save_failure case_id=%s segment_id=%s user_id=%s bytes=%s status=%s duration_ms=%s error=%s", case_id, body.segment_id or "", user["id"], payload_bytes, exc.status_code, round((time.monotonic() - started) * 1000), exc.detail)
        raise
    except Exception:
        LOGGER.exception("annotation_draft_save_failure case_id=%s segment_id=%s user_id=%s bytes=%s duration_ms=%s", case_id, body.segment_id or "", user["id"], payload_bytes, round((time.monotonic() - started) * 1000))
        raise
    finally:
        con.close()


@app.get("/cases/{case_id}/draft")
def get_draft(case_id: str, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    con = db()
    try:
        check_lease(con, case_id, user["id"], lease_token)
        segments = _read_segment_drafts(case_id)
        if segments:
            return {"case_id": case_id, "payload": {"case_id": case_id, "segments": segments}}
        path = _active_draft_path(case_id)
        if not path.is_file():
            return {"case_id": case_id, "payload": None}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "服务端草稿不可读取") from exc
        if not isinstance(payload, dict):
            raise HTTPException(409, "服务端草稿格式无效")
        return {"case_id": case_id, "payload": payload}
    finally:
        con.close()


@app.put("/cases/{case_id}/annotations")
def save_annotations(case_id: str, body: DraftIn, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    """Commit a complete segment; success requires a database version and progress."""
    if body.segment_id is None:
        raise HTTPException(400, "保存勾画必须指定视频或序列")
    return put_draft(case_id, body, lease_token, user)


@app.get("/cases/{case_id}/annotations")
def get_annotations(case_id: str, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    """Restore committed database versions, independently of later draft backups."""
    con = db()
    try:
        check_lease(con, case_id, user["id"], lease_token)
        case = con.execute("SELECT assigned_annotator_id,status,metadata FROM cases WHERE id=?", (case_id,)).fetchone()
        if not case or case["assigned_annotator_id"] != user["id"] or case["status"] not in {"annotation_in_progress", "rejected"}:
            raise HTTPException(403, "当前用户不可编辑此病例")
        rows = con.execute("SELECT segment_id,payload_json,progress_json,updated_at FROM annotation_projects WHERE case_id=? ORDER BY updated_at DESC,segment_id", (case_id,)).fetchall()
        segments, stats = {}, {}
        for row in rows:
            if row["segment_id"] not in segments:
                segments[row["segment_id"]] = json.loads(row["payload_json"])
                stats[row["segment_id"]] = json.loads(row["progress_json"])
        if not segments:
            return {"case_id": case_id, "payload": None, "progress": {}}
        metadata = json.loads(case["metadata"] or "{}")
        videos = metadata.get("video_files") or []
        if videos:
            # The old empty video selection and the first named video are one source.
            first = str(videos[0])
            latest_first = next((row for row in rows if row["segment_id"] in {"default", first}), None)
            if latest_first:
                data = json.loads(latest_first["payload_json"])
                progress = json.loads(latest_first["progress_json"])
                segments[first] = segments["default"] = data
                stats[first] = stats["default"] = progress
        selected = rows[0]["segment_id"]
        payload = {**segments[selected], "case_id": case_id, "segments": segments, "currentVideo": "" if selected == "default" else selected}
        return {"case_id": case_id, "payload": payload, "progress": stats}
    finally:
        con.close()


@app.post("/cases/{case_id}/cancel-claim")
def cancel_case_claim(case_id: str, user=Depends(require_role("annotator"))):
    """Release case ownership and every child lease without changing annotation data."""
    con = db()
    try:
        with _case_draft_lock(case_id):
            cursor = con.execute("UPDATE cases SET status='available',assigned_annotator_id=NULL,assigned_reviewer_id=NULL WHERE id=? AND assigned_annotator_id=? AND status IN ('annotation_in_progress','rejected')", (case_id, user["id"]))
            if cursor.rowcount != 1:
                con.rollback()
                case = con.execute("SELECT assigned_annotator_id,status FROM cases WHERE id=?", (case_id,)).fetchone()
                if not case:
                    raise HTTPException(404, "案例不存在")
                if case["assigned_annotator_id"] != user["id"]:
                    raise HTTPException(403, "仅本人领取的病例可取消选取")
                raise HTTPException(409, "该病例当前状态不可取消选取")
            con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
            audit(con, user["id"], "annotation_cancel_claim", case_id)
            con.commit()
            return {"case_id": case_id, "status": "available", "assigned_annotator_id": None}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.post("/cases/{case_id}/release")
def release_case(case_id: str, body: DraftIn, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    con = db()
    try:
        with _case_draft_lock(case_id):
            require_active_annotation_case(con, case_id, user["id"], lease_token)
            snapshot = _snapshot_active_draft(case_id)
            try:
                _write_active_draft(case_id, body.payload)
                con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
                con.execute("UPDATE cases SET status='available',assigned_annotator_id=NULL,assigned_reviewer_id=NULL WHERE id=?", (case_id,))
                audit(con, user["id"], "annotation_release", case_id)
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    LOGGER.exception("退出病例失败后的数据库回滚失败: %s", case_id)
                _restore_active_draft(snapshot)
                raise
            _discard_draft_backup(snapshot)
            return {"case_id": case_id, "status": "available"}
    finally:
        con.close()


@app.post("/cases/{case_id}/handoff")
def handoff_case(case_id: str, body: HandoffIn, lease_token: Annotated[str, Header()], user=Depends(require_role("annotator"))):
    con = db()
    try:
        with _case_draft_lock(case_id):
            case = require_active_annotation_case(con, case_id, user["id"], lease_token)
            target = con.execute("SELECT id,roles,role,status FROM users WHERE id=?", (body.target_annotator_id,)).fetchone()
            if not target or target["id"] == user["id"] or target["status"] != "active" or not ({"annotator", "admin"} & roles_of(target)):
                raise HTTPException(400, "接手标注员无效")
            reviewer_id = _handoff_reviewer(con, case["assigned_reviewer_id"], target["id"])
            if reviewer_id is None:
                raise HTTPException(409, "暂无可用审核员")
            snapshot = _snapshot_active_draft(case_id)
            try:
                _write_active_draft(case_id, body.draft)
                con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
                con.execute("UPDATE cases SET status='annotation_in_progress',assigned_annotator_id=?,assigned_reviewer_id=? WHERE id=?", (target["id"], reviewer_id, case_id))
                audit(con, user["id"], "annotation_handoff", case_id, json.dumps({"target_annotator_id": target["id"], "reviewer_id": reviewer_id}))
                con.commit()
            except Exception:
                try:
                    con.rollback()
                except Exception:
                    LOGGER.exception("转交病例失败后的数据库回滚失败: %s", case_id)
                _restore_active_draft(snapshot)
                raise
            _discard_draft_backup(snapshot)
            return {"case_id": case_id, "status": "annotation_in_progress", "assigned_annotator_id": target["id"], "assigned_reviewer_id": reviewer_id}
    finally:
        con.close()


def _case_source(case_id: str, video: str = "") -> Path | None:
    """Locate server-side frame directory or uploaded video without exposing paths."""
    # Prefer the recorded relative source path. This preserves nested folders and
    # non-ASCII names; fall back to the legacy case_id directory for old rows.
    source_relative = ""
    con = db()
    row = con.execute("SELECT metadata FROM cases WHERE id=?", (case_id,)).fetchone()
    con.close()
    if row:
        try:
            source_relative = str((json.loads(row["metadata"] or "{}") or {}).get("source_relative") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            source_relative = ""
    case_root = CASES_DIR / case_id
    if source_relative:
        relative = Path(source_relative)
        try:
            candidate_root = (CASES_DIR / relative).resolve()
            cases_root = CASES_DIR.resolve()
            if candidate_root == cases_root or cases_root not in candidate_root.parents:
                candidate_root = None
        except OSError:
            candidate_root = None
        if candidate_root is not None and candidate_root.exists():
            case_root = candidate_root
    candidates = [case_root / "frames"]
    if case_root.is_dir():
        candidates.extend(p for p in case_root.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".avi", ".mov", ".mkv", ".webm"})
    # Folder-upload sessions are keyed by case_id; never scan another case's files.
    con = db()
    session = con.execute(
        "SELECT id FROM upload_sessions WHERE case_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    con.close()
    if session:
        root = DATA_DIR / "uploads" / session["id"]
        candidates.append(root / "frames")
        if root.is_dir():
            candidates.extend(
                p for p in root.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp4", ".avi", ".mov", ".mkv"}
            )
    existing = [p for p in candidates if p.exists()]
    if video:
        # Only allow selecting a filename from this case's immediate source set.
        requested = Path(video).name
        if requested != video or requested in {"", ".", ".."}:
            return None
        return next((p for p in existing if p.is_file() and p.name == requested), None)
    return next((p for p in existing if p.is_dir()), None) or next((p for p in existing if p.is_file()), None)


def _case_root(case_id: str, metadata: dict) -> Path | None:
    """Resolve a recorded case root and reject path traversal before file access."""
    relative = Path(str(metadata.get("source_relative") or case_id))
    try:
        root = (CASES_DIR / relative).resolve()
        base = CASES_DIR.resolve()
    except OSError:
        return None
    if root == base or base not in root.parents or not root.is_dir():
        return None
    return root


def _dicom_slice_path(case_id: str, metadata: dict, series_id: str, frame: int) -> Path:
    if frame < 0:
        raise HTTPException(400, "CT 切片序号无效")
    if metadata.get("imaging_type") != "ct_dicom":
        raise HTTPException(409, "当前病例不是 CT DICOM 序列")
    series = next((item for item in metadata.get("dicom_series", []) if isinstance(item, dict) and item.get("id") == series_id), None)
    if not series or not isinstance(series.get("_files"), list):
        root = _case_root(case_id, metadata)
        rescanned = _scan_case_directory(root) if root is not None else None
        refreshed = rescanned[1] if rescanned else {}
        series = next((item for item in refreshed.get("dicom_series", []) if isinstance(item, dict) and item.get("id") == series_id), None)
        # Metadata may be stale after an administrator re-scans or replaces a
        # CT case.  Keep the request safe but recover from an empty/old series
        # id by using the first valid sequence discovered on disk.
        if not series and not series_id:
            series = next((item for item in refreshed.get("dicom_series", []) if isinstance(item, dict) and isinstance(item.get("_files"), list) and item.get("_files")), None)
        if not series and series_id:
            series = next((item for item in refreshed.get("dicom_series", []) if isinstance(item, dict) and isinstance(item.get("_files"), list) and item.get("_files")), None)
    if not series:
        raise HTTPException(404, "CT 序列不存在")
    files = series.get("_files")
    if not isinstance(files, list) or frame >= len(files):
        raise HTTPException(404, "CT 切片序号超出范围")
    rel = Path(str(files[frame].get("path") if isinstance(files[frame], dict) else ""))
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(409, "CT 序列文件记录无效")
    root = _case_root(case_id, metadata)
    if root is None:
        raise HTTPException(404, "CT 病例源目录不存在")
    try:
        path = (root / rel).resolve()
    except OSError as exc:
        raise HTTPException(409, "CT 切片路径无效") from exc
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "CT 切片文件不存在")
    return path


def _ct_window(window_center: float | None, window_width: float | None, default_center: float = 40.0, default_width: float = 400.0) -> tuple[float, float]:
    center = float(window_center) if window_center is not None else default_center
    width = float(window_width) if window_width is not None else default_width
    if not -2000 <= center <= 3000 or not 1 <= width <= 8000:
        raise HTTPException(400, "CT 窗宽窗位参数超出允许范围")
    return center, width


def _mpr_render_size(shape, row_spacing: float, column_spacing: float):
    height, width = shape
    if row_spacing <= 0 or column_spacing <= 0:
        return width, height
    unit = min(row_spacing, column_spacing)
    height = max(1, round(height * row_spacing / unit))
    width = max(1, round(width * column_spacing / unit))
    scale = min(1.0, 1600 / max(height, width))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _mpr_preview_index(index: int, axis: int, info: dict) -> int:
    original_size, preview_size = info["original_shape"][axis], info["preview_shape"][axis]
    if index < 0 or index >= original_size:
        raise HTTPException(404, "CT 重建位置超出范围")
    if info.get("patient_resampled"):
        return min(preview_size - 1, round(index * max(0, preview_size - 1) / max(1, original_size - 1)))
    return min(preview_size - 1, index // max(1, info["strides"][axis]))


def _render_ct_hu_jpeg(hu, window_center: float | None, window_width: float | None, default_center: float = 40.0, default_width: float = 400.0, invert: bool = False, row_spacing: float = 1.0, column_spacing: float = 1.0, native_window: bool = False, source_resolution: bool = False) -> bytes:
    """Encode only a windowed preview; never expose DICOM bytes or tags."""
    try:
        import colorsys
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(503, "CT 图像编码组件未安装") from exc
    center, width = (_nifti_window(window_center, window_width, default_center, default_width) if native_window
                     else _ct_window(window_center, window_width, default_center, default_width))
    low, high = center - width / 2.0, center + width / 2.0
    image = np.clip((hu - low) * 255.0 / (high - low), 0, 255).astype(np.uint8)
    if invert:
        image = 255 - image
    rendered = Image.fromarray(image, mode="L")
    # Reformat planes use millimetre spacing so anisotropic CT is not displayed
    # as an anatomically misleading square-pixel image.  Scale both axes from
    # the same physical unit; limiting one axis alone would distort anatomy.
    if not source_resolution and row_spacing > 0 and column_spacing > 0 and image.ndim == 2:
        target_size = _mpr_render_size(image.shape, row_spacing, column_spacing)
        if target_size != rendered.size:
            rendered = rendered.resize(target_size, Image.Resampling.BILINEAR)
    encoded = BytesIO()
    rendered.save(encoded, format="JPEG", quality=92)
    return encoded.getvalue()


def _ct_oblique_projection_geometry(shape, spacing_z: float, spacing_y: float, spacing_x: float):
    depth, height, width = shape
    shift_x = min(max(1, round((depth - 1) * spacing_z / max(spacing_x, 0.01) * 0.42)), max(1, round(width * 0.30))) if depth > 1 else 0
    shift_y = min(max(1, round((depth - 1) * spacing_z / max(spacing_y, 0.01) * 0.30)), max(1, round(height * 0.24))) if depth > 1 else 0
    return shift_x, shift_y, (height + shift_y, width + shift_x)


def _ct_normal_mip_reconstruction(volume):
    """Render the conventional axial-aligned maximum-intensity reconstruction.

    Keep every source voxel at its original x/y coordinate.  The former
    oblique preview deliberately shifted each depth plane, which created
    doubled edges and ghosting for annotated spine CT volumes.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise HTTPException(503, "CT 重建依赖 numpy 未安装") from exc
    source = np.asarray(volume, dtype=np.float32)
    if source.ndim != 3 or not source.size:
        raise HTTPException(422, "CT 重建源数据无效")
    return np.max(source, axis=0)


def _ct_normal_label_projection(volume):
    """Project labels onto the same unshifted normal reconstruction grid."""
    try:
        import numpy as np
    except ImportError as exc:
        raise HTTPException(503, "CT 重建依赖 numpy 未安装") from exc
    source = np.asarray(volume)
    if source.ndim != 3 or not source.size:
        raise HTTPException(422, "参考勾画源数据无效")
    return np.max(source, axis=0)


def _ct_oblique_mip_reconstruction(volume, spacing_z: float, spacing_y: float, spacing_x: float, window_center: float = 40.0, window_width: float = 400.0):
    """Render a bone focused, front to back transparent CT navigation preview.

    The returned grayscale is display intensity (0..255), not HU.  Air and
    soft tissue contribute no opacity, so the depth stack cannot collapse
    into two bright slabs as it does with a maximum intensity projection.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise HTTPException(503, "CT 重建依赖 numpy 未安装") from exc
    depth, height, width = volume.shape
    shift_x, shift_y, output_shape = _ct_oblique_projection_geometry(volume.shape, spacing_z, spacing_y, spacing_x)
    # Non-HU predictor uploads also occur.  Use their upper tail for the same
    # tissue selection without turning a normalized 0..1 volume black.
    finite = np.asarray(volume)[np.isfinite(volume)]
    threshold = window_center + window_width * 0.30
    scale = max(window_width, 200.0)
    if finite.size and (float(finite.max()) < 20 or float(finite.min()) > 3000):
        threshold = float(np.percentile(finite, 75))
        scale = max(float(np.percentile(finite, 99)) - threshold, 1e-6)
    radiance = np.zeros(output_shape, dtype=np.float32)
    transmittance = np.ones(output_shape, dtype=np.float32)
    # Each layer is deliberately light: a long 128-slice ray retains depth,
    # while a short bone segment is still visible after display gamma.
    layer_opacity = min(0.13, 0.13 * math.sqrt(32.0 / max(depth, 1)))
    for index in range(depth - 1, -1, -1):
        plane = np.asarray(volume[index], dtype=np.float32)
        x_offset = round(index * shift_x / max(1, depth - 1))
        y_offset = shift_y - round(index * shift_y / max(1, depth - 1))
        density = np.clip((np.nan_to_num(plane, nan=threshold, posinf=threshold, neginf=threshold) - threshold) / scale, 0.0, 1.0)
        opacity = density * layer_opacity
        target_light = radiance[y_offset:y_offset + height, x_offset:x_offset + width]
        target_trans = transmittance[y_offset:y_offset + height, x_offset:x_offset + width]
        depth_shade = 0.72 + 0.28 * index / max(depth - 1, 1)
        target_light += target_trans * opacity * (0.55 + 0.45 * density) * depth_shade
        target_trans *= 1.0 - opacity
    return np.sqrt(np.clip(radiance, 0.0, 1.0)) * 255.0


def _ct_oblique_label_projection(volume, spacing_z: float, spacing_y: float, spacing_x: float):
    """Project labels with precisely the CT preview's screen coordinates."""
    import numpy as np

    depth, height, width = volume.shape
    shift_x, shift_y, output_shape = _ct_oblique_projection_geometry(volume.shape, spacing_z, spacing_y, spacing_x)
    output = np.zeros(output_shape, dtype=volume.dtype)
    for index, plane in enumerate(volume):
        x_offset = round(index * shift_x / max(1, depth - 1))
        y_offset = shift_y - round(index * shift_y / max(1, depth - 1))
        target = output[y_offset:y_offset + height, x_offset:x_offset + width]
        np.maximum(target, plane, out=target)
    return output


def _render_ct_slice(path: Path, window_center: float | None, window_width: float | None) -> bytes:
    if pydicom is None:
        raise HTTPException(503, "CT DICOM 解码组件未安装，请安装 pydicom 后重启服务")
    try:
        import numpy as np
        dataset = pydicom.dcmread(str(path), force=True)
        if str(getattr(dataset, "Modality", "")).upper() != "CT":
            raise ValueError("文件不是 CT DICOM")
        if int(_dicom_number(getattr(dataset, "NumberOfFrames", None), 1.0)) > 1:
            raise ValueError("暂不支持多帧 CT DICOM")
        if str(getattr(dataset, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.2.1":
            raise ValueError("暂不支持 Enhanced CT DICOM")
        pixels = dataset.pixel_array.astype(np.float32)
    except Exception as exc:
        raise HTTPException(422, f"无法解码 CT 切片: {exc}") from exc
    slope = _dicom_number(getattr(dataset, "RescaleSlope", None), 1.0)
    intercept = _dicom_number(getattr(dataset, "RescaleIntercept", None), 0.0)
    hu = pixels * slope + intercept
    return _render_ct_hu_jpeg(
        hu, window_center, window_width,
        _dicom_number(getattr(dataset, "WindowCenter", None), 40.0),
        max(_dicom_number(getattr(dataset, "WindowWidth", None), 400.0), 1.0),
        str(getattr(dataset, "PhotometricInterpretation", "")).upper() == "MONOCHROME1",
    )


def _ct_series_for_mpr(case_id: str, metadata: dict, series_id: str) -> tuple[dict, Path]:
    """Return private series bookkeeping only after source-root validation."""
    root = _case_root(case_id, metadata)
    if root is None:
        raise HTTPException(404, "CT 病例源目录不存在")
    series = next((item for item in metadata.get("dicom_series", []) if isinstance(item, dict) and item.get("id") == series_id), None)
    has_files = isinstance(series.get("_files"), list) and bool(series.get("_files")) if series else False
    has_dimensions = bool(series) and int(series.get("rows") or 0) > 0 and int(series.get("columns") or 0) > 0
    # Historical records may retain private source paths but predate the MPR
    # geometry fields.  Refresh those records from the validated case root
    # before deciding whether reconstruction is available.
    if not series or not has_files or not has_dimensions:
        rescanned = _scan_case_directory(root)
        refreshed = rescanned[1] if rescanned else {}
        candidates = refreshed.get("dicom_series", []) if isinstance(refreshed.get("dicom_series"), list) else []
        series = next((item for item in candidates if isinstance(item, dict) and item.get("id") == series_id), None)
        if not series and not series_id:
            series = next((item for item in candidates if isinstance(item, dict) and isinstance(item.get("_files"), list) and item.get("_files")), None)
    has_files = isinstance(series.get("_files"), list) and bool(series.get("_files")) if series else False
    has_dimensions = bool(series) and int(series.get("rows") or 0) > 0 and int(series.get("columns") or 0) > 0
    if not series or not has_files:
        raise HTTPException(404, "CT 序列不存在")
    if not has_dimensions:
        raise HTTPException(422, "CT 序列缺少有效图像尺寸，无法进行多平面重建")
    return series, root


def _ct_preview_strides(shape: tuple[int, int, int]) -> tuple[int, int, int]:
    """Choose near-isotropic source strides without exceeding preview budget."""
    count = max(1, math.prod(shape))
    scale = max(1, math.ceil((count / CT_MPR_MAX_VOXELS) ** (1.0 / 3.0)))
    strides = [scale, scale, scale]
    while math.prod(math.ceil(size / stride) for size, stride in zip(shape, strides)) > CT_MPR_MAX_VOXELS:
        # Increase the stride with the largest remaining preview dimension.
        axis = max(range(3), key=lambda index: shape[index] / strides[index])
        strides[axis] += 1
    return tuple(strides)


def _ct_is_axis_aligned(orientation: tuple[float, ...], tolerance: float = 1e-3) -> bool:
    """Accept signed axis permutations, including 90-degree in-plane rotations."""
    row, column = orientation[:3], orientation[3:]
    normal = (
        row[1] * column[2] - row[2] * column[1],
        row[2] * column[0] - row[0] * column[2],
        row[0] * column[1] - row[1] * column[0],
    )
    vectors = (row, column, normal)
    dominant = [max(range(3), key=lambda index: abs(vector[index])) for vector in vectors]
    return len(set(dominant)) == 3 and all(abs(vector[axis]) >= 1.0 - tolerance for vector, axis in zip(vectors, dominant))


def _ct_oblique_patient_preview(volume, origin: tuple[float, float, float], orientation: tuple[float, ...], spacing: tuple[float, float, float], max_voxels=None, background=-1024.0):
    """Resample a bounded source preview into orthogonal patient coordinates."""
    try:
        import numpy as np
        if len(orientation) != 6:
            raise ValueError("invalid image orientation")
        source = np.asarray(volume, dtype=np.float32)
        if source.ndim != 3 or not all(size > 0 for size in source.shape):
            raise ValueError("invalid source volume")
        row = np.asarray(orientation[:3], dtype=np.float64)
        column = np.asarray(orientation[3:], dtype=np.float64)
        normal = (
            row[1] * column[2] - row[2] * column[1],
            row[2] * column[0] - row[0] * column[2],
            row[0] * column[1] - row[1] * column[0],
        )
        normal = np.asarray(normal, dtype=np.float64)
        row_norm, column_norm, normal_norm = np.linalg.norm(row), np.linalg.norm(column), np.linalg.norm(normal)
        source_spacing = np.asarray(spacing, dtype=np.float64)
        source_origin = np.asarray(origin, dtype=np.float64)
        if (
            not np.isfinite(row).all()
            or not np.isfinite(column).all()
            or not np.isfinite(source_origin).all()
            or not np.isfinite(source_spacing).all()
            or min(row_norm, column_norm, normal_norm, *source_spacing) <= 0
        ):
            raise ValueError("invalid CT geometry")
        row /= row_norm
        column /= column_norm
        normal /= normal_norm
        basis = np.column_stack((row * source_spacing[0], column * source_spacing[1], normal * source_spacing[2]))
        inverse_basis = np.linalg.inv(basis)
        source_size = np.asarray((source.shape[2], source.shape[1], source.shape[0]), dtype=np.float64)
        source_corners = np.asarray([
            source_origin + basis @ np.asarray((x, y, z), dtype=np.float64)
            for x in (0.0, max(0.0, source_size[0] - 1.0))
            for y in (0.0, max(0.0, source_size[1] - 1.0))
            for z in (0.0, max(0.0, source_size[2] - 1.0))
        ])
        lower = source_corners.min(axis=0)
        upper = source_corners.max(axis=0)
        extent = np.maximum(0.01, upper - lower)
        output_spacing = max(0.01, float(source_spacing.min()))
        while True:
            output_size = tuple(max(1, int(math.ceil(length / output_spacing)) + 1) for length in extent)
            voxel_budget = CT_MPR_MAX_VOXELS if max_voxels is None else max_voxels
            if math.prod(output_size) <= voxel_budget:
                break
            output_spacing *= (math.prod(output_size) / voxel_budget) ** (1.0 / 3.0) * 1.001

        if sitk is not None:
            image = sitk.GetImageFromArray(source)
            image.SetOrigin(tuple(source_origin))
            image.SetSpacing(tuple(source_spacing))
            # SimpleITK direction is row-major; its columns are source x/y/z patient axes.
            image.SetDirection(tuple(np.column_stack((row, column, normal)).reshape(-1)))
            rebuilt = sitk.Resample(
                image, output_size, sitk.Transform(), sitk.sitkLinear,
                tuple(lower), (output_spacing,) * 3, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                background, sitk.sitkFloat32,
            )
            return sitk.GetArrayFromImage(rebuilt), (output_spacing, output_spacing, output_spacing)

        # Dependency-free fallback for navigation only. Work in small blocks so a
        # bounded output volume never also creates a full-volume coordinate array.
        output_x, output_y, output_z = output_size
        rebuilt = np.full((output_z, output_y, output_x), background, dtype=np.float32)
        maximum_block_voxels = 1_000_000
        block_z = min(output_z, 16)
        block_x = min(output_x, 512)
        block_y = max(1, min(output_y, maximum_block_voxels // max(1, block_z * block_x)))
        for z_start in range(0, output_z, block_z):
            z_stop = min(output_z, z_start + block_z)
            z_values = lower[2] + np.arange(z_start, z_stop, dtype=np.float64) * output_spacing
            for y_start in range(0, output_y, block_y):
                y_stop = min(output_y, y_start + block_y)
                y_values = lower[1] + np.arange(y_start, y_stop, dtype=np.float64) * output_spacing
                for x_start in range(0, output_x, block_x):
                    x_stop = min(output_x, x_start + block_x)
                    x_values = lower[0] + np.arange(x_start, x_stop, dtype=np.float64) * output_spacing
                    physical = np.empty((z_stop - z_start, y_stop - y_start, x_stop - x_start, 3), dtype=np.float64)
                    physical[..., 0] = x_values[None, None, :]
                    physical[..., 1] = y_values[None, :, None]
                    physical[..., 2] = z_values[:, None, None]
                    coordinates = np.einsum("ij,...j->...i", inverse_basis, physical - source_origin)
                    nearest = np.rint(coordinates).astype(np.int64)
                    valid = (
                        (nearest[..., 0] >= 0) & (nearest[..., 0] < source.shape[2])
                        & (nearest[..., 1] >= 0) & (nearest[..., 1] < source.shape[1])
                        & (nearest[..., 2] >= 0) & (nearest[..., 2] < source.shape[0])
                    )
                    target = rebuilt[z_start:z_stop, y_start:y_stop, x_start:x_stop]
                    target[valid] = source[
                        nearest[..., 2][valid], nearest[..., 1][valid], nearest[..., 0][valid]
                    ]
        return rebuilt, (output_spacing, output_spacing, output_spacing)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("CT oblique MPR resampling failed: %s", type(exc).__name__)
        raise HTTPException(422, "CT斜位重建失败，仍可轴位浏览") from None


def _nifti_window(center, width, default_center, default_width) -> tuple[float, float]:
    center = float(default_center if center is None else center)
    width = float(default_width if width is None else width)
    if not math.isfinite(center) or not math.isfinite(width) or abs(center) > 1e12 or not 0 < width <= 1e12:
        raise HTTPException(400, "影像窗宽窗位参数无效")
    return center, width


def _nifti_series_source(case_id: str, metadata: dict, series_id: str):
    if nib is None:
        raise HTTPException(503, "NIfTI 解码组件 nibabel 未安装")
    root = _case_root(case_id, metadata)
    if root is None:
        raise HTTPException(404, "病例源目录不存在")
    items = metadata.get("volume_series") or []
    source_series = next((item for item in items if isinstance(item, dict)
                          and (not series_id or item.get("id") == series_id)), None)
    if source_series is None or not source_series.get("_image_file"):
        scanned = _scan_case_directory(root)
        items = scanned[1].get("volume_series", []) if scanned else []
        source_series = next((item for item in items if not series_id or item.get("id") == series_id), None)
    if source_series is None:
        raise HTTPException(404, "影像序列不存在")
    try:
        path = _nifti_source_path(root, str(source_series["_image_file"]))
        labels = metadata.get("label_files", [])
        labels = list(labels) if isinstance(labels, list) else []
        if metadata.get("label_file"):
            labels.append(metadata["label_file"])
        if any(path == _nifti_source_path(root, str(label)) for label in labels):
            raise ValueError("label is not an image source")
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
        with _NIFTI_SOURCE_LOCK:
            cached = _NIFTI_SOURCE_CACHE.get(str(path))
            if cached and cached[0] == signature:
                image, geometry = cached[1:]
            else:
                image = nib.load(str(path), mmap=True, keep_file_open=True)
                geometry = _nifti_geometry(image)
                while len(_NIFTI_SOURCE_CACHE) >= 2:
                    _NIFTI_SOURCE_CACHE.pop(next(iter(_NIFTI_SOURCE_CACHE)))
                _NIFTI_SOURCE_CACHE[str(path)] = (signature, image, geometry)
        return source_series, image, geometry, path
    except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError):
        raise HTTPException(422, "原始 NIfTI 影像或患者空间信息无效") from None


def _nifti_mpr_volume(case_id: str, metadata: dict, series_id: str):
    """Keep only bounded previews cached; read scaled original planes lazily."""
    import numpy as np
    source_series, image, geometry, path = _nifti_series_source(case_id, metadata, series_id)
    stat_info = path.stat()
    signature = (stat_info.st_size, stat_info.st_mtime_ns)
    cache_key = "nifti:" + str(path)
    # Serializing preview builds also bounds transient allocations when the
    # browser requests several MPR planes simultaneously.
    with _CT_MPR_CACHE_LOCK:
        now = time.monotonic()
        cached = _CT_MPR_CACHE.get(cache_key)
        if cached and cached[1] == signature and now - cached[0] < CT_MPR_CACHE_TTL_SECONDS:
            return cached[2], copy.deepcopy(cached[3])
        original_shape = tuple(reversed(geometry["shape"]))
        strides = list(_ct_preview_strides(original_shape))
        while math.prod(math.ceil(size / stride) for size, stride in zip(original_shape, strides)) > 16_000_000:
            axis = max(range(3), key=lambda index: original_shape[index] / strides[index])
            strides[axis] += 1
        preview_shape = tuple(math.ceil(size / stride) for size, stride in zip(original_shape, strides))
        volume = np.empty(preview_shape, dtype=np.float32)
        sampled = list(enumerate(range(0, original_shape[0], strides[0])))
        if geometry["orientation"][geometry["axes"][2], 1] < 0:
            sampled.reverse()
        with _NIFTI_SOURCE_LOCK:
            for target, source in sampled:
                volume[target] = _nifti_axial_pixels(image, geometry, source)[::strides[1], ::strides[2]]
        spacing = geometry["spacing"] * np.asarray(tuple(reversed(strides)))
        directions = geometry["affine"][:3, :3] / geometry["spacing"]
        orientation = tuple(directions[:, 0]) + tuple(directions[:, 1])
        patient_resampled = not _ct_is_axis_aligned(orientation)
        if patient_resampled:
            volume, spacing = _ct_oblique_patient_preview(
                volume, tuple(geometry["affine"][:3, 3]), orientation, tuple(spacing),
                max_voxels=16_000_000, background=0.0 if source_series["modality"] == "MR" else -1024.0)
            # The oblique helper obeys the shared voxel budget. Native caches
            # retain at most 64 MB, regardless of shared DICOM configuration.
            if volume.nbytes > 64_000_000:
                raise HTTPException(422, "斜位 NIfTI 预览超过内存预算，仍可原分辨率切片标注")
        info = {"window_center": source_series["window_center"], "window_width": source_series["window_width"],
                "spacing_x": float(spacing[0]), "spacing_y": float(spacing[1]), "spacing_z": float(spacing[2]),
                "invert": False, "original_shape": original_shape, "preview_shape": tuple(volume.shape),
                "strides": tuple(strides), "patient_resampled": patient_resampled,
                "source_affine": source_series["source_affine"], "frame_affine_lps": source_series["frame_affine_lps"]}
        expired = [key for key, value in _CT_MPR_CACHE.items() if now - value[0] >= CT_MPR_CACHE_TTL_SECONDS]
        for key in expired:
            _CT_MPR_CACHE.pop(key, None)
        while _CT_MPR_CACHE and (len(_CT_MPR_CACHE) >= CT_MPR_CACHE_ENTRIES or
                sum(getattr(value[2], "nbytes", 0) for value in _CT_MPR_CACHE.values()) + volume.nbytes > 64_000_000):
            oldest = min(_CT_MPR_CACHE, key=lambda key: _CT_MPR_CACHE[key][0])
            _CT_MPR_CACHE.pop(oldest, None)
        _CT_MPR_CACHE[cache_key] = (time.monotonic(), signature, volume, copy.deepcopy(info))
        return volume, info


def _ct_mpr_volume(case_id: str, metadata: dict, series_id: str):
    """Build one bounded navigation preview while leaving source axial slices intact."""
    if metadata.get("imaging_type") in {"ct_nifti", "mri_nifti"}:
        return _nifti_mpr_volume(case_id, metadata, series_id)
    if pydicom is None:
        raise HTTPException(503, "CT DICOM 解码组件未安装，请安装 pydicom 后重启服务")
    try:
        import numpy as np
    except ImportError as exc:
        raise HTTPException(503, "CT 重建依赖 numpy 未安装") from exc
    series, root = _ct_series_for_mpr(case_id, metadata, series_id)
    files = series["_files"]
    rows, columns = int(series.get("rows") or 0), int(series.get("columns") or 0)
    if rows <= 0 or columns <= 0:
        raise HTTPException(422, "CT 序列缺少有效图像尺寸，无法进行多平面重建")
    original_shape = (len(files), rows, columns)
    strides = _ct_preview_strides(original_shape)
    signature = _dicom_tree_signature(root)
    cache_key = f"{root}:{series.get('id', '')}"
    while True:
        now = time.monotonic()
        with _CT_MPR_CACHE_LOCK:
            cached = _CT_MPR_CACHE.get(cache_key)
            if cached and cached[1] == signature and now - cached[0] < CT_MPR_CACHE_TTL_SECONDS:
                return cached[2], copy.deepcopy(cached[3])
            ready = _CT_MPR_BUILDING.get(cache_key)
            if ready is None:
                ready = threading.Event()
                _CT_MPR_BUILDING[cache_key] = ready
                builder = True
            else:
                builder = False
        if builder:
            break
        # A simultaneous coronal/sagittal/MIP request shares one bounded
        # decoder.  A failed build clears its event, then a waiter retries.
        ready.wait(timeout=30)

    try:
        sampled_indices = list(range(0, len(files), strides[0]))
        if sampled_indices[-1] != len(files) - 1:
            sampled_indices.append(len(files) - 1)
        volume = np.empty((len(sampled_indices), math.ceil(rows / strides[1]), math.ceil(columns / strides[2])), dtype=np.float32)
        first_orientation = None
        first_position = None
        positions: list[float] = []
        spacing_x = spacing_y = None
        invert = False
        orientation_tolerance = 1e-3
        sample_position = 0
        for slice_index, entry in enumerate(files):
            rel = Path(str(entry.get("path") if isinstance(entry, dict) else ""))
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                raise ValueError("invalid relative source path")
            path = (root / rel).resolve()
            if root not in path.parents or not path.is_file():
                raise ValueError("missing source slice")
            dataset = pydicom.dcmread(str(path), force=True)
            if (
                str(getattr(dataset, "Modality", "")).upper() != "CT"
                or int(_dicom_number(getattr(dataset, "NumberOfFrames", None), 1.0)) > 1
                or str(getattr(dataset, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.2.1"
            ):
                raise ValueError("not a conventional single-frame CT slice")
            try:
                orientation = tuple(float(getattr(dataset, "ImageOrientationPatient")[index]) for index in range(6))
                position = tuple(float(getattr(dataset, "ImagePositionPatient")[index]) for index in range(3))
                pixel_spacing = getattr(dataset, "PixelSpacing")
                current_spacing_y, current_spacing_x = float(pixel_spacing[0]), float(pixel_spacing[1])
            except (AttributeError, IndexError, TypeError, ValueError) as exc:
                raise ValueError("missing image geometry") from exc
            row, column = orientation[:3], orientation[3:]
            normal = (
                row[1] * column[2] - row[2] * column[1],
                row[2] * column[0] - row[0] * column[2],
                row[0] * column[1] - row[1] * column[0],
            )
            normal_length = math.sqrt(sum(v * v for v in normal))
            normal = tuple(v / normal_length for v in normal)
            if first_orientation is None:
                first_orientation = orientation
                spacing_x, spacing_y = current_spacing_x, current_spacing_y
                first_position = position
            elif (
                any(abs(left - right) > orientation_tolerance for left, right in zip(first_orientation, orientation))
                or abs(current_spacing_x - spacing_x) > max(0.001, spacing_x * 0.01)
                or abs(current_spacing_y - spacing_y) > max(0.001, spacing_y * 0.01)
            ):
                raise ValueError("inconsistent orientation or pixel spacing")
            positions.append(sum(position[index] * normal[index] for index in range(3)))
            if slice_index in sampled_indices:
                pixels = dataset.pixel_array
                if getattr(pixels, "shape", None) != (rows, columns):
                    raise ValueError("inconsistent slice dimensions")
                preview = pixels[::strides[1], ::strides[2]]
                volume[sample_position, :preview.shape[0], :preview.shape[1]] = preview.astype(np.float32) * _dicom_number(getattr(dataset, "RescaleSlope", None), 1.0) + _dicom_number(getattr(dataset, "RescaleIntercept", None), 0.0)
                sample_position += 1
            invert = str(getattr(dataset, "PhotometricInterpretation", "")).upper() == "MONOCHROME1"

        if len(positions) > 1:
            intervals = [right - left for left, right in zip(positions, positions[1:])]
            if any(interval <= 0 for interval in intervals):
                raise ValueError("slice positions are not strictly ordered")
            sorted_intervals = sorted(intervals)
            spacing_z = sorted_intervals[len(sorted_intervals) // 2]
            tolerance = max(0.05, spacing_z * 0.02)
            if any(abs(interval - spacing_z) > tolerance for interval in intervals):
                raise ValueError("inconsistent slice spacing")
        else:
            spacing_z = max(_dicom_number(series.get("spacing_z"), 1.0), 0.01)
        preview_spacing = (
            max(spacing_x or 1.0, 0.01) * strides[2],
            max(spacing_y or 1.0, 0.01) * strides[1],
            max(spacing_z, 0.01) * strides[0],
        )
        patient_resampled = not _ct_is_axis_aligned(first_orientation or ())
        if patient_resampled:
            volume, preview_spacing = _ct_oblique_patient_preview(volume, first_position, first_orientation, preview_spacing)
        render_info = {
            "window_center": _dicom_number(series.get("window_center"), 40.0),
            "window_width": max(_dicom_number(series.get("window_width"), 400.0), 1.0),
            "spacing_x": preview_spacing[0],
            "spacing_y": preview_spacing[1],
            "spacing_z": preview_spacing[2],
            "invert": invert,
            "original_shape": original_shape,
            "preview_shape": tuple(int(value) for value in volume.shape),
            "strides": strides,
            "patient_resampled": patient_resampled,
        }
        with _CT_MPR_CACHE_LOCK:
            current = time.monotonic()
            expired = [key for key, value in _CT_MPR_CACHE.items() if current - value[0] >= CT_MPR_CACHE_TTL_SECONDS]
            for key in expired:
                _CT_MPR_CACHE.pop(key, None)
            while len(_CT_MPR_CACHE) >= CT_MPR_CACHE_ENTRIES:
                oldest = min(_CT_MPR_CACHE, key=lambda key: _CT_MPR_CACHE[key][0])
                _CT_MPR_CACHE.pop(oldest, None)
            _CT_MPR_CACHE[cache_key] = (current, signature, volume, copy.deepcopy(render_info))
        return volume, render_info
    except HTTPException:
        raise
    except Exception:
        LOGGER.exception("CT MPR build failed for case=%s series=%s", case_id, series_id)
        raise HTTPException(422, "不支持该序列 MPR，仍可轴位浏览") from None
    finally:
        with _CT_MPR_CACHE_LOCK:
            _CT_MPR_BUILDING.pop(cache_key, None)
            ready.set()


def _case_preview_metadata(case_id: str, user: dict) -> dict:
    """Apply the same read-only queue visibility rules to frame and MPR JPEGs."""
    con = db()
    exists = con.execute("SELECT id,assigned_annotator_id,assigned_reviewer_id,status,metadata FROM cases WHERE id=?", (case_id,)).fetchone()
    lease = con.execute("SELECT annotator_id,expires_at FROM leases WHERE case_id=?", (case_id,)).fetchone()
    con.close()
    if not exists:
        raise HTTPException(404, "案例不存在")
    roles = roles_of(user)
    if "admin" not in roles and exists["status"] in {"annotation_in_progress", "rejected"} and exists["assigned_annotator_id"] not in {None, user["id"]}:
        raise HTTPException(403, "该病例已由其他标注员领取，不可进入标注页面")
    reviewer_access = exists["assigned_reviewer_id"] == user["id"] and exists["status"] == "annotation_submitted"
    preview_access = (
        exists["status"] in {"available", "rejected"}
        or exists["assigned_annotator_id"] == user["id"]
        or exists["assigned_reviewer_id"] == user["id"]
    )
    if "admin" not in roles and not preview_access and not (lease and lease["annotator_id"] == user["id"] and lease["expires_at"] > time.time()) and not reviewer_access:
        raise HTTPException(403, "请先领取案例")
    try:
        return json.loads(exists["metadata"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


@app.get("/cases/{case_id}/frames/{frame}")
def case_frame(case_id: str, frame: int, video: str = "", series: str = "", window_center: float | None = None, window_width: float | None = None, user=Depends(current_user)):
    if frame < 0:
        raise HTTPException(400, "帧号无效")
    metadata = _case_preview_metadata(case_id, user)
    if metadata.get("imaging_type") in {"ct_nifti", "mri_nifti"}:
        source_series, image, geometry, _ = _nifti_series_source(case_id, metadata, series)
        with _NIFTI_SOURCE_LOCK:
            pixels = _nifti_axial_pixels(image, geometry, frame)
        return Response(content=_render_ct_hu_jpeg(
            pixels, window_center, window_width, source_series["window_center"], source_series["window_width"],
            native_window=True, source_resolution=True), media_type="image/jpeg")
    if metadata.get("imaging_type") == "ct_dicom":
        available_series = metadata.get("dicom_series") if isinstance(metadata.get("dicom_series"), list) else []
        requested_series = series or str(available_series[0].get("id", "") if available_series else "")
        path = _dicom_slice_path(case_id, metadata, requested_series, frame)
        return Response(content=_render_ct_slice(path, window_center, window_width), media_type="image/jpeg")
    source = _case_source(case_id, video)
    if not source: raise HTTPException(404, "案例暂无可读取帧")
    if source.is_dir():
        files = sorted(p for p in source.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"})
        if frame >= len(files): raise HTTPException(404, "帧号超出范围")
        return FileResponse(files[frame])
    try:
        import cv2
        cap = cv2.VideoCapture(str(source)); cap.set(cv2.CAP_PROP_POS_FRAMES, frame); ok, image = cap.read(); cap.release()
        if not ok: raise HTTPException(404, "帧号超出范围")
        ok, encoded = cv2.imencode('.jpg', image)
        if not ok: raise HTTPException(500, "帧编码失败")
        return Response(content=encoded.tobytes(), media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, f"无法读取视频帧: {exc}") from exc


def _nifti_reference_source(case_id: str, metadata: dict, series: str):
    if metadata.get("imaging_type") not in {"ct_nifti", "mri_nifti"}:
        raise HTTPException(404, "病例没有原始 NIfTI 参考勾画")
    source_series, image, image_geometry, _ = _nifti_series_source(case_id, metadata, series)
    labels = metadata.get("label_files", [])
    labels = [str(value) for value in labels] if isinstance(labels, list) else []
    if metadata.get("label_file"):
        labels.append(str(metadata["label_file"]))
    labels = list(dict.fromkeys(labels))
    if not labels:
        raise HTTPException(404, "病例没有原始参考勾画")
    root = _case_root(case_id, metadata)
    if root is None:
        raise HTTPException(404, "病例源目录不存在")

    label_relative = _reference_label_for_series(str(source_series.get("_image_file", "")), labels)
    if label_relative is None:
        raise HTTPException(409, "参考勾画无法唯一匹配当前影像序列")

    try:
        import numpy as np
        label_path = _nifti_source_path(root, label_relative)
        label_image = nib.load(str(label_path), mmap=True, keep_file_open=True)
        label_geometry = _nifti_geometry(label_image)
        if (tuple(label_geometry["source_shape"]) != tuple(image_geometry["source_shape"])
                or not np.allclose(label_geometry["source_affine_mm"], image_geometry["source_affine_mm"], rtol=1e-6, atol=1e-5)
                or tuple(label_geometry["axes"]) != tuple(image_geometry["axes"])
                or not np.array_equal(label_geometry["orientation"], image_geometry["orientation"])):
            raise HTTPException(422, "参考勾画与原始影像空间不匹配")
        return label_image, label_geometry, label_path
    except HTTPException:
        raise
    except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError):
        raise HTTPException(422, "原始 NIfTI 参考勾画无效") from None


def _reference_contour_response(plane, target_size=None):
    import colorsys
    import numpy as np
    from PIL import Image
    try:
        if (not np.isfinite(plane).all() or not np.equal(plane, np.floor(plane)).all()
                or np.any(plane < 0) or np.any(plane > 65535)):
            raise HTTPException(422, "参考勾画类别值无效")
        if target_size is not None:
            plane = np.asarray(Image.fromarray(plane.astype(np.uint16)).resize(target_size, Image.Resampling.NEAREST))
        rgba = np.zeros((*plane.shape, 4), dtype=np.uint8)
        classes = np.unique(plane[plane != 0])
        if classes.size > 256:
            raise HTTPException(422, "参考勾画类别数量过多")
        for value in classes:
            mask = plane == value
            padded = np.pad(mask, 1, constant_values=False)
            interior = (padded[1:-1, :-2] & padded[1:-1, 2:]
                        & padded[:-2, 1:-1] & padded[2:, 1:-1])
            boundary = mask & ~interior
            hue = (int(value) * 0.618033988749895) % 1.0
            red, green, blue = colorsys.hsv_to_rgb(hue, 0.9, 1.0)
            rgba[boundary] = (round(red * 255), round(green * 255), round(blue * 255), 235)
        buffer = BytesIO()
        Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
        return Response(content=buffer.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "private, no-store"})
    except HTTPException:
        raise
    except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError):
        raise HTTPException(422, "原始 NIfTI 参考勾画无效") from None


@app.get("/cases/{case_id}/reference-labels/{frame}")
def case_reference_label(case_id: str, frame: int, series: str = "", user=Depends(current_user)):
    """Return a native NIfTI reference contour aligned to the axial image plane."""
    if frame < 0:
        raise HTTPException(400, "切片号无效")
    metadata = _case_preview_metadata(case_id, user)
    label_image, geometry, _ = _nifti_reference_source(case_id, metadata, series)
    with _NIFTI_SOURCE_LOCK:
        plane = _nifti_axial_pixels(label_image, geometry, frame)
    return _reference_contour_response(plane)


def _nifti_reference_mpr_volume(case_id: str, metadata: dict, series: str, info: dict):
    import numpy as np
    if info.get("patient_resampled"):
        raise HTTPException(409, "斜位 NIfTI 参考勾画暂仅支持原分辨率轴位")
    label_image, geometry, path = _nifti_reference_source(case_id, metadata, series)
    stat_info = path.stat()
    signature = (stat_info.st_size, stat_info.st_mtime_ns, tuple(info["strides"]), tuple(info["preview_shape"]),
                 tuple(geometry["source_affine_mm"].ravel()))
    cache_key = str(path)
    with _CT_MPR_CACHE_LOCK:
        now = time.monotonic()
        cached = _NIFTI_LABEL_MPR_CACHE.get(cache_key)
        if cached and cached[1] == signature and now - cached[0] < CT_MPR_CACHE_TTL_SECONDS:
            return cached[2]
        if math.prod(info["preview_shape"]) > 16_000_000:
            raise HTTPException(422, "参考勾画预览超过内存预算")
        volume = np.empty(info["preview_shape"], dtype=np.uint16)
        strides = info["strides"]
        sampled = list(enumerate(range(0, info["original_shape"][0], strides[0])))
        if geometry["orientation"][geometry["axes"][2], 1] < 0:
            sampled.reverse()
        classes = set()
        with _NIFTI_SOURCE_LOCK:
            for target, source in sampled:
                plane = _nifti_axial_pixels(label_image, geometry, source)[::strides[1], ::strides[2]]
                if (not np.isfinite(plane).all() or not np.equal(plane, np.floor(plane)).all()
                        or np.any(plane < 0) or np.any(plane > 65535)):
                    raise HTTPException(422, "参考勾画类别值无效")
                classes.update(np.unique(plane).tolist())
                if len(classes - {0}) > 256:
                    raise HTTPException(422, "参考勾画类别数量过多")
                volume[target] = plane
        for key, value in list(_NIFTI_LABEL_MPR_CACHE.items()):
            if now - value[0] >= CT_MPR_CACHE_TTL_SECONDS:
                _NIFTI_LABEL_MPR_CACHE.pop(key, None)
        while _NIFTI_LABEL_MPR_CACHE and (len(_NIFTI_LABEL_MPR_CACHE) >= CT_MPR_CACHE_ENTRIES or
                sum(value[2].nbytes for value in _NIFTI_LABEL_MPR_CACHE.values()) + volume.nbytes > 32_000_000):
            oldest = min(_NIFTI_LABEL_MPR_CACHE, key=lambda key: _NIFTI_LABEL_MPR_CACHE[key][0])
            _NIFTI_LABEL_MPR_CACHE.pop(oldest, None)
        _NIFTI_LABEL_MPR_CACHE[cache_key] = (time.monotonic(), signature, volume)
        return volume


@app.get("/cases/{case_id}/reference-labels/mpr/{plane}/{index}")
def case_reference_label_mpr(case_id: str, plane: str, index: int, series: str = "", user=Depends(current_user)):
    if plane not in {"coronal", "sagittal", "mip", "recon"}:
        raise HTTPException(404, "不支持的参考勾画重建视图")
    if index < 0:
        raise HTTPException(400, "CT 重建位置无效")
    metadata = _case_preview_metadata(case_id, user)
    if metadata.get("imaging_type") not in {"ct_nifti", "mri_nifti"}:
        raise HTTPException(404, "病例没有原始 NIfTI 参考勾画")
    _, info = _nifti_mpr_volume(case_id, metadata, series)
    volume = _nifti_reference_mpr_volume(case_id, metadata, series, info)
    if plane == "coronal":
        mask = volume[:, _mpr_preview_index(index, 1, info), :]
        spacing = info["spacing_z"], info["spacing_x"]
    elif plane == "sagittal":
        mask = volume[:, :, _mpr_preview_index(index, 2, info)]
        spacing = info["spacing_z"], info["spacing_y"]
    else:
        mask = _ct_normal_label_projection(volume)
        spacing = info["spacing_y"], info["spacing_x"]
    return _reference_contour_response(mask, _mpr_render_size(mask.shape, *spacing))


@app.get("/cases/{case_id}/mpr/{plane}/{index}")
def case_ct_mpr(case_id: str, plane: str, index: int, series: str = "", window_center: float | None = None, window_width: float | None = None, user=Depends(current_user)):
    """Serve a read-only MPR JPEG for authenticated viewers of a CT case."""
    if plane not in {"coronal", "sagittal", "mip", "recon"}:
        raise HTTPException(404, "不支持的 CT 重建视图")
    if index < 0:
        raise HTTPException(400, "CT 重建位置无效")
    metadata = _case_preview_metadata(case_id, user)
    native = metadata.get("imaging_type") in {"ct_nifti", "mri_nifti"}
    if metadata.get("imaging_type") != "ct_dicom" and not native:
        raise HTTPException(409, "当前病例不是支持的 CT/MRI 体影像")
    available_series = metadata.get("volume_series" if native else "dicom_series") or []
    requested_series = series or str(available_series[0].get("id", "") if available_series else "")
    volume, info = _ct_mpr_volume(case_id, metadata, requested_series)
    original_shape = tuple(int(value) for value in info["original_shape"])
    preview_shape = tuple(int(value) for value in info["preview_shape"])
    if native and info.get("patient_resampled") and plane in {"coronal", "sagittal"}:
        raise HTTPException(409, "斜位 NIfTI 暂不支持十字定位重建；请使用原分辨率轴位切片标注")

    if plane == "coronal":
        mapped = _mpr_preview_index(index, 1, info)
        hu, row_spacing, column_spacing = volume[:, mapped, :], info["spacing_z"], info["spacing_x"]
    elif plane == "sagittal":
        mapped = _mpr_preview_index(index, 2, info)
        hu, row_spacing, column_spacing = volume[:, :, mapped], info["spacing_z"], info["spacing_y"]
    else:
        center, width = (_nifti_window(window_center, window_width, info["window_center"], info["window_width"]) if native
                         else _ct_window(window_center, window_width, info["window_center"], info["window_width"]))
        hu = _ct_normal_mip_reconstruction(volume)
        row_spacing, column_spacing = info["spacing_y"], info["spacing_x"]
    return Response(
        content=_render_ct_hu_jpeg(hu, window_center, window_width, info["window_center"], info["window_width"], bool(info["invert"]) if plane in {"coronal", "sagittal"} else False, row_spacing, column_spacing, native_window=native),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=10",
            "X-CT-MPR-Plane": plane,
            "X-CT-MPR-Preview": "source=%dx%dx%d;preview=%dx%dx%d;stride=%dx%dx%d" % (*original_shape, *preview_shape, *info["strides"]),
        },
    )


class InferenceIn(BaseModel):
    frame: int = Field(ge=0)
    organ: str = Field(min_length=1, max_length=80)
    prompts: list[dict] = Field(min_length=1)
    video: str = ""
    series: str = ""
    model_version: str | None = None
    window_center: float | None = Field(default=None, ge=-2000, le=3000)
    window_width: float | None = Field(default=None, ge=1, le=8000)


def _record_inference_run(case_id: str, model_version: str | None, prompt_source: str, status: str, error: str | None, user_id: int) -> str:
    """Persist every CT/model invocation outcome without storing prompt payloads."""
    run_id = "inf_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    now = time.time()
    con = db()
    con.execute(
        "INSERT INTO inference_runs(id,case_id,model_version,prompt_source,status,output_path,error,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, case_id, model_version, prompt_source, status, None, error, user_id, now, now),
    )
    con.commit(); con.close()
    return run_id


def _ct_inference_error(case_id: str, user_id: int, code: str, message: str, model_version: str | None = None) -> dict:
    run_id = _record_inference_run(case_id, model_version, "interactive_ct", "failed", message, user_id)
    return {
        "ok": False,
        "error": {"code": code, "message": message},
        "model_version": model_version,
        "modality": "ct_dicom",
        "inference_run_id": run_id,
    }


def _ct_prompt_payload(prompts: list[dict]) -> list[dict]:
    """Whitelist point fields before sending a request to an external CT hook."""
    clean: list[dict] = []
    for prompt in prompts:
        if not isinstance(prompt, dict):
            raise ValueError("CT 提示点格式无效")
        try:
            x, y = float(prompt["x"]), float(prompt["y"])
            label = int(prompt.get("label", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("CT 提示点需要有效的 x、y 坐标和正负标签") from exc
        if not all(map(math.isfinite, (x, y))) or label not in {0, 1}:
            raise ValueError("CT 提示点坐标或正负标签无效")
        clean.append({"x": x, "y": y, "label": label})
    return clean


class BatchInferenceIn(BaseModel):
    frames: list[int] = Field(min_length=1, max_length=200)
    organ: str = Field(min_length=1, max_length=80)
    prompts_by_frame: dict[str, list[dict]] = Field(default_factory=dict)
    video: str = ""
    series: str = ""
    model_version: str | None = None


class DatasetExportIn(BaseModel):
    submission_ids: list[int] = Field(default_factory=list)
    allow_single_modality: bool = False
    output_name: str = Field(default="phase2_dataset", min_length=1, max_length=100)
    seed: int = 2026
    npz_window: int = Field(default=8, ge=8)
    npz_stride: int = Field(default=4, ge=1)


class TrainingIn(BaseModel):
    dataset_version: str | None = Field(default=None, min_length=1, max_length=120)
    submission_ids: list[int] = Field(default_factory=list)
    reference_case_ids: list[str] = Field(default_factory=list)
    reference_dataset_ids: list[str] = Field(default_factory=list)
    model_base: str = Field(default="sam2.1_tiny", min_length=1, max_length=80)
    config: dict = Field(default_factory=dict)
    dry_run: bool = True


class CTSpineWeightIn(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=8, max_length=1000)
    local_path: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    license: str = Field(min_length=1, max_length=200)
    label_map_json: dict
    model_family: str = Field(min_length=1, max_length=100)


class CTSpineTrainingIn(BaseModel):
    dataset_version: str | None = Field(default=None, min_length=1, max_length=120)
    submission_ids: list[int] = Field(default_factory=list)
    reference_case_ids: list[str] = Field(default_factory=list)
    reference_dataset_ids: list[str] = Field(default_factory=list)
    weight_id: str = Field(min_length=1, max_length=120)
    mode: str = Field(default="full", pattern=r"^(full|lora)$")
    max_steps: int = Field(default=100, ge=1, le=1000000)
    dry_run: bool = True


class EvaluationIn(BaseModel):
    model_version: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=120)


def _phase2_update(run_id: str, **values):
    fields = ", ".join(f"{k}=?" for k in values)
    statement = f"UPDATE training_runs SET {fields},updated_at=? WHERE id=?"
    params = (*values.values(), time.time(), run_id)
    con = db()
    try:
        con.execute(statement, params)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _multimodal_metrics(metrics: object) -> dict | None:
    """Return only a complete CT/video validation+test metric contract.

    Do not infer a modality score from a mixed aggregate: a model may only be
    published or recorded as multimodal when its own evaluator emitted each
    required partition explicitly.
    """
    if not isinstance(metrics, dict):
        return None
    modalities = metrics.get("modalities")
    if not isinstance(modalities, dict):
        return None
    def finite_rate(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 <= number <= 1 else None

    def hd95_value(value: object) -> float | dict | None:
        if isinstance(value, dict):
            reason = value.get("reason")
            if value.get("status") == "not_applicable" and isinstance(reason, str) and reason.strip():
                return {"status": "not_applicable", "reason": reason.strip()}
            return None
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    def nonnegative(value: object, *, positive: bool = False) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or (positive and number <= 0):
            return None
        return number

    def positive_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def nonnegative_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def aggregate_scores(value: object) -> dict | None:
        if not isinstance(value, dict):
            return None
        if "empty_target_recall" not in value:
            return None
        dice = finite_rate(value.get("dice"))
        iou = finite_rate(value.get("iou"))
        boundary_fscore = finite_rate(value.get("boundary_fscore"))
        hd95 = hd95_value(value.get("hd95"))
        empty_target_recall = value["empty_target_recall"]
        if empty_target_recall is not None:
            empty_target_recall = finite_rate(empty_target_recall)
        if (
            dice is None or iou is None or boundary_fscore is None or hd95 is None
            or (value["empty_target_recall"] is not None and empty_target_recall is None)
        ):
            return None
        return {
            "dice": dice, "iou": iou, "hd95": hd95,
            "boundary_fscore": boundary_fscore,
            "empty_target_recall": empty_target_recall,
        }

    case_metric_names = (
        "dice", "iou", "hd95", "boundary_fscore", "empty_target_recall",
        "adjacent_iou", "flicker", "target_loss_rate",
    )

    def case_metrics(value: object) -> dict | None:
        if not isinstance(value, dict):
            return None
        result = {}
        for name in case_metric_names:
            raw = value.get(name)
            if raw is None and name == "empty_target_recall":
                result[name] = None
                continue
            parsed = nonnegative(raw) if name == "hd95" else finite_rate(raw)
            if parsed is None:
                return None
            result[name] = parsed
        return result

    def case_level_contract(value: object) -> dict | None:
        if not isinstance(value, dict) or value.get("status") not in {"evaluated", "insufficient_cases"}:
            return None
        count = positive_int(value.get("case_count"))
        mean = case_metrics(value.get("mean"))
        intervals = value.get("confidence_interval_95")
        cases = value.get("cases")
        bootstrap = value.get("bootstrap")
        if count is None or mean is None or not isinstance(intervals, dict) or not isinstance(cases, list) or len(cases) != count:
            return None
        if (value.get("aggregation") != "within_case_micro_then_equal_case_mean"
                or not isinstance(bootstrap, dict) or bootstrap.get("method") != "percentile"
                or positive_int(bootstrap.get("resamples")) is None
                or not isinstance(bootstrap.get("seed"), int) or isinstance(bootstrap.get("seed"), bool)
                or nonnegative(bootstrap.get("confidence"), positive=True) != 0.95):
            return None
        normalised_intervals = {}
        for name in case_metric_names:
            interval = intervals.get(name)
            if not isinstance(interval, dict) or interval.get("status") not in {"evaluated", "insufficient_cases"}:
                return None
            interval_count = nonnegative_int(interval.get("case_count"))
            lower, upper = interval.get("lower"), interval.get("upper")
            if interval_count is None:
                return None
            if interval["status"] == "evaluated":
                if count < 2 or interval_count < 2 or interval_count > count:
                    return None
                lower = nonnegative(lower) if name == "hd95" else finite_rate(lower)
                upper = nonnegative(upper) if name == "hd95" else finite_rate(upper)
                if lower is None or upper is None or lower > upper:
                    return None
            elif lower is not None or upper is not None or interval_count > count:
                return None
            normalised_intervals[name] = {**interval, "case_count": interval_count, "lower": lower, "upper": upper}
        normalised_cases, case_ids = [], set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"].strip():
                return None
            if case["case_id"] in case_ids or positive_int(case.get("sample_count")) is None:
                return None
            values = case_metrics(case)
            if values is None:
                return None
            case_ids.add(case["case_id"])
            normalised_cases.append({"case_id": case["case_id"], "sample_count": case["sample_count"], **values})
        expected_status = "evaluated" if count >= 2 else "insufficient_cases"
        if value["status"] != expected_status:
            return None
        return {**value, "case_count": count, "mean": mean, "confidence_interval_95": normalised_intervals, "cases": normalised_cases}

    def window_contract(value: object) -> dict | None:
        if not isinstance(value, dict):
            return None
        status = value.get("status")
        overlap = value.get("overlap_frame_count")
        comparisons = value.get("comparison_count")
        if (not isinstance(overlap, int) or isinstance(overlap, bool) or overlap < 0
                or not isinstance(comparisons, int) or isinstance(comparisons, bool) or comparisons < 0):
            return None
        if status == "evaluated":
            mean_iou = finite_rate(value.get("mean_iou"))
            if overlap < 1 or comparisons < 1 or mean_iou is None:
                return None
            return {**value, "mean_iou": mean_iou}
        reason = value.get("reason")
        if status == "not_applicable" and overlap == comparisons == 0 and value.get("mean_iou") is None and isinstance(reason, str) and reason.strip():
            return {**value, "reason": reason.strip()}
        return None

    performance = metrics.get("performance")
    if not isinstance(performance, dict):
        return None
    frame_count = positive_int(performance.get("effective_scored_frames"))
    end_to_end = nonnegative(performance.get("effective_end_to_end_seconds"), positive=True)
    propagation = nonnegative(performance.get("effective_propagation_seconds"), positive=True)
    fps = nonnegative(performance.get("effective_fps"), positive=True)
    propagation_fps = nonnegative(performance.get("effective_propagation_fps"), positive=True)
    p50 = nonnegative(performance.get("latency_p50_ms"))
    p95 = nonnegative(performance.get("latency_p95_ms"))
    model_load = nonnegative(performance.get("model_load_seconds"))
    device = performance.get("device")
    memory_status = performance.get("peak_gpu_memory_status")
    memory = performance.get("peak_gpu_memory_mb")
    latency_method = performance.get("latency_measurement_method")
    if (frame_count is None or end_to_end is None or propagation is None or fps is None
            or propagation_fps is None or p50 is None or p95 is None
            or model_load is None or p95 < p50 or not isinstance(device, str) or not device.strip()
            or latency_method not in {"estimated_amortized", "measured_per_frame"}
            or not math.isclose(fps, frame_count / end_to_end, rel_tol=1e-9, abs_tol=1e-9)
            or not math.isclose(propagation_fps, frame_count / propagation, rel_tol=1e-9, abs_tol=1e-9)):
        return None
    if device.lower().startswith("cuda"):
        memory = nonnegative(memory)
        if memory_status != "evaluated" or memory is None:
            return None
    elif memory_status != "not_applicable" or memory is not None:
        return None

    result = {}
    for modality in ("ct", "video"):
        item = modalities.get(modality)
        if not isinstance(item, dict) or not isinstance(item.get("val"), dict) or not isinstance(item.get("test"), dict):
            return None
        partitions = {}
        for partition in ("val", "test"):
            scores = item[partition]
            if "empty_target_recall" not in scores:
                return None
            try:
                sample_count = int(scores.get("sample_count", 0))
            except (TypeError, ValueError):
                return None
            partition_empty_target_recall = scores["empty_target_recall"]
            if partition_empty_target_recall is not None:
                partition_empty_target_recall = finite_rate(partition_empty_target_recall)
                if partition_empty_target_recall is None:
                    return None
            per_class = scores.get("per_class")
            macro = aggregate_scores(scores.get("macro"))
            micro = aggregate_scores(scores.get("micro"))
            temporal = scores.get("temporal")
            case_level = case_level_contract(scores.get("case_level"))
            window = window_contract(scores.get("window_boundary_consistency"))
            if sample_count <= 0 or not isinstance(per_class, dict) or not per_class or not macro or not micro or not isinstance(temporal, dict) or case_level is None or window is None:
                return None
            classes = {}
            for class_name, class_scores in per_class.items():
                if not isinstance(class_name, str) or not class_name.strip() or not isinstance(class_scores, dict):
                    return None
                try:
                    class_sample_count = int(class_scores.get("sample_count", 0))
                except (TypeError, ValueError):
                    return None
                aggregate = aggregate_scores(class_scores)
                class_case_level = case_level_contract(class_scores.get("case_level"))
                class_window = window_contract(class_scores.get("window_boundary_consistency"))
                if class_sample_count <= 0 or not aggregate or class_case_level is None or class_window is None:
                    return None
                classes[class_name] = {
                    "sample_count": class_sample_count, **aggregate,
                    "case_level": class_case_level, "window_boundary_consistency": class_window,
                }
            try:
                temporal_sample_count = int(temporal.get("sample_count", 0))
            except (TypeError, ValueError):
                return None
            adjacent_iou = finite_rate(temporal.get("adjacent_iou"))
            flicker = finite_rate(temporal.get("flicker"))
            target_loss_rate = finite_rate(temporal.get("target_loss_rate"))
            if temporal_sample_count <= 0 or adjacent_iou is None or flicker is None or target_loss_rate is None:
                return None
            # Preserve the legacy outer Dice/IoU aliases for API consumers while
            # making their source explicit and validating the full report.
            partitions[partition] = {
                "sample_count": sample_count,
                "dice": micro["dice"],
                "iou": micro["iou"],
                "empty_target_recall": micro["empty_target_recall"],
                "per_class": classes,
                "macro": macro,
                "micro": micro,
                "temporal": {
                    "sample_count": temporal_sample_count,
                    "adjacent_iou": adjacent_iou,
                    "flicker": flicker,
                    "target_loss_rate": target_loss_rate,
                },
                "case_level": case_level,
                "window_boundary_consistency": window,
            }
        result[modality] = partitions
    return result


def _multimodal_publish_adapter_error(config: object) -> str | None:
    """Validate the explicit dual-modality serving declaration before publish."""
    if not isinstance(config, dict):
        return "训练配置无效"
    adapters = config.get("inference_adapters")
    if not isinstance(adapters, dict):
        return "缺少 CT 和视频推理适配器声明"
    if adapters.get("video") != "medsam2_video_hook":
        return "MedSAM2 视频推理必须声明 medsam2_video_hook 适配器"
    if adapters.get("ct") != "external_ct_hook":
        return "CT 必须声明经验证的 external_ct_hook 适配器"
    model_config = config.get("model_config")
    if not isinstance(model_config, str) or not model_config.strip():
        return "缺少视频推理模型配置声明"
    try:
        _medsam2_model_config(model_config)
    except ValueError:
        return "视频推理模型配置文件不存在"
    try:
        _multimodal_base_checkpoint(config)
    except ValueError as exc:
        return str(exc)
    hook_error = _ct_hook_command_error()
    if hook_error:
        return hook_error
    return None


def _configured_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _medsam2_model_config(value: str) -> tuple[str, Path]:
    """Resolve a hook-compatible MedSAM2 Hydra config name and its YAML file."""
    config_root = (CODE_ROOT / "third_party" / "medsam2" / "sam2").resolve()
    supplied = Path(value.strip()).expanduser()
    supplied_path = supplied if supplied.is_absolute() else config_root / supplied
    hydra_name = not supplied_path.is_file() and supplied_path.suffix.lower() not in {".yaml", ".yml"}
    if supplied_path.is_file() and supplied_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("MedSAM2 配置必须是 YAML 文件")
    # Hydra config names can contain dots (for example sam2.1_*), so append
    # instead of replacing a pathlib-derived suffix.
    candidate = Path(f"{supplied_path}.yaml") if hydra_name else supplied_path
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(config_root)
    except (OSError, ValueError) as exc:
        raise ValueError("MedSAM2 配置必须位于 third_party/medsam2/sam2 目录内") from exc
    if not resolved.is_file():
        raise ValueError("MedSAM2 配置文件不存在")
    # Keep the exact package-relative name accepted by medsam2_video_prompt_hook.
    package_name = relative.as_posix()
    if hydra_name and package_name.endswith(".yaml"):
        package_name = package_name[:-5]
    return package_name, resolved


def _normalise_multimodal_training_config(config: dict) -> dict:
    """Store the exact config keys consumed by the MedSAM2 training entrypoint."""
    normalised = dict(config)
    model_config, _ = _medsam2_model_config(str(normalised["model_config"]))
    checkpoint = _multimodal_base_checkpoint(normalised)
    normalised["model_config"] = model_config
    normalised["base_checkpoint"] = str(checkpoint)
    normalised.pop("checkpoint", None)
    return normalised


def _multimodal_base_checkpoint(config: object) -> Path:
    """Return the one verified MedSAM2 base checkpoint shared by all callers."""
    if not isinstance(config, dict):
        raise ValueError("训练配置无效")
    fields = (("checkpoint", config.get("checkpoint")), ("base_checkpoint", config.get("base_checkpoint")))
    paths: list[tuple[str, Path]] = []
    for name, value in fields:
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError("缺少可用的视频推理基础 checkpoint")
        paths.append((name, _configured_path(value)))
    if not paths:
        raise ValueError("缺少可用的视频推理基础 checkpoint")
    if len(paths) == 2:
        if not all(path.is_file() for _, path in paths):
            raise ValueError("checkpoint 与 base_checkpoint 必须均指向存在文件")
        try:
            if paths[0][1].resolve() != paths[1][1].resolve():
                raise ValueError("checkpoint 与 base_checkpoint 指向不同文件")
        except OSError as exc:
            raise ValueError("无法解析基础 checkpoint 路径") from exc
    path = paths[0][1]
    if not path.is_file():
        raise ValueError("缺少可用的视频推理基础 checkpoint")
    try:
        return path.resolve()
    except OSError as exc:
        raise ValueError("无法解析基础 checkpoint 路径") from exc


def _ct_hook_command_error() -> str | None:
    """Check the configured CT command without executing patient data through it."""
    command_text = _env("SWTOOL_CT_HOOK", "WOMANSEG_CT_HOOK").strip()
    if not command_text:
        return "服务未配置 SWTOOL_CT_HOOK，不能发布 CT 多模态模型"
    try:
        command = shlex.split(command_text)
    except ValueError:
        return "SWTOOL_CT_HOOK 命令格式无效"
    if not command:
        return "SWTOOL_CT_HOOK 未指定可执行命令"
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        executable = Path(shutil.which(command[0]) or "")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return "配置的 CT 专用模型适配器不可执行"
    return None


def _multimodal_training_config_error(config: object) -> str | None:
    """Reject formal runs whose serving and training inputs disagree."""
    if not isinstance(config, dict):
        return "训练配置无效"
    try:
        checkpoint_path = _multimodal_base_checkpoint(config)
    except ValueError as exc:
        return str(exc)
    adapter_error = _multimodal_publish_adapter_error(config)
    if adapter_error:
        return adapter_error
    try:
        _, model_config_path = _medsam2_model_config(str(config["model_config"]))
    except ValueError:
        return "视频推理模型配置文件不存在"
    if checkpoint_path.suffix.lower() not in {".pt", ".pth", ".ckpt"}:
        return "基础 checkpoint 文件类型必须是 .pt、.pth 或 .ckpt"
    if model_config_path.suffix.lower() not in {".yaml", ".yml"}:
        return "MedSAM2 模型配置必须是 YAML 文件"
    if checkpoint_path.stat().st_size <= 0 or model_config_path.stat().st_size <= 0:
        return "基础 checkpoint 或模型配置文件为空"
    return None


def _frozen_multimodal_export_error(dataset_root: Path) -> str | None:
    """Validate the frozen export report used by a multimodal training run."""
    if not dataset_root.is_dir() or not (dataset_root / "manifest.json").is_file():
        return "冻结数据集目录或 manifest.json 不存在"
    samples_manifest = dataset_root / "medsam2_npz" / "samples_manifest.json"
    if not samples_manifest.is_file():
        return "冻结数据集缺少 MedSAM2 NPZ 导出报告"
    try:
        report = json.loads(samples_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "MedSAM2 NPZ 导出报告无效"
    if not isinstance(report, dict) or report.get("publishable") is not True:
        return "MedSAM2 NPZ 导出未通过可发布检查"
    counts = report.get("sample_counts")
    if not isinstance(counts, dict):
        return "MedSAM2 NPZ 导出报告缺少样本统计"
    for partition in ("val", "test"):
        split = counts.get(partition)
        if not isinstance(split, dict):
            return f"MedSAM2 NPZ 导出缺少 {partition} 样本统计"
        for modality in ("ct_dicom", "video"):
            try:
                count = int(split.get(modality, 0))
            except (TypeError, ValueError):
                count = 0
            if count < 1:
                return f"MedSAM2 NPZ 导出缺少 {partition} 的 {modality} 样本"
    return None


def _multimodal_publish_gate(metrics: object) -> str | None:
    """Apply CT/video val+test gates.

    Conservative defaults: samples>=1, cases>=2, Dice>=0.5, IoU>=0.3, boundary F>=0.3,
    adjacent IoU>=0.3, flicker<=0.5, target loss<=0.2 and HD95<=50 px.
    The requirement-defined performance defaults are FPS>=15 and P95<=66.7 ms.
    Window overlap must be evaluable; its optional minimum defaults to 0. GPU memory
    is only gated when ``SWTOOL_MM_MAX_GPU_MEMORY_MB`` is configured.
    """
    if not isinstance(metrics, dict) or not isinstance(metrics.get("performance"), dict):
        return "多模态模型缺少性能评测，不能发布"
    performance_input = metrics["performance"]
    try:
        performance_numbers = {
            name: float(performance_input[name])
            for name in (
                "model_load_seconds", "effective_end_to_end_seconds",
                "effective_propagation_seconds", "effective_fps", "effective_propagation_fps",
                "latency_p50_ms", "latency_p95_ms",
            )
        }
    except (KeyError, TypeError, ValueError):
        return "多模态模型性能评测字段非法，不能发布"
    if (any(not math.isfinite(value) or value < 0 for value in performance_numbers.values())
            or performance_numbers["effective_propagation_seconds"] <= 0
            or performance_numbers["effective_end_to_end_seconds"] <= 0
            or performance_numbers["effective_fps"] <= 0
            or performance_numbers["effective_propagation_fps"] <= 0
            or performance_numbers["latency_p95_ms"] < performance_numbers["latency_p50_ms"]):
        return "多模态模型性能评测字段非法，不能发布"
    if performance_input.get("latency_measurement_method") != "measured_per_frame":
        return "多模态推理 P95 延迟不是逐帧实测，不能发布"
    modalities = _multimodal_metrics(metrics)
    if not modalities:
        return "多模态模型缺少 CT 和视频各自的 val/test 指标，不能发布"
    try:
        min_samples = int(_env("SWTOOL_MM_MIN_SAMPLE_COUNT", "WOMANSEG_MM_MIN_SAMPLE_COUNT", "1"))
        min_dice = float(_env("SWTOOL_MM_MIN_DICE", "WOMANSEG_MM_MIN_DICE", "0.5"))
        min_iou = float(_env("SWTOOL_MM_MIN_IOU", "WOMANSEG_MM_MIN_IOU", "0.3"))
        min_boundary = float(_env("SWTOOL_MM_MIN_BOUNDARY_FSCORE", "WOMANSEG_MM_MIN_BOUNDARY_FSCORE", "0.3"))
        min_adjacent_iou = float(_env("SWTOOL_MM_MIN_ADJACENT_IOU", "WOMANSEG_MM_MIN_ADJACENT_IOU", "0.3"))
        max_flicker = float(_env("SWTOOL_MM_MAX_FLICKER", "WOMANSEG_MM_MAX_FLICKER", "0.5"))
        max_target_loss = float(_env("SWTOOL_MM_MAX_TARGET_LOSS_RATE", "WOMANSEG_MM_MAX_TARGET_LOSS_RATE", "0.2"))
        max_hd95 = float(_env("SWTOOL_MM_MAX_HD95", "WOMANSEG_MM_MAX_HD95", "50.0"))
        min_window_iou = float(_env("SWTOOL_MM_MIN_WINDOW_BOUNDARY_IOU", "WOMANSEG_MM_MIN_WINDOW_BOUNDARY_IOU", "0"))
        min_fps = float(_env("SWTOOL_MM_MIN_FPS", "WOMANSEG_MM_MIN_FPS", "15"))
        max_p95_latency = float(_env("SWTOOL_MM_MAX_P95_LATENCY_MS", "WOMANSEG_MM_MAX_P95_LATENCY_MS", "66.7"))
        max_memory_raw = _env("SWTOOL_MM_MAX_GPU_MEMORY_MB", "WOMANSEG_MM_MAX_GPU_MEMORY_MB", "").strip()
        max_gpu_memory = float(max_memory_raw) if max_memory_raw else None
    except ValueError:
        return "多模态发布门禁阈值配置无效"
    if (
        min_samples < 1
        or not all(math.isfinite(value) for value in (min_dice, min_iou, min_boundary, min_adjacent_iou, max_flicker, max_target_loss, max_hd95, min_window_iou, min_fps, max_p95_latency))
        or not all(0 <= value <= 1 for value in (min_dice, min_iou, min_boundary, min_adjacent_iou, max_flicker, max_target_loss, min_window_iou))
        or max_hd95 < 0 or min_fps <= 0 or max_p95_latency < 0
        or (max_gpu_memory is not None and (not math.isfinite(max_gpu_memory) or max_gpu_memory < 0))
    ):
        return "多模态发布门禁阈值配置无效"
    for modality, partitions in modalities.items():
        for partition, scores in partitions.items():
            if scores["sample_count"] < min_samples:
                return f"{modality} {partition} 样本数未达到发布门禁"
            if scores["case_level"]["case_count"] < 2:
                return f"{modality} {partition} 病例数不足 2，无法形成病例级 95% CI"
            window = scores["window_boundary_consistency"]
            if window["status"] != "evaluated":
                return f"{modality} {partition} 窗口边界一致性不可评估"
            if window["mean_iou"] < min_window_iou:
                return f"{modality} {partition} 窗口边界 IoU 未达到发布门禁"
            for aggregate_name in ("macro", "micro"):
                aggregate = scores[aggregate_name]
                if aggregate["dice"] < min_dice or aggregate["iou"] < min_iou:
                    return f"{modality} {partition} {aggregate_name} Dice/IoU 未达到发布门禁"
                if aggregate["boundary_fscore"] < min_boundary:
                    return f"{modality} {partition} {aggregate_name} boundary F-score 未达到发布门禁"
                if isinstance(aggregate["hd95"], float) and aggregate["hd95"] > max_hd95:
                    return f"{modality} {partition} {aggregate_name} HD95 未达到发布门禁"
            temporal = scores["temporal"]
            if temporal["adjacent_iou"] < min_adjacent_iou:
                return f"{modality} {partition} adjacent IoU 未达到发布门禁"
            if temporal["flicker"] > max_flicker:
                return f"{modality} {partition} flicker 未达到发布门禁"
            if temporal["target_loss_rate"] > max_target_loss:
                return f"{modality} {partition} target loss rate 未达到发布门禁"
    performance = metrics["performance"]
    if performance["effective_fps"] < min_fps:
        return "多模态推理 FPS 未达到发布门禁"
    if performance["latency_p95_ms"] > max_p95_latency:
        return "多模态推理 P95 延迟未达到发布门禁"
    if max_gpu_memory is not None:
        if performance["peak_gpu_memory_status"] != "evaluated":
            return "多模态推理 GPU 峰值显存不可评估"
        if performance["peak_gpu_memory_mb"] > max_gpu_memory:
            return "多模态推理 GPU 峰值显存未达到发布门禁"
    return None


def _multimodal_regression_error(metrics: object) -> str | None:
    """Require the evaluator's explicit temporal-regression record."""
    if not isinstance(metrics, dict):
        return "多模态评测记录无效"
    if metrics.get("status") != "evaluated" or metrics.get("publishable") is not True:
        return "多模态模型缺少通过的评测记录"
    if (
        metrics.get("protocol") != "oracle_first_frame_temporal_regression"
        or metrics.get("oracle_first_frame") is not True
        or metrics.get("not_prompt_free") is not True
    ):
        return "多模态模型缺少受控时序发布回归记录"
    return None


def _model_artifact_error(row: object, con) -> str | None:
    """Validate a stored model before publish or rollback can expose it."""
    checkpoint = Path(str(row["checkpoint_path"] or ""))
    if not checkpoint.is_file() or checkpoint.stat().st_size <= 0:
        return "模型 checkpoint 不存在或为空"
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, str(row["checkpoint_sha256"] or "")):
        return "模型 checkpoint SHA256 校验失败"
    try:
        metrics = json.loads(row["metrics_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return "模型指标元数据无效"
    model_base = metrics.get("model_base")
    if model_base == "medsam2_multimodal":
        gate_error = _multimodal_publish_gate(metrics)
        if gate_error:
            return gate_error
        regression_error = _multimodal_regression_error(metrics)
        if regression_error:
            return regression_error
        adapter_error = _multimodal_publish_adapter_error(metrics)
        if adapter_error:
            return adapter_error
    training = con.execute("SELECT status FROM training_runs WHERE id=?", (row["training_run"],)).fetchone()
    if not training or training["status"] != "succeeded":
        return "模型关联的训练任务不是成功状态"
    return None


def _medsam2_training_runtime_error() -> str | None:
    """Return a deployable-error message instead of starting a doomed task."""
    required = {
        "本地 MedSAM2 运行时": ROOT / "third_party" / "medsam2" / "training" / "train.py",
        "MedSAM2 联合训练适配器": ROOT / "scripts" / "train_medsam2_multimodal.py",
        "MedSAM2 分模态评测器": ROOT / "scripts" / "medsam2_multimodal_eval.py",
        "MedSAM2 官方训练配置": ROOT / "third_party" / "medsam2" / "sam2" / "configs" / "sam2.1_hiera_tiny512_FLARE_RECIST.yaml",
    }
    missing = [name for name, path in required.items() if not _script_path(path).is_file()]
    if missing:
        return "本地 MedSAM2 训练运行时不完整: " + "、".join(missing)
    return None


def _phase2_readiness() -> dict:
    """Return a non-sensitive readiness summary for the administrator console.

    This deliberately reports counts and boolean state only.  Dataset roots,
    checkpoint filenames, CUDA device metadata and hook command text are not
    returned by the API because they may expose deployment or case details.
    """
    con = db()
    try:
        frozen_rows = con.execute(
            "SELECT root_path FROM dataset_versions WHERE status='frozen'"
        ).fetchall()
    finally:
        con.close()

    usable_frozen_count = 0
    for row in frozen_rows:
        dataset_root = Path(str(row["root_path"] or ""))
        if _frozen_multimodal_export_error(dataset_root) is None:
            usable_frozen_count += 1

    runtime_error = _medsam2_training_runtime_error()
    runtime_complete = runtime_error is None

    cuda_available = False
    try:
        import torch
        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        # The endpoint remains usable on deployments that do not ship PyTorch.
        cuda_available = False

    checkpoint_dir = ROOT / "models" / "medsam2"
    checkpoint_count = 0
    if checkpoint_dir.is_dir():
        checkpoint_count = sum(
            1
            for candidate in checkpoint_dir.rglob("*")
            if candidate.is_file()
            and candidate.suffix.lower() in {".pt", ".pth", ".ckpt"}
            # CTLesion is a CT prompt model, not a MedSAM2 joint-training base.
            and "ctlesion" not in candidate.name.lower()
        )

    ct_hook_error = _ct_hook_command_error()
    ct_hook_configured = ct_hook_error is None
    frozen_available = usable_frozen_count > 0
    checkpoint_available = checkpoint_count > 0
    ready_for_dry_run = frozen_available and runtime_complete
    ready_for_real_training = (
        ready_for_dry_run
        and cuda_available
        and checkpoint_available
        and ct_hook_configured
    )

    return {
        "items": [
            {
                "key": "frozen_datasets",
                "label": "冻结数据集",
                "status": "ready" if frozen_available else "blocked",
                "frozen_count": len(frozen_rows),
                "usable_count": usable_frozen_count,
                "available": frozen_available,
                "message": "存在可供 MedSAM2 使用的冻结数据集" if frozen_available else "尚无包含 MedSAM2 NPZ 的可用冻结数据集",
            },
            {
                "key": "medsam2_runtime",
                "label": "MedSAM2 运行时",
                "status": "ready" if runtime_complete else "blocked",
                "available": runtime_complete,
                "message": "本地 MedSAM2 训练运行时完整" if runtime_complete else runtime_error,
            },
            {
                "key": "cuda",
                "label": "CUDA",
                "status": "ready" if cuda_available else "blocked",
                "available": cuda_available,
                "message": "CUDA 可用于正式训练" if cuda_available else "未检测到可用于正式训练的 CUDA 运行时",
            },
            {
                "key": "base_checkpoints",
                "label": "基础 checkpoint",
                "status": "action_required",
                "candidate_count": checkpoint_count,
                "available": checkpoint_available,
                "requires_admin_path": True,
                "message": "训练任务必须显式选择与配置匹配的基础 checkpoint",
            },
            {
                "key": "ct_external_hook",
                "label": "CT 外部推理适配器",
                "status": "ready" if ct_hook_configured else "action_required",
                "configured": ct_hook_configured,
                "requires_service_config": not ct_hook_configured,
                "message": "CT 外部推理适配器已配置" if ct_hook_configured else ct_hook_error,
            },
        ],
        "ready_for_dry_run": ready_for_dry_run,
        "ready_for_real_training": ready_for_real_training,
    }


def _normalise_multimodal_metrics(raw: object) -> dict:
    """Normalise the evaluator's `ct_dicom` source name to the public `ct` API."""
    if not isinstance(raw, dict):
        raise ValueError("分模态评测结果不是 JSON 对象")
    raw_modalities = raw.get("modalities")
    if not isinstance(raw_modalities, dict):
        raise ValueError("分模态评测结果缺少 modalities")
    metrics = dict(raw)
    metrics["modalities"] = {
        "ct": raw_modalities.get("ct_dicom"),
        "video": raw_modalities.get("video"),
    }
    normalised = _multimodal_metrics(metrics)
    if not normalised:
        raise ValueError("分模态评测没有产生 CT 与视频完整的 val/test 质量及时序指标")
    metrics["modalities"] = normalised
    return metrics


def _run_phase2_training(run_id: str, dataset_root: Path, output: Path, config: dict, dry_run: bool):
    _phase2_update(run_id, status="running", progress=0.2 if config.get("background_preparation") else 0.05)
    try:
        from scripts.data_contract import validate_root
        validate_root(dataset_root)
    except Exception as exc:
        _phase2_update(run_id, status="failed", progress=1, error=f"数据契约校验失败: {exc}")
        return
    model_base = str(config.get("model_base", "sam2.1_tiny"))
    output.mkdir(parents=True, exist_ok=True)
    cfg_path = output / "config.json"; cfg_path.write_text(json.dumps({"data": {"root": str(dataset_root)}, **config}, ensure_ascii=False, indent=2), encoding="utf-8")
    # 使用当前服务进程的解释器，确保 systemd/Conda 环境中的依赖一致。
    if model_base == "medsam2_multimodal":
        cmd = [
            sys.executable, str(_script_path(ROOT / "scripts" / "train_medsam2_multimodal.py")),
            "--data", str(dataset_root), "--config", str(cfg_path), "--output", str(output),
        ]
    elif model_base in {"unet", "deeplabv3"}:
        cmd = [sys.executable, str(_script_path(ROOT / "scripts" / "train_phase2_baseline.py")), "--data", str(dataset_root), "--output", str(output), "--model", model_base]
    else:
        cmd = [sys.executable, str(_script_path(ROOT / "scripts" / "train_phase2_sam2.py")), "--data", str(dataset_root), "--config", str(cfg_path), "--output", str(output), "--max-steps", str(int(config.get("max_steps", 1)))]
    if dry_run: cmd.append("--dry-run")
    elif model_base in {"unet", "deeplabv3"}: cmd.extend(["--max-steps", str(int(config.get("max_steps", 1)))])
    else: cmd.append("--run")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=int(config.get("timeout_sec", 86400)))
        metrics = output / ("dry_run_report.json" if dry_run and model_base == "medsam2_multimodal" else "metrics.json")
        if proc.returncode != 0:
            _phase2_update(run_id, status="failed", progress=1, error=(proc.stderr or proc.stdout)[-2000:]); return
        if model_base == "medsam2_multimodal" and not dry_run:
            checkpoint = output / "best.pt"
            if not checkpoint.is_file() or checkpoint.stat().st_size <= 0:
                _phase2_update(run_id, status="failed", progress=1, error="MedSAM2 训练未生成有效 best.pt")
                return
            # The trainer invokes this evaluator too, but invoke it from the service
            # boundary as well: only this result becomes the publishable record.
            evaluation_cmd = [
                sys.executable, str(_script_path(ROOT / "scripts" / "medsam2_multimodal_eval.py")),
                "--dataset", str(dataset_root / "medsam2_npz"), "--predictions", str(output / "predictions"),
                "--output", str(metrics),
            ]
            evaluation = subprocess.run(
                evaluation_cmd, cwd=str(ROOT), capture_output=True, text=True,
                timeout=int(config.get("evaluation_timeout_sec", config.get("timeout_sec", 86400))),
            )
            if evaluation.returncode != 0:
                _phase2_update(run_id, status="failed", progress=1, error=(evaluation.stderr or evaluation.stdout)[-2000:])
                return
        if not metrics.is_file():
            _phase2_update(run_id, status="failed", progress=1, error="训练未生成 metrics.json")
            return
        try:
            metrics_payload = json.loads(metrics.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _phase2_update(run_id, status="failed", progress=1, error=f"训练指标文件无效: {exc}")
            return
        if model_base == "medsam2_multimodal":
            if dry_run:
                if not bool(metrics_payload.get("dry_run")) or bool(metrics_payload.get("publishable")):
                    _phase2_update(run_id, status="failed", progress=1, error="MedSAM2 dry-run 报告未标明不可发布")
                    return
            else:
                try:
                    metrics_payload = _normalise_multimodal_metrics(metrics_payload)
                    metrics_payload["model_base"] = "medsam2_multimodal"
                    metrics_payload["training_runtime"] = "local_official_medsam2"
                    metrics.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                except (OSError, ValueError, TypeError) as exc:
                    _phase2_update(run_id, status="failed", progress=1, error=f"多模态独立评测不完整: {exc}")
                    return
        _phase2_update(run_id, status="succeeded", progress=1, metrics_path=str(metrics))
    except Exception as exc:
        _phase2_update(run_id, status="failed", progress=1, error=str(exc))


@app.post("/admin/phase2/datasets/export")
def phase2_export(body: DatasetExportIn, admin=Depends(require_training_permission)):
    return _phase2_prepare_dataset(body, admin)


def _snapshot_training_draft(case_id: str, destination: Path):
    """Capture segment files under the writer lock for an immutable training input."""
    with _case_draft_lock(case_id):
        segments = _read_segment_drafts(case_id)
        if not segments:
            raise HTTPException(409, "服务器草稿不存在")
        con = db()
        try:
            row = con.execute("SELECT metadata FROM cases WHERE id=?", (case_id,)).fetchone()
        finally:
            con.close()
        videos = (json.loads(row['metadata'] or '{}').get('video_files') or []) if row else []
        if videos and 'default' in segments:
            segments.setdefault(str(videos[0]), segments.pop('default'))
        payload = {"case_id": case_id, "segments": segments}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = destination / (hashlib.sha256(case_id.encode()).hexdigest() + '.json')
        _write_bytes_atomically(target, raw, '.training_annotation.')
    return target, hashlib.sha256(raw).hexdigest()


def _phase2_prepare_dataset(body: DatasetExportIn, admin, completed=False, reference_case_ids=None, reference_dataset_ids=None, progress_callback=None):
    if body.npz_stride >= body.npz_window:
        raise HTTPException(422, "MedSAM2 NPZ 窗口步长必须小于窗口长度，以生成重叠窗口一致性证据")
    con = db(); query = "SELECT s.id,s.case_id,s.path,s.status AS submission_status,c.metadata FROM submissions s JOIN cases c ON c.id=s.case_id WHERE c.status='approved' AND s.status='approved' AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id)"; params = ()
    if completed:
        query = query.replace("c.status='approved' AND s.status='approved' AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id)", "((c.status IN ('annotation_submitted','approved') AND s.status IN ('submitted','approved') AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id)) OR (s.status='draft' AND c.status IN ('annotation_in_progress','rejected') AND s.id=(SELECT MAX(sd.id) FROM submissions sd WHERE sd.case_id=c.id AND sd.status='draft')))")
    if body.submission_ids:
        marks = ",".join("?" for _ in body.submission_ids); query += f" AND s.id IN ({marks})"; params = tuple(body.submission_ids)
    rows = con.execute(query, params).fetchall() if body.submission_ids or not reference_case_ids else []
    if reference_case_ids:
        for case_id in dict.fromkeys(reference_case_ids):
            row = con.execute("SELECT id,metadata FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                con.close(); raise HTTPException(409, "已有标签病例不存在")
            metadata = json.loads(row['metadata'] or '{}')
            if metadata.get('imaging_type') != 'ct_nifti' or not (metadata.get('label_file') or metadata.get('label_files')):
                con.close(); raise HTTPException(409, "病例没有 CT 分割标签")
            rows.append({'id': None, 'case_id': row['id'], 'metadata': row['metadata'], 'submission_status': 'imported_reference'})
    if not rows: con.close(); raise HTTPException(409, "没有可导出的审核通过标注")
    if body.submission_ids and set(body.submission_ids) != {row['id'] for row in rows if row['id'] is not None}:
        con.close(); raise HTTPException(409, "所选标注已失效或尚未审核通过，请刷新后重选")
    version = "ds_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    output = DATA_DIR / "phase2" / "datasets" / version
    case_submission_paths: dict[str, str] = {}
    case_paths: dict[str, str] = {}
    for row in rows:
        case_id = str(row["case_id"])
        if case_id in case_submission_paths:
            con.close()
            raise HTTPException(409, f"病例 {case_id} 选中了多个审核通过提交；请只选择一个审核快照导出")
        if row['id'] is None:
            metadata = json.loads(row['metadata'] or '{}')
            root = _case_root(case_id, metadata)
            if root is None:
                con.close(); raise HTTPException(409, "已有标签病例源目录不存在")
            case_paths[case_id] = str(root)
            continue
        submission_path = Path(str(row["path"] or ""))
        if dict(row).get('submission_status') == 'draft':
            annotation_path = submission_path if submission_path.is_dir() else None
        elif submission_path.is_file():
            annotation_path = submission_path
        elif submission_path.is_dir():
            annotation_candidates = (
                submission_path / "annotation.json",
                submission_path / "annotations" / "annotation.json",
            )
            annotation_path = next((path for path in annotation_candidates if path.is_file()), None)
        else:
            annotation_path = None
        if annotation_path is None and dict(row).get('submission_status') != 'draft':
            con.close()
            raise HTTPException(409, f"审核提交 {row['id']} 的标注文件不存在")
        if dict(row).get('submission_status') == 'draft':
            annotation_path, _ = _snapshot_training_draft(case_id, output / 'source_annotations')
        case_submission_paths[case_id] = str(annotation_path)
        try:
            metadata = json.loads(row['metadata'] or '{}') if 'metadata' in row.keys() else {}
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError):
            metadata = {}
        root = _case_root(case_id, metadata)
        if root is None and 'metadata' in row.keys():
            con.close(); raise HTTPException(409, "审核病例源目录不存在")
        if root is not None:
            case_paths[case_id] = str(root)
    case_ids = list(dict.fromkeys([*case_submission_paths, *(reference_case_ids or [])]))
    annotation_paths = [f"{case_id}={path}" for case_id, path in case_submission_paths.items()]
    cmd = [
        sys.executable, str(_script_path(ROOT / "scripts" / "export_phase2_dataset.py")),
        "--cases-root", str(CASES_DIR), "--case-ids", *case_ids,
        "--output", str(output), "--dataset-version", version,
        "--npz-window", str(body.npz_window), "--npz-stride", str(body.npz_stride),
        "--seed", str(body.seed),
    ]
    if body.allow_single_modality:
        cmd.append('--allow-single-modality')
    if reference_case_ids:
        cmd.extend(['--reference-cases', *reference_case_ids])
    for case_id, path in case_paths.items():
        cmd.extend(['--case-paths', f'{case_id}={path}'])
    for annotation_path in annotation_paths:
        cmd.extend(["--annotation-paths", annotation_path])
    if progress_callback is None:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    else:
        output.mkdir(parents=True, exist_ok=True)
        progress_file = output.parent / (output.name + '.preparation.json')
        cmd.extend(['--progress-file', str(progress_file)])
        proc = _run_training_export(cmd, output, progress_file, progress_callback)
    if proc.returncode != 0: con.close(); raise HTTPException(422, (proc.stderr or proc.stdout)[-2000:])
    digest = hashlib.sha256("".join(sorted(p.read_text(encoding="utf-8") for p in [output/"manifest.json", output/"frame_map.csv"])).encode()).hexdigest()
    report_path = output / "export_report.json"
    validation = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    if not isinstance(validation, dict):
        con.close()
        raise HTTPException(422, "导出报告格式无效")
    modality_counts = validation.get("modality_counts")
    if not isinstance(modality_counts, dict):
        modality_counts = {}
    if completed:
        validation['annotation_selection'] = 'completed_debug'
        validation['debug_only'] = True
        validation['source_submission_ids'] = [r['id'] for r in rows if r['id'] is not None]
        validation['source_submission_statuses'] = {str(r['id']): r['submission_status'] for r in rows if r['id'] is not None}
        validation['reference_case_ids'] = reference_case_ids or []
        validation['reference_dataset_ids'] = reference_dataset_ids or []
        (output / 'export_report.json').write_text(json.dumps(validation, ensure_ascii=False), encoding='utf-8')
    now = time.time()
    con.execute(
        "INSERT INTO dataset_versions(id,source_submission_ids,root_path,status,sha256,export_report,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (version, json.dumps([r["id"] for r in rows if r["id"] is not None]), str(output), "frozen", digest, json.dumps(validation, ensure_ascii=False), admin["id"], now),
    )
    con.commit(); con.close()
    return {"id": version, "status": "frozen", "root_path": str(output), "validation": validation, "modality_counts": modality_counts}


@app.get('/admin/phase2/datasets/completed')
def phase2_completed_datasets(user=Depends(require_training_permission)):
    return _phase2_selectable_submissions(completed=True)


@app.get('/admin/phase2/datasets/approved')
def phase2_approved_datasets(user=Depends(require_training_permission)):
    return _phase2_selectable_submissions()


def _phase2_selectable_submissions(completed=False):
    con = db()
    try:
        query = "SELECT s.id,s.case_id,s.status AS submission_status,c.metadata FROM submissions s JOIN cases c ON c.id=s.case_id WHERE c.status='approved' AND s.status='approved' AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id) ORDER BY s.id DESC"
        if completed:
            query = query.replace("c.status='approved' AND s.status='approved' AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id)", "((c.status IN ('annotation_submitted','approved') AND s.status IN ('submitted','approved') AND s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.case_id=c.id)) OR (s.status='draft' AND c.status IN ('annotation_in_progress','rejected') AND s.id=(SELECT MAX(sd.id) FROM submissions sd WHERE sd.case_id=c.id AND sd.status='draft')))")
        rows = con.execute(query).fetchall()
        items = []
        for row in rows:
            try:
                metadata = json.loads(row['metadata'] or '{}')
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if row['submission_status'] == 'draft' and not _has_server_draft_file(row['case_id'], metadata):
                continue
            kind = str(metadata.get('imaging_type') or 'video').strip().lower()
            modality = 'ct' if kind in {'ct', 'ct_dicom', 'ct_nifti'} else 'mri' if kind in {'mr', 'mri', 'mr_dicom', 'mri_dicom', 'mri_nifti'} else 'video' if kind == 'video' else None
            if modality:
                items.append({'submission_id': row['id'], 'case_id': row['case_id'], 'modality': modality, 'source': 'server_draft' if row['submission_status'] == 'draft' else 'completed', 'display_name': row['case_id'] + (' · 服务器草稿 · 调试训练' if row['submission_status'] == 'draft' else ' · 已完成 · 直接训练')})
        if completed:
            grouped = {}
            for row in con.execute("SELECT id,metadata FROM cases ORDER BY id").fetchall():
                try:
                    metadata = json.loads(row['metadata'] or '{}')
                except (TypeError, ValueError):
                    continue
                if not isinstance(metadata, dict) or metadata.get('imaging_type') != 'ct_nifti':
                    continue
                root = _case_root(row['id'], metadata)
                labels = metadata.get('label_files', [])
                labels = list(labels) if isinstance(labels, list) else []
                if metadata.get('label_file'): labels.append(metadata['label_file'])
                images = metadata.get('imaging_files', [])
                images = list(images) if isinstance(images, list) else []
                if metadata.get('image_file'): images.append(metadata['image_file'])
                try:
                    present = root is not None and bool(labels) and bool(images) and all(_nifti_source_path(root, str(x)).is_file() for x in [*labels, *images])
                except (HTTPException, ValueError, OSError):
                    present = False
                if present:
                    dataset_name = str(metadata.get('import_dataset') or 'CT')
                    relative = str(metadata.get('source_relative') or row['id'])
                    source_dir = str(Path(relative).parent)
                    dataset_id = hashlib.sha256(f'{dataset_name}\0{source_dir}'.encode()).hexdigest()[:20]
                    group = grouped.setdefault(dataset_id, {'reference_dataset_id': dataset_id, 'case_ids': [], 'modality': 'ct', 'source': 'reference_label_directory', 'dataset_name': dataset_name, 'source_directory': source_dir})
                    group['case_ids'].append(row['id'])
            for group in grouped.values():
                group['case_count'] = len(group.pop('case_ids'))
                group['display_name'] = f"{group['dataset_name']} · {group['case_count']} 例（{group['source_directory']} 目录，已有分割标签）"
                items.append(group)
        return {'items': items}
    finally:
        con.close()


@app.get("/admin/phase2/datasets")
def phase2_datasets(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM dataset_versions ORDER BY created_at DESC").fetchall(); con.close()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["export_report"] = json.loads(item.get("export_report") or "{}")
        except json.JSONDecodeError:
            item["export_report"] = {}
        item["modality_counts"] = item["export_report"].get("modality_counts", {})
        items.append(item)
    return {"items": items}


def _has_server_draft_file(case_id: str, metadata: dict) -> bool:
    root = _case_root(case_id, metadata)
    legacy = root / "drafts" / "segments" if root else Path("/") / "__no_legacy_annotation_drafts__"
    current = _segment_draft_dir(case_id)
    return any(directory.is_dir() and any(directory.glob("*.json")) for directory in {legacy, current})


@app.get("/admin/phase2/readiness")
def phase2_readiness(admin=Depends(require_training_permission)):
    """Expose only deployment readiness state, never local paths or secrets."""
    return _phase2_readiness()


def _predictor_dataset_visible(row, user) -> tuple[bool, dict]:
    try:
        report = json.loads(row["export_report"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    is_admin = "admin" in roles_of(user)
    shared = row["status"] == "predictor_only" and report.get("shared_with_users") is True
    return is_admin or shared, report


@app.get("/predictor/datasets")
def predictor_datasets(user=Depends(current_user)):
    con = db()
    try:
        rows = con.execute("SELECT id,status,export_report FROM dataset_versions WHERE status IN ('frozen','predictor_only') ORDER BY created_at DESC").fetchall()
    finally:
        con.close()
    items = []
    for row in rows:
        visible, report = _predictor_dataset_visible(row, user)
        if visible:
            items.append({"id": row["id"], "status": row["status"],
                          "display_name": str(report.get("display_name") or row["id"]),
                          "default_for_predictor": report.get("default_for_predictor") is True})
    return {"items": items}


def _ctspine_json(row):
    item = dict(row)
    for key in ("label_map_json", "metrics_json"):
        if key in item:
            try: item[key] = json.loads(item[key] or "{}")
            except (TypeError, json.JSONDecodeError): item[key] = {}
    return item


@app.get("/admin/ctspine/model-catalog")
def ctspine_model_catalog(admin=Depends(require_training_permission)):
    return {"items": [
        {"id": "swinunetr_ct_pretrain", "name": "MONAI Swin UNETR CT pretrain", "modes": ["full", "lora"], "status": "available"},
        {"id": "nnunet_ctspine1k", "name": "nnU-Net CTSpine1K", "modes": ["full"], "lora": "unavailable", "reason": "卷积适配器未验证", "status": "available"},
        {"id": "totalsegmentator_vertebrae_pp", "name": "TotalSegmentator vertebrae", "modes": ["inference", "prelabel"], "status": "available", "notes": "不可直接作为 CTSpine1K 25 类最终模型"},
    ]}


@app.get("/admin/ctspine/weights")
def ctspine_weights(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM ctspine_weight_registry ORDER BY created_at DESC").fetchall(); con.close()
    return {"items": [_ctspine_json(row) for row in rows]}


@app.post("/admin/ctspine/weights")
def ctspine_register_weight(body: CTSpineWeightIn, admin=Depends(require_training_permission)):
    path = Path(body.local_path).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    try:
        path.relative_to((ROOT / "models" / "ctspine").resolve())
    except ValueError:
        raise HTTPException(400, "权重文件必须位于 models/ctspine 目录")
    if not path.is_file(): raise HTTPException(400, "权重本地文件不存在")
    if not body.source_url.startswith(("http://", "https://")): raise HTTPException(400, "source_url 必须为 http(s)")
    if not body.label_map_json: raise HTTPException(400, "label_map_json 不能为空")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest.lower() != body.sha256.lower(): raise HTTPException(400, "权重 sha256 不匹配")
    con = db()
    try:
        con.execute("INSERT INTO ctspine_weight_registry(id,name,source_url,local_path,sha256,license,label_map_json,model_family,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (body.id, body.name, body.source_url, str(path), digest, body.license, json.dumps(body.label_map_json, ensure_ascii=False), body.model_family, "registered", admin["id"], time.time()))
        con.commit()
    except DBIntegrityError:
        con.close(); raise HTTPException(409, "权重 ID 已存在")
    con.close(); return {"id": body.id, "status": "registered", "sha256": digest}


def _ctspine_run_thread(run_id, dataset_root, weight, output, config):
    try:
        output.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(_script_path(ROOT / "scripts" / "train_ctspine.py")), "--dataset", str(dataset_root), "--weight", str(weight["local_path"]), "--output", str(output), "--mode", config["mode"], "--model-family", config["model_family"], "--max-steps", str(config["max_steps"])]
        if config.get("dry_run"): cmd.append("--dry-run")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=86400)
        if proc.returncode != 0: raise RuntimeError((proc.stderr or proc.stdout or "训练脚本失败")[-2000:])
        metrics = output / "metrics.json"
        _phase2_update(run_id, status="succeeded", progress=1, metrics_path=str(metrics))
    except Exception as exc:
        _phase2_update(run_id, status="failed", progress=1, error=str(exc))


def _run_training_export(cmd, output, progress_file, progress_callback):
    """Track real export events without tying preparation to the HTTP request."""
    with (output.parent / (output.name + '.stdout.log')).open('w+') as stdout, (output.parent / (output.name + '.stderr.log')).open('w+') as stderr:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=stdout, stderr=stderr, text=True)
        started = time.monotonic()
        previous = None
        try:
            while proc.poll() is None:
                try:
                    progress = json.loads(progress_file.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    progress = None
                if isinstance(progress, dict) and progress != previous:
                    progress_callback(progress)
                    previous = progress
                if time.monotonic() - started > 86400:
                    raise TimeoutError('训练输入准备超时')
                time.sleep(1)
            try:
                progress = json.loads(progress_file.read_text(encoding='utf-8'))
                if isinstance(progress, dict): progress_callback(progress)
            except (OSError, ValueError):
                pass
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        stdout.seek(0); stderr.seek(0)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout.read(), stderr.read())


def _queue_training_preparation(body, user, ctspine=False, mixed=False, model_family=None):
    selection = _phase2_training_input(body, user, mixed=mixed, prepare=False)
    run_id = ('ctspine_' if ctspine else 'tr_') + time.strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3)
    output = DATA_DIR / ('ctspine' if ctspine else 'phase2') / 'runs' / run_id
    config = dict(body.config) if not ctspine else {'model_type': 'ctspine', 'weight_id': body.weight_id, 'model_family': model_family, 'mode': body.mode, 'max_steps': body.max_steps}
    config.update(selection)
    config.update({'source_submission_ids': body.submission_ids, 'reference_dataset_ids': body.reference_dataset_ids, 'dry_run': body.dry_run, 'model_base': model_family if ctspine else body.model_base, 'debug_input': True, 'background_preparation': True})
    now = time.time()
    con = db()
    try:
        con.execute("INSERT INTO training_runs(id,dataset_version,config_json,status,progress,output_path,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, '', json.dumps(config, ensure_ascii=False), 'preparing', 0, str(output), user['id'], now, now))
        con.commit()
    finally: con.close()
    try:
        threading.Thread(target=_prepare_training_run, args=(run_id, body, {'id': user['id']}, config, ctspine, mixed), daemon=True).start()
    except Exception as exc:
        _phase2_update(run_id, status='failed', error='无法启动任务准备：' + str(exc))
        return {'id': run_id, 'status': 'failed', 'dry_run': body.dry_run, 'model_base': config['model_base']}
    return {'id': run_id, 'status': 'preparing', 'dry_run': body.dry_run, 'model_base': config['model_base']}


def _prepare_training_run(run_id, body, user, config, ctspine, mixed):
    def progress(value):
        total = config['preparation']['total']
        done = max(0, min(total, int(value.get('done', 0))))
        config['preparation'] = {'stage': str(value.get('stage', 'exporting')), 'done': done, 'total': total}
        _phase2_update(run_id, config_json=json.dumps(config, ensure_ascii=False), progress=(0.18 * done / total if total else 0))
    try:
        progress({'stage': 'exporting', 'done': 0})
        prepared = _phase2_prepare_dataset(DatasetExportIn(submission_ids=body.submission_ids, allow_single_modality=not mixed), user, completed=True, reference_case_ids=config['reference_case_ids'], reference_dataset_ids=body.reference_dataset_ids, progress_callback=progress)
        progress({'stage': 'ready', 'done': config['preparation']['total']})
        ready_body = body.model_copy(update={'dataset_version': prepared['id'], 'submission_ids': [], 'reference_case_ids': [], 'reference_dataset_ids': []})
        if ctspine:
            _start_ctspine_training(ready_body, user, existing_run_id=run_id)
        else:
            _start_phase2_training(ready_body, user, existing_run_id=run_id)
    except Exception as exc:
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        _phase2_update(run_id, status='failed', error=message[-2000:])


def _phase2_training_input(body, user, mixed=False, prepare=True):
    if body.submission_ids or body.reference_case_ids or body.reference_dataset_ids:
        if body.dataset_version:
            raise HTTPException(422, "请选择标注或整个导入目录数据集，不能同时指定")
        items = _phase2_selectable_submissions(completed=True)['items']
        available = {x['submission_id']: x for x in items if 'submission_id' in x}
        references = {x['reference_case_id']: x for x in items if 'reference_case_id' in x}
        groups = {x['reference_dataset_id']: x for x in items if 'reference_dataset_id' in x}
        ids, ref_ids, group_ids = set(body.submission_ids), set(body.reference_case_ids), set(body.reference_dataset_ids)
        if not ids.issubset(available) or not ref_ids.issubset(references) or not group_ids.issubset(groups):
            raise HTTPException(409, "所选标注或整个导入目录不可用，或已发生变化")
        selected = [available[x] for x in ids] + [references[x] for x in ref_ids] + [groups[x] for x in group_ids]
        expanded_refs = list(dict.fromkeys([*ref_ids, *(case_id for group_id in group_ids for case_id in _reference_dataset_case_ids(groups[group_id]['dataset_name'], groups[group_id]['source_directory']) )]))
        kinds = {x['modality'] for x in selected}
        if 'mri' in kinds:
            raise HTTPException(409, "MRI 在线训练尚未接入")
        if mixed and kinds != {'ct', 'video'}:
            raise HTTPException(409, "联合训练至少选择 CT 整个标注目录和视频完成标注")
        if isinstance(body, CTSpineTrainingIn) and kinds != {'ct'}:
            raise HTTPException(409, "CTSpine 只能选择 CT 标注目录")
        source_case_ids = [x['case_id'] for x in selected if 'case_id' in x and 'reference_dataset_id' not in x]
        if len(source_case_ids) != len(set(source_case_ids)):
            raise HTTPException(422, "同一病例不能同时选择人工提交和已有标签")
        if not prepare:
            return {'reference_case_ids': expanded_refs, 'input_modalities': {kind: 1 for kind in kinds}, 'preparation': {'stage': 'waiting', 'done': 0, 'total': len(expanded_refs) + len(ids)}}
        result = _phase2_prepare_dataset(DatasetExportIn(submission_ids=body.submission_ids, allow_single_modality=not mixed), user, completed=True, reference_case_ids=expanded_refs, reference_dataset_ids=body.reference_dataset_ids)
        return result['id'], True, expanded_refs
    if not body.dataset_version:
        raise HTTPException(422, "请选择已完成标注或已有数据集")
    # MRI remains a read-only training tab. Enforce this server-side as well,
    # since callers can bypass the browser and submit a dataset ID directly.
    con = db()
    dataset = con.execute("SELECT export_report FROM dataset_versions WHERE id=?", (body.dataset_version,)).fetchone()
    con.close()
    if dataset:
        try:
            report = json.loads(dataset["export_report"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            report = {}
        counts = report.get("modality_counts", {}) if isinstance(report, dict) else {}
        has_mri = False
        if isinstance(counts, dict):
            for key, value in counts.items():
                try:
                    positive = float(value or 0) > 0
                except (TypeError, ValueError):
                    positive = False
                if str(key).lower() in {"mr", "mri", "mr_dicom", "mri_dicom", "mr_nifti", "mri_nifti"} and positive:
                    has_mri = True
                    break
        if has_mri:
            raise HTTPException(409, "MRI 在线训练尚未接入")
    if not prepare:
        return {'dataset_version': body.dataset_version}
    return body.dataset_version, False, []


def _reference_dataset_case_ids(dataset_name, source_directory):
    con = db()
    try:
        result = []
        for row in con.execute("SELECT id,metadata FROM cases ORDER BY id"):
            try: metadata = json.loads(row['metadata'] or '{}')
            except (TypeError, ValueError): continue
            if str(metadata.get('import_dataset') or 'CT') != dataset_name or str(Path(str(metadata.get('source_relative') or row['id'])).parent) != source_directory:
                continue
            if metadata.get('imaging_type') != 'ct_nifti' or not (metadata.get('label_file') or metadata.get('label_files')):
                continue
            root = _case_root(row['id'], metadata)
            labels = list(metadata.get('label_files', []) or [])
            if metadata.get('label_file'): labels.append(metadata['label_file'])
            images = list(metadata.get('imaging_files', []) or [])
            if metadata.get('image_file'): images.append(metadata['image_file'])
            try: valid = root is not None and bool(labels) and bool(images) and all(_nifti_source_path(root, str(x)).is_file() for x in [*labels, *images])
            except (HTTPException, ValueError, OSError): valid = False
            if valid: result.append(row['id'])
        return result
    finally: con.close()


@app.post("/admin/ctspine/trainings")
def ctspine_training(body: CTSpineTrainingIn, admin=Depends(require_training_permission)):
    if body.submission_ids or body.reference_case_ids or body.reference_dataset_ids:
        con = db()
        weight = con.execute("SELECT * FROM ctspine_weight_registry WHERE id=? AND status='registered'", (body.weight_id,)).fetchone()
        con.close()
        if not weight:
            raise HTTPException(404, "权重未登记或不可用")
        family = str(weight['model_family']).lower()
        if body.mode == 'lora' and 'swin' not in family and 'transformer' not in family:
            raise HTTPException(409, "当前模型基座不支持已验证的 LoRA")
        return _queue_training_preparation(body, admin, ctspine=True, model_family=family)
    return _start_ctspine_training(body, admin)


def _start_ctspine_training(body, admin, existing_run_id=None):
    dataset_id, debug_input, expanded_refs = _phase2_training_input(body, admin)
    con = db(); dataset = con.execute("SELECT * FROM dataset_versions WHERE id=? AND status='frozen'", (dataset_id,)).fetchone(); weight = con.execute("SELECT * FROM ctspine_weight_registry WHERE id=? AND status='registered'", (body.weight_id,)).fetchone()
    if not dataset: con.close(); raise HTTPException(404, "数据集版本不存在或未冻结")
    if not weight: con.close(); raise HTTPException(404, "权重未登记或不可用")
    root = Path(str(dataset["root_path"])); manifest_ok = (root / "manifest.json").is_file()
    if not manifest_ok: con.close(); raise HTTPException(409, "CTSpine 数据集缺少标准 manifest.json（必须包含 train/val image/label 清单）")
    family = str(weight["model_family"]).lower()
    if body.mode == "lora" and "swin" not in family and "transformer" not in family: con.close(); raise HTTPException(409, "当前模型基座不支持已验证的 LoRA")
    run_id = existing_run_id or "ctspine_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3); output = DATA_DIR / "ctspine" / "runs" / run_id; now = time.time()
    config = {"debug_input": debug_input or bool(json.loads(dataset["export_report"] or "{}").get("debug_only")), "source_submission_ids": body.submission_ids, "reference_case_ids": expanded_refs, "reference_dataset_ids": body.reference_dataset_ids, "model_type": "ctspine", "model_base": family, "model_family": family, "weight_id": body.weight_id, "label_map": json.loads(weight["label_map_json"]), "mode": body.mode, "max_steps": body.max_steps, "dry_run": body.dry_run, "base_weight": body.weight_id}
    if existing_run_id:
        previous = con.execute("SELECT config_json FROM training_runs WHERE id=?", (run_id,)).fetchone()
        if not previous:
            con.close(); raise RuntimeError('任务记录不存在')
        pending_config = json.loads(previous['config_json'])
        for key in ('source_submission_ids', 'reference_case_ids', 'reference_dataset_ids', 'input_modalities', 'preparation', 'background_preparation'):
            if key in pending_config: config[key] = pending_config[key]
        con.execute("UPDATE training_runs SET dataset_version=?,config_json=?,status='queued',progress=0.2,updated_at=? WHERE id=?", (dataset_id, json.dumps(config, ensure_ascii=False), now, run_id))
    else:
        con.execute("INSERT INTO training_runs(id,dataset_version,config_json,status,progress,output_path,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, dataset_id, json.dumps(config, ensure_ascii=False), "queued", 0, str(output), admin["id"], now, now))
    con.commit(); con.close()
    _phase2_update(run_id, status="running")
    threading.Thread(target=_ctspine_run_thread, args=(run_id, root, weight, output, config), daemon=True).start()
    return {"id": run_id, "status": "queued", "mode": body.mode, "dry_run": body.dry_run}


@app.get("/admin/ctspine/trainings")
def ctspine_trainings(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM training_runs WHERE config_json LIKE '%\"model_type\": \"ctspine\"%' ORDER BY created_at DESC").fetchall(); con.close(); return {"items": [_training_run_item(row) for row in rows]}


@app.get("/admin/ctspine/trainings/{training_id}")
def ctspine_training_detail(training_id: str, admin=Depends(require_training_permission)):
    con = db(); row = con.execute("SELECT * FROM training_runs WHERE id=? AND config_json LIKE '%\"model_type\": \"ctspine\"%'", (training_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404, "训练任务不存在")
    item = dict(row)
    try: item["config"] = json.loads(item.pop("config_json") or "{}")
    except json.JSONDecodeError: item["config"] = {}
    return item


@app.post("/admin/ctspine/trainings/{training_id}/promote")
def ctspine_promote(training_id: str, admin=Depends(require_training_permission)):
    con = db(); run = con.execute("SELECT * FROM training_runs WHERE id=?", (training_id,)).fetchone()
    if not run: con.close(); raise HTTPException(404, "训练任务不存在")
    try: cfg = json.loads(run["config_json"] or "{}"); metrics = json.loads(Path(run["metrics_path"]).read_text()) if run["metrics_path"] else {}
    except (OSError, TypeError, json.JSONDecodeError): con.close(); raise HTTPException(409, "候选训练指标无效")
    if cfg.get("debug_input"):
        con.close(); raise HTTPException(409, "完成标注调试训练产物不能提升为在线候选")
    if run["status"] != "succeeded" or cfg.get("dry_run") or metrics.get("dry_run"): con.close(); raise HTTPException(409, "候选尚未成功或为 dry-run")
    weight = con.execute("SELECT * FROM ctspine_weight_registry WHERE id=? AND status='registered'", (cfg.get("weight_id"),)).fetchone()
    if not weight or not weight["license"] or not weight["source_url"] or not weight["label_map_json"]: con.close(); raise HTTPException(409, "基础权重登记信息不完整")
    required = ["macro_dice", "numbering_accuracy", "hd95", "failure_rate", "latency_p95_ms"]
    if any(k not in metrics or not isinstance(metrics[k], (int, float)) for k in required): con.close(); raise HTTPException(409, "缺少完整冻结验证指标")
    current = con.execute("SELECT * FROM model_versions WHERE status='published' ORDER BY created_at DESC").fetchall(); current_ct = None
    for row in current:
        try:
            if json.loads(row["metrics_json"] or "{}").get("model_type") == "ctspine": current_ct = row; break
        except json.JSONDecodeError: pass
    if current_ct:
        old = json.loads(current_ct["metrics_json"] or "{}")
        if float(metrics["macro_dice"]) < float(old.get("macro_dice", -1)) + .005 or float(metrics["numbering_accuracy"]) < float(old.get("numbering_accuracy", -1)) or float(metrics["hd95"]) > float(old.get("hd95", 1e99)) or float(metrics["failure_rate"]) > float(old.get("failure_rate", 1e99)) or float(metrics["latency_p95_ms"]) > float(old.get("latency_p95_ms", 1e99)):
            con.close(); raise HTTPException(409, "候选未通过 CTSpine 指标门禁")
    checkpoint = Path(run["output_path"]) / "best.pt"
    if not checkpoint.is_file(): con.close(); raise HTTPException(409, "训练产物缺少 best.pt")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest(); metrics.update({"model_type": "ctspine", "base_weight_id": cfg.get("weight_id"), "data_version": run["dataset_version"], "publishable": True}); version = "ctspine_" + time.strftime("%Y%m%d_%H%M%S") + "_" + digest[:8]
    con.execute("UPDATE model_versions SET status='retired' WHERE status='published' AND metrics_json LIKE '%\"model_type\": \"ctspine\"%'")
    con.execute("INSERT INTO model_versions(id,training_run,version,checkpoint_path,checkpoint_sha256,status,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (version, training_id, version, str(checkpoint), digest, "published", json.dumps(metrics, ensure_ascii=False), time.time())); con.commit(); con.close()
    return {"id": version, "status": "published", "checkpoint_sha256": digest}


@app.post("/admin/phase2/trainings")
def phase2_training(body: TrainingIn, admin=Depends(require_training_permission)):
    allowed_models = {"sam2.1_tiny", "sam2.1_small", "sam2.1_base_plus", "sam2.1_large", "medsam2_multimodal", "unet", "deeplabv3"}
    if body.model_base not in allowed_models:
        raise HTTPException(400, "不支持的模型基座")
    if body.submission_ids or body.reference_case_ids or body.reference_dataset_ids:
        return _queue_training_preparation(body, admin, mixed=body.model_base == 'medsam2_multimodal')
    return _start_phase2_training(body, admin)


def _start_phase2_training(body, admin, existing_run_id=None):
    allowed_models = {"sam2.1_tiny", "sam2.1_small", "sam2.1_base_plus", "sam2.1_large", "medsam2_multimodal", "unet", "deeplabv3"}
    if body.model_base not in allowed_models:
        raise HTTPException(400, "不支持的模型基座")
    dataset_id, debug_input, expanded_refs = _phase2_training_input(body, admin, mixed=body.model_base == "medsam2_multimodal")
    con = db(); dataset = con.execute("SELECT * FROM dataset_versions WHERE id=? AND status='frozen'", (dataset_id,)).fetchone()
    if not dataset: con.close(); raise HTTPException(404, "数据集版本不存在或未冻结")
    run_id = existing_run_id or "tr_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3); output = DATA_DIR / "phase2" / "runs" / run_id; now = time.time()
    config = dict(body.config); config["debug_input"] = debug_input or bool(json.loads(dataset["export_report"] or "{}").get("debug_only")); config["source_submission_ids"] = body.submission_ids; config["reference_case_ids"] = expanded_refs; config["reference_dataset_ids"] = body.reference_dataset_ids; config["model_base"] = body.model_base; config["dry_run"] = body.dry_run
    if body.model_base == "medsam2_multimodal":
        export_error = _frozen_multimodal_export_error(Path(str(dataset["root_path"] or "")))
        if export_error:
            con.close(); raise HTTPException(409, export_error)
        if not body.dry_run:
            config_error = _multimodal_training_config_error(config)
            if config_error:
                con.close(); raise HTTPException(409, config_error)
            config = _normalise_multimodal_training_config(config)
        readiness = _phase2_readiness()
        cuda_ready = next((item.get("available") for item in readiness["items"] if item.get("key") == "cuda"), False)
        ready = readiness.get("ready_for_dry_run") and (body.dry_run or cuda_ready)
        if not ready:
            con.close()
            mode = "dry-run" if body.dry_run else "正式训练"
            blockers = "；".join(str(item.get("message")) for item in readiness["items"] if not item.get("available", True))
            raise HTTPException(409, f"多模态 {mode} 未通过训练就绪检查: {blockers}")
    if existing_run_id:
        previous = con.execute("SELECT config_json FROM training_runs WHERE id=?", (run_id,)).fetchone()
        if not previous:
            con.close(); raise RuntimeError('任务记录不存在')
        pending_config = json.loads(previous['config_json'])
        for key in ('source_submission_ids', 'reference_case_ids', 'reference_dataset_ids', 'input_modalities', 'preparation', 'background_preparation'):
            if key in pending_config: config[key] = pending_config[key]
        con.execute("UPDATE training_runs SET dataset_version=?,config_json=?,status='queued',progress=0.2,updated_at=? WHERE id=?", (dataset_id, json.dumps(config, ensure_ascii=False), now, run_id))
    else:
        con.execute("INSERT INTO training_runs(id,dataset_version,config_json,status,progress,output_path,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, dataset_id, json.dumps(config, ensure_ascii=False), "queued", 0, str(output), admin["id"], now, now))
    con.commit(); con.close()
    threading.Thread(target=_run_phase2_training, args=(run_id, Path(dataset["root_path"]), output, config, body.dry_run), daemon=True).start()
    return {"id": run_id, "status": "queued", "model_base": body.model_base, "adapter": "train_medsam2_multimodal" if body.model_base == "medsam2_multimodal" else None, "dry_run": body.dry_run}


def _training_run_item(row):
    item = dict(row)
    try: config = json.loads(item.get('config_json') or '{}')
    except (TypeError, ValueError): config = {}
    if not isinstance(config, dict): config = {}
    item['modality_counts'] = config.get('input_modalities', {})
    item['preparation'] = config.get('preparation')
    return item


@app.get("/admin/phase2/trainings")
def phase2_trainings(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM training_runs ORDER BY created_at DESC").fetchall(); con.close(); return {"items": [_training_run_item(r) for r in rows]}


@app.get("/admin/phase2/trainings/{training_id}")
def phase2_training_detail(training_id: str, admin=Depends(require_training_permission)):
    con = db(); row = con.execute("SELECT * FROM training_runs WHERE id=?", (training_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404, "训练任务不存在")
    item = dict(row)
    try: item["config"] = json.loads(item.pop("config_json") or "{}")
    except json.JSONDecodeError: item["config"] = {}
    return item


@app.get("/admin/phase2/model-catalog")
def phase2_model_catalog(admin=Depends(require_training_permission)):
    medsam2_runtime_error = _medsam2_training_runtime_error()
    return {"items": [
        {"id": "sam2.1_tiny", "name": "SAM2.1 Tiny", "adapter": "train_sam2_native_noprompt", "status": "available", "notes": "当前可执行单帧内部 token 基线"},
        {"id": "sam2.1_small", "name": "SAM2.1 Small", "adapter": "train_sam2_native_noprompt", "status": "available", "notes": "需配置对应 checkpoint"},
        {"id": "sam2.1_base_plus", "name": "SAM2.1 Base+", "adapter": "train_sam2_native_noprompt", "status": "available", "notes": "需配置对应 checkpoint"},
        {"id": "sam2.1_large", "name": "SAM2.1 Large", "adapter": "train_sam2_native_noprompt", "status": "available", "notes": "需配置对应 checkpoint"},
        {
            "id": "medsam2_multimodal", "name": "MedSAM2 CT + 视频",
            "adapter": "train_medsam2_multimodal",
            "status": "unavailable" if medsam2_runtime_error else "available",
            "notes": medsam2_runtime_error or "本地官方 MedSAM2 训练适配器可用。仅完整 NPZ 数据、真实 CT/视频分模态评测和双端推理适配器均通过门禁的模型可以发布。",
        },
        {"id": "unet", "name": "U-Net", "adapter": "train_phase2_baseline", "status": "available", "notes": "轻量二值并集基线，适用于先验证数据链路"},
        {"id": "deeplabv3", "name": "DeepLabV3", "adapter": "train_phase2_baseline", "status": "available", "notes": "轻量二值并集基线，适用于先验证数据链路"},
    ]}


@app.post("/admin/phase2/models/{training_id}/publish")
def phase2_publish_model(training_id: str, admin=Depends(require_training_permission)):
    con = db(); run = con.execute("SELECT * FROM training_runs WHERE id=? AND status='succeeded'", (training_id,)).fetchone()
    if not run: con.close(); raise HTTPException(404, "训练任务不存在或尚未成功")
    checkpoint = Path(run["output_path"]) / "best.pt"
    if not checkpoint.is_file(): con.close(); raise HTTPException(409, "训练产物缺少 best.pt")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest(); version = "model_" + time.strftime("%Y%m%d_%H%M%S") + "_" + digest[:8]; now = time.time()
    metrics = Path(run["metrics_path"]).read_text(encoding="utf-8") if run["metrics_path"] and Path(run["metrics_path"]).is_file() else "{}"
    try:
        run_config = json.loads(run["config_json"] or "{}")
        metrics_payload = json.loads(metrics)
    except (TypeError, json.JSONDecodeError):
        con.close(); raise HTTPException(409, "训练配置或指标记录无效")
    if not isinstance(run_config, dict) or not isinstance(metrics_payload, dict):
        con.close(); raise HTTPException(409, "训练配置或指标记录无效")
    if run_config.get("debug_input"):
        con.close(); raise HTTPException(409, "完成标注调试训练产物不能直接发布，请使用审核数据集训练")
    model_base = run_config.get("model_base", "unknown")
    if bool(run_config.get("dry_run")) or bool(metrics_payload.get("dry_run")):
        con.close(); raise HTTPException(409, "dry-run 训练产物不能发布")
    if metrics_payload.get("status") == "failed":
        con.close(); raise HTTPException(409, "训练评估失败，不能发布模型")
    if not metrics_payload.get("val") and not metrics_payload.get("test") and metrics_payload.get("model_base", "").startswith("sam2.1"):
        con.close(); raise HTTPException(409, "缺少验证集或测试集指标，不能发布模型")
    metrics_payload["model_base"] = model_base
    metrics_payload["model_type"] = model_base
    metrics_payload["inference_backend"] = "sam2_prompt_hook" if model_base.startswith("sam2.1") else ("medsam2_multimodal_adapter" if model_base == "medsam2_multimodal" else "2d_segmentation")
    if model_base.startswith("sam2.1"):
        cfg = run_config
        metrics_payload["model_config"] = cfg.get("model_config") or "configs/sam2.1/sam2.1_hiera_t.yaml"
        metrics_payload["base_checkpoint"] = cfg.get("checkpoint") or str(ROOT / "third_party" / "sam2" / "sam2.1_hiera_tiny.pt")
    elif model_base == "medsam2_multimodal":
        config_error = _multimodal_training_config_error(run_config)
        if config_error:
            con.close(); raise HTTPException(409, config_error)
        metrics_payload["model_config"] = run_config["model_config"]
        metrics_payload["base_checkpoint"] = run_config.get("checkpoint") or run_config["base_checkpoint"]
        metrics_payload["inference_adapters"] = run_config["inference_adapters"]
    metrics = json.dumps(metrics_payload, ensure_ascii=False)
    artifact_error = _model_artifact_error({
        "checkpoint_path": str(checkpoint),
        "checkpoint_sha256": digest,
        "metrics_json": metrics,
        "training_run": training_id,
    }, con)
    if artifact_error:
        con.close(); raise HTTPException(409, artifact_error)
    con.execute("UPDATE model_versions SET status='retired' WHERE status='published'")
    con.execute("INSERT INTO model_versions(id,training_run,version,checkpoint_path,checkpoint_sha256,status,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (version, training_id, version, str(checkpoint), digest, "published", metrics, now)); audit(con, admin["id"], "phase2_model_publish", details=version); con.commit(); con.close()
    return {"id": version, "status": "published", "checkpoint_sha256": digest}


@app.get("/admin/phase2/models")
def phase2_models(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM model_versions ORDER BY created_at DESC").fetchall(); con.close(); return {"items": [dict(r) for r in rows]}


@app.get("/admin/phase2/models/{model_id}")
def phase2_model_detail(model_id: str, admin=Depends(require_training_permission)):
    con = db(); row = con.execute("SELECT * FROM model_versions WHERE id=? OR version=?", (model_id, model_id)).fetchone(); con.close()
    if not row: raise HTTPException(404, "模型版本不存在")
    item = dict(row)
    try: item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
    except json.JSONDecodeError: item["metrics"] = {}
    return item


def _predictor_conversion_source(checkpoint_path: str) -> dict[str, Path]:
    checkpoint = Path(checkpoint_path).absolute()
    allowed_roots = [ROOT / "models", DATA_DIR]
    configured_root = os.getenv("SWTOOL_PREDICTOR_SOURCE_ROOT", "").strip()
    if configured_root:
        allowed_roots.append(Path(configured_root))
    try:
        resolved = checkpoint.resolve(strict=True)
        if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
            raise HTTPException(409, "模型 checkpoint 超出允许的转换来源目录")
        if any(path.is_symlink() for path in (checkpoint, *checkpoint.parents)):
            raise HTTPException(409, "转换来源不能使用符号链接")
        if checkpoint.name not in {"checkpoint_best.pth", "checkpoint_final.pth"}:
            raise HTTPException(409, "该模型为二维 RGB 二值分割基线；当前 Predictor 使用三维 CT nnU-Net 预处理和滑窗，无法消费 best.pt，需适配独立模型类型及二维推理流程")
        if checkpoint.parent.name in {"lowres", "fullres"}:
            source = checkpoint.parent.parent
            directories = {name: source / name for name in ("lowres", "fullres")}
        elif checkpoint.parent.name.startswith("fold_"):
            configuration_dir = checkpoint.parent.parent
            pieces = configuration_dir.name.rsplit("__", 1)
            if len(pieces) != 2 or pieces[1] not in {"3d_lowres", "3d_fullres"}:
                raise HTTPException(409, "nnU-Net 来源必须是 3d_lowres 或 3d_fullres 训练配置目录")
            directories = {name: configuration_dir.parent / f"{pieces[0]}__3d_{name}" / checkpoint.parent.name for name in ("lowres", "fullres")}
        else:
            raise HTTPException(409, "转换需要 lowres/fullres 模型目录或标准 nnU-Net 配置目录下的 fold_N checkpoint")
        for name, directory in directories.items():
            metadata = directory if (directory / "plans.json").is_file() else directory.parent
            chosen = checkpoint if directory == checkpoint.parent else directory / ("checkpoint_best.pth" if (directory / "checkpoint_best.pth").is_file() else "checkpoint_final.pth")
            required = [chosen, metadata / "plans.json", metadata / "dataset.json"]
            for path in required:
                if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents)):
                    raise HTTPException(409, "一键转换需要完整且非符号链接的 lowres/fullres checkpoint、plans.json 和 dataset.json")
        return directories
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, "转换来源模型文件不存在或路径无效") from exc


@app.post("/admin/phase2/models/{model_id}/convert-predictor")
def phase2_convert_predictor(model_id: str, admin=Depends(require_training_permission)):
    con = db()
    try:
        row = con.execute("SELECT * FROM model_versions WHERE (id=? OR version=?) AND status='published'", (model_id, model_id)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "已发布模型不存在")
    source = _predictor_conversion_source(str(row["checkpoint_path"] or ""))
    if not hmac.compare_digest(hashlib.sha256(Path(row["checkpoint_path"]).read_bytes()).hexdigest(), str(row["checkpoint_sha256"] or "")):
        raise HTTPException(409, "已发布 checkpoint 摘要校验失败，不能转换")
    if not _predictor_signing_ready():
        raise HTTPException(503, "模型签发私钥未就绪，请管理员配置 SWTOOL_PREDICTOR_SIGNING_KEY")
    target = Path(os.getenv("SWTOOL_PREDICTOR_MODEL_ROOT", os.getenv("PREDICTOR_MODEL_ROOT", str(ROOT / "predictor" / "demo_data")))).absolute()
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise HTTPException(409, "密文模型目标目录不能使用符号链接")
    exporter = _script_path(CODE_ROOT / "predictor" / "tools" / "export_onnx.py")
    issuer = _script_path(CODE_ROOT / "predictor" / "tools" / "model_manifest_issue.py")
    export_python = os.getenv("SWTOOL_PREDICTOR_EXPORT_PYTHON", "").strip() or sys.executable
    if not exporter.is_file() or not issuer.is_file():
        raise HTTPException(503, "模型转换工具尚未部署")
    with _PREDICTOR_RELEASE_LOCK:
        try:
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".predictor-convert-", dir=target) as staging_dir:
                staging = Path(staging_dir)
                files = ["lowres.onnx.enc", "fullres.onnx.enc", "lowres.onnx.enc.json", "fullres.onnx.enc.json", "predictor.models"]
                for filename in files:
                    if (target / filename).is_symlink():
                        raise HTTPException(409, "密文模型目标文件不能使用符号链接")
                for name in ("lowres", "fullres"):
                    command = [export_python, str(exporter), str(source[name]), "--output", str(staging / f"{name}.onnx.enc"), "--encrypted-output"]
                    if source[name] == Path(row["checkpoint_path"]).absolute().parent:
                        command.extend(["--checkpoint", Path(row["checkpoint_path"]).name])
                    subprocess.run(command, check=True, capture_output=True, timeout=1800)
                subprocess.run([export_python, str(issuer), "--lowres", str(staging / files[0]), "--fullres", str(staging / files[1]), "--private-key", str(PREDICTOR_SIGNING_KEY), "--output", str(staging / files[-1])], check=True, capture_output=True, timeout=120)
                if any(path.is_symlink() for path in (PREDICTOR_RELEASE_DIR, *PREDICTOR_RELEASE_DIR.parents)):
                    raise HTTPException(409, "发行目录不能使用符号链接")
                PREDICTOR_RELEASE_DIR.mkdir(parents=True, exist_ok=True)
                package_name = "swtool-predictor-models.zip"
                if (PREDICTOR_RELEASE_DIR / package_name).is_symlink():
                    raise HTTPException(409, "模型更新包不能使用符号链接")
                with zipfile.ZipFile(staging / package_name, "w", compression=zipfile.ZIP_STORED) as package:
                    for filename in files:
                        package.write(staging / filename, filename if filename == "predictor.models" else f"share/predictor/demo_data/{filename}")
                    package.writestr("model-version.json", json.dumps({"model_id": row["id"], "version": row["version"], "checkpoint_sha256": row["checkpoint_sha256"]}, ensure_ascii=False))
                with tempfile.NamedTemporaryFile(prefix=".predictor-models-", dir=PREDICTOR_RELEASE_DIR, delete=False) as release_temp:
                    release_temp_path = Path(release_temp.name)
                try:
                    shutil.copyfile(staging / package_name, release_temp_path)
                    os.replace(release_temp_path, PREDICTOR_RELEASE_DIR / package_name)
                finally:
                    release_temp_path.unlink(missing_ok=True)
                for filename in files:
                    os.replace(staging / filename, target / filename)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "模型转换超时，请检查训练模型与运行环境") from exc
        except subprocess.CalledProcessError as exc:
            LOGGER.warning("Predictor conversion failed for %s, exit code %s", row["id"], exc.returncode)
            raise HTTPException(409, "模型转换或签名失败，请检查 nnU-Net 模型格式及转换依赖") from exc
        except OSError as exc:
            raise HTTPException(503, "无法写入密文模型目录，请检查配置和权限") from exc
        with _predictor_distribution_db() as con:
            audit(con, admin["id"], "predictor_model_convert", details=str(row["id"]))
    return {"model_id": row["id"], "files": files[:-1], "manifest": files[-1], "release_id": "model-package"}


@app.get("/models/published")
def published_models(user=Depends(require_role("annotator"))):
    con = db(); rows = con.execute("SELECT id,version,checkpoint_sha256,metrics_json,created_at FROM model_versions WHERE status='published' ORDER BY created_at DESC").fetchall(); con.close()
    items = []
    for row in rows:
        item = dict(row)
        try:
            meta = json.loads(item.pop("metrics_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        item["model_type"] = meta.get("model_type", meta.get("model_base"))
        item["inference_backend"] = meta.get("inference_backend")
        items.append(item)
    return {"items": items}


@app.get("/models/current")
def current_model(user=Depends(require_role("annotator"))):
    con = db(); row = con.execute("SELECT id,version,checkpoint_path,checkpoint_sha256,metrics_json,created_at FROM model_versions WHERE status='published' ORDER BY created_at DESC LIMIT 1").fetchone(); con.close()
    if not row: return {"model": None}
    item = dict(row)
    try: item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
    except json.JSONDecodeError: item["metrics"] = {}
    return {"model": item}


@app.post("/admin/phase2/models/{model_id}/rollback")
def phase2_rollback_model(model_id: str, admin=Depends(require_training_permission)):
    con = db(); target = con.execute("SELECT * FROM model_versions WHERE (id=? OR version=?)", (model_id, model_id)).fetchone()
    if not target: con.close(); raise HTTPException(404, "模型版本不存在")
    artifact_error = _model_artifact_error(target, con)
    if artifact_error:
        con.close(); raise HTTPException(409, artifact_error)
    con.execute("UPDATE model_versions SET status='retired' WHERE status='published'")
    con.execute("UPDATE model_versions SET status='published' WHERE id=?", (target["id"],)); audit(con, admin["id"], "phase2_model_rollback", details=target["version"]); con.commit(); con.close()
    return {"id": target["id"], "version": target["version"], "status": "published"}


@app.get("/admin/phase2/inference-runs")
def phase2_inference_runs(admin=Depends(require_training_permission)):
    con = db(); rows = con.execute("SELECT * FROM inference_runs ORDER BY created_at DESC LIMIT 200").fetchall(); con.close(); return {"items": [dict(r) for r in rows]}


def _run_multimodal_independent_evaluation(model: object, dataset: object, output: Path) -> dict:
    """Run fresh predictions and evaluation for a frozen dataset.

    This deliberately accepts no training metrics or prediction directory: both
    same-dataset and cross-dataset evaluations must start from the published
    checkpoint and the selected frozen ``medsam2_npz`` export.
    """
    dataset_root = Path(str(dataset["root_path"] or ""))
    dataset_error = _frozen_multimodal_export_error(dataset_root)
    if dataset_error:
        raise RuntimeError(dataset_error)
    checkpoint = Path(str(model["checkpoint_path"] or ""))
    if not checkpoint.is_file() or checkpoint.stat().st_size <= 0:
        raise RuntimeError("发布模型 checkpoint 不存在或为空")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, str(model["checkpoint_sha256"] or "")):
        raise RuntimeError("发布模型 checkpoint SHA256 校验失败")
    try:
        metadata = json.loads(model["metrics_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("发布模型的指标元数据无效") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("发布模型的指标元数据无效")
    model_config = metadata.get("model_config")
    if not isinstance(model_config, str) or not model_config.strip():
        raise RuntimeError("发布模型缺少 model_config")
    try:
        model_config, _ = _medsam2_model_config(model_config)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    predictor = _script_path(ROOT / "scripts" / "medsam2_multimodal_predict.py")
    evaluator = _script_path(ROOT / "scripts" / "medsam2_multimodal_eval.py")
    if not predictor.is_file() or not evaluator.is_file():
        raise RuntimeError("MedSAM2 独立预测器或评测器不存在")
    npz_root = dataset_root / "medsam2_npz"
    predictions = output / "predictions"
    report_path = output / "metrics.json"
    output.mkdir(parents=True, exist_ok=False)
    try:
        timeout = int(_env("SWTOOL_MM_EVALUATION_TIMEOUT_SEC", "WOMANSEG_MM_EVALUATION_TIMEOUT_SEC", "86400"))
    except ValueError as exc:
        raise RuntimeError("多模态独立评测超时配置无效") from exc
    if timeout < 1:
        raise RuntimeError("多模态独立评测超时配置无效")
    device = _env("SWTOOL_MM_EVALUATION_DEVICE", "WOMANSEG_MM_EVALUATION_DEVICE", "cuda").strip() or "cuda"
    commands = (
        [
            sys.executable, str(predictor), "--dataset", str(npz_root),
            "--checkpoint", str(checkpoint), "--output", str(predictions),
            "--model-config", model_config, "--device", device,
        ],
        [
            sys.executable, str(evaluator), "--dataset", str(npz_root),
            "--predictions", str(predictions), "--output", str(report_path),
        ],
    )
    for command, stage in zip(commands, ("模型预测", "指标评测")):
        try:
            process = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"多模态独立{stage}超时") from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "未知错误")[-2000:]
            raise RuntimeError(f"多模态独立{stage}失败: {detail}")
    if not report_path.is_file():
        raise RuntimeError("独立评测器未生成 metrics.json")
    try:
        report = _normalise_multimodal_metrics(json.loads(report_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"独立评测报告无效: {exc}") from exc
    regression_error = _multimodal_regression_error(report)
    if regression_error:
        raise RuntimeError(regression_error)
    report.update({
        "model_version": model["version"],
        "dataset_version": dataset["id"],
        "method": "published_checkpoint_independent_medsam2_evaluation",
        "checkpoint_sha256": digest,
        "model_config": model_config,
    })
    return report


def _run_legacy_independent_evaluation(model: object, dataset: object) -> dict:
    root = Path(dataset["root_path"])
    frames = list((root / "frames").rglob("*.png")) if (root / "frames").is_dir() else []
    masks = list((root / "masks").rglob("*.png")) if (root / "masks").is_dir() else []
    report = {
        "model_version": model["version"], "dataset_version": dataset["id"],
        "frames": len(frames), "masks": len(masks),
        "mask_coverage": round(len(masks) / len(frames), 4) if frames else 0,
        "method": "测试集 oracle-point 评估", "dice_mean": None, "iou_mean": None,
        "evaluated_frames": 0,
        "note": "正向点来自真实掩码中心，仅用于模型回归，不代表完全无提示指标",
    }
    try:
        split = json.loads((root / "splits.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("数据集缺少有效 splits.json") from exc
    if not isinstance(split, dict) or not isinstance(split.get("test"), list):
        raise RuntimeError("数据集缺少有效 splits.json")
    test_cases = set(split["test"])
    test_frames = [path for path in frames if path.parts[-2] in test_cases]
    if not test_frames:
        raise RuntimeError("测试集为空，无法计算 Dice/IoU")
    import numpy as np
    import cv2
    from PIL import Image
    from scripts.sam2_prompt_hook import predict
    meta = json.loads(model["metrics_json"] or "{}")
    config = meta.get("model_config")
    checkpoint = meta.get("base_checkpoint")
    if not config or not checkpoint:
        raise RuntimeError("发布模型缺少 SAM2 基座配置或 checkpoint")
    dices = []
    ious = []
    for frame_path in test_frames[:200]:
        case_id = frame_path.parts[-2]
        frame_no = int(frame_path.stem)
        image = Image.open(frame_path)
        gt = np.zeros((image.height, image.width), dtype=np.uint8)
        for mask_path in (root / "masks" / case_id).glob("*/*.png"):
            if mask_path.stem == frame_path.stem:
                gt = np.maximum(gt, np.asarray(Image.open(mask_path).convert("L")))
        ys, xs = np.where(gt > 0)
        if not len(xs):
            continue
        result = predict({
            "source": str(root / "frames" / case_id), "frame": frame_no, "organ": "organ",
            "prompts": [{"x": int(xs.mean()), "y": int(ys.mean()), "label": 1}],
            "model_config": config, "model_checkpoint": checkpoint,
            "model_weights": model["checkpoint_path"],
        })
        if not result.get("ok"):
            raise RuntimeError(result.get("error", {}).get("message", "模型推理失败"))
        pred = np.zeros_like(gt)
        points = np.asarray(result.get("mask_points") or [], dtype=np.int32)
        if len(points) >= 3:
            cv2.fillPoly(pred, [points[:, :2]], 255)
        inter = np.logical_and(gt > 0, pred > 0).sum()
        union = np.logical_or(gt > 0, pred > 0).sum()
        dices.append(float(2 * inter / (int((gt > 0).sum()) + int((pred > 0).sum()) + 1e-8)))
        ious.append(float(inter / (union + 1e-8)))
    report["evaluated_frames"] = len(dices)
    if not dices or not ious:
        raise RuntimeError("测试集没有有效评分帧，无法计算 Dice/IoU")
    report["dice_mean"] = float(np.mean(dices))
    report["iou_mean"] = float(np.mean(ious))
    if not math.isfinite(report["dice_mean"]) or not math.isfinite(report["iou_mean"]):
        raise RuntimeError("独立评估指标为空或无效")
    return report


def _run_phase3_evaluation(
    run_id: str, model: dict, dataset: dict, trained_dataset: str | None, multimodal: bool,
) -> None:
    con = db()
    try:
        con.execute("UPDATE evaluation_runs SET status=?,error=? WHERE id=?", ("running", None, run_id))
        con.commit()
        try:
            if multimodal:
                report = _run_multimodal_independent_evaluation(
                    model, dataset, DATA_DIR / "evaluation_runs" / run_id,
                )
                report["training_dataset_version"] = trained_dataset
            else:
                report = _run_legacy_independent_evaluation(model, dataset)
        except Exception as exc:
            error = str(exc)
            row = con.execute("SELECT report_json FROM evaluation_runs WHERE id=?", (run_id,)).fetchone()
            try:
                failed_report = json.loads(row["report_json"] or "{}") if row else {}
            except (TypeError, json.JSONDecodeError):
                failed_report = {}
            if not isinstance(failed_report, dict):
                failed_report = {}
            failed_report["note"] = error
            con.execute(
                "UPDATE evaluation_runs SET status=?,report_json=?,error=? WHERE id=?",
                ("failed", json.dumps(failed_report, ensure_ascii=False), error, run_id),
            )
            con.commit()
            return
        con.execute(
            "UPDATE evaluation_runs SET status=?,report_json=?,error=? WHERE id=?",
            ("succeeded", json.dumps(report, ensure_ascii=False), None, run_id),
        )
        con.commit()
    finally:
        con.close()


@app.post("/admin/phase3/evaluations")
def phase3_evaluate(body: EvaluationIn, admin=Depends(require_role("admin"))):
    con = db()
    model = con.execute("SELECT * FROM model_versions WHERE (id=? OR version=?) AND status='published'", (body.model_version, body.model_version)).fetchone()
    dataset = con.execute("SELECT * FROM dataset_versions WHERE id=? AND status='frozen'", (body.dataset_version,)).fetchone()
    if not model or not dataset:
        con.close()
        raise HTTPException(404, "模型或数据集不存在/未发布")
    try:
        model_metrics = json.loads(model["metrics_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        con.close()
        raise HTTPException(409, "发布模型的指标元数据无效")
    if not isinstance(model_metrics, dict):
        con.close()
        raise HTTPException(409, "发布模型的指标元数据无效")
    multimodal = model_metrics.get("model_base") == "medsam2_multimodal"
    trained_dataset = None
    if multimodal:
        training = con.execute("SELECT dataset_version FROM training_runs WHERE id=?", (model["training_run"],)).fetchone()
        trained_dataset = training["dataset_version"] if training else None
    run_id = "ev_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    initial_report = {
        "model_version": model["version"], "dataset_version": body.dataset_version,
        "training_dataset_version": trained_dataset,
        "method": (
            "published_checkpoint_independent_medsam2_evaluation"
            if multimodal else "测试集 oracle-point 评估"
        ),
    }
    if multimodal:
        initial_report["modalities"] = {}
    con.execute(
        "INSERT INTO evaluation_runs(id,model_version,dataset_version,status,report_json,error,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, model["version"], body.dataset_version, "queued", json.dumps(initial_report, ensure_ascii=False), None, admin["id"], time.time()),
    )
    con.commit()
    con.close()
    threading.Thread(
        target=_run_phase3_evaluation,
        args=(run_id, dict(model), dict(dataset), trained_dataset, multimodal),
        name=f"swtool-phase3-evaluation-{run_id}", daemon=True,
    ).start()
    return {"id": run_id, "status": "queued", "report": initial_report}


@app.get("/admin/phase3/evaluations")
def phase3_evaluations(admin=Depends(require_role("admin"))):
    con = db(); rows = con.execute("SELECT * FROM evaluation_runs ORDER BY created_at DESC LIMIT 200").fetchall(); con.close()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["report"] = json.loads(item.get("report_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["report"] = {}
        items.append(item)
    return {"items": items}


@app.get("/admin/phase3/evaluations/{evaluation_id}")
def phase3_evaluation_detail(evaluation_id: str, admin=Depends(require_role("admin"))):
    con = db(); row = con.execute("SELECT * FROM evaluation_runs WHERE id=?", (evaluation_id,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "评估任务不存在")
    item = dict(row)
    try:
        item["report"] = json.loads(item.get("report_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["report"] = {}
    return item


@app.get("/admin/phase3/summary")
def phase3_summary(admin=Depends(require_role("admin"))):
    con = db(); rows = con.execute("SELECT * FROM evaluation_runs ORDER BY created_at DESC LIMIT 20").fetchall(); con.close()
    items=[]
    for row in rows:
        item=dict(row)
        try: item["report"]=json.loads(item.pop("report_json") or "{}")
        except json.JSONDecodeError: item["report"]={}
        items.append(item)
    return {"items": items, "latest": items[0] if items else None}


@app.post("/cases/{case_id}/inference")
def inference(case_id: str, body: InferenceIn, lease_token: Annotated[str, Header()] = "", user=Depends(require_role("annotator"))):
    con = db(); check_lease(con, case_id, user["id"], lease_token); con.close()
    published = None
    model_meta: dict = {}
    if body.model_version:
        con = db()
        published = con.execute(
            "SELECT id,version,checkpoint_path,training_run,metrics_json FROM model_versions WHERE (id=? OR version=?) AND status='published'",
            (body.model_version, body.model_version),
        ).fetchone()
        con.close()
        if not published:
            raise HTTPException(404, "模型版本不存在或尚未发布")
        try:
            model_meta = json.loads(published["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raise HTTPException(409, "发布模型元数据无效，不能用于推理")
    con = db(); case = con.execute("SELECT metadata FROM cases WHERE id=?", (case_id,)).fetchone(); con.close()
    try:
        metadata = json.loads(case["metadata"] or "{}") if case else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if metadata.get("imaging_type") in {"ct_nifti", "mri_nifti"}:
        return _ct_inference_error(case_id, user["id"], "MODEL_ADAPTER_UNSUPPORTED", "原始 NIfTI 影像支持查看和手工标注，当前候选模型尚未适配该格式", body.model_version)
    if metadata.get("imaging_type") == "ct_dicom":
        if body.model_version and model_meta.get("model_base") != "medsam2_multimodal":
            return _ct_inference_error(case_id, user["id"], "MODEL_ADAPTER_UNSUPPORTED", "当前发布模型没有 CT 提示推理适配器", body.model_version)
        if body.model_version and not _env("SWTOOL_CT_HOOK", "WOMANSEG_CT_HOOK").strip():
            return _ct_inference_error(case_id, user["id"], "MODEL_ADAPTER_UNSUPPORTED", "内置 CTLesion 适配器不能加载已发布的 MedSAM2 多模态权重；请配置经验证的 CT 多模态推理适配器", body.model_version)
        available_series = metadata.get("dicom_series") if isinstance(metadata.get("dicom_series"), list) else []
        series_id = body.series.strip()
        series = next((item for item in available_series if isinstance(item, dict) and item.get("id") == series_id), None)
        if not series:
            return _ct_inference_error(case_id, user["id"], "CT_SERIES_INVALID", "请选择当前 CT 病例中有效的序列后再生成候选")
        try:
            # Recheck containment and existence before the external hook sees a path.
            # No patient name or DICOM metadata is placed in the hook payload.
            source = _dicom_slice_path(case_id, metadata, series_id, body.frame)
            prompts = _ct_prompt_payload(body.prompts)
        except HTTPException as exc:
            return _ct_inference_error(case_id, user["id"], "CT_SOURCE_UNAVAILABLE", str(exc.detail))
        except ValueError as exc:
            return _ct_inference_error(case_id, user["id"], "CT_PROMPTS_INVALID", str(exc))
        try:
            from scripts.ct_prompt_hook import (
                CTHookExecutionError,
                CTHookProtocolError,
                CTHookUnavailableError,
                predict as ct_predict,
            )
            payload = {
                "modality": "ct_dicom",
                "case_id": case_id,
                "series": series_id,
                "source": str(source),
                "frame": body.frame,
                "organ": body.organ,
                "prompts": prompts,
                "window_center": body.window_center if body.window_center is not None else 40.0,
                "window_width": body.window_width if body.window_width is not None else 400.0,
            }
            if published:
                payload.update({
                    "model_version": published["version"],
                    "model_type": model_meta.get("model_type", model_meta.get("model_base")),
                    "model_weights": published["checkpoint_path"],
                    "model_config": model_meta.get("model_config"),
                })
            result = ct_predict(payload)
            adapter_model_version = result["model_version"]
            model_version = body.model_version or adapter_model_version
            if not result.get("ok"):
                message = str((result.get("error") or {}).get("message") or "CT 专用模型未生成候选")
                return _ct_inference_error(case_id, user["id"], "CT_MODEL_FAILED", message, model_version)
            result.update({"model_version": model_version, "modality": "ct_dicom"})
            if published:
                result["inference_backend"] = model_meta.get("inference_backend")
            result["inference_run_id"] = _record_inference_run(case_id, model_version, "interactive_ct", "succeeded" if result.get("ok") else "failed", None, user["id"])
            return result
        except CTHookUnavailableError as exc:
            return _ct_inference_error(case_id, user["id"], "CT_MODEL_UNAVAILABLE", str(exc))
        except (CTHookExecutionError, CTHookProtocolError) as exc:
            return _ct_inference_error(case_id, user["id"], "CT_MODEL_FAILED", str(exc))
        except Exception:
            # Avoid exposing configured command details or filesystem paths.
            return _ct_inference_error(case_id, user["id"], "CT_MODEL_FAILED", "CT 专用模型推理失败，请联系系统管理员检查院内部署的 CT 模型适配器")
    source = _case_source(case_id, body.video)
    if not source:
        return {"ok": False, "error": {"code": "FRAME_UNAVAILABLE", "message": "案例暂无帧目录"}}
    try:
        model_config = None; model_checkpoint = None
        model_weights = published["checkpoint_path"] if body.model_version and published else None
        if published:
            if model_meta.get("model_base") not in {"sam2.1_tiny", "sam2.1_small", "sam2.1_base_plus", "sam2.1_large", "medsam2_multimodal"}:
                raise HTTPException(409, "当前发布模型没有视频提示推理适配器")
            model_config = model_meta.get("model_config")
            model_checkpoint = model_meta.get("base_checkpoint")
            if model_meta.get("model_base") == "medsam2_multimodal" and not model_checkpoint:
                raise HTTPException(409, "发布的 MedSAM2 多模态模型缺少基础 checkpoint 元数据")
            if not model_config:
                c2 = db(); run = c2.execute("SELECT config_json FROM training_runs WHERE id=?", (published["training_run"],)).fetchone(); c2.close()
                model_config = (json.loads(run["config_json"]) if run else {}).get("model_config")
            if model_meta.get("model_base") == "medsam2_multimodal":
                adapters = model_meta.get("inference_adapters")
                if not isinstance(adapters, dict) or adapters.get("video") != "medsam2_video_hook":
                    raise HTTPException(409, "发布的 MedSAM2 模型未声明 medsam2_video_hook 视频适配器")
                if not model_config or not model_checkpoint or not model_weights:
                    raise HTTPException(409, "发布的 MedSAM2 模型缺少视频推理所需权重元数据")
                from scripts.medsam2_video_prompt_hook import predict
            else:
                from scripts.sam2_prompt_hook import predict
        else:
            from scripts.sam2_prompt_hook import predict
        result = predict({"source": str(source), "frame": body.frame, "organ": body.organ, "prompts": body.prompts, "model_config": model_config, "model_checkpoint": model_checkpoint, "model_weights": model_weights})
        result["model_version"] = body.model_version or "prompt_hook"
        run_id = "inf_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3); now = time.time(); con = db()
        failed_message = str((result.get("error") or {}).get("message") or "")
        con.execute("INSERT INTO inference_runs(id,case_id,model_version,prompt_source,status,output_path,error,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, case_id, body.model_version, "interactive", "succeeded" if result.get("ok") else "failed", None, failed_message or None, user["id"], now, now)); con.commit(); con.close(); result["inference_run_id"] = run_id
        return result
    except Exception as exc:
        run_id = "inf_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3); now = time.time(); con = db(); con.execute("INSERT INTO inference_runs(id,case_id,model_version,prompt_source,status,output_path,error,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, case_id, body.model_version, "interactive", "failed", None, str(exc), user["id"], now, now)); con.commit(); con.close()
        return {"ok": False, "error": {"code": "SAM2_UNAVAILABLE", "message": str(exc)}}


@app.get("/ct/model-status")
def ct_model_status(user=Depends(require_role("annotator"))):
    """Expose only CT adapter readiness, never its command or local paths."""
    from scripts.ct_prompt_hook import is_configured
    configured = is_configured()
    return {
        "configured": configured,
        "backend": "external_ct_hook" if _env("SWTOOL_CT_HOOK", "WOMANSEG_CT_HOOK").strip() else "medsam2_ctlesion",
        "message": "已启用 CT 单切片点提示候选模型，结果须人工审核和修正" if configured else "尚未配置 CT 专用候选模型，可继续手工标注",
    }


@app.post("/cases/{case_id}/inference/batch")
def batch_inference(case_id: str, body: BatchInferenceIn, lease_token: Annotated[str, Header()] = "", user=Depends(require_role("annotator"))):
    """对关键帧批量执行推理；没有提示的帧会明确返回跳过，避免伪造结果。"""
    con = db(); check_lease(con, case_id, user["id"], lease_token); con.close()
    results = []
    for frame in body.frames:
        prompts = body.prompts_by_frame.get(str(frame), [])
        if not prompts:
            results.append({"frame": frame, "ok": False, "skipped": True, "reason": "PROMPTS_REQUIRED"}); continue
        try:
            result = inference(case_id, InferenceIn(frame=frame, organ=body.organ, prompts=prompts, video=body.video, series=body.series, model_version=body.model_version), lease_token, user)
            results.append({"frame": frame, **result})
        except HTTPException as exc:
            results.append({"frame": frame, "ok": False, "error": {"code": "HTTP_ERROR", "message": str(exc.detail)}})
    return {"items": results, "total": len(body.frames), "completed": sum(1 for x in results if x.get("ok"))}


@app.post("/cases/{case_id}/annotations")
def upload_annotation(case_id: str, lease_token: Annotated[str, Header()], version: Annotated[str, Header()], sha256: Annotated[str, Header()], file: UploadFile = File(...), user=Depends(require_role("annotator"))):
    con = db()
    committed = False
    try:
        with _case_draft_lock(case_id):
            case = require_active_annotation_case(con, case_id, user["id"], lease_token)
            if case["assigned_reviewer_id"] is None:
                raise HTTPException(409, "案例尚未分配审核员")
            if not version or len(sha256) != 64:
                raise HTTPException(400, "版本或 sha256 无效")
            try:
                metadata = json.loads(case["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            case_root = _case_root(case_id, metadata)
            if case_root is None:
                raise HTTPException(400, "病例目录无效")
            case_dir = case_root / "versions" / version / "annotations"
            case_dir.mkdir(parents=True, exist_ok=True)
            target = case_dir / "annotation.zip"
            raw = file.file.read()
            with target.open("wb") as out:
                out.write(raw)
            digest_ctx = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_ctx.update(chunk)
            digest = digest_ctx.hexdigest()
            if not hmac.compare_digest(digest, sha256):
                target.unlink(missing_ok=True)
                raise HTTPException(400, "sha256 不匹配")
            try:
                with zipfile.ZipFile(target) as z:
                    bad = [n for n in z.namelist() if n.startswith("frames/") or Path(n).suffix.lower() in {".mp4", ".avi", ".mov", ".mkv"} or Path(n).is_absolute() or ".." in Path(n).parts]
                    if bad:
                        raise HTTPException(400, "标注包不得包含原始视频或 frames/")
                    z.extractall(case_dir)
            except zipfile.BadZipFile:
                # Browser clients may submit a JSON delta directly; it still cannot contain source media.
                if file.content_type != "application/json":
                    raise HTTPException(400, "标注包必须为 zip 或 application/json")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPException(400, "JSON 标注内容无效") from exc
                (case_dir / "annotation.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rid = case["assigned_reviewer_id"]
            try:
                con.execute("INSERT INTO submissions(case_id,annotator_id,reviewer_id,version,sha256,path,status) VALUES(?,?,?,?,?,?,?)", (case_id,user["id"],rid,version,sha256,str(case_dir),"submitted"))
            except DBIntegrityError:
                pass
            con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
            con.execute("UPDATE cases SET status='annotation_submitted' WHERE id=? AND assigned_annotator_id=? AND status IN ('annotation_in_progress','rejected')", (case_id, user["id"]))
            audit(con, user["id"], "annotation_submit", case_id, version)
            con.commit()
            committed = True
            draft_cleanup_pending = False
            try:
                _remove_active_draft(case_id)
            except OSError as exc:
                # Keep the committed version intact; the audit event marks this
                # draft for an administrative retry after the lock is released.
                draft_cleanup_pending = True
                LOGGER.exception("已提交标注的草稿清理失败: %s", case_id)
                try:
                    audit(con, user["id"], "annotation_draft_cleanup_pending", case_id, str(exc))
                    con.commit()
                except Exception:
                    LOGGER.exception("草稿待清理审计记录失败: %s", case_id)
            return {"case_id": case_id, "version": version, "status": "annotation_submitted", "reviewer_id": rid, "draft_cleanup_pending": draft_cleanup_pending}
    except Exception:
        if not committed:
            try:
                con.rollback()
            except Exception:
                LOGGER.exception("提交标注失败后的数据库回滚失败: %s", case_id)
        raise
    finally:
        con.close()


@app.post("/cases/{case_id}/review")
def review(case_id: str, decision: str, note: str = "", user=Depends(require_role("annotator"))):
    if decision not in {"approve", "reject"}: raise HTTPException(400, "decision 必须为 approve/reject")
    con = db(); row = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row or row["assigned_reviewer_id"] != user["id"]: con.close(); raise HTTPException(403, "该案例未分配给你")
    if row["status"] != "annotation_submitted":
        con.close(); raise HTTPException(409, "案例当前没有待审核标注")
    submission = con.execute("SELECT annotator_id FROM submissions WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if not submission: con.close(); raise HTTPException(409, "案例尚无待审核标注")
    if submission["annotator_id"] == user["id"]: con.close(); raise HTTPException(403, "提交者不得审核自己的标注")
    status = "approved" if decision == "approve" else "rejected"
    con.execute("UPDATE cases SET status=? WHERE id=?", (status, case_id))
    con.execute("UPDATE submissions SET status=? WHERE id=(SELECT id FROM submissions WHERE case_id=? ORDER BY id DESC LIMIT 1)", (status, case_id))
    if decision == "reject":
        # A rejected submission must be reclaimable by its original annotator.
        # Keep submissions and audit history intact, but remove the active
        # lease so it cannot block the next annotation session.
        con.execute("DELETE FROM leases WHERE case_id=?", (case_id,))
    con.execute("INSERT INTO audit_events(user_id,action,case_id,details,created_at) VALUES(?,?,?,?,?)", (user["id"], decision, case_id, note, time.time()))
    con.commit(); con.close(); return {"case_id": case_id, "status": status}


def _zip_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """Validate an archive member name before it is mapped to disk."""
    name = info.filename
    if (not name or "\\" in name or name.startswith(("/", "\\")) or "\x00" in name
            or any(ord(ch) < 32 for ch in name)):
        raise HTTPException(400, "ZIP 包含不安全的文件路径")
    parts = tuple(part for part in name.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(400, "ZIP 包含不安全的文件路径")
    return parts


def _zip_member_is_allowed(parts: tuple[str, ...], is_directory: bool) -> bool:
    """Only permit clinical source media and their small sidecar files."""
    if is_directory:
        return True
    filename = parts[-1].lower()
    suffix = Path(filename).suffix.lower()
    if suffix in VIDEO_SUFFIXES or suffix in DICOM_SUFFIXES:
        return True
    if suffix in FRAME_IMAGE_SUFFIXES:
        return any(part.lower() == "frames" for part in parts[:-1])
    if filename in {"metadata.json", "title.txt"}:
        return True
    # Some CT exports use extension-less, one-slice-per-file DICOM objects.
    return not suffix


def _safe_extract_case_zip(archive: Path, staging_dir: Path) -> Path:
    """Stream a validated clinical ZIP to a staging directory, never extractall."""
    extract_root = staging_dir / "content"
    extract_root.mkdir(parents=True, exist_ok=False)
    total_size = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if not infos:
                raise HTTPException(400, "ZIP 为空")
            if len(infos) > MAX_CASE_ARCHIVE_ENTRIES:
                raise HTTPException(400, "ZIP 条目数量超过安全上限")
            for info in infos:
                parts = _zip_member_parts(info)
                if info.flag_bits & 0x1:
                    raise HTTPException(400, "不支持加密 ZIP，请使用未加密的病例压缩包")
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR)):
                    raise HTTPException(400, "ZIP 不允许符号链接或特殊文件")
                is_directory = info.is_dir()
                if not _zip_member_is_allowed(parts, is_directory):
                    raise HTTPException(400, "ZIP 仅允许病例视频、frames 图像、CT DICOM、metadata.json 和 title.txt")
                if info.file_size < 0 or info.file_size > MAX_CASE_UPLOAD_BYTES:
                    raise HTTPException(400, "ZIP 内单个文件超过病例安全上限")
                total_size += info.file_size
                if total_size > MAX_CASE_UPLOAD_BYTES:
                    raise HTTPException(400, "ZIP 解压总大小超过病例安全上限")

            extracted = 0
            root_resolved = extract_root.resolve()
            for info in infos:
                parts = _zip_member_parts(info)
                destination = (extract_root.joinpath(*parts)).resolve()
                if destination != root_resolved and root_resolved not in destination.parents:
                    raise HTTPException(400, "ZIP 解压路径超出病例目录")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(info, "r") as source, destination.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        extracted += len(chunk)
                        if written > info.file_size or written > MAX_CASE_UPLOAD_BYTES or extracted > MAX_CASE_UPLOAD_BYTES:
                            raise HTTPException(400, "ZIP 解压大小超过安全上限")
                        output.write(chunk)
                if written != info.file_size:
                    raise HTTPException(400, "ZIP 文件大小校验失败")
                if not Path(parts[-1]).suffix:
                    # Extension-less files are reserved for CT exports, not a
                    # loophole for arbitrary payloads.
                    if pydicom is None:
                        raise HTTPException(400, "服务端未安装 DICOM 解码器，不能验证无扩展名 CT 文件")
                    try:
                        dataset = pydicom.dcmread(str(destination), stop_before_pixels=True, force=True, specific_tags=["Modality"])
                    except (InvalidDicomError, OSError, ValueError) as exc:
                        raise HTTPException(400, "ZIP 中无扩展名文件不是有效 CT DICOM") from exc
                    if str(getattr(dataset, "Modality", "")).upper() != "CT":
                        raise HTTPException(400, "ZIP 中无扩展名文件不是 CT DICOM")
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "ZIP 文件无效或已损坏") from exc

    children = [path for path in extract_root.iterdir()]
    # A single top-level folder is the conventional archive form.  Strip only
    # that shell; multiple roots retain their relative layout unchanged.
    return children[0] if len(children) == 1 and children[0].is_dir() else extract_root


def _find_case_media_directory(case_root: Path):
    """Find media under a multi-root ZIP without flattening user folders."""
    scanned = _scan_case_directory(case_root)
    if scanned:
        return case_root, scanned
    try:
        directories = sorted((path for path in case_root.rglob("*") if path.is_dir()), key=lambda path: str(path))
    except OSError:
        return None
    for directory in directories:
        scanned = _scan_case_directory(directory)
        if scanned:
            return directory, scanned
    return None


def _mark_upload_failed(con, session_id: str, upload_root: Path, staging_dir: Path | None = None) -> None:
    """Leave the audit/session row but remove failed raw upload material."""
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)
    shutil.rmtree(upload_root, ignore_errors=True)
    con.execute("UPDATE upload_sessions SET status='failed' WHERE id=?", (session_id,))
    con.commit()


@app.post("/admin/cases/upload/init")
def upload_init(body: UploadInit, user=Depends(require_role("admin"))):
    if not body.files:
        raise HTTPException(400, "请至少选择一个病例文件或一个 ZIP 压缩包")
    zip_specs = [spec for spec in body.files if Path(str(spec.name or "")).suffix.lower() == ".zip"]
    if zip_specs and (len(body.files) != 1 or len(zip_specs) != 1):
        raise HTTPException(400, "ZIP 上传必须单独作为一个病例会话，不能与其他媒体或元数据混合")
    allowed_suffixes = VIDEO_SUFFIXES | FRAME_IMAGE_SUFFIXES | DICOM_SUFFIXES | {".json", ".csv", ".txt", ".zip"}
    for spec in body.files:
        raw_name = str(spec.name or "")
        filename = Path(raw_name)
        suffix = filename.suffix.lower()
        # Clinical DICOM often has no filename extension.  Permit that form,
        # but continue rejecting executable/script uploads and all path input.
        if (not raw_name or raw_name.startswith(("/", "\\")) or "\\" in raw_name or filename.is_absolute()
                or ".." in filename.parts or any(ord(ch) < 32 for ch in raw_name)
                or not filename.name or suffix in DANGEROUS_UPLOAD_SUFFIXES or (suffix and suffix not in allowed_suffixes)):
            raise HTTPException(400, "仅允许病例视频、frames 图像、元数据或 CT DICOM（.dcm/.ima/.dicom/无扩展名）文件，文件名不得包含路径或可执行脚本")
    sid = secrets.token_urlsafe(18); root = DATA_DIR / "uploads" / sid; root.mkdir(parents=True, exist_ok=True)
    con = db()
    if con.execute("SELECT 1 FROM cases WHERE id=?", (body.case_id,)).fetchone():
        con.close(); shutil.rmtree(root, ignore_errors=True); raise HTTPException(409, "案例已存在")
    _validate_assignment(con, body.assigned_annotator_id, body.assigned_reviewer_id)
    con.execute("INSERT INTO upload_sessions(case_id,admin_id,status,created_at,assigned_annotator_id,assigned_reviewer_id,id) VALUES(?,?,?,?,?,?,?)", (body.case_id, user["id"], "uploading", time.time(), body.assigned_annotator_id, body.assigned_reviewer_id, sid))
    for spec in body.files:
        raw_name = str(spec.name or "")
        filename = Path(raw_name)
        relative_path = Path(*filename.parts)
        destination = (root / relative_path).resolve()
        if root.resolve() not in destination.parents:
            con.close(); shutil.rmtree(root, ignore_errors=True); raise HTTPException(400, "上传路径超出病例目录")
        fid = secrets.token_urlsafe(12); con.execute("INSERT INTO upload_files VALUES(?,?,?,?,?,?,?,?)", (fid, sid, raw_name, spec.size, spec.sha256.lower(), spec.chunk_size, 0, str(destination)))
    con.commit(); result = [{"file_id": f["id"], "name": f["name"]} for f in con.execute("SELECT id,name FROM upload_files WHERE session_id=?", (sid,)).fetchall()]; con.close(); return {"session_id": sid, "files": result}

@app.put("/admin/cases/upload/{session_id}/files/{file_id}/chunks")
async def upload_chunk(session_id: str, file_id: str, offset: int, file: UploadFile = File(...), user=Depends(require_role("admin"))):
    con = db(); row = con.execute("SELECT f.*,s.admin_id FROM upload_files f JOIN upload_sessions s ON s.id=f.session_id WHERE f.id=? AND f.session_id=?", (file_id, session_id)).fetchone()
    if not row or row["admin_id"] != user["id"]: con.close(); raise HTTPException(404, "上传文件不存在")
    if offset > row["size"]: con.close(); raise HTTPException(409, "offset 超出文件大小")
    data = await file.read(); end = offset + len(data)
    if end > row["size"]: con.close(); raise HTTPException(409, "分片超出声明大小")
    digest = hashlib.sha256(data).hexdigest()
    old = con.execute("SELECT sha256,size FROM upload_chunks WHERE file_id=? AND \"offset\"=?", (file_id, offset)).fetchone()
    if old:
        con.close(); return {"offset": end, "received": row["received"], "idempotent": True}
    path = Path(row["path"]); path.parent.mkdir(parents=True, exist_ok=True)
    mode = "r+b" if path.exists() else "wb"; handle = path.open(mode); handle.seek(offset); handle.write(data); handle.close()
    con.execute("INSERT INTO upload_chunks VALUES(?,?,?,?)", (file_id, offset, len(data), digest)); con.execute("UPDATE upload_files SET received=received+? WHERE id=?", (len(data), file_id)); con.commit(); received = con.execute("SELECT received FROM upload_files WHERE id=?", (file_id,)).fetchone()[0]; con.close()
    return {"offset": end, "received": received, "sha256": digest}

@app.get("/admin/cases/upload/{session_id}/status")
def upload_status(session_id: str, user=Depends(require_role("admin"))):
    con = db(); s = con.execute("SELECT * FROM upload_sessions WHERE id=? AND admin_id=?", (session_id, user["id"])).fetchone()
    if not s: con.close(); raise HTTPException(404, "上传会话不存在")
    rows = con.execute("SELECT id,name,size,received,sha256,chunk_size FROM upload_files WHERE session_id=?", (session_id,)).fetchall(); con.close(); return {"session_id": session_id, "status": s["status"], "files": [dict(r) for r in rows]}

@app.post("/admin/cases/upload/{session_id}/complete")
def upload_complete(session_id: str, user=Depends(require_role("admin"))):
    con = db(); s = con.execute("SELECT * FROM upload_sessions WHERE id=? AND admin_id=?", (session_id, user["id"])).fetchone(); rows = con.execute("SELECT * FROM upload_files WHERE session_id=?", (session_id,)).fetchall()
    if not s or not rows: con.close(); raise HTTPException(404, "上传会话不存在")
    for row in rows:
        if row["received"] != row["size"]: con.close(); raise HTTPException(409, f"文件未完成: {row['name']}")
        ctx = hashlib.sha256();
        with Path(row["path"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): ctx.update(chunk)
        if ctx.hexdigest() != row["sha256"]: con.close(); raise HTTPException(400, f"文件哈希不匹配: {row['name']}")
    root = DATA_DIR / "uploads" / session_id
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    actual_id = s["case_id"]
    target = CASES_DIR / actual_id
    suffix = 1
    while target.exists():
        actual_id = f"{s['case_id']}_{suffix}"; suffix += 1; target = CASES_DIR / actual_id
    is_zip = len(rows) == 1 and Path(str(rows[0]["name"])).suffix.lower() == ".zip"
    staging_dir = None
    target_created = False
    if is_zip:
        try:
            staging_dir = Path(tempfile.mkdtemp(prefix=f".upload-{session_id}-", dir=CASES_DIR))
            extracted_root = _safe_extract_case_zip(Path(rows[0]["path"]), staging_dir)
            if not _find_case_media_directory(extracted_root):
                raise HTTPException(400, "ZIP 解压后未发现有效病例视频、frames 图像或 CT DICOM")
            # The target was checked above, but re-check immediately before
            # moving so a concurrent upload can never be overwritten.
            if target.exists():
                raise HTTPException(409, "病例目录已存在，请重新发起上传")
            try:
                os.replace(extracted_root, target)
            except OSError:
                shutil.move(str(extracted_root), str(target))
            target_created = True
            scanned_result = _find_case_media_directory(target)
            if not scanned_result:
                shutil.rmtree(target, ignore_errors=True)
                raise HTTPException(400, "ZIP 病例扫描失败")
            _, scanned = scanned_result
            title, meta = scanned
            # The archive is upload transport only. It must not remain in the
            # clinical case directory after extraction succeeds.
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(staging_dir, ignore_errors=True)
        except HTTPException:
            if target_created and target.exists() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            _mark_upload_failed(con, session_id, root, staging_dir)
            con.close()
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            if target_created and target.exists() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            _mark_upload_failed(con, session_id, root, staging_dir)
            con.close()
            raise HTTPException(400, "ZIP 解压或病例入库失败") from exc
    else:
        try:
            os.replace(root, target)
        except OSError:
            shutil.move(str(root), str(target))
        scanned = _scan_case_directory(target)
        if scanned:
            title, meta = scanned
        else:
            title, meta = actual_id, {"source": "server_directory", "source_relative": actual_id}
    # Keep upload-relative technical paths internally for resumability/audit;
    # _public_case_metadata deliberately never returns this field.
    meta["source_files"] = [str(row["name"]) for row in rows]
    con.execute("INSERT INTO cases(id,title,metadata,status,assigned_reviewer_id,created_at,assigned_annotator_id) VALUES(?,?,?,?,?,?,?)", (actual_id, title, json.dumps(meta, ensure_ascii=False), "available", s["assigned_reviewer_id"] if "assigned_reviewer_id" in s.keys() else None, time.time(), s["assigned_annotator_id"] if "assigned_annotator_id" in s.keys() else None))
    con.execute("UPDATE upload_sessions SET case_id=?,status='completed' WHERE id=?", (actual_id, session_id)); audit(con, user["id"], "case_upload_complete", actual_id, "zip" if is_zip else "folder"); con.commit(); con.close(); return {"case_id": actual_id, "status": "ready"}

def _issue_view(con, issue_id, user):
    row = con.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row: raise HTTPException(404, "工单不存在")
    if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: raise HTTPException(403, "无权访问该工单")
    comments = [dict(x) for x in con.execute("SELECT id,author_id,body,created_at FROM issue_comments WHERE issue_id=? ORDER BY id", (issue_id,)).fetchall()]
    status_events = [dict(x) for x in con.execute("SELECT id,from_status,to_status,changed_by,changed_at FROM issue_status_events WHERE issue_id=? ORDER BY changed_at,id", (issue_id,)).fetchall()]
    result = dict(row) | {"labels": json.loads(row["labels"] or "[]"), "comments": comments, "status_events": status_events}
    if "admin" not in roles_of(user) and result.get("case_id"):
        result["case_id"] = anonymized_case(result["case_id"])
    return result

@app.get("/issues")
def list_issues(page: int = 1, page_size: int = 20, search: str = "", status: str = "", user=Depends(current_issue_user)):
    _requirements_guard()
    page, page_size = max(1, page), min(100, max(1, page_size)); con = db(); params=[]; where=[]
    if "admin" not in roles_of(user): where.append("author_id=?"); params.append(user["id"])
    if status == "closed": status = "resolved"
    if status: where.append("status=?"); params.append(status)
    else: where.append("status!='withdrawn'")
    if search: where.append("(title LIKE ? OR description LIKE ?)"); params.extend([f"%{search}%", f"%{search}%"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""; total = con.execute("SELECT COUNT(*) FROM issues"+clause, params).fetchone()[0]
    rows = con.execute("SELECT * FROM issues"+clause+" ORDER BY manual_order ASC NULLS LAST, updated_at DESC LIMIT ? OFFSET ?", params+[page_size,(page-1)*page_size]).fetchall(); con.close()
    items = []
    for row in rows:
        item = dict(row) | {"labels": json.loads(row["labels"] or "[]")}
        if "admin" not in roles_of(user) and item.get("case_id"):
            item["case_id"] = anonymized_case(item["case_id"])
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": (total+page_size-1)//page_size}

@app.get("/issues/{issue_id}")
def get_issue(issue_id: str, user=Depends(current_issue_user)):
    _requirements_guard()
    con=db(); result=_issue_view(con, issue_id, user); con.close(); return result

def _requirements_center_manager(user, connection):
    if "admin" in roles_of(user):
        return True
    owner = connection.execute("SELECT owner_id FROM requirements_driver_center_owner WHERE center_id=?", ("standalone",)).fetchone()
    return bool(owner and str(owner["owner_id"]) == str(user["id"]))

@app.post("/issues")
def create_issue(body: dict, user=Depends(current_issue_user)):
    _requirements_guard()
    title=str(body.get("title") or "").strip()
    if not title: raise HTTPException(400,"工单标题不能为空")
    now=time.time(); ident=secrets.token_hex(12); labels=body.get("labels") if isinstance(body.get("labels"),list) else []
    expected = now + 7 * 86400
    if body.get("expected_resolution_at") not in (None, ""):
        expected = _ticket_timestamp(body.get("expected_resolution_at"))
        if expected is None: raise HTTPException(400, "预计解决时间格式无效")
    con=db(); initial_status = "open" if _requirements_center_manager(user, con) else "pending"; con.execute("INSERT INTO issues(id,title,description,labels,case_id,status,author_id,created_at,updated_at,expected_resolution_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,title,str(body.get("description") or ""),json.dumps([str(x) for x in labels],ensure_ascii=False),body.get("case_id"),initial_status,user["id"],now,now,expected)); con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)",(ident,None,initial_status,user["id"],now)); con.commit(); result=_issue_view(con,ident,user); con.close(); return result

def _validate_requirement_edit(body, user):
    if "parent_ticket_id" in body: raise HTTPException(400, "父工单只能在创建时设置")
    """Apply the same field validation to sessions and user-scoped tokens."""
    body = dict(body)
    if "title" in body:
        if not isinstance(body["title"], str) or not body["title"].strip():
            raise HTTPException(400, "需求标题不能为空")
        body["title"] = body["title"].strip()
        if len(body["title"]) > 200: raise HTTPException(400, "需求标题不能超过200字")
    if "description" in body and not isinstance(body["description"], str):
        raise HTTPException(400, "需求描述必须是文本")
    if "platforms" in body and "labels" not in body: body["labels"] = body.pop("platforms")
    if "labels" in body:
        if not isinstance(body["labels"], list) or not all(isinstance(x, str) for x in body["labels"]):
            raise HTTPException(400, "需求标签必须是文本列表")
    if "case_id" in body and body["case_id"] is not None and not isinstance(body["case_id"], str):
        raise HTTPException(400, "关联病例编号必须是文本或空值")
    if "expected_delivery_date" in body:
        body["expected_resolution_at"] = body.pop("expected_delivery_date")
    if "expected_resolution_at" in body:
        value = body["expected_resolution_at"]
        parsed = _ticket_timestamp(value)
        if value is not None and (parsed is None or parsed <= 0):
            raise HTTPException(400, "预计解决时间格式无效")
        body["expected_resolution_at"] = parsed
    return body

@app.patch("/issues/{issue_id}")
def update_issue(issue_id: str, body: dict, user=Depends(current_issue_user)):
    _requirements_guard()
    user = dict(user)
    con=db(); issue=_issue_view(con,issue_id,user); sets=[]; params=[]
    if not _requirements_center_manager(user, con) and issue["author_id"] != user["id"]: raise HTTPException(403,"无权修改该工单")
    try: body = _validate_requirement_edit(body, user)
    except HTTPException:
        con.close(); raise
    if "expected_resolution_at" in body:
        expected = body["expected_resolution_at"]
        if expected is not None:
            try: expected = float(expected)
            except (TypeError, ValueError):
                con.close(); raise HTTPException(400,"预计解决时间格式无效")
            if not math.isfinite(expected) or expected <= 0:
                con.close(); raise HTTPException(400,"预计解决时间格式无效")
        sets.append("expected_resolution_at=?"); params.append(expected)
    if body.get("status") == "closed": body = dict(body, status="resolved")
    if "status" in body and body["status"] not in {"pending", "open", "in_progress", "resolved", "withdrawn"}:
        con.close(); raise HTTPException(400,"工单状态无效")
    for key in ("title","description","case_id","status"):
        if key in body: sets.append(key+"=?"); params.append(body[key])
    if "labels" in body: sets.append("labels=?"); params.append(json.dumps(body["labels"] if isinstance(body["labels"],list) else [],ensure_ascii=False))
    if "status" in body and body["status"] != issue["status"]:
        previous_status = issue["status"]
        if body["status"] == "resolved":
            sets.append("resolved_at=COALESCE(resolved_at,?)"); params.append(time.time())
        else:
            sets.append("resolved_at=NULL")
    if sets:
        now = time.time()
        sets.append("updated_at=?"); params.extend([now,issue_id]); con.execute("UPDATE issues SET "+",".join(sets)+" WHERE id=?",params)
        if "status" in body and body["status"] != issue["status"]:
            con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)", (issue_id, previous_status, body["status"], user["id"], now))
        if set(body) & {"title", "description", "expected_resolution_at", "labels", "case_id"}:
            audit(con, user["id"], "requirement_edit", issue_id, ",".join(sorted(set(body) & {"title", "description", "expected_resolution_at", "labels", "case_id"})))
        con.commit()
    result=_issue_view(con,issue_id,user); con.close(); return result

@app.post("/issues/{issue_id}/comments")
def comment_issue(issue_id: str, body: dict, user=Depends(current_issue_user)):
    _requirements_guard()
    text=str(body.get("body") or body.get("content") or "").strip();
    if not text: raise HTTPException(400,"评论内容不能为空")
    con=db(); _issue_view(con,issue_id,user); now=time.time(); con.execute("INSERT INTO issue_comments(issue_id,author_id,body,created_at) VALUES(?,?,?,?)",(issue_id,user["id"],text,now)); con.execute("UPDATE issues SET updated_at=? WHERE id=?",(now,issue_id)); con.commit(); result=_issue_view(con,issue_id,user); con.close(); return result

# SWTool 需求中心兼容适配器。数据仍落在 SWTool 的 issues 表中，因此旧数据、
# 运维令牌和现有页面与 /api/tickets 客户端共享同一套记录。
REQUIREMENTS_PLUGIN = "requirements-center"
def _requirements_enabled(con=None):
    runtime = globals().get("plugin_runtime")
    installed = runtime.active(REQUIREMENTS_PLUGIN) if runtime else None
    if installed is not None: return installed
    own = con is None
    con = con or db()
    row = con.execute("SELECT active FROM plugin_settings WHERE name=?", (REQUIREMENTS_PLUGIN,)).fetchone()
    active = True if row is None else bool(row[0])
    if own: con.close()
    return active

def _requirements_guard():
    if not _requirements_enabled():
        raise HTTPException(404, "需求中心插件未激活")

def _ticket_item(con, row, user, include_detail=False):
    item = dict(row)
    author = con.execute("SELECT username FROM users WHERE id=?", (row["author_id"],)).fetchone()
    item["author_username"] = author["username"] if author else "已注销用户"
    item["labels"] = json.loads(row["labels"] or "[]")
    item["platforms"] = item["labels"]
    item["platform"] = item["labels"][0] if item["labels"] else "swtool"
    item["visibility"] = "public"
    item["expected_delivery_date"] = item.get("expected_resolution_at")
    item["ownedByViewer"] = row["author_id"] == user["id"]
    item["ratedByViewer"] = False
    item["attachments"] = [_attachment_item(x) for x in con.execute("SELECT * FROM issue_attachments WHERE issue_id=? AND comment_id IS NULL ORDER BY created_at,id", (row["id"],)).fetchall()]
    if include_detail:
        permission = "" if _requirements_center_manager(user, con) else " AND author_id=?"
        params = () if not permission else (user["id"],)
        parent_id = item.get("parent_ticket_id")
        parent = con.execute("SELECT id,title,status FROM issues WHERE id=?" + permission, (parent_id, *params)).fetchone() if parent_id else None
        item["parent_ticket"] = dict(parent) if parent else None
        children = con.execute("SELECT id,title,status FROM issues WHERE parent_ticket_id=?" + permission + " ORDER BY created_at,id LIMIT 201", (row["id"], *params)).fetchall()
        item["child_tickets"] = [dict(child) for child in children[:200]]
        item["children_truncated"] = len(children) > 200
        item["comments"] = [dict(x) | {"editableByViewer": "admin" in roles_of(user) or str(x["author_id"]) == str(user["id"])} for x in con.execute("SELECT id,author_id,body,created_at,edited_at FROM issue_comments WHERE issue_id=? ORDER BY id", (row["id"],)).fetchall()]
        for comment in item["comments"]:
            comment["attachments"] = [_attachment_item(x) for x in con.execute("SELECT * FROM issue_attachments WHERE issue_id=? AND comment_id=? ORDER BY created_at,id", (row["id"], comment["id"])).fetchall()]
        item["status_events"] = [dict(x) for x in con.execute("SELECT id,from_status,to_status,changed_by,changed_at FROM issue_status_events WHERE issue_id=? ORDER BY changed_at,id", (row["id"],)).fetchall()]
    return item

def _attachment_item(row):
    return {"id": row["id"], "name": row["original_name"], "original_name": row["original_name"], "mime_type": row["mime_type"], "size": row["size"], "created_at": row["created_at"], "url": f"/api/tickets/{row['issue_id']}/attachments/{row['id']}"}

def _attachment_access(con, issue_id, attachment_id, user):
    row = con.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row: raise HTTPException(404, "需求不存在")
    if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: raise HTTPException(403, "无权访问该需求附件")
    attachment = con.execute("SELECT * FROM issue_attachments WHERE id=? AND issue_id=?", (attachment_id, issue_id)).fetchone()
    if not attachment: raise HTTPException(404, "附件不存在")
    return attachment

def _ticket_timestamp(value):
    if value is None or value == "": return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

@app.get("/api/plugins/requirements-center")
def requirements_plugin_info():
    con = db(); active = _requirements_enabled(con); con.close()
    installed = plugin_runtime.state().get(REQUIREMENTS_PLUGIN) if globals().get('plugin_runtime') else None
    version = installed["manifest"]["version"] if installed else "1.3.0"
    return {"name": REQUIREMENTS_PLUGIN, "displayName": "需求中心", "version": version, "installed": True,
            "active": active, "frontendVisible": active, "providerEntry": "server/app.py",
            "interoperability": {"api": "/api/tickets", "legacyApi": "/issues", "sharedStorage": "issues"}}

@app.get("/admin/plugins")
def list_plugins(user=Depends(current_user)):
    if "admin" not in roles_of(user): raise HTTPException(403, "仅管理员可管理插件")
    con = db(); active = _requirements_enabled(con); con.close()
    items = plugin_runtime.items() if globals().get('plugin_runtime') else []
    if not any(item["name"] == REQUIREMENTS_PLUGIN for item in items):
        items.insert(0, {"name": REQUIREMENTS_PLUGIN, "displayName": "需求中心", "version": "1.3.0", "active": active, "installed": True, "hotReload": True})
    return {"items": items}

@app.get("/api/plugins")
def standalone_plugins(user=Depends(current_user)):
    if not os.getenv('SWTOOL_REQUIREMENTS_STANDALONE'):
        raise HTTPException(404, '仅独立发行提供内置需求开关')
    if globals().get('plugin_runtime'):
        items = plugin_runtime.items(active_only=True)
        if items:
            return {'items': items}
    info = requirements_plugin_info()
    return {'items': [info] if info['active'] else []}

@app.patch("/admin/plugins/{plugin_name}")
def set_plugin(plugin_name: str, body: dict, user=Depends(current_user)):
    if "admin" not in roles_of(user): raise HTTPException(403, "仅管理员可管理插件")
    if not isinstance(body.get("active"), bool): raise HTTPException(400, "active 必须为布尔值")
    if os.getenv('SWTOOL_REQUIREMENTS_STANDALONE') and plugin_name != REQUIREMENTS_PLUGIN:
        raise HTTPException(404, '独立发行仅提供需求中心开关')
    if globals().get('plugin_runtime') and (plugin_name != REQUIREMENTS_PLUGIN or plugin_runtime.active(plugin_name) is not None):
        return plugin_runtime.toggle(plugin_name, body["active"])
    con = db(); con.execute("INSERT INTO plugin_settings(name,active,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at,updated_by=excluded.updated_by", (plugin_name, body["active"], time.time(), user["id"])); con.commit(); con.close()
    return {"name": plugin_name, "active": body["active"]}

@app.get("/tickets")
@app.get("/tickets.html")
@app.get("/ticket_submit.html")
def requirements_page():
    return RedirectResponse(url="/#/issues", status_code=307)

@app.get("/api/tickets")
def list_tickets(q: str = "", search: str = "", status: str = "", page: int = 1, page_size: int = 20, scope: str = "", user=Depends(current_issue_user)):
    _requirements_guard(); page, page_size = max(1, page), min(100, max(1, page_size)); con = db(); params=[]; where=[]
    if not _requirements_center_manager(user, con):
        if scope != "public" or dict(user).get("_requirements_key") or dict(user).get("_ops_token"):
            where.append("author_id=?"); params.append(user["id"])
        else:
            where.append("(author_id=? OR status NOT IN ('pending','withdrawn'))"); params.append(user["id"])
    q = q or search
    if q: where.append("(title LIKE ? OR description LIKE ? OR labels LIKE ?)"); params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if status == "closed": status = "resolved"
    if status: where.append("status=?"); params.append(status)
    else: where.append("status!='withdrawn'")
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = con.execute("SELECT COUNT(*) FROM issues" + clause, params).fetchone()[0]
    rows = con.execute("SELECT * FROM issues" + clause + " ORDER BY manual_order ASC NULLS LAST, updated_at DESC LIMIT ? OFFSET ?", params + [page_size, (page-1)*page_size]).fetchall()
    items = [_ticket_item(con, row, user) for row in rows]; con.close()
    return {"tickets": items, "items": items, "total": total, "page": page, "page_size": page_size, "total_pages": (total+page_size-1)//page_size, "pages": (total+page_size-1)//page_size}

@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str, user=Depends(current_issue_user)):
    _requirements_guard(); con = db(); row = con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404, "需求不存在")
    if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: con.close(); raise HTTPException(403, "无权访问该需求")
    item = _ticket_item(con, row, user, True); con.close(); return item

@app.post("/api/tickets/{ticket_id}/attachments")
async def upload_ticket_attachment(ticket_id: str, file: UploadFile = File(...), comment_id: int | None = None, user=Depends(current_issue_user)):
    _requirements_guard(); user = dict(user)
    con = db(); row = con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404, "需求不存在")
    if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: con.close(); raise HTTPException(403, "无权上传该需求附件")
    if comment_id is not None and not con.execute("SELECT 1 FROM issue_comments WHERE id=? AND issue_id=?", (comment_id, ticket_id)).fetchone(): con.close(); raise HTTPException(404, "回复不存在")
    original = Path(file.filename or "附件").name[:180]
    mime = (file.content_type or "application/octet-stream").lower()
    allowed = mime.startswith("image/") or mime in {"application/pdf", "text/plain", "text/csv", "application/zip", "application/json"}
    if not allowed: con.close(); raise HTTPException(400, "仅支持图片、PDF、文本、CSV、JSON 或 ZIP 附件")
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024: con.close(); raise HTTPException(413, "单个附件不能超过 10 MiB")
    attachment_id = secrets.token_hex(16); root = DATA_DIR / "requirements_attachments" / ticket_id; root.mkdir(parents=True, exist_ok=True)
    stored = root / attachment_id; stored.write_bytes(data); now = time.time()
    con.execute("INSERT INTO issue_attachments(id,issue_id,comment_id,uploaded_by,original_name,stored_path,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id,ticket_id,comment_id,user["id"],original,str(stored),mime,len(data),now)); con.execute("UPDATE issues SET updated_at=? WHERE id=?", (now,ticket_id)); con.commit(); result = _attachment_item(con.execute("SELECT * FROM issue_attachments WHERE id=?", (attachment_id,)).fetchone()); con.close(); return result

@app.get("/api/tickets/{ticket_id}/attachments/{attachment_id}")
def download_ticket_attachment(ticket_id: str, attachment_id: str, user=Depends(current_issue_user)):
    _requirements_guard(); con = db(); attachment = _attachment_access(con, ticket_id, attachment_id, user); path = Path(attachment["stored_path"]).resolve(); con.close()
    if not path.is_file() or not path.is_relative_to((DATA_DIR / "requirements_attachments").resolve()): raise HTTPException(404, "附件文件不存在")
    return FileResponse(path, media_type=attachment["mime_type"], filename=attachment["original_name"])

@app.post("/api/tickets", status_code=201)
def create_ticket(body: dict, user=Depends(current_issue_user)):
    _requirements_guard()
    user = dict(user)
    title = str(body.get("title") or "").strip()
    if not title: raise HTTPException(400, "需求标题不能为空")
    labels = body.get("platforms") if isinstance(body.get("platforms"), list) else body.get("labels") if isinstance(body.get("labels"), list) else []
    ident, now = secrets.token_hex(16), time.time(); expected = now + 7 * 86400
    if body.get("expected_delivery_date") not in (None, ""):
        expected = _ticket_timestamp(body.get("expected_delivery_date"))
        if expected is None: raise HTTPException(400, "预计交付日期格式无效")
    parent_id = body.get("parent_ticket_id")
    if parent_id == "": parent_id = None
    if parent_id is not None and (not isinstance(parent_id, str) or len(parent_id) > 128): raise HTTPException(400, "父工单编号无效")
    con = db()
    try:
        manager = _requirements_center_manager(user, con)
        if parent_id:
            permission = "" if manager else " AND author_id=?"
            params = (parent_id,) if manager else (parent_id, user["id"])
            parent = con.execute("SELECT id FROM issues WHERE id=?" + permission + " FOR KEY SHARE", params).fetchone()
            if not parent: raise HTTPException(404, "父工单不存在")
        initial_status = "open" if manager else "pending"
        con.execute("INSERT INTO issues(id,title,description,labels,case_id,status,author_id,created_at,updated_at,expected_resolution_at,parent_ticket_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident,title,str(body.get("description") or ""),json.dumps([str(x) for x in labels], ensure_ascii=False),body.get("case_id"),initial_status,user["id"],now,now,expected,parent_id))
        con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)", (ident,None,initial_status,user["id"],now))
        item = _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ident,)).fetchone(), user, True)
        con.commit()
        return item
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


@app.patch("/api/tickets/{ticket_id}")
def update_ticket(ticket_id: str, body: dict, user=Depends(current_issue_user)):
    if set(body) & {"move_before", "move_after"}:
        _requirements_guard()
        if set(body) not in ({"move_before"}, {"move_after"}):
            raise HTTPException(400, "排序请求只能包含一个移动字段")
        target_id = body[next(iter(body))]
        if not isinstance(target_id, str) or not target_id or target_id == ticket_id:
            raise HTTPException(400, "排序目标无效")
        con = db()
        try:
            if not _requirements_center_manager(user, con):
                raise HTTPException(403, "仅中心管理员可调整需求顺序")
            con.execute("SELECT pg_advisory_xact_lock(734921104)")
            # Each standalone database contains one requirements center.
            moved = con.execute("SELECT id FROM issues WHERE id=? FOR UPDATE", (ticket_id,)).fetchone()
            target = con.execute("SELECT id FROM issues WHERE id=? FOR UPDATE", (target_id,)).fetchone()
            if not moved or not target:
                raise HTTPException(404, "当前中心不存在该排序目标")
            rows = con.execute("SELECT id FROM issues ORDER BY manual_order ASC NULLS LAST, updated_at DESC LIMIT 10001 FOR UPDATE").fetchall()
            if len(rows) > 10000:
                raise HTTPException(400, "工单过多，无法调整顺序")
            ordered = [str(item["id"]) for item in rows]
            ordered.remove(ticket_id)
            index = ordered.index(target_id)
            ordered.insert(index + ("move_after" in body), ticket_id)
            for index, ident in enumerate(ordered):
                con.execute("UPDATE issues SET manual_order=? WHERE id=?", (index, ident))
            audit(con, user["id"], "requirement_reorder", ticket_id, next(iter(body)) + ":" + target_id)
            item = _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone(), user, True)
            con.commit()
            return item
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    _requirements_guard(); user = dict(user); con = db(); row = con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404, "需求不存在")
    if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: con.close(); raise HTTPException(403, "无权修改该需求")
    try: body = _validate_requirement_edit(body, user)
    except HTTPException:
        con.close(); raise
    status = body.get("status")
    if status == "closed": status = "resolved"
    if status is not None and status not in {"pending", "open", "in_progress", "resolved", "withdrawn"}: con.close(); raise HTTPException(400, "需求状态无效")
    now = time.time(); sets=[]; params=[]
    if status is not None and status != row["status"]:
        sets += ["status=?", "resolved_at=?"]
        resolved_at = (row["resolved_at"] or now) if status == "resolved" else None
        params += [status, resolved_at]
    if "title" in body: sets.append("title=?"); params.append(str(body["title"]).strip())
    if "description" in body: sets.append("description=?"); params.append(str(body["description"]))
    if "expected_resolution_at" in body:
        sets.append("expected_resolution_at=?"); params.append(_ticket_timestamp(body["expected_resolution_at"]))
    for field in ("labels", "case_id"):
        if field in body:
            sets.append(field+"=?")
            params.append(json.dumps(body[field], ensure_ascii=False) if field == "labels" else body[field])
    if sets:
        sets.append("updated_at=?"); params += [now, ticket_id]; con.execute("UPDATE issues SET " + ",".join(sets) + " WHERE id=?", params)
        if status is not None and status != row["status"]: con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)", (ticket_id,row["status"],status,user["id"],now))
        if set(body) & {"title", "description", "expected_resolution_at", "labels", "case_id"}:
            audit(con, user["id"], "requirement_edit", ticket_id, ",".join(sorted(set(body) & {"title", "description", "expected_resolution_at", "labels", "case_id"})))
        con.commit()
    item = _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone(), user, True); con.close(); return item

@app.delete("/api/tickets/{ticket_id}")
def delete_ticket(ticket_id: str, user=Depends(current_issue_user)):
    _requirements_guard()
    con = db()
    paths = []
    try:
        row = con.execute("SELECT * FROM issues WHERE id=? FOR UPDATE", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, "需求不存在")
        if not _requirements_center_manager(user, con) and str(row["author_id"]) != str(user["id"]):
            raise HTTPException(403, "无权删除该需求")
        if row["status"] != "withdrawn":
            raise HTTPException(409, "仅可永久删除回收站中的需求")
        paths = [item["stored_path"] for item in con.execute("SELECT stored_path FROM issue_attachments WHERE issue_id=?", (ticket_id,)).fetchall()]
        con.execute("DELETE FROM issue_attachments WHERE issue_id=?", (ticket_id,))
        con.execute("DELETE FROM issue_comments WHERE issue_id=?", (ticket_id,))
        con.execute("DELETE FROM issue_status_events WHERE issue_id=?", (ticket_id,))
        con.execute("DELETE FROM issues WHERE id=?", (ticket_id,))
        audit(con, user["id"], "requirement_delete", ticket_id, "withdrawn")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    attachment_root = (DATA_DIR / "requirements_attachments").resolve()
    for stored_path in paths:
        path = Path(stored_path)
        try:
            if not path.is_symlink() and path.resolve().is_relative_to(attachment_root):
                path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": True, "id": ticket_id}

@app.post("/api/tickets/{ticket_id}/comments")
def add_ticket_comment(ticket_id: str, body: dict, user=Depends(current_issue_user)):
    _requirements_guard(); text = str(body.get("body") or body.get("content") or "").strip()
    if not text: raise HTTPException(400, "评论内容不能为空")
    user = dict(user)
    con = db(); row = con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone()
    if not row: con.close(); raise HTTPException(404, "需求不存在")
    if "admin" not in roles_of(user) and row["author_id"] != user["id"]: con.close(); raise HTTPException(403, "无权回复该需求")
    now=time.time(); con.execute("INSERT INTO issue_comments(issue_id,author_id,body,created_at) VALUES(?,?,?,?)", (ticket_id,user["id"],text,now)); con.execute("UPDATE issues SET updated_at=? WHERE id=?", (now,ticket_id)); con.commit(); item = _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone(), user, True); con.close(); return item

@app.patch("/api/tickets/{ticket_id}/comments/{comment_id}")
def edit_ticket_comment(ticket_id: str, comment_id: int, body: dict, user=Depends(current_issue_user)):
    _requirements_guard(); text = str(body.get("body") or body.get("content") or "").strip()
    if not text or len(text) > 4000: raise HTTPException(400, "回复内容不能为空且不得超过4000字")
    if set(body) != {"body", "if_updated_at"}: raise HTTPException(400, "回复编辑版本无效")
    con = db()
    try:
        con.execute("BEGIN")
        row = con.execute("SELECT * FROM issues WHERE id=? FOR UPDATE", (ticket_id,)).fetchone()
        if not row: raise HTTPException(404, "需求不存在")
        if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: raise HTTPException(403, "无权访问该需求")
        comment = con.execute("SELECT * FROM issue_comments WHERE id=? AND issue_id=? FOR UPDATE", (comment_id, ticket_id)).fetchone()
        if not comment: raise HTTPException(404, "回复不存在")
        if "admin" not in roles_of(user) and comment["author_id"] != user["id"]: raise HTTPException(403, "无权编辑此回复")
        version = comment["edited_at"] if comment["edited_at"] is not None else comment["created_at"]
        if str(body["if_updated_at"]) != str(version): raise HTTPException(409, "回复已被修改，请刷新后重试")
        now = time.time()
        con.execute("UPDATE issue_comments SET body=?,edited_at=? WHERE id=? AND issue_id=?", (text, now, comment_id, ticket_id))
        con.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, ticket_id))
        audit(con, user["id"], "ticket_comment_edited", str(comment_id), ticket_id)
        con.commit()
        return _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone(), user, True)
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

@app.delete("/api/tickets/{ticket_id}/comments/{comment_id}")
def delete_ticket_comment(ticket_id: str, comment_id: int, user=Depends(current_issue_user)):
    _requirements_guard(); con = db(); paths = []
    try:
        con.execute("BEGIN")
        row = con.execute("SELECT * FROM issues WHERE id=? FOR UPDATE", (ticket_id,)).fetchone()
        if not row: raise HTTPException(404, "需求不存在")
        if not _requirements_center_manager(user, con) and row["author_id"] != user["id"]: raise HTTPException(403, "无权访问该需求")
        comment = con.execute("SELECT * FROM issue_comments WHERE id=? AND issue_id=? FOR UPDATE", (comment_id, ticket_id)).fetchone()
        if not comment: raise HTTPException(404, "回复不存在")
        if "admin" not in roles_of(user) and comment["author_id"] != user["id"]: raise HTTPException(403, "无权删除此回复")
        paths = [x["stored_path"] for x in con.execute("SELECT stored_path FROM issue_attachments WHERE issue_id=? AND comment_id=?", (ticket_id, comment_id)).fetchall()]
        con.execute("DELETE FROM issue_attachments WHERE issue_id=? AND comment_id=?", (ticket_id, comment_id))
        con.execute("DELETE FROM issue_comments WHERE id=? AND issue_id=?", (comment_id, ticket_id))
        now = time.time(); con.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, ticket_id))
        audit(con, user["id"], "ticket_comment_deleted", str(comment_id), ticket_id)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    # Keep unreferenced files for the controlled attachment collector: a legacy
    # import may have reused a storage path. Removing the DB rows revokes access.
    with db() as con:
        return _ticket_item(con, con.execute("SELECT * FROM issues WHERE id=?", (ticket_id,)).fetchone(), user, True)

@app.get("/issues/export/json")
def export_issues(user=Depends(current_issue_user)):
    _requirements_guard()
    con=db(); rows=con.execute("SELECT * FROM issues"+("" if "admin" in roles_of(user) else " WHERE author_id=?"),(() if "admin" in roles_of(user) else (user["id"],))).fetchall(); payload={"schema_version":1,"issues":[]}
    for r in rows: payload["issues"].append(_issue_view(con,r["id"],user))
    con.close(); return payload

@app.post("/issues/import/json")
def import_issues(body: dict, user=Depends(current_issue_user)):
    _requirements_guard()
    if "admin" not in roles_of(user): raise HTTPException(403,"仅管理员可导入工单")
    imported=0; con=db()
    for raw in body.get("issues",[]) if isinstance(body.get("issues"),list) else []:
        ident=str(raw.get("id") or secrets.token_hex(12));
        if con.execute("SELECT 1 FROM issues WHERE id=?",(ident,)).fetchone(): ident=secrets.token_hex(12)
        def import_timestamp(value):
            try: value=float(value)
            except (TypeError,ValueError): return None
            return value if math.isfinite(value) and value>0 else None
        now=time.time(); valid_statuses={"pending","open","in_progress","resolved","withdrawn"}; status_value=raw.get("status"); status_value="resolved" if status_value == "closed" else status_value; status=status_value if isinstance(status_value,str) and status_value in valid_statuses else "open"; con.execute("INSERT INTO issues(id,title,description,labels,case_id,status,author_id,created_at,updated_at,expected_resolution_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(ident,str(raw.get("title") or "未命名"),str(raw.get("description") or ""),json.dumps(raw.get("labels") if isinstance(raw.get("labels"),list) else [],ensure_ascii=False),raw.get("case_id"),status,user["id"],now,now,import_timestamp(raw.get("expected_resolution_at")),(import_timestamp(raw.get("resolved_at")) or now) if status == "resolved" else None)); restored_events=0
        raw_events=raw.get("status_events")
        if isinstance(raw_events,list):
            for event in raw_events:
                if not isinstance(event,dict): continue
                to_status=event.get("to_status"); from_status=event.get("from_status")
                to_status="resolved" if to_status == "closed" else to_status
                from_status="resolved" if from_status == "closed" else from_status
                changed_at=import_timestamp(event.get("changed_at"))
                try: changed_by=int(event.get("changed_by"))
                except (TypeError,ValueError): changed_by=None
                if not isinstance(to_status,str) or to_status not in valid_statuses or (from_status is not None and (not isinstance(from_status,str) or from_status not in valid_statuses)) or changed_at is None or changed_by is None or changed_by <= 0: continue
                con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)",(ident,from_status,to_status,changed_by,changed_at)); restored_events+=1
        if restored_events==0: con.execute("INSERT INTO issue_status_events(issue_id,from_status,to_status,changed_by,changed_at) VALUES(?,?,?,?,?)",(ident,None,status,user["id"],now))
        imported+=1
    con.commit(); con.close(); return {"imported":imported}


@app.get("/health")
def health(): return {"status": "ok"}


@app.get("/tools/releases")
def releases():
    package_manifest = Path(__file__).resolve().parents[1] / "release" / "releases.json"
    manifest_path = package_manifest if package_manifest.is_file() else Path(__file__).resolve().parents[2] / "release" / "releases.json"
    if not manifest_path.is_file():
        return {"current_version": "1.0.0", "releases": []}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "版本清单暂不可用") from exc
    items = loaded.get("releases", []) if isinstance(loaded, dict) else loaded
    if not isinstance(items, list):
        raise HTTPException(503, "版本清单格式无效")
    keys = ("platform", "version", "released_at", "features", "notes", "url", "sha256", "size", "signature")
    return {"current_version": loaded.get("current_version", "1.0.0") if isinstance(loaded, dict) else "1.0.0",
            "releases": [{key: item.get(key) for key in keys} for item in items if isinstance(item, dict)]}


@app.get("/guide")
def guide():
    """Return the fixed public operator guide without requiring a session.

    The markdown is served from the checked-in documentation directory so the
    online guide and the offline document cannot drift into two separate copies.
    """
    guide_path = Path(__file__).resolve().parents[1] / "docs" / "用户指南.md"
    if not guide_path.is_file():
        raise HTTPException(404, "用户指南暂不可用")
    try:
        content = guide_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, "用户指南读取失败") from exc
    return {"source": "swtool/docs/用户指南.md", "content": content}


PREDICTOR_JOBS_DIR = DATA_DIR / "predictor_jobs"
_PREDICTOR_GPU_SLOT = threading.Semaphore(1)
PREDICTOR_REMOTE_URL = os.getenv("SWTOOL_PREDICTOR_REMOTE_URL", "").strip().rstrip("/")
PREDICTOR_REMOTE_TOKEN = os.getenv("SWTOOL_PREDICTOR_REMOTE_TOKEN", "").strip()
_PREDICTOR_SERVICE = "swtool-predictor.service"
_PREDICTOR_SERVICE_LOCK = threading.RLock()


def _predictor_systemctl_path() -> str | None:
    return shutil.which("systemctl")


def _predictor_service_status(check_management: bool = True) -> dict:
    result = {"configured": bool(PREDICTOR_REMOTE_URL), "manageable": False,
              "active": False, "status": "unavailable", "reason": ""}
    if not PREDICTOR_REMOTE_URL:
        result["reason"] = "未配置远端 Predictor 服务"
        return result
    systemctl = _predictor_systemctl_path()
    if not systemctl:
        result["reason"] = "服务器未安装 systemctl"
        return result
    try:
        state = subprocess.run([systemctl, "is-active", _PREDICTOR_SERVICE],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        result["reason"] = "无法查询 Predictor 服务状态"
        return result
    status = state.stdout.strip()
    if status not in {"active", "inactive", "activating", "deactivating", "failed"}:
        result["reason"] = "Predictor 服务单元不可用"
        return result
    result["status"] = status
    result["active"] = status == "active"
    if not check_management:
        return result
    sudo = shutil.which("sudo")
    if not sudo:
        result["reason"] = "服务器未安装 sudo"
        return result
    try:
        allowed = all(subprocess.run([sudo, "-n", "-l", systemctl, action, _PREDICTOR_SERVICE],
                                     capture_output=True, text=True, timeout=5).returncode == 0
                      for action in ("start", "stop"))
    except (OSError, subprocess.TimeoutExpired):
        allowed = False
    result["manageable"] = allowed
    if not allowed:
        result["reason"] = "服务账户没有 Predictor 启停权限"
    return result


def _predictor_require_active() -> None:
    if PREDICTOR_REMOTE_URL and not _predictor_service_status(False)["active"]:
        raise HTTPException(503, "Predictor 模型服务未运行，请联系管理员启动")


@app.get("/predictor/service")
def predictor_service():
    service = _predictor_service_status(False)
    if not service["configured"]:
        reason = "模型服务未配置"
    elif service["active"]:
        reason = ""
    elif service["status"] in {"inactive", "failed", "deactivating"}:
        reason = "模型服务未启动"
    else:
        reason = "模型服务暂不可用"
    return {"configured": service["configured"], "active": service["active"],
            "status": service["status"], "reason": reason}


@app.get("/admin/predictor/service")
def admin_predictor_service(admin=Depends(require_role("admin"))):
    return _predictor_service_status()


@app.post("/admin/predictor/service/{action}")
def admin_predictor_service_action(action: str, admin=Depends(require_role("admin"))):
    if action not in {"start", "stop"}:
        raise HTTPException(400, "仅支持启动或停止 Predictor 服务")
    with _PREDICTOR_SERVICE_LOCK:
        status = _predictor_service_status()
        if not status["manageable"]:
            raise HTTPException(503, status["reason"] or "Predictor 服务不可管理")
        if action == "stop":
            con = db()
            try:
                active_jobs = con.execute("SELECT COUNT(*) AS n FROM predictor_jobs WHERE status IN ('queued','running')").fetchone()["n"]
            finally:
                con.close()
            if active_jobs:
                raise HTTPException(409, f"仍有 {active_jobs} 个推理任务未结束，请先等待或取消")
        systemctl = _predictor_systemctl_path()
        sudo = shutil.which("sudo")
        try:
            command = subprocess.run([sudo, "-n", systemctl, action, _PREDICTOR_SERVICE],
                                     capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.warning("Predictor service %s failed: %s", action, type(exc).__name__)
            raise HTTPException(503, "Predictor 服务操作超时或不可用") from exc
        if command.returncode != 0:
            LOGGER.warning("Predictor service %s exited %s", action, command.returncode)
            raise HTTPException(503, "Predictor 服务操作失败，请检查服务器日志")
        audit_con = db()
        try:
            audit(audit_con, admin["id"], f"predictor_service_{action}")
            audit_con.commit()
        finally:
            audit_con.close()
        return _predictor_service_status()


def _predictor_runtime(model_name: str) -> tuple[Path, Path, Path, Path, Path, str]:
    predictor_root = ROOT / "predictor"
    python = Path(os.getenv("SWTOOL_PREDICTOR_PYTHON", sys.executable)).expanduser()
    script = _script_path(Path(os.getenv("SWTOOL_PREDICTOR_SCRIPT", str(predictor_root / "_internal" / "infer_report.py"))).expanduser())
    results_root = Path(os.getenv("SWTOOL_PREDICTOR_RESULTS_ROOT", str(ROOT / "nnUNet_results"))).expanduser()
    preprocessed_root = Path(os.getenv("SWTOOL_PREDICTOR_PREPROCESSED_ROOT", str(ROOT / "nnUNet_preprocessed"))).expanduser()
    license_dir = Path(os.getenv("SWTOOL_PREDICTOR_LICENSE_DIR", str(predictor_root))).expanduser()
    checkpoint = (os.getenv("SWTOOL_PREDICTOR_LOWRES_CHECKPOINT", "checkpoint_best.pth")
                  if model_name == "lowres" else
                  os.getenv("SWTOOL_PREDICTOR_CHECKPOINT", "checkpoint_final.pth"))
    if PREDICTOR_REMOTE_URL:
        return python, script, results_root, preprocessed_root, license_dir, checkpoint
    if checkpoint not in {"checkpoint_final.pth", "checkpoint_best.pth"}:
        raise HTTPException(503, "Predictor checkpoint 配置无效")
    configuration = "3d_fullres" if model_name == "fullres" else "3d_lowres"
    model_dir = results_root / "Dataset501_CTSpine1K" / f"nnUNetTrainer__nnUNetPlans__{configuration}"
    if (not python.is_file() or not script.is_file() or not preprocessed_root.is_dir() or
            not (model_dir / "fold_0" / checkpoint).is_file()):
        raise HTTPException(503, f"{model_name} Predictor 运行时或 nnU-Net checkpoint 未配置，请检查 SWTOOL_PREDICTOR_* 路径")
    return (python.absolute(), script.resolve(), results_root.resolve(),
            preprocessed_root.resolve(), license_dir.resolve(), checkpoint)


def _predictor_local_command(runtime: tuple[Path, Path, Path, Path, Path, str], model_name: str,
                             ct: Path, spacing_zyx: tuple[float, float, float], report: Path,
                             prediction: Path, truth: Path | None) -> list[str]:
    python, script, results_root, preprocessed_root, license_dir, checkpoint = runtime
    command = [str(python), str(script), "--ct", str(ct),
               "--spacing-zyx", *(str(value) for value in spacing_zyx),
               "--results-root", str(results_root), "--preprocessed-root", str(preprocessed_root),
               "--configuration", "3d_fullres" if model_name == "fullres" else "3d_lowres",
               "--checkpoint", checkpoint,
               "--license-dir", str(license_dir), "--output", str(report),
               "--prediction-output", str(prediction)]
    if truth is not None:
        command.extend(["--truth", str(truth)])
    return command


def _predictor_job_view(row) -> dict:
    item = dict(row)
    item.pop("input_path", None)
    item.pop("report_path", None)
    item.pop("cancel_requested", None)
    item.pop("guest_owner", None)
    try:
        item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        item["metrics"] = {}
    item["job_id"] = item["id"]
    item["report_url"] = f"/api/v1/predictor/jobs/{item['id']}/report" if item["status"] == "succeeded" else None
    return item


def _predictor_authorized_job(job_id: str, user):
    con = db()
    row = con.execute("SELECT * FROM predictor_jobs WHERE id=?", (job_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "推理任务不存在")
    guest_owner = user.get("guest_owner") if isinstance(user, dict) else None
    row_owner = row["guest_owner"] if "guest_owner" in row.keys() else None
    if guest_owner:
        allowed = row_owner == guest_owner
    else:
        allowed = "admin" in roles_of(user) or (not row_owner and row["created_by"] == user["id"])
    if not allowed:
        raise HTTPException(404, "推理任务不存在")
    return row


def _predictor_dicom_geometry(root: Path, selected: dict) -> tuple[float, float, float]:
    """Verify that every source slice belongs to one regular Z,Y,X CT grid."""
    files = selected["_files"]
    expected_shape = (int(selected["rows"]), int(selected["columns"]))
    reference_orientation = reference_position = reference_spacing = normal = None
    projected_positions = []
    for entry in files:
        rel = Path(entry["path"])
        path = (root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or root.resolve() not in path.parents or not path.is_file():
            raise ValueError("DICOM 路径无效")
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        try:
            orientation = tuple(float(ds.ImageOrientationPatient[i]) for i in range(6))
            position = tuple(float(ds.ImagePositionPatient[i]) for i in range(3))
            pixel_spacing = tuple(float(ds.PixelSpacing[i]) for i in range(2))
            dimensions = (int(ds.Rows), int(ds.Columns))
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("DICOM 缺少完整的切片几何信息") from exc
        if (str(getattr(ds, "Modality", "")).upper() != "CT" or dimensions != expected_shape or
                not all(math.isfinite(v) for v in (*orientation, *position, *pixel_spacing)) or
                any(v <= 0 for v in pixel_spacing)):
            raise ValueError("DICOM 切片尺寸、模态或体素间距不一致")
        row, column = orientation[:3], orientation[3:]
        row_norm = math.sqrt(sum(v * v for v in row))
        column_norm = math.sqrt(sum(v * v for v in column))
        dot = sum(a * b for a, b in zip(row, column))
        if abs(row_norm - 1) > 1e-3 or abs(column_norm - 1) > 1e-3 or abs(dot) > 1e-3:
            raise ValueError("DICOM 切片方向无效")
        if reference_orientation is None:
            reference_orientation, reference_position, reference_spacing = orientation, position, pixel_spacing
            normal = (
                row[1] * column[2] - row[2] * column[1],
                row[2] * column[0] - row[0] * column[2],
                row[0] * column[1] - row[1] * column[0],
            )
        elif (any(abs(a - b) > 1e-3 for a, b in zip(reference_orientation, orientation)) or
              any(abs(a - b) > max(0.001, a * 0.01) for a, b in zip(reference_spacing, pixel_spacing))):
            raise ValueError("DICOM 切片方向或像素间距不一致")
        delta = tuple(position[i] - reference_position[i] for i in range(3))
        projected = sum(delta[i] * normal[i] for i in range(3))
        offset = math.sqrt(sum((delta[i] - projected * normal[i]) ** 2 for i in range(3)))
        if offset > max(0.05, min(pixel_spacing) * 0.02):
            raise ValueError("DICOM 切片在层内发生位移，无法映射为规则体积")
        projected_positions.append(projected)
    if len(projected_positions) > 1:
        intervals = [b - a for a, b in zip(projected_positions, projected_positions[1:])]
        if any(value <= 1e-4 for value in intervals):
            raise ValueError("DICOM 切片位置重复或顺序错误")
        spacing_z = sorted(intervals)[len(intervals) // 2]
        tolerance = max(0.05, spacing_z * 0.02)
        if any(abs(value - spacing_z) > tolerance for value in intervals):
            raise ValueError("DICOM 层间距不均匀或有缺层")
    else:
        first = pydicom.dcmread(str(root / files[0]["path"]), stop_before_pixels=True, force=True)
        value = getattr(first, "SpacingBetweenSlices", None)
        if value is None:
            value = getattr(first, "SliceThickness", None)
        spacing_z = _predictor_parse_spacing((value, *reference_spacing))[0]
    return _predictor_parse_spacing((spacing_z, *reference_spacing))


def _predictor_dicom_orientation(root: Path, selected: dict) -> tuple[tuple[int, int, int], tuple[bool, bool, bool]]:
    """Map a regular DICOM Z,Y,X grid to the training S,A,R axis order."""
    first = root / selected["_files"][0]["path"]
    ds = pydicom.dcmread(str(first), stop_before_pixels=True, force=True)
    row = tuple(float(value) for value in ds.ImageOrientationPatient[:3])
    column = tuple(float(value) for value in ds.ImageOrientationPatient[3:])
    normal = (row[1] * column[2] - row[2] * column[1],
              row[2] * column[0] - row[0] * column[2],
              row[0] * column[1] - row[1] * column[0])
    # DICOM uses L,P,S; the training NIfTI uses R,A,S. Source array axes
    # advance along slice normal, pixel row, and pixel column respectively.
    ras = tuple((-vector[0], -vector[1], vector[2]) for vector in (normal, column, row))
    target_components = (2, 1, 0)  # canonical array axes Z=S, Y=A, X=R
    axes = tuple(max(range(3), key=lambda index: abs(ras[index][component]))
                 for component in target_components)
    if len(set(axes)) != 3 or any(
        abs(abs(ras[axes[target]][component]) - 1.0) > 1e-3 or
        any(abs(ras[axes[target]][other]) > 1e-3 for other in range(3) if other != component)
        for target, component in enumerate(target_components)
    ):
        raise ValueError("DICOM 为斜位，无法无损统一到训练轴位；请先重采样为正交 CT")
    flips = tuple(ras[axes[target]][component] < 0
                  for target, component in enumerate(target_components))
    return axes, flips


def _predictor_model_grid(ct: Path, workdir: Path, spacing: tuple[float, float, float],
                          orientation: tuple[tuple[int, int, int], tuple[bool, bool, bool]] | None
                          ) -> tuple[Path, tuple[float, float, float]]:
    """Keep the original CT for preview; write only the model input in S,A,R order."""
    if orientation is None:
        return ct, spacing
    import numpy as np
    axes, flips = orientation
    source = np.load(ct, mmap_mode="r", allow_pickle=False)
    view = source.transpose(axes)[tuple(slice(None, None, -1) if flip else slice(None)
                                         for flip in flips)]
    target_path = workdir / "model_ct.npy"
    target = np.lib.format.open_memmap(target_path, mode="w+", dtype=source.dtype, shape=view.shape)
    for index in range(view.shape[0]):
        target[index] = view[index]
    target.flush()
    del target, view, source
    return target_path, tuple(spacing[axis] for axis in axes)


def _predictor_restore_grid(prediction: Path, output: Path,
                            orientation: tuple[tuple[int, int, int], tuple[bool, bool, bool]] | None
                            ) -> None:
    if orientation is None:
        return
    import numpy as np
    axes, flips = orientation
    source = np.load(prediction, mmap_mode="r", allow_pickle=False)
    view = source[tuple(slice(None, None, -1) if flip else slice(None) for flip in flips)]
    view = view.transpose(tuple(int(index) for index in np.argsort(axes)))
    target = np.lib.format.open_memmap(output, mode="w+", dtype=source.dtype, shape=view.shape)
    for index in range(view.shape[0]):
        target[index] = view[index]
    target.flush()
    del target, view, source


def _predictor_ct_source(source: Path, workdir: Path) -> tuple[Path, Path | None, tuple[float, float, float] | None,
                                                             tuple[tuple[int, int, int], tuple[bool, bool, bool]] | None]:
    """Produce a Z,Y,X CT; DICOM also supplies its physical voxel spacing."""
    import numpy as np
    if source.is_file() and source.suffix.lower() == ".npy":
        ct = source
        root = source.parent
    else:
        root = source
        candidates = sorted(root.rglob("*.npy"))
        volumes = [p for p in candidates if "truth" not in p.stem.lower()]
        named = [p for p in volumes if "ct" in p.stem.lower()]
        if len(named) > 1 or (not named and len(volumes) > 1):
            raise ValueError("数据源包含多个 CT 体积，请选择单个病例或单独上传 CT")
        ct = named[0] if named else volumes[0] if volumes else None
    if ct is not None:
        volume = np.load(ct, mmap_mode="r", allow_pickle=False)
        if volume.ndim != 3 or not np.issubdtype(volume.dtype, np.number):
            raise ValueError("CT 必须是三维数值数组")
        truth = next((p for p in sorted(root.rglob("*.npy")) if "truth" in p.stem.lower() and p.parent == ct.parent), None)
        if truth is not None and tuple(np.load(truth, mmap_mode="r", allow_pickle=False).shape) != tuple(volume.shape):
            raise ValueError("CT 与真值尺寸不一致，不能计算 Dice/IoU")
        local_ct = workdir / "ct.npy"
        if ct.resolve() != local_ct.resolve():
            shutil.copyfile(ct, local_ct)
        return local_ct, truth, None, None
    if pydicom is None:
        raise RuntimeError("服务端缺少 pydicom，无法导入 DICOM CT")
    series = _scan_ct_series(root)
    if not series:
        raise ValueError("未发现可推理的单帧 CT DICOM 序列或三维 CT.npy")
    selected = max(series, key=lambda item: item["slice_count"])
    files = selected["_files"]
    spacing = _predictor_dicom_geometry(root, selected)
    orientation = _predictor_dicom_orientation(root, selected)
    volume = np.lib.format.open_memmap(workdir / "ct.npy", mode="w+", dtype=np.float32,
                                       shape=(len(files), int(selected["rows"]), int(selected["columns"])))
    for index, entry in enumerate(files):
        rel = Path(entry["path"])
        path = (root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or root.resolve() not in path.parents:
            raise ValueError("DICOM 路径无效")
        ds = pydicom.dcmread(str(path), force=True)
        pixels = ds.pixel_array
        if pixels.shape != volume.shape[1:]:
            raise ValueError("DICOM 序列切片尺寸不一致")
        volume[index] = pixels.astype(np.float32) * _dicom_number(getattr(ds, "RescaleSlope", None), 1.0) + _dicom_number(getattr(ds, "RescaleIntercept", None), 0.0)
    volume.flush()
    del volume
    return workdir / "ct.npy", None, spacing, orientation


def _predictor_parse_spacing(value) -> tuple[float, float, float]:
    """Validate a Z,Y,X spacing in millimetres without a guessed default."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("spacing_zyx 必须是三个毫米数值") from exc
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("spacing_zyx 必须是三个毫米数值")
    try:
        spacing = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("spacing_zyx 必须是三个毫米数值") from exc
    if not all(math.isfinite(item) and item > 0 for item in spacing):
        raise ValueError("spacing_zyx 必须是三个正的有限毫米数值")
    return spacing


def _predictor_source_spacing(row, workdir: Path, dicom_spacing) -> tuple[float, float, float]:
    if dicom_spacing is not None:
        return dicom_spacing
    if row["dataset_id"]:
        con = db()
        try:
            dataset = con.execute("SELECT export_report FROM dataset_versions WHERE id=?", (row["dataset_id"],)).fetchone()
        finally:
            con.close()
        try:
            report = json.loads(dataset["export_report"] or "{}") if dataset else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            report = {}
        if report.get("axis_order") == "ZYX":
            value = report.get("spacing_zyx", report.get("spacing"))
            if value is not None:
                return _predictor_parse_spacing(value)
    path = workdir / "input_spacing.json"
    if path.is_file():
        return _predictor_parse_spacing(json.loads(path.read_text(encoding="utf-8")))
    raise ValueError("此 CT 缺少真实体素间距；请提供 spacing_zyx=[z,y,x]（毫米）")


def _predictor_store_spacing(workdir: Path, spacing_zyx: tuple[float, float, float]) -> None:
    """Keep the exact spacing used for inference available to every preview path."""
    spacing = _predictor_parse_spacing(spacing_zyx)
    target = workdir / "input_spacing.json"
    staged = workdir / "input_spacing.json.tmp"
    staged.write_text(json.dumps(spacing), encoding="utf-8")
    staged.replace(target)


def _predictor_only_ct_source(root: Path) -> None:
    """Validate a dedicated Predictor dataset before queueing a copy of its CT."""
    import numpy as np

    try:
        entries = list(root.iterdir())
        if not entries or any(p.name not in {"ct.npy", "truth.npy"} or not p.is_file() or p.is_symlink() for p in entries):
            raise ValueError("仅允许 ct.npy 和可选的 truth.npy")
        ct_path = root / "ct.npy"
        if not ct_path.is_file() or ct_path.is_symlink():
            raise ValueError("缺少 ct.npy")
        volumes = []
        for path in (ct_path, root / "truth.npy"):
            if not path.exists():
                continue
            if path.stat().st_size > 1024 * 1024 * 1024:
                raise ValueError("CT 文件超过 1GB 限制")
            volume = np.load(path, mmap_mode="r", allow_pickle=False)
            if not isinstance(volume, np.ndarray) or volume.ndim != 3 or not all(volume.shape) or not np.issubdtype(volume.dtype, np.number) or np.issubdtype(volume.dtype, np.complexfloating):
                raise ValueError("CT 和真值必须是三维实数数组")
            volumes.append(volume)
        if int(np.prod(volumes[0].shape)) > 200_000_000:
            raise ValueError("CT 体积超过预览限制")
        if len(volumes) == 2 and volumes[1].shape != volumes[0].shape:
            raise ValueError("CT 与真值尺寸不一致")
    except (OSError, ValueError, EOFError) as exc:
        raise HTTPException(422, f"Predictor 数据集无效：{exc}") from exc


def _predictor_cancel_requested(job_id: str) -> bool:
    con = db()
    try:
        row = con.execute("SELECT cancel_requested FROM predictor_jobs WHERE id=?", (job_id,)).fetchone()
        return not row or bool(row["cancel_requested"])
    finally:
        con.close()


def _remote_json(method: str, url: str, body: bytes | None = None, content_type: str = "application/json") -> dict:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = content_type
    if PREDICTOR_REMOTE_TOKEN:
        headers["Authorization"] = f"Bearer {PREDICTOR_REMOTE_TOKEN}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(os.getenv("SWTOOL_PREDICTOR_REMOTE_TIMEOUT_SECONDS", "30")))) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"远端 Predictor 连接失败: {exc}") from exc
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("远端 Predictor 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("远端 Predictor 返回格式无效")
    return value


def _remote_upload(job_id: str, model_name: str, ct_path: Path, spacing_zyx: tuple[float, float, float]) -> str:
    boundary = "----swtool-" + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model_name}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"spacing_zyx\"\r\n\r\n{json.dumps(spacing_zyx)}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"ct\"; filename=\"{job_id}.npy\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
        ct_path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    result = _remote_json("POST", f"{PREDICTOR_REMOTE_URL}/v1/jobs", b"".join(parts), f"multipart/form-data; boundary={boundary}")
    remote_id = str(result.get("job_id") or result.get("id") or "")
    if not remote_id:
        raise RuntimeError("远端 Predictor 未返回任务编号")
    return remote_id


def _remote_bytes(method: str, url: str) -> bytes:
    headers = {}
    if PREDICTOR_REMOTE_TOKEN:
        headers["Authorization"] = f"Bearer {PREDICTOR_REMOTE_TOKEN}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=60) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"远端 Predictor 文件读取失败: {exc}") from exc


def _predictor_validate_prediction(ct_path: Path, prediction_path: Path) -> None:
    import numpy as np

    if not prediction_path.is_file() or not prediction_path.stat().st_size:
        raise RuntimeError("Predictor 未生成预测体积")
    ct = np.load(ct_path, mmap_mode="r", allow_pickle=False)
    prediction = np.load(prediction_path, mmap_mode="r", allow_pickle=False)
    if ct.ndim != 3 or prediction.ndim != 3 or not np.issubdtype(prediction.dtype, np.number) or np.issubdtype(prediction.dtype, np.complexfloating):
        raise RuntimeError("Predictor 预测体积尺寸或格式无效")
    if not all(prediction.shape) or int(np.prod(prediction.shape)) > 200_000_000:
        raise RuntimeError("Predictor 预测体积超过预览限制")
    if prediction.shape != ct.shape:
        raise RuntimeError(f"Predictor 预测体积尺寸 {prediction.shape} 与原 CT {ct.shape} 不一致")


def _run_remote_predictor_job(job_id: str, row, workdir: Path, model_name: str) -> None:
    ct, _truth, dicom_spacing, orientation = _predictor_ct_source(Path(row["input_path"]), workdir)
    spacing_zyx = _predictor_source_spacing(row, workdir, dicom_spacing)
    _predictor_store_spacing(workdir, spacing_zyx)
    model_ct, model_spacing = _predictor_model_grid(ct, workdir, spacing_zyx, orientation)
    remote_id = _remote_upload(job_id, model_name, model_ct, model_spacing)
    deadline = time.perf_counter() + max(1, int(os.getenv("SWTOOL_PREDICTOR_TIMEOUT_SECONDS", "3600")))
    while True:
        if _predictor_cancel_requested(job_id):
            try:
                _remote_json("POST", f"{PREDICTOR_REMOTE_URL}/v1/jobs/{remote_id}/cancel", b"{}")
            finally:
                _predictor_mark_cancelled(job_id)
            return
        state = _remote_json("GET", f"{PREDICTOR_REMOTE_URL}/v1/jobs/{remote_id}")
        status = str(state.get("status") or "running")
        progress = float(state.get("progress") or (1 if status == "succeeded" else 0.5))
        stage = str(state.get("stage") or "推理处理中")[:120]
        con = db(); con.execute("UPDATE predictor_jobs SET status='running',progress=?,metrics_json=?,updated_at=? WHERE id=?", (min(progress, 0.99), json.dumps({"stage": stage}, ensure_ascii=False), time.time(), job_id)); con.commit(); con.close()
        if status in {"succeeded", "failed", "cancelled"}:
            if status != "succeeded":
                raise RuntimeError(str(state.get("error") or f"远端 Predictor 任务{status}"))
            report = workdir / "inference_test_report.html"
            report_bytes = _remote_bytes("GET", f"{PREDICTOR_REMOTE_URL}/v1/jobs/{remote_id}/report")
            prediction_bytes = _remote_bytes("GET", f"{PREDICTOR_REMOTE_URL}/v1/jobs/{remote_id}/prediction")
            if not report_bytes or not prediction_bytes:
                raise RuntimeError("远端 Predictor 报告或预测结果为空")
            model_prediction = workdir / ("model_pred.npy" if orientation else "pred.npy")
            model_prediction.write_bytes(prediction_bytes)
            _predictor_restore_grid(model_prediction, workdir / "pred.npy", orientation)
            _predictor_validate_prediction(ct, workdir / "pred.npy")
            report.write_bytes(report_bytes)
            metrics = state.get("result") if isinstance(state.get("result"), dict) else {}
            if orientation is not None:
                import numpy as np
                original_shape = list(np.load(ct, mmap_mode="r", allow_pickle=False).shape)
                metrics["model_input_shape"] = metrics.get("input_shape")
                metrics["model_spacing_zyx"] = metrics.get("spacing_zyx")
                metrics["input_shape"] = original_shape
                metrics["output_shape"] = original_shape
                metrics["spacing_zyx"] = list(spacing_zyx)
            metrics["remote_job_id"] = remote_id
            con = db(); con.execute("UPDATE predictor_jobs SET status='succeeded',progress=1,metrics_json=?,report_path=?,updated_at=? WHERE id=?", (json.dumps(metrics, ensure_ascii=False), str(report), time.time(), job_id)); con.commit(); con.close()
            return
        if time.perf_counter() >= deadline:
            raise TimeoutError("远端 Predictor 推理超时")
        time.sleep(1.0)


def _predictor_mark_cancelled(job_id: str) -> None:
    con = db()
    con.execute("UPDATE predictor_jobs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), job_id))
    con.commit()
    con.close()


def _run_predictor_job(job_id: str, runtime: tuple[Path, Path, Path, Path, Path, str]) -> None:
    workdir = PREDICTOR_JOBS_DIR / job_id
    with _PREDICTOR_GPU_SLOT:
        con = db()
        row = con.execute("SELECT * FROM predictor_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["cancel_requested"]:
            con.close()
            if row:
                _predictor_mark_cancelled(job_id)
            return
        con.execute("UPDATE predictor_jobs SET status='running',progress=0.1,updated_at=? WHERE id=?", (time.time(), job_id))
        con.commit()
        con.close()
        try:
            if PREDICTOR_REMOTE_URL:
                _run_remote_predictor_job(job_id, row, workdir, str(row["model_name"]))
                return
            ct, truth, dicom_spacing, orientation = _predictor_ct_source(Path(row["input_path"]), workdir)
            spacing_zyx = _predictor_source_spacing(row, workdir, dicom_spacing)
            _predictor_store_spacing(workdir, spacing_zyx)
            model_ct, model_spacing = _predictor_model_grid(ct, workdir, spacing_zyx, orientation)
            if _predictor_cancel_requested(job_id):
                _predictor_mark_cancelled(job_id)
                return
            con = db()
            con.execute("UPDATE predictor_jobs SET progress=0.35,updated_at=? WHERE id=?", (time.time(), job_id))
            con.commit(); con.close()
            report = workdir / "inference_test_report.html"
            model_prediction = workdir / ("model_pred.npy" if orientation else "pred.npy")
            command = _predictor_local_command(runtime, str(row["model_name"]), model_ct, model_spacing,
                                               report, model_prediction, truth)
            started = time.perf_counter()
            deadline = started + max(1, int(os.getenv("SWTOOL_PREDICTOR_TIMEOUT_SECONDS", "3600")))
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if _predictor_cancel_requested(job_id):
                            _predictor_mark_cancelled(job_id)
                            return
                        if time.perf_counter() >= deadline:
                            raise TimeoutError("Predictor 推理超时")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            if process.returncode or not report.is_file():
                raise RuntimeError((stderr or stdout or "Predictor 没有生成报告").strip()[-1000:])
            _predictor_restore_grid(model_prediction, workdir / "pred.npy", orientation)
            _predictor_validate_prediction(ct, workdir / "pred.npy")
            try:
                metrics = json.loads(stdout.strip().splitlines()[-1])
            except (ValueError, IndexError, json.JSONDecodeError):
                metrics = {}
            metrics.pop("output", None)
            metrics["wall_elapsed_ms"] = elapsed_ms
            con = db()
            latest = con.execute("SELECT cancel_requested FROM predictor_jobs WHERE id=?", (job_id,)).fetchone()
            status = "cancelled" if latest and latest["cancel_requested"] else "succeeded"
            con.execute("UPDATE predictor_jobs SET status=?,progress=?,metrics_json=?,report_path=?,updated_at=? WHERE id=?",
                        (status, 1, json.dumps(metrics, ensure_ascii=False), str(report) if status == "succeeded" else None, time.time(), job_id))
            con.commit(); con.close()
        except Exception as exc:
            LOGGER.exception("Predictor job failed: %s", job_id)
            con = db()
            con.execute("UPDATE predictor_jobs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:1000], time.time(), job_id))
            con.commit(); con.close()


@app.post("/predictor/jobs")
async def create_predictor_job(request: Request, user=Depends(predictor_user)):
    content_type = request.headers.get("content-type", "")
    uploads = []
    spacing_raw = None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        model_name = str(form.get("model_name") or form.get("model") or "fullres")
        case_id = str(form.get("case_id") or "")
        dataset_id = str(form.get("dataset_id") or "")
        spacing_raw = form.get("spacing_zyx")
        uploads = [item for key, item in form.multi_items() if key in {"files", "file", "files[]"} and isinstance(item, StarletteUploadFile)]
    else:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "请求 JSON 格式无效") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "请求 JSON 必须是对象")
        model_name = str(payload.get("model_name") or payload.get("model") or "fullres")
        case_id = str(payload.get("case_id") or "")
        dataset_id = str(payload.get("dataset_id") or "")
        spacing_raw = payload.get("spacing_zyx")
    guest_owner = user.get("guest_owner") if isinstance(user, dict) else None
    if guest_owner and (case_id or dataset_id):
        raise HTTPException(403, "游客只能使用自己上传的 CT")
    _predictor_require_active()
    if model_name not in {"lowres", "fullres"}:
        raise HTTPException(400, "只支持 lowres 或 fullres 模型")
    if spacing_raw is not None:
        try:
            spacing_zyx = _predictor_parse_spacing(spacing_raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        spacing_zyx = None
    if sum(bool(value) for value in (uploads, case_id, dataset_id)) != 1:
        raise HTTPException(400, "请选择一个 CT 病例、数据集或上传 CT 文件")
    runtime = _predictor_runtime(model_name)
    source = None
    workdir = None
    con = db()
    try:
        if case_id:
            visible = {item["id"] for item in list_cases(user)}
            if case_id not in visible:
                raise HTTPException(404, "CT 病例不存在或无访问权限")
            row = con.execute("SELECT metadata FROM cases WHERE id=?", (case_id,)).fetchone()
            metadata = json.loads(row["metadata"] or "{}") if row else {}
            if metadata.get("imaging_type") != "ct_dicom":
                raise HTTPException(422, "所选病例不是 CT DICOM")
            source = _case_root(case_id, metadata)
        elif dataset_id:
            row = con.execute("SELECT root_path,status,export_report FROM dataset_versions WHERE id=? AND status IN ('frozen','predictor_only')", (dataset_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Predictor 数据集不存在")
            if not _predictor_dataset_visible(row, user)[0]:
                raise HTTPException(403, "此 Predictor 数据集未共享")
            root = Path(row["root_path"]).resolve()
            if not root.is_relative_to(DATA_DIR.resolve()):
                raise HTTPException(422, "数据集路径超出服务端数据目录")
            if row["status"] == "predictor_only" and root.is_dir():
                _predictor_only_ct_source(root)
            source = root
        if (case_id or dataset_id) and (source is None or not source.is_dir()):
            raise HTTPException(404, "CT 数据源不存在")
        job_id = "pred_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4)
        workdir = PREDICTOR_JOBS_DIR / job_id
        workdir.mkdir(parents=True, exist_ok=False)
        if spacing_zyx is not None:
            (workdir / "input_spacing.json").write_text(json.dumps(spacing_zyx), encoding="utf-8")
        if uploads:
            source = workdir / "upload"
            source.mkdir()
            total = 0
            for index, item in enumerate(uploads):
                name = Path(item.filename or "").name
                if not name or Path(name).suffix.lower() not in DICOM_SUFFIXES | {".npy", ".zip"}:
                    raise HTTPException(400, "仅支持 DICOM、三维 NPY 或 CT ZIP")
                target = source / f"{index:05d}_{name}"
                with target.open("xb") as output:
                    while chunk := await item.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_CASE_UPLOAD_BYTES:
                            raise HTTPException(413, "CT 上传超过大小限制")
                        output.write(chunk)
            zips = list(source.glob("*.zip"))
            if zips:
                if len(zips) != 1 or len(uploads) != 1:
                    raise HTTPException(400, "ZIP 应单独上传")
                source = _safe_extract_case_zip(zips[0], workdir)
        with _PREDICTOR_SERVICE_LOCK:
            _predictor_require_active()
            now = time.time()
            con.execute("INSERT INTO predictor_jobs(id,model_name,case_id,dataset_id,input_path,status,created_by,created_at,updated_at,guest_owner) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (job_id, model_name, case_id or None, dataset_id or None, str(source), "queued", user["id"], now, now, guest_owner))
            audit(con, user["id"], "predictor_job_create", case_id or None, json.dumps({"job_id": job_id, "model": model_name, "dataset_id": dataset_id or None}, ensure_ascii=False))
            con.commit()
    except Exception:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        raise
    finally:
        con.close()
    threading.Thread(target=_run_predictor_job, args=(job_id, runtime), name=f"swtool-predictor-{job_id}", daemon=True).start()
    return {"id": job_id, "job_id": job_id, "status": "queued", "progress": 0}


@app.get("/predictor/jobs")
def list_predictor_jobs(user=Depends(predictor_user)):
    con = db()
    guest_owner = user.get("guest_owner") if isinstance(user, dict) else None
    if guest_owner:
        rows = con.execute("SELECT * FROM predictor_jobs WHERE guest_owner=? ORDER BY created_at DESC LIMIT 100", (guest_owner,)).fetchall()
    elif "admin" in roles_of(user):
        rows = con.execute("SELECT * FROM predictor_jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    else:
        rows = con.execute("SELECT * FROM predictor_jobs WHERE created_by=? AND guest_owner IS NULL ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
    con.close()
    return {"items": [_predictor_job_view(row) for row in rows]}


@app.get("/predictor/jobs/{job_id}")
def get_predictor_job(job_id: str, user=Depends(predictor_user)):
    return _predictor_job_view(_predictor_authorized_job(job_id, user))


def _predictor_preview_volumes(job_id: str, user):
    """Open original CT and aligned full-volume labels after checking ownership."""
    import numpy as np

    row = _predictor_authorized_job(job_id, user)
    if row["status"] != "succeeded":
        raise HTTPException(409, "推理结果尚未生成")
    workdir = PREDICTOR_JOBS_DIR / job_id
    arrays = []
    for name in ("ct.npy", "pred.npy"):
        path = workdir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024 * 1024:
            raise HTTPException(404, "推理预览数据不存在或超过大小限制")
        try:
            array = np.load(path, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError, EOFError) as exc:
            raise HTTPException(409, "推理预览数据无效") from exc
        if array.ndim != 3 or not np.issubdtype(array.dtype, np.number) or np.issubdtype(array.dtype, np.complexfloating) or not all(array.shape):
            raise HTTPException(409, "推理预览数据必须是三维数值数组")
        volume_limit = 200_000_000
        if int(np.prod(array.shape)) > volume_limit:
            raise HTTPException(413, "推理体积超过预览限制")
        arrays.append(array)
    if arrays[0].shape != arrays[1].shape:
        raise HTTPException(409, "预测与原 CT 尺寸不一致；请重新运行完整体积推理")
    return arrays[0], arrays[1]


def _predictor_case_series(job_id: str, user, original_shape):
    """Resolve the case sequence that supplied the predictor's source CT."""
    row = _predictor_authorized_job(job_id, user)
    if not row["case_id"]:
        return None
    con = db()
    try:
        case = con.execute("SELECT metadata FROM cases WHERE id=?", (row["case_id"],)).fetchone()
    finally:
        con.close()
    try:
        metadata = json.loads(case["metadata"] or "{}") if case else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if metadata.get("imaging_type") != "ct_dicom":
        return None
    sequences = metadata.get("dicom_series")
    if not isinstance(sequences, list):
        return None
    matches = [series for series in sequences if isinstance(series, dict) and
               (int(series.get("slice_count") or 0), int(series.get("rows") or 0), int(series.get("columns") or 0)) == tuple(original_shape)]
    if not matches:
        return None
    # _predictor_ct_source chooses the largest series; _scan_ct_series sorts
    # equal-size candidates by id before returning them.
    series = sorted(matches, key=lambda item: str(item.get("id") or ""))[0]
    return row["case_id"], metadata, series


def _predictor_preview_spacing(job_id: str, user, original_shape):
    """Use the same physical Z,Y,X spacing submitted to Predictor."""
    row = _predictor_authorized_job(job_id, user)
    path = PREDICTOR_JOBS_DIR / job_id / "input_spacing.json"
    if path.is_file():
        try:
            return _predictor_parse_spacing(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "推理任务的体素间距记录无效") from exc
    selected = _predictor_case_series(job_id, user, original_shape)
    if selected is not None:
        series = selected[2]
        return _predictor_parse_spacing([series.get(key) for key in ("spacing_z", "spacing_y", "spacing_x")])
    try:
        return _predictor_source_spacing(row, PREDICTOR_JOBS_DIR / job_id, None)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _predictor_annotation_preview(job_id: str, user, original_shape, pred):
    """Share annotation's CT reconstruction grid and sample labels onto it."""
    import numpy as np

    selected = _predictor_case_series(job_id, user, original_shape)
    if selected is None:
        return None
    case_id, metadata, series = selected
    try:
        volume, info = _ct_mpr_volume(case_id, metadata, str(series.get("id") or ""))
    except HTTPException:
        return None
    if info.get("patient_resampled") or tuple(info["original_shape"]) != tuple(original_shape):
        # Patient-axis resampling needs a full physical affine for labels.
        return None
    sz, sy, sx = (int(value) for value in info["strides"])
    z_indices = list(range(0, original_shape[0], sz))
    if z_indices[-1] != original_shape[0] - 1:
        z_indices.append(original_shape[0] - 1)
    if (len(z_indices), math.ceil(original_shape[1] / sy), math.ceil(original_shape[2] / sx)) != tuple(volume.shape):
        return None
    labels = np.empty(volume.shape, dtype=pred.dtype)
    for target_z, source_z in enumerate(z_indices):
        labels[target_z] = pred[source_z, ::sy, ::sx]
    return volume, labels, info


@app.get("/predictor/jobs/{job_id}/preview")
def predictor_job_preview(job_id: str, user=Depends(predictor_user)):
    import numpy as np

    ct, pred = _predictor_preview_volumes(job_id, user)
    # Scan one axial slice at a time so the metadata request does not create
    # another full-volume boolean mask for large prediction arrays.
    foreground_voxels = 0
    suggested_slice = 0
    suggested_count = 0
    for axial_index in range(int(pred.shape[0])):
        count = int(np.count_nonzero(np.asarray(pred[axial_index]) > 0))
        foreground_voxels += count
        if count > suggested_count:
            suggested_count = count
            suggested_slice = axial_index
    return {
        "shape": [int(size) for size in ct.shape],
        "suggested_slice": suggested_slice,
        "foreground_voxels": foreground_voxels,
    }


@app.get("/predictor/jobs/{job_id}/preview/volume")
def predictor_job_preview_volume(job_id: str, window_center: float | None = None, window_width: float | None = None, user=Depends(predictor_user)):
    """Return a bounded point cloud for interactive CT volume preview."""
    import numpy as np

    ct, pred = _predictor_preview_volumes(job_id, user)
    max_points = 26000
    steps = tuple(max(1, int(math.ceil(size / 96))) for size in ct.shape)
    sampled = np.asarray(ct[::steps[0], ::steps[1], ::steps[2]], dtype=np.float32)
    labels = np.asarray(pred[::steps[0], ::steps[1], ::steps[2]]) > 0
    finite = sampled[np.isfinite(sampled)]
    if not finite.size:
        return {"shape": [int(v) for v in ct.shape], "points": []}
    center, width = _ct_window(window_center, window_width, 40.0, 400.0)
    lo, hi = center - width / 2.0, center + width / 2.0
    # Normalized predictor uploads do not use HU. Use their robust range only
    # when the requested CT window cannot intersect the source values.
    if window_center is None and window_width is None and (float(finite.max()) <= lo or float(finite.min()) >= hi):
        lo, hi = np.percentile(finite, [2, 98])
    if hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
    visible = np.isfinite(sampled) & ((sampled >= lo) | labels)
    coords = np.argwhere(visible)
    if coords.shape[0] > max_points:
        pick = np.linspace(0, coords.shape[0] - 1, max_points, dtype=np.int64)
        coords = coords[pick]
    shape = np.maximum(np.asarray(sampled.shape, dtype=np.float32) - 1, 1)
    points = []
    for z, y, x in coords:
        value = float(np.clip((sampled[z, y, x] - lo) / max(hi - lo, 1e-6), 0, 1))
        points.append([float(x / shape[2] * 2 - 1), float(1 - y / shape[1] * 2), float(1 - z / shape[0] * 2), value, 1 if labels[z, y, x] else 0])
    return {"shape": [int(v) for v in ct.shape], "points": points}


def _predictor_oblique_mip(ct, pred, spacing_z: float = 1.0, spacing_y: float = 1.0, spacing_x: float = 1.0, window_center: float = 40.0, window_width: float = 400.0):
    """Build predictor MIP with the same oblique reconstruction as the CT MPR view.

    Sampling adjusts source spacing just like ``_ct_mpr_volume``.
    """
    import numpy as np

    steps = tuple(max(1, math.ceil(size / 160)) for size in ct.shape)
    sampling = tuple(slice(None, None, step) for step in steps)
    preview_ct, preview_pred = ct[sampling], pred[sampling]
    effective_spacing = (
        max(float(spacing_z), 0.01) * steps[0],
        max(float(spacing_y), 0.01) * steps[1],
        max(float(spacing_x), 0.01) * steps[2],
    )
    hu = _ct_oblique_mip_reconstruction(preview_ct, *effective_spacing, window_center, window_width)
    # The label projection shares geometry with the CT volume rendering.
    mask = _ct_oblique_label_projection(preview_pred, *effective_spacing)
    mask = np.nan_to_num(mask, nan=0.0, posinf=0.0, neginf=0.0)
    return hu, mask


def _predictor_mask_outline(mask):
    import numpy as np

    padded = np.pad(mask, 1, constant_values=0)
    return (mask > 0) & (
        (mask != padded[:-2, 1:-1])
        | (mask != padded[2:, 1:-1])
        | (mask != padded[1:-1, :-2])
        | (mask != padded[1:-1, 2:])
    )


@app.get("/predictor/jobs/{job_id}/preview/{plane}/{index}")
def predictor_job_preview_image(job_id: str, plane: str, index: int, window_center: float | None = None, window_width: float | None = None, user=Depends(predictor_user)):
    if plane not in {"axial", "coronal", "sagittal", "mip"}:
        raise HTTPException(404, "不支持的推理预览视图")
    ct, pred = _predictor_preview_volumes(job_id, user)
    z, y, x = ct.shape
    limit = {"axial": z, "coronal": y, "sagittal": x, "mip": 1}[plane]
    if index < 0 or index >= limit:
        raise HTTPException(404, "推理预览位置超出范围")
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(503, "推理预览图像组件未安装") from exc
    annotation = None if plane == "axial" else _predictor_annotation_preview(job_id, user, ct.shape, pred)
    recon_info = annotation[2] if annotation is not None else None
    if plane == "axial":
        hu, mask = ct[index, :, :], pred[index, :, :]
    elif plane == "coronal":
        if annotation is not None:
            preview_ct, preview_mask, info = annotation
            mapped = min(preview_ct.shape[1] - 1, index // int(info["strides"][1]))
            hu, mask = preview_ct[:, mapped, :], preview_mask[:, mapped, :]
        else:
            hu, mask = ct[:, index, :], pred[:, index, :]
    elif plane == "sagittal":
        if annotation is not None:
            preview_ct, preview_mask, info = annotation
            mapped = min(preview_ct.shape[2] - 1, index // int(info["strides"][2]))
            hu, mask = preview_ct[:, :, mapped], preview_mask[:, :, mapped]
        else:
            hu, mask = ct[:, :, index], pred[:, :, index]
    else:
        if annotation is not None:
            preview_ct, preview_mask, info = annotation
            spacing = (info["spacing_z"], info["spacing_y"], info["spacing_x"])
            center, width = _ct_window(window_center, window_width, info["window_center"], info["window_width"])
            hu = _ct_oblique_mip_reconstruction(preview_ct, *spacing, center, width)
            mask = _ct_oblique_label_projection(preview_mask, *spacing)
        else:
            center, width = _ct_window(window_center, window_width, 40.0, 400.0)
            spacing = _predictor_preview_spacing(job_id, user, ct.shape)
            hu, mask = _predictor_oblique_mip(ct, pred, *spacing, window_center=center, window_width=width)
    if hu.size > 16_000_000:
        raise HTTPException(413, "推理切片超过预览限制")
    center, width = _ct_window(window_center, window_width,
                               recon_info["window_center"] if recon_info else 40.0,
                               recon_info["window_width"] if recon_info else 400.0)
    low, high = center - width / 2.0, center + width / 2.0
    finite = np.asarray(hu)[np.isfinite(hu)]
    # Predictor uploads may be normalized or use a non-HU scale.  Keep the
    # normal CT 40/400 window, but fall back to the volume's robust range when
    # it does not intersect the requested window; this prevents an all-black
    # reconstruction while preserving explicit CT window behavior.
    auto_window = window_center is None and window_width is None
    if recon_info is None and plane != "mip" and finite.size and (float(finite.max()) <= low or float(finite.min()) >= high or (auto_window and float(finite.min()) >= -20 and float(finite.max()) <= 20)):
        robust_low, robust_high = np.percentile(finite, [2, 98])
        if robust_high > robust_low:
            low, high = float(robust_low), float(robust_high)
    gray = (np.clip(hu, 0, 255) if plane == "mip" else np.clip((np.nan_to_num(hu, nan=low, posinf=high, neginf=low) - low) * (255.0 / max(high - low, 1e-6)), 0, 255)).astype(np.uint8)
    if recon_info is not None and plane in {"coronal", "sagittal"} and recon_info["invert"]:
        gray = 255 - gray
    # Match annotation's physical aspect ratio for reformatted planes.  CT
    # intensity uses bilinear sampling; labels use nearest-neighbour sampling.
    row_spacing, column_spacing = (1.0, 1.0)
    if plane in {"coronal", "sagittal", "mip"}:
        spacing_z, spacing_y, spacing_x = (
            (recon_info["spacing_z"], recon_info["spacing_y"], recon_info["spacing_x"])
            if recon_info is not None else _predictor_preview_spacing(job_id, user, ct.shape)
        )
        row_spacing, column_spacing = (
            (spacing_y, spacing_x) if plane == "mip" else
            (spacing_z, spacing_x if plane == "coronal" else spacing_y)
        )
    unit = min(row_spacing, column_spacing)
    target_height = max(1, round(gray.shape[0] * row_spacing / unit))
    target_width = max(1, round(gray.shape[1] * column_spacing / unit))
    scale = min(1.0, 1600 / max(target_height, target_width))
    target_height = max(1, round(target_height * scale))
    target_width = max(1, round(target_width * scale))
    target_size = (target_width, target_height)
    foreground = np.asarray(mask) > 0
    if target_size != (gray.shape[1], gray.shape[0]):
        gray = np.asarray(Image.fromarray(gray).resize(target_size, Image.Resampling.BILINEAR))
        if plane == "mip":
            mask = np.asarray(Image.fromarray(np.asarray(mask).astype(np.uint8)).resize(target_size, Image.Resampling.NEAREST))
            foreground = mask > 0
        else:
            foreground = np.asarray(Image.fromarray(foreground.astype(np.uint8)).resize(target_size, Image.Resampling.NEAREST)) > 0
    rgb = np.repeat(gray[:, :, None], 3, axis=2)
    if plane == "mip":
        rgb[_predictor_mask_outline(mask)] = (255, 80, 48)
    else:
        rgb[foreground, 0] = (rgb[foreground, 0].astype(np.uint16) + 255) // 2
        rgb[foreground, 1] = rgb[foreground, 1] // 2
        rgb[foreground, 2] = rgb[foreground, 2] // 2
    image = Image.fromarray(rgb, mode="RGB")
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    return Response(content=encoded.getvalue(), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@app.post("/predictor/jobs/{job_id}/cancel")
def cancel_predictor_job(job_id: str, user=Depends(predictor_user)):
    row = _predictor_authorized_job(job_id, user)
    if row["status"] in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(409, "推理任务已经结束")
    con = db()
    con.execute("UPDATE predictor_jobs SET cancel_requested=TRUE,updated_at=? WHERE id=?", (time.time(), job_id))
    audit(con, user["id"], "predictor_job_cancel", row["case_id"], job_id)
    con.commit(); con.close()
    return {"job_id": job_id, "status": "cancelling"}


@app.post("/predictor/jobs/{job_id}/retry")
def retry_predictor_job(job_id: str, user=Depends(predictor_user)):
    with _PREDICTOR_SERVICE_LOCK:
        row = _predictor_authorized_job(job_id, user)
        _predictor_require_active()
        if row["status"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "只有失败或取消的推理任务可以重试")
        runtime = _predictor_runtime(row["model_name"])
        if not Path(row["input_path"]).exists():
            raise HTTPException(404, "原始 CT 数据已不存在，无法重试")
        con = db()
        cursor = con.execute(
            "UPDATE predictor_jobs SET status='queued',progress=0,error=NULL,metrics_json=NULL,report_path=NULL,cancel_requested=FALSE,updated_at=? WHERE id=? AND status IN ('failed','cancelled')",
            (time.time(), job_id),
        )
        if not cursor.rowcount:
            con.close()
            raise HTTPException(409, "推理任务状态已改变")
        audit(con, user["id"], "predictor_job_retry", row["case_id"], job_id)
        con.commit()
        con.close()
    threading.Thread(target=_run_predictor_job, args=(job_id, runtime), name=f"swtool-predictor-{job_id}", daemon=True).start()
    return {"id": job_id, "job_id": job_id, "status": "queued", "progress": 0}


@app.get("/predictor/jobs/{job_id}/report")
def predictor_job_report(job_id: str, user=Depends(predictor_user)):
    row = _predictor_authorized_job(job_id, user)
    if row["status"] != "succeeded":
        raise HTTPException(409, "推理报告尚未生成")
    report = Path(row["report_path"] or "").resolve()
    expected = (PREDICTOR_JOBS_DIR / job_id / "inference_test_report.html").resolve()
    if report != expected or not report.is_file():
        raise HTTPException(404, "推理报告不存在")
    return FileResponse(report, media_type="text/html; charset=utf-8", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


PREDICTOR_RELEASE_DIR = Path(os.getenv("SWTOOL_PREDICTOR_RELEASE_DIR", str(DATA_DIR / "predictor_releases")))
PREDICTOR_SIGNING_KEY = Path(os.getenv("SWTOOL_PREDICTOR_SIGNING_KEY", str(Path.home() / ".config/swtool-predictor/signing-key.pem")))
_PREDICTOR_RELEASE_FILES = {
    "windows-installer": ("swtool-predictor-setup.exe", "程序包", "application/octet-stream"),
    "windows-gpu-environment": ("swtool-predictor-gpu-environment.7z", "环境包", "application/x-7z-compressed"),
}
_PREDICTOR_PUBLIC_RELEASE_IDS = ("windows-installer", "windows-gpu-environment")
_PREDICTOR_RELEASE_LOCK = threading.RLock()
_PREDICTOR_BOOTSTRAP_FILES = {
    "0.1.0-20261009": "swtool-predictor-runtime-0.1.0-20261009.7z",
}


@app.get("/predictor/bootstrap/{version}/win-x64.7z")
def predictor_bootstrap_payload(version: str, request: Request):
    """Public, immutable installer payload; models still require device authorization."""
    if not _predictor_release_enabled():
        raise HTTPException(404, "安装运行时下载未开放")
    filename = _PREDICTOR_BOOTSTRAP_FILES.get(version)
    if filename is None:
        raise HTTPException(404, "安装运行时版本不存在")
    root = PREDICTOR_RELEASE_DIR.resolve()
    raw = PREDICTOR_RELEASE_DIR / filename
    path = raw.resolve()
    if any(p.is_symlink() for p in (raw, *raw.parents)) or path.parent != root or not path.is_file():
        raise HTTPException(404, "安装运行时尚未发布")
    file_stat = path.stat()
    etag = '"' + hashlib.sha256(f"{file_stat.st_mtime_ns}:{file_stat.st_size}".encode()).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache",
               "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, filename="win-x64.7z", media_type="application/octet-stream",
                        stat_result=file_stat, headers=headers)


@contextmanager
def _predictor_distribution_db():
    con = db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _predictor_distribution_user(user):
    if user["status"] != "active":
        raise HTTPException(403, "账号审核通过后才能下载发行包")
    return user


def _predictor_release_enabled() -> bool:
    path = DATA_DIR / "predictor_release_settings.json"
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def _predictor_release_file(release_id: str) -> Path:
    if release_id not in _PREDICTOR_RELEASE_FILES:
        raise HTTPException(404, "发行包不存在")
    root = PREDICTOR_RELEASE_DIR.resolve()
    raw = root / _PREDICTOR_RELEASE_FILES[release_id][0]
    path = raw.resolve()
    if raw.is_symlink() or path.parent != root or not path.is_file():
        raise HTTPException(404, "发行包尚未发布")
    return path


def _predictor_signing_ready() -> bool:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        path = PREDICTOR_SIGNING_KEY
        if path.is_symlink() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return False
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        return isinstance(key, Ed25519PrivateKey) and key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex() == "3860fbf6f5a69816438ec97bd5ff98138f2856267ee8aba1e9c4d1e4cf4ac1c9"
    except (OSError, ValueError, TypeError):
        return False


@app.get("/predictor/releases")
def predictor_releases(user=Depends(current_user_optional)):
    if user is not None:
        _predictor_distribution_user(user)
    enabled = _predictor_release_enabled()
    items = []
    for release_id in _PREDICTOR_PUBLIC_RELEASE_IDS:
        filename, label, _ = _PREDICTOR_RELEASE_FILES[release_id]
        try:
            path = _predictor_release_file(release_id)
            size = path.stat().st_size
        except HTTPException:
            size = None
        items.append({"id": release_id, "filename": filename, "label": label,
                      "size": size, "available": size is not None})
    result = {"enabled": enabled, "files": items,
              "notice": "仅支持 Windows NVIDIA GPU。程序包按安装向导安装；环境包解压到独立可写目录，保留原目录结构，不要混入程序安装目录。按下方步骤选择 python.exe、完成设备授权和 GPU 诊断后启动服务。"}
    if user is not None and "admin" in roles_of(user):
        result["signing_ready"] = _predictor_signing_ready()
    return result


class PredictorReleaseSettingsIn(BaseModel):
    enabled: bool


@app.put("/admin/predictor/releases")
def predictor_release_settings(body: PredictorReleaseSettingsIn, admin=Depends(require_role("admin"))):
    _predictor_distribution_user(admin)
    with _PREDICTOR_RELEASE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".predictor-release-", dir=DATA_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"enabled": body.enabled}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, DATA_DIR / "predictor_release_settings.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        with _predictor_distribution_db() as con:
            audit(con, admin["id"], "predictor_release_settings", details=json.dumps({"enabled": body.enabled}))
    return {"enabled": body.enabled}


@app.post("/predictor/releases/{release_id}/download-link")
def predictor_release_download_link(release_id: str, user=Depends(current_user_optional)):
    if user is not None:
        _predictor_distribution_user(user)
    _predictor_release_file(release_id)
    expires = int(time.time()) + 300
    payload = base64.urlsafe_b64encode(json.dumps({"uid": user["id"] if user is not None else None, "exp": expires, "release": release_id}, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET, b"predictor-release-v1\0" + payload.encode(), hashlib.sha256).hexdigest()
    with _predictor_distribution_db() as con:
        audit(con, user["id"] if user is not None else None, "predictor_download_link", details=release_id)
    return {"url": f"/api/v1/predictor/releases/{release_id}/download?ticket={payload}.{signature}", "expires_at": expires}


@app.get("/predictor/releases/{release_id}/download")
def predictor_release_download(release_id: str, ticket: str):
    if release_id not in _PREDICTOR_PUBLIC_RELEASE_IDS:
        raise HTTPException(404, "发行包不存在")
    try:
        if len(ticket) > 1024:
            raise ValueError
        payload, signature = ticket.split(".")
        expected = hmac.new(SECRET, b"predictor-release-v1\0" + payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        if data["release"] != release_id or data["exp"] < time.time():
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "下载链接无效或已过期，请重新点击下载")
    with _predictor_distribution_db() as con:
        user_id = data.get("uid")
        if user_id is not None:
            user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise HTTPException(401, "用户不存在")
            _predictor_distribution_user(user)
        path = _predictor_release_file(release_id)
        audit(con, user_id, "predictor_release_download", details=release_id)
    filename, _, media_type = _PREDICTOR_RELEASE_FILES[release_id]
    return FileResponse(path, filename=filename, media_type=media_type,
                        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


@app.post("/admin/predictor/licenses")
async def predictor_issue_license(request: Request, admin=Depends(require_role("admin"))):
    _predictor_distribution_user(admin)
    # Bound the entire multipart body before parsing, including oversized non-file fields.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, "授权请求过大；仅上传 predictor.request 密锁文件")
    request._body = bytes(raw)
    form = await request.form()
    try:
        upload = form.get("file")
        license_type = form.get("license_type")
        if not isinstance(upload, StarletteUploadFile) or license_type not in {"L", "F", "LF"}:
            raise HTTPException(422, "请上传 predictor.request 并选择 L、F 或 LF 授权")
        content = await upload.read(16385)
        from predictor._internal.license_protocol import parse_request
        try:
            device = parse_request(content)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        fingerprint = device["fingerprint"]
        if not _predictor_signing_ready():
            raise HTTPException(503, "服务端授权签发密钥未配置或与客户端不匹配")
        script = _script_path(ROOT / "predictor/tools/license_issue.py")
        if not script.is_file():
            raise HTTPException(503, "授权签发工具未安装")
        with tempfile.TemporaryDirectory(prefix="swtool-license-") as directory:
            source, output = Path(directory) / "predictor.request", Path(directory) / "predictor.license"
            source.write_bytes(content)
            try:
                result = subprocess.run([sys.executable, str(script), "--request", str(source), "--output", str(output),
                                         "--type", license_type, "--private-key", str(PREDICTOR_SIGNING_KEY)],
                                        capture_output=True, timeout=15, check=False)
                if result.returncode or not output.is_file():
                    raise HTTPException(503, "授权签发失败，请检查服务端签发配置")
            except (OSError, subprocess.TimeoutExpired):
                raise HTTPException(503, "授权签发工具不可用，请检查服务端配置")
            license_bytes = output.read_bytes()
        with _predictor_distribution_db() as con:
            audit(con, admin["id"], "predictor_license_issue", details=json.dumps({"fingerprint": fingerprint, "type": license_type, "validity": "permanent"}))
        return Response(license_bytes, media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="predictor.license"', "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    finally:
        await form.close()


from server.requirements_archive import install_routes as _install_requirements_archive_routes
_install_requirements_archive_routes(sys.modules[__name__])

from server.requirements_federation import install_routes as _install_requirements_federation_routes
_install_requirements_federation_routes(sys.modules[__name__])
from server.requirements_source import install_routes as _install_requirements_source_routes
_install_requirements_source_routes(sys.modules[__name__])
from server.requirements_releases import install_routes as _install_requirements_release_routes
_install_requirements_release_routes(sys.modules[__name__])

from server.requirements_driver import install_routes as _install_requirements_driver_routes
_install_requirements_driver_routes(sys.modules[__name__])

if not os.getenv("SWTOOL_REQUIREMENTS_STANDALONE"):
    from server.ctspine_cbct import install_routes as _install_ctspine_cbct_routes
    _install_ctspine_cbct_routes(sys.modules[__name__])

from server.plugin_runtime import install_routes as _install_plugin_routes
_install_plugin_routes(sys.modules[__name__])

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
