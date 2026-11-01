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
| app/crawler/rate_limiter.py | 所有站點請求共用的限速、退避及 Retry-After |
| app/engine/query_service.py | 分頁、快照完整性、快取及查詢恢復 |
| app/engine/filter.py | 純函式比對、候選聚合與選取計算 |
| app/engine/pdf_inspector.py | PDF 有效性與首頁題型證據；受大小、時間限制 |
| app/engine/path_builder.py | 純路徑計算、模板驗證、清理與碰撞方案，不下載 |
| app/engine/downloader.py | 串流暫存、驗證、歸檔與附件重試 |
| app/engine/task_service.py | 任務狀態、取消、進度、工作池與 UI 提交去重 |
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

- 全域同站最多 2 個在途請求，啟動間隔 0.5～1.5 秒；所有模組共用限速器。
- WebForms 查詢在同一 Client 內串行，依最新頁面保留所有 hidden fields，包含分段 ViewState；事件、控制項及分頁參數必須經 fixture 驗證。
- 網路 Client 不跨工作執行緒共享可變表單／Cookie 狀態。下載如需 Cookie，從已確認的 Session 快照建立獨立 Client。
- 依 SPEC §7 實作逾時、重試、Retry-After、Session 重建及網域／重新導向驗證。
- 解析器必須區分無結果、來源錯誤頁、結構變更與正常列表；不可將解析失敗當作空結果。
- HTML 編碼依回應標頭、頁面宣告及已驗證的來源規則處理，保留診斷資訊；內部統一 Unicode，輸出文字檔採 UTF-8。不得以忽略解碼錯誤悄悄破壞官方名稱。
- 查詢進度與完整性分開；達上限、分頁失敗或迴圈時依 SPEC 禁止從不完整快照批量下載。

## 5. 題型判定規則

沒有答案不能推論純申論；有答案不能推論純測驗或混合。預設 UNKNOWN，記錄可追溯證據。

pdf_inspector 只抽取首頁，檢查前 300 字元，不以文字抽取順序保證視覺位置。不掃描後續頁面或 OCR。使用可終止工作程序實作預設 5 秒逾時，不把無法停止的執行緒等待當成硬逾時。

規則優先明確混合證據；純題型須有排他證據。「單一選擇題」或「未找到測驗規範」不能單獨證明純題型。衝突、掃描、失敗或未知格式依 SPEC §4.1 降級並保存原因，不使用裸 except/pass 吞掉問題。

未知題型命名為「試題」；答案必須為「測驗答案」。一筆考科可有多份試題附件；答案配對依來源關聯，不按字串相似度猜測。

## 6. 路徑、下載與持久化

嚴格遵循 SPEC §5 的變數定義與模板契約。不可使用 eval 或允許任意屬性／索引取值的模板解析。目錄分隔符由模板結構處理，資料值中的斜線只當作待清理字元。

path_builder 提供路徑計算與安全檢查；downloader 寫入前再次解析實際父目錄並核對根目錄邊界，防範 symlink／junction 逃逸。涵蓋保留名稱、大小寫碰撞、尾端句點、空名稱、超長路徑與清理後同名。不同附件以穩定 ID 消歧，不覆寫。

下載先寫同檔案系統內的 `.part`，成功狀態、內容、可比較長度及 PDF 驗證通過後才嗅探與計算最終路徑。原子更名後更新 manifest；啟動核對處理中間崩潰。無效 PDF 記為失敗；有效但無法辨識題型的 PDF 可作 UNKNOWN 歸檔。

SQLite 寫入使用短交易並序列化狀態更新；不在網路等待期間持有交易。不得僅憑非零大小、同名或 URL 相同宣稱完成。略過須比對 manifest、大小與 SHA-256。

更新檢查、版本保留、取消、附件大小上限、恢復與部分完成語意均依 SPEC §6–7。MVP 恢復為附件層級重試，不宣稱 HTTP Range 續傳。

## 7. UI 與可觀測性

Streamlit 重跑不可重複啟動任務；以任務 ID 與快照 ID 綁定背景工作。工作執行緒不直接呼叫 UI，透過狀態儲存及事件回報進度。

顯示快照時間、查詢完整性、紀錄數與附件數、暫定命名、完成／略過／失敗統計及重試入口。不可將部分成功顯示為全部完成。取消停止新工作並關閉串流，保留有效完成檔案。

日誌以穩定事件碼加繁體中文訊息記錄錯誤分類與 ID；不輸出 Session Cookie、表單 token 或敏感 URL query。對外錯誤要說明可採取的動作。

## 8. 測試與完成條件

常規測試使用離線 fixture 及模擬 HTTP；不在每次測試對官方大量查詢。網路實測採小樣本與相同限速，記錄日期、結果及未驗證範圍。

必要驗證：

1. 分頁去重、末頁、迴圈、官方總筆數核對、結果上限與 Session 失效重建。
2. 多欄 AND、同欄 OR、正規化、簡稱回退、可見項目選取、候選份數及零選取。
3. 多附件拆卷、共用 URL、缺少答案、UNKNOWN、衝突、掃描 PDF、只讀首頁與嗅探逾時。
4. A／B／C／D 模板、年度格式、未知變數、目錄逃逸、保留名稱、清理碰撞及長路徑。
5. HTML 偽裝 PDF、截斷檔、損壞檔、超限、缺少 Content-Length、429／503／404 及重試次數。
6. 取消與重新啟動、歸檔後未提交狀態、雜湊不符、更新版本，以及試題成功答案失敗。
7. UI 重複提交防護、不完整快照禁用下載、進度及部分完成重試。

效能使用具環境與樣本說明的報告，區分快取命中、首次查詢、PDF 大小與題型未知率，不以普遍 50ms 或 100% 準確率驗收。

每個階段完成時更新來源驗證紀錄、操作說明與相關測試。交付依 SPEC §8 的順序，先完成資料正確性、下載安全及恢復，再擴充 UI 與自訂模板。
