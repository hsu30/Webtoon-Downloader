# 可靠下載、校驗與恢復

本頁說明下載器的重試、校驗、儲存及恢復行為。下載器只有在指定範圍的來源清單已核對、全部章節圖片驗證成功、產物已發布、收尾及指定結果輸出成功時，才以 exit code `0` 結束。

## 一般下載

```bash
webtoon-downloader "WEBTOON_URL" --separate -N nt --out "/downloads/webtoon"
```

預設使用 `number-title`、同時處理 2 章／8 張圖片，以及 `full` 校驗。圖片會先下載到輸出目錄下的 `.webtoon-downloader/` 工作區，完整接收並解碼；整章驗證通過後才發布，例如：

```text
85-第83話 3枚金幣(1)/
```

章節與圖片編號仍沿用總數補零規則。作品身分由語系、系列類型與 `title_no` 決定，章節由來源 `episode_no` 決定；資料夾標題或最大編號不作為完成依據。

來源 API 的 cursor 必須走到終點，指定範圍超界、來源回應缺少證據、空 viewer 或缺頁都會失敗，不會默默縮小要求範圍。

圖片預設分章存放。未要求額外輸出時，成功結束只留下章節資料夾與圖檔：工作區、暫存圖片及預設報告均會清除。失敗或中斷保留恢復資料。此預設同樣適用於手動 CLI 與排程呼叫，重跑沒有新章時也會收尾。

## 重試參數

| 參數 | 預設 | 意義 |
| --- | --- | --- |
| `--request-retries` | `3` | API／HTML 第一次請求以外的額外嘗試，包含 body 讀取 |
| `--page-retries` | `3` | 單張圖片的額外嘗試，包含完整 body、解碼與轉檔；磁碟錯誤不盲目重試 |
| `--chapter-retries` | `1` | 章節的額外恢復輪次，重取 viewer 清單，但不灌滿已耗盡的同頁重試額度 |
| `--retry-strategy` | `exponential` | `exponential`、`linear`、`fixed` 或 `none`；`none` 關閉所有自動重試 |
| `--retry-delay` | `1` 秒 | 基礎退避時間，含 jitter |
| `--retry-max-delay` | `30` 秒 | 程式退避上限；不覆蓋伺服器要求的較長 `Retry-After` |
| `--request-timeout` | `30` 秒 | API／HTML 單次 read 等待上限；connect 上限為 15 秒 |
| `--image-read-timeout` | `90` 秒 | 圖片單次 read 等待上限 |
| `--request-time-budget` | `300` 秒 | 一個完整邏輯操作及其重試等待的總預算，也涵蓋持續滴流的 response body |
| `--chapter-time-budget` | `900` 秒 | 取得章節名額後的總預算，包含重試、驗證、封裝與發布 |

次數 `0` 表示只做首次嘗試，負值拒絕。時間與資源參數拒絕 NaN／無限值。若伺服器要求等待的時間超過剩餘預算，下載器會結束為未完成，不提前違反 `Retry-After` 再送請求。

`429` 會觸發共用冷卻；timeout、部分傳輸／protocol 錯誤、無效圖片及暫時性 HTTP 狀態可重試。`401/403/404`、權限不足、磁碟滿與資源超限不會持續網路重試。

總預算取消後會等待已開始的檔案／解碼工作收尾，確保沒有背景寫入跑到 writer 或 lock 關閉之後；作業系統層長時間阻塞的磁碟操作不具硬即時中止保證。

## 續傳與損壞修復

```bash
webtoon-downloader "WEBTOON_URL" --separate -N nt \
  --out "/downloads/webtoon" --resume
```

`--resume` 重新核對來源，重用同一作品、章節、圖片位置、來源 URL 與輸出設定相符的已驗證暫存頁面，補抓其餘頁面。已完成章節每次仍需通過所選校驗等級。

同一來源圖片 URL 出現在兩個不同位置，會保留兩個預期頁面，不會自動去重。來源清單更新時，符合條件的既有頁面可重用，其餘重新下載。

程式管理的正式產物損壞時，`--resume` 允許在新版產物完整驗證後，以保留備份的方式修復。原內容保存在該章工作區的 `backup-*`，不會自動刪除。沒有可信狀態、內容又不符合當次驗證的既有目標，則回報衝突並保留。

沒有 manifest 的舊資料會先取得來源、下載並驗證預期頁面；現有內容只有逐項符合當次內容 hash 與檔名集合時才採認，否則保留並報告衝突。這類資料不會僅因「有資料夾」或「頁數相同」被跳過。

舊版若曾將標題中的斜線當成路徑分隔符，例如 `12-Part (1/2)/001.jpg`，其實是兩層目錄。新輸出固定使用 `12-Part (1_2)/001.jpg`；重驗舊巢狀章節時會回報 `legacy_nested_chapter_requires_manual_migration`，不自動重複下載至另一個章節目錄。舊 manifest 若指向多層分章目錄也會被拒絕；請先確認資料及目標不存在，再進行人工整理。

同一穩定章節已記錄的目標路徑會優先沿用；缺少快取時也會沿用同編號及同標題、僅補零不同的既有資料夾。相同輸出目錄不能由兩個程序同時發布或清理；鎖定由 OS 管理，不在輸出目錄外增加 lock 檔。

成功後預設不保留驗證快取，因此下一次重新校驗舊章可能需要重新下載來源圖片。需要保留快取以便跨次重用時，明確加入 `--keep-workspace`；失敗暫存不需要此參數就會保留。

## full 與 quick

- `--verify full`：預設。完整解碼圖片、核對輸出 hash 與預期項目；ZIP／CBZ 完整讀取圖片項目與 CRC，PDF 核對頁數、尺寸、內嵌圖片與來源解碼後像素。
- `--verify quick`：供已存在可信完成記錄的資料快速重用，核對檔案集合、大小與時間戳記，以及封裝基本結構。它無法保證辨識同大小、同時間戳記的內容篡改或損壞，報告會明列 `quick`。

兩個模式都要求**新下載內容完整解碼與產物驗證**。quick 不會放寬新下載資料的標準。

PDF 無法直接嵌入的 WebP 會先轉為 PNG，完整驗證時比對解碼後像素；不對 WebP 再做一輪有損壓縮。

## 結果報告與失敗恢復

預設失敗或中斷時，在工作區 `results/` 保存具 `schema_version` 的 JSON；成功時不留下預設報告。`--keep-workspace` 可保留成功報告，也能指定工作區以外的位置：

```bash
webtoon-downloader "WEBTOON_URL" --separate --out "/downloads/webtoon" \
  --result-file "/downloads/webtoon/latest-result.json"
```

報告包含來源快照與 cursor 證據、預期章節、未完成章節、逐頁結果、實際嘗試次數、驗證等級與錯誤分類。對外可分享欄位中的 URL 會移除認證與非識別用途的 query；內部 manifest 為恢復需要保存原始來源 URL，應視為本地狀態。

`--result-file -` 改為 stdout JSON，無論成功失敗都不建立報告檔；失敗的章節恢復資料仍保留。進度與診斷走 stderr。`result_delivered` 表示結果已交付；`report_written` 只表示磁碟報告寫入。`retryable` 是結構化重試建議，不應以錯誤字串推定。

`--inspect` 可唯讀取得完整來源清單、cursor 證據與 `publication_status`（`completed`、`ongoing`、`unknown`），不建立工作區。公開清單章數只代表目前可用內容，不能單獨證明作品完結或本地歷史完整。

```bash
webtoon-downloader "WEBTOON_URL" --separate --out "/downloads/webtoon" \
  --retry-failed "/downloads/webtoon/latest-result.json"
```

`--retry-failed` 驗證報告作品身分與原本範圍，重新核對原範圍內的所有章節；已完成鄰章只驗證，符合條件時不重抓圖片。不能與 `--start`、`--end`、`--latest` 合用，也不會直接重放報告中的本地路徑或任意 URL。

若原本連來源清單都未能建立，報告沒有已知章節範圍，請重新執行原下載命令；`--retry-failed` 不會猜測這類報告的範圍。

預設讓其他獨立章節完成後彙整部分失敗。`--fail-fast` 則在首個章節失敗後取消其餘工作並完成收尾；它仍是下載失敗，不會誤標為使用者 Ctrl+C。

| Exit code | 含義 |
| --- | --- |
| `0` | 指定範圍全部完成、收尾與指定結果輸出成功 |
| `1` | 部分完成、來源完整性未知、校驗／發布／報告失敗等執行錯誤 |
| `2` | CLI 使用方式或參數錯誤 |
| `130` | SIGINT／Ctrl+C 中止 |
| `143` | SIGTERM 中止 |

排程應以 exit code 與 JSON 判斷，不解析 log 字串，也不以最大資料夾編號推算完成進度。

## 儲存與 NAS 資源

- `images --separate`：整章在暫存資料夾完成驗證後，以同檔案系統 rename 發布。
- 平面圖片輸出：先驗證全部暫存圖片，再逐檔發布；檔案系統不提供多檔交易，因此消費端必須以章節 manifest／報告完成狀態判斷整章可用。額外的同章圖片不會被靜默接受或刪除。
- ZIP／CBZ／PDF：先完成及驗證封裝，再以不覆寫目標的 hard link 發布。目標檔案系統需要支援 hard link；不支援時會準確失敗，不退回可能覆寫既有資料的做法。
- 目標與工作區需位於同一檔案系統，且 advisory lock 必須在實際 NAS volume 上可靠運作。程式拒絕 symlink 路徑與越出工作區的 state 路徑。
- 預設成功後清除已確認屬於本作品的工作區；失敗或中斷保留。遇到其他未完成工作、無法辨識內容、symlink 或唯一舊檔備份時，停止自動清理並回報非零，不刪除這些資料。`--keep-workspace` 可明確保留成功快取。

預設 `--max-image-bytes 67108864`、`--max-image-pixels 100000000`、`--minimum-free-bytes 67108864`。較小記憶體的 NAS 可降低頁面併發與像素上限，例如：

```bash
webtoon-downloader "WEBTOON_URL" --separate --resume --out "/downloads/webtoon" \
  --concurrent-chapters 1 --concurrent-pages 4 --max-image-pixels 25000000
```

圖片傳輸、CPU 工作池與章節併發都有上限。超限會清楚失敗，不截斷圖片或跳過頁面後算成功。

## Python API

`await download_webtoon_result(options)` 是完整可靠性流程，回傳 `BatchResult`。相容入口 `await download_webtoon(options)` 回傳成功產物路徑；有未完成內容時拋出 `WebtoonDownloadError`。

`WebtoonDownloader`、`ChapterDownloader` 與各 writer 是低階組合元件，保留既有介面，不單獨提供跨章節 manifest／報告交易保證。應用程式需要本文件的完整交付契約時，使用上述公開入口。

完整性保證涵蓋當次可驗證、可存取的來源清單。API 沒有原子快照版本，來源在檢查後改變、同 URL 的內容被服務端替換、或未公開／已移除的內容，無法僅由本地校驗證明；程式會檢查取得的清單變動與實際下載產物，不宣稱超出來源證據的完整性。
