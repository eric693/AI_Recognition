# AI 智慧報帳系統

## 功能
- 單張 / 批次上傳發票圖片（JPG、PNG、WEBP、GIF、PDF）
- 透過 OpenAI GPT-4o 自動辨識發票欄位：
  - 發票編號、日期、發票人、品名、銷售額、稅額、總計、備註
- 發票列表查詢、搜尋、篩選
- 待確認 / 已確認 / 已駁回工作流程
- 編輯與刪除發票記錄

## 本地開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 複製環境設定
cp .env.example .env

# 啟動
python app.py
```

瀏覽器開啟 http://localhost:5000

## 部署到 Render

1. 將此專案推送到 GitHub
2. 在 Render 建立新的 **Web Service**
3. 連接 GitHub repo
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
6. 環境變數：
   - `SECRET_KEY`：隨機字串
7. 若需要持久化上傳檔案，在 Render 建立 Disk，掛載至 `/opt/render/project/src/uploads`
8. 若需要持久化資料庫，建立 PostgreSQL 服務，設定 `DATABASE_URL` 環境變數

## 注意事項
- OpenAI API Key 儲存在瀏覽器 localStorage，不會傳送到伺服器以外的地方
- 每次 AI 辨識會呼叫 GPT-4o，請注意 API 費用
- 免費方案 Render 的 Disk 不支援，建議使用 PostgreSQL + 外部儲存（如 Cloudinary）處理圖片
