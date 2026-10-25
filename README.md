# Webtoon Downloader

從 Webtoons 公開網頁下載章節的命令列工具，支援圖片資料夾、ZIP、CBZ 與 PDF，並提供重試、完整性校驗及續傳。

[![Tag](https://img.shields.io/github/v/tag/hsu30/Webtoon-Downloader?sort=date&label=tag)](https://github.com/hsu30/Webtoon-Downloader/tags)
[![Checks](https://img.shields.io/github/actions/workflow/status/hsu30/Webtoon-Downloader/main.yml?branch=master&label=checks)](https://github.com/hsu30/Webtoon-Downloader/actions/workflows/main.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

本專案由個人維護，公開供有需要的人自行使用或修改。使用說明與原始碼以此倉庫為準。

## 功能

- 下載完整系列、指定章節範圍或最新章節。
- 輸出為圖片、ZIP、含 ComicInfo metadata 的 CBZ，或 PDF。
- 圖片支援 JPG、PNG、WebP；分章資料夾支援標題、編號或 `number-title` 命名。
- API、HTML 與圖片內容讀取失敗時，依次數與時間預算重試。
- 下載內容先暫存並校驗，指定範圍全部完成後才回報成功。
- 支援 `--resume`、失敗報告恢復、JSON 結果與明確 exit code，方便外部排程整合。

## 安裝

需要 Python **3.10 以上**與 Git。以下從本倉庫的固定 tag 安裝；PyPI 上的同名套件不代表此倉庫版本。

使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv tool install --python 3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@v2.4.0-numbertitle.3"
```

或使用支援 uv backend 的 [pipx](https://pipx.pypa.io/)：

```bash
pipx install --backend uv --python python3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@v2.4.0-numbertitle.3"
```

確認指令可用：

```bash
webtoon-downloader --version
webtoon-downloader --help
```

工具目錄需在 `PATH`；可依安裝工具執行 `uv tool update-shell` 或 `pipx ensurepath`，再重新開啟終端。

完整安裝、升級與源碼使用方式見[開始使用](docs/getting-started.md)。

## 使用

將範例中的 `WEBTOON_URL` 換成系列列表頁 URL，例如 `https://www.webtoons.com/zh-hant/.../list?title_no=...`。

每章儲存為個別資料夾，預設使用 `number-title`：

```bash
webtoon-downloader "WEBTOON_URL" --separate --out "./downloads"
```

名稱例如 `85-第83話 3枚金幣(1)`；編號會依系列總章數補零。

下載指定範圍並允許續傳：

```bash
webtoon-downloader "WEBTOON_URL" --start 10 --end 25 --separate --resume --out "./downloads"
```

輸出 CBZ：

```bash
webtoon-downloader "WEBTOON_URL" --save-as cbz --out "./downloads"
```

保存結果報告，之後恢復未完成工作：

```bash
webtoon-downloader "WEBTOON_URL" --separate --out "./downloads" --result-file "./download-result.json"
webtoon-downloader "WEBTOON_URL" --separate --out "./downloads" --retry-failed "./download-result.json"
```

`--separate` 僅用於圖片輸出。未指定時，CLI 將圖片放在同一輸出資料夾。

## 完整性與限制

- 預設 `full` 校驗；`quick` 適合可信的既有完成記錄，無法保證辨識同大小、同時間戳記的損壞。
- 完整性依本次可存取的來源章節與圖片清單判定，不涵蓋未公開、已移除、Daily Pass 或 app-only 內容。
- 網路、來源 API 或儲存空間問題仍可能使下載失敗；工具會保留可恢復狀態並以非零 exit code 回報。
- `.webtoon-downloader/` 保存暫存、狀態與備份，不會自動清理。封裝發布需要輸出檔案系統支援 hard link，並需要可靠的 advisory lock。

詳細行為見[可靠下載與恢復](docs/download-reliability.md)及[常見問題](docs/faq.md)。

## 文件與開發

- [文件首頁](docs/index.md)
- [開始使用](docs/getting-started.md)
- [CLI 參數](docs/cli.md)
- [可靠下載與恢復](docs/download-reliability.md)
- [架構](docs/architecture.md)
- [開發與測試](docs/development.md)
- [回報與修改](CONTRIBUTING.md)

問題可提交至[本倉庫 Issues](https://github.com/hsu30/Webtoon-Downloader/issues)。請附版本、環境、可重現命令及已遮蔽敏感資訊的錯誤內容。

## 來源與授權

本專案源自 [Ali Taibi（Zehina）的 Webtoon-Downloader](https://github.com/Zehina/Webtoon-Downloader)，目前在此倉庫獨立維護。保留原始提交歷史與著作權聲明，採用 [MIT License](LICENSE)。
