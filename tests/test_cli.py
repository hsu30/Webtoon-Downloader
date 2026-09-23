from __future__ import annotations

import importlib.metadata

from click.testing import CliRunner

from webtoon_downloader.cmd.cli import cli, normalize_chapter_naming


# This test ensures that the version displayed by the CLI matches the version specified in the package metadata.
## See https://github.com/Zehina/Webtoon-Downloader/issues/108
def test_cli_version_matches_package_metadata() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0

    expected_version = importlib.metadata.version("webtoon_downloader")
    assert f"version {expected_version}" in result.output


def test_cli_help_lists_number_title_chapter_naming() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "--chapter-naming" in result.output
    assert "number-title" in result.output


def test_chapter_naming_aliases_are_normalized() -> None:
    assert normalize_chapter_naming(None, None, "t") == "title"
    assert normalize_chapter_naming(None, None, "n") == "number"
    assert normalize_chapter_naming(None, None, "nt") == "number-title"
