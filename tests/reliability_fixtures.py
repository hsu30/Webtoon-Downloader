"""Synthetic API responses and expected results for self-contained offline tests."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from webtoon_downloader.core.webtoon.downloaders.result import ChapterResult, PageExpectation, PageResult
from webtoon_downloader.core.webtoon.inventory import EpisodeInventory, EpisodePageEvidence, SeriesIdentity
from webtoon_downloader.core.webtoon.models import ChapterInfo

_EPISODE_PAYLOADS: dict[str, dict[str, Any]] = {
    "first": {
        "success": True,
        "result": {
            "episodeList": [
                {
                    "episodeNo": 1,
                    "thumbnail": "",
                    "episodeTitle": "First",
                    "viewerLink": "/zh-hant/romance/example/first/viewer?title_no=4601&episode_no=1",
                    "exposureDateMillis": 0,
                    "displayUp": True,
                    "hasBgm": False,
                },
                {
                    "episodeNo": 2,
                    "thumbnail": "",
                    "episodeTitle": "Second",
                    "viewerLink": "/zh-hant/romance/example/second/viewer?title_no=4601&episode_no=2",
                    "exposureDateMillis": 0,
                    "displayUp": True,
                    "hasBgm": False,
                },
            ],
            "nextCursor": 2,
        },
    },
    "last": {
        "success": True,
        "result": {
            "episodeList": [
                {
                    "episodeNo": 5,
                    "thumbnail": "",
                    "episodeTitle": "Third available episode",
                    "viewerLink": "/zh-hant/romance/example/third/viewer?title_no=4601&episode_no=5",
                    "exposureDateMillis": 0,
                    "displayUp": True,
                    "hasBgm": False,
                }
            ],
            "nextCursor": 0,
        },
    },
}

SERIES_HTML = b'<!doctype html>\n<html lang="zh-Hant">\n  <head>\n    <link rel="canonical" href="https://www.webtoons.com/zh-hant/romance/example/list?title_no=4601" />\n  </head>\n  <body>\n    <strong class="subject">Example</strong>\n  </body>\n</html>\n'

VIEWER_HTML = b'<!doctype html>\n<html lang="zh-Hant">\n  <body>\n    <div class="viewer_img _img_viewer_area">\n      <img data-url="https://images.example.invalid/first.jpg" />\n      <img data-url="https://images.example.invalid/second.jpg" />\n    </div>\n  </body>\n</html>\n'


def episode_payload(name: str) -> dict[str, Any]:
    return deepcopy(_EPISODE_PAYLOADS[name])


def source_inventory() -> EpisodeInventory:
    first = episode_payload("first")
    last = episode_payload("last")
    items = first["result"]["episodeList"] + last["result"]["episodeList"]
    chapters = tuple(
        ChapterInfo(
            number=number,
            viewer_url=f"https://www.webtoons.com{item['viewerLink']}",
            data_episode_no=item["episodeNo"],
            title=item["episodeTitle"],
            series_title="Example",
            total_chapters=len(items),
        )
        for number, item in enumerate(items, start=1)
    )
    return EpisodeInventory(
        series=SeriesIdentity(language="zh-hant", series_type="webtoon", title_no=4601),
        chapters=chapters,
        evidence=(
            EpisodePageEvidence.from_payload(first),
            EpisodePageEvidence.from_payload(last, requested_cursor=2),
        ),
    )


def completed_chapter(episode_no: int) -> ChapterResult:
    expected = tuple(
        PageExpectation(number, f"https://images.example.invalid/{number}.jpg", f"{number}.jpg") for number in (1, 2)
    )
    pages = tuple(
        PageResult(page, size=32, sha256=hashlib.sha256(str(page).encode()).hexdigest(), content_verified=True)
        for page in expected
    )
    return ChapterResult(
        episode_no=episode_no,
        expected_pages=expected,
        series=source_inventory().series,
        pages=pages,
        artifact=Path(f"verified-{episode_no}"),
        artifact_verified=True,
        published=True,
    )
