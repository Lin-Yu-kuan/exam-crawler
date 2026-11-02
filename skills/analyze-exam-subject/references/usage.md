# 安裝與工具使用

## 安裝

整個 `analyze-exam-subject` 資料夾可直接攜帶，不匯入爬蟲程式碼。從原始套件複製，保留 scripts 與 references 相對結構；不要只複製 SKILL.md。安裝前檢查目標是否已有同名版本，勿盲目覆寫。

- Antigravity 專案範圍：`<專案>/.agents/skills/analyze-exam-subject/`。官方目前預設 `.agents/skills`，並相容舊 `.agent/skills`。參考 https://antigravity.google/docs/skills?app=antigravity-ide （2026-11-02 查核）。
- Codex 個人範圍：`$CODEX_HOME/skills/analyze-exam-subject/`；未設定 CODEX_HOME 時使用 `~/.codex/skills/analyze-exam-subject/`。

本套件不自動安裝或修改全域設定。安裝後依宿主方式重新載入技能，確認清單可見；也可明確要求宿主讀取 SKILL.md。兩個宿主的對話選取介面可能不同，文字路徑及編號選取是共同替代方式。

## 環境

Python 3.10+；盤點只用標準函式庫。PDF 擷取需要 pypdf。缺少時，先提示使用者在選定環境執行 `python -m pip install pypdf`，不靜默安裝。無 OCR 引擎或宿主視覺能力時，掃描頁只能回報待視覺檢查。所有命令中的路徑請加引號；以下以技能資料夾為目前目錄。

```shell
python scripts/exam_files.py inventory "D:/題庫" --output "D:/題庫/_analysis/盤點/inventory.json"
python scripts/exam_files.py inventory "D:/題庫" --manifest "D:/下載記錄/manifest.sqlite3" --output "D:/題庫/_analysis/盤點/inventory-with-metadata.json"
python scripts/exam_files.py extract --inventory "D:/題庫/_analysis/盤點/inventory.json" --pages 1 --output "D:/題庫/_analysis/盤點/headers.json"
python scripts/exam_files.py extract --inventory "D:/題庫/_analysis/盤點/inventory.json" --selection "D:/題庫/_analysis/資料結構/本次/selection.json" --output "D:/題庫/_analysis/資料結構/本次/extraction.json"
```

`--pages` 為一基頁碼，以逗號分隔，例如 `1,2`；未提供代表全文。未提供 selection 時只允許首頁預覽，防止選取前擷取全部試卷。輸出必須是不存在的新檔，不覆寫來源。`--resume` 接受先前的 extraction.json，重用合格結果但仍輸出到新檔。

盤點檔案讀取失敗、資料夾存取失敗與未知 manifest 會列入 warnings；PDF 失敗會逐檔回報，其他檔案繼續。命令成功不表示所有 PDF 都成功，應檢查每筆 status。

## Manifest 邊界

只支援本專案 SQLite schema_version 1／2 的 `meta`、`attachments` 與 `snapshot_records` 已知欄位，使用 SQLite `mode=ro` 及 query_only，不呼叫會初始化／遷移資料庫的爬蟲類別。

metadata 只在附件 SHA-256 與實際檔案相符時掛載，保留各 snapshot／record／asset 的關聯；資料庫 path 不作為讀取任意檔案的指令。未知版本、缺表或讀取錯誤回報 warning 並退回檔名／表頭人工辨識。舊版 attachment_versions 不自動猜測歸屬；若舊檔雜湊未命中，視為 metadata 未確認。

## 驗證

在有 pypdf 的環境執行 `python -m unittest discover -s tests -v`。測試使用自行產生的 PDF，不讀取使用者試卷。`tests/behavior-cases.md` 提供宿主 AI 人工驗收案例；腳本測試不能證明語意分類正確。
