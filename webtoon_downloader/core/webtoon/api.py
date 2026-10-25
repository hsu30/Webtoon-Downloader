from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

import dacite
from furl import furl

from webtoon_downloader.core.webtoon.client import WebtoonHttpClient, WebtoonMobileURL

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from webtoon_downloader.core.webtoon.inventory import EpisodePageEvidence

DEFAULT_EPISODE_PAGE_SIZE: Final = 30
"""Webtoon's default episode API page size."""

REQUEST_ALL_EPISODES_PAGE_SIZE: Final = 99999
"""Historical all-episodes page size used for one-call episode list requests."""

WebtoonSeriesType: TypeAlias = Literal["webtoon", "canvas"]
"""Known Webtoon episode API path segment values."""

WebtoonLanguageCode: TypeAlias = Literal["de", "en", "es", "fr", "id", "th", "zh-hant"]
"""Language path segments currently exposed by the global Webtoon site."""

WEBTOON_TYPE_ORIGINAL: Final[Literal["webtoon"]] = "webtoon"
"""Webtoon's API path segment for Originals series."""

WEBTOON_TYPE_CANVAS: Final[Literal["canvas"]] = "canvas"
"""Webtoon's API path segment for Canvas series."""

WEBTOON_API_ROOT_PATH_SEGMENTS: Final = ("api", "v1")
"""Root path segments for Webtoon's mobile API."""

WEBTOON_LANGUAGE_CODES: Final[tuple[WebtoonLanguageCode, ...]] = ("de", "en", "es", "fr", "id", "th", "zh-hant")
"""Language path segments currently exposed by the global Webtoon site."""

EPISODES_PATH_SEGMENT: Final = "episodes"
"""Webtoon's mobile API path segment for episode list requests."""


def build_series_api_url(webtoon_type: WebtoonSeriesType, series_id: int) -> str:
    """Build a Webtoon mobile series API URL."""
    url = furl(WebtoonMobileURL)
    url.path.segments = [*WEBTOON_API_ROOT_PATH_SEGMENTS, webtoon_type, str(series_id)]
    return str(url.url)


def build_episodes_url(
    series_api_url: str,
    *,
    page_size: int,
    reading_language_code: WebtoonLanguageCode | None = None,
    cursor: int | None = None,
) -> str:
    """Build a Webtoon mobile episode-list API URL."""
    url = furl(series_api_url)
    path_segments = [segment for segment in url.path.segments if segment]
    if not path_segments or path_segments[-1] != EPISODES_PATH_SEGMENT:
        path_segments.append(EPISODES_PATH_SEGMENT)

    url.path.segments = path_segments
    url.args.clear()
    url.args["pageSize"] = page_size
    if reading_language_code:
        url.args["readingLanguageCode"] = reading_language_code
    if cursor is not None:
        url.args["cursor"] = cursor

    return str(url.url)


@dataclass
class EpisodeInfo:
    """Episode item returned by Webtoon's mobile episode API."""

    episodeNo: int
    thumbnail: str
    episodeTitle: str
    viewerLink: str
    exposureDateMillis: int
    displayUp: bool
    hasBgm: bool | None  # In Canvas this is not defined


@dataclass
class GetEpisodesResponseResult:
    """Payload wrapper containing the episode list returned by Webtoon."""

    episodeList: list[EpisodeInfo]


@dataclass
class GetEpisodesResponse:
    """Top-level response shape returned by Webtoon's episode API."""

    result: GetEpisodesResponseResult


@dataclass
class WebtoonAPI:
    """Small client for Webtoon's mobile API endpoints."""

    client: WebtoonHttpClient
    page_evidence: tuple[EpisodePageEvidence, ...] = field(default=(), init=False)

    async def get_episodes_data(
        self,
        series_api_url: str,
        page_size: int = DEFAULT_EPISODE_PAGE_SIZE,
        reading_language_code: WebtoonLanguageCode | None = None,
    ) -> list[EpisodeInfo]:
        """Return episode data for a given series ID."""
        from webtoon_downloader.core.webtoon.inventory import (
            EpisodePageEvidence,
            InventoryContractError,
            InventoryProblem,
        )

        cursor = None
        seen: set[int | None] = set()
        ids: set[int] = set()
        data: list[EpisodeInfo] = []
        evidence = []
        while cursor not in seen:
            if len(seen) >= 10000:
                raise InventoryContractError(InventoryProblem.NONTERMINAL_SOURCE)
            seen.add(cursor)
            url = build_episodes_url(
                series_api_url, page_size=page_size, reading_language_code=reading_language_code, cursor=cursor
            )
            response = await self.client.get(url)
            response.raise_for_status()
            payload = response.json()
            page = EpisodePageEvidence.from_payload(payload, requested_cursor=cursor)
            if page.next_cursor is None:
                raise InventoryContractError(InventoryProblem.MISSING_CURSOR)
            if not page.episode_ids and page.next_cursor != 0:
                raise InventoryContractError(InventoryProblem.EMPTY_NONTERMINAL_PAGE)
            if len(set(page.episode_ids)) != len(page.episode_ids) or ids.intersection(page.episode_ids):
                raise InventoryContractError(InventoryProblem.DUPLICATE_IDENTITIES)
            parsed = dacite.from_dict(data_class=GetEpisodesResponse, data=payload)
            data.extend(parsed.result.episodeList)
            evidence.append(page)
            ids.update(page.episode_ids)
            if page.next_cursor == 0:
                self.page_evidence = tuple(evidence)
                return data
            cursor = page.next_cursor
        raise InventoryContractError(InventoryProblem.BROKEN_CURSOR_CHAIN)
