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

target_exam 預設 null；可為完整考試名稱字串，或 exam_name／category／year 的非空精確條件物件。模糊名稱先與使用者釐清。這只影響主統計，不擴張選取。分類表內容變更即使沒改版本也會失效快取。

在技能資料夾執行，路徑須指向實際檔案，輸出皆為新檔：

```shell
python scripts/compact_analysis.py prepare --inventory inventory.json --selection selection.json --render-index render-index.json --topics topics.json --output work.json
python scripts/compact_analysis.py packet --work work.json --batch-id S來源短ID-1 --output packet-001.json
python scripts/compact_analysis.py merge --work work.json --results result-001.json result-002.json --output question-index.json
python scripts/compact_analysis.py summary --index question-index.json --examples 2 --output summary.json
```

prepare 的 stdout 顯示實際 batch_id。work 是本機完整登錄，不要整份放入模型上下文。packet 只包含當批圖片、source ID、角色、主題表及必要指令；宿主須實際開圖，不把圖片路徑當圖片內容。每批結果獨立落盤，不在聊天中重述全部題目。

## AI 精簡回覆

```json
{"results":[{"source":"S實際來源ID","reviewed_pages":[1,2],"questions":[
  {"p":1,"q":"1","r":"r","t":["TREE"]},
  {"p":1,"q":"2","r":"i","t":[]},
  {"p":2,"q":"3(1)","r":"c","t":["TREE"],"why":"索引與樹結構交叉"},
  {"p":2,"q":"4","r":"u","t":[],"why":"圖中符號無法辨識"}
]}]}
```

- source 複製 packet 的 ID；p 是起始 PDF 頁，q 保留印刷題號／子題號；r/c/i/u 是相關／交叉／不相關／待確認，t 是主題 ID。
- 跨頁題加 pages，例如 `[2,3]`；父題拆成子題時加 `count:false`，子題 count 預設 true。
- 交叉與待確認題需 why；一般題不必寫理由。爭議排除也可加 why。
- sample 為可選短代表題摘要，僅少量代表題提供，不抄錄全文。
- reviewed_pages 只列實際看清且已盤點完整的頁面；跨頁題尚未完成先不要標該頁完成。空白／答案頁可標完成但 questions 為空；答案不可當新題目。
- 無視覺時 reviewed_pages 留空，文字暫判可填 questions，但不計入已確認主統計。不可判讀題以 u 及 why 揭露。
- 另存 visual-review.json 的能力與頁面觀察；工具不能證明宿主真的看過圖片。答案版本衝突記入既有觀察及報告，不建立虛構題目。

## 統計與快取

merge 補回完整來源、圖片證據、長題目 ID 與判讀狀態；相同題號、起始頁及來源的完全相同結果去重，衝突拒絕合併。同題來源與內容不能靠相似字串強行合併。

summary 只計 countable、completed 的相關／交叉題；多標籤不重複增加總題數。指定目標考試時主統計採精確篩選，其他題列補充數量；保留年度／考試分層，不猜獨立命題事件。報告只讀統計與每主題最多 2 題代表來源。

```shell
python scripts/compact_analysis.py prepare --inventory new-inventory.json --selection new-selection.json --render-index new-render-index.json --topics topics.json --prior question-index.json --output new-work.json
```

來源、圖片、備考科目、目標、分類表及版本一致，且整卷已核對並無待確認題，才跳過 AI。新增試卷只新增工作；修改報告格式只重跑 summary 與寫報告。全數命中時用 `{"results":[]}` 作空結果檔供 merge 使用。

快取以整份 PDF 為單位。部分完成、待確認題、重繪圖片都保守重跑該卷；變更分類表／目標則全部失效，不假裝已實現單題差異重算。沒有 compact cache 的舊索引須先完成一次新流程。prepare 後來源或圖片變更，merge 拒絕沿用。

先用 10～20 頁記錄實際 usage 與耗時比較。宿主不提供 usage 時只能報文字字元／位元組變化，不能冒充 Token；圖片成本不會因 JSON 變短而消失。
