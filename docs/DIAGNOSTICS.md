# 開發者診斷

這些指令是給維護者驗證登入、endpoint 行為、requests/Playwright parity 與 live TMS regression 使用。一般課程自動化請從 [CLI.md](CLI.md) 開始。

## 登入除錯

```powershell
uv run tms-vghks-cli auth ocr-test --image captcha.jpg --profiles v6-small,v6-tiny
uv run tms-vghks-cli auth diagnostics --accounts .tms_accounts.toml --label account1
uv run tms-vghks-cli auth error-probes --accounts .tms_accounts.toml --backend both --scenarios all
uv run tms-vghks-cli auth requests-prepare --session-dir .tms_session --captcha-path .tms_session/captcha.jpg
uv run tms-vghks-cli auth requests-submit --session-dir .tms_session --account EMPLOYEE_ID --password PASSWORD --captcha 1234
uv run tms-vghks-cli auth requests-load-state --session-dir .tms_session
uv run tms-vghks-cli auth requests-auto --accounts .tms_accounts.toml --label account1
uv run tms-vghks-cli auth requests-probe-wrong-captcha --account EMPLOYEE_ID --password PASSWORD
uv run tms-vghks-cli auth requests-batch --accounts .tms_accounts.toml
```

診斷 captcha/login state transitions 時使用 low-level requests 指令。一般 saved session 請優先使用主要 CLI 文件中的 `auth requests-login`。帳號設定檔不再接受 `captcha_mode`；只有在強制診斷登入路徑時，才使用一次性的 `--captcha-mode manual|paddleocr-sdk`。Captcha 辨識一般會依序嘗試本機 PaddleOCR SDK、可選的 `[ocr].paddleocr_api_token`，最後才進入手動 fallback。

## Requests 診斷

```powershell
uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --course 5416 --item-order 2 --wait-seconds 600 --force-watch-time
uv run tms-vghks-cli diag forms --accounts .tms_accounts.toml --kind both --scope completed --probe-only --candidate-limit 3
uv run tms-vghks-cli diag kexam-records --accounts .tms_accounts.toml --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613
uv run tms-vghks-cli diag quiz-resubmit --accounts .tms_accounts.toml --course 5416 --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613 --quiz auto --question-bank latest
```

`--quiz auto` 會先使用可信題庫答案，接著在 `.tms_accounts.toml` 有設定 `[gemini]` 時使用 Gemini，最後才使用內建 deterministic heuristic。`diag forms --probe-only` 若回報 `form_summary.kexam = true`，代表該課程測驗項目由 KExam engine 支援；KExam records 與 submit probes 就應視為該測驗項目的具體實作證據。

## Network 與 Parity

```powershell
uv run tms-vghks-cli diag network 5416 --item-order 1 --action read-wait --wait-ms 10000 --login-method saved
uv run tms-vghks-cli diag compare --accounts .tms_accounts.toml --label account1 --detail-limit 1
uv run tms-vghks-cli diag parity --observations .tms_session/network_observations.jsonl --output REQUESTS_PARITY_REPORT.md
uv run tms-vghks-cli diag reproduction --input .tms_session/network_observations.jsonl
```

將 endpoint path 推進一般自動化前，請使用 parity 與 reproduction output 驗證 requests 行為。

## Playwright 診斷

```powershell
uv run tms-vghks-cli diag reading-playwright --accounts .tms_accounts.toml --course 5416 --item-order 2
uv run tms-vghks-cli diag playwright-forms --login-method saved --course-limit 2 --activity-limit 3
uv run tms-vghks-cli diag playwright-kexam-records --login-method saved --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613
uv run tms-vghks-cli diag playwright-quiz-resubmit --login-method saved --course 5416 --exam-url https://tms.vghks.gov.tw/course/5416/exam/10613 --quiz auto --question-bank latest
```

Playwright 診斷用來收集瀏覽器證據，再將穩定的 endpoints、tokens、欄位名稱與驗證行為移植回 requests。
