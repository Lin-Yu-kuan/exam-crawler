# 精簡考點分析（預設）

降低重複輸入與輸出，保留逐頁視覺核對。Python 不呼叫 AI，也無法控制宿主是否自動攜帶歷史上下文，不保證 Token 節省比例。

## 節省原則

- 不轉錄全文、不逐題解題、不為一般明確分類撰寫摘要或理由。
- 圖片是主要證據，擷取文字留在本機；僅在文字過小、程式碼／公式或題號不明時讀取相應片段，不重複傳入整頁文字。
- 每批預設 5 頁，只讀當批 packet、必要跨頁資訊與分類表，不整份讀 work.json 或過去結果。宿主仍可能保留歷史，此工具不是隔離上下文的 API 執行器。
- 跨頁題額外讀同卷必要頁，完整後只輸出一次；不要為批次大小切斷題目。
- 最後讀 summary.json 寫報告，不重新讀全部圖片和索引。需要代表題內容時按 ID 回查少量題目。

## 輸入與命令

先完成 selection 與 render。以最初 5 頁建立精簡主題表並凍結 classification_version，topics.json 為：

```json
{"topics":[{"topic_id":"TREE","label":"樹"},{"topic_id":"STACK","label":"堆疊"}]}
```

target_exam 預設 null；可為完整考試名稱字串，或 exam_name／category／year 的非空精確條件物件。模糊名稱先與使用者釐清。這只影響主統計，不擴張選取，也不納入分析快取；`summary --target` 可在同一份索引切換。分類表內容變更即使沒改版本也會失效快取。

在技能資料夾執行，路徑須指向實際檔案，輸出皆為新檔：

```shell
python scripts/compact_analysis.py prepare --inventory inventory.json --selection selection.json --render-index render-index.json --topics topics.json --output work.json
python scripts/compact_analysis.py status --work work.json
python scripts/compact_analysis.py packet --work work.json --batch-id S來源短ID-1 --context 1 --output packet-001.json
python scripts/compact_analysis.py merge --work work.json --results result-001.json result-002.json --visual-review visual-review.json --extraction extraction.json --output question-index.json
python scripts/compact_analysis.py summary --index question-index.json --target "高考" --examples 2 --output summary.json
```

prepare 的 stdout 顯示實際 batch_id；`status` 列出待處理批次與已完成快取，長流程不必讀完整 work.json。`merge --visual-review` 會用宿主實際開圖紀錄核對 page／image 雜湊，這是指定 `checked` 的唯一可靠來源；`merge --extraction` 會補 `text_extracted_pages` 與 `vision_only`；`merge --fast-verify` 可用 prepare 記錄的 mtime／size 跳過未變更檔案的雜湊（預設完整重算）。`--require-complete` 可在交付前拒絕 partial。`summary --target` 只改變統計目標，不會重跑 AI。

work 是本機完整登錄，不要整份放入模型上下文。packet 只包含當批圖片、source ID、角色、主題表及必要規則；`--context 1` 會額外帶入同卷前後各一頁到 `context_pages`，只供跨頁題續讀，不重複計算。宿主須實際開圖，不把圖片路徑當圖片內容。每批結果獨立落盤，不在聊天中重述全部題目。packet 的 `rules` 包含資料與指令的邊界、答案頁不可當題、跨頁與 `reviewed_pages` 規則；`target_exam` 不會放進 packet，避免模型只分類目標考試。

## AI 精簡回覆

```json
{"results":[{"source":"S實際來源ID","reviewed_pages":[1,2],"questions":[
  {"p":1,"q":"1","r":"r","t":["TREE"],"sample":"樹插入"},
  {"p":1,"q":"2","r":"i","t":[]},
  {"p":2,"q":"3(1)","r":"c","t":["TREE"],"why":"索引與樹結構交叉"},
  {"p":2,"q":"4","r":"u","t":[],"why":"圖中符號無法辨識"}
]}]}
```

- source 複製 packet 的 ID；p 是起始 PDF 頁，q 保留印刷題號／子題號；r/c/i/u 是相關／交叉／不相關／待確認，t 是主題 ID。
- 跨頁題加 pages，例如 `[2,3]`；父題拆成子題時加 `count:false`，子題 count 預設 true。若續頁在另一個批次，使用 `packet --context 1` 讀取 `context_pages`；讀完後在該批重新輸出完整題目並把續頁列入 `reviewed_pages`。merge 不會只因為 `reviewed_pages` 的聯集就標 checked。
- 交叉與待確認題需 why；一般題不必寫理由。爭議排除也可加 why。why 與 sample 都有長度上限，避免把全文塞進精簡欄位。
- sample 為可選短代表題摘要，僅少量代表題提供，不抄錄全文。
- reviewed_pages 只列實際看清且已盤點完整的頁面；答案頁可標完成但 questions 為空；答案不可當新題目。
- 可選 `answers` 記錄官方答案配對（source／p／q／role／answer／basis），由 merge 補成 `answer_refs`；可選 `duplicate_of` 標記疑似相同題，由 merge 補成 `duplicate_candidates`，不自動合併。`score` 為可選非負數字，僅在來源確有配分時提供，summary 不自動推算占比。
- 可選 `parent` 指向父題 ID（父題需 count:false）；可選 `evidence` 寫短證據摘錄（上限 300 字），merge 會補成 `parent_question_id` 與 `evidence_excerpt`。
- 有提供 `merge --extraction` 時，若題目所有頁都沒有 text_extracted，`interpretation_method` 會是 vision_only；否則為 text_and_vision。沒有 extraction 時不猜文字狀態。
- 無視覺時 reviewed_pages 留空，文字暫判可填 questions，但不計入已確認主統計。不可判讀題以 u 及 why 揭露。
- 另存 visual-review.json 的能力與頁面觀察；merge 會核對實際圖片雜湊。沒有 visual-review 或 `host_vision.available` 不是 true 時，所有題目維持 not_checked、整體 partial；只有實際開圖且頁面 checked 才能累積已確認主統計。

## 統計與快取

merge 補回完整來源、圖片證據、長題目 ID 與判讀狀態；相同題號、起始頁及來源的完全相同結果去重，衝突拒絕合併。同題來源與內容不能靠相似字串強行合併。merge 也會用 visual-review 的實際圖片證據覆寫 image_evidence，並把 `unreadable` 頁面維持在 partial。

summary 只計 countable、completed 的相關／交叉題；多標籤不重複增加總題數。指定目標考試時主統計採精確篩選，其他題列補充數量；保留年度／考試分層。未建立命題事件對應時 `event_count` 為 null，不以分層推估。報告只讀統計與每主題最多 2 題代表來源。

```shell
python scripts/compact_analysis.py prepare --inventory new-inventory.json --selection new-selection.json --render-index new-render-index.json --topics topics.json --prior question-index.json --output new-work.json
```

來源、圖片、備考科目、分類表及分類版本一致，且整卷已核對並無待確認題，才跳過 AI。新增試卷只新增工作；修改報告格式或目標考試只重跑 summary 與寫報告。全數命中時可省略 `--results`（或使用 `{"results":[]}` 空結果檔）。

快取以整份 PDF 為單位，analysis key 只包含備考科目、分類表、分類版本、protocol、PDF 與圖片雜湊及來源角色；目標考試、年度、考試名稱、官方科目等報告 metadata 不參與快取，修正或補登時不必重跑 AI。部分完成、待確認題、重繪圖片仍保守重跑該卷；變更分類表／分類版本則全部失效，不假裝已實現單題差異重算。沒有 compact cache 的舊索引須先完成一次新流程。prepare 後來源或圖片變更，merge 拒絕沿用。

先用 10～20 頁記錄實際 usage 與耗時比較。宿主不提供 usage 時只能報文字字元／位元組變化，不能冒充 Token；圖片成本不會因 JSON 變短而消失。
