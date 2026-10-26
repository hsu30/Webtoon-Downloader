from __future__ import annotations

from dataclasses import replace

import pytest

from webtoon_downloader.core.webtoon.downloaders.options import WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.models import ChapterInfo
from webtoon_downloader.core.webtoon.namer import ChapterNamingMode, SeparateFileNameGenerator


def make_chapter(*, total_chapters: int = 99) -> ChapterInfo:
    return ChapterInfo(
        number=85,
        viewer_url="https://example.com/chapter/85",
        data_episode_no=85,
        title="第83話 3枚金幣(1)",
        series_title="Example",
        total_chapters=total_chapters,
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("title", "第83話 3枚金幣(1)"),
        ("number", "85"),
        ("number-title", "85-第83話 3枚金幣(1)"),
    ],
)
def test_separate_chapter_naming_modes(mode: ChapterNamingMode, expected: str) -> None:
    generator = SeparateFileNameGenerator(chapter_mode=mode)

    assert generator.get_chapter_directory(make_chapter()).name == expected


def test_number_title_keeps_existing_zero_padding() -> None:
    generator = SeparateFileNameGenerator(chapter_mode="number-title")

    assert generator.get_chapter_directory(make_chapter(total_chapters=100)).name == "085-第83話 3枚金幣(1)"


def test_legacy_title_directory_option_remains_supported() -> None:
    generator = SeparateFileNameGenerator(use_chapter_title_directories=True)

    assert generator.get_chapter_directory(make_chapter()).name == "第83話 3枚金幣(1)"


def test_invalid_chapter_naming_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported chapter naming mode"):
        SeparateFileNameGenerator(chapter_mode="invalid")  # type: ignore[arg-type]


def test_download_options_default_to_number_title() -> None:
    options = WebtoonDownloadOptions(url="https://example.com/webtoon")

    assert options.chapter_mode == "number-title"


@pytest.mark.parametrize(
    "number,total,title,expected",
    [
        (50, 79, "第49話 證明(1/2)", "50-第49話 證明(1_2)"),
        (51, 79, "第50話 證明(2/2)", "51-第50話 證明(2_2)"),
        (212, 343, "[第2部第63話] 寄生蟲\uff081/2\uff09", "212-[第2部第63話] 寄生蟲\uff081_2\uff09"),
        (213, 343, "[第2部第64話] 寄生蟲\uff082/2\uff09", "213-[第2部第64話] 寄生蟲\uff082_2\uff09"),
        (85, 99, r"Part 1/2\3", "85-Part 1_2_3"),
    ],
)
def test_slash_titles_always_produce_one_directory_component(number, total, title, expected):
    chapter = replace(make_chapter(total_chapters=total), number=number, title=title)
    path = SeparateFileNameGenerator(chapter_mode="number-title").get_chapter_directory(chapter)
    assert path.name == expected
    assert len(path.parts) == 1
