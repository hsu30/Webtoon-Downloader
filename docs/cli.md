# CLI 參數

```bash
webtoon-downloader [OPTIONS] URL
```

`URL` 使用 Webtoons 系列列表頁。以下 `WEBTOON_URL` 是需要替換的範例值；完整選項以 `webtoon-downloader --help` 為準。

## 選擇章節

| 參數 | 說明 |
| --- | --- |
| `--start`／`-s` | 起始章節位置，從 1 開始 |
| `--end`／`-e` | 結束章節位置，包含該章 |
| `--latest`／`-l` | 只下載完整來源清單中的最後一章，不可與起訖合用 |

位置依取得的來源清單排序，不一定等於 URL 的 `episode_no` 或標題內的話數。反向或超出來源範圍的要求會報錯，不會默默縮短。

## 輸出與命名

| 參數 | 說明 |
| --- | --- |
| `--out`／`-o` | 直接存放章節產物的目錄 |
| `--save-as`／`-sa` | `images`（預設）、`zip`、`cbz`、`pdf` |
| `--separate` | 圖片每章一個資料夾；不可與封裝格式合用 |
| `--chapter-naming`／`-N` | 分章資料夾命名：`title`／`t`、`number`／`n`、`number-title`／`nt`；預設 `number-title` |

```bash
webtoon-downloader "WEBTOON_URL" --separate -N nt --out "./downloads"
webtoon-downloader "WEBTOON_URL" --save-as cbz --out "./downloads"
```

章節與頁碼依總數補零；合法空白、中文與括號會保留。不加 `--separate` 時，圖片使用平面輸出。

## 圖片與 metadata

| 參數 | 說明 |
| --- | --- |
| `--image-format`／`-f` | `jpg`（預設）、`png`、`webp` |
| `--quality` | Webtoons 來源品質設定：40 到 100，間隔 10 |
| `--export-metadata`／`-em` | 輸出系列摘要、章節標題與作者備註 |
| `--export-format`／`-ef` | `json`（預設）、`text`、`all` |

來源品質與轉檔是不同步驟；`--quality` 不等於 Pillow 的編碼品質參數。CBZ 另包含 ComicInfo metadata。

## 重試與恢復

重試涵蓋 API／HTML 及圖片的 body 讀取；`--request-retries`、`--page-retries` 與 `--chapter-retries` 均表示首次以外的額外次數。

```bash
webtoon-downloader "WEBTOON_URL" --separate --resume --out "./downloads"
webtoon-downloader "WEBTOON_URL" --separate --out "./downloads" --result-file "./result.json"
webtoon-downloader "WEBTOON_URL" --separate --out "./downloads" --retry-failed "./result.json"
```

`--verify full` 是預設；`quick` 主要比對可信既有狀態的集合、大小與時間戳記。`--retry-strategy none` 關閉自動重試，`--fail-fast` 會在首章失敗後取消其餘工作並收尾。

所有次數、退避、timeout、時間預算、校驗及資源上限的完整說明，見[可靠下載與恢復](download-reliability.md)。

## 併發與網路

- `--concurrent-chapters`：預設 2。
- `--concurrent-pages`：預設 8，全章節共用。
- `--proxy`：明確指定代理位址；程式不自動採用 shell 的 proxy 環境變數。

```bash
webtoon-downloader "WEBTOON_URL" --separate --concurrent-chapters 1 --concurrent-pages 4
webtoon-downloader "WEBTOON_URL" --separate --proxy "http://127.0.0.1:7890"
```

## 診斷與排程

`--debug` 啟用較詳細的診斷並寫入 `webtoon_downloader.log`。分享 log 前請遮蔽認證、代理資訊與不需要公開的路徑。

| Exit code | 意義 |
| --- | --- |
| `0` | 指定範圍全部驗證完成，報告已保存 |
| `1` | 未完成、來源未知、校驗或執行失敗 |
| `2` | 參數錯誤 |
| `130`／`143` | SIGINT／SIGTERM 中止 |

排程應讀取 exit code 與 JSON，不用最大資料夾編號或特定錯誤字串推算完成狀態。
