from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from webtoon_downloader.core.webtoon.inventory import ChapterScope, SeriesIdentity

DownloadResult: TypeAlias = str | Path
"""Legacy public result alias, retained until the reliability pipeline is integrated."""


@dataclass(frozen=True)
class PageExpectation:
    """One source-list position, including repeated URLs at different positions."""

    position: int
    source_url: str
    target_name: str


@dataclass(frozen=True)
class PageResult:
    page: PageExpectation
    size: int
    sha256: str = ""
    content_verified: bool = False
    error: str | None = None
    attempts: int = 0

    @property
    def is_verified(self) -> bool:
        return (
            self.page.position > 0
            and bool(self.page.source_url)
            and bool(self.page.target_name)
            and self.size > 0
            and self.content_verified
            and len(self.sha256) == 64
            and all(char in "0123456789abcdef" for char in self.sha256)
            and self.error is None
        )


@dataclass(frozen=True)
class ChapterResult:
    """Collected verification evidence; this model does not inspect the filesystem."""

    episode_no: int
    expected_pages: tuple[PageExpectation, ...]
    series: SeriesIdentity
    pages: tuple[PageResult, ...] = ()
    artifact: Path | None = None
    artifact_verified: bool = False
    published: bool = False
    interrupted: bool = False
    error: str | None = None
    attempts: int = 0

    @property
    def is_complete(self) -> bool:
        expected_positions = tuple(page.position for page in self.expected_pages)
        if not self.expected_pages or expected_positions != tuple(range(1, len(self.expected_pages) + 1)):
            return False
        if len({page.target_name for page in self.expected_pages}) != len(self.expected_pages):
            return False
        actual_pages = tuple(result.page for result in self.pages)
        return (
            len(actual_pages) == len(self.expected_pages)
            and set(actual_pages) == set(self.expected_pages)
            and all(result.is_verified for result in self.pages)
            and self.artifact is not None
            and self.artifact_verified
            and self.published
            and not self.interrupted
            and self.error is None
        )


@dataclass(frozen=True)
class BatchResult:
    """Success requires the entire selected scope, not only attempted chapters."""

    scope: ChapterScope | None
    chapters: tuple[ChapterResult, ...] = ()
    report_written: bool = False
    interrupted: bool = False
    error: str | None = None
    report_path: Path | None = None
    verification: Literal["full", "quick"] = "full"
    interruption_code: Literal[130, 143] = 130
    result_delivered: bool = False
    retryable: bool = False

    @property
    def incomplete_episode_ids(self) -> tuple[int, ...]:
        if self.scope is None:
            return ()
        completed = {
            chapter.episode_no
            for chapter in self.chapters
            if chapter.series == self.scope.inventory.series and chapter.is_complete
        }
        return tuple(item for item in self.scope.episode_ids if item not in completed)

    @property
    def is_complete(self) -> bool:
        if self.scope is None or not self.scope.inventory.is_complete:
            return False
        actual_ids = tuple(chapter.episode_no for chapter in self.chapters)
        return (
            len(actual_ids) == len(self.scope.episode_ids)
            and set(actual_ids) == set(self.scope.episode_ids)
            and all(chapter.series == self.scope.inventory.series for chapter in self.chapters)
            and all(chapter.is_complete for chapter in self.chapters)
            and (self.report_written or self.result_delivered)
            and not self.interrupted
            and self.error is None
        )

    @property
    def status(self) -> Literal["complete", "partial", "failed", "interrupted"]:
        if self.interrupted or any(chapter.interrupted for chapter in self.chapters):
            return "interrupted"
        if self.is_complete:
            return "complete"
        if any(chapter.is_complete for chapter in self.chapters):
            return "partial"
        return "failed"

    @property
    def exit_code(self) -> int:
        if self.status == "interrupted":
            return self.interruption_code
        return 0 if self.is_complete else 1
