"""Build and verify chapter artifacts only from validated staged pages."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from webtoon_downloader.core.webtoon.downloaders.state import (
    IntegrityError,
    checked_path,
    image_evidence,
    sync_directory,
    verify_image,
)


def _pixel_hash(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def build_artifact(
    work: Path,
    target: Path,
    kind: str,
    pages: list[dict[str, Any]],
    metadata: dict[str, bytes],
) -> None:
    if kind == "images":
        _build_folder(work, target, pages, metadata)
    elif kind in {"zip", "cbz"}:
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for page in pages:
                archive.write(checked_path(work, page["staged"]), arcname=page["name"])
            for name, data in metadata.items():
                archive.writestr(name, data)
    elif kind == "pdf":
        with pymupdf.open() as document:
            for record in pages:
                width, height = record["dimensions"]
                page = document.new_page(width=width, height=height)
                source = checked_path(work, record["staged"]).read_bytes()
                if Path(record["name"]).suffix.lower() == ".webp":
                    with Image.open(io.BytesIO(source)) as image, io.BytesIO() as output:
                        image.save(output, format="PNG")
                        source = output.getvalue()
                page.insert_image(pymupdf.Rect(0, 0, width, height), stream=source)
            document.save(target)
    else:
        raise IntegrityError("unknown_storage_type")
    if target.is_file():
        with target.open("r+b") as handle:
            os.fsync(handle.fileno())


def _build_folder(work: Path, target: Path, pages: list[dict[str, Any]], metadata: dict[str, bytes]) -> None:
    target.mkdir()
    for page in pages:
        destination = checked_path(target, page["name"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked_path(work, page["staged"]), destination)
        with destination.open("r+b") as handle:
            os.fsync(handle.fileno())
    for name, data in metadata.items():
        destination = checked_path(target, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    for directory in reversed([target, *[path for path in target.rglob("*") if path.is_dir()]]):
        sync_directory(directory)


def verify_artifact(
    work: Path,
    target: Path,
    kind: str,
    pages: list[dict[str, Any]],
    metadata: dict[str, bytes],
    full: bool,
    max_pixels: int,
) -> bool:
    try:
        if kind == "images":
            return _verify_folder(target, pages, metadata, full, max_pixels)
        if not target.is_file():
            return False
        if kind in {"zip", "cbz"}:
            return _verify_zip(target, pages, metadata, full, max_pixels)
        return _verify_pdf(work, target, pages, full, max_pixels)
    except Exception:
        return False


def _verify_folder(
    target: Path,
    pages: list[dict[str, Any]],
    metadata: dict[str, bytes],
    full: bool,
    max_pixels: int,
) -> bool:
    if not target.is_dir():
        return False
    expected = {page["name"] for page in pages} | set(metadata)
    actual = {str(path.relative_to(target)).replace("\\", "/") for path in target.rglob("*") if path.is_file()}
    if actual != expected:
        return False
    for page in pages:
        if not verify_image(checked_path(target, page["name"]), page, full, max_pixels):
            return False
    return all(checked_path(target, name).read_bytes() == data for name, data in metadata.items())


def _verify_zip(
    target: Path,
    pages: list[dict[str, Any]],
    metadata: dict[str, bytes],
    full: bool,
    max_pixels: int,
) -> bool:
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != {page["name"] for page in pages} | set(metadata):
            return False
        for page in pages:
            if archive.getinfo(page["name"]).file_size != page["size"]:
                return False
            if full:
                evidence = image_evidence(archive.read(page["name"]), max_pixels)
                if evidence["sha256"] != page["sha256"]:
                    return False
        return all(archive.read(name) == data for name, data in metadata.items())


def _verify_pdf(work: Path, target: Path, pages: list[dict[str, Any]], full: bool, max_pixels: int) -> bool:
    with pymupdf.open(target) as document:
        if document.page_count != len(pages):
            return False
        for number, record in enumerate(pages):
            page = document.load_page(number)
            if list(map(round, (page.rect.width, page.rect.height))) != record["dimensions"]:
                return False
            if full:
                images = page.get_images()
                if len(images) != 1:
                    return False
                data = document.extract_image(images[0][0])["image"]
                image_evidence(data, max_pixels)
                expected = checked_path(work, record["staged"]).read_bytes()
                if _pixel_hash(data) != _pixel_hash(expected):
                    return False
    return True
