"""Read-only inventory and default output containing only the requested artifacts."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from tests.reliable_server import (
    SERIES_URL,
    FixtureWebtoonClient,
    ReliableServer,
    download_options,
    run_source,
)
from webtoon_downloader.cmd import cli as command
from webtoon_downloader.core.webtoon.downloaders import comic
from webtoon_downloader.core.webtoon.downloaders.options import WebtoonDownloadOptions
from webtoon_downloader.core.webtoon.downloaders.result import BatchResult
from webtoon_downloader.core.webtoon.downloaders.state import WorkspaceBusyError, output_lock


@pytest.fixture
def output() -> Path:
    path = Path(__file__).resolve().parents[1] / "test_folder/minimal-runner-downloads" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.mark.parametrize("state", ["completed", "ongoing", "unknown"])
def test_inspect_is_verified_json_only_and_creates_no_files(monkeypatch, output, state):
    server = ReliableServer()
    original = server.respond

    async def respond(request):
        response = await original(request)
        if request.url.path.endswith("/list"):
            marker = {
                "completed": '<p class="day_info"><span class="txt_ico_completed"></span>完結</p>',
                "ongoing": '<p class="day_info">每週四更新</p>',
                "unknown": '<p class="summary">本作品即將完結</p>',
            }[state]
            return httpx.Response(200, request=request, text=response.text + marker)
        return response

    server.respond = respond
    monkeypatch.setattr(
        command, "WebtoonHttpClient", lambda **_: FixtureWebtoonClient(server, download_options(str(output)))
    )
    result = CliRunner().invoke(command.cli, [SERIES_URL, "--inspect", "--out", str(output / "never")])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["publication_status"] == state
    assert payload["inventory_complete"] is True
    assert [c["data_episode_no"] for c in payload["chapters"]] == [1, 2, 5]
    assert payload["evidence"][-1]["next_cursor"] == 0
    assert list(output.iterdir()) == []


def test_inspect_incomplete_cursor_is_a_failure_without_disk_output(monkeypatch, output):
    server = ReliableServer()
    server.source_fault = "missing_cursor"
    monkeypatch.setattr(
        command, "WebtoonHttpClient", lambda **_: FixtureWebtoonClient(server, download_options(str(output)))
    )
    result = CliRunner().invoke(command.cli, [SERIES_URL, "--inspect"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["inventory_complete"] is False
    assert list(output.iterdir()) == []


def install_source(monkeypatch, server):
    async def download(options: WebtoonDownloadOptions) -> BatchResult:
        return await run_source(server, options)

    monkeypatch.setattr(comic, "download_webtoon_result", download)


@pytest.mark.parametrize("machine", [False, True])
def test_default_cli_success_and_no_new_chapters_leave_only_images(monkeypatch, output, machine):
    install_source(monkeypatch, ReliableServer())
    args = [SERIES_URL, "--out", str(output), "--minimum-free-bytes", "0"]
    if machine:
        args.extend(["--result-file", "-"])
    for _ in range(2):
        result = CliRunner().invoke(command.cli, args)
        assert result.exit_code == 0, result.output
        if machine:
            payload = json.loads(result.stdout)
            assert payload["status"] == "complete"
            assert payload["report_written"] is False
            assert payload["result_delivered"] is True
            assert payload["report_path"] is None
        assert sorted(p.name for p in output.iterdir()) == ["1-First", "2-Second", "3-Third available episode"]
        assert all(p.suffix == ".jpg" for p in output.rglob("*") if p.is_file())


@pytest.mark.asyncio
async def test_failure_is_retained_and_resume_success_cleans_cache(output):
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["404"]
    options = download_options(str(output), keep_workspace=False)
    failed = await run_source(server, options)
    assert failed.exit_code == 1
    assert failed.report_path.is_file()
    assert (output / ".webtoon-downloader").is_dir()
    recovered = await run_source(server, download_options(str(output), resume=True, keep_workspace=False))
    assert recovered.is_complete
    assert not (output / ".webtoon-downloader").exists()
    assert recovered.report_path is None


@pytest.mark.asyncio
async def test_interruption_retains_recovery_workspace(output):
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["slow"]
    task = asyncio.create_task(run_source(server, download_options(str(output), keep_workspace=False)))
    while not server.calls[server.image_url(2, 2)]:
        await asyncio.sleep(0.01)
    task.cancel()
    result = await task
    assert result.status == "interrupted"
    assert (output / ".webtoon-downloader").is_dir()


@pytest.mark.asyncio
async def test_explicit_report_is_preserved_outside_the_cleaned_cache(output):
    report = output / "requested.json"
    result = await run_source(
        ReliableServer(), download_options(str(output), keep_workspace=False, result_file=str(report))
    )
    assert result.is_complete and result.report_written
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "complete"
    assert not (output / ".webtoon-downloader").exists()


def test_explicit_keep_workspace_retains_the_report(monkeypatch, output):
    install_source(monkeypatch, ReliableServer())
    result = CliRunner().invoke(command.cli, [SERIES_URL, "--out", str(output), "--keep-workspace"])
    assert result.exit_code == 0, result.output
    assert len(list(output.glob(".webtoon-downloader/*/results/*.json"))) == 1


@pytest.mark.asyncio
async def test_cleanup_refuses_to_delete_unique_backup(output):
    server = ReliableServer()
    assert (await run_source(server, download_options(str(output)))).is_complete
    damaged = output / "2-Second/2.jpg"
    damaged.write_bytes(b"unique old content")
    result = await run_source(server, download_options(str(output), resume=True, keep_workspace=False))
    assert result.exit_code == 1
    assert "cleanup" in result.error
    backups = list(output.glob(".webtoon-downloader/*/chapters/2/backup-*/2.jpg"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"unique old content"


@pytest.mark.asyncio
async def test_unknown_workspace_content_is_not_removed(output):
    server = ReliableServer()
    assert (await run_source(server, download_options(str(output)))).is_complete
    personal = output / ".webtoon-downloader/zh-hant-webtoon-4601/personal.txt"
    personal.write_text("keep", encoding="utf-8")
    result = await run_source(server, download_options(str(output), resume=True, keep_workspace=False))
    assert result.exit_code == 1
    assert personal.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_successful_subrange_does_not_remove_another_failed_chapter(output):
    server = ReliableServer()
    server.faults[server.image_url(2, 2)] = ["404"]
    assert (await run_source(server, download_options(str(output), keep_workspace=False))).exit_code == 1
    result = await run_source(server, download_options(str(output), start=1, end=1, resume=True, keep_workspace=False))
    assert result.exit_code == 1
    assert (output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2/manifest.json").is_file()


def test_output_lock_prevents_cleanup_by_a_second_owner(output):
    with output_lock(output), pytest.raises(WorkspaceBusyError), output_lock(output):
        pytest.fail("second owner must not enter")


@pytest.mark.parametrize("arguments,suffix", [(["--no-separate"], ".jpg"), (["--save-as", "cbz"], ".cbz")])
def test_explicit_other_output_modes_still_work(monkeypatch, output, arguments, suffix):
    install_source(monkeypatch, ReliableServer())
    result = CliRunner().invoke(command.cli, [SERIES_URL, "--out", str(output), *arguments])
    assert result.exit_code == 0, result.output
    assert all(p.is_file() and p.suffix == suffix for p in output.iterdir())


@pytest.mark.asyncio
async def test_existing_number_title_zero_padding_is_preserved_without_cache(output):
    server = ReliableServer()
    existing = output / "01-First"
    existing.mkdir()
    for n in (1, 2):
        (existing / f"{n}.jpg").write_bytes(server.image)
    result = await run_source(server, download_options(str(output), keep_workspace=False))
    assert result.is_complete
    assert existing.is_dir()
    assert not (output / "1-First").exists()
    assert not (output / ".webtoon-downloader").exists()


@pytest.mark.asyncio
async def test_requested_report_write_failure_keeps_recovery_data(monkeypatch, output):
    from webtoon_downloader.core.webtoon.downloaders import reliable

    original = reliable.atomic_json
    report = output / "requested.json"

    def write(target, payload):
        if target == report:
            raise PermissionError("fixture")
        return original(target, payload)

    monkeypatch.setattr(reliable, "atomic_json", write)
    result = await run_source(
        ReliableServer(), download_options(str(output), keep_workspace=False, result_file=str(report))
    )
    assert result.exit_code == 1 and not result.result_delivered
    assert (output / ".webtoon-downloader").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires additional Windows privileges")
@pytest.mark.asyncio
async def test_cleanup_never_follows_a_link_to_user_content(output):
    server = ReliableServer()
    assert (await run_source(server, download_options(str(output)))).is_complete
    personal = output / "personal"
    personal.mkdir()
    marker = personal / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    link = output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/1/pages/link"
    link.symlink_to(personal, target_is_directory=True)
    result = await run_source(server, download_options(str(output), resume=True, keep_workspace=False))
    assert result.exit_code == 1
    assert marker.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["第49話 證明(1/2)", "[第2部第63話] 寄生蟲\uff081/2\uff09"])
async def test_new_slash_title_is_flat_and_legacy_layout_is_not_duplicated(output, title):
    server = ReliableServer()
    server.title_override = title
    fresh = output / "fresh"
    result = await run_source(server, download_options(str(fresh), start=2, end=2, keep_workspace=False))
    assert result.is_complete
    flat_name = "2-" + title.replace("/", "_")
    assert sorted(p.name for p in fresh.iterdir()) == [flat_name]
    assert all(p.is_file() for p in (fresh / flat_name).iterdir())

    old = output / "legacy"
    leaf = old / ("2-" + title)
    leaf.mkdir(parents=True)
    (leaf / "1.jpg").write_bytes(server.image)
    (leaf / "2.jpg").write_bytes(server.image)
    before = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*.jpg")}
    refused = await run_source(server, download_options(str(old), start=2, end=2, resume=True, keep_workspace=False))
    assert refused.exit_code == 1
    assert "legacy_nested_chapter_requires_manual_migration" in refused.chapters[0].error
    assert not (old / flat_name).exists()
    assert before == {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*.jpg")}


@pytest.mark.asyncio
async def test_nested_cached_target_cannot_recreate_legacy_directory(output):
    server = ReliableServer()
    server.title_override = "Part (1/2)"
    assert (await run_source(server, download_options(str(output), start=2, end=2))).is_complete
    manifest = output / ".webtoon-downloader/zh-hant-webtoon-4601/chapters/2/manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["target"] = "2-Part (1/2)"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    before = {p: p.read_bytes() for p in output.glob("2-Part*/*.jpg")}
    result = await run_source(server, download_options(str(output), start=2, end=2, resume=True))
    assert result.exit_code == 1
    assert "legacy_nested_manifest_requires_manual_migration" in result.chapters[0].error
    assert not (output / "2-Part (1").exists()
    assert before == {p: p.read_bytes() for p in output.glob("2-Part*/*.jpg")}
