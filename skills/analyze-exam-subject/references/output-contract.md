# 輸出契約

inventory、selection、extraction 與 render-index 使用 schema_version=1；新增視覺欄位的 question-index 使用 schema_version=2。讀取舊版 question-index 時，缺少視覺資訊一律解讀為 not_checked，不能默認已確認。既有 extract CLI 與輸出不變。

JSON 使用 UTF-8。檔案路徑保存絕對路徑，頁碼為 PDF 一基頁碼，不是印刷頁碼。未知值用 null，不能編造 ID 或年度。

## inventory.json（工具生成）

root、files、contents、warnings。files 每筆有 path、relative_path、sha256、size、status、metadata；metadata 為 manifest 來源關聯列表。contents 以 sha256 分組且保留 paths。雜湊是內容識別，不是命題事件識別。

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

同一檔案可有多個來源事件，保留多筆 selected_sources；工具只依 path＋sha256 驗證選取及去重。role 可為 question、official_answer、corrected_answer、unknown；答案角色由可靠來源確認，未知不自動套用。

## extraction.json（工具生成）

tool_version、pypdf_version、page_scope、documents。每筆 document 包含 sha256、paths、status、pages 及 error（若有）。每頁有 page、text、status；有文字也標示 text_extracted 而不是「完整判讀」。空頁為 needs_visual_review，無法抽取為 failed，檔案混合成功失敗為 partial。此檔不包含 AI 語意分類。

## question-index.json（宿主分析生成）

- schema_version、study_subject、target_exam、classification_version。
- status：partial／complete；coverage：selected_files、processed_files、failed_files、pending_files，皆以唯一已選路徑計數。
- topics：穩定 topic_id、label、parent_id。
- questions：question_id、content_sha256、pages、question_number、parent_question_id、countable、summary、relevance、topic_ids、reason、evidence_excerpt、source_refs、analysis_status。
- relevance：relevant／cross_domain／irrelevant／uncertain；analysis_status：completed／needs_review／unreadable。
- source_refs：path、year、exam_name、category、official_subject、event_id（未知為 null）、事件判定依據。metadata 缺少時保留 null。
- answer_refs：來源 path、page、題號、role、配對依據、衝突說明；AI 推導另放 ai_notes。
- duplicate_candidates：疑似相同題的 ID 配對與理由，不自動合併。
- source_status：每個已選路徑／雜湊的 pending、processed、failed、needs_review，以及錯誤或缺口。
- questions 新增 interpretation_method（text_only／vision_only／text_and_vision）、visual_status（not_checked／checked／unreadable）、image_evidence（source_sha256、page、image_path、image_sha256）；未做視覺核對的文字結果只作暫定。
- coverage 新增 selected_pages、text_extracted_pages、rendered_pages、visually_checked_pages、pending_visual_pages、unreadable_pages、render_failed_pages、unknown_page_count_files。頁面採唯一來源內容＋頁碼計數；無法取得全卷頁數時 selected_pages=null，不報頁面覆蓋百分比。

## render-index.json（工具生成）

- kind 固定 exam-render-index，schema_version=1，config 包含 render_version、pypdfium_version、pdfium_version、pillow_version、requested_dpi、max_pixels。
- page_scope=null 表示全卷，或一基頁碼列表。status 為 in_progress／rendered／partial，僅表示渲染作業。
- documents 每筆保留 sha256、paths、total_pages（開啟失敗時缺少）、status（in_progress／rendered／partial／failed）、pages、error（若有）。
- pages：page、source_sha256、status（rendered／failed）、visual_status（固定 not_checked）。成功另含 image_path、image_sha256、width、height、requested_dpi、actual_dpi、render_version、cache_key、reused；失敗含 error。
- 相同 output 只允許搭配指向自己的 --resume 更新且格式須相符；其他既有檔案不能覆寫。逐頁原子保存，失敗頁可重試，已成功頁驗證後可重用。

## visual-review.json（宿主生成）

schema_version=1，host_vision 含 available（true／false／null）、tool、probe_image、probe_result；結果基於實際開圖，不基於模型名稱。

pages 每筆含 source_sha256、page、image_sha256、image_path、visual_status（not_checked／checked／unreadable）、observations、reason、reviewed_at。只有實際檢視且可讀才設 checked；看過但看不清設 unreadable。圖片證據可指向較高解析度的重繪版本。

工具不產生 checked 判定；宿主分析恢復前核對來源及圖片實際雜湊、備考科目、目標範圍與分類版本。不存在或已變更的圖片需重新產生並視覺核對。所有選取頁尚未核對或有不可判讀內容時，整體分析維持 partial。

question_id 在本次分析中穩定，建議以內容雜湊＋頁碼＋題號產生；不能僅以題號識別跨卷題目。相同內容的題目可引用多個 source_refs。父題拆成獨立子題時 countable=false，子題才計數。相關與交叉題的唯一 ID 聯集為相關題數；不相關與未確認分開列出。主題命中數逐 topic 計唯一題 ID，不累加重複標籤。

恢復前核對 selection 的來源雜湊、study_subject、target_exam、classification_version；任何變更都要重新分析受影響部分。統計基於已確認且 countable 的題目，揭露未確認部分與分母，不以零填補未解析資料。
