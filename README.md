# LINE 庫存機器人維護候選版

此目錄是 2026-11-03 正式版本的可攜交接候選檔案，尚未上傳 GitHub、未重新部署。
不要從舊 GitHub main 直接部署；舊 main 缺少目前的多個模組。

## 測試

Python 3.11 或 3.12：`python -X utf8 run_offline_tests.py`。
這組 83 項測試不需正式憑證，不會連正式資料庫、Google Sheets 或 LINE。
官方 LINE SDK 序列化另以 `python -X utf8 -m unittest -q test_line_buttons.py` 驗證，所有發送介面皆 mock。

PostgreSQL 整合測試須在隔離 Linux 測試環境執行 `python run_isolated_tests.py`。
需要 PostgreSQL 16 工具及 psycopg2；執行器只建立 `/tmp/group-credit-pg-*` Unix socket 的拋棄式資料庫，測試拒絕正式連線字串。
不能直接把 test_postgres.py 指向正式資料庫。

禁止直接 import app.py 當離線檢查：啟動程式可能讀取真實服務。
requirements.txt 保留正式版本原內容；尚未建立相依套件版本鎖檔，重建映像不保證套件版本與舊映像完全相同。

## 維護

主程式 app.py；群組權限、欠款及撤回：group_credit*.py；管理／客人選單：bot_ui.py、bottom_quick.py。
daily_report.py 是獨立每日報表工作的程式，不代表每次機器人更新都應更新報表工作。
操作說明見 UI_GUIDE.md。入庫須選品項、貼資料、核對、確認後才寫入。
管理群按「選單」直接顯示十個入庫商品快捷。客人選單仍依原核准群組及定價權限提供商品。

機密透過環境變數、Secret Manager 與既有金鑰掛載提供；不要提交 .env、JSON 金鑰、Token、資料庫連線密碼、卡密、正式資料匯出或 *.private.*。
.dockerignore 使用檔案白名單，不把測試、文件、私密設定複製到映像。

## 與正式快照的差異

- 正式程式的卡號／密碼說明範例改為明確假資料，未改入出庫邏輯。
- 測試路徑改為獨立目錄可執行；舊版函式的 AST 雜湊取代依賴另一份本機舊 app.py。
- 新增維護文件與測試入口，擴充 Git／Docker 排除規則。
- migration*.sql 僅保存已存在功能的結構參考，不是完整正式資料庫備份，也不可在正式環境自動執行。

雲端發佈、查核與部署流程已準備，見 DEPLOYMENT_SETUP.md、.github/workflows/cloud-control.yml、ops/。另有16項無憑證管道測試：`python -X utf8 -m unittest -q test_cloud_control.py`。目前尚未上傳GitHub、建立WIF/IAM或啟用環境，不代表已可遠端部署。
先取得使用者對公開上傳、新雲端權限與首次建置部署費用的確認，再啟用驗證。不自動修改資料庫結構，不發LINE測試訊息。
