from __future__ import annotations

import logging
import random
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType

import httpx
from furl import furl

from webtoon_downloader.core.retry import ResourceLimitError, RetryController, RetryPolicy, RetryStrategy

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.1; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.91",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.91",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",
]
"""
List of random user agents
"""

MOBILE_USER_AGENTS = [
    """Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/114.0.5735.99 Mobile/15E148 Safari/604.1""",
    """Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/114.0.5735.124 Mobile/15E148 Safari/604.1""",
    """Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/114.1 Mobile/15E148 Safari/605.1.15""",
    """Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36""",
    """Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/21.0 Chrome/110.0.5481.154 Mobile Safari/537.36""",
]
"""
List of random user agents of mobiles
"""

WebtoonURL = "https://www.webtoons.com"
WebtoonMobileURL = "https://m.webtoons.com"


DEFAULT_REQUEST_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=30.0)
"""Default timeout used for regular Webtoon page and API requests."""

IMAGE_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=30.0)
"""Longer read timeout for image streams because the CDN may pause before sending body bytes."""


class WebtoonHttpClient:
    """
    An asynchronous HTTP client configured for scrapping webtoon website.

    The client uses HTTP/2, custom headers with a randomly selected user agent,
    and is configured with high limits for maximum connections and keep-alive connections.
    """

    def __init__(
        self,
        proxy: str | None = None,
        retry_strategy: RetryStrategy | None = None,
        *,
        request_retries: int = 3,
        retry_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        request_timeout: float = 30.0,
        image_read_timeout: float = 90.0,
        max_image_bytes: int = 64 * 1024 * 1024,
        request_time_budget: float = 300.0,
    ):
        self.proxy = proxy
        self.retry_strategy = retry_strategy
        self.request_retries = request_retries
        self.max_image_bytes = max_image_bytes
        self.image_timeout = httpx.Timeout(connect=15.0, read=image_read_timeout, write=30.0, pool=30.0)
        self.retries = RetryController(RetryPolicy(retry_strategy, retry_delay, retry_max_delay, request_time_budget))
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=200),
            http2=True,
            headers=self._generate_headers(),
            follow_redirects=True,
            trust_env=False,
            timeout=httpx.Timeout(connect=15.0, read=request_timeout, write=30.0, pool=30.0),
            transport=self._build_transport(),
        )

    def _build_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.AsyncHTTPTransport(http2=True, proxy=self.proxy)

    async def __aenter__(self) -> WebtoonHttpClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, traceback)

    async def get(self, url: str) -> httpx.Response:
        async def operation() -> httpx.Response:
            headers = (
                {**self._client.headers, "user-agent": self._get_mobile_ua()}
                if WebtoonMobileURL in url.lower()
                else None
            )
            response = await self._client.get(url, headers=headers)
            response.raise_for_status()
            return response

        return await self.retries.run(operation, self.request_retries)

    @asynccontextmanager
    async def stream(self, method: str, url: str) -> AsyncGenerator[httpx.Response]:
        async with self._client.stream(method, url) as response:
            response.raise_for_status()

            yield response

    @asynccontextmanager
    async def stream_image(self, url: str, quality: int | None = 100) -> AsyncGenerator[httpx.Response]:
        quality = quality or 100
        if quality <= 0 or quality > 100:
            raise ValueError("Quality must be between 1 and 100")  # noqa: TRY003

        f_url = furl(url)
        current_type = f_url.args.get("type")
        # Only act if it's a known quality type (qNN, qNNN, etc.)
        if current_type and re.fullmatch(r"q\d{1,3}", current_type):
            if quality == 100:
                f_url.remove(args=["type"])
            else:
                f_url.set(args={"type": f"q{quality}"})
        elif not current_type and quality < 100:
            # Add type if missing and quality < 100
            f_url.set(args={"type": f"q{quality}"})

        url = f_url.url
        async with self._client.stream(
            "GET",
            url,
            timeout=self.image_timeout,
            headers={
                "referer": WebtoonURL,
                **self._generate_headers(),
            },
        ) as response:
            response.raise_for_status()

            yield response

    async def image_chunks(self, response: httpx.Response) -> AsyncGenerator[bytes]:
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self.max_image_bytes:
                raise ResourceLimitError("image_byte_limit")
            yield chunk
        length = response.headers.get("content-length")
        if length and response.headers.get("content-encoding", "identity") == "identity" and received != int(length):
            raise httpx.LocalProtocolError("InvalidBodyLengthError")

    def _get_mobile_ua(self) -> str:
        """Returns a randomly chosen user agent for mobile devices"""
        return random.choice(MOBILE_USER_AGENTS)

    def _generate_headers(self) -> dict[str, str]:
        """
        Generates HTTP headers for the webtoon client, including a randomly chosen user agent.
        """
        return {
            "accept-language": "en-US,en;q=0.9",
            "dnt": "1",
            "user-agent": random.choice(USER_AGENTS),
        }
