# 架構

## 公開下載流程

```text
CLI / WebtoonDownloadOptions
  → download_webtoon_result()
  → ReliableDownloader
  → 來源章節清單與 cursor 核對
  → 固定章節範圍
  → 逐章取得 viewer 圖片清單
  → 逐頁下載、重試、解碼、暫存
  → 完整性與封裝驗證
  → 發布產物、保存 manifest 與結果報告
```

CLI 使用 `download_webtoon_result()` 取得 `BatchResult` 並設定 exit code。相容入口 `download_webtoon()` 回傳成功產物路徑；未完成時拋出 `WebtoonDownloadError`。

## 模組職責

| 模組 | 職責 |
| --- | --- |
| `cmd/cli.py`、`cmd/progress.py` | 參數、訊號處理、進度與結果呈現 |
| `core/retry.py` | 退避、429 冷卻、完整操作預算與錯誤分類 |
| `core/webtoon/client.py` | HTTP client、body 讀取、timeout、明確代理與大小限制 |
| `core/webtoon/api.py`、`fetchers.py` | 系列識別、cursor 分頁與章節資料 |
| `core/webtoon/inventory.py` | 來源完整性證據與範圍契約 |
| `core/webtoon/extractor.py` | HTML metadata 與 viewer 圖片序列 |
| `core/webtoon/downloaders/reliable.py` | 章節協調、續傳、校驗、發布與報告 |
| `core/webtoon/downloaders/state.py` | 受控路徑、狀態落地、OS lock 與檔案證據 |
| `core/webtoon/downloaders/artifacts.py` | 圖片／ZIP／CBZ／PDF 建立與格式校驗 |
| `core/webtoon/downloaders/result.py` | 預期頁面、逐頁、章節與批次結果 |
| `transformers/image.py` | 完整解碼及圖片轉檔 |

表中 `cmd`、`core`、`transformers` 均位於 `webtoon_downloader/`。

## 身分與完整性

作品以語系、系列類型與 `title_no` 識別；章節以來源 `episode_no` 識別。資料夾名稱與呈現位置不是完成身分。

每章必須逐項符合預期圖片序列，全部圖片有效、產物校驗與發布成功後才算完成。整批則需預期章節集合與完成集合一致，且結果報告保存成功。

API cursor 的終點是當次來源清單證據；不連號的 episode ID 不表示缺章。來源沒有原子快照版本，不能據此證明未公開內容或之後的服務端變更。

## 重試與收尾

重試包住完整請求與 body／解碼操作，而不是只包 HTTP transport。頁面額度在章節恢復輪次間共用，並受操作與章節時間預算限制。

取消後會等待相關任務及已開始的 worker 收尾，再釋放 writer 與 lock。章節互斥鎖會回報同章占用；不同章節的發布鎖則在章節預算內排隊。

## 狀態與發布

`.webtoon-downloader/` 保存 manifest、逐頁證據、暫存產物、備份及報告。JSON 以暫存檔與 atomic replace 更新；章節先在同一檔案系統的工作區完成驗證，再發布。

資料夾以 rename 發布；封裝以不覆寫既有目標的 hard link 發布。平面圖片沒有多檔原子交易，整章完成需以 manifest／報告為準。受管理的損壞產物可在續傳時以備份方式修復，未管理的衝突內容保留。

## 低階元件與擴充

`WebtoonDownloader`、`ChapterDownloader`、`HttpImageDownloader`、`AioWriter` 及 `DataExporter` 保留作為低階組合元件；單獨使用不具備公開流程的跨章節交易與報告保證。

新增參數、格式或來源解析時，需同時檢查公開協調流程、狀態相容性、驗證器、CLI 與測試，不能只新增 writer 就宣稱完整性交付已支援。
