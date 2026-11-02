# 📚 國考刷題神器 (MOEX Exam Paper Crawler & Organizer)

> 專為國家考試考生打造的**考選部歷屆試題一鍵篩選、排版歸檔與去重下載工具**。  
> 告別在官網一頁頁繁瑣點擊與雜亂檔名，自動將試題與測驗答案依您喜歡的結構整理成個人題庫！

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
4. **雙軌部署支援**
   - 🌐 **雲端網頁版**：免安裝任何環境，打開瀏覽器就能查、選、整包下載 ZIP。
   - 💻 **Windows 免安裝版 / 本機版**：原生資料夾對話框，一鍵批次歸檔至指定的本機硬碟資料夾。

---

## 🚀 快速開始

### 方式一：線上網頁版（免安裝）
直接訪問部署好的公開網址（支援 Streamlit Community Cloud），立即開始選題與打包下載！

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

4. **啟動應用程式**：
   ```bash
   python main.py
   ```
   瀏覽器將自動開啟 `http://localhost:8501`。

---

## ☁️ 部署到 Streamlit Community Cloud

本專案已針對 Streamlit Cloud 完成環境自動適配：
1. Fork 或 Push 本專案至您的 GitHub 帳號。
2. 前往 [Streamlit Community Cloud](https://share.streamlit.io/) 並登入 GitHub。
3. 點擊 **"Create app"**：
   - **Repository**: `您的帳號/exam-crawler`
   - **Branch**: `main`
   - **Main file path**: `app/ui/web_app.py`（或 `main.py`）
4. 點擊 **"Deploy"**，約 1~2 分鐘即刻上線！

---

## 📄 授權條款

本專案採 MIT 授權條款釋出。
考選部試題與解答之著作權與公開資訊權限均屬中華民國考選部所有。

