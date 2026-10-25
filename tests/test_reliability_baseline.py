"""Keep the original fault injections as regression tests after the fixes."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import httpx
import pytest
from h2.exceptions import InvalidBodyLengthError
from PIL import Image

from tests.reliability_fixtures import SERIES_HTML, VIEWER_HTML, episode_payload, source_inventory
from webtoon_downloader.core.downloaders.image import HttpImageDownloader, ImageDownloadResult
from webtoon_downloader.core.exceptions import ChapterDownloadError
from webtoon_downloader.core.webtoon.api import WebtoonAPI
from webtoon_downloader.core.webtoon.client import WebtoonHttpClient
from webtoon_downloader.core.webtoon.downloaders.chapter import ChapterDownloader
from webtoon_downloader.core.webtoon.downloaders.comic import WebtoonDownloader
from webtoon_downloader.core.webtoon.fetchers import WebtoonFetcher
from webtoon_downloader.core.webtoon.inventory import InventoryContractError
from webtoon_downloader.core.webtoon.models import ChapterInfo
from webtoon_downloader.core.webtoon.namer import SeparateFileNameGenerator
from webtoon_downloader.storage import AioZipWriter
from webtoon_downloader.transformers.image import AioImageFormatTransformer

SERIES_URL = "https://www.webtoons.com/zh-hant/romance/example/list?title_no=4601"
VIEWER_URL = "https://www.webtoons.com/zh-hant/romance/example/viewer?title_no=4601&episode_no=125"
FIRST_IMAGE = "https://images.example.invalid/first.jpg"
SECOND_IMAGE = "https://images.example.invalid/second.jpg"


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, kind: Literal["timeout", "length"], page_written: asyncio.Event) -> None:
        self.kind = kind
        self.page_written = page_written

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.kind == "timeout":
            raise httpx.ReadTimeout("timeout")
        await asyncio.wait_for(self.page_written.wait(), timeout=5)
        cause = InvalidBodyLengthError(91173, 0)
        raise httpx.LocalProtocolError(str(cause)) from cause
        yield b""  # Keep this an async generator even when the body fails before its first byte.


class ScenarioTransport(httpx.AsyncBaseTransport):
    def __init__(self, fault: Literal["timeout", "length", "none"] = "none") -> None:
        self.fault = fault
        self.calls: Counter[str] = Counter()
        self.page_written = asyncio.Event()
        with io.BytesIO() as output:
            Image.new("RGB", (8, 8), "white").save(output, format="JPEG")
            self.image = output.getvalue()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls[url] += 1
        if request.url.host == "images.example.invalid":
            if url == SECOND_IMAGE and self.fault == "length" and self.calls[url] == 1:
                return httpx.Response(200, request=request, stream=BrokenBody("length", self.page_written))
            return httpx.Response(200, request=request, content=self.image)
        if request.url.path.endswith("/viewer"):
            if self.fault == "timeout" and self.calls[url] == 1:
                return httpx.Response(200, request=request, stream=BrokenBody("timeout", self.page_written))
            return httpx.Response(200, request=request, content=VIEWER_HTML)
        if request.url.path.endswith("/episodes"):
            name = "last" if request.url.params.get("cursor") == "2" else "first"
            return httpx.Response(200, request=request, json=episode_payload(name))
        if request.url.path.endswith("/list"):
            return httpx.Response(200, request=request, content=SERIES_HTML)
        raise AssertionError(url)


class ObservedZipWriter(AioZipWriter):
    def __init__(self, container: io.BytesIO, page_written: asyncio.Event) -> None:
        super().__init__(container)
        self.page_written = page_written

    async def write(self, stream: AsyncIterator[bytes], item_name: str) -> int:
        size = await super().write(stream, item_name)
        self.page_written.set()
        return size


def fixture_client(monkeypatch: pytest.MonkeyPatch, transport: ScenarioTransport) -> WebtoonHttpClient:
    # Exercise the real WebtoonHttpClient retry construction with an offline base transport.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)
    return WebtoonHttpClient(retry_strategy="exponential", retry_delay=0, retry_max_delay=0)


def chapter_downloader(client: WebtoonHttpClient) -> ChapterDownloader:
    return ChapterDownloader(
        client=client,
        image_downloader=HttpImageDownloader(
            client, concurrent_downloads_limit=2, transformers=[AioImageFormatTransformer("JPG")]
        ),
        file_name_generator=SeparateFileNameGenerator(chapter_mode="number-title"),
        concurrent_downloads_limit=3,
    )


@pytest.mark.asyncio
async def test_chapter_body_timeout_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScenarioTransport("timeout")
    chapter = source_inventory().chapters[0]
    container = io.BytesIO()
    async with fixture_client(monkeypatch, transport) as client:
        downloader = chapter_downloader(client)
        await downloader.run(chapter, directory=Path("unused"), storage=AioZipWriter(container))

    assert transport.calls[chapter.viewer_url] == 2
    with zipfile.ZipFile(container) as archive:
        assert len(archive.namelist()) == 2


@pytest.mark.asyncio
async def test_length_mismatch_retries_the_missing_image(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScenarioTransport("length")
    chapter = source_inventory().chapters[0]
    chapter = ChapterInfo(124, VIEWER_URL, 125, "Example chapter", chapter.series_title, 166)
    container = io.BytesIO()
    async with fixture_client(monkeypatch, transport) as client:
        downloader = chapter_downloader(client)
        writer = ObservedZipWriter(container, transport.page_written)
        await downloader.run(chapter, directory=Path("unused"), storage=writer)

    assert transport.calls[SECOND_IMAGE] == 2
    container.seek(0)
    with zipfile.ZipFile(container) as archive:
        assert set(archive.namelist()) == {"124-Example chapter/1.jpg", "124-Example chapter/2.jpg"}
        assert archive.read(archive.namelist()[0]) == transport.image


@pytest.mark.asyncio
async def test_api_follows_the_nonterminal_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScenarioTransport()
    async with fixture_client(monkeypatch, transport) as client:
        api = WebtoonAPI(client)
        chapters = await api.get_episodes_data(
            "https://m.webtoons.com/api/v1/webtoon/4601", page_size=99999, reading_language_code="zh-hant"
        )

    assert [chapter.episodeNo for chapter in chapters] == [1, 2, 5]
    assert len(transport.calls) == 2
    assert episode_payload("first")["result"]["nextCursor"] == 2


@pytest.mark.asyncio
async def test_fetcher_rejects_an_out_of_range_end(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScenarioTransport()
    async with fixture_client(monkeypatch, transport) as client:
        with pytest.raises(InventoryContractError):
            await WebtoonFetcher(client, SERIES_URL).get_chapters_details(SERIES_URL, 1, 999)


@pytest.mark.asyncio
async def test_low_level_orchestrator_drains_tasks_before_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScenarioTransport()
    completed: set[int] = set()
    neighbours_finished = asyncio.Event()

    async def run_chapter(chapter: ChapterInfo, *_: object, **__: object) -> list[ImageDownloadResult]:
        if chapter.data_episode_no == 2:
            await asyncio.wait_for(neighbours_finished.wait(), timeout=5)
            raise ChapterDownloadError(chapter.viewer_url, httpx.ReadTimeout("simulated middle chapter failure"))
        completed.add(chapter.data_episode_no)
        if completed == {1, 5}:
            neighbours_finished.set()
        return []

    async def get_chapters() -> list[ChapterInfo]:
        return list(source_inventory().chapters)

    async with fixture_client(monkeypatch, transport) as client:
        downloader = chapter_downloader(client)
        monkeypatch.setattr(downloader, "run", run_chapter)
        batch = WebtoonDownloader(SERIES_URL, client, downloader, "images", 100)
        monkeypatch.setattr(batch, "_get_chapters", get_chapters)
        with pytest.raises(ChapterDownloadError):
            await batch.run()

    assert completed == {1, 5}
