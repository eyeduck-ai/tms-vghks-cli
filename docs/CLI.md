# CLI 參考

日常使用建議從短指令開始；群組指令則用於診斷、匯出與除錯。第一次從 GitHub 下載、安裝與設定帳號時，請先看 README 的快速啟動。

## 日常指令

```powershell
uv run tms-vghks-cli sign-in
uv run tms-vghks-cli pending
uv run tms-vghks-cli completed
uv run tms-vghks-cli course <course-id-or-url>
uv run tms-vghks-cli go --dry-run
uv run tms-vghks-cli go --quiz auto
```

`pending` 是可選檢查；`go` 會自己讀取待完成課程。使用 `go --dry-run` 可以預覽處理計畫，不會完成課程或送出資料。`go` 預設為 `--backend requests`，會並行處理多門課程，並維持每門課內的項目順序。閱讀/影片時間會用分段 watchTime replay 累積。`--quiz auto` 代表明確授權 CLI 自動嘗試測驗 backend 與題庫流程。

## Human 與 Agent 模式

當 stdin 和 stdout 都是 TTY 時，指令預設為 human mode：輸出文字，必要時允許互動輸入。否則預設為 agent mode：輸出 JSON 且不要求互動。可用 `--human` 或 `--agent` 覆寫自動判斷。

```powershell
uv run tms-vghks-cli pending --agent
```

## 登入

```powershell
uv run tms-vghks-cli auth status
uv run tms-vghks-cli auth login --headless
uv run tms-vghks-cli sign-in --label account1
uv run tms-vghks-cli auth ocr-test --image captcha.jpg --profiles v6-small,v6-tiny
uv run tms-vghks-cli auth diagnostics --accounts .tms_accounts.toml --label account1
uv run tms-vghks-cli auth error-probes --accounts .tms_accounts.toml --backend both --scenarios all
```

Requests login 會在 `.tms_session/accounts/<label>` 儲存每個帳號的 session bundle。驗證碼辨識會先嘗試本機 PaddleOCR SDK；如果 `.tms_accounts.toml` 設定 `[ocr].paddleocr_api_token`，本機 OCR 失敗後會再嘗試 PaddleOCR API。OCR 失敗時會刷新 captcha 重試，最後才進入互動式手動輸入。

單帳號使用時可省略 `--accounts`。若沒有 CLI 帳密且預設 `.tms_accounts.toml` 不存在或無法使用，human mode 會在 stderr 顯示警告並提示輸入 TMS 帳號密碼。若明確指定 `--accounts PATH`，檔案無法讀取或解析時會直接失敗。

## 課程

```powershell
uv run tms-vghks-cli pending
uv run tms-vghks-cli courses list completed --login-method saved
uv run tms-vghks-cli course 5416
uv run tms-vghks-cli courses inspect https://tms.vghks.gov.tw/course/5416 --backend playwright
```

需要比對 backend 行為時可用 `--backend requests|playwright|hybrid`。預設是 `requests`。

## 自動化

```powershell
uv run tms-vghks-cli go --quiz auto
uv run tms-vghks-cli go --label account1 --dry-run
uv run tms-vghks-cli go --backend hybrid --quiz auto --survey neutral
```

重要預設值：

- `--backend requests`
- `--concurrency 4`
- `--max-concurrency 8`
- `--survey neutral`
- `--quiz confirm`

測驗策略：

- `--quiz confirm`：保守預設；需要確認的測驗送出路徑會先詢問。
- `--quiz auto`：明確授權自動作答，順序是可信題庫、Gemini、deterministic heuristic。
- `--quiz skip`：略過測驗項目。

閱讀/影片自動化會等待項目要求時間，並依觀察到的 `ReadLog.recordTime` interval 送出 watchTime。送出後會重新讀取課程內容驗證進度。

## 進階選項

日常 help 只顯示常用選項：`--human`、`--agent`、`--label`、`--quiz`、`--survey` 與 `--dry-run`。進階選項如 `--accounts`、`--account`、`--password`、`--session-dir`、`--backend`、concurrency controls、transient retry options 與 debug/fallback switches 仍保留給腳本、多帳號執行與診斷使用。需要完整進階介面時，請看群組指令的 help。

## 診斷

```powershell
uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --course 5416 --item-order 2 --wait-seconds 600 --force-watch-time
uv run tms-vghks-cli diag forms --accounts .tms_accounts.toml --kind both --scope completed --probe-only --candidate-limit 3
uv run tms-vghks-cli diag kexam-records --accounts .tms_accounts.toml --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613
uv run tms-vghks-cli diag quiz-resubmit --accounts .tms_accounts.toml --course 5416 --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613 --quiz auto
uv run tms-vghks-cli diag network 5416 --item-order 1 --action read-wait --wait-ms 10000 --login-method saved
uv run tms-vghks-cli diag compare --accounts .tms_accounts.toml --label account1 --detail-limit 1
uv run tms-vghks-cli diag parity --observations .tms_session/network_observations.jsonl --output REQUESTS_PARITY_REPORT.md
```

Playwright 診斷用來收集瀏覽器證據，再將穩定的 endpoints、tokens、欄位名稱與驗證行為移植回 requests。

## 題庫

```powershell
uv run tms-vghks-cli bank export --login-method saved --output .tms_private_exports/question-bank.jsonl --include quiz,survey --allow-private-export
uv run tms-vghks-cli bank probe --login-method saved --historical-quiz-bank --output .tms_private_exports/question-bank-history.jsonl --markdown .tms_private_exports/question-bank-history.md --allow-private-export
uv run tms-vghks-cli bank build --history .tms_private_exports/question-bank-history.jsonl
```

Private exports 會放在 `.tms_private_exports/`，此目錄會被 git 忽略。這些檔案可能包含 account labels、raw collection provenance 與詳細診斷證據。Root 層的 `question-bank-YYYYMMDD.jsonl` 是 `--question-bank latest` 使用的 shared rich question bank；可以提交，並保留題目、選項、答案、分數、作答時間、record ids、redacted record URLs 與 verification metadata。`bank build` 會阻擋嚴重秘密，例如 passwords、API keys、cookies/sessions、authorization headers、captcha data、hidden fields，以及未遮罩的 `ajaxAuth`/`key`/`token` URL values。
