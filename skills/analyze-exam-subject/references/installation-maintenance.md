# 安裝與維護

僅安裝、搬移或維護時閱讀；一般分析從 [階段導航](../SKILL.md#階段導航) 開始。

## 安裝

整個 `analyze-exam-subject` 資料夾可直接攜帶，不匯入爬蟲程式碼。從原始套件複製，保留 scripts 與 references 相對結構；不要只複製 SKILL.md。安裝前檢查目標是否已有同名版本，勿盲目覆寫。

- Antigravity 專案範圍：`<專案>/.agents/skills/analyze-exam-subject/`。官方目前預設 `.agents/skills`，並相容舊 `.agent/skills`。參考 https://antigravity.google/docs/skills?app=antigravity-ide （2026-11-02 查核）。
- Codex 個人範圍：`$CODEX_HOME/skills/analyze-exam-subject/`；未設定 CODEX_HOME 時使用 `~/.codex/skills/analyze-exam-subject/`。

本套件不自動安裝或修改全域設定。安裝後依宿主方式重新載入技能，確認清單可見；也可明確要求宿主讀取 SKILL.md。兩個宿主的對話選取介面可能不同，文字路徑及編號選取是共同替代方式。

## 驗證

在有 pypdf、pypdfium2 與 Pillow 的環境執行 `python -m unittest discover -s tests -v`。渲染測試會在缺少 pypdfium2 或 Pillow 時 skip，不會讓整個測試模組 ImportError。測試使用自行產生的 PDF，不讀取使用者試卷。[宿主行為驗收案例](../tests/behavior-cases.md) 提供宿主 AI 人工驗收案例；腳本測試不能證明語意分類正確。

目前驗證結果與尚未完成的雙平台測試見 [驗證紀錄](../tests/validation.md)。

先用 10～20 頁記錄實際 usage 與耗時比較。宿主不提供 usage 時只能報文字字元／位元組變化，不能冒充 Token；圖片成本不會因 JSON 變短而消失。
