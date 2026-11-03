# 國考刷題神器 — 開發指南（AGENT.md）

## 1. 適用範圍與原則

實作行為與驗收以 SPEC.md 為契約；本文件定義模組責任及工程約束。共同契約變更必須同步修改兩份文件。先完成 SPEC §2 的來源驗證，不將文件中的網站假設視為已確認事實。

UI、註解、Docstring 與人類可讀日誌使用台灣繁體中文；程式識別字、協定欄位與穩定事件碼可使用英文。保留官方原始名稱，同時建立正規化比對鍵。

核心查詢、篩選、命名及下載不呼叫外部商業 AI API，不需 API Key。網路操作仍需要連線；離線能力限於已有快照與本機檔案處理。不可宣稱零套件依賴或完全離線爬取。

## 2. 技術選擇與結構

MVP 固定 Python 3.10+、httpx 同步 Client、BeautifulSoup＋lxml、pypdf、SQLite、Streamlit。以小型背景工作池處理下載，避免同時維護同步與非同步兩套實作。鎖定經驗證的套件版本，將開發依賴與執行依賴分開。

| 模組 | 責任與邊界 |
| --- | --- |
| app/models.py | Enum、dataclass、穩定 ID 與共用契約，不做 I/O |
| app/config.py | 超時、限速、大小限制、來源白名單及模板預設 |
| app/crawler/moex_client.py | HTTP、Session、表單事件；不處理 UI 或寫檔 |
| app/crawler/parser.py | HTML／表單解析為模型；不做網路或 PDF I/O |
| app/crawler/rate_limiter.py | 所有站點請求共用的限速、在途上限（預設 2，範圍 1～4）、退避及 Retry-After |
| app/engine/query_service.py | 年度批次查詢、快照完整性（統計與列數雙軌核對）、快取及查詢恢復 |
| app/engine/filter.py | 純函式比對、候選聚合與選取計算 |
| app/engine/pdf_inspector.py | PDF 有效性與首頁題型證據；受大小、時間限制 |
| app/engine/path_builder.py | 純路徑計算、模板驗證、清理與碰撞方案，不下載 |
| app/engine/downloader.py | 串流暫存、驗證、歸檔與附件重試；可被多執行緒呼叫，需執行緒安全 |
| app/engine/task_service.py | 任務狀態、取消、進度、執行緒池派送（查詢串行／下載併發）與 UI 提交去重 |
| app/storage/manifest.py | SQLite 快照、附件狀態、版本與啟動核對 |
| app/ui/web_app.py | Streamlit 畫面與使用者輸入，不內嵌業務邏輯 |

入口為 main.py；另保留 docs/source-validation.md、tests/fixtures/、tests/ 與 downloads/。產生的題庫、資料庫、暫存及日誌不納入版本控制。

## 3. 資料模型契約

核心模型使用具型別提示的 dataclass；所有函式提供參數及回傳型別。以下為必要欄位概念，實作時可增加欄位但不可縮回單一試題 URL 模型：

```python
from dataclasses import dataclass
from enum import Enum

class QuestionType(str, Enum):
    ESSAY = "申論題"
    CHOICE = "測驗題"
    MIXED = "混合題"
    UNKNOWN = "未確認"

class AssetRole(str, Enum):
    QUESTION = "試題"
    CHOICE_ANSWER = "測驗答案"
    OTHER = "其他"

@dataclass(frozen=True)
class TypeEvidence:
    question_type: QuestionType
    source: str
    reason: str
    rule_version: str
    conflicted: bool = False

@dataclass(frozen=True)
class ExamAsset:
    asset_id: str
    official_id: str | None
    role: AssetRole
    original_label: str
    url: str
    observed_at: str
    source_version: str | None
    evidence: TypeEvidence

@dataclass(frozen=True)
class ExamRecord:
    record_id: str
    official_id: str | None
    year: int
    exam_name: str
    exam_short_name: str | None
    category: str
    subject: str
    level: str | None
    source_page: str
    assets: tuple[ExamAsset, ...]
```

下載狀態、路徑、大小、雜湊與 HTTP 驗證器另存 manifest，不在解析模型中混入工作狀態。共用附件以紀錄—附件關聯表表示；傳輸去重與歸檔目的地分開處理。

ID 優先採官方識別碼；缺少時依 SPEC §4 產生穩定雜湊並標記策略版本。保留來源原文、正規化比對值及觀察時間，不能僅依檔名去重。

## 4. 網路與解析規則

- 全域同站在途請求上限預設 2（可設定 1～4），啟動間隔 0.5～1.5 秒；所有模組共用同一個限速器與計時節奏，worker 不各自計時。查詢事件在同一 Session 內串行，下載階段可用執行緒池併發，但每次請求仍須通過共用限速器。不得宣稱特定併發數保證不被 WAF 封鎖；429／503 依 Retry-After 退避，403 停止對該來源的新請求並記錄原因，不當作限流重試。
- WebForms 查詢在同一 Client 內串行，依最新頁面保留所有 hidden fields，包含分段 ViewState；事件與控制項參數必須經 fixture 驗證。此站點**無分頁**，不得實作不存在的分頁事件；下拉選單變更（如年度迄）必須以 `__EVENTTARGET` 觸發 postback，否則變更不生效。
- 進階查詢（考試名稱／類科／科目關鍵字）需先以 `ibtnFull` 切換並重新解析表單，查詢按 `btnQuery`（簡易查詢為 `btnSearch`）；**空白關鍵字欄位一律不送出**，關鍵字改變時必須重建表單以免 ViewState 殘留，切回簡易查詢時要重新取得表單。
- 網路 Client 不跨工作執行緒共享可變表單／Cookie 狀態。下載如需 Cookie，從已確認的 Session 快照建立獨立 Client。
- 依 SPEC §7 實作逾時、重試、Retry-After、Session 重建及網域／重新導向驗證。
- 解析器必須區分無結果、來源錯誤頁（含以 HTTP 200 回傳的錯誤頁）、結構變更與正常列表；不可將解析失敗當作空結果。類科列可能以 `label`+checkbox 或無 checkbox 的 `span` 呈現，兩者皆需支援。
- HTML 編碼依回應標頭、頁面宣告及已驗證的來源規則處理，保留診斷資訊；內部統一 Unicode，輸出文字檔採 UTF-8。不得以忽略解碼錯誤悄悄破壞官方名稱。
- 查詢進度與完整性分開；以年度為批次單位（官方單次上限 6,000 筆，超過時只回提示文字且無結果列）。完整性採官方統計與實際列數雙軌核對：僅統計文字與列數不一致（無解析失敗、上限或錯誤頁）時標示「完整（統計差異）」並保留兩組數字，仍可下載；若是合併重複列、跨批次去重後對不上或解析異常，則必須標示「結果未完整」並禁止批量下載。

## 5. 題型判定規則

沒有答案不能推論純申論；有答案不能推論純測驗或混合。預設 UNKNOWN，記錄可追溯證據。

pdf_inspector 只抽取首頁，檢查前 300 字元，不以文字抽取順序保證視覺位置。不掃描後續頁面或 OCR。使用可終止工作程序實作預設 5 秒逾時，不把無法停止的執行緒等待當成硬逾時。

規則優先明確混合證據；純題型須有排他證據。「單一選擇題」或「未找到測驗規範」不能單獨證明純題型。衝突、掃描、失敗或未知格式依 SPEC §4.1 降級並保存原因，不使用裸 except/pass 吞掉問題。

未知題型命名為「試題」；答案必須為「測驗答案」。一筆考科可有多份試題附件；答案配對依來源關聯，不按字串相似度猜測。

來源的「更正答案」（`t=M`）屬測驗答案角色但與原答案並存；兩者須各自保留並以穩定附件 ID 消歧，不得合併、覆寫或視為新考科。

## 6. 路徑、下載與持久化

嚴格遵循 SPEC §5 的變數定義與模板契約。不可使用 eval 或允許任意屬性／索引取值的模板解析。目錄分隔符由模板結構處理，資料值中的斜線只當作待清理字元。

path_builder 提供路徑計算與安全檢查；downloader 寫入前再次解析實際父目錄並核對根目錄邊界，防範 symlink／junction 逃逸。涵蓋保留名稱、大小寫碰撞、尾端句點、空名稱、超長路徑與清理後同名。不同附件以穩定 ID 消歧，不覆寫。

下載先寫同檔案系統內的 `.part`，成功狀態、內容、可比較長度及 PDF 驗證通過後才嗅探與計算最終路徑。原子更名後更新 manifest；啟動核對處理中間崩潰。無效 PDF 記為失敗；有效但無法辨識題型的 PDF 可作 UNKNOWN 歸檔。

SQLite 寫入使用短交易並序列化狀態更新；不在網路等待期間持有交易。不得僅憑非零大小、同名或 URL 相同宣稱完成。略過須比對 manifest、大小與 SHA-256。內容去重只能發生在驗證（狀態、內容、長度、PDF、題型）之後：SHA-256 相同且既有檔完好時共用路徑並寫入 `dedupe_of`，否則不可共用；不得為避免重複而跳過驗證或覆寫既有檔案。

更新檢查、版本保留、取消、附件大小上限、恢復與部分完成語意均依 SPEC §6–7。MVP 恢復為附件層級重試，不宣稱 HTTP Range 續傳。

模式變更後，已存在且完好的附件若目標路徑不同，必須搬移（同檔案系統用 `os.replace`）而非只 `shutil.copy2`；搬移後刪除舊檔並清除空目錄，不得留下新舊兩份。共用檔（`dedupe_of`）只由擁有者搬移並同步更新所有指向舊路徑的附件與版本紀錄；非擁有者不得搬移。`version > 1` 的版本檔須保留內容雜湊後綴；目標碰撞不得覆寫。整理與清理須可重複執行，且只能刪除內容與已追蹤檔案相同的未追蹤 PDF。下載任務執行中不得變更歸檔模式、根目錄或併發數。

併發下載時，downloader 不共享可變的 HTTP Client／Cookie 狀態（必要時由已確認的 Session 快照各自建立 Client），manifest 寫入以短交易序列化，且不得在持有交易或鎖的期間等待網路 I/O。派送採有界方式，每個 future 獨立捕捉例外；驗證與歸檔完成才計入成功數。

## 7. UI 與可觀測性

控制器與任務服務均須有介面版本；初始化與 session 復用檢查實際服務版本及匯出方法。版本不相容時顯示完整程序重啟提示並停止新任務與匯出，不以部分模組熱重載或替換背景服務升級。舊任務成果不猜測遷移、不回退全目錄打包；重啟後重新提交任務，保留 PDF 與 manifest。

ZIP 匯出僅使用指定任務累積的成功附件結果（含略過與共用檔案），不得掃描整個題庫目錄。結果須保存附件 ID 與最終相對路徑，重試保留先前成功成果；匯出不受提交後的篩選變更影響。同一實體路徑只收錄一次，檔案遺失或超出根目錄時停止匯出並顯示原因，UI 顯示實際檔案數與任務識別。

Streamlit 重跑不可重複啟動任務；以任務 ID 與快照 ID 綁定背景工作。工作執行緒不直接呼叫 UI，透過狀態儲存及事件回報進度。

顯示快照時間、查詢完整性、紀錄數與附件數、暫定命名、完成／略過／失敗統計及重試入口。不可將部分成功顯示為全部完成。取消停止新工作並關閉串流，保留有效完成檔案。

日誌以穩定事件碼加繁體中文訊息記錄錯誤分類與 ID；不輸出 Session Cookie、表單 token 或敏感 URL query。對外錯誤要說明可採取的動作。

## 8. 測試與完成條件

常規測試使用離線 fixture 及模擬 HTTP；不在每次測試對官方大量查詢。網路實測採小樣本與相同限速，記錄日期、結果及未驗證範圍。

必要驗證：

1. 年度批次合併去重、重複列去重（同列渲染兩次）、官方統計核對、官方 6,000 筆上限、統計不符與 Session 失效重建。
2. 多欄 AND、同欄 OR、正規化、簡稱回退、可見項目選取、候選份數及零選取。
3. 多附件拆卷、共用 URL、缺少答案、UNKNOWN、衝突、掃描 PDF、只讀首頁與嗅探逾時。
4. A／B／C／D 模板、年度格式、未知變數、目錄逃逸、保留名稱、清理碰撞及長路徑。
5. HTML 偽裝 PDF、截斷檔、損壞檔、超限、缺少 Content-Length、429／503／404 及重試次數。
6. 取消與重新啟動、歸檔後未提交狀態、雜湊不符、更新版本，以及試題成功答案失敗。
7. UI 重複提交防護、不完整快照禁用下載、進度及部分完成重試。

效能使用具環境與樣本說明的報告，區分快取命中、首次查詢、PDF 大小與題型未知率，不以普遍 50ms 或 100% 準確率驗收。

每個階段完成時更新來源驗證紀錄、操作說明與相關測試。交付依 SPEC §8 的順序，先完成資料正確性、下載安全及恢復，再擴充 UI 與自訂模板。
