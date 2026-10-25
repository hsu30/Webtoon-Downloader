# 開始使用

## 環境需求

- Python 3.10 以上與 Git。
- uv，或支援 uv backend 的 pipx。
- 可讀寫的輸出目錄；NAS 需留意 CPU 架構、系統函式庫與檔案系統支援。

本倉庫以 Git 源碼與 tag 提供版本。不要將 PyPI 同名套件視為此倉庫的發行來源。

## 安裝使用者全域指令

uv 會建立隔離環境，並提供 `webtoon-downloader` 指令：

```bash
uv tool install --python 3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@v2.4.0-numbertitle.3"
uv tool update-shell
```

若已使用 pipx：

```bash
pipx install --backend uv --python python3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@v2.4.0-numbertitle.3"
pipx ensurepath
```

重新開啟終端後確認：

```bash
webtoon-downloader --version
webtoon-downloader --help
```

「使用者全域」表示安裝者可在不同工作目錄呼叫，並非所有系統帳號共用。排程應使用相同帳號及 executable 的絕對路徑。

## 更新或固定版本

先在[版本 tag](https://github.com/hsu30/Webtoon-Downloader/tags)選定版本，再把下列 `REF` 換成 tag 或完整 commit SHA：

```bash
uv tool install --force --python 3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@REF"
```

pipx：

```bash
pipx install --force --backend uv --python python3.10 "git+https://github.com/hsu30/Webtoon-Downloader.git@REF"
```

固定完整 commit SHA 可避免同名 tag 或分支變動與舊快取造成混淆。更新後重新確認 `--version`；不要同時以多個工具安裝同名指令而不確認 `PATH` 指向。

## 從源碼執行

```bash
git clone https://github.com/hsu30/Webtoon-Downloader.git
cd Webtoon-Downloader
uv sync --locked --no-default-groups
uv run --locked --no-default-groups webtoon-downloader --help
```

`uv.lock` 用於專案環境的一致安裝；uv tool／pipx 的 Git 安裝則依套件 metadata 解析依賴，不會自動套用這份 lockfile。

## 第一個下載

使用系列列表頁 URL：

```bash
webtoon-downloader "WEBTOON_URL" --start 1 --end 1 --separate --out "./downloads"
```

`--separate` 啟用每章資料夾，命名預設為 `number-title`。`--out` 是直接存放章節產物的位置，不會再自動加一層系列名稱。

未指定 `--out` 時，下載器會在目前目錄依系列名稱建立輸出目錄。可省略起訖下載整個來源清單，或用 `--latest` 只下載最新章節。

## 中斷後恢復

```bash
webtoon-downloader "WEBTOON_URL" --separate --resume --out "./downloads"
```

保留 `.webtoon-downloader/` 狀態目錄，讓工具核對來源並重用已驗證內容。詳細的報告恢復、錯誤退出與儲存限制見[可靠下載與恢復](download-reliability.md)。

## NAS 注意事項

PyMuPDF、Pillow 與 lxml 含原生元件；Python 版本符合不代表所有 NAS 架構與舊系統都能安裝相同 binary。輸出 volume 也需支援 hard link 與 advisory lock，可先以一章 CBZ 測試實際環境。

需要較低資源用量時：

```bash
webtoon-downloader "WEBTOON_URL" --separate --resume --out "/downloads/webtoon" --concurrent-chapters 1 --concurrent-pages 4 --max-image-pixels 25000000
```

Linux 排程先以 `command -v webtoon-downloader` 查出實際 executable 路徑，再用該完整路徑執行。
