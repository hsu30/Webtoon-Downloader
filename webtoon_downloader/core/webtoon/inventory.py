"""Source inventory and range contracts, independent of download side effects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from webtoon_downloader.core.webtoon.api import WEBTOON_LANGUAGE_CODES, WebtoonLanguageCode, WebtoonSeriesType
from webtoon_downloader.core.webtoon.models import ChapterInfo


class InventoryProblem(str, Enum):
    INVALID_SERIES_IDENTITY = "Series language, type or title_no is invalid"
    API_FAILURE = "Episode API did not report success"
    INVALID_LIST = "Episode API has no valid episodeList"
    INVALID_EPISODE = "Episode API contains an invalid episodeNo"
    INVALID_CURSOR = "Source nextCursor is invalid"
    EMPTY_SOURCE = "Source chapter list is empty"
    MISSING_EVIDENCE = "Source cursor evidence is missing"
    DUPLICATE_IDENTITIES = "Source chapter identities are invalid or duplicated"
    INVALID_ORDER = "Source chapter presentation order is inconsistent"
    INVALID_TOTAL = "Source chapter totals are inconsistent"
    BROKEN_CURSOR_CHAIN = "Source cursor chain is broken or repeated"
    MISSING_CURSOR = "Source nextCursor is missing"
    EMPTY_NONTERMINAL_PAGE = "Source has an empty nonterminal page"
    PAGES_AFTER_TERMINAL = "Source has pages after the terminal cursor"
    NONTERMINAL_SOURCE = "Source has not reached the terminal cursor"
    PARSED_LIST_MISMATCH = "Source page identities do not match parsed chapters"
    LATEST_WITH_START = "Latest cannot be combined with a start chapter"
    INVALID_RANGE_BOUNDS = "Chapter range bounds must be positive integers"
    RANGE_OUT_OF_BOUNDS = "Chapter range is reversed or exceeds the source inventory"
    INVALID_SELECTION = "Selected chapter scope is empty or duplicated"
    SELECTION_MISMATCH = "Selected chapter scope does not match source order and identities"


class InventoryContractError(ValueError):
    """A source response or requested range cannot establish a valid scope."""

    def __init__(self, reason: InventoryProblem) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class SeriesIdentity:
    language: WebtoonLanguageCode
    series_type: WebtoonSeriesType
    title_no: int

    def __post_init__(self) -> None:
        if (
            self.language not in WEBTOON_LANGUAGE_CODES
            or self.series_type not in {"webtoon", "canvas"}
            or not _is_positive_integer(self.title_no)
        ):
            raise InventoryContractError(InventoryProblem.INVALID_SERIES_IDENTITY)


@dataclass(frozen=True)
class EpisodePageEvidence:
    """Evidence from one successful API page; a missing cursor is unverified."""

    requested_cursor: int | None
    next_cursor: int | None
    episode_ids: tuple[int, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], *, requested_cursor: int | None = None) -> EpisodePageEvidence:
        if payload.get("success") is not True:
            raise InventoryContractError(InventoryProblem.API_FAILURE)
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("episodeList"), list):
            raise InventoryContractError(InventoryProblem.INVALID_LIST)

        ids = []
        for item in result["episodeList"]:
            if not isinstance(item, dict) or not _is_positive_integer(item.get("episodeNo")):
                raise InventoryContractError(InventoryProblem.INVALID_EPISODE)
            ids.append(item["episodeNo"])

        next_cursor = result.get("nextCursor")
        if next_cursor is not None and (
            not isinstance(next_cursor, int) or isinstance(next_cursor, bool) or next_cursor < 0
        ):
            raise InventoryContractError(InventoryProblem.INVALID_CURSOR)
        return cls(requested_cursor=requested_cursor, next_cursor=next_cursor, episode_ids=tuple(ids))


@dataclass(frozen=True)
class EpisodeInventory:
    """A frozen source list plus the cursor chain that establishes its boundary."""

    series: SeriesIdentity
    chapters: tuple[ChapterInfo, ...]
    evidence: tuple[EpisodePageEvidence, ...]

    @property
    def incomplete_reason(self) -> InventoryProblem | None:
        problem = self._chapter_problem() or self._cursor_problem()
        if problem is not None:
            return problem
        observed_ids = tuple(item for page in self.evidence for item in page.episode_ids)
        if observed_ids != tuple(chapter.data_episode_no for chapter in self.chapters):
            return InventoryProblem.PARSED_LIST_MISMATCH
        return None

    def _chapter_problem(self) -> InventoryProblem | None:
        if not self.chapters:
            return InventoryProblem.EMPTY_SOURCE

        chapter_ids = tuple(chapter.data_episode_no for chapter in self.chapters)
        if any(not _is_positive_integer(item) for item in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids):
            return InventoryProblem.DUPLICATE_IDENTITIES
        if tuple(chapter.number for chapter in self.chapters) != tuple(range(1, len(self.chapters) + 1)):
            return InventoryProblem.INVALID_ORDER
        if any(chapter.total_chapters != len(self.chapters) for chapter in self.chapters):
            return InventoryProblem.INVALID_TOTAL
        return None

    def _cursor_problem(self) -> InventoryProblem | None:
        if not self.evidence:
            return InventoryProblem.MISSING_EVIDENCE
        expected_cursor = None
        visited: set[int | None] = set()
        for index, page in enumerate(self.evidence):
            if page.requested_cursor != expected_cursor or page.requested_cursor in visited:
                return InventoryProblem.BROKEN_CURSOR_CHAIN
            visited.add(page.requested_cursor)
            if page.next_cursor is None:
                return InventoryProblem.MISSING_CURSOR
            if page.next_cursor < 0 or isinstance(page.next_cursor, bool):
                return InventoryProblem.INVALID_CURSOR
            if not page.episode_ids and page.next_cursor != 0:
                return InventoryProblem.EMPTY_NONTERMINAL_PAGE
            if page.next_cursor == 0 and index != len(self.evidence) - 1:
                return InventoryProblem.PAGES_AFTER_TERMINAL
            expected_cursor = page.next_cursor

        if expected_cursor != 0:
            return InventoryProblem.NONTERMINAL_SOURCE
        return None

    @property
    def is_complete(self) -> bool:
        return self.incomplete_reason is None

    def select(self, start: int | None = None, end: int | Literal["latest"] | None = None) -> ChapterScope:
        reason = self.incomplete_reason
        if reason is not None:
            raise InventoryContractError(reason)
        if end == "latest":
            if start is not None:
                raise InventoryContractError(InventoryProblem.LATEST_WITH_START)
            return ChapterScope(self, (self.chapters[-1].data_episode_no,))

        first = 1 if start is None else start
        last = len(self.chapters) if end is None else end
        if not _is_positive_integer(first) or not _is_positive_integer(last):
            raise InventoryContractError(InventoryProblem.INVALID_RANGE_BOUNDS)
        if first > last or last > len(self.chapters):
            raise InventoryContractError(InventoryProblem.RANGE_OUT_OF_BOUNDS)
        return ChapterScope(self, tuple(chapter.data_episode_no for chapter in self.chapters[first - 1 : last]))


@dataclass(frozen=True)
class ChapterScope:
    """Selected stable identities; source verification remains mandatory for success."""

    inventory: EpisodeInventory
    episode_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        source_ids = tuple(chapter.data_episode_no for chapter in self.inventory.chapters)
        if not self.episode_ids or len(set(self.episode_ids)) != len(self.episode_ids):
            raise InventoryContractError(InventoryProblem.INVALID_SELECTION)
        selected = set(self.episode_ids)
        if tuple(item for item in source_ids if item in selected) != self.episode_ids:
            raise InventoryContractError(InventoryProblem.SELECTION_MISMATCH)
