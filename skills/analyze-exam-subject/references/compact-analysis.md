# 精簡考點分析（預設）

降低重複輸入與輸出，保留逐頁視覺核對。Python 不呼叫 AI，也無法控制宿主是否攜帶歷史上下文，不保證 Token 節省比例。

## 抽樣建立分類表

**進入條件**：已選取來源、渲染並確認宿主能力結果。

**輸入**：selection、render-index、樣本圖片；無視覺時使用可讀文字作暫定分類。

**操作**：

先完成 selection 與 render，依 [視覺能力確認](visual-review.md#宿主能力與逐頁檢查) 驗證宿主能力，再從已選來源抽樣建立分類表；不要只用第一份試卷或最初 5 頁決定整批主題。抽樣用來建立分類，不代替後續完整逐頁分析。

- 依已知官方科目、年度及考試分組，優先涵蓋不同官方科目、較早與較近年度、不同考試及合科卷。同雜湊來源只抽一次；metadata 未知的已選來源另列，不能因無法分組就略過。
- 從代表試卷選有題目的頁面，兼顧卷首、中段與後段，避免只讀封面或前幾題。小量來源可直接檢視全部題頁；大型來源先做一輪分散抽樣，若仍出現新的主題或分類邊界，再針對缺口補樣，不要求遍歷所有分組組合。
- 直接從 render-index 取樣並開圖，無需先建立 work 或 packet。讀跨頁題時補齊相關頁；無視覺時只能以可讀文字建立暫定分類，遵守視覺文件的 partial 規則。
- 在執行目錄保存簡短的 `classification-notes.md`：抽樣來源雜湊與 PDF 頁碼、涵蓋的科目／年度／考試、未覆蓋範圍，以及容易混淆主題的納入／排除界線。它是宿主工作筆記，不是腳本新增的輸入格式。

依實際樣本建立精簡主題表；既有分類表可作起點，但須核對是否適用。主題粒度應一致，不把每道題目各建一類，也不把未在資料中觀察到的考點當成既有命中。確認後保存 `topics.json`，設定 selection 的 `classification_version`，再執行 prepare。topics.json 格式為：

```json
{"topics":[{"topic_id":"TREE","label":"樹"},{"topic_id":"STACK","label":"堆疊"}]}
```

後續遇到分類表未涵蓋的相關題，不得硬塞入既有主題或標為不相關。先記錄分類缺口；需要新增、拆分或合併主題時，更新分類表與版本、重新 prepare，依新版重新核對已分析題目，不混用新舊結果。分類表變更的快取失效規則見 [分析恢復與快取](#分析恢復與快取)。

**產出**：宿主保存 topics、classification-notes，設定 selection 的 classification_version。

**完成條件**：跨來源抽樣涵蓋範圍、未覆蓋範圍及分類界線已記錄。

**例外與下一步**：分類就緒進入 [批次分析](#批次分析)；發現缺口依本節修訂並重新核對。

## 批次分析

**進入條件**：分類表及版本已設定。

**輸入**：inventory、selection、render-index、topics。

**操作**：

- 不轉錄全文、不逐題解題、不為一般明確分類撰寫摘要或理由。
- 圖片是主要證據，擷取文字留在本機；僅在文字過小、程式碼／公式或題號不明時讀取相應片段，不重複傳入整頁文字。
- 每批預設 5 頁，只讀當批 packet、必要跨頁資訊與分類表，不整份讀 work.json 或過去結果。宿主仍可能保留歷史，此工具不是隔離上下文的 API 執行器。
- 跨頁題額外讀同卷必要頁，完整後只輸出一次；不要為批次大小切斷題目。
- 最後讀 summary.json 寫報告，不重新讀全部圖片和索引。需要代表題內容時按 ID 回查少量題目。

在技能資料夾執行；範例路徑改為實際檔案並加引號，輸出用新檔。

```shell
python scripts/compact_analysis.py prepare --inventory inventory.json --selection selection.json --render-index render-index.json --topics topics.json --output work.json
python scripts/compact_analysis.py status --work work.json
python scripts/compact_analysis.py packet --work work.json --batch-id S來源短ID-1 --context 1 --output packet-001.json
```

`prepare` 檢查每個已選來源都有 render 索引且至少一頁可用；缺頁產生 warning 與 missing_pages，不能視為完整。stdout 顯示實際 batch_id；`status` 列出待處理批次與已完成快取，不必讀完整 work.json。

work 是本機完整登錄，不要整份放入模型上下文。packet 只包含當批圖片、source ID、角色、主題表及必要規則；`--context 1` 會額外帶入同卷前後各一頁到 `context_pages`，只供跨頁題續讀，不重複計算。宿主須實際開圖，不把圖片路徑當圖片內容。每批結果獨立落盤，不在聊天中重述全部題目。packet 的 `rules` 包含資料與指令的邊界、答案頁不可當題、跨頁與 `reviewed_pages` 規則；`target_exam` 不會放進 packet，避免模型只分類目標考試。

依 [AI 精簡回覆](#ai-精簡回覆) 填結果，逐頁核對依 [視覺檢查](visual-review.md#宿主能力與逐頁檢查)。

**產出**：工具生成 work、packet；宿主逐批保存結果與 visual-review。

**完成條件**：當批題目及頁面狀態已如實保存；跨頁題未讀完整不得標完成。

**例外與下一步**：持續下一批；結束或只能部分交付時進入 [合併與統計](#合併與統計)，中斷查 [分析恢復](#分析恢復與快取)。

## AI 精簡回覆

本節為宿主填寫格式；輸入是當批 packet 與實際開啟的圖片，輸出為獨立 result 檔。

依題意標註相關、交叉、不相關、待確認，相關與交叉題歸入主題／子主題。例如備考科目是資料結構：樹操作可納入；純 SQL 不因卷名含資料結構就納入；索引與樹的交叉題可多標籤。保留原官方科目供追溯。

官方答案只在考試、試卷與題號可靠配對時套用。原答案、更正答案與 AI 推導分開標記；有明確更正關聯才採更正版並保留原版，對應不明則揭露衝突，不猜測。

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

填妥後回到 [批次分析](#批次分析) 保存結果，格式正確不能代替圖片核對。

## 合併與統計

**進入條件**：已有批次結果或完整快取；可合併部分結果但需揭露缺口。

**輸入**：work、結果、visual-review；有文字擷取時必須傳 extraction。只改目標考試可直接使用既有 question-index。

**操作**：

目標條件格式見 [selection](output-contract.md#selection)。這只影響主統計，不擴張選取，也不納入分析快取；`summary --target` 可在同一份索引切換。分類表內容變更即使沒改版本也會失效快取。

```shell
python scripts/compact_analysis.py merge --work work.json --results result-001.json result-002.json --visual-review visual-review.json --extraction extraction.json --output question-index.json
python scripts/compact_analysis.py summary --index question-index.json --target "高考" --examples 2 --output summary.json
```

`merge --visual-review` 核對實際圖片證據；`--extraction` 補文字頁數與判讀方式。`--fast-verify` 使用 prepare 記錄的 mtime／size 跳過未變更檔案的雜湊，預設完整重算；`--require-complete` 可在交付前拒絕 partial。

merge 補回完整來源、圖片證據、長題目 ID 與判讀狀態；相同題號、起始頁及來源的完全相同結果去重，衝突拒絕合併。同題來源與內容不能靠相似字串強行合併。merge 也會用 visual-review 的實際圖片證據覆寫 image_evidence，並把 `unreadable` 頁面維持在 partial。

summary 只計 countable、completed 的相關／交叉題；多標籤不重複增加總題數。指定目標考試時主統計採精確篩選，其他題列補充數量；保留年度／考試分層。未建立命題事件對應時 `event_count` 為 null，不以分層推估。報告只讀統計與每主題最多 2 題代表來源。

同內容只解析一次，不代表相同命題事件。不同年度或考試的重複題保留各次出現；同場共用一卷的多類科不重複計獨立命題。疑似重複只標記，不自動合併；事件關係不明時揭露不確定性。

區分唯一練習題數、主題命中數與命題事件數；未建立事件對應時 event_count 為 null。獨立子題計數時不再累加父題；多標籤題總數只算一次，主題命中按各 topic 的唯一題 ID 計數，百分比說明分母。配分必須在來源中明確且可歸屬，不將合科整卷分數算入備考科目。

**產出**：工具生成 question-index（schema 3）與 summary（schema 2）。

**完成條件**：檢查衝突、warnings、coverage、未確認部分與分母；未解析資料不以零填補。

**例外與下一步**：只改報告格式或目標考試時只重跑 summary 並另存，不重跑分類。依 [報告與交付](report-template.md#報告與交付) 寫報告。

## 分析恢復與快取

**進入條件**：已有舊索引或工作進度，或來源／分類／報告設定改變。

**輸入**：目前來源、圖片、selection、topics、先前 question-index；工作進度用 status 查看。

**操作**：

```shell
python scripts/compact_analysis.py prepare --inventory new-inventory.json --selection new-selection.json --render-index new-render-index.json --topics topics.json --prior question-index.json --output new-work.json
```

來源、圖片、備考科目、分類表及分類版本一致，且整卷已核對並無待確認題，才跳過 AI。新增試卷只新增工作；修改報告格式或目標考試只重跑 summary 與寫報告。全數命中時可省略 `--results`（或使用 `{"results":[]}` 空結果檔）。

快取以整份 PDF 為單位，analysis key 只包含備考科目、分類表、分類版本、protocol、PDF 與圖片雜湊及來源角色；目標考試、年度、考試名稱、官方科目等報告 metadata 不參與快取，修正或補登時不必重跑 AI。部分完成、待確認題、重繪圖片仍保守重跑該卷；變更分類表／分類版本則全部失效，不假裝已實現單題差異重算。沒有 compact cache 的舊索引須先完成一次新流程。prepare 後來源或圖片變更，merge 拒絕沿用。

`--no-cache` 可強制重跑，`--prior` 重用完整快取。修正或補登報告 metadata 後重跑 merge／summary，不重跑 AI；只改 target_exam 可直接重跑 summary。

**產出**：新的 work 與可重用快取，列出待處理批次。

**完成條件**：已核對實際來源、圖片與分類，不僅憑舊完成標記跳過。

**例外與下一步**：文字或圖片恢復分別查 [文字擷取恢復](usage.md#文字擷取恢復)、[渲染恢復](visual-review.md#渲染恢復)。待處理工作回到 [批次分析](#批次分析)，全快取命中直接 [合併與統計](#合併與統計)。
