"""An offline source with cursor pagination, real tiny JPEGs and injectable faults."""

from __future__ import annotations

import asyncio
import io
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
from PIL import Image

from tests.reliability_fixtures import SERIES_HTML, episode_payload
from webtoon_downloader.core.webtoon.client import WebtoonHttpClient
from webtoon_downloader.core.webtoon.downloaders.comic import download_webtoon_result
from webtoon_downloader.core.webtoon.downloaders.options import WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult

SERIES_URL = "https://www.webtoons.com/zh-hant/romance/example/list?title_no=4601"


class FaultBody(httpx.AsyncByteStream):
    def __init__(self, fault: str) -> None:
        self.fault = fault

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.fault == "slow":
            await asyncio.sleep(10)
        if self.fault in {"read", "slow"}:
            raise httpx.ReadTimeout("fixture")
        raise httpx.LocalProtocolError("InvalidBodyLengthError")
        yield b""


class ReliableServer:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.faults: dict[str, list[str]] = {}
        self.source_fault: str | None = None
        self.growing_episode: int | None = None
        self.omit_on_refresh: int | None = None
        self.pages_per_episode = 2
        self.title_override: str | None = None
        with io.BytesIO() as output:
            Image.new("RGB", (8, 16), "white").save(output, format="JPEG")
            self.image = output.getvalue()

    @staticmethod
    def image_url(episode: int, position: int) -> str:
        return f"https://images.example.invalid/{episode}/{position}.jpg"

    async def respond(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls[url] += 1
        queue = self.faults.get(url, [])
        fault = queue.pop(0) if queue else None
        if fault in {"read", "length", "slow"}:
            return httpx.Response(200, request=request, stream=FaultBody(fault))
        if fault and fault.isdigit():
            return httpx.Response(int(fault), request=request)
        if fault in {"invalid", "empty"}:
            return httpx.Response(200, request=request, content=b"<html>error</html>" if fault == "invalid" else b"")
        if request.url.path.endswith("/episodes"):
            return self.episodes_response(request)
        if request.url.path.endswith("/list"):
            return httpx.Response(200, request=request, content=SERIES_HTML)
        if request.url.path.endswith("/viewer"):
            episode = int(request.url.params["episode_no"])
            count = self.pages_per_episode + int(self.growing_episode == episode and self.calls[url] > 1)
            html = (
                '<div class="viewer_img _img_viewer_area">'
                + "".join(f'<img data-url="{self.image_url(episode, number)}" />' for number in range(1, count + 1))
                + '<div class="author_text">Afterword</div></div>'
            )
            return httpx.Response(200, request=request, text=html)
        if request.url.host == "images.example.invalid":
            return httpx.Response(200, request=request, content=self.image)
        raise AssertionError(url)

    def episodes_response(self, request: httpx.Request) -> httpx.Response:
        last = request.url.params.get("cursor") == "2"
        payload = episode_payload("last" if last else "first")
        if self.title_override:
            for episode in payload["result"]["episodeList"]:
                if episode["episodeNo"] == 2:
                    episode["episodeTitle"] = self.title_override
        if self.omit_on_refresh and not last and self.calls[str(request.url)] > 1:
            payload["result"]["episodeList"] = [
                item for item in payload["result"]["episodeList"] if item["episodeNo"] != self.omit_on_refresh
            ]
        if self.source_fault == "missing_cursor":
            payload["result"].pop("nextCursor")
        elif self.source_fault == "duplicate" and last:
            payload["result"]["episodeList"][0]["episodeNo"] = 2
        return httpx.Response(200, request=request, json=payload)


class FixtureWebtoonClient(WebtoonHttpClient):
    def __init__(self, server: ReliableServer, options: WebtoonDownloadOptions) -> None:
        self.server = server
        super().__init__(
            retry_strategy=options.retry_strategy,
            request_retries=options.request_retries,
            retry_delay=options.retry_delay,
            retry_max_delay=options.retry_max_delay,
            max_image_bytes=options.max_image_bytes,
            request_time_budget=options.request_time_budget,
        )

    def _build_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self.server.respond)


async def run_source(server: ReliableServer, options: WebtoonDownloadOptions) -> BatchResult:
    async with FixtureWebtoonClient(server, options) as client:
        return await download_webtoon_result(options, client=client)


def download_options(destination: str, **changes: object) -> WebtoonDownloadOptions:
    original = WebtoonDownloadOptions(
        SERIES_URL,
        destination=destination,
        retry_strategy="exponential",
        retry_delay=0,
        retry_max_delay=0,
        request_retries=1,
        page_retries=1,
        chapter_retries=0,
        minimum_free_bytes=0,
        keep_workspace=True,
    )
    return replace(original, **changes)
