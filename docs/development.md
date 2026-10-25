# 開發與測試

## 本地環境

```bash
git clone https://github.com/hsu30/Webtoon-Downloader.git
cd Webtoon-Downloader
uv sync --locked
```

預設 dependency groups 包含開發與文件工具。只執行下載器時可使用 `--no-default-groups`；工具指令見[開始使用](getting-started.md)。

需要提交前 hooks 時，另行執行：

```bash
uv run --locked pre-commit install
```

## 驗證

```bash
uv lock --check
uv run --locked ruff check --no-fix webtoon_downloader tests
uv run --locked mypy --no-incremental webtoon_downloader
uv run --locked pytest -p no:cacheprovider -q
uv run --locked mkdocs build --strict
```

`make check` 另外執行 pre-commit 與 deptry。`make test` 產生 coverage；tox 對應 Python 3.10 到 3.14。

網路測試標記為 `network`，預設排除；需要實際連線時明確使用 `pytest -m network`。一般回歸使用程式內的合成回應、fake transport 與小型有效圖片，不需要額外下載 fixtures。

測試會保留診斷資料於 `test_folder/`，此目錄不追蹤。要保留 pytest 的檔案測試輸出，可指定一個尚不存在的目錄：

```bash
uv run --locked python -c "from pathlib import Path; Path('test_folder').mkdir(exist_ok=True)"
uv run --locked pytest -p no:cacheprovider -q --basetemp "test_folder/test-run-001"
```

重跑時使用新的目錄名稱，避免 pytest 清理已存在的 basetemp。

## 修改位置

- CLI／參數：`cmd/cli.py`、`core/webtoon/downloaders/options.py`。
- 來源與解析：`core/webtoon/{api,fetchers,extractor,inventory}.py`。
- 可靠性流程：`core/retry.py`、`core/webtoon/downloaders/{reliable,state,artifacts,result}.py`。
- 圖片與儲存：`transformers/`、`storage/`。

上述路徑均位於 `webtoon_downloader/`。完整架構見[架構](architecture.md)。

修改完整性流程時至少考慮：缺章、缺頁、錯配但數量相同、傳輸中斷、重試耗盡、取消、發布衝突、損壞恢復及無法保存報告。不要將未完成內容改判成功來避開例外。

## 建置與版本

```bash
uv build
```

建置結果位於 `dist/`。專案使用 `uv_build`；套件名稱為 `webtoon_downloader`，指令為 `webtoon-downloader`。

版本定義於 `pyproject.toml`，變更後以 `uv lock` 同步。既有 tag 保留其歷史命名；建立後續 tag 時需與套件版本對應。現有 `v2.4.0-numbertitle.3` 對應 PEP 440 版本 `2.4.0+numbertitle.3`。

不配置 PyPI 發布或套件 artifact 上傳。tag workflow 只驗證版本與建置，安裝來源使用 Git tag／commit。

## 文件

```bash
uv run --locked mkdocs serve
```

使用文件說明穩定功能、限制與可重現命令；不納入本機設定、私人協作紀錄、訂閱內容或開發過程日誌。既有本機封存文件不隨來源或文件站發行。

Pages workflow 僅可手動觸發，需先由倉庫管理者設定 GitHub Pages；一般 push 不部署。預設以倉庫 `docs/` 為文件入口，未假設已有公開文件站。

## CI

- `main.yml`：push／PR 的品質、跨 Python 版本測試與文件建置。
- `on-release-main.yml`：tag／手動版本驗證與建置，不執行發布。
- `deploy-pages.yml`：手動 Pages 部署。

這些設定不建立 GitHub Release，也不變更倉庫權限或自動發布 PyPI 套件。
