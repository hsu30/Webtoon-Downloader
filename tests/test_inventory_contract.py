from __future__ import annotations

from dataclasses import replace

import pytest

from tests.reliability_fixtures import episode_payload, source_inventory
from webtoon_downloader.core.webtoon.inventory import ChapterScope, EpisodePageEvidence, InventoryContractError


def test_inventory_requires_the_full_cursor_chain_and_preserves_nonconsecutive_ids() -> None:
    inventory = source_inventory()

    assert inventory.is_complete
    assert inventory.select().episode_ids == (1, 2, 5)
    assert inventory.select(start=2, end=3).episode_ids == (2, 5)
    assert inventory.select(end="latest").episode_ids == (5,)


@pytest.mark.parametrize(
    "evidence",
    [
        (),
        (EpisodePageEvidence(None, 2, (1, 2)),),
        (EpisodePageEvidence(None, None, (1, 2, 5)),),
        (EpisodePageEvidence(2, 0, (1, 2, 5)),),
        (EpisodePageEvidence(None, 2, (1, 2)), EpisodePageEvidence(3, 0, (5,))),
        (EpisodePageEvidence(None, 2, (1, 2)), EpisodePageEvidence(2, 2, (5,))),
        (
            EpisodePageEvidence(None, 2, (1,)),
            EpisodePageEvidence(2, 2, (2,)),
            EpisodePageEvidence(2, 0, (5,)),
        ),
        (EpisodePageEvidence(None, 0, (1, 2)), EpisodePageEvidence(0, 0, (5,))),
        (EpisodePageEvidence(None, 2, ()), EpisodePageEvidence(2, 0, (1, 2, 5))),
        (EpisodePageEvidence(None, 0, (1, 2)),),
        (EpisodePageEvidence(None, 0, (1, 2, 2)),),
        (EpisodePageEvidence(None, 0, (5, 2, 1)),),
    ],
)
def test_inventory_rejects_missing_truncated_repeated_or_mismatched_pages(
    evidence: tuple[EpisodePageEvidence, ...],
) -> None:
    inventory = replace(source_inventory(), evidence=evidence)

    assert not inventory.is_complete
    with pytest.raises(InventoryContractError):
        inventory.select()


def test_missing_cursor_is_unknown_rather_than_an_assumed_terminal_page() -> None:
    payload = episode_payload("last")
    payload["result"].pop("nextCursor")

    page = EpisodePageEvidence.from_payload(payload, requested_cursor=2)

    assert page.next_cursor is None


@pytest.mark.parametrize("success", [False, None, "true", 1])
def test_api_success_must_be_explicit(success: object) -> None:
    payload = episode_payload("first")
    payload["success"] = success

    with pytest.raises(InventoryContractError):
        EpisodePageEvidence.from_payload(payload)


@pytest.mark.parametrize("cursor", [-1, True, "2", []])
def test_invalid_cursor_is_rejected(cursor: object) -> None:
    payload = episode_payload("first")
    payload["result"]["nextCursor"] = cursor

    with pytest.raises(InventoryContractError):
        EpisodePageEvidence.from_payload(payload)


@pytest.mark.parametrize("episode_no", [0, -1, True, "5", None])
def test_malformed_episode_is_not_silently_skipped(episode_no: object) -> None:
    payload = episode_payload("first")
    payload["result"]["episodeList"][1]["episodeNo"] = episode_no

    with pytest.raises(InventoryContractError):
        EpisodePageEvidence.from_payload(payload)


@pytest.mark.parametrize("payload", [{}, {"success": True}, {"success": True, "result": {"episodeList": None}}])
def test_missing_or_malformed_api_list_is_not_an_empty_success(payload: dict[str, object]) -> None:
    with pytest.raises(InventoryContractError):
        EpisodePageEvidence.from_payload(payload)


@pytest.mark.parametrize("title_no", [0, -1, True])
def test_invalid_series_identity_is_rejected(title_no: int) -> None:
    with pytest.raises(InventoryContractError):
        replace(source_inventory().series, title_no=title_no)


@pytest.mark.parametrize("start,end", [(0, None), (-1, None), (1, 0), (1, 4), (4, None), (3, 2), (1, "latest")])
def test_invalid_range_is_not_silently_clipped(start: int, end: int | str | None) -> None:
    with pytest.raises(InventoryContractError):
        source_inventory().select(start, end)  # type: ignore[arg-type]


def test_empty_source_cannot_be_a_successful_empty_batch() -> None:
    inventory = replace(source_inventory(), chapters=(), evidence=(EpisodePageEvidence(None, 0, ()),))

    assert not inventory.is_complete
    with pytest.raises(InventoryContractError):
        inventory.select(end="latest")


def test_duplicate_source_chapters_are_invalid_even_with_a_terminal_cursor() -> None:
    inventory = source_inventory()
    chapters = (inventory.chapters[0], replace(inventory.chapters[1], data_episode_no=1), inventory.chapters[2])
    duplicate = replace(inventory, chapters=chapters, evidence=(EpisodePageEvidence(None, 0, (1, 1, 5)),))

    assert not duplicate.is_complete


def test_partially_parsed_source_cannot_be_represented_as_complete() -> None:
    inventory = source_inventory()
    shortened = tuple(
        replace(chapter, number=i, total_chapters=2) for i, chapter in enumerate(inventory.chapters[::2], 1)
    )

    assert not replace(inventory, chapters=shortened).is_complete


@pytest.mark.parametrize("ids", [(), (1, 1), (2, 1), (999,)])
def test_selected_scope_rejects_empty_duplicate_reordered_or_foreign_chapters(ids: tuple[int, ...]) -> None:
    with pytest.raises(InventoryContractError):
        ChapterScope(source_inventory(), ids)
