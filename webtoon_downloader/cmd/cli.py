from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from typing import Any, Literal

import rich_click as click

import webtoon_downloader.cmd.exceptions
import webtoon_downloader.logger
from webtoon_downloader.cmd.exceptions import (
    CLIInvalidConcurrentCountError,
    CLIInvalidQualityError,
    CLIInvalidStartAndEndRangeError,
    CLILatestWithStartOrEndError,
    CLISeparateOptionWithNonImageSaveAsError,
)
from webtoon_downloader.cmd.progress import ChapterProgressManager, init_progress
from webtoon_downloader.core.exceptions import DownloadError
from webtoon_downloader.core.webtoon.client import RetryStrategy
from webtoon_downloader.core.webtoon.downloaders import comic
from webtoon_downloader.core.webtoon.downloaders.options import (
    DEFAULT_CONCURRENT_CHAPTER_DOWNLOADS,
    DEFAULT_CONCURRENT_IMAGE_DOWNLOADS,
    StorageType,
    WebtoonDownloadOptions,
)
from webtoon_downloader.core.webtoon.exporter import DataExporterFormat
from webtoon_downloader.core.webtoon.namer import ChapterNamingMode
from webtoon_downloader.transformers.image import ImageFormat

help_config = click.RichHelpConfiguration(
    show_metavars_column=False,
    append_metavars_help=True,
    style_errors_suggestion="magenta italic",
    errors_suggestion="Try running '--help' for more information.",
)


class GracefulExit(SystemExit):
    code = 1


def validate_concurrent_count(ctx: Any, param: Any, value: int | None) -> int | None:  # pylint: disable=unused-argument
    if value is not None and value <= 0:
        raise CLIInvalidConcurrentCountError(value)

    return value


def validate_quality(ctx: Any, param: Any, value: int) -> int:
    if value < 40 or value > 100 or value % 10 != 0:
        raise CLIInvalidQualityError(value)

    return value


def normalize_chapter_naming(_ctx: Any, _param: Any, value: str) -> ChapterNamingMode:
    """Normalize chapter naming aliases to their canonical values."""
    normalized = value.lower()
    if normalized in {"t", "title"}:
        return "title"
    if normalized in {"n", "number"}:
        return "number"
    # Click validates the accepted values before invoking this callback.
    return "number-title"


def _unwrap_error_chain(exc: Exception) -> list[str]:
    """Return the exception type chain for compact user-facing error summaries."""
    chain = [exc.__class__.__name__]
    current: Exception | None = exc
    while isinstance(current, DownloadError) and current.cause is not None:
        current = current.cause
        chain.append(current.__class__.__name__)
    return chain


def reliability_options(command: Callable[..., Any]) -> Callable[..., Any]:
    for name, default in (("request-retries", 3), ("page-retries", 3), ("chapter-retries", 1)):
        command = click.option(
            f"--{name}",
            type=click.IntRange(min=0),
            default=default,
            show_default=True,
            help=f"Extra {name.removesuffix('-retries')} attempts after the initial attempt.",
        )(command)
    for name, float_default in (("retry-delay", 1.0), ("retry-max-delay", 30.0)):
        command = click.option(
            f"--{name}",
            type=click.FloatRange(min=0),
            default=float_default,
            show_default=True,
            help="Retry backoff duration in seconds; server Retry-After is respected.",
        )(command)
    for name, float_default in (
        ("request-timeout", 30.0),
        ("request-time-budget", 300.0),
        ("image-read-timeout", 90.0),
        ("chapter-time-budget", 900.0),
    ):
        command = click.option(
            f"--{name}",
            type=click.FloatRange(min=0, min_open=True),
            default=float_default,
            show_default=True,
            help=f"{name.replace('-', ' ').capitalize()} in seconds.",
        )(command)
    for name in ("resume", "fail-fast"):
        command = click.option(
            f"--{name}",
            is_flag=True,
            help="Reuse validated staged pages and repair managed output with backups."
            if name == "resume"
            else "Cancel remaining work after the first failed chapter.",
        )(command)
    command = click.option(
        "--verify",
        type=click.Choice(["full", "quick"]),
        default="full",
        show_default=True,
        help="Full decodes and hashes; quick trusts unchanged recorded size and timestamps.",
    )(command)
    command = click.option(
        "--result-file", type=click.Path(dir_okay=False, resolve_path=True), help="JSON result report path."
    )(command)
    command = click.option(
        "--retry-failed",
        type=click.Path(exists=True, dir_okay=False, resolve_path=True),
        help="Recover a failed JSON report, rechecking its original chapter scope.",
    )(command)
    command = click.option(
        "--max-image-bytes",
        type=click.IntRange(min=1),
        default=64 * 1024 * 1024,
        help="Maximum received bytes per image.",
    )(command)
    command = click.option(
        "--max-image-pixels", type=click.IntRange(min=1), default=100_000_000, help="Maximum decoded pixels per image."
    )(command)
    return click.option(
        "--minimum-free-bytes",
        type=click.IntRange(min=0),
        default=64 * 1024 * 1024,
        help="Minimum free workspace bytes before fetching a page.",
    )(command)


@click.command()
@click.version_option(package_name="webtoon_downloader")
@click.pass_context
@click.rich_config(help_config=help_config)
@click.argument("url", required=True, type=str)
@reliability_options
@click.option(
    "--start",
    "-s",
    type=click.IntRange(min=1),
    help="Start chapter",
)
@click.option("--end", "-e", type=click.IntRange(min=1), help="End chapter")
@click.option(
    "--latest",
    "-l",
    is_flag=True,
    help="Download only the latest chapter",
)
@click.option(
    "--export-metadata",
    "-em",
    is_flag=True,
    help="Export texts like series summary, chapter name, or author notes into additional files",
)
@click.option(
    "--export-format",
    "-ef",
    type=click.Choice(["all", "json", "text"]),
    default="json",
    show_default=True,
    help="Format to store exported texts in",
)
@click.option(
    "--image-format",
    "-f",
    type=click.Choice(["jpg", "png", "webp"]),
    default="jpg",
    show_default=True,
    help="Image format of downloaded images",
)
@click.option(
    "--out",
    "-o",
    type=click.Path(file_okay=False, writable=True, resolve_path=True),
    help="Download parent folder path",
)
@click.option(
    "--save-as",
    "-sa",
    type=click.Choice(["images", "zip", "cbz", "pdf"]),
    default="images",
    show_default=True,
    help="Choose the format to save each downloaded chapter",
)
@click.option(
    "--separate",
    is_flag=True,
    help="Download each chapter in separate folders",
)
@click.option(
    "--chapter-naming",
    "-N",
    type=click.Choice(["title", "number", "number-title", "t", "n", "nt"], case_sensitive=False),
    default="number-title",
    show_default=True,
    callback=normalize_chapter_naming,
    help="Naming format for chapter directories when using --separate",
)
@click.option(
    "--dest",
    callback=webtoon_downloader.cmd.exceptions.handle_deprecated_options,
    type=str,
    expose_value=False,
    hidden=True,
    help="[Deprecated] Use --out instead",
)
@click.option(
    "--export-texts",
    callback=webtoon_downloader.cmd.exceptions.handle_deprecated_options,
    is_flag=True,
    expose_value=False,
    hidden=True,
    help="[Deprecated] Use --export-metadata instead",
)
@click.option(
    "--concurrent-chapters",
    type=int,
    default=DEFAULT_CONCURRENT_CHAPTER_DOWNLOADS,
    callback=validate_concurrent_count,
    help="Number of workers for concurrent chapter downloads",
)
@click.option(
    "--concurrent-pages",
    type=int,
    default=DEFAULT_CONCURRENT_IMAGE_DOWNLOADS,
    callback=validate_concurrent_count,
    help="Number of workers for concurrent image downloads. This value is shared between all concurrent chapter downloads.",
)
@click.option(
    "--proxy",
    type=str,
    help="proxy address to use for making requests. e.g. http://127.0.0.1:7890",
)
@click.option(
    "--retry-strategy",
    type=click.Choice(["exponential", "linear", "fixed", "none"]),
    default="exponential",
    show_default=True,
    help="Retry strategy for failed requests",
)
@click.option(
    "--quality",
    type=int,
    default=100,
    show_default=True,
    callback=validate_quality,
    help="Image quality (must be between 40 and 100, divisible by 10)",
)
@click.option("--debug", type=bool, is_flag=True, help="Enable debug mode")
def cli(  # noqa: C901
    ctx: click.Context,
    url: str,
    start: int,
    end: int,
    latest: bool,
    out: str,
    image_format: ImageFormat,
    separate: bool,
    chapter_naming: ChapterNamingMode,
    export_metadata: bool,
    export_format: DataExporterFormat,
    save_as: StorageType,
    concurrent_chapters: int,
    concurrent_pages: int,
    proxy: str,
    retry_strategy: RetryStrategy | Literal["none"] | None,
    quality: int,
    debug: bool,
    **reliability: Any,
) -> None:
    log, console = webtoon_downloader.logger.setup(
        log_filename="webtoon_downloader.log" if debug else None,
        enable_traceback=debug,
        enable_console_logging=debug,
    )

    if not url:
        console.print(
            '[red]A Webtoon URL of the form [green]"https://www.webtoons.com/.../list?title_no=??"[/] of is required.'
        )
        ctx.exit(1)
    if latest and (start or end):
        raise CLILatestWithStartOrEndError(ctx)
    if separate and (save_as != "images"):
        raise CLISeparateOptionWithNonImageSaveAsError(ctx)
    if start is not None and end is not None and start > end:
        raise CLIInvalidStartAndEndRangeError(ctx)

    progress = init_progress(console)
    series_download_task = progress.add_task(
        "Downloading Chapters...",
        type="Chapters",
        type_color="grey93",
        number_format=">02d",
        rendered_completed="00",
        rendered_total="??",
    )

    progress_manager = ChapterProgressManager(progress, series_download_task)
    try:
        opts = WebtoonDownloadOptions(
            url=url,
            start=start,
            end=end,
            latest=latest,
            destination=out,
            export_metadata=export_metadata,
            exporter_format=export_format,
            separate=separate,
            chapter_mode=chapter_naming,
            image_format=image_format,
            save_as=save_as,
            chapter_progress_callback=progress_manager.advance_progress,
            on_webtoon_fetched=progress_manager.on_webtoon_fetched,
            concurrent_chapters=concurrent_chapters,
            concurrent_pages=concurrent_pages,
            retry_strategy=retry_strategy if retry_strategy != "none" else None,
            quality=quality,
            proxy=proxy,
            **reliability,
        )
    except ValueError as error:
        raise click.BadParameter(str(error)) from error

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(comic.download_webtoon_result(opts))
    interrupted = 0

    def stop(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        if interrupted == 0:
            interrupted = 130 if signum == signal.SIGINT else 143
            opts.interruption_code = 130 if signum == signal.SIGINT else 143
            task.cancel()

    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    for number in previous:
        signal.signal(number, stop)
    code = 1
    try:
        with progress:
            result = loop.run_until_complete(task)
            code = interrupted or result.exit_code
            console.print(f"Download {result.status}; verification={result.verification}")
            if result.report_path:
                console.print(f"Result report: {result.report_path}")
            if result.incomplete_episode_ids:
                console.print(f"Incomplete episode IDs: {list(result.incomplete_episode_ids)}")
            if result.error:
                console.print(f"[red]{result.error}[/]")
            for chapter in result.chapters:
                if chapter.error:
                    console.print(f"[red]Episode {chapter.episode_no}: {chapter.error}[/]")
    except asyncio.CancelledError:
        code = interrupted or 130
    except Exception as error:
        console.print(f"[red]Download failed: {type(error).__name__}[/]")
        log.exception("Download failed")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        pending = asyncio.all_tasks(loop)
        for remaining in pending:
            remaining.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        webtoon_downloader.logger.shutdown()
        asyncio.set_event_loop(None)
        loop.close()
    ctx.exit(code)


def run() -> None:
    """CLI entrypoint"""
    if len(sys.argv) <= 1:
        sys.argv.append("--help")

    cli()  # pylint: disable=no-value-for-parameter
