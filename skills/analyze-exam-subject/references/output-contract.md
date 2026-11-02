# 輸出契約

inventory、selection、extraction、render-index 與 visual-review 使用 `schema_version=1`；`question-index` 使用 `schema_version=3`；`summary` 使用 `schema_version=2`；compact `work` 使用 `protocol=2`。讀取舊版 `question-index` 時，缺少視覺資訊一律解讀為 `not_checked`，不能默認已確認。沒有 compact cache 的舊索引不能當作分析快取。既有 extract CLI 與輸出不變。

JSON 使用 UTF-8。檔案路徑保存絕對路徑，頁碼為 PDF 一基頁碼，不是印刷頁碼。未知值用 null，不能編造 ID 或年度。

預設採 [精簡分析契約](compact-analysis.md)。AI 只產生短欄位結果，由 merge 補齊 question-index。summary／reason／evidence_excerpt 不再是每題必填；一般題可為空，頁碼與圖片證據仍保留。parent_question_id 可省略，以 countable=false 排除已拆分父題。可選 `answers`、`duplicate_of`、`score` 由 merge 驗證後補成 `answer_refs`、`duplicate_candidates`、`score`。

work.json 保存 config、config_hash、來源 registry、batches、可重用 cache 與 `verification`（檔案 mtime/size）；僅供本機程式使用。`config_hash` 只涵蓋分析設定（備考科目、分類表、分類版本、protocol）；個別來源 cache_key 另含 PDF／圖片雜湊與來源角色。`target_exam`、年度、考試名稱、官方科目等報告 metadata 不參與分析快取。`merge --fast-verify` 可用 verification 跳過未變更檔案的雜湊；預設仍完整重算。packet 檔才提供宿主當批輸入；`packet --context` 會另加同卷相鄰頁的 `context_pages`，只供跨頁題續讀。question-index 的 cache 是整卷精簡結果與 complete 標記；無此欄位的舊索引不能當作分析快取。

summary.json 保存唯一相關題數、補充題數、待確認數、coverage、主題計數、每主題有限代表題、年度考試分層與可選的答案／重複題計數。分類時使用 r/c/i/u；落盤索引轉為完整 relevance 列舉。不把 summary 的文字大小視為模型 Token 用量。

## inventory.json（工具生成）

root、files、contents、warnings。files 每筆有 path、relative_path、sha256、size、status、metadata；失敗檔保留 status 與 error，其餘欄位可缺。metadata 為 manifest 來源關聯列表。contents 以 sha256 分組且保留 paths。雜湊是內容識別，不是命題事件識別。

## selection.json（使用者選取後由宿主保存）

```json
{
  "schema_version": 1,
  "study_subject": "資料結構",
  "target_exam": null,
  "classification_version": "1",
  "selected_sources": [
    {
      "path": "D:/題庫/合科/試題.pdf",
      "sha256": "替換為盤點的完整 SHA-256",
      "official_subject": "資料結構與資料庫及資料探勘",
      "role": "question",
      "year": 114,
      "exam_name": null,
      "category": null,
      "metadata_evidence": "manifest 或 PDF 表頭及頁碼"
    }
  ]
}
```

path 寫絕對路徑；未知 metadata 寫 null。`classification_version` 是 compact 分析必填。同一檔案可有多個來源事件，保留多筆 selected_sources；工具只依 path＋sha256 驗證選取及去重。role 可為 question、official_answer、corrected_answer、unknown；答案角色由可靠來源確認，未知不自動套用。

## extraction.json（工具生成）

tool_version、pypdf_version、page_scope、documents。每筆 document 包含 sha256、paths、status、pages 及 error（若有）。每頁有 page、text、status；有文字也標示 text_extracted 而不是「完整判讀」。空頁為 needs_visual_review，無法抽取為 failed，檔案混合成功失敗為 partial。此檔不包含 AI 語意分類。

## question-index.json（宿主分析生成，schema 3）

- schema_version、study_subject、target_exam、classification_version、config_hash、warnings。
- status：partial／complete；coverage：見下方。
- topics：穩定 topic_id、label、parent_id。
- questions：question_id、content_sha256、pages、question_number、parent_question_id（可省略）、countable、summary、relevance、topic_ids、reason、evidence_excerpt（可省略）、source_refs、answer_refs、duplicate_candidates、score（可選）、analysis_status、visual_status、interpretation_method、image_evidence。
- relevance：relevant／cross_domain／irrelevant／uncertain；analysis_status：completed／needs_review／unreadable。
- content_sha256 是來源 PDF 的內容雜湊，不是單題語意雜湊；question_id 以來源短 ID＋起始頁＋題號產生，跨卷同題以 duplicate_candidates 標記。
- source_refs：path、sha256、year、exam_name、category、official_subject、role、event_id（未知為 null）、metadata_evidence。path 必須是絕對路徑。
- answer_refs：source、path、sha256、page、question_number、role、answer（可選答案內容）、basis、conflict；只接受 official_answer／corrected_answer，頁碼必須在已渲染範圍。
- duplicate_candidates：other_question_id、reason；只標記，不自動合併，指向的題目必須存在。
- visual_status：not_checked／checked／unreadable。`checked` 必須來自 `merge --visual-review` 的實際圖片證據且 `host_vision.available=true`；未提供 visual-review 時所有題目維持 not_checked、整體為 partial，只保留文字暫判。render-index 本身永遠是 not_checked。
- interpretation_method：text_only／vision_only／text_and_vision；有提供 `merge --extraction` 且題目所有頁都沒有 text_extracted 時為 vision_only。
- image_evidence：source_sha256、page、image_path、image_sha256；可指向 300 DPI detail-index 的實際判讀圖片。
- source_status：每個已選路徑的列表，欄位 path、sha256、status（pending／processed／failed／needs_review）、detail。
- coverage：
  - selected_files、processed_files、failed_files、pending_files、needs_review_files。
  - selected_pages、text_extracted_pages（未提供 extraction 時 null）、rendered_pages、visually_checked_pages、pending_visual_pages、unrendered_pages、unreadable_pages、render_failed_pages、unknown_page_count_files。
  - pending_visual_pages 以「已選頁－已視覺核對頁」計，包含尚未渲染的頁；unrendered_pages 另列「已選頁－已渲染頁」。頁面採唯一來源內容＋頁碼計數；無法取得全卷頁數時 selected_pages=null，不報頁面覆蓋百分比。
- 檔案與頁面計數可以重疊；不得把各階段頁數相加當總數。

## render-index.json（工具生成）

- kind 固定 exam-render-index，schema_version=1，config 包含 render_version、pypdfium_version、pdfium_version、pillow_version、requested_dpi、max_pixels。
- page_scope=null 表示全卷，或一基頁碼列表。status 為 in_progress／rendered／partial，僅表示渲染作業。
- documents 每筆保留 sha256、paths、total_pages（開啟失敗時缺少）、status（in_progress／rendered／partial／failed）、pages、error（若有）。
- pages：page、source_sha256、status（rendered／failed）、visual_status（固定 not_checked）。成功另含 image_path、image_sha256、width、height、requested_dpi、actual_dpi、render_version、cache_key、reused；失敗含 error。
- 同一個 output 只允許搭配指向自己的 `--resume` 更新，且選取、page_scope 與渲染設定都必須相同；否則使用新輸出。變更範圍或設定不得覆寫同一索引。
- 逐頁進度先 append 到 `<output>.journal.jsonl` 並 fsync，文件結束折回索引後刪除 journal；恢復時先重放 journal。失敗頁可重試，已成功頁驗證後可重用。已在本次圖片目錄內的圖片直接沿用，不重複複製。

## visual-review.json（宿主生成，merge 讀取）

schema_version=1，host_vision 含 available（true／false／null）、tool、probe_image、probe_result；結果基於實際開圖，不基於模型名稱。

pages 每筆含 source_sha256、page、image_sha256、image_path、visual_status（not_checked／checked／unreadable）、observations、reason、reviewed_at。只有實際檢視且可讀才設 checked；看過但看不清設 unreadable。圖片證據可指向較高解析度的重繪版本。

`merge --visual-review` 會核對來源、頁碼與圖片實際雜湊；`host_vision.available` 不是 true 時不得有 checked 頁，且 AI 的 reviewed_pages 必須留空。`unreadable` 頁面會使所屬題目 analysis_status=unreadable，整體分析維持 partial。不存在或已變更的圖片需重新產生並視覺核對。

## 統計與恢復

question_id 在本次分析中穩定，以來源內容雜湊＋頁碼＋題號產生；不能僅以題號識別跨卷題目。相同內容的題目可引用多個 source_refs。父題拆成獨立子題時 countable=false，子題才計數。相關與交叉題的唯一 ID 聯集為相關題數；不相關與未確認分開列出。主題命中數逐 topic 計唯一題 ID，不累加重複標籤。

恢復前核對 selection 的來源雜湊、study_subject、classification_version、topics 與圖片雜湊；任何變更都要重新分析受影響部分。`target_exam`、年度、考試名稱、官方科目等 metadata 變更只重跑 merge／summary，不必重跑 AI。統計基於已確認且 countable 的題目，揭露未確認部分與分母，不以零填補未解析資料。
