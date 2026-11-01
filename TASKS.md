# 開發任務清單（TASKS.md）

依據：SPEC.md 定義功能與驗收，AGENT.md 定義工程約束。本清單只安排執行順序，不擴充功能範圍。所有任務初始均未完成；建立清單不代表開始實作。

## 執行節奏

- 一次只進行一個微任務，依編號順序執行；先確認輸入與預期結果，再實作並執行該項驗證。
- 每項以明確模組邊界交付；使用 fixture／模擬介面隔離依賴，完成後可單獨重跑驗證，不必重新爬站。
- 驗證通過才勾選完成，並在該任務下記錄驗證命令、結果及未解問題。失敗則留在當項修正，不帶著未解契約進入下一項。
- 每完成一項回報交付內容、驗證結果與下一步。無需為每項例行工作再次要求確認；遇到缺少必要資訊或來源能力不符規格時，先記錄阻礙並釐清。
- Phase 1 全部通過後才進入 Phase 2；Phase 2 最後執行一次小樣本整合驗收。常規測試離線進行，官方網站實測遵守共同限速。

## Phase 1：建立可驗證的資料與下載核心

### [x] P1-1 驗證官方來源並建立解析樣本

- **交付**：docs/source-validation.md、去敏感資訊的 HTML／PDF fixture 與預期結果；記錄入口、表單事件、分頁、附件網域及未驗證特例。
- **驗證**：以少量限速請求重現一組查詢與附件取得；確認正常結果、無結果及錯誤頁的辨識依據。未取得的樣本明列缺口，不捏造觀察結果。
- **完成條件**：足以支援下一項的來源契約已確認；不能確認的核心欄位或分頁機制列為阻礙。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `docs/source-validation.md`（入口、表單控制項、事件行為、結果結構、錯誤頁、附件標頭、缺口清單）
  - `tests/fixtures/html/`：`search_form.html`、`full_form.html`、`result_sample.html`（2 考試／75 類科／371 科目）、`result_stats_mismatch.html`、`result_noresult.html`、`result_limit_exceeded.html`、`error_notfound.html`（token 均遮罩）
  - `tests/fixtures/pdf/`：7 份官方 PDF 樣本（測驗、混合、申論、答案）
  - `tests/fixtures/expected/result_sample.expected.json`：逐筆預期紀錄與 PDF 雜湊
  - `scripts/`：來源探測、fixture 產生與自我檢查腳本（可重跑）
- 驗證命令與結果：`python scripts/verify_fixtures.py` → 全部通過（統計與列數一致、關鍵紀錄、統計不一致樣本、無結果／上限／錯誤頁辨識、token 遮罩、7 份 PDF 檔頭與 SHA-256 相符）
- 已確認的來源契約：
  - 入口 `https://wwwq.moex.gov.tw/exam/wFrmExamQandASearch.aspx`；附件同網域 `/exam/wHandExamQandA_File.ashx?t=&code=&c=&s=&q=`，無需 Cookie、無重導、提供 Content-Length 與 Content-Disposition
  - 年度欄位值為西元年、顯示民國；**年度（迄）需 postback 才生效**
  - **無分頁**：單次查詢即回傳整年度全部結果（113 年 6.4 MB、3,566 科目列）；DOM 每列重複兩次需以 checkbox 去重
  - **官方 6,000 筆上限**：超過時回傳提示文字且表格 0 列，須視為「結果過大／未完整」
  - 無結果＝「查無結果」；路徑不存在為 HTTP **200** 的「您所輸入的網址並不存在！」頁
- 未解問題（已列於文件 §7）：無文字層 PDF 樣本、拆卷（`q=2`）案例、Session／ViewState 失效實測、官方統計與渲染列數差異原因（113 年差 1 類科／2 科目）、顯示模式 1／2 版面、超過兩年度的行為
- 契約調整：已於 2026-11-01 依文件 §8 的發現修正 SPEC §2／§3.1／§3.3／§7／§8、AGENT §2／§4／§8 與本檔 P1-3（無分頁、官方 6,000 筆上限、迄年 postback、完整性雙軌核對、伺服端關鍵字僅作前篩）。

### [x] P1-2 定義模型並完成純 HTML 解析

- **交付**：models.py、parser.py；考科與多附件模型、穩定 ID、原始值與正規化值、UNKNOWN 題型及證據欄位。
- **驗證**：離線測試正常列表、拆卷、多筆共用附件、缺答案、重複紀錄及結構異常；確認解析器不發送網路請求、不寫檔。
- **完成條件**：fixture 能產出預期模型；缺答案維持 UNKNOWN，結構異常不被當成零結果。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `app/models.py`：`QuestionType`／`AssetRole`／`AssetScope`／`ParseStatus`、`TypeEvidence`、`ExamAsset`、`ExamRecord`、`OfficialCounts`、`ParsedCounts`、`ParsedPage`、`SearchFormPage`；穩定 ID（官方識別碼優先，缺少時雜湊並標記策略版本 `moex-v1`）、`normalize_text`／`compact_key`
  - `app/crawler/parser.py`：`parse_result_page`、`parse_search_form`、`decode_html`；只接受 HTML 文字，不做網路／檔案／PDF I/O
  - `tests/test_parser.py`（23 項）、`tests/conftest.py`（fixture 路徑與合成列建構器）、`pytest.ini`、`requirements.txt`、`requirements-dev.txt`
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **23 passed**（真實樣本 2 考試／75 類科／371 科目、狀態 OK、無警告）
- 測試涵蓋：正常列表與計數、穩定 ID 與正規化鍵、試題／答案附件、考試層級附件不混入考科、無答案維持 UNKNOWN、重複列合併（合成）、拆卷多試題（合成 q=1/q=2）、跨紀錄共用 URL（合成）、無 checkbox 類科列、統計不符、無結果、6,000 筆上限、錯誤頁（HTTP 200）、缺表格／有統計無列、表單 hidden fields 與 postback 目標、編碼解碼、解析器無網路與檔案 I/O（AST 檢查）
- 過程中發現並已處理：官方附件型別 `t=M`（更正答案），已納入 `CHOICE_ANSWER` 角色、保留原始標籤「更正答案」，並同步更新 `docs/source-validation.md`、SPEC §4.2、AGENT §5
- 未解問題：官方拆卷（`q=2`）仍無實例，拆卷測試使用合成標記；PDF 題型證據待 P1-4

### [x] P1-3 完成有完整性標記的查詢快照

- **交付**：moex_client.py、rate_limiter.py、query_service.py，以及 manifest 的快照儲存；串行表單事件（含年度 postback）、單次全量結果的年度批次查詢、快取與查詢取消。
- **驗證**：模擬 HTTP 測試 hidden fields 更新、年度 postback、合併去重、重複列去重、6,000 筆上限提示、統計與列數不符、Session 重建、429 退避與快取時間；使用可控制時鐘驗證限速。
- **完成條件**：完整與未完整快照可明確區分；失敗不偽裝成功，取消後不再派發請求。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `app/crawler/rate_limiter.py`：在途上限（預設 2，範圍 1～4）、全域啟動間隔 0.5～1.5 秒、指數退避（基底 1 秒、上限 30 秒、含 jitter）、`Retry-After` 全域暫停；時鐘與睡眠可注入
  - `app/crawler/moex_client.py`：`MoexClient`（Session、hidden fields 回收、迄年 postback、逐跳網域驗證、重試政策、403 不重試、404 不重試、429／503 退避、錯誤頁偵測、狀態失效時重建 Session 並重播一次）
  - `app/engine/query_service.py`：`QueryService`（年度批次、合併去重、完整性彙總、24 小時 TTL 快取、強制重查、取消、進度回呼、最大 20 年安全上限）
  - `app/storage/manifest.py`：SQLite 快照儲存（`snapshots`／`snapshot_records`／`snapshot_years`，短交易、每操作獨立連線）
  - 測試：`tests/test_rate_limiter.py`、`tests/test_moex_client.py`、`tests/test_query_service.py`、`tests/test_manifest.py`；`tests/conftest.py` 新增 `FakeMoexServer`
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **61 passed**
- 涵蓋情境：啟動間隔介於 0.5～1.5 秒、跨執行緒在途上限 ≤2、退避成長與上限、Retry-After 全域暫停；迄年 postback 與最新 ViewState、同 Session 重用、錯誤頁觸發 Session 重建、403／404 不重試、5xx 重試與耗盡、重新導向到外站被阻擋、非白名單網址被阻擋；多年份合併、跨批次去重、上限／統計不符／無結果狀態、快取命中與 TTL 過期、強制重查、取消後不再派發、進度回呼、年度範圍上限；快照寫入讀回與 TTL 判定
- 對官方站點小樣本實測（113 年）：3 個請求（表單 → 迄年 postback → 查詢），得 3,566 筆紀錄，狀態正確標示「結果未完整（統計不符）」，詳見 `docs/source-validation.md` §6.1
- 未解問題：快照中的官方統計與解析列數差異仍為已知來源問題（非程式錯誤）；拆卷案例仍缺實例

### [x] P1-4 完成附件驗證與保守題型嗅探

- **交付**：pdf_inspector.py；PDF 有效性結果、首頁題型證據、未知與衝突處理、可終止的嗅探程序。
- **驗證**：離線測試明確三種題型、掃描頁、證據不足、證據衝突、HTML 偽裝 PDF、損壞 PDF 與逾時；確認不抽取第二頁。
- **完成條件**：有效但無法辨識題型的 PDF 可回傳 UNKNOWN；無效 PDF 與嗅探失敗是不同結果。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `app/engine/pdf_inspector.py`：`inspect_pdf`（有效性＋首頁文字＋證據）、`classify_first_page_text`（純函式規則）、`sniff_first_page_text`（`multiprocessing` spawn 子程序，5 秒逾時可 `terminate`／`kill`）、`is_pdf_magic`
  - `app/models.py` 新增 `PdfStatus`（有效／無效／嗅探失敗）與 `PdfInspection`
  - `tests/test_pdf_inspector.py`（18 項）
- 規則（`pdf-v1`）：無文字層 → 未確認；明確混合敘述優先（若同時有排他敘述標示衝突）；排他性敘述（全部為測驗題／申論題）→ 對應題型；題數加總等於總題數 → 測驗題；其餘維持未確認。只讀第一頁、只保留前 300 字元，不掃描後續頁面、不做 OCR。
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **79 passed**
- 涵蓋情境：明確混合／測驗／申論（排他敘述）、題數加總判定、題數不符、單獨「單一選擇題」不足、排他證據互相衝突、混合與排他並存之衝突標示、空白文字、真實樣本（混合、以題數判定測驗、未確認）、無文字層掃描檔、HTML 偽裝、截斷損壞、逾時（`SNIFF_FAILED`）、檔案不存在、檔頭位移
- 樣本實測：7 份官方 PDF 中 1 份混合題、1 份測驗題、5 份未確認（前 300 字元無排他證據），詳見 `docs/source-validation.md` §6.2
- 未解問題：無文字層的官方樣本仍未取得（測試使用自製空白 PDF）；300 字元視窗對實務樣本的覆蓋率偏低，需在 P2-5 以更多標註樣本量測未知率

### [x] P1-5 完成可恢復的附件下載與版本紀錄

- **交付**：downloader.py、manifest 附件狀態與啟動核對；串流 `.part`、大小限制、驗證、原子歸檔、雜湊、更新版本及附件層級重試。先以注入的固定安全目的路徑驗證，命名引擎於 P2-2 接入。
- **驗證**：模擬下載中斷、超限、長度不符、無長度、404、取消、完成檔略過、雜湊損壞、版本更新，以及歸檔成功但資料庫尚未提交時重啟。
- **完成條件**：只有已驗證附件標示完成；可略過完整附件並重試失敗附件，不覆寫既有版本。

**驗證紀錄（2026-11-01）**（註：本項於 P2-1、P2-2 之後補做，Phase 2 其餘任務須待本項通過後才繼續）

- 交付物：
  - `app/engine/downloader.py`：`Downloader.download`（串流至 `.parts/<hash>.part`、大小上限、Content-Length 比對、PDF 有效性嗅探、原子 `os.replace`、SHA-256、版本遞增、修復損壞檔、`force` 供更新檢查）；錯誤碼 `size-limit`／`length-mismatch`／`invalid-pdf`／`not-found`／`access-denied`／`blocked-host`／`transport-error`／`server-error`／`cancelled`／`io-error`
  - `app/storage/manifest.py`：`attachments`／`attachment_versions` 資料表、`mark_attachment_started`／`begin_attachment_verify`／`complete_attachment`／`fail_attachment`／`get_attachment`／`list_attachments`／`reconcile`
  - `app/models.py`：`AttachmentStatus`（待下載、下載中、驗證中、已完成、已略過、失敗、已取消）
  - `tests/test_downloader.py`（17 項）
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **157 passed**
- 涵蓋情境：正常歸檔與 manifest 紀錄、第二次略過（0 次請求）、檔案遭竄改後重新下載並修復原路徑、超過大小上限即中止且不留 `.part`、Content-Length 不符、缺少 Content-Length 仍驗證 PDF、串流中斷重試後失敗、404／403 不重試、500 後重試成功、非白名單網域不發請求、串流中取消、HTML 偽裝 PDF、內容更新保留舊版並產生帶雜湊的新檔名（版本 2）、歸檔成功但狀態未提交時重啟核對補記完成、殘留 `.part` 清理與中斷標記、未追蹤 PDF 回報
- 未解問題：MVP 不做位元組續傳（重新下載為附件層級）；`reconcile` 不會自動收編未追蹤檔案，僅回報清單

## Phase 2：完成篩選、歸檔與本機操作流程

### [x] P2-1 完成候選聚合與選取規則

- **交付**：filter.py；跨欄 AND、同欄 OR、簡稱回退、候選統計與可見項目批量選取。
- **驗證**：以固定快照測試多欄組合、拆卷與共用附件計數、快速過濾後全選／反選、零選取，以及選取後凍結紀錄 ID。
- **完成條件**：輸出只包含真實存在的紀錄；考科數、附件數與實際傳輸去重可分別驗證。

**驗證紀錄（2026-11-01）**

- 交付物：`app/engine/filter.py`（`FilterState`、`Candidate`／`CandidateSets`、`SelectionStats`、`FrozenSelection`；`build_candidates`、`apply_selection`、`matches`、`quick_filter`、`select_all_visible`／`clear_visible`／`invert_visible`、`selection_stats`、`can_download`、`sort_records`、`freeze_selection`／`resolve_frozen`、`expand_exam_query`）；`tests/test_filter.py`（22 項）
- 版本化簡稱表：`SHORT_NAME_VERSION = "short-names-v1"`，僅收錄語意明確的簡稱（如 `高考三級`→`高等考試三級`、`初考`→`初等考試`），未命中時回退為原字串比對，不做模糊推測
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **101 passed**
- 涵蓋情境：三欄 AND、同欄 OR、空白插入的正規化比對、年度範圍、候選份數（考試候選不受考試欄位本身影響）、快速過濾僅影響可見項目、全選／取消／反選不動到隱藏項目、零選取禁止下載、拆卷（1 考科 2 試題→附件數大於考科數）、共用 URL（檔數 2、傳輸去重 1）、簡稱展開與回退、排序穩定次鍵、凍結與解析（缺紀錄報錯、零選取不可凍結）
- 樣本統計：`result_sample.html` 371 考科／試題 371／答案 239／總檔數 610／實際傳輸去重 610
- 效能（全年度 3,566 筆、623 類科候選、1,262 科目候選）：初始候選 0.02 秒、選取 0.18 秒、重建候選 0.38 秒（本機量測，非驗收門檻）
- 未解問題：候選以「顯示值」為單位，同名的類科／科目會跨考試合併計數（符合 SPEC 以值選取的定義，UI 需標示）；來源未提供考試簡稱與等別，`exam_short_name`／`level` 仍為 `None`

### [x] P2-2 完成安全路徑與四種歸檔模式

- **交付**：path_builder.py；A／B／C／D 模板、所有變數、暫定路徑預覽、穩定 ID 消歧，以及下載器寫入前的根目錄邊界檢查。
- **驗證**：使用暫存目錄測試年度格式、未知變數、答案後綴、拆卷、保留名稱、清理碰撞、大小寫碰撞、長路徑、絕對路徑與 `..`；在目標 Windows 環境驗證 junction／符號連結逃逸。
- **完成條件**：所有最終檔案落於指定根目錄；不同附件不互相覆寫，答案一律標為「測驗答案」。

**驗證紀錄（2026-11-01）**

- 交付物：`app/engine/path_builder.py`（`PathMode`、`PlannedFile`／`PathPlan`、`validate_template`、`sanitize_segment`、`build_plan`、`ensure_within_root`／`is_within_root`、`preview_lines`）；`tests/test_path_builder.py`（39 項）
- 變數契約：`{年度}` 純民國數字、`{年度標籤}`「113年」、`{考試名稱}` 官方全名、`{考試簡稱}` 未建立對照時回退全名、`{類科}`／`{科目}` 來源值、`{等別}` 缺資料時「等別未確認」、`{題型類別}` 申論題／測驗題／混合題／未確認、`{題型}` 申論試題／測驗試題／混合試題／試題／測驗答案
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **127 passed**
- 涵蓋情境：A／B／C 預設模板與 D 自訂、全部變數渲染、簡稱回退、四種題型 token、答案一律「測驗答案」、`{題型類別}` 對答案採考科整體題型（未確認時為「未確認」）、模板拒絕（缺 `{題型}`、非 `.pdf`、未知變數、格式運算、絕對路徑、磁碟機、`..`、空目錄段、反斜線）、清理（非法字元、控制字元、尾端句點／空白、保留名稱 `CON`／`COM1`、全形字元、空名稱）、資料值中的斜線只當待清理字元、拆卷於角色後綴前插入 `q1`／`q2`、大小寫與相容正規化碰撞消歧、同名科目不覆寫、長路徑以穩定雜湊縮短、總長度上限、根目錄邊界（`..` 與 **Windows junction 逃逸**）、預覽不建立任何檔案、題型未確認標示暫定
- 實際預覽範例（根目錄 `C:/downloads`，A 模式）：
  - `國文/113_113年公務人員初等考試_初等考試_一般行政_試題.pdf（暫定，題型未確認）`
  - `國文/113_113年公務人員初等考試_初等考試_一般行政_測驗答案.pdf`
- 未解問題：`{題型類別}` 用於答案時採考科整體題型，拆卷可能使同一考科檔案分屬不同資料夾（依 SPEC 以 manifest 為準，UI 預覽需提示）；名稱本身含「試題」字樣時，消歧短 ID 插入位置以最後一次出現為準

### [x] P2-3 串接任務生命週期與進度事件

- **交付**：task_service.py；快照、篩選、下載、題型與命名串接；任務 ID、重複提交防護、狀態轉移、取消及失敗附件重試；下載階段以執行緒池有界派送併發（預設 2，範圍 1～4，全數經共用限速器）。
- **驗證**：以模擬 UI 測試完整流程、不完整快照禁止下載、重複提交、取消、重新啟動，以及試題成功但答案失敗。
- **完成條件**：部分成功顯示部分完成；重試只處理未完成附件，工作執行緒不直接呼叫 UI。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `app/engine/task_service.py`：`TaskService`（`submit`／`cancel`／`retry_failed`／`get`／`list_tasks`／`wait`）、`TaskState`／`TaskEvent`、`SnapshotIncompleteError`／`TaskStateError`／`TaskNotFound`；有界派送（同時提交數 ≤ `max_workers`，預設 2、允許 1～4）
  - `app/engine/downloader.py`：新增 `DownloadTarget.resolve_path`（嗅探後才定案檔名）、`rate_limiter`（下載期間佔用共用限速器名額）、manifest 記錄 `question_type`
  - `app/storage/manifest.py`：`attachments.question_type`
  - `app/models.py`：`TaskStatus`（待開始／查詢中／待勾選／下載中／取消中／已取消／完成／部分完成／失敗）
  - `tests/test_task_service.py`（15 項）
- 驗證命令與結果：`.\.venv\Scripts\python.exe -m pytest -q` → **172 passed**
- 涵蓋情境：完整流程（4 附件全部完成）、未完整快照直接拒絕且不派發、重複提交只建立一個任務且不重複下載、試題成功＋答案失敗→**部分完成**、重試只處理未完成附件（第 2 次嘗試只呼叫 1 次）、執行中不可重試、取消後不再派發且已完成檔案保留、取消前未開始、同時在途數受 `max_workers` 限制、非法併發數被拒、**嗅探題型改變檔名（`_測驗試題.pdf`）**、非 PDF 角色列為不支援、未知任務報錯、狀態監聽器收到進度、AST 檢查確認任務服務不依賴 UI
- 未解問題：`TaskService` 以記憶體保存任務狀態，重啟後需由 manifest 重建「已完成／未完成」清單（P2-4 會顯示並提供重試入口）；同一時間多任務共用同一個 `Downloader` 實例時，限速器仍為全域共用，符合 SPEC

### [x] P2-4 完成本機 Streamlit 操作介面

- **交付**：main.py、web_app.py；查詢表單、候選清單、統計、路徑預覽、快照時間、進度、取消與重試入口，預設監聽 localhost。
- **驗證**：使用固定快照與模擬背景任務檢查輸入錯誤、零選取、未完整快照、UI 重跑、下載進度、部分完成與重試行為。
- **完成條件**：使用者可完成查詢到歸檔流程，且介面重跑不重複啟動任務；業務邏輯留在 engine。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `app/ui/controller.py`：`AppController`（查詢、候選、快速過濾與可見項目操作、統計、預覽、開始下載、進度、取消、重試、啟動核對）；`validate_query_inputs`
  - `app/ui/web_app.py`：Streamlit 畫面（側欄下載設定、查詢表單、三個候選清單＋快速過濾、統計指標、路徑預覽表、下載進度／取消／重試、啟動核對訊息）
  - `main.py`：以 `streamlit run` 啟動，`--server.address=localhost`、預設 port 8501
  - `requirements.txt` 新增 `streamlit==1.66.1`
  - `tests/test_app_controller.py`（21 項）、`tests/test_ui_smoke.py`（5 項）
- 驗證命令與結果：
  - `.\.venv\Scripts\python.exe -m pytest -q` → **198 passed**
  - `streamlit.testing.v1.AppTest` 實際執行頁面腳本 → 0 exceptions，標題與按鈕正常渲染
  - `python main.py --server.port=8599` → `/_stcore/health` 回 `ok`、首頁 HTTP 200
- 涵蓋情境：年度輸入錯誤（起迄顛倒、超出 81～115、非正數）、零選取停用下載、未完整快照停用下載、**重複點擊／重跑只建立一個任務**、取消與重試委派、部分完成狀態與訊息、重啟後任務不存在時回傳 None、快速過濾只影響可見項目與全選／取消／反選、預覽路徑與自訂模板錯誤、模組匯入無副作用、入口參數組裝
- 未解問題：介面以 1 秒輪詢（`st.rerun`）更新進度，長下載時頁面會週期性重繪；尚未做多分頁或遠端部署（不在 MVP 範圍）

### [x] P2-5 完成整合驗收與交付說明

- **交付**：操作說明、經驗證的依賴版本、驗收紀錄與已知限制；補齊來源驗證狀態及效能樣本報告。
- **驗證**：先跑必要離線測試，再以小樣本進行官方來源端到端驗收；核對實際檔案、manifest、命名與來源關聯，並測試中斷後恢復及離線快照操作。
- **完成條件**：SPEC §8 的必要驗收都有結果；記錄樣本、日期及環境，不宣稱未測的準確率或速度。仍有阻礙時不得標記整體完成。

**驗證紀錄（2026-11-01）**

- 交付物：
  - `docs/usage.md`：安裝、啟動、操作流程、歸檔模式與變數、題型判定、疑難排解、已知限制
  - `docs/acceptance.md`：SPEC §8 五項對照、端到端實測、恢復與離線情境、效能取樣、題型樣本統計、未測範圍
  - `scripts/e2e_check.py`（可重跑的端到端驗收，含任務服務流程）、`scripts/bench_local.py`（離線效能取樣）
  - `requirements.txt` 鎖定經驗證版本（httpx 0.28.1、beautifulsoup4 4.15.0、lxml 6.1.3、pypdf 6.21.4、streamlit 1.66.1）
  - 契約修正：新增 `ParseStatus.OK_WITH_DIFF`「完整（統計差異）」，同步更新 SPEC §3.3、AGENT §4
- 驗證命令與結果：
  - 離線：`.\.venv\Scripts\python.exe -m pytest -q` → **200 passed**；`scripts/verify_fixtures.py` → 全部通過
  - 端到端（113 年、初等一般行政國文，共 9 個步驟）：查詢 3 次請求／10.94 秒／3,566 筆、狀態「完整（統計差異）」；下載試題 602,464 bytes、答案 37,561 bytes；命名符合 A 模式；manifest 題型「測驗題」與「未確認」正確；重跑 0 請求全部略過；刪檔後 1 次請求修復；離線快取 0 請求；啟動核對 0／0／0／0；任務服務「完成（2/2）」且略過 2
  - 效能樣本：首次查詢 10.94 秒、快取命中 105 ms、建立候選 24 ms、三欄全選 168 ms、PDF 檢查 211～271 ms／份
- 阻礙處理：真實快照原恆為「未完整」導致無法下載；經決議改以「完整（統計差異）」處理（僅統計文字與列數不一致、且無解析失敗／上限／錯誤頁時允許下載並顯示兩組數字；合併重複列或跨批次去重對不上仍標示未完整）。端到端已驗證可完成下載。
- 未解／未測：無文字層官方 PDF、拆卷（`q=2`）案例、顯示模式 1／2、一次超過兩年度、macOS／Linux 與 CI；效能數字僅代表本機與當次網路環境

### [x] P2-6 第一階段類科／科目過濾（2026-11-01 追加需求）

- **需求**：使用者在查詢表單就要能用「類科」「科目」關鍵字縮小範圍，第二階段只看到符合的候選。
- **交付**：
  - `app/crawler/moex_client.py`：`search(keywords=…)` 走官方進階查詢（`ibtnFull` 切換 → `btnQuery` 查詢），空白欄位不送出、關鍵字變更時重建表單、切回簡易查詢時重新取得表單；`reset()` 同時清除進階狀態
  - `app/engine/filter.py`：`records_matching_keywords`（NFKC＋去空白的子字串比對，空關鍵字不限制）
  - `app/engine/query_service.py`：關鍵字驅動進階查詢；診斷仍記錄**完整解析**結果，再以本機函式過濾紀錄；首批進階失敗時整批回退簡易查詢＋本機過濾並記錄警告；統計核對拆成「來源統計 vs 解析」與「合併去重 vs 批次總和」兩層
  - `app/ui/web_app.py`：查詢表單新增類科／科目欄位與說明；`app/ui/controller.py`：`query_scope_text()` 顯示年度與已套用關鍵字
  - 文件同步：SPEC §3.1、AGENT §4、`docs/source-validation.md` §6.3、`docs/usage.md`、`docs/acceptance.md` §5
- **驗證**：
  - `pytest -q` → **215 passed**（新增進階查詢 5 項、關鍵字查詢 3 項、關鍵字過濾 4 項、查詢範圍 2 項、UI 欄位 1 項）
  - 真實站點（`scripts/probe_advanced.py`，每次 4 請求）：科目「行政法」→ 320 KB／137 科目／「完整」；類科「一般行政」→ 194 KB／87 科目／「完整（統計差異）」；類科＋科目 → 68 KB／13 科目／「完整」；全量對照 6.4 MB
  - `MoexClient.search(keywords=('', '一般行政', '國文'))` → 4 請求（2 次 postback）、官方＝解析（6/12/14）、「完整」
  - `streamlit.testing.AppTest` → 0 例外，三個關鍵字欄位存在
- **限制**：多年度時每年仍需 1 次迄年 postback（請求數 ≈ 1 次切換 ＋ 每年 2 次）；官方關鍵字比對不做全形／空白正規化，故保留本機第二層過濾；要放寬範圍需重新查詢（會命中 24 小時快取）

### [x] P2-7 候選 chips 與「完整抓取」選項（2026-11-01 追加需求）

- **需求**：第二階段要能「點一下就把該項目叉掉」，且叉除不得重新查詢官方；候選很多時畫面不能爆掉；第一階段要能選擇不加關鍵字的完整抓取。
- **交付**：
  - `app/ui/controller.py`：候選結果依「快照＋三欄選取＋年度」快取（`_selection_key`），叉除／加入只重算本機候選；新增 `selected_values`／`add_values`／`remove_values`，查詢成功後重設快速過濾文字
  - `app/ui/web_app.py`：已選項目以 `st.pills` 呈現（點一下叉除，只顯示目前可見者、上限 `MAX_CHIPS=40`，超出以說明文字提示）；未選項目用「從可見清單加入」多選（**完整列出**，可直接輸入搜尋）；全選／取消／反選可見改為改完即重畫；查詢表單新增「完整抓取（不加關鍵字）」勾選，勾選時送出空 `QuerySpec`
- **驗證**：
  - `pytest -q` → **230 passed**（新增控制器 5 項、介面字串 2 項、任務訊息 1 項、`tests/test_ui_candidates.py` 7 項）
  - `AppTest` 實測候選區：三個欄位都有「已選（點一下可叉除）」chips、科目超過上限時只列 40 項、「取消可見」後出現每頁 30 項的加入清單與「載入更多」、叉掉單一 chip 後 `state.exams` 僅少該值且**網路替身未被呼叫**（任何呼叫都會 AssertionError）
  - 實機瀏覽器（`localhost:8502`、113 年、快取命中 0 請求）：叉掉「初等考試」chip → 考試 20／20、類科 609／609、科目 1242／1242、考科數 3566→3510，時間戳仍為「（快取）」；快速過濾「行政」→ 科目可見 67 項、chips 僅列前 40 項並提示；展開狀態重畫後保留
  - `AppTest.from_file('app/ui/web_app.py')` → 0 例外；`scripts/verify_fixtures.py` 全部通過
  - 實機端到端（`localhost:8502`）：113 年 ＋ 類科「一般行政」＋ 科目「國文」→ 考試自動修剪 2／2、類科 4／57、科目 3／18、6 考科 12 檔；任務「全部完成（12/12）」、manifest 12 筆「已完成」、版本皆 1、`.parts` 無殘留；再次下載 → 「全部完成（12/12，其中略過 12 個已存在檔案）」、「新下載 0／略過 12」、檔案 mtime 未變（0 請求）
- **實測發現並修正**：(1) 取消某考試後「已選 623／609」出現 N > 總數 → 控制器在計算候選時修剪已離開候選池的選取值；(2) 叉 chip 後 expander 會收合 → 為 expander 加上 `key` 保留展開狀態；(3) 全選／取消／反選後同輪仍顯示舊清單 → 改完即 `st.rerun()`；(4) 重跑下載的進度只寫「完成 12」，看不出其實全部略過 → `TaskState.message` 與進度列改為「新下載 N／略過 N」；(5) 加入清單原本只載 30 項就分頁，瀏覽器端搜尋找不到未載入項目（實機輸入科目關鍵字得到 No results）→ 改為一次列出全部並移除「載入更多」
- **限制**：chips 一次最多顯示 40 個已選項目（需用快速過濾縮小範圍才看得到其餘）；候選快取失效於選取或快照變更，仍為 0.4 秒級重算；「完整抓取」固定抓整年度（約 6 MB／11 秒）

### [x] P2-8 內容去重（SHA-256，2026-11-02 追加需求）

- **需求**：同一份 PDF 在不同類科下有不同下載網址時，不要重複存兩份；多筆 manifest 紀錄指向同一實體檔。
- **交付**：
  - `app/storage/manifest.py`：`SCHEMA_VERSION=2`、`attachments.dedupe_of` 欄位與 v1→v2 自動遷移（`ALTER TABLE`）；`find_content_duplicate(sha256, exclude_asset_id)`；`complete_attachment(..., dedupe_of=)`
  - `app/engine/downloader.py`：驗證（狀態、長度、PDF 特徵、題型嗅探）後才比對 SHA-256；命中且既有檔完好（大小＋雜湊）時不寫入，直接沿用既有路徑並記錄 `dedupe_of`；`DownloadResult.dedupe_of`／`shared`
  - `app/engine/task_service.py`：`TaskState.deduped` 計數、事件標註「（內容相同，共用檔案）」、完成訊息列出共用數
  - `app/ui/web_app.py`：進度列改為「新下載 N／共用 N／略過 N／失敗 N／取消 N」；「預計歸檔」加註實際檔案可能較少
  - 文件同步：SPEC §6、AGENT §4、`docs/usage.md`、`docs/acceptance.md`
- **驗證**：
  - `pytest -q` → **237 passed**（新增 `tests/test_manifest_dedupe.py` 4 項、下載器去重 2 項、任務共用計數 1 項）
  - 實機（`localhost:8502`、全新根目錄 `downloads_dedupe`、113 年、科目「法學知識與英文」＋ 2 個警察類科）：4 個附件 → 實體檔 **2 個**（356,301 B 試題、47,731 B 答案）；任務「全部完成（4/4，其中 2 個與既有檔案內容相同（共用檔案））」、「新下載 2／共用 2」；manifest 2 筆帶 `dedupe_of`、不重複路徑數 2（`scripts/check_dedupe.py`）
- **限制**：必須先下載才知道 SHA-256，所以省的是磁碟與檔案數，**不省請求**；共用檔若被刪除，下次下載會自己寫一份（不會指向不存在的路徑）；跨快照共用也會發生（以 SHA-256 相同為準）

### [x] P2-9 介面便條紙修正（2026-11-02 追加需求）

- **需求**（使用者於畫面上標註 5 張便條紙）：
  1. 查詢表單的「關鍵字會先送官方進階查詢…」整段說明拿掉
  2. 「完整抓取」看不出勾了能幹嘛
  3. 「強制重新查詢」同樣看不懂
  4. 送出按鈕「查詢」改為「開始過濾」
  5. 「歸檔路徑預覽」好像不能用
- **交付**：
  - `app/ui/web_app.py`：刪除關鍵字說明 caption；勾選項改為「不套用關鍵字，抓整年度全部考科」與「重新向官網下載（忽略今日快取）」，兩者都加 `help` 說明代價與時機；按鈕改「開始過濾」、spinner 改「正在取得考畢試題清單…」、未取得資料提示同步
  - `_render_preview` 重寫：`st.spinner("計算中…")` 提供回饋、表格前顯示「共 N 個檔案、M 個資料夾；其中 K 個題型暫定」、長路徑欄位加寬、`width="stretch"` 取代已棄用的 `use_container_width`、零附件改顯示 `st.info`
  - `app/ui/controller.py`：新增 `preview_fingerprint(mode, template)`（快照＋年度＋三欄選取＋模式＋模板），選取或設定一改舊預覽立即失效
  - `app/ui/web_app.py::_ensure_controller`：以 **`CONTROLLER_API_VERSION`** 偵測 session 內的控制器是否過期（熱重載後舊實例會自動重建）；`_render_preview` 對缺方法的舊實例改顯示「程式已更新，請重新整理頁面」而不是整頁例外
- **驗證**：
  - `pytest -q` → **245 passed**（新增 `tests/test_ui_preview.py` 4 項、`tests/test_ui_hot_reload.py` 2 項、預覽指紋 1 項、表單用語 1 項；另更新 3 項舊斷言）
  - 實機（`localhost:8502`）：勾選項與按鈕已換成新文案並帶問號說明；按「開始過濾」→「計算預覽」→ 出現表格、0 例外，摘要「共 5669 個檔案、1262 個資料夾；其中 3566 個題型暫定」
  - `scripts/bench_preview.py`：全量 3,566 筆的 `build_plan` 約 **3.7 秒**（原本按鈕沒有任何進度提示，才會讓人以為「不能用」）
- **限制**：預覽仍是按鈕觸發（不隨選取自動重算，避免每次叉 chip 都等 3.7 秒）；表格只列前 200 筆
