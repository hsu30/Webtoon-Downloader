# Webtoon Downloader

Webtoon Downloader 是用於下載 Webtoons 公開章節的 Python CLI，支援圖片、ZIP、CBZ 與 PDF。

本文件描述此倉庫的安裝方式、下載與恢復流程、限制及開發介面。專案由個人維護，使用者可依需要自行使用或修改。

## 從這裡開始

| 需求 | 文件 |
| --- | --- |
| 安裝、更新或從源碼執行 | [開始使用](getting-started.md) |
| 範圍、命名、格式及參數 | [CLI 參數](cli.md) |
| 重試、校驗、續傳及結果報告 | [可靠下載與恢復](download-reliability.md) |
| 排錯與已知限制 | [常見問題](faq.md) |
| 理解下載流程與擴充界面 | [架構](architecture.md) |
| 執行測試與本地建置 | [開發與測試](development.md) |
| Python 模組參考 | [API](modules.md) |

## 交付原則

只有指定範圍的來源清單已核對、所有預期章節與圖片通過驗證、產物完成發布且報告保存成功，CLI 才以 `0` 結束。

驗證範圍受來源公開介面限制，不代表可以取得 Daily Pass、app-only 或其他無法存取的內容。

## 原始碼與回報

- [倉庫](https://github.com/hsu30/Webtoon-Downloader)
- [版本 tag](https://github.com/hsu30/Webtoon-Downloader/tags)
- [Issues](https://github.com/hsu30/Webtoon-Downloader/issues)

本專案源自 [Zehina/Webtoon-Downloader](https://github.com/Zehina/Webtoon-Downloader)，原始授權與著作權聲明保留於倉庫的 `LICENSE`。
