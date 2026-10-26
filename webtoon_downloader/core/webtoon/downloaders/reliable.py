"""The public download pipeline: inventory, retry, stage, verify, publish, report."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import uuid
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from webtoon_downloader.core.file import slugify_name
from webtoon_downloader.core.retry import ImageContentError, PageOperationError, ResourceLimitError, blocking, retryable
from webtoon_downloader.core.webtoon.client import WebtoonHttpClient
from webtoon_downloader.core.webtoon.comicinfo import ComicInfoMetadata, build_comicinfo_xml
from webtoon_downloader.core.webtoon.downloaders.artifacts import build_artifact, verify_artifact
from webtoon_downloader.core.webtoon.downloaders.options import WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult, ChapterResult, PageExpectation, PageResult
from webtoon_downloader.core.webtoon.downloaders.state import (
    SCHEMA_VERSION,
    IntegrityError,
    atomic_json,
    checked_path,
    exclusive_lock,
    image_evidence,
    output_lock,
    publication_lock,
    publish_file,
    read_json,
    sync_directory,
    verify_image,
)
from webtoon_downloader.core.webtoon.extractor import (
    ElementNotFoundError,
    WebtoonMainPageExtractor,
    WebtoonViewerPageExtractor,
)
from webtoon_downloader.core.webtoon.fetchers import WebtoonFetcher
from webtoon_downloader.core.webtoon.inventory import ChapterScope, EpisodeInventory, InventoryContractError
from webtoon_downloader.core.webtoon.models import ChapterInfo, PageInfo
from webtoon_downloader.core.webtoon.namer import NonSeparateFileNameGenerator, SeparateFileNameGenerator
from webtoon_downloader.storage import AioFolderWriter
from webtoon_downloader.transformers.image import AioImageFormatTransformer

log = logging.getLogger(__name__)


def public_url(value: str) -> str:
    parts = urlsplit(value)
    query = urlencode([(key, item) for key, item in parse_qsl(parts.query) if key in {"title_no", "episode_no"}])
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, query, ""))


def report_value(value: Any, key: str = "") -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {name: report_value(item, name) for name, item in value.items()}
    if isinstance(value, tuple | list):
        return [report_value(item, key) for item in value]
    if isinstance(value, str) and key in {"source_url", "viewer_url"}:
        return public_url(value)
    return value


def error_name(error: BaseException) -> str:
    # Stable exception categories avoid leaking credentials embedded in request URLs.
    chain: list[str] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 8:
        suffix = f"[{current.response.status_code}]" if isinstance(current, httpx.HTTPStatusError) else ""
        detail = (
            f": {current}" if isinstance(current, IntegrityError | ResourceLimitError | InventoryContractError) else ""
        )
        chain.append(type(current).__name__ + suffix + detail)
        current = current.__cause__
    reason = str(error) if isinstance(error, PageOperationError) else ""
    return " -> ".join(chain) + (f": {reason}" if reason else "")


class ReliableDownloader:
    def __init__(self, options: WebtoonDownloadOptions, client: WebtoonHttpClient) -> None:
        self.options = options
        self.url = re.sub(r"\\(?=[?=&])", "", options.url)
        if not urlsplit(self.url).scheme:
            self.url = f"https://{self.url}"
        self.client = client
        self.root = Path(options.destination) if options.destination else Path.cwd()
        self.scope: ChapterScope | None = None
        self.results: dict[int, ChapterResult] = {}
        self.notes: dict[int, str] = {}
        self.semaphore = asyncio.Semaphore(options.concurrent_chapters)
        self.page_semaphore = asyncio.Semaphore(options.concurrent_pages)
        self.cpu_semaphore = asyncio.Semaphore(min(2, options.concurrent_pages))
        self.interrupted = False
        self.stop = False
        self.run_id = uuid.uuid4().hex
        self.series_metadata: WebtoonMainPageExtractor | None = None
        self.report: Path | None = None
        self.failure_retryable = False
        self.chapter_failures: dict[int, bool] = {}
        self.output_guard = ExitStack()
        self.owns_output = False

    async def run(self) -> BatchResult:
        failure = None
        try:
            await self._run()
        except asyncio.CancelledError:
            self.interrupted = True
        except Exception as error:
            failure = error_name(error)
            self.failure_retryable = retryable(error)
        try:
            if not self.owns_output:
                try:
                    checked_path(self.root, ".webtoon-downloader")
                    self.root.mkdir(parents=True, exist_ok=True)
                    self.output_guard.enter_context(output_lock(self.root))
                    self.owns_output = True
                except Exception as error:
                    failure = f"output_unavailable: {error_name(error)}"
                    self.failure_retryable = False
            return self._finish(failure)
        finally:
            self.output_guard.close()

    async def _run(self) -> None:
        inventory = await WebtoonFetcher(self.client, self.url).get_inventory(self.url)
        self.scope = self._scope(inventory)
        if not self.options.destination:
            self.root = Path(slugify_name(inventory.chapters[0].series_title))
        checked_path(self.root, ".webtoon-downloader")
        self.root.mkdir(parents=True, exist_ok=True)
        self.output_guard.enter_context(output_lock(self.root))
        self.owns_output = True
        if (
            self.options.result_file
            and self.options.result_file != "-"
            and not self.options.keep_workspace
            and Path(self.options.result_file).resolve().is_relative_to((self.root / ".webtoon-downloader").resolve())
        ):
            raise IntegrityError("explicit_report_inside_cleaned_workspace_use_keep_workspace")
        self.series_metadata = WebtoonMainPageExtractor((await self.client.get(self.url)).text)
        chapters = [chapter for chapter in inventory.chapters if chapter.data_episode_no in self.scope.episode_ids]
        if self.options.on_webtoon_fetched:
            await self.options.on_webtoon_fetched(chapters)
        await self._run_tasks(chapters)
        if not self.interrupted and not self.stop:
            refreshed = await WebtoonFetcher(self.client, self.url).get_inventory(self.url)
            known = {chapter.data_episode_no: chapter.viewer_url for chapter in refreshed.chapters}
            changed = [
                chapter.data_episode_no
                for chapter in chapters
                if known.get(chapter.data_episode_no) != chapter.viewer_url
            ]
            for episode in changed:
                self.results[episode] = replace(
                    self.results[episode], published=False, error="source_changed_during_batch"
                )
            if changed:
                raise IntegrityError("source_changed_during_batch")
        await self._export_series(chapters)

    async def _run_tasks(self, chapters: list[ChapterInfo]) -> None:
        tasks = [asyncio.create_task(self._worker(chapter)) for chapter in chapters]
        try:
            for task in asyncio.as_completed(tasks):
                result = await task
                if not result.is_complete and self.options.fail_fast:
                    self.stop = True
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    break
        except asyncio.CancelledError:
            self.interrupted = True
            for task in tasks:
                task.cancel()
            raise
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _scope(self, inventory: EpisodeInventory) -> ChapterScope:
        if not self.options.retry_failed:
            return inventory.select(self.options.start, "latest" if self.options.latest else self.options.end)
        previous = read_json(Path(self.options.retry_failed))
        if previous.get("series") != asdict(inventory.series):
            raise IntegrityError("retry_report_series_mismatch")
        expected = previous.get("expected_episode_ids")
        incomplete = previous.get("incomplete_episode_ids")
        if (
            not isinstance(expected, list)
            or not expected
            or not isinstance(incomplete, list)
            or (not incomplete and previous.get("exit_code") == 0)
        ):
            raise IntegrityError("retry_report_has_no_recoverable_scope")
        if any(type(item) is not int for item in (*expected, *incomplete)) or not set(incomplete).issubset(expected):
            raise IntegrityError("invalid_retry_report_scope")
        # Revalidate the original scope too: previously complete neighbors may have changed.
        return ChapterScope(inventory, tuple(expected))

    def _base(self) -> Path:
        if self.scope is None:
            return checked_path(self.root, ".webtoon-downloader/unresolved")
        series = self.scope.inventory.series
        return checked_path(self.root, f".webtoon-downloader/{series.language}-{series.series_type}-{series.title_no}")

    async def _worker(self, chapter: ChapterInfo) -> ChapterResult:
        if self.scope is None:
            raise IntegrityError("missing_download_scope")
        result = ChapterResult(chapter.data_episode_no, (), self.scope.inventory.series)
        work = self._base() / "chapters" / str(chapter.data_episode_no)
        try:
            async with self.semaphore:
                if self.stop:
                    return replace(result, error="not_started_fail_fast")
                checked_path(self.root, work.relative_to(self.root))
                work.mkdir(parents=True, exist_ok=True)
                with exclusive_lock(work / "chapter.lock"):
                    await self._progress(chapter, "Start")
                    result = await asyncio.wait_for(self._chapter(chapter, work), self.options.chapter_time_budget)
        except asyncio.CancelledError:
            result = self._recover_result(
                chapter, work, self.interrupted, "interrupted" if self.interrupted else "fail_fast"
            )
        except Exception as error:
            result = self._recover_result(chapter, work, False, error_name(error))
            self.chapter_failures.setdefault(chapter.data_episode_no, retryable(error))
        if result.is_complete:
            self.chapter_failures.pop(chapter.data_episode_no, None)
        self.results[chapter.data_episode_no] = result
        return result

    def _recover_result(self, chapter: ChapterInfo, work: Path, interrupted: bool, error: str) -> ChapterResult:
        if self.scope is None:
            raise IntegrityError("missing_download_scope")
        try:
            manifest = read_json(work / "manifest.json")
            self._validate_manifest(manifest, chapter)
            result = self._result(manifest)
        except (OSError, ValueError, KeyError, TypeError):
            result = ChapterResult(chapter.data_episode_no, (), self.scope.inventory.series)
        return replace(result, published=False, interrupted=interrupted, error=error)

    async def _chapter(self, chapter: ChapterInfo, work: Path) -> ChapterResult:
        attempts: dict[str, int] = {}
        rounds = self.options.chapter_retries if self.options.retry_strategy else 0
        for attempt in range(rounds + 1):
            try:
                return await self._attempt(
                    chapter,
                    work,
                    attempts,
                    reuse=self.options.resume or bool(self.options.retry_failed) or attempt > 0,
                    round_number=attempt + 1,
                )
            except Exception as error:
                if attempt == rounds or not retryable(error):
                    raise
                await asyncio.sleep(self.client.retries.policy.wait(attempt + 1))
        raise IntegrityError("unreachable_chapter_state")

    async def _attempt(
        self, chapter: ChapterInfo, work: Path, attempts: dict[str, int], reuse: bool, round_number: int
    ) -> ChapterResult:
        response = await self.client.get(chapter.viewer_url)
        extractor = WebtoonViewerPageExtractor(response.text)
        self.notes[chapter.data_episode_no] = extractor.chapter_notes
        urls = extractor.img_urls
        if not urls:
            raise IntegrityError("empty_viewer_image_list")
        await self._progress(chapter, "ChapterInfoFetched", extractor)
        metadata = self._metadata(chapter, extractor)
        signature = hashlib.sha256(
            json.dumps(
                [
                    urls,
                    self.options.image_format.lower(),
                    self.options.quality,
                    self.options.save_as,
                    self.options.separate,
                    {key: value.decode("utf-8") for key, value in metadata.items()},
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        manifest = self._manifest(chapter, work, urls, signature)
        if manifest.get("completed") and await self._verify_existing(work, manifest, metadata):
            for _ in urls:
                await self._progress(chapter, "PageCompleted")
            await self._progress(chapter, "Completed")
            cached = self._result(manifest)
            return replace(cached, attempts=0, pages=tuple(replace(page, attempts=0) for page in cached.pages))
        manifest["completed"] = False
        manifest["chapter_attempt"] = round_number
        atomic_json(work / "manifest.json", manifest)
        await self._pages(chapter, work, manifest, attempts, reuse)
        latest = WebtoonViewerPageExtractor((await self.client.get(chapter.viewer_url)).text)
        if latest.img_urls != urls:
            raise ImageContentError("viewer_image_list_changed")
        await self._publish(work, manifest, metadata)
        await self._progress(chapter, "Completed")
        return self._result(manifest)

    def _manifest(self, chapter: ChapterInfo, work: Path, urls: list[str], signature: str) -> dict[str, Any]:
        if self.scope is None:
            raise IntegrityError("missing_download_scope")
        path = work / "manifest.json"
        previous = read_json(path) if path.exists() else {}
        if previous:
            self._validate_manifest(previous, chapter)
            if previous.get("signature") == signature:
                self._validate_page_list(previous, urls, work)
                return previous
        generator = (
            SeparateFileNameGenerator(chapter_mode=self.options.chapter_mode)
            if self.options.separate
            else NonSeparateFileNameGenerator()
        )
        directory = generator.get_chapter_directory(chapter)
        expected = []
        for position, url in enumerate(urls, 1):
            if urlsplit(url).scheme != "https":
                raise IntegrityError("non_https_image_url")
            name = Path(generator.get_page_filename(PageInfo(position, url, len(urls), chapter))).with_suffix(
                f".{self.options.image_format.lower()}"
            )
            if self.options.save_as != "images":
                name = directory / name
            expected.append({"position": position, "source_url": url, "name": name.as_posix()})
        target = (
            directory.as_posix()
            if self.options.save_as == "images"
            else f"{chapter.number:0{len(str(chapter.total_chapters))}d}.{self.options.save_as}"
        )
        if previous.get("kind") == self.options.save_as and previous.get("separate") == self.options.separate:
            target = previous["target"]
        elif self.options.save_as == "images" and self.options.separate and self.options.chapter_mode == "number-title":
            target = self._existing_target(chapter, target)
        checked_path(self.root, target)
        settings = [self.options.image_format.lower(), self.options.quality]
        reusable = previous.get("pages", []) if previous.get("content_settings") == settings else []
        reusable = [
            record
            for record in reusable
            if any(all(record.get(key) == item[key] for key in ("position", "source_url", "name")) for item in expected)
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "series": asdict(self.scope.inventory.series),
            "episode_no": chapter.data_episode_no,
            "signature": signature,
            "kind": self.options.save_as,
            "separate": self.options.separate,
            "content_settings": settings,
            "target": target,
            "expected": expected,
            "pages": reusable,
            "completed": False,
            "owned_target": previous.get("owned_target", False),
            "previous_names": [item["name"] for item in previous.get("expected", [])],
            "failures": [],
        }

    def _reject_legacy_target(self, chapter: ChapterInfo, target: str) -> None:
        prefix = chapter.title.split("/", 1)[0]
        if "/" in chapter.title:
            legacy = [
                path.name
                for path in self.root.iterdir()
                if path.is_dir() and re.fullmatch(rf"0*{chapter.number}-{re.escape(prefix)}", path.name)
            ]
            if legacy:
                message = (
                    f"legacy_nested_chapter_requires_manual_migration: chapter={chapter.number}; "
                    f"existing={legacy!r}; canonical={target!r}; no chapter data moved or duplicated"
                )
                raise IntegrityError(message)

    def _existing_target(self, chapter: ChapterInfo, target: str) -> str:
        # A growing inventory can change zero padding (09 -> 009). Preserve existing folder names.
        self._reject_legacy_target(chapter, target)
        matches = [
            path
            for path in self.root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and re.fullmatch(rf"0*{chapter.number}-{re.escape(target.split('-', 1)[1])}", path.name)
        ]
        if len(matches) > 1:
            raise IntegrityError("ambiguous_existing_chapter_folders")
        return matches[0].name if matches else target

    def _validate_page_list(self, previous: dict[str, Any], urls: list[str], work: Path) -> None:
        if (
            len(previous["expected"]) != len(urls)
            or [item.get("source_url") for item in previous["expected"]] != urls
            or [item.get("position") for item in previous["expected"]] != list(range(1, len(urls) + 1))
        ):
            raise IntegrityError("manifest_source_list_mismatch")
        names = [item["name"] for item in previous["expected"]]
        if len(set(names)) != len(names):
            raise IntegrityError("manifest_duplicate_page_names")
        for name in names:
            checked_path(work, name)

    def _validate_manifest(self, manifest: dict[str, Any], chapter: ChapterInfo) -> None:
        if self.scope is None:
            raise IntegrityError("missing_download_scope")
        if (
            manifest.get("series") != asdict(self.scope.inventory.series)
            or manifest.get("episode_no") != chapter.data_episode_no
        ):
            raise IntegrityError("manifest_identity_mismatch")
        checked_path(self.root, manifest["target"])
        if (
            manifest.get("kind") == "images"
            and manifest.get("separate") is True
            and len(Path(manifest["target"]).parts) != 1
        ):
            raise IntegrityError("legacy_nested_manifest_requires_manual_migration")
        if self.options.save_as == "images" and self.options.separate and self.options.chapter_mode == "number-title":
            self._reject_legacy_target(chapter, manifest["target"])
        if not isinstance(manifest.get("expected"), list) or not isinstance(manifest.get("pages"), list):
            raise IntegrityError("invalid_manifest_pages")

    async def _pages(
        self, chapter: ChapterInfo, work: Path, manifest: dict[str, Any], attempts: dict[str, int], reuse: bool
    ) -> None:
        known = {record["position"]: record for record in manifest["pages"]} if reuse else {}
        manifest["pages"] = []
        manifest["failures"] = []
        tasks = [
            asyncio.create_task(self._page(chapter, work, expected, known.get(expected["position"]), attempts))
            for expected in manifest["expected"]
        ]
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    record = await task
                    manifest["pages"].append(record)
                    manifest["pages"].sort(key=lambda page: page["position"])
                    atomic_json(work / "manifest.json", manifest)
                    await self._progress(chapter, "PageCompleted")
                except Exception as error:
                    manifest["failures"].append({
                        "position": getattr(error, "position", None),
                        "attempts": getattr(error, "attempts", 0),
                        "error": error_name(error),
                    })
                    atomic_json(work / "manifest.json", manifest)
            errors = [task.exception() for task in tasks if not task.cancelled() and task.exception() is not None]
            if errors:
                self.chapter_failures[chapter.data_episode_no] = all(
                    isinstance(error, Exception) and retryable(error) for error in errors
                )
                first_error = errors[0]
                if first_error is not None:
                    raise first_error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _page(
        self,
        chapter: ChapterInfo,
        work: Path,
        expected: dict[str, Any],
        record: dict[str, Any] | None,
        attempts: dict[str, int],
    ) -> dict[str, Any]:
        async with self.page_semaphore:
            if record and all(record.get(key) == expected[key] for key in ("position", "source_url", "name")):
                staged = checked_path(work, record["staged"])
                if await blocking(
                    verify_image,
                    staged,
                    {**record, "mtime_ns": record.get("staged_mtime_ns")},
                    self.options.verify == "full",
                    self.options.max_image_pixels,
                ):
                    return {**record, "attempts": 0}

            async def operation() -> dict[str, Any]:
                if shutil.disk_usage(work).free < self.options.minimum_free_bytes:
                    raise ResourceLimitError("minimum_free_space")
                async with (
                    self.client.stream_image(expected["source_url"], self.options.quality) as response,
                    self.cpu_semaphore,
                ):
                    transformer = AioImageFormatTransformer(self.options.image_format, self.options.max_image_pixels)
                    stream, _ = await transformer.transform(self.client.image_chunks(response), expected["name"])
                    name = f"pages/{uuid.uuid4().hex}{Path(expected['name']).suffix}"
                    staged = checked_path(work, name)
                    async with AioFolderWriter(work) as writer:
                        await writer.write(stream, name)
                    evidence = await blocking(image_evidence, staged.read_bytes(), self.options.max_image_pixels)
                with staged.open("r+b") as handle:
                    os.fsync(handle.fileno())
                return {**expected, **evidence, "staged": name, "staged_mtime_ns": staged.stat().st_mtime_ns}

            key = hashlib.sha256(f"{expected['position']}:{expected['source_url']}".encode()).hexdigest()
            try:
                record = await self.client.retries.run(operation, self.options.page_retries, ledger=attempts, key=key)
                return {**record, "attempts": attempts.get(key, 0)}
            except Exception as error:
                raise PageOperationError(expected["position"], attempts.get(key, 0), error) from error

    async def _verify_existing(self, work: Path, manifest: dict[str, Any], metadata: dict[str, bytes]) -> bool:
        target = checked_path(self.root, manifest["target"])
        if not self._records_match(manifest):
            return False
        if self.options.save_as == "images" and not self.options.separate:
            return await self._verify_flat(manifest, metadata)
        if self.options.save_as == "pdf" and not self._verify_sidecars(metadata):
            return False
        if (
            target.is_file()
            and self.options.verify == "quick"
            and (
                target.stat().st_size != manifest.get("artifact_size")
                or target.stat().st_mtime_ns != manifest.get("artifact_mtime_ns")
            )
        ):
            return False
        return await blocking(
            verify_artifact,
            work,
            target,
            self.options.save_as,
            manifest["pages"],
            metadata,
            self.options.verify == "full",
            self.options.max_image_pixels,
        )

    def _records_match(self, manifest: dict[str, Any]) -> bool:
        expected = manifest["expected"]
        pages = manifest["pages"]
        return (
            len(expected) > 0
            and len(expected) == len(pages)
            and all(
                all(page.get(key) == item[key] for key in ("position", "name", "source_url"))
                for page, item in zip(pages, expected, strict=False)
            )
        )

    async def _publish(self, work: Path, manifest: dict[str, Any], metadata: dict[str, bytes]) -> None:
        if not self._records_match(manifest):
            raise IntegrityError("chapter_page_set_mismatch")
        staged = checked_path(work, f"artifact-{uuid.uuid4().hex}")
        await blocking(build_artifact, work, staged, self.options.save_as, manifest["pages"], metadata)
        if not await blocking(
            verify_artifact,
            work,
            staged,
            self.options.save_as,
            manifest["pages"],
            metadata,
            True,
            self.options.max_image_pixels,
        ):
            raise IntegrityError("staged_artifact_verification_failed")
        async with publication_lock(checked_path(self.root, ".webtoon-downloader/publish.lock")):
            if self.options.save_as == "images" and not self.options.separate:
                self._publish_flat(work, staged, manifest, metadata)
                if not await self._verify_flat(manifest, metadata, full=True):
                    raise IntegrityError("published_flat_artifact_mismatch")
            else:
                await self._publish_single(work, staged, manifest, metadata)
                if self.options.save_as == "pdf":
                    self._publish_sidecars(work, metadata)
        manifest["completed"] = True
        manifest["owned_target"] = True
        target = checked_path(self.root, manifest["target"])
        if self.options.save_as == "images":
            for page in manifest["pages"]:
                page["mtime_ns"] = checked_path(target, page["name"]).stat().st_mtime_ns
        else:
            manifest["artifact_size"] = target.stat().st_size
            manifest["artifact_mtime_ns"] = target.stat().st_mtime_ns
        atomic_json(work / "manifest.json", manifest)

    async def _publish_single(
        self, work: Path, staged: Path, manifest: dict[str, Any], metadata: dict[str, bytes]
    ) -> None:
        target = checked_path(self.root, manifest["target"])
        if target.exists():
            if await blocking(
                verify_artifact,
                work,
                target,
                self.options.save_as,
                manifest["pages"],
                metadata,
                True,
                self.options.max_image_pixels,
            ):
                return
            if not manifest.get("owned_target") or not (self.options.resume or self.options.retry_failed):
                raise IntegrityError("existing_artifact_conflict_use_resume_for_managed_data")
            backup = work / f"backup-{uuid.uuid4().hex}"
            os.rename(target, backup)
            try:
                self._publish_new(staged, target)
            except BaseException:
                if not target.exists():
                    os.rename(backup, target)
                raise
        else:
            self._publish_new(staged, target)

    def _publish_new(self, staged: Path, target: Path) -> None:
        if staged.is_dir():
            os.rename(staged, target)
            sync_directory(target.parent)
        else:
            publish_file(staged, target)

    def _publish_flat(self, work: Path, staged: Path, manifest: dict[str, Any], metadata: dict[str, bytes]) -> None:
        names = [page["name"] for page in manifest["pages"]] + list(metadata)
        prefix = manifest["expected"][0]["name"].split("_", 1)[0] + "_"
        actual = {path.name for path in self.root.iterdir() if path.name.startswith(prefix) and path.is_file()}
        previous_names = set(manifest.get("previous_names", []))
        actual |= {name for name in previous_names if checked_path(self.root, name).is_file()}
        extra = actual - set(names)
        if extra:
            if (
                not manifest.get("owned_target")
                or not (self.options.resume or self.options.retry_failed)
                or not extra.issubset(previous_names)
            ):
                raise IntegrityError("unexpected_flat_chapter_files")
            for name in extra:
                os.rename(checked_path(self.root, name), work / f"backup-{uuid.uuid4().hex}")
        for name in names:
            source = checked_path(staged, name)
            target = checked_path(self.root, name)
            if target.exists() and target.read_bytes() != source.read_bytes():
                if not manifest.get("owned_target") or not (self.options.resume or self.options.retry_failed):
                    raise IntegrityError("existing_flat_artifact_conflict")
                os.rename(target, work / f"backup-{uuid.uuid4().hex}")
            if not target.exists():
                publish_file(source, target)

    async def _verify_flat(
        self, manifest: dict[str, Any], metadata: dict[str, bytes], full: bool | None = None
    ) -> bool:
        prefix = manifest["expected"][0]["name"].split("_", 1)[0] + "_"
        actual = {path.name for path in self.root.iterdir() if path.name.startswith(prefix) and path.is_file()}
        if actual != {page["name"] for page in manifest["pages"]} | set(metadata):
            return False
        for page in manifest["pages"]:
            if not await blocking(
                verify_image,
                checked_path(self.root, page["name"]),
                page,
                self.options.verify == "full" if full is None else full,
                self.options.max_image_pixels,
            ):
                return False
        return all(checked_path(self.root, name).read_bytes() == data for name, data in metadata.items())

    def _verify_sidecars(self, metadata: dict[str, bytes]) -> bool:
        return all(
            checked_path(self.root, name).is_file() and checked_path(self.root, name).read_bytes() == data
            for name, data in metadata.items()
        )

    def _publish_sidecars(self, work: Path, metadata: dict[str, bytes]) -> None:
        for name, data in metadata.items():
            target = checked_path(self.root, name)
            if target.exists():
                if target.read_bytes() != data:
                    raise IntegrityError("metadata_sidecar_conflict")
            else:
                staged = checked_path(work, f"metadata-{uuid.uuid4().hex}")
                with staged.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                publish_file(staged, target)

    def _result(self, manifest: dict[str, Any]) -> ChapterResult:
        if self.scope is None:
            raise IntegrityError("missing_download_scope")
        expected = tuple(
            PageExpectation(item["position"], item["source_url"], item["name"]) for item in manifest["expected"]
        )
        pages = tuple(
            PageResult(
                PageExpectation(item["position"], item["source_url"], item["name"]),
                item["size"],
                item["sha256"],
                True,
                attempts=item.get("attempts", 0),
            )
            for item in manifest["pages"]
        )
        for failure in manifest.get("failures", []):
            matching = next((page for page in expected if page.position == failure.get("position")), None)
            if matching is not None:
                pages += (PageResult(matching, 0, error=failure["error"], attempts=failure["attempts"]),)
        return ChapterResult(
            manifest["episode_no"],
            expected,
            self.scope.inventory.series,
            pages,
            checked_path(self.root, manifest["target"]),
            bool(manifest.get("completed")),
            bool(manifest.get("completed")),
            attempts=manifest.get("chapter_attempt", 0),
        )

    def _metadata(self, chapter: ChapterInfo, extractor: WebtoonViewerPageExtractor) -> dict[str, bytes]:
        metadata = {}
        if self.options.export_metadata and self.options.exporter_format in {"text", "all"}:
            generator = SeparateFileNameGenerator() if self.options.separate else NonSeparateFileNameGenerator()
            directory = (
                SeparateFileNameGenerator(chapter_mode=self.options.chapter_mode).get_chapter_directory(chapter)
                if self.options.separate and self.options.save_as != "images"
                else Path(".")
            )
            metadata[(directory / generator.get_title_filename(chapter)).as_posix()] = (chapter.title + "\n").encode()
            if extractor.chapter_notes:
                metadata[(directory / generator.get_notes_filename(chapter)).as_posix()] = (
                    extractor.chapter_notes + "\n"
                ).encode()
        if self.options.save_as == "cbz":
            if self.scope is None:
                raise IntegrityError("missing_download_scope")
            series = self.series_metadata
            metadata["ComicInfo.xml"] = build_comicinfo_xml(
                ComicInfoMetadata(
                    series=chapter.series_title,
                    title=chapter.title,
                    number=str(chapter.number),
                    count=chapter.total_chapters,
                    notes=extractor.chapter_notes,
                    language_iso=self.scope.inventory.series.language,
                    writer=series.author if series else None,
                    genre=series.genre if series else None,
                    page_count=len(extractor.img_urls),
                    web=public_url(self.url),
                )
            )
        return metadata

    async def _export_series(self, chapters: list[ChapterInfo]) -> None:
        if not self.options.export_metadata:
            return
        summary = None
        try:
            if self.series_metadata:
                summary = self.series_metadata.series_summary
        except ElementNotFoundError:
            pass
        if self.options.exporter_format in {"json", "all"}:
            atomic_json(
                checked_path(self.root, "info.json"),
                {
                    "schema_version": SCHEMA_VERSION,
                    "summary": summary,
                    "chapters": {
                        str(chapter.number): {
                            "title": chapter.title,
                            "notes": self.notes.get(chapter.data_episode_no, ""),
                        }
                        for chapter in chapters
                        if self.results.get(chapter.data_episode_no)
                        and self.results[chapter.data_episode_no].is_complete
                    },
                },
            )
        if summary and self.options.exporter_format in {"text", "all"}:
            target = checked_path(self.root, "summary.txt")
            if not target.exists():
                with target.open("x", encoding="utf-8") as output:
                    output.write(summary + "\n")

    async def _progress(
        self, chapter: ChapterInfo, event: Any, extractor: WebtoonViewerPageExtractor | None = None
    ) -> None:
        if self.options.chapter_progress_callback:
            await self.options.chapter_progress_callback(chapter, event, extractor)

    def _clean_successful_workspace(self) -> None:  # noqa: C901 - fail closed before recursive cache removal
        """Remove only recognized cache data, never backups, links or another unfinished scope."""
        if not self.owns_output or self.scope is None:
            return
        base = self._base()
        if not base.exists():
            return
        # Resolve and inspect the exact namespace before any recursive removal.
        cache = checked_path(self.root, ".webtoon-downloader")
        if base.resolve().parent != cache.resolve() or not base.resolve().is_relative_to(self.root.resolve()):
            raise IntegrityError("unsafe_cleanup_target")
        if any(path.name not in {base.name, "publish.lock"} for path in cache.iterdir()):
            raise IntegrityError("workspace_cleanup_other_work_remains")
        resolved_base = base.resolve()
        for path in base.rglob("*"):
            if (
                path.is_symlink()
                or getattr(path.lstat(), "st_file_attributes", 0) & 0x400
                or os.path.ismount(path)
                or not path.resolve().is_relative_to(resolved_base)
                or path.name.startswith("backup-")
            ):
                raise IntegrityError("workspace_cleanup_preserves_links_or_backups")
        if any(path.name not in {"chapters", "results"} for path in base.iterdir()):
            raise IntegrityError("workspace_cleanup_unknown_content")
        for chapter in (base / "chapters").iterdir():
            if not chapter.is_dir() or not chapter.name.isdecimal():
                raise IntegrityError("workspace_cleanup_unknown_chapter")
            manifest = read_json(chapter / "manifest.json")
            if manifest.get("series") != asdict(self.scope.inventory.series):
                raise IntegrityError("workspace_cleanup_identity_mismatch")
            if manifest.get("completed") is not True:
                # A successful incremental range must not discard another failed range.
                raise IntegrityError("workspace_cleanup_unfinished_chapters")
            allowed = {"manifest.json", "chapter.lock", "pages"}
            if any(
                p.name not in allowed and not re.fullmatch(r"artifact-[0-9a-f]{32}", p.name) for p in chapter.iterdir()
            ):
                raise IntegrityError("workspace_cleanup_unknown_content")
            pages = chapter / "pages"
            if pages.exists() and any(
                not p.is_file() or not re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9]+", p.name) for p in pages.iterdir()
            ):
                raise IntegrityError("workspace_cleanup_unknown_page")
        reports = base / "results"
        if reports.exists():
            for path in reports.iterdir():
                if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name) or read_json(path).get("series") != asdict(
                    self.scope.inventory.series
                ):
                    raise IntegrityError("workspace_cleanup_unknown_report")
        shutil.rmtree(base)
        if all(path.name == "publish.lock" and path.is_file() and not path.is_symlink() for path in cache.iterdir()):
            for path in cache.iterdir():
                path.unlink()
            cache.rmdir()
        sync_directory(self.root)

    def _report_payload(self, result: BatchResult) -> dict[str, Any]:
        payload: dict[str, Any] = report_value(asdict(result))
        payload.update({
            "schema_version": SCHEMA_VERSION,
            "status": result.status,
            "exit_code": result.exit_code,
            "series": asdict(self.scope.inventory.series) if self.scope else None,
            "expected_episode_ids": list(self.scope.episode_ids) if self.scope else [],
            "incomplete_episode_ids": list(result.incomplete_episode_ids),
            "run_id": self.run_id,
        })
        return payload

    def _requested_report_failure(self, candidate: BatchResult) -> BatchResult | None:
        if not candidate.is_complete or not self.options.result_file or self.options.result_file == "-":
            return None
        # A requested disk report must be writable before discarding recovery data.
        try:
            provisional = replace(candidate, report_written=True, report_path=Path(self.options.result_file))
            atomic_json(Path(self.options.result_file), self._report_payload(provisional))
        except Exception as failure:
            return replace(
                candidate, result_delivered=False, error=f"report_write_failed: {error_name(failure)}", retryable=False
            )
        return None

    def _finish(self, error: str | None) -> BatchResult:
        expected = self.scope.episode_ids if self.scope else ()
        if self.scope:
            for episode in expected:
                if episode not in self.results:
                    self.results[episode] = ChapterResult(
                        episode, (), self.scope.inventory.series, interrupted=self.interrupted, error="not_started"
                    )
        result = BatchResult(
            self.scope,
            tuple(self.results[item] for item in expected),
            interrupted=self.interrupted,
            error=error,
            verification=self.options.verify,
            interruption_code=self.options.interruption_code,
            retryable=(
                self.failure_retryable if error else bool(self.chapter_failures) and all(self.chapter_failures.values())
            ),
        )
        stdout_result = self.options.result_file == "-"
        # No implicit report after success: default image output contains only chapter images.
        candidate = replace(result, result_delivered=True)
        report_failure = self._requested_report_failure(candidate)
        if report_failure is not None:
            return report_failure
        if candidate.is_complete and not self.options.keep_workspace:
            try:
                self._clean_successful_workspace()
            except Exception as failure:
                result = replace(result, error=f"workspace_cleanup_failed: {error_name(failure)}", retryable=False)
        memory_result = not self.options.result_file and (
            not self.owns_output or (not self.options.keep_workspace and result.error is None and candidate.is_complete)
        )
        target = (
            Path(self.options.result_file)
            if self.options.result_file
            else self._base() / "results" / f"{self.run_id}.json"
        )
        result = replace(
            result,
            report_written=not (stdout_result or memory_result),
            result_delivered=True,
            report_path=None if stdout_result or memory_result else target,
        )
        payload = self._report_payload(result)
        try:
            if stdout_result:
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
                sys.stdout.flush()
            elif not memory_result:
                atomic_json(target, payload)
        except Exception as failure:
            result = replace(
                result,
                report_written=False,
                result_delivered=False,
                retryable=False,
                error=f"report_write_failed: {error_name(failure)}",
            )
        return result
