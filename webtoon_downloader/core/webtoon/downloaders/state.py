"""Persistent, owned workspaces. No automatic deletion of user or staged data."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import os
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image

from webtoon_downloader.core.retry import ImageContentError, ResourceLimitError

SCHEMA_VERSION = 1
_OUTPUTS: set[str] = set()
_OUTPUT_GUARD = threading.Lock()


class IntegrityError(ValueError):
    """An artifact or state file cannot establish complete delivery."""


class WorkspaceBusyError(BlockingIOError):
    """A cooperating process currently owns an advisory lock."""


def checked_path(root: Path, relative: str | Path) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or (isinstance(relative, str) and "\\" in relative):
        raise IntegrityError("unsafe_relative_path")
    target = root / path
    for component in (root, *root.parents, target, *target.parents):
        if component.is_symlink():
            raise IntegrityError("symlink_path_rejected")
    if not target.resolve().is_relative_to(root.resolve()):
        raise IntegrityError("path_outside_workspace")
    return target


def atomic_json(target: Path, payload: dict[str, Any]) -> None:
    checked_path(target.parent, target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.new")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    sync_directory(target.parent)


def sync_directory(directory: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read_json(path: Path) -> dict[str, Any]:
    checked_path(path.parent, path.name)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise IntegrityError("state_file_too_large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SCHEMA_VERSION
    ):
        raise IntegrityError("unsupported_state_schema")
    return payload


@contextmanager
def output_lock(root: Path) -> Iterator[None]:
    """Serialize output publication/cleanup without leaving a lock file outside the workspace."""
    key = str(root.resolve()).casefold() if sys.platform == "win32" else str(root.resolve())
    with _OUTPUT_GUARD:
        if key in _OUTPUTS:
            raise WorkspaceBusyError("output_busy")
        _OUTPUTS.add(key)
    try:
        with _platform_output_lock(root):
            yield
    finally:
        with _OUTPUT_GUARD:
            _OUTPUTS.remove(key)


@contextmanager
def _platform_output_lock(root: Path) -> Iterator[None]:
    if sys.platform != "win32":
        import fcntl

        descriptor = os.open(root, os.O_RDONLY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkspaceBusyError("output_busy") from error
            yield
        finally:
            os.close(descriptor)
    else:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, f"Local\\WebtoonDownloader-{name}")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 0) in {0, 0x80}
            if not acquired:
                raise WorkspaceBusyError("output_busy")
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Use OS advisory locks; the lock file persists, so stale files are harmless."""
    checked_path(path.parent, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
        try:
            _set_lock(handle, True)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise WorkspaceBusyError("workspace_busy") from error
            raise
        try:
            yield
        finally:
            _set_lock(handle, False)


def _set_lock(handle: BinaryIO, acquire: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


@asynccontextmanager
async def publication_lock(path: Path) -> AsyncIterator[None]:
    """Queue independent publications; cancellation is bounded by the chapter budget."""
    while True:
        manager = exclusive_lock(path)
        try:
            manager.__enter__()
        except WorkspaceBusyError:
            await asyncio.sleep(0.05)
        else:
            break
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def image_evidence(data: bytes, max_pixels: int) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > max_pixels:
                raise ResourceLimitError("image_pixel_limit")
            image.load()
            dimensions = [image.width, image.height]
    except (OSError, ValueError) as error:
        if isinstance(error, ResourceLimitError):
            raise
        raise ImageContentError(str(error)) from error
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "dimensions": dimensions}


def verify_image(path: Path, record: dict[str, Any], full: bool, max_pixels: int) -> bool:
    checked_path(path.parent, path.name)
    if not path.is_file() or path.stat().st_size != record.get("size") or path.stat().st_size == 0:
        return False
    if not full:
        return path.stat().st_mtime_ns == record.get("mtime_ns")
    try:
        evidence = image_evidence(path.read_bytes(), max_pixels)
        return all(evidence[key] == record.get(key) for key in ("size", "sha256", "dimensions"))
    except (OSError, ImageContentError):
        return False


def publish_file(staged: Path, destination: Path) -> None:
    """Hard link is atomic and refuses to replace an existing destination."""
    checked_path(destination.parent, destination.name)
    os.link(staged, destination)
    sync_directory(destination.parent)
