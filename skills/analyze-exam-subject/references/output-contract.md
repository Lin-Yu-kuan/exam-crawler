# 輸出契約（schema_version = 1）

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

question_id 在本次分析中穩定，建議以內容雜湊＋頁碼＋題號產生；不能僅以題號識別跨卷題目。相同內容的題目可引用多個 source_refs。父題拆成獨立子題時 countable=false，子題才計數。相關與交叉題的唯一 ID 聯集為相關題數；不相關與未確認分開列出。主題命中數逐 topic 計唯一題 ID，不累加重複標籤。

恢復前核對 selection 的來源雜湊、study_subject、target_exam、classification_version；任何變更都要重新分析受影響部分。統計基於已確認且 countable 的題目，揭露未確認部分與分母，不以零填補未解析資料。
