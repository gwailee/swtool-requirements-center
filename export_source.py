# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Reproducible source ZIP from a fixed manifest; never collect runtime data."""
import base64
import hashlib
import json
import os
from io import BytesIO
from pathlib import Path, PurePosixPath
import stat
import zipfile

SOURCE_ROOT = Path(__file__).absolute().parent
if (SOURCE_ROOT / "standalone" / "SOURCE_MANIFEST.json").is_file():
    SOURCE_ROOT = SOURCE_ROOT / "standalone"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ENTRIES = 512
BLOCKED_PARTS = {".git", ".venv", "__pycache__", "cache", "data", "server_data", "uploads", "productiondata", "credentials", "secrets", ".installed"}
BLOCKED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".zip", ".key", ".pem", ".jks", ".log"}


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or str(path) != name:
        raise ValueError("Unsafe source manifest path")
    parts = path.parts
    if any(part in {".", ".."} or part.lower() in BLOCKED_PARTS for part in parts):
        raise ValueError("Runtime data is not source")
    if any(part.startswith(".") for part in parts) or path.suffix.lower() in BLOCKED_SUFFIXES:
        raise ValueError("Secret or generated source path")
    if path.name != "service.env.example" and (".env" in path.name or "secret" in path.name.lower() or "credential" in path.name.lower()):
        raise ValueError("Environment or secret source path")
    return path


def _read_source(name, limit=MAX_FILE_BYTES):
    _safe_name(name)
    root = SOURCE_ROOT.absolute()
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        components = (*root.parts[1:], *PurePosixPath(name).parts)
        for component in components[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Missing or oversized source file")
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("Oversized source file")
        return payload
    except OSError as exc:
        raise ValueError("Missing or Symlink source is forbidden") from exc
    finally:
        os.close(directory)


def build_source_archive() -> bytes:
    manifest_bytes = _read_source("SOURCE_MANIFEST.json", MAX_MANIFEST_BYTES)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported source manifest")
    if len(manifest['files']) > MAX_ENTRIES:
        raise ValueError("Too many source entries")
    entries = {"SOURCE_MANIFEST.json": manifest_bytes}
    total_bytes = len(manifest_bytes)
    for entry in manifest["files"]:
        name = entry["path"]
        _safe_name(name)
        if name in entries:
            raise ValueError("Duplicate archive member")
        source_name = entry.get("resource", name)
        if "resource" in entry:
            if not name.startswith("web/vendor/katex/fonts/") or PurePosixPath(name).suffix not in {".ttf", ".woff", ".woff2"}:
                raise ValueError("Unapproved binary resource")
            try:
                payload = _read_source(name)
            except ValueError:
                if (SOURCE_ROOT / name).exists():
                    raise
                payload = base64.b64decode(_read_source(source_name).replace(b"\n", b""), validate=True)
            if len(payload) != entry["size"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                raise ValueError("Binary resource integrity failure")
            entries[source_name] = _read_source(source_name)
            total_bytes += len(entries[source_name])
        else:
            payload = _read_source(name)
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("Oversized archive member")
        total_bytes += len(payload)
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("Oversized source archive")
        entries[name] = payload
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in sorted(entries.items()):
            member = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            archive.writestr(member, payload)
    return stream.getvalue()
