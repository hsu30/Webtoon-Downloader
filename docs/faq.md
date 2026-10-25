# 常見問題

## 安裝後不是這個倉庫的版本？

請從本倉庫的 Git URL 與 tag／commit 安裝，而不是直接安裝 PyPI 的同名套件。以 `command -v webtoon-downloader`（Linux）或 `Get-Command webtoon-downloader`（PowerShell）確認實際指令，再檢查 `--version`。安裝與更新命令見[開始使用](getting-started.md)。

## 指令找不到？

確認使用者工具目錄在 `PATH`，必要時執行 `uv tool update-shell` 或 `pipx ensurepath` 並重新開啟終端。排程的使用者與 PATH 可能不同，應使用安裝者帳號及 executable 的完整路徑。

## ReadTimeout、長度錯誤或 429 怎麼處理？

保留狀態目錄並以 `--resume` 再執行。可降低 `--concurrent-chapters`／`--concurrent-pages`，檢查連線或明確設定代理。工具會在有限預算內重試，但持續限流或網路中斷仍會失敗。

`403/404`、磁碟滿及權限問題不應靠不斷增加重試解決。從 JSON 報告確認失敗類型，再處理存取或環境問題。

## 有資料夾是否代表完成？

不代表。完成判定依來源清單、manifest、實際內容驗證與發布結果；外部工具不應只檢查資料夾是否存在或編號是否最大。

## 為什麼舊資料沒有直接跳過？

沒有可信 manifest 的資料，需要依當次來源逐項比對；無法可靠對應或內容不同時會保留並回報衝突。工具不會為了繼續下載而自動刪掉整章。

## quick 和 full 差在哪？

full 會完整解碼並核對內容 hash；quick 用可信既有記錄及檔案屬性加速重用，無法保證辨識同大小、同時間戳記的損壞。新下載的資料不因 quick 模式省略完整驗證。

## 缺少來源清單時，為什麼不能直接 retry-failed？

來源尚未取得時，報告沒有可核對的章節範圍，請重新執行原命令。`--retry-failed` 不會猜測範圍。

## 可以下載 Daily Pass 或 app-only 章節嗎？

本專案使用公開網頁與 API，不提供解鎖、繞過存取限制或官方 app 的認證流程。來源不可存取時會回報失敗。

## 章節數字為什麼與標題或 episode_no 不同？

下載範圍與呈現編號依來源有序清單的位置；`episode_no` 是來源識別，標題的話數是作品文字。三者不可互相替代。

## NAS 安裝或發布失敗？

請檢查 Python、CPU 架構、glibc 與原生套件支援，以及 volume 的 hard link、advisory lock、權限和空間。支援 Python 3.10 不等於支援所有舊 NAS 系統。先用一章與新的輸出目錄測試。

## 網站更新後失效？

HTML／API 結構可能變動。可到[Issues](https://github.com/hsu30/Webtoon-Downloader/issues)提供版本、環境、公開系列 URL、可重現命令及已遮蔽的錯誤資訊。請勿直接上傳訂閱設定、完整 manifest、代理認證或其他私人資料。

更完整的儲存、恢復與限制說明見[可靠下載與恢復](download-reliability.md)。
