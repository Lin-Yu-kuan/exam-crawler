# 視覺優先工作流程

## 渲染

`render` 使用 pypdfium2＋Pillow，不是 OCR，也不自行呼叫模型。完整頁面包含文字、點陣圖片與向量內容；圖片格式為 PNG。預設 150 DPI，指定 `--dpi 300` 可重繪細節；接受正數且最高 1200 DPI。單張最多 16,000,000 像素，必要時等比例縮放，actual_dpi 可能小於 requested_dpi。無法安全渲染時回報 failed，不分配超限 bitmap。

所有頁面依序處理並釋放 PDF page、bitmap 與 Pillow image。圖片存於索引旁 `render-images/`；每頁結束即原子更新 render-index。中斷時索引保留 in_progress，可恢復；恢復時先保留舊索引尚未處理的頁，再把本次處理頁覆蓋，不會把已完成的頁截掉。單頁失敗不阻止其他頁。

```shell
python scripts/exam_files.py render --inventory "D:/題庫/_analysis/盤點/inventory.json" --pages 1 --output "D:/題庫/_analysis/盤點/render-index.json"
python scripts/exam_files.py render --inventory "D:/題庫/_analysis/盤點/inventory.json" --selection "D:/題庫/_analysis/資料結構/本次/selection.json" --output "D:/題庫/_analysis/資料結構/本次/render-index.json"
python scripts/exam_files.py render --inventory "D:/題庫/_analysis/盤點/inventory.json" --selection "D:/題庫/_analysis/資料結構/本次/selection.json" --resume "D:/題庫/_analysis/資料結構/本次/render-index.json" --output "D:/題庫/_analysis/資料結構/本次/render-index.json"
python scripts/exam_files.py render --inventory "D:/題庫/_analysis/盤點/inventory.json" --selection "D:/題庫/_analysis/資料結構/本次/selection.json" --pages 2 --dpi 300 --output "D:/題庫/_analysis/資料結構/本次/detail-index.json"
```

詳細重繪只做待看清的來源：先由已確認 selection 複製出該來源子集，避免同一頁碼套用所有試卷。上例的 selection 應為此子集。不要覆寫原完整索引；visual-review 記錄實際使用的 detail-index 圖片證據，`merge --visual-review` 會把該證據寫回 question-index 的 `image_evidence`。

恢復時每個已選來源先驗證當前雜湊，不符便標 failed，要求重新盤點。同一個 output 只允許相同選取、相同 page_scope 與相同渲染設定恢復；變更範圍或設定請用新輸出，避免舊頁被截掉。快取按來源雜湊、頁碼、requested_dpi、像素上限、工具／PDFium／pypdfium2／Pillow 版本匹配。重用前核對 PNG 雜湊、格式及尺寸；丟失或遭修改的圖片重新產生。已在本次 `render-images/` 內的圖片直接沿用，不重複複製；來自其他目錄的圖片才複製到本次目錄。

## 宿主能力與逐頁檢查

1. 檢查宿主實際提供的圖片讀取能力，開啟第一張成功渲染的實際頁面。沒有工具、工具失敗或沒有看到圖片時，不宣稱支援視覺；把結果寫進 visual-review 的 `host_vision`。
2. 若可用，逐頁對照文字與圖片，包括非空文字頁。記錄題號、選項、公式、圖形關係與跨頁延續；圖片清楚但沒有題目也要明確記錄。完成後用 `merge --visual-review`，不要只讓模型宣稱 reviewed。
3. 若不可用，保存所有生成圖片與待檢視頁碼；文字分析只標暫定，不能將依賴圖形的題目標完成。`host_vision.available=false` 時 reviewed_pages 必須留空，merge 會把整體維持 partial。不把缺圖題當成不相關而排除。
4. 清晰度不足時重繪；仍不清晰標 unreadable，附原因。模型能接收圖片不等於能可靠判讀每道手寫或複雜圖表題。
5. 不將整個題庫一次送入上下文；每頁保存觀察與引用。跨頁題若續頁落在另一個批次，必須在讀完續頁的那一批重新輸出完整題目；merge 不會只因 `reviewed_pages` 的聯集就標 checked。

## 覆蓋率

用唯一 `(source_sha256, page)` 為頁面計數單位，重複來源路徑不膨脹分母。分別計選取頁、文字擷取頁、已渲染頁、已視覺檢查頁、未檢查頁、不可判讀頁及渲染失敗頁。處理階段可重疊，不能把各階段頁數相加當總數。`merge` 的 coverage 會輸出這些欄位；`pending_visual_pages` 包含尚未渲染的已選頁，`unrendered_pages` 另列渲染缺口；`text_extracted_pages` 未執行文字擷取時為 null。

未能開啟的 PDF 不知道頁數時，selected_pages=null，另列 unknown_page_count_files，不能假設為 0。題目總數未知時不報題目覆蓋百分比。選取全卷但只做首頁預覽，不得拿首頁當完整分母。

無視覺的模擬驗收：將 host_vision.available 設 false，對所有渲染成功頁設定 not_checked。即使全部有 text_extracted，visually_checked_pages 仍為 0，待檢視清單涵蓋全部頁面，報告 status 為 partial。`merge --visual-review` 遇到 available=false 會拒絕非空 reviewed_pages，避免文字分析被誤標為已完成。
