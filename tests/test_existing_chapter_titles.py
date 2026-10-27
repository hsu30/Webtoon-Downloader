"""Caption changes select candidates; full page verification establishes content identity."""

from __future__ import annotations

import io
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image

from tests.reliable_server import ReliableServer, download_options, run_source


@pytest.fixture
def output() -> Path:
    path = Path(__file__).resolve().parents[1] / "test_folder/existing-caption-tests" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def existing(root: Path, name: str, data: bytes) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for number in (1, 2):
        (folder / f"{number}.jpg").write_bytes(data)
    return folder


@pytest.mark.asyncio
@pytest.mark.parametrize("old_name", ["2-Old caption", "02-Old caption", "2_Old caption", "02"])
async def test_full_validation_adopts_unique_old_folder_without_renaming(output, old_name):
    server = ReliableServer()
    server.title_override = "New caption (1)"
    old = existing(output, old_name, server.image)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in old.iterdir()}
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True, keep_workspace=False))
    assert result.is_complete
    assert result.chapters[0].artifact == old
    assert sorted(p.name for p in output.iterdir()) == [old_name]
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in before.items())
    assert all(page.content_verified and page.attempts > 0 for page in result.chapters[0].pages)


@pytest.mark.asyncio
async def test_changed_caption_never_overwrites_conflicting_unmanaged_content(output):
    server = ReliableServer()
    server.title_override = "New caption"
    old = existing(output, "2-Old caption", b"unique original content")
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True, keep_workspace=False))
    assert result.exit_code == 1
    assert result.incomplete_episode_ids == (2,)
    assert not (output / "2-New caption").exists()
    assert all(p.read_bytes() == b"unique original content" for p in old.iterdir())
    assert not list(output.glob(".webtoon-downloader/*/chapters/2/backup-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing_page", "different_valid_image"])
async def test_old_name_does_not_bypass_page_set_or_hash_verification(output, problem):
    server = ReliableServer()
    old = output / "2-Old caption"
    old.mkdir()
    (old / "1.jpg").write_bytes(server.image)
    if problem == "different_valid_image":
        with io.BytesIO() as stream:
            Image.new("RGB", (8, 16), "blue").save(stream, format="JPEG")
            (old / "2.jpg").write_bytes(stream.getvalue())
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True))
    assert result.exit_code == 1 and result.incomplete_episode_ids == (2,)
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    assert not (output / "2-Second").exists()


@pytest.mark.asyncio
async def test_duplicate_numbered_candidates_fail_even_if_one_title_is_current(output):
    server = ReliableServer()
    first = existing(output, "2-Second", server.image)
    second = existing(output, "02-Other", b"keep other")
    result = await run_source(server, download_options(str(output), start=2, end=2))
    assert result.exit_code == 1
    assert "ambiguous_existing_chapter_folders" in result.chapters[0].error
    assert (first / "1.jpg").read_bytes() == server.image
    assert (second / "1.jpg").read_bytes() == b"keep other"
    assert server.calls[server.image_url(2, 1)] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [False, True])
async def test_stale_cache_cannot_transfer_ownership_to_another_existing_folder(output, matching):
    server = ReliableServer()
    server.title_override = "New caption"
    template = output / "template"
    assert (await run_source(server, download_options(str(template), start=2, end=2))).is_complete
    root = output / "target"
    payload = server.image if matching else b"do not move or replace this data"
    old = existing(root, "2-Old caption", payload)
    relative_work = ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2"
    shutil.copytree(template / relative_work, root / relative_work)
    # The copied completed cache still claims ownership of 2-New caption, which does not exist here.
    result = await run_source(server, download_options(str(root), start=2, end=2, resume=True, keep_workspace=False))
    assert result.exit_code == (0 if matching else 1)
    assert not (root / "2-New caption").exists()
    assert all(p.read_bytes() == payload for p in old.iterdir())
    if matching:
        assert result.chapters[0].artifact == old
        assert not (root / ".webtoon-downloader").exists()
    else:
        manifest = json.loads((root / relative_work / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["owned_target"] is False
        assert manifest["target"] == old.name
        assert not list((root / relative_work).glob("backup-*"))


@pytest.mark.asyncio
async def test_cached_stable_episode_reuses_old_caption_after_full_verification(output):
    server = ReliableServer()
    assert (await run_source(server, download_options(str(output), start=2, end=2))).is_complete
    calls = server.calls.copy()
    server.title_override = "New caption"
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True))
    assert result.is_complete and result.chapters[0].artifact == output / "2-Second"
    assert not (output / "2-New caption").exists()
    assert server.calls[server.image_url(2, 1)] == calls[server.image_url(2, 1)]


@pytest.mark.asyncio
async def test_cached_number_shift_is_not_treated_as_a_caption_change(output):
    server = ReliableServer()
    assert (await run_source(server, download_options(str(output), start=2, end=2))).is_complete
    path = output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["target"] = "9-Previous position"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True))
    assert result.exit_code == 1
    assert "cached_chapter_number_changed" in result.chapters[0].error
    assert not (output / "9-Previous position").exists()
    assert (output / "2-Second/1.jpg").read_bytes() == server.image


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
@pytest.mark.asyncio
async def test_numbered_symlink_is_not_ignored_in_favor_of_creating_a_duplicate(output):
    server = ReliableServer()
    foreign = existing(output, "foreign", server.image)
    (output / "2-Old caption").symlink_to(foreign, target_is_directory=True)
    result = await run_source(server, download_options(str(output), start=2, end=2))
    assert result.exit_code == 1
    assert not (output / "2-Second").exists()
    assert (foreign / "1.jpg").read_bytes() == server.image
