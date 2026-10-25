from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import tracemalloc
import uuid
import zipfile
from pathlib import Path

import pytest

from tests.reliable_server import ReliableServer, download_options, run_source
from webtoon_downloader.core.webtoon.downloaders import reliable
from webtoon_downloader.core.webtoon.downloaders.state import exclusive_lock


@pytest.fixture
def retained_output() -> Path:
    # Keep generated evidence for review; never clean unrelated test_folder content.
    root = Path(__file__).resolve().parents[1] / "test_folder" / "reliability-tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["images", "zip", "cbz", "pdf"])
async def test_complete_delivery_for_every_output_format(retained_output: Path, kind: str) -> None:
    server = ReliableServer()
    result = await run_source(server, download_options(str(retained_output), save_as=kind, separate=kind == "images"))

    assert result.is_complete, result
    assert result.exit_code == 0
    assert [chapter.episode_no for chapter in result.chapters] == [1, 2, 5]
    assert all(len(chapter.pages) == 2 and chapter.artifact.exists() for chapter in result.chapters)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["incomplete_episode_ids"] == []


@pytest.mark.asyncio
async def test_retry_missing_page_then_resume_without_redownloading_valid_pages(retained_output: Path) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = ["length", "length"]
    options = download_options(str(retained_output))
    failed = await run_source(server, options)

    assert failed.incomplete_episode_ids == (2,)
    assert failed.exit_code == 1
    assert not (retained_output / "2-Second").exists()
    assert server.calls[broken] == 2
    calls = server.calls.copy()
    recovered = await run_source(server, download_options(str(retained_output), resume=True))

    assert recovered.is_complete, recovered
    assert server.calls[broken] == 3
    for episode, page in ((1, 1), (1, 2), (2, 1), (5, 1), (5, 2)):
        url = server.image_url(episode, page)
        assert server.calls[url] == calls[url]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["read", "length", "invalid", "empty", "503"])
async def test_transient_image_failures_retry_the_entire_body(retained_output: Path, fault: str) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = [fault]

    result = await run_source(server, download_options(str(retained_output)))

    assert result.is_complete, result
    assert server.calls[broken] == 2


@pytest.mark.asyncio
async def test_request_body_timeout_recovers_before_downloading_pages(retained_output: Path) -> None:
    server = ReliableServer()
    url = "https://www.webtoons.com/zh-hant/romance/example/second/viewer?title_no=4601&episode_no=2"
    server.faults[url] = ["read"]

    result = await run_source(server, download_options(str(retained_output)))

    assert result.is_complete
    assert server.calls[url] == 3  # failed body, successful body, final source-list check


@pytest.mark.asyncio
async def test_chapter_retry_does_not_reset_the_failed_page_budget(retained_output: Path) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = ["length"] * 10

    result = await run_source(server, download_options(str(retained_output), chapter_retries=2))

    assert result.incomplete_episode_ids == (2,)
    assert server.calls[broken] == 2
    assert server.calls[server.image_url(2, 1)] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["403", "404"])
async def test_permanent_http_errors_are_not_retried(retained_output: Path, code: str) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = [code] * 10

    result = await run_source(server, download_options(str(retained_output), chapter_retries=2))

    assert result.exit_code == 1
    assert server.calls[broken] == 1


@pytest.mark.asyncio
async def test_none_disables_page_and_chapter_retries(retained_output: Path) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = ["read"] * 10

    result = await run_source(server, download_options(str(retained_output), retry_strategy=None, chapter_retries=2))

    assert result.exit_code == 1
    assert server.calls[broken] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_cursor", "duplicate"])
async def test_unverified_source_never_becomes_a_successful_batch(retained_output: Path, fault: str) -> None:
    server = ReliableServer()
    server.source_fault = fault

    result = await run_source(server, download_options(str(retained_output)))

    assert result.exit_code == 1
    assert result.scope is None
    assert not any("images.example.invalid" in url for url in server.calls)
    assert result.report_path.exists()


@pytest.mark.asyncio
async def test_retry_failed_rechecks_the_original_scope_and_repairs_only_failed_pages(retained_output: Path) -> None:
    server = ReliableServer()
    broken = server.image_url(2, 2)
    server.faults[broken] = ["read"] * 2
    failed = await run_source(server, download_options(str(retained_output)))
    previous = server.calls.copy()

    result = await run_source(server, download_options(str(retained_output), retry_failed=str(failed.report_path)))

    assert result.is_complete
    assert result.scope.episode_ids == (1, 2, 5)
    assert server.calls[server.image_url(1, 1)] == previous[server.image_url(1, 1)]
    assert server.calls[broken] == 3


@pytest.mark.asyncio
async def test_corrupt_managed_output_is_repaired_with_a_preserved_backup(retained_output: Path) -> None:
    server = ReliableServer()
    initial = await run_source(server, download_options(str(retained_output)))
    assert initial.is_complete
    target = retained_output / "2-Second" / "2.jpg"
    target.write_bytes(b"broken")
    previous = server.calls.copy()

    repaired = await run_source(server, download_options(str(retained_output), resume=True))

    assert repaired.is_complete, repaired
    assert target.read_bytes() == server.image
    backups = list((retained_output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2").glob("backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "2.jpg").read_bytes() == b"broken"
    assert all(server.calls[server.image_url(2, number)] == previous[server.image_url(2, number)] for number in (1, 2))


@pytest.mark.asyncio
async def test_unmanaged_existing_content_is_preserved_on_conflict(retained_output: Path) -> None:
    directory = retained_output / "2-Second"
    directory.mkdir()
    marker = directory / "personal.txt"
    marker.write_text("keep", encoding="utf-8")

    result = await run_source(ReliableServer(), download_options(str(retained_output), resume=True))

    assert result.exit_code == 1
    assert result.incomplete_episode_ids == (2,)
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in directory.iterdir()) == ["personal.txt"]


@pytest.mark.asyncio
async def test_default_full_validation_detects_same_size_tampering(retained_output: Path) -> None:
    server = ReliableServer()
    assert (await run_source(server, download_options(str(retained_output)))).is_complete
    target = retained_output / "2-Second" / "2.jpg"
    stats = target.stat()
    data = target.read_bytes()
    target.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    os.utime(target, ns=(stats.st_atime_ns, stats.st_mtime_ns))
    timestamp_restored = target.stat().st_mtime_ns == stats.st_mtime_ns

    quick = await run_source(server, download_options(str(retained_output), verify="quick"))
    full = await run_source(server, download_options(str(retained_output)))

    # Some mounted filesystems round utime; quick may detect that metadata change.
    assert quick.is_complete == timestamp_restored
    assert quick.verification == "quick"
    assert full.exit_code == 1
    assert full.incomplete_episode_ids == (2,)


@pytest.mark.asyncio
async def test_flat_images_resume_without_overwriting_other_chapters(retained_output: Path) -> None:
    server = ReliableServer()
    options = download_options(str(retained_output), separate=False)
    result = await run_source(server, options)
    assert result.is_complete, result
    calls = server.calls.copy()

    recovered = await run_source(server, download_options(str(retained_output), separate=False, resume=True))

    assert recovered.is_complete
    assert server.calls[server.image_url(1, 1)] == calls[server.image_url(1, 1)]
    assert (retained_output / "2_2.jpg").exists()


@pytest.mark.asyncio
async def test_chapter_time_budget_drains_pending_tasks(retained_output: Path) -> None:
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["slow"]
    result = await run_source(server, download_options(str(retained_output), chapter_time_budget=0.2))

    assert result.exit_code == 1
    assert 2 in result.incomplete_episode_ids
    assert not (retained_output / "2-Second").exists()
    assert not any(
        "ReliableDownloader" in repr(task.get_coro())
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_cancellation_saves_the_original_scope_and_incomplete_ids(retained_output: Path) -> None:
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["slow"]
    task = asyncio.create_task(run_source(server, download_options(str(retained_output))))
    for _ in range(100):
        if server.calls[server.image_url(2, 2)]:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    result = await task

    assert result.status == "interrupted"
    assert result.exit_code == 130
    assert 2 in result.incomplete_episode_ids
    assert result.report_path.exists()


@pytest.mark.asyncio
async def test_fail_fast_is_failure_rather_than_user_interruption(retained_output: Path) -> None:
    server = ReliableServer()
    server.faults[server.image_url(2, 1)] = ["404"]
    result = await run_source(server, download_options(str(retained_output), fail_fast=True))

    assert result.exit_code == 1
    assert result.status != "interrupted"
    assert len(result.chapters) == 3


@pytest.mark.asyncio
async def test_legacy_scheme_and_shell_escape_normalization_is_preserved(retained_output: Path) -> None:
    options = download_options(str(retained_output), url=r"www.webtoons.com/zh-hant/romance/example/list\?title_no\=4601")
    result = await run_source(ReliableServer(), options)

    assert result.is_complete, result


@pytest.mark.asyncio
async def test_locked_chapter_is_reported_without_breaking_the_other_process_lock(retained_output: Path) -> None:
    lock = retained_output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2/chapter.lock"
    with exclusive_lock(lock):
        result = await run_source(ReliableServer(), download_options(str(retained_output)))

    assert result.incomplete_episode_ids == (2,)
    assert result.exit_code == 1
    assert lock.exists()


@pytest.mark.asyncio
async def test_corrupt_manifest_cannot_shrink_the_expected_page_set(retained_output: Path) -> None:
    server = ReliableServer()
    assert (await run_source(server, download_options(str(retained_output)))).is_complete
    path = retained_output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2/manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expected"] = data["expected"][:1]
    data["pages"] = data["pages"][:1]
    path.write_text(json.dumps(data), encoding="utf-8")

    result = await run_source(server, download_options(str(retained_output), resume=True))

    assert result.exit_code == 1
    assert result.incomplete_episode_ids == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"max_image_bytes": 1}, {"max_image_pixels": 1}, {"minimum_free_bytes": 10**18}])
async def test_resource_limits_do_not_turn_skipped_pages_into_success(retained_output: Path, changes: dict) -> None:
    result = await run_source(ReliableServer(), download_options(str(retained_output), **changes))

    assert result.exit_code == 1
    assert not any(chapter.is_complete for chapter in result.chapters)


@pytest.mark.asyncio
async def test_viewer_changes_reuse_matching_pages_but_fetch_the_new_page(retained_output: Path) -> None:
    server = ReliableServer()
    server.growing_episode = 2
    result = await run_source(server, download_options(str(retained_output), chapter_retries=1))

    assert result.is_complete, result
    assert len(result.chapters[1].pages) == 3
    assert all(server.calls[server.image_url(2, number)] == 1 for number in (1, 2, 3))


@pytest.mark.asyncio
async def test_removed_source_chapter_prevents_final_batch_success(retained_output: Path) -> None:
    server = ReliableServer()
    server.omit_on_refresh = 2
    result = await run_source(server, download_options(str(retained_output)))

    assert result.exit_code == 1
    assert result.incomplete_episode_ids == (2,)
    assert "source_changed" in result.error


@pytest.mark.asyncio
async def test_report_write_failure_is_not_success(retained_output: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = reliable.atomic_json

    def fail_report(path: Path, payload: dict) -> None:
        if path.name == "blocked.json":
            raise PermissionError("fixture")
        original(path, payload)

    monkeypatch.setattr(reliable, "atomic_json", fail_report)
    result = await run_source(
        ReliableServer(), download_options(str(retained_output), result_file=str(retained_output / "blocked.json"))
    )

    assert result.exit_code == 1
    assert not result.report_written
    assert all(chapter.is_complete for chapter in result.chapters)


@pytest.mark.asyncio
async def test_publish_failure_restores_the_previous_managed_target(
    retained_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ReliableServer()
    assert (await run_source(server, download_options(str(retained_output)))).is_complete
    target = retained_output / "2-Second/2.jpg"
    target.write_bytes(b"original-damaged-file")
    original = reliable.ReliableDownloader._publish_new

    def fail_publish(self: reliable.ReliableDownloader, staged: Path, final: Path) -> None:
        if final.name == "2-Second":
            raise OSError("fixture")
        original(self, staged, final)

    monkeypatch.setattr(reliable.ReliableDownloader, "_publish_new", fail_publish)
    result = await run_source(server, download_options(str(retained_output), resume=True))

    assert result.exit_code == 1
    assert target.read_bytes() == b"original-damaged-file"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["images", "zip", "cbz", "pdf"])
@pytest.mark.parametrize("image_format", ["PNG", "WEBP"])
async def test_converted_pages_and_text_metadata_are_delivered(
    retained_output: Path, kind: str, image_format: str
) -> None:
    result = await run_source(
        ReliableServer(),
        download_options(
            str(retained_output),
            save_as=kind,
            separate=kind == "images",
            image_format=image_format,
            export_metadata=True,
            exporter_format="all",
        ),
    )

    assert result.is_complete, result
    assert (retained_output / "info.json").exists()
    if kind == "pdf":
        assert (retained_output / "2_title.txt").read_text(encoding="utf-8").strip() == "Second"


@pytest.mark.asyncio
async def test_flat_extra_images_are_not_silently_accepted(retained_output: Path) -> None:
    server = ReliableServer()
    (retained_output / "2_999.jpg").write_bytes(server.image)
    result = await run_source(server, download_options(str(retained_output), separate=False))

    assert result.exit_code == 1
    assert result.incomplete_episode_ids == (2,)
    assert (retained_output / "2_999.jpg").read_bytes() == server.image


@pytest.mark.asyncio
async def test_number_title_output_keeps_unicode_spaces_and_parentheses(retained_output: Path) -> None:
    server = ReliableServer()
    server.title_override = "第83話 3枚金幣(1)"
    result = await run_source(server, download_options(str(retained_output), export_metadata=True))

    assert result.is_complete
    assert (retained_output / "2-第83話 3枚金幣(1)/1.jpg").exists()
    info = json.loads((retained_output / "info.json").read_text(encoding="utf-8"))
    assert info["chapters"]["2"]["notes"] == "Afterword"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["zip", "cbz", "pdf"])
async def test_corrupt_container_is_rebuilt_from_verified_pages_and_backed_up(retained_output: Path, kind: str) -> None:
    server = ReliableServer()
    initial = await run_source(server, download_options(str(retained_output), save_as=kind, separate=False))
    assert initial.is_complete
    target = initial.chapters[1].artifact
    target.write_bytes(b"damaged-container")
    calls = server.calls.copy()

    result = await run_source(server, download_options(str(retained_output), save_as=kind, separate=False, resume=True))

    assert result.is_complete, result
    work = retained_output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2"
    backups = list(work.glob("backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"damaged-container"
    assert all(server.calls[server.image_url(2, page)] == calls[server.image_url(2, page)] for page in (1, 2))


@pytest.mark.asyncio
async def test_valid_zip_with_wrong_image_content_fails_full_verification(retained_output: Path) -> None:
    server = ReliableServer()
    initial = await run_source(server, download_options(str(retained_output), save_as="zip", separate=False))
    assert initial.is_complete
    target = initial.chapters[1].artifact
    with zipfile.ZipFile(target) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    contents[initial.chapters[1].pages[0].page.target_name] = b"not-the-downloaded-image"
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)

    result = await run_source(server, download_options(str(retained_output), save_as="zip", separate=False))

    assert result.incomplete_episode_ids == (2,)
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_independent_chapters_wait_for_publication_lock_instead_of_failing(
    retained_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = reliable.ReliableDownloader._publish_single

    async def slow_publish(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(reliable.ReliableDownloader, "_publish_single", slow_publish)
    result = await run_source(ReliableServer(), download_options(str(retained_output), concurrent_chapters=3))

    assert result.is_complete, result
    assert len(result.chapters) == 3


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32", reason="OS symlink creation requires privileges on Windows; exercised on Linux"
)
async def test_symlink_target_is_rejected_without_writing_through_it(retained_output: Path) -> None:
    outside = retained_output / "personal"
    outside.mkdir()
    (retained_output / "2-Second").symlink_to(outside, target_is_directory=True)

    result = await run_source(ReliableServer(), download_options(str(retained_output)))

    assert result.exit_code == 1
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_nas_concurrency_comparison_preserves_complete_delivery(
    retained_output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = []
    for name, chapters, pages in (("nas", 2, 8), ("legacy", 6, 120)):
        server = ReliableServer()
        server.pages_per_episode = 12
        respond = server.respond
        active = 0
        peak = 0

        async def measured(request, respond=respond):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await respond(request)
            finally:
                active -= 1

        monkeypatch.setattr(server, "respond", measured)
        tracemalloc.start()
        started = time.monotonic()
        result = await run_source(
            server, download_options(str(retained_output / name), concurrent_chapters=chapters, concurrent_pages=pages)
        )
        _, python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result.is_complete, result
        assert peak <= pages
        observations.append({
            "profile": name,
            "seconds": time.monotonic() - started,
            "peak_requests": peak,
            "python_peak_bytes": python_peak,
            "pages": 36,
        })
    (retained_output / "benchmark.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
    assert observations[0]["peak_requests"] <= 8
