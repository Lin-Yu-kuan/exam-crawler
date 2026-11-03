# 📚 國考刷題神器 (MOEX Exam Paper Crawler & Organizer)

> 專為國家考試考生打造的**考選部歷屆試題一鍵篩選、排版歸檔與去重下載工具**。  
> 告別在官網一頁頁繁瑣點擊與雜亂檔名，自動將試題與測驗答案依您喜歡的結構整理成個人題庫！

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://exam-crawler-fpzztbmsmk5szzqla3w5p4.streamlit.app/)

👉 **線上即用網址**：[https://exam-crawler-fpzztbmsmk5szzqla3w5p4.streamlit.app/](https://exam-crawler-fpzztbmsmk5szzqla3w5p4.streamlit.app/)  
*(免安裝任何環境，打開瀏覽器就能直接查題、勾選考科與一鍵打包下載！)*

---

## ✨ 核心特色

1. **多向互相收斂（Faceted Filtering）**
   - 交叉挑選「考試」、「類科」、「科目」，三欄毫秒級同步篩選。
   - 保留未選候選清單供隨時反悔恢復，不遺漏任何一份試題。
2. **靈活的歸檔積木與即時結構預覽**
   - 支援「依科目」、「依年度考試」、「平鋪」及「自訂雙層/單層資料夾」等多種歸檔模板。
   - 檔名自由組合（年度、簡稱、類科、科目、題型），側邊欄即時生成樹狀目錄圖。
3. **智慧去重與驗證**
   - 支援 24 小時查詢快照快取與中斷續載。
   - 相同內容之附件透過 SHA-256 自動去重，絕不浪費頻寬重複下載。
   - 變更歸檔模式後，已下載檔案會**搬移**到新目錄結構（不是只複製），並自動清除舊空資料夾；可一鍵整理與清理重複舊檔。
4. **雙軌部署支援**
   - 🌐 **雲端網頁版**：免安裝任何環境，開啟網頁一鍵打包分類下載為 ZIP 壓縮檔。
   - 💻 **Windows 免安裝版 / 本機版**：原生資料夾對話框，一鍵批次歸檔至指定的本機硬碟資料夾（預設 `C:\國考題庫`）。

---

## 🚀 使用方式

### 方式一：線上網頁版（最推薦，免安裝）
直接點擊前往：👉 **[國考刷題神器 線上版](https://exam-crawler-fpzztbmsmk5szzqla3w5p4.streamlit.app/)**
1. 輸入欲練習的考試或科目關鍵字（例：`資訊處理`、`行政法`、`資料結構`）並點擊「查詢試題」。
2. 在三欄清單中勾選需要的考科。
3. 點擊「開始下載」，完成後點選「📦 下載本次任務題庫包 (ZIP 壓縮檔)」。ZIP 只包含該次任務選取並成功處理的檔案，不混入歷史題庫；已存在或共用的檔案也會納入，同一路徑只收錄一次。畫面顯示實際打包檔案數；雲端 ZIP 檔名與該次歸檔根目錄同名（`國考題庫_YYYYMMDD_HHMM.zip`）。
   - 部分完成可先匯出成功檔案；重試後會合併先前與本次成功成果。提交後調整篩選不會改變原任務的匯出範圍。
   - 若檔案遺失或路徑超出下載根目錄，會停止匯出並說明原因；歷史 PDF 與查詢快取仍保留。
   - 更新後若顯示「程式已更新，請重新啟動應用程式後重新查詢及下載」，需完整重啟服務程序（只重新整理瀏覽器不足）。部署須包含 UI、控制器及任務服務的同一版本，再重新查詢、選取並建立下載任務；既有檔案會先驗證再略過下載。雲端暫存檔若因平台重啟消失，則正常重新下載。

### 方式二：本機執行 (Python)

1. **複製專案**：
   ```bash
   git clone https://github.com/Lin-Yu-kuan/exam-crawler.git
   cd exam-crawler
   ```

2. **建立並啟用虛擬環境**：
   ```bash
   python -m venv .venv
   # Windows PowerShell
   .venv\Scripts\Activate.ps1
   # macOS / Linux
   source .venv/bin/activate
   ```

3. **安裝相依套件**：
   ```bash
   pip install -r requirements.txt
   ```

4. **啟動本機伺服器**：
   ```bash
   python main.py
   ```
   瀏覽器將自動開啟 `http://localhost:8501`。

---

## 📄 授權條款

本專案採 MIT 授權條款釋出。  
考選部試題與解答之著作權與公開資訊權限均屬中華民國考選部所有。
