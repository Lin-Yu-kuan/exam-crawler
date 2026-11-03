# 工具使用

本文件負責環境、盤點選取與文字擷取。命令以技能資料夾為目前目錄；範例路徑須替換為 run-dir 回傳的本次目錄。

## 環境

Python 3.10+；盤點只用標準函式庫。PDF 擷取需要 pypdf。缺少時，先提示使用者在選定環境執行 `python -m pip install pypdf`，不靜默安裝。無 OCR 引擎或宿主視覺能力時，掃描頁只能回報待視覺檢查。所有命令中的路徑請加引號；以下以技能資料夾為目前目錄。完整依賴見 [`requirements.txt`](../requirements.txt)：`pypdf>=6,<7`、`pypdfium2>=5,<6`、`Pillow>=10.1`。

逐頁圖片功能需要 `pypdfium2` 與 `Pillow`；缺少時提示 `python -m pip install pypdfium2 Pillow`，不自動安裝。圖片產生不需要視覺模型；判讀需要宿主實際提供圖片工具。無視覺時保留文字分析與待檢視圖片，不使用外部 OCR API。參閱 [視覺檢查及 render 指令](visual-review.md#渲染)。

## 盤點與選取

**進入條件**：使用者提供本機資料夾；未提供時請其選擇或給絕對路徑，不自行掃描。

**輸入**：來源資料夾；manifest 非必需，只讀明確提供且受支援的檔案，使用前讀 [Manifest 邊界](#manifest-邊界)。

**操作**：

先建立唯一目錄，避免不同次分析互相覆寫；命令會印出實際路徑，後續輸出都放在該目錄：

```shell
python scripts/exam_files.py run-dir "D:/題庫" --subject "資料結構"
python scripts/exam_files.py run-dir "D:/題庫" --subject "資料結構" --output-root "D:/分析輸出"
```

工具會清理 `<>:"/\|?*`、控制字元、尾端句點與 Windows 保留名稱（`CON`、`NUL`、`COM1` 等），並使用時間戳加隨機碼。未指定 `--output-root` 時輸出根目錄固定在來源的 `_analysis` 下；來源唯讀時用外部可寫入根目錄，工具仍會拒絕連結目錄。

```shell
python scripts/exam_files.py inventory "D:/題庫" --output "D:/題庫/_analysis/盤點/inventory.json"
python scripts/exam_files.py inventory "D:/題庫" --manifest "D:/下載記錄/manifest.sqlite3" --output "D:/題庫/_analysis/盤點/inventory-with-metadata.json"
```

按官方科目列出編號、試題檔案數、年度範圍與代表檔案。缺少 metadata 時，檔名與資料夾只作提示，必要時以首頁擷取或渲染核對表頭；不將題型當科目，不猜年度或考試。無法確認的檔案列為「科目待確認」，盤點階段不做全文分析。

請使用者命名一個備考科目、選取來源科目，並選填目標考試／等別／類科；沒有多選介面時接受編號列表。已明確指定的範圍不重問；未確認檔案讓使用者依檔案編號選擇，不靜默排除或加入。零選取不分析，AI 建議不能擴張選取。

一次分析一個備考科目，同一來源可供不同次分析使用。選取代表分析來源，不代表整卷都屬於該科。依 [selection 格式](output-contract.md#selection) 保存 selection.json，路徑使用絕對路徑，未知 metadata 寫 null。

盤點檔案讀取失敗、資料夾存取失敗與未知 manifest 列入 warnings；PDF 失敗逐檔回報，其他檔案繼續。命令成功不表示所有 PDF 成功，須檢查每筆 status。

**產出**：工具生成 inventory；宿主保存 selection。

**完成條件**：使用者已確認備考科目與非空來源範圍，未知 metadata 留 null。

**例外與下一步**：表頭不明查 [文字擷取](#文字擷取) 或 [渲染](visual-review.md#渲染) 的首頁預覽。零選取停止；確認後進入渲染。

## 文字擷取

**進入條件**：需要核對表頭或文字片段；全文須先確認 selection。

**輸入**：inventory；全文另需 selection。

**操作**：


```shell
python scripts/exam_files.py extract --inventory "D:/題庫/_analysis/盤點/inventory.json" --pages 1 --output "D:/題庫/_analysis/盤點/headers.json"
python scripts/exam_files.py extract --inventory "D:/題庫/_analysis/盤點/inventory.json" --selection "D:/題庫/_analysis/資料結構/本次/selection.json" --output "D:/題庫/_analysis/資料結構/本次/extraction.json"
```

`--pages` 為一基頁碼，以逗號分隔，例如 `1,2`；未提供代表全文。未提供 selection 時只允許首頁預覽，防止選取前擷取全部試卷。輸出以暫存檔原子發佈，必須是不存在的新檔，不覆寫來源。


**產出**：工具生成 headers.json 或 extraction.json。

**完成條件**：已保存文字與各檔 status，不把擷取成功當判讀完成。

**例外與下一步**：回到原階段；有擷取結果時合併須傳 --extraction；中斷查 [文字擷取恢復](#文字擷取恢復)。

## 文字擷取恢復

**進入條件**：已有舊 extraction.json，需恢復擷取。

**輸入**：舊擷取結果、目前來源、頁碼範圍與環境。

**操作**：

`extract --resume` 接受先前 extraction.json，重用合格結果但仍輸出到新檔。只重用完整擷取，且來源內容雜湊、工具版本、pypdf 版本與頁碼範圍皆一致；不符合時重新擷取。文字與渲染索引不可混用。

**產出**：新的 extraction.json。

**完成條件**：已核對來源、版本、範圍與逐檔結果。

**例外與下一步**：回原階段；圖片恢復見 [渲染恢復](visual-review.md#渲染恢復)。

## Manifest 邊界

只支援本專案 SQLite schema_version 1／2 的 `meta`、`attachments` 與 `snapshot_records` 已知欄位，使用 SQLite `mode=ro` 及 query_only，不呼叫會初始化／遷移資料庫的爬蟲類別。

metadata 只在附件 SHA-256 與實際檔案相符時掛載，保留各 snapshot／record／asset 的關聯；資料庫 path 不作為讀取任意檔案的指令。未知版本、缺表或讀取錯誤回報 warning 並退回檔名／表頭人工辨識。舊版 attachment_versions 不自動猜測歸屬；若舊檔雜湊未命中，視為 metadata 未確認。
