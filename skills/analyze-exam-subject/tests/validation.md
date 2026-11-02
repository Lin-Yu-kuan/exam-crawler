# 本機驗證紀錄

日期：2026-11-02（Asia/Taipei）。環境：Windows、Python 3.14.7、pypdf 6.21.4、pypdfium2 5.14.0、PDFium 156.0.8076.0、Pillow 13.0.0。

## 已完成

- `python -m unittest discover -s tests -v`：51 項，49 項通過，2 項因 Windows 不允許建立符號連結而略過。
- 通過：中文／巢狀路徑、輸出排除、SHA-256 去重及來源保留、損壞 PDF、無文字頁、未選取阻止全文擷取、內容變更失效、恢復範圍核對、已知 manifest 多事件唯讀、未知 manifest 退回、CLI 與輸出不覆寫。
- 原子輸出：`write_new` 以暫存檔＋排他硬連結發佈；既有檔不覆寫、無殘留 `.tmp`。符號連結輸出在程式中有拒絕檢查，但實際建立符號連結的測試因 Windows 權限略過。
- 平台／manifest：manifest schema v1 與 v2 都有測試；Windows junction 以 `mklink /J` 實測，inventory 不會跟隨；符號連結仍因權限略過。
- 效能／驗證：`merge --fast-verify` 可用 prepare 的 mtime／size 跳過未變更檔案；未變更時不重算、變更時仍重算並拒絕；預設仍完整驗證。
- 安全名稱與 `run-dir`：Windows 保留名稱、路徑分隔符、尾端句點、空白與 `..` 都清理；連續執行得到不同目錄；輸出入可在來源 `_analysis` 或 `--output-root` 指定的外部可寫入根目錄。
- 靜態檢查：frontmatter、名稱、描述長度、所有 Markdown 相對連結存在且不逃出技能資料夾、Python 語法，均由 `tests/test_skill_metadata.py` 自動執行。
- 合科行為案例已逐項人工核對技能規則：使用者決定來源、題目級排除純 SQL、保留頁碼題號、不同官方科目合併報告。這是指令審查，並非真實考題語意分類測試。
- 渲染：六種頁面、像素上限、旋轉方向、逐頁中斷恢復、快取雜湊驗證、損壞圖片修復、DPI 改變失效、來源變更失效、未選來源排除、單頁失敗重試及 render CLI。
- 渲染效能／恢復：逐頁改寫 append-only journal，文件結束折回索引；中斷後可重放 journal 還原已完成頁，不再每頁重寫整份索引。
- 恢復一致性：同一個 output 只允許相同選取、page_scope 與設定；恢復中斷後未處理頁不會被截掉；已在本次目錄的圖片直接沿用，不再每次複製；明確 `--dpi 150` 與預設 150.0 可共用快取。
- 視覺：所有頁面維持 not_checked 模擬沒有視覺判讀；**沒有 visual-review 時 merge 一律 partial、題目 not_checked**；`merge --visual-review` 核對實際圖片雜湊，host_vision.available=false 時拒絕非空 reviewed_pages，unreadable 頁面使整體 partial。
- 跨頁：同一題跨批次時，只有單一批次的 reviewed_pages 涵蓋全部頁碼才標 checked；僅把兩批 reviewed_pages 聯集不會完成題目。`packet --context 1` 會把同卷前後相鄰頁放進 `context_pages`，供續讀且不重複計算。
- 契約：`question-index` schema 3 的 coverage 檔案／頁面欄位、source_status 每路徑狀態、answer_refs、duplicate_candidates、analysis_status=unreadable 皆有測試。
- 快取：target_exam、年度、考試名稱、官方科目變更不再使分析快取失效；`summary --target` 可在同一索引切換統計目標。
- 精簡分析新增測試：來源補齊、主題多標籤去重、packet 不含完整登錄或全文、packet 自足規則且不含 target_exam、衝突拒絕、未視覺核對不進主統計、整卷快取、分類及圖片變更失效、目標考試分層、父題排除、未知引用拒絕、新試卷只排新批次、缺渲染頁警告。
- 端到端 CLI：`run-dir`→`inventory`→`render`→`prepare`→`status`→`packet`→`merge --visual-review`→`summary` 以合成兩頁 PDF 跑通，輸出 schema 3／2。
- 真實題庫 dogfood：以 112 年公務人員初等考試「國文」完整 8 頁試題（45 題）＋1 頁標準答案跑完整流程，實際用 `read_image` 開啟 9 頁並寫入 visual-review；結果 `complete`，45 題全部 completed、45 筆 `answer_refs`、coverage 9/9 頁。主題分布：閱讀理解 33、成語與詞語 7、字音字形／國學常識 5。
- 契約欄位：`parent_question_id`、`evidence_excerpt`、`vision_only`、`--extraction`、`text_extracted_pages`、`answer_refs.answer` 已補上並有測試。
- 先前的 2 頁 partial dogfood 已由上述完整 run 取代；報告與索引保存在 `.pytest_cache/dogfood-full/`，不屬於技能交付內容。
- 合成兩題的精簡回覆與補齊索引已比較 JSON 位元組大小，精簡回覆較小且來源可還原；這不是實際模型 Token 節省率，也未量測宿主歷史上下文成本。
- Codex 此環境已透過實際圖片工具開啟六頁合成樣本；確認內容可見。這是渲染目視檢查，不是複雜考題辨識準確率測試。
- 已取得 PyYAML 6.0.3，skill-creator 的 quick_validate.py 本次執行通過（Skill is valid）。

## 限制與待驗證

- 符號連結測試因 Windows 缺少建立權限略過；junction 已用 `mklink /J` 實測，其他 reparse point 類型尚未測試。
- 未在 Antigravity 實際載入與操作；已在 Codex／本機以真實 112 初等國文完整 8 頁＋答案卷跑過，但不是雙平台驗證。
- Python 3.10 為設計最低版本，實際執行版本為 3.14；其他 Python 版本與作業系統尚待實測。
- 尚未安裝到任何宿主技能目錄，也未修改全域設定。
- 端到端 CLI 使用合成 PDF，不代表真實考題的語意分類、跨頁題判讀或答案版本配對已驗證。

此執行環境的系統暫存目錄寫入受限，測試時可將 TEMP 與 TMP 設為專案 `.pytest_cache/skill-temp`。本次 `pypdf` 裝在 `.pytest_cache/test-deps`、渲染依賴裝在 `.pytest_cache/visual-deps`，透過 `PYTHONPATH` 使用；未修改全域 Python。合成 PDF／圖片只存於測試快取，不作為交付題庫。
