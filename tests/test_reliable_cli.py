from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from tests.reliable_server import SERIES_URL, ReliableServer, run_source
from webtoon_downloader.cmd.cli import cli
from webtoon_downloader.core.webtoon.downloaders import comic
from webtoon_downloader.core.webtoon.downloaders.options import WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult


@pytest.mark.parametrize("failure", [False, True])
def test_cli_persists_real_outcomes_and_returns_nonzero_for_missing_pages(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    root = Path(__file__).resolve().parents[1] / "test_folder/reliability-cli" / uuid.uuid4().hex
    root.mkdir(parents=True)
    report = root / "result.json"
    server = ReliableServer()
    if failure:
        server.faults[server.image_url(2, 2)] = ["404"]

    async def download(options: WebtoonDownloadOptions) -> BatchResult:
        return await run_source(server, options)

    monkeypatch.setattr(comic, "download_webtoon_result", download)
    monkeypatch.setattr("webtoon_downloader.logger.setup", lambda **_: (logging.getLogger("cli-test"), Console()))
    result = CliRunner().invoke(
        cli,
        [
            SERIES_URL,
            "--out",
            str(root),
            "--separate",
            "--result-file",
            str(report),
            "--retry-delay",
            "0",
            "--retry-max-delay",
            "0",
            "--minimum-free-bytes",
            "0",
        ],
    )

    assert result.exit_code == (1 if failure else 0), result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["exit_code"] == result.exit_code
    assert payload["incomplete_episode_ids"] == ([2] if failure else [])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--page-retries", "-1"],
        ["--start", "0"],
        ["--request-time-budget", "0"],
        ["--retry-delay", "nan"],
        ["--chapter-time-budget", "inf"],
    ],
)
def test_invalid_reliability_parameters_exit_as_usage_errors(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("webtoon_downloader.logger.setup", lambda **_: (logging.getLogger("cli-test"), Console()))
    result = CliRunner().invoke(cli, [SERIES_URL, *arguments])
    assert result.exit_code == 2


def test_sigterm_returns_143_and_saves_the_same_code_in_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1] / "test_folder/reliability-cli" / uuid.uuid4().hex
    root.mkdir(parents=True)
    report = root / "interrupted.json"
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["slow"]

    async def download(options: WebtoonDownloadOptions) -> BatchResult:
        async def interrupt() -> None:
            while not server.calls[server.image_url(2, 2)]:
                await asyncio.sleep(0.01)
            signal.raise_signal(signal.SIGTERM)

        interrupter = asyncio.create_task(interrupt())
        try:
            return await run_source(server, options)
        finally:
            interrupter.cancel()
            await asyncio.gather(interrupter, return_exceptions=True)

    old_handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(comic, "download_webtoon_result", download)
    monkeypatch.setattr("webtoon_downloader.logger.setup", lambda **_: (logging.getLogger("cli-test"), Console()))
    result = CliRunner().invoke(
        cli,
        [
            SERIES_URL,
            "--out",
            str(root),
            "--separate",
            "--result-file",
            str(report),
            "--retry-delay",
            "0",
            "--retry-max-delay",
            "0",
            "--minimum-free-bytes",
            "0",
        ],
    )

    assert result.exit_code == 143, result.output
    assert json.loads(report.read_text(encoding="utf-8"))["exit_code"] == 143
    assert signal.getsignal(signal.SIGTERM) == old_handler
