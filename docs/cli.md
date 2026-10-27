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
| `--separate`／`--no-separate` | 圖片預設每章一個資料夾；後者改為平面輸出。封裝格式預設不使用 separate |
| `--chapter-naming`／`-N` | 分章資料夾命名：`title`／`t`、`number`／`n`、`number-title`／`nt`；預設 `number-title` |

```bash
webtoon-downloader "WEBTOON_URL" --separate -N nt --out "./downloads"
webtoon-downloader "WEBTOON_URL" --save-as cbz --out "./downloads"
```

章節與頁碼依總數補零；合法空白、中文與括號會保留。圖片預設採分章 `number-title` 資料夾。

標題中的斜線與反斜線一律轉成 `_`，例如 `12-Part (1/2)` 輸出為單層 `12-Part (1_2)`。若發現舊版依斜線建立的巢狀章節目錄，下載器會要求先整理，而不自動建立另一份重複章節或搬移既有檔案。

來源標題改變時，唯一的同編號舊目錄會作為校驗候選；完整內容符合後沿用舊名，不另建新名稱的副本。編號只用於尋找候選，不代表內容已完整。多個候選、內容不符或來源順序衝突會回報失敗並保留資料。

正常成功後只留下要求的章節產物，`.webtoon-downloader` 工作區會清除；失敗或中斷保留供續傳。既有使用者檔案、唯一舊檔備份與其他未完成工作不會被清除；無法安全清理時回傳非零碼並保留診斷。

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

`--keep-workspace` 明確保留成功後的校驗快取與預設報告。成功清除快取後，下次驗證舊章會重新取得來源內容，不能期待跨次零下載重用。

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

`--inspect` 只輸出完整公開章節清單、分頁證據與官方作品狀態 JSON，不下載或寫檔。官方狀態未知時為 `unknown`，不代表完結。

`--result-file -` 將唯一一份結果 JSON 寫入 stdout，進度寫入 stderr，不建立結果檔。排程可使用這個介面，搭配 exit code 判定結果。結果新增 `result_delivered` 與 `retryable`；stdout 模式的 `report_written` 為 `false`。

`--debug` 啟用較詳細的診斷並寫入 `webtoon_downloader.log`。分享 log 前請遮蔽認證、代理資訊與不需要公開的路徑。

| Exit code | 意義 |
| --- | --- |
| `0` | 指定範圍全部驗證完成、收尾及指定結果輸出成功；或 inspect 成功 |
| `1` | 未完成、來源未知、校驗或執行失敗 |
| `2` | 參數錯誤 |
| `130`／`143` | SIGINT／SIGTERM 中止 |

排程應讀取 exit code 與 JSON，不用最大資料夾編號或特定錯誤字串推算完成狀態。
