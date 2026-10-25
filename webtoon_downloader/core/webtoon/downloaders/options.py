from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from webtoon_downloader.core.webtoon.client import RetryStrategy
from webtoon_downloader.core.webtoon.downloaders.callbacks import ChapterProgressCallback, OnWebtoonFetchCallback
from webtoon_downloader.core.webtoon.exporter import DataExporterFormat
from webtoon_downloader.core.webtoon.namer import ChapterNamingMode
from webtoon_downloader.transformers.image import ImageFormat

StorageType: TypeAlias = Literal["images", "zip", "cbz", "pdf"]
"""Valid option for storing the downloaded images."""


DEFAULT_CONCURRENT_IMAGE_DOWNLOADS = 8
"""Default number of asynchronous image download workers. More == More likely to get server rate limited"""

DEFAULT_CONCURRENT_CHAPTER_DOWNLOADS = 2
"""Default number of asynchronous chapter download workers. This does not affect rate limiting or download speed since that is all controlled by the number of concurrent image downloads."""


@dataclass
class WebtoonDownloadOptions:
    """
    Options for downloading chapters from a Webtoon.

    Attributes:
        url                       : Webtoon URL
        start                     : Starting chapter number. If None, the download starts from the first chapter.
        end                       : Ending chapter number. If None, the download goes until the last chapter.
        latest                    : Flag to download only the latest chapter. Ignores values set by start and end.
        destination               : The directory where the chapters will be downloaded.
        export_metadata           : Flag to export webtoon and chapter metadata.
        exporter_forma            : Format for exporting metadata.
        separate                  : Flag to store each chapter in separate directories.
        chapter_mode               : Naming mode for separate chapter directories.
        save_as                   : Format to save chapters.
        image_format              : Format to save chapter images.
        chapter_progress_callback : Callback function for chapter download progress.
        on_webtoon_fetched        : function invoked after fetching Webtoon information.
        concurrent_chapters       : The number of chapters to download concurrently.
        concurrent_pages          : The number of images to download concurrently.
        retry_strategy            : The strategy to use for retrying failed downloads.
        proxy                     : proxy address to use for making requests.
        quality                   : The quality of the image to download
    """

    url: str

    start: int | None = None
    end: int | None = None
    latest: bool = False
    destination: str | None = None

    quality: int = 100

    export_metadata: bool = False
    exporter_format: DataExporterFormat = "json"

    separate: bool = True
    chapter_mode: ChapterNamingMode = "number-title"
    save_as: StorageType = "images"
    image_format: ImageFormat = "JPG"

    chapter_progress_callback: ChapterProgressCallback | None = None
    on_webtoon_fetched: OnWebtoonFetchCallback | None = None

    concurrent_chapters: int = DEFAULT_CONCURRENT_CHAPTER_DOWNLOADS
    concurrent_pages: int = DEFAULT_CONCURRENT_IMAGE_DOWNLOADS

    retry_strategy: RetryStrategy | None = "exponential"
    proxy: str | None = None

    request_retries: int = 3
    page_retries: int = 3
    chapter_retries: int = 1
    retry_delay: float = 1.0
    retry_max_delay: float = 30.0
    request_timeout: float = 30.0
    request_time_budget: float = 300.0
    image_read_timeout: float = 90.0
    chapter_time_budget: float = 900.0
    resume: bool = False
    verify: Literal["full", "quick"] = "full"
    fail_fast: bool = False
    result_file: str | None = None
    keep_workspace: bool = False
    retry_failed: str | None = None
    max_image_bytes: int = 64 * 1024 * 1024
    max_image_pixels: int = 100_000_000
    minimum_free_bytes: int = 64 * 1024 * 1024
    interruption_code: Literal[130, 143] = field(default=130, init=False)

    def __post_init__(self) -> None:
        counts = (self.request_retries, self.page_retries, self.chapter_retries, self.minimum_free_bytes)
        limits = (
            self.concurrent_chapters,
            self.concurrent_pages,
            self.request_timeout,
            self.request_time_budget,
            self.image_read_timeout,
            self.chapter_time_budget,
            self.max_image_bytes,
            self.max_image_pixels,
        )
        if any(value < 0 for value in counts) or any(value <= 0 for value in limits):
            raise ValueError("invalid_download_limits")
        if any(not math.isfinite(value) for value in (*counts, *limits, self.retry_delay, self.retry_max_delay)):
            raise ValueError("nonfinite_download_limits")
        if self.retry_delay < 0 or self.retry_max_delay < self.retry_delay:
            raise ValueError("invalid_retry_delays")
        if self.verify not in {"full", "quick"}:
            raise ValueError("invalid_verification_mode")
        if self.latest and (self.start is not None or self.end is not None):
            raise ValueError("latest_conflicts_with_range")
        if self.retry_failed and (self.start is not None or self.end is not None or self.latest):
            raise ValueError("retry_failed_conflicts_with_range")
