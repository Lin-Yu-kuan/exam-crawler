# 本機驗證紀錄

日期：2026-11-02（Asia/Taipei）。環境：Windows、Python 3.14、pypdf 6.21.4。

## 已完成

- `python -m unittest discover -s skills/analyze-exam-subject/tests -v`：8 項中 7 項通過，1 項略過。
- 通過：中文／巢狀路徑、輸出排除、SHA-256 去重及來源保留、損壞 PDF、無文字頁、未選取阻止全文擷取、內容變更失效、恢復範圍核對、已知 manifest 多事件唯讀、未知 manifest 退回、CLI 與輸出不覆寫。
- 靜態檢查通過：兩欄 frontmatter、技能名稱規則、描述長度、所有 Markdown 相對引用、Python 語法與無宿主專用工具要求。
- 合科行為案例已逐項人工核對技能規則：使用者決定來源、題目級排除純 SQL、保留頁碼題號、不同官方科目合併報告。這是指令審查，並非真實考題語意分類測試。

## 限制與待驗證

- 符號連結測試因 Windows 缺少建立權限略過；junction 實際逃逸案例尚未測試。
- skill-creator 的 quick_validate.py 因環境缺少 PyYAML 無法執行；嘗試取得依賴時 PyPI DNS 無法解析。已改用標準函式庫檢查本套件的簡單兩欄 frontmatter 與引用，不宣稱官方驗證器通過。
- 未在 Antigravity 實際載入與操作，也未執行雙平台端到端考題分析；格式可攜不等於雙平台行為已驗證。
- Python 3.10 為設計最低版本，實際執行版本為 3.14；其他 Python 版本與作業系統尚待實測。
- 尚未安裝到任何宿主技能目錄，也未修改全域設定。

此執行環境的系統暫存目錄寫入受限，測試時將 TEMP 與 TMP 設為專案 `.pytest_cache/skill-temp`。一般環境可直接使用系統暫存目錄。
