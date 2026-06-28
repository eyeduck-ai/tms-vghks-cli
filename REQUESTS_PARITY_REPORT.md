# Playwright vs Requests Parity Report

## Summary

- Requests covers authenticated read paths, session reuse, course listing, course detail, and some result probing.
- Requests can now replay the observed ReadLog/watchTime flow for reading/video time accumulation when course-detail verification increases.
- KExam requests record reading, completed-course read-only probing, and resubmit verification are live-verified; generic quiz/survey form submit remains partial because raw HTML may miss rendered-only fields.

## Status Counts

- `equivalent`: 3
- `partially_equivalent`: 6
- `not_equivalent_yet`: 0

## Parity Matrix

| Feature | Playwright entrypoints | Requests entrypoints | Required HTTP conditions | Status | Gaps | Verification |
|---|---|---|---|---|---|---|
| login_session | TmsSession.ensure_login<br>TmsSession.login_playwright_with_ocr<br>TmsSession.sync_cookies_to_requests<br>PlaywrightBackendTools.auto_login_tms<br>PlaywrightBackendTools.ensure_authenticated<br>PlaywrightBackendTools.check_status<br>PlaywrightBackendTools.recover_transient_error | TmsSession.prepare_requests_login<br>TmsSession.submit_requests_login<br>run_batch_requests_login<br>TmsSession.sync_cookies_to_browser<br>RequestsBackendTools.auto_login_tms<br>RequestsBackendTools.ensure_authenticated<br>RequestsBackendTools.check_status<br>RequestsBackendTools.recover_transient_error | Login CSRF/captcha fields are available from the login page.<br>The requests session receives the same authenticated cookie bundle.<br>Multi-login responses can be handled without browser-only interaction. | `equivalent` | Manual browser login remains useful when captcha or login UI behavior changes. | Run requests account login and then list courses with the saved account session.<br>Run Playwright OCR login for the same account and compare logged-in status plus course counts. |
| course_list_and_detail | TmsSession.list_pending_courses_playwright<br>TmsSession.list_completed_courses_playwright<br>TmsSession.get_course_detail_playwright<br>PlaywrightBackendTools.list_pending_courses<br>PlaywrightBackendTools.list_completed_courses<br>PlaywrightBackendTools.get_course_detail | TmsSession.list_pending_courses_requests<br>TmsSession.list_completed_courses_requests<br>TmsSession.get_course_detail_requests<br>TmsSession.use_backend<br>TmsSession.backend_tools<br>RequestsBackendTools.list_pending_courses<br>RequestsBackendTools.list_completed_courses<br>RequestsBackendTools.get_course_detail | Pending/completed/detail pages return parseable HTML to authenticated GET requests.<br>Course rows expose the same ids, titles, progress, item states, results, and detail URLs. | `equivalent` | If TMS moves fields behind client-side rendering, requests parsing may become partial. | Compare requests and Playwright list outputs for pending and completed courses.<br>Compare requests and Playwright detail outputs for at least one representative course. |
| reading_video_completion | TmsRunner.run_reading_playwright<br>TmsRunner.start_reading_playwright<br>TmsRunner.finish_reading_playwright<br>PlaywrightBackendTools.complete_item<br>PlaywrightBackendTools.complete_reading | TmsRunner.run_reading_requests<br>TmsRunner.start_reading_requests<br>TmsRunner.finish_reading_requests<br>run_requests_watch_time<br>diag reading<br>RequestsBackendTools.complete_item<br>RequestsBackendTools.complete_reading | The course detail activity button exposes a normal checkPassPrevious AJAX entrypoint or a direct media URL.<br>The media HTML contains ReadLog recordUrl with logID, timing, _lock, ajaxAuth, and recordTime.<br>The runner replays watchTime in recordTime-sized intervals for long waits.<br>Course detail confirms result_seconds increased after the requests flow. | `partially_equivalent` | Verified for ReadLog/watchTime reading/video accumulation, not for every possible media template.<br>Already-passed reading/video rows short-circuit without watchTime POST in normal automation; diagnostics can force a watchTime POST.<br>Browser-only focus, audit, duplicate-session, or alternate completion flows still require Playwright fallback. | Run diag reading-playwright or diag network to confirm the page uses watchTime.<br>Run diag reading and verify course detail result_seconds increases; add --force-watch-time for already-passed diagnostic checks. |
| survey_submission | TmsRunner.run_survey_playwright<br>validate_playwright_forms<br>PlaywrightBackendTools.complete_item<br>PlaywrightBackendTools.complete_survey | TmsRunner.run_survey_requests<br>classify_activity_form_requests<br>run_survey_requests_submit<br>diag forms<br>RequestsBackendTools.complete_item<br>RequestsBackendTools.complete_survey | The direct activity HTML exposes all fillable fields, hidden fields, and submit action.<br>CSRF/ajax tokens can be derived from normal HTML or captured network metadata.<br>Submitted answers can be verified from refreshed course detail. | `partially_equivalent` | Requests survey submit can auto-attempt normal HTML form POSTs, but is not fully equivalent to Playwright rendered-DOM submission.<br>Rendered-only fields or JavaScript-mutated state may be missing from raw HTML.<br>Contenteditable survey fields remain Playwright-only. | Capture or compare the same survey through Playwright rendered DOM and requests raw HTML.<br>Run diag forms --probe-only --candidate-limit N on completed courses before any live submit attempt.<br>Run diag forms against an explicitly authorized survey and verify course detail. |
| quiz_submission | TmsRunner.run_quiz_playwright<br>collect_quiz_questions<br>apply_quiz_answers<br>PlaywrightBackendTools.complete_item<br>PlaywrightBackendTools.complete_quiz | TmsRunner.run_quiz_requests<br>classify_activity_form_requests<br>run_quiz_requests_submit<br>diag forms<br>RequestsBackendTools.complete_item<br>RequestsBackendTools.complete_quiz | The quiz form HTML exposes questions, options, required groups, hidden fields, and submit action.<br>The answer payload and tokens are captured from a normal browser submit.<br>The result page or course detail verifies pass/fail after submit.<br>For KExam, the exam page exposes takeExam, confirmRecord, submitExam, and record-list endpoints. | `partially_equivalent` | KExam record read, answer-record parsing, read-only completed-course probe, and resubmit verification are live-verified in requests.<br>Generic non-KExam quiz submit remains limited to raw HTML standard forms.<br>Question DOM may still require JavaScript state reflection before parsing selected answers. | Run diag kexam-records and diag quiz-resubmit against course 5416 exam 10613.<br>Capture or compare Playwright extracted questions with requests form classification.<br>Run diag forms --probe-only --candidate-limit N on completed courses before any live submit attempt.<br>Run diag forms against an explicitly authorized quiz and verify course detail/result. |
| kexam_records_resubmit | read_kexam_exam_page_playwright<br>run_playwright_quiz_resubmit_diagnostic<br>probe_kexam_attempt_with_page | read_kexam_exam_page_requests<br>run_requests_kexam_resubmit_diagnostic<br>probe_kexam_attempt_requests<br>diag kexam-records<br>diag quiz-resubmit | The exam page exposes record modal metadata, best-score record URL, and takeExam entrypoint.<br>The take page exposes confirmRecord, submitExam, submittedRedir, record, and questionData.<br>After submit, the record list or best record changes and can be verified. | `equivalent` | Equivalent for KExam exam pages only; generic non-KExam engines stay under quiz_submission. | Run Playwright/capture and requests KExam record diagnostics against course 5416 exam 10613.<br>Run diag quiz-resubmit and verify a new or updated record id. |
| question_bank_history_export | export_question_bank_playwright<br>export_historical_quiz_bank_playwright<br>probe_activity_playwright<br>probe_kexam_attempt_with_page | export_question_bank<br>fetch_result_modal_summary<br>probe_activity_requests<br>probe_kexam_attempt_requests | Completed activity rows expose a direct result modal URL or detail URL.<br>KExam record URLs are discoverable and fetchable with the authenticated requests session.<br>Question and answer DOM is present in raw HTML, or metadata-only export is acceptable. | `partially_equivalent` | Playwright can click review/result labels when no direct URL is present.<br>Playwright can reflect checked/disabled/value state into DOM before parsing. | Run requests export in probe-only mode and compare issue counts with Playwright probe output.<br>For known KExam record URLs, compare question count and selected/correct answer metadata. |
| form_validation_classification | validate_playwright_forms<br>classify_form_html after rendered-page capture | TmsSession.fetch_activity_html<br>classify_activity_form_requests<br>fetch_activity_html_requests<br>RequestsBackendTools.fetch_activity_html | The raw activity/result HTML contains the same form controls as the rendered page.<br>Checked, disabled, textarea, and contenteditable values are present without JavaScript reflection. | `partially_equivalent` | Rendered-only state can make requests classification undercount fields or answers.<br>Requests classification intentionally does not click or submit anything. | Compare classification, field counts, question count, and selected answer count for the same activity. |
| scheduler_run_backend | TmsRunner.run_scheduler with OperationBackend.PLAYWRIGHT<br>TmsRunner.run_scheduler with OperationBackend.HYBRID<br>PlaywrightBackendTools.run_scheduler<br>HybridBackendTools.run_scheduler | TmsRunner.run_scheduler with OperationBackend.REQUESTS<br>TmsRunner.run_item_requests<br>RequestsBackendTools.run_scheduler<br>RequestsBackendTools.run_course | Both backends can fetch pending courses and refresh each course detail after every item.<br>Each course worker owns an isolated session/context when real concurrency is used.<br>Live mutation requires the per-item endpoint conditions listed above. | `partially_equivalent` | Requests backend can attempt full course completion through watchTime, survey submit, and quiz/KExam submit.<br>Hybrid remains available for requests-first fallback on unverified media templates or browser-only learning-state flows. | Run the requests backend on pending courses and verify course_runs, item_results, summary, and errors.<br>Run Playwright or hybrid with --concurrency > 1 and verify separate course workers do not reorder items within a course. |

## Read-Only Verification Commands

```powershell
uv run tms-vghks-cli auth requests-login --accounts .tms_accounts.toml --label account1
```
```powershell
uv run tms-vghks-cli courses list pending --accounts .tms_accounts.toml --label account1
```
```powershell
uv run tms-vghks-cli courses list pending --accounts .tms_accounts.toml --label account1 --backend playwright
```
```powershell
uv run tms-vghks-cli courses list completed --accounts .tms_accounts.toml --label account1
```
```powershell
uv run tms-vghks-cli courses list completed --accounts .tms_accounts.toml --label account1 --backend playwright
```
```powershell
uv run tms-vghks-cli courses inspect <course-url-or-id> --accounts .tms_accounts.toml --label account1
```
```powershell
uv run tms-vghks-cli courses inspect <course-url-or-id> --accounts .tms_accounts.toml --label account1 --backend playwright
```
```powershell
uv run tms-vghks-cli diag compare --accounts .tms_accounts.toml --label account1 --detail-limit 1
```
```powershell
uv run tms-vghks-cli bank export --login-method saved --probe-only
```
```powershell
uv run tms-vghks-cli bank probe --login-method saved --course-limit 1 --activity-limit 3
```
```powershell
uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action open-only --login-method saved --output .tms_session/network_observations.jsonl
```
```powershell
uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action read-wait --wait-ms 10000 --login-method saved
```
```powershell
uv run tms-vghks-cli diag network <course-url-or-id> --item-order 1 --action form-open --login-method saved
```
```powershell
uv run tms-vghks-cli diag reproduction --input .tms_session/network_observations.jsonl
```
```powershell
uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --label account1 --wait-seconds 60
```
```powershell
uv run tms-vghks-cli diag reading --accounts .tms_accounts.toml --label account1 --wait-seconds 60 --force-watch-time
```
```powershell
uv run tms-vghks-cli diag forms --accounts .tms_accounts.toml --label account1 --kind both --scope completed --probe-only --candidate-limit 3
```

## Live Mutation Manual Protocol

1. Choose one pending reading/video, quiz, or survey item with explicit permission to test it.
2. Run diag network for the item and review only sanitized method, URL, header key, form key, and response summary data.
3. Implement requests replay only for endpoints proven by that normal browser flow.
4. Wait the real required time or watch interval for reading/video items.
5. Verify success by re-reading course detail through requests.

## Assumptions

- Requests is the default read and live automation backend and can attempt verified reading/video watchTime replay.
- Hybrid is an explicit requests-first fallback backend while quiz/survey requests submit continues accumulating live verification cases.
- No requests path should fake elapsed time, skip validation, or infer completion without course-detail verification.
- Survey choice fields use the middle option, and survey text/input/textarea fields use the fixed value 無.
