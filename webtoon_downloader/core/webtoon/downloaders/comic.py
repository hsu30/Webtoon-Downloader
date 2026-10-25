from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

from furl import furl

from webtoon_downloader.core import file as fileutil
from webtoon_downloader.core.exceptions import NoChaptersFoundError, WebtoonDownloadError
from webtoon_downloader.core.webtoon.client import WebtoonHttpClient
from webtoon_downloader.core.webtoon.comicinfo import SeriesMetadata
from webtoon_downloader.core.webtoon.downloaders.callbacks import OnWebtoonFetchCallback
from webtoon_downloader.core.webtoon.downloaders.chapter import ChapterDownloader
from webtoon_downloader.core.webtoon.downloaders.options import StorageType, WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult, DownloadResult
from webtoon_downloader.core.webtoon.exporter import DataExporter
from webtoon_downloader.core.webtoon.extractor import ElementNotFoundError, WebtoonMainPageExtractor
from webtoon_downloader.core.webtoon.fetchers import EndChapter, WebtoonFetcher
from webtoon_downloader.core.webtoon.models import ChapterInfo
from webtoon_downloader.storage import AioFolderWriter, AioPdfWriter, AioWriter, AioZipWriter

log = logging.getLogger(__name__)


@dataclass
class WebtoonDownloader:
    """
    Facilitates the downloading of Webtoon chapters.

    Manages the entire process of downloading multiple chapters from a Webtoon series, including fetching chapter details, setting up storage, and handling concurrency.

    Attributes:
        url                     : URL of the Webtoon series to download.
        chapter_downloader      : The downloader responsible for individual chapters.
        storage_type            : The type of storage to use for the downloaded chapters.
        quality                 : The quality of the image to download.
        start_chapter           : The first chapter to download.
        end_chapter             : The last chapter to download.
        directory               : The directory where the downloaded chapters will be stored.
        exporter                : Optional data exporter for exporting series details.
        on_webtoon_fetched      : Optional callback executed after fetching Webtoon information.
        proxy                   : Optional proxy address for making requests.
    """

    url: str
    client: WebtoonHttpClient
    chapter_downloader: ChapterDownloader
    storage_type: StorageType
    quality: int

    start_chapter: int | None = None
    end_chapter: EndChapter = None
    directory: str | PathLike[str] | None = None
    exporter: DataExporter | None = None
    on_webtoon_fetched: OnWebtoonFetchCallback | None = None
    proxy: str | None = None

    _directory: Path = field(init=False)

    def __post_init__(self) -> None:
        # sanitize and check the url is valid
        self.url = re.sub(r"\\(?=[?=&])", "", self.url)
        url = furl(self.url)

        # if the url has no scheme, attempt to add one
        if not url.scheme:
            self.url = f"https://{self.url}"
            url = furl(self.url)

        if not url.scheme or not url.host:
            raise WebtoonDownloadError(self.url, cause=ValueError("Invalid URL"))

    async def run(self) -> list[DownloadResult]:
        """
        Asynchronously downloads chapters from a Webtoon series.

        Orchestrates the downloading process by initializing the client, fetching chapter details, setting up storage, and managing concurrent downloads.

        Returns:
            A list containing download results for each chapter.
        """
        chapter_list = await self._get_chapters()
        resp = await self.client.get(self.url)
        extractor = WebtoonMainPageExtractor(resp.text)
        series_title = self._resolve_series_title(extractor, chapter_list)

        if self.directory:
            self._directory = Path(self.directory)
        else:
            self._directory = Path(fileutil.slugify_name(series_title))

        await self._export_data(extractor)
        series_metadata: SeriesMetadata | None = None
        if self.storage_type == "cbz":
            series_metadata = self._build_series_metadata(extractor, chapter_list)

        tasks = []
        for chapter_info in chapter_list:
            task = self._create_task(chapter_info, self.quality, series_metadata=series_metadata)
            tasks.append(task)

        try:
            results = await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.exporter:
            await self.exporter.write_data(self._directory)

        return results

    async def _get_chapters(self) -> list[ChapterInfo]:
        """
        Fetches chapter information for the specified Webtoon series.

        Returns:
            A list of `ChapterInfo` objects containing information about each chapter.
        """
        fetcher = WebtoonFetcher(self.client, self.url)
        chapters = await fetcher.get_chapters_details(self.url, self.start_chapter, self.end_chapter)
        if not chapters:
            raise NoChaptersFoundError

        if self.on_webtoon_fetched:
            await self.on_webtoon_fetched(chapters)

        return chapters

    def _create_task(
        self,
        chapter_info: ChapterInfo,
        quality: int = 100,
        series_metadata: SeriesMetadata | None = None,
    ) -> asyncio.Task:
        """
        Creates an asynchronous task for downloading a Webtoon chapter.

        Args:
            chapter_info    : Information about the specific chapter to download.
            quality         : The quality of the image to download.

        Returns:
            An asyncio Task object for downloading the chapter.
        """

        async def task() -> list[DownloadResult]:
            storage = await self._get_storage(chapter_info)
            return await self.chapter_downloader.run(
                chapter_info,
                self._directory,
                storage,
                quality,
                series_metadata=series_metadata,
            )

        return asyncio.create_task(task())

    def _resolve_series_title(self, extractor: WebtoonMainPageExtractor, chapter_list: list[ChapterInfo]) -> str:
        try:
            title = extractor.series_title.strip()
            if title:
                return title
        except ElementNotFoundError:
            log.debug("Series title not found in main page extractor")

        if chapter_list and chapter_list[0].series_title.strip():
            return chapter_list[0].series_title.strip()

        raise WebtoonDownloadError(self.url, cause=ValueError("Unable to determine series title"))

    def _extract_language_from_url(self) -> str | None:
        segments = furl(self.url).path.segments
        if not segments:
            return None

        first_segment = segments[0]
        if isinstance(first_segment, str) and len(first_segment) == 2 and first_segment.isalpha():
            return first_segment.lower()
        return None

    def _build_series_metadata(
        self, extractor: WebtoonMainPageExtractor, chapter_list: list[ChapterInfo]
    ) -> SeriesMetadata:
        title = self._resolve_series_title(extractor, chapter_list)

        try:
            summary = extractor.series_summary
        except ElementNotFoundError:
            summary = None
            log.debug("ComicInfo: summary not found")

        return SeriesMetadata(
            title=title,
            summary=summary,
            author=extractor.author,
            genre=extractor.genre,
            language=self._extract_language_from_url(),
            url=self.url,
        )

    async def _get_storage(self, chapter_info: ChapterInfo) -> AioWriter:
        """
        Determines the appropriate storage writer based on the specified storage type.

        Args:
            chapter_info: Information about the chapter for which storage is being set up.

        Returns:
            An instance of a storage writer (`AioWriter` subclass) appropriate for the storage type.
        """
        dest = f"{chapter_info.number:0{len(str(chapter_info.total_chapters))}d}"
        if self.storage_type in ["zip", "cbz"]:
            return AioZipWriter(self._directory / f"{dest}.{self.storage_type}")
        elif self.storage_type == "pdf":
            return AioPdfWriter(self._directory / f"{dest}.pdf")
        else:
            return AioFolderWriter(self._directory)

    async def _export_data(self, extractor: WebtoonMainPageExtractor) -> None:
        """
        Exports series summary data if an exporter is set.

        Args:
            extractor: The extractor used to obtain the series summary.
        """
        if not self.exporter:
            return
        await self.exporter.add_series_summary(extractor.series_summary, self._directory / "summary.txt")


async def download_webtoon_result(
    opts: WebtoonDownloadOptions,
    *,
    client: WebtoonHttpClient | None = None,
) -> BatchResult:
    """Download the selected scope and return its verified, structured outcome."""
    from webtoon_downloader.core.webtoon.downloaders.reliable import ReliableDownloader

    if client is not None:
        return await ReliableDownloader(opts, client).run()
    async with WebtoonHttpClient(
        proxy=opts.proxy,
        retry_strategy=opts.retry_strategy,
        request_retries=opts.request_retries,
        retry_delay=opts.retry_delay,
        retry_max_delay=opts.retry_max_delay,
        request_timeout=opts.request_timeout,
        image_read_timeout=opts.image_read_timeout,
        max_image_bytes=opts.max_image_bytes,
        request_time_budget=opts.request_time_budget,
    ) as owned_client:
        return await ReliableDownloader(opts, owned_client).run()


async def download_webtoon(opts: WebtoonDownloadOptions) -> list[DownloadResult]:
    """Compatibility entrypoint: return artifact paths or raise on incomplete delivery."""
    result = await download_webtoon_result(opts)
    if not result.is_complete:
        raise WebtoonDownloadError(opts.url, message=f"Download {result.status}; report: {result.report_path}")
    return [chapter.artifact for chapter in result.chapters if chapter.artifact is not None]
