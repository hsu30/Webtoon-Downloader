from __future__ import annotations

from dataclasses import replace

import pytest

from tests.reliability_fixtures import completed_chapter, source_inventory
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult, ChapterResult


def test_completed_batch_requires_all_selected_chapters_and_a_saved_report() -> None:
    batch = BatchResult(
        scope=source_inventory().select(),
        chapters=tuple(completed_chapter(number) for number in (1, 2, 5)),
        report_written=True,
    )

    assert batch.is_complete
    assert batch.status == "complete"
    assert batch.exit_code == 0
    assert batch.incomplete_episode_ids == ()
    assert not replace(batch, report_written=False).is_complete
    assert replace(batch, error="report write failed").exit_code == 1


def test_completed_first_and_last_chapters_cannot_hide_a_missing_middle_chapter() -> None:
    batch = BatchResult(
        scope=source_inventory().select(),
        chapters=(completed_chapter(1), completed_chapter(5)),
        report_written=True,
    )

    assert not batch.is_complete
    assert batch.status == "partial"
    assert batch.exit_code == 1
    assert batch.incomplete_episode_ids == (2,)


@pytest.mark.parametrize("ids", [(1, 1, 5), (1, 2, 999), (1, 2, 5, 999)])
def test_duplicate_or_extra_chapters_cannot_satisfy_the_selected_scope(ids: tuple[int, ...]) -> None:
    batch = BatchResult(source_inventory().select(), tuple(completed_chapter(number) for number in ids), True)

    assert not batch.is_complete
    assert batch.exit_code == 1


def test_source_verification_is_required_even_when_all_parsed_chapters_are_complete() -> None:
    scope = source_inventory().select()
    unverified = replace(scope, inventory=replace(scope.inventory, evidence=()))
    batch = BatchResult(unverified, tuple(completed_chapter(number) for number in (1, 2, 5)), True)

    assert not batch.is_complete
    assert batch.exit_code == 1


def test_source_fetch_failure_cannot_be_a_successful_empty_batch() -> None:
    batch = BatchResult(scope=None, error="source request failed", report_written=True)

    assert batch.status == "failed"
    assert batch.exit_code == 1


def test_same_episode_number_from_another_series_does_not_fill_a_missing_chapter() -> None:
    wrong_chapter = completed_chapter(2)
    wrong_chapter = replace(wrong_chapter, series=replace(wrong_chapter.series, title_no=999))
    batch = BatchResult(source_inventory().select(), (completed_chapter(1), wrong_chapter, completed_chapter(5)), True)

    assert not batch.is_complete
    assert batch.incomplete_episode_ids == (2,)
    assert batch.exit_code == 1


def test_chapter_needs_all_verification_and_publication_evidence() -> None:
    complete = completed_chapter(125)
    incomplete = [
        replace(complete, expected_pages=()),
        replace(complete, pages=()),
        replace(complete, pages=complete.pages[:1]),
        replace(complete, pages=(complete.pages[0], complete.pages[0])),
        replace(complete, artifact=None),
        replace(complete, artifact_verified=False),
        replace(complete, published=False),
        replace(complete, interrupted=True),
        replace(complete, error="checksum failed"),
    ]

    assert complete.is_complete
    assert all(not chapter.is_complete for chapter in incomplete)
    assert replace(complete, pages=complete.pages[::-1]).is_complete


def test_same_page_count_with_the_wrong_source_or_filename_fails() -> None:
    complete = completed_chapter(125)
    first, second = complete.pages

    for changed in (
        replace(second.page, source_url="https://images.example.invalid/wrong.jpg"),
        replace(second.page, target_name="wrong.jpg"),
        replace(second.page, position=3),
    ):
        assert not replace(complete, pages=(first, replace(second, page=changed))).is_complete


def test_repeated_source_urls_at_different_positions_are_preserved() -> None:
    complete = completed_chapter(125)
    first, second = complete.pages
    repeated = replace(second.page, source_url=first.page.source_url)
    chapter = replace(complete, expected_pages=(first.page, repeated), pages=(first, replace(second, page=repeated)))

    assert chapter.is_complete


def test_duplicate_output_names_or_noncontiguous_positions_are_invalid() -> None:
    complete = completed_chapter(125)
    first, second = complete.pages
    for changed in (replace(second.page, target_name=first.page.target_name), replace(second.page, position=3)):
        chapter = replace(complete, expected_pages=(first.page, changed), pages=(first, replace(second, page=changed)))
        assert not chapter.is_complete


def test_page_requires_nonempty_decoded_and_hashed_content() -> None:
    page = completed_chapter(125).pages[0]

    assert page.is_verified
    for changed in (
        replace(page, size=0),
        replace(page, content_verified=False),
        replace(page, sha256=""),
        replace(page, sha256="z" * 64),
        replace(page, error="decode failed"),
    ):
        assert not changed.is_verified


def test_interrupted_chapter_is_never_a_completed_batch() -> None:
    batch = BatchResult(
        source_inventory().select(),
        (completed_chapter(1), ChapterResult(2, (), source_inventory().series, interrupted=True), completed_chapter(5)),
        report_written=True,
    )

    assert batch.status == "interrupted"
    assert batch.exit_code == 130
    assert batch.incomplete_episode_ids == (2,)
