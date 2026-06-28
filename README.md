# TMS VGHKS 自動化 CLI

這是給 VGHKS TMS+ 學習平台使用的 requests 優先自動化工具。一般登入、課程列表、課程內容解析、閱讀/影片時間累積、KExam 測驗送出，以及標準測驗/問卷表單都預設走 `requests`。Playwright 保留給瀏覽器登入、網路/DOM 診斷與除錯備援。

## 快速啟動

一般使用者從 GitHub 下載後，requests backend 就足夠啟動日常自動化：

```powershell
git clone https://github.com/eyeduck-ai/tms-vghks-cli.git
cd tms-vghks-cli
uv sync --no-default-groups

Copy-Item .tms_accounts.example.toml .tms_accounts.toml
notepad .tms_accounts.toml

uv run tms-vghks-cli sign-in
uv run tms-vghks-cli go --dry-run
uv run tms-vghks-cli go --quiz auto
```

在 `.tms_accounts.toml` 填入 TMS 帳號：

```toml
[[accounts]]
label = "account1"
account = "EMPLOYEE_ID"
password = "PASSWORD"
```

## 可選功能

```powershell
# 瀏覽器診斷與 Playwright 備援。
uv sync --extra playwright
uv run playwright install chromium

# 本機 PaddleOCR 驗證碼辨識。
uv sync --extra ocr-sdk

# 開發環境與所有可選功能。
uv sync --group dev --all-extras
```

如果希望測驗題庫沒有命中時使用 Gemini fallback，可在 `.tms_accounts.toml` 加上：

```toml
[gemini]
api_key = "GEMINI_API_KEY"
model = "gemini-3.5-flash"
```

在一般終端機中，CLI 預設為 human mode，會輸出文字並允許互動輸入。非 TTY 自動化環境預設為 agent mode，會輸出 JSON 且不要求互動。可用 `--human` 或 `--agent` 手動指定模式。

## CLI 指令群組

- `sign-in`、`pending`、`completed`、`course`、`go`：日常短指令。
- `auth ...`：登入、session、OCR 測試與登入診斷。
- `courses ...`：待完成/已完成課程列表與課程內容檢查。
- `diag ...`：閱讀、表單、網路、KExam、backend 比對與 parity 診斷。
- `bank ...`：私有歷史匯出/probe 與共享題庫建置。

執行 `uv run tms-vghks-cli --help` 或閱讀 [docs/CLI.md](docs/CLI.md) 可查看短指令與進階群組範例。

## Backend 補充

需要瀏覽器除錯時可用 `--backend playwright`；需要 requests 優先、失敗時退回 Playwright 的診斷情境可用 `--backend hybrid`。一般非 KExam 測驗/問卷的 requests submit 仍依賴標準 raw HTML form；若欄位只在 rendered DOM 中出現，可能需要 Playwright 診斷。問卷選項會使用中間選項，自由文字欄位填入 `無`。

Python 呼叫端可使用 `session.requests_tools()`、`session.playwright_tools()`、`session.hybrid_tools()` 或 `session.backend_tools("requests")` 取得相同 backend 功能。

## 開發檢查

```powershell
uv run python -m unittest discover -s tests
uv run python -c "import sys; before=set(sys.modules); import tms_vghks; print(sorted(m for m in set(sys.modules)-before if m.startswith('playwright')))"
git diff --check
```

實作細節與 parity 狀態記錄在 [REQUESTS_PARITY_REPORT.md](REQUESTS_PARITY_REPORT.md)。
