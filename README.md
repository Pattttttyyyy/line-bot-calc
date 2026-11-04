# LINE 庫存機器人雲端維護

2026-11-03已將正式程式、可攜測試與雲端部署管道同步至GitHub main，PR #2已合併。
Google Cloud網頁無法在GPT雲端瀏覽器開啟時，可改由GitHub Actions執行查核與核准式部署，不依賴原電腦或Cloud Shell。

## 雲端接手入口

1. 新GPT對話先讀本README、DEPLOYMENT_SETUP.md與UI_GUIDE.md，並以GitHub main作為程式來源。
2. 先用 `ops/cloud-request.json` 的check模式查核正式狀態，讀取Actions的cloud工作安全摘要。不要把本節歷史版本當永遠不變的目前版本。
3. 功能修改用分支與PR，先測試並取得本次變更核准後合併。只合併程式不會觸發此工作流程部署。
4. 要部署時，取得使用者對本次變更與用量費用的同意，再更新main的請求檔：mode=deploy、source_sha=已合併且核准的40碼commit、expected_revision=最新查核所見的正式版本，request_id用新的識別文字。
5. Actions會先跑測試，再等待production環境的核准；核准通過才建置映像與部署。可從手機GitHub頁面核准，不必輸入終端指令。建立管道的同意不是對所有未來變更的永久部署授權。
6. 每次完成部署後，把請求檔改回check模式，避免重送舊部署請求。也可在GitHub的工作流程頁手動執行。

目前GPT GitHub連接曾回覆寫入403；不要誤認讀取成功就代表能寫入。可使用已登入的GitHub雲端瀏覽器編輯／提交檔案；新瀏覽器的登入與舊電腦不會自動共用，不要把密碼或Token貼到對話。

## 已完成的線上驗證（2026-11-03）

- [PR #2](https://github.com/Pattttttyyyy/line-bot-calc/pull/2)已合併。
- [首次唯讀查核](https://github.com/Pattttttyyyy/line-bot-calc/actions/runs/41646504595)成功。
- [首次部署驗證](https://github.com/Pattttttyyyy/line-bot-calc/actions/runs/41648505312)成功，source commit為 `0eff49c4ac6fa85ce8b21f26185a832f29456a72`。
- 當次版本 `line-bot-calc-gh-41648505312-1`，映像digest `sha256:1be45dcbe2a404a257c1a64cba0b5dfb4f152d0627a81b68d69e9ab6d79878de`，健康200。512MiB、1CPU、最小0／最大1與既有環境／掛載／服務帳號保留。
- 修正了min0且無流量待命時latestReadyRevisionName尚保留舊版的驗證誤判：現在核對latestCreatedRevisionName與新修訂Ready狀態，再切流量。
- 沒有LINE試發、正式出入庫、撤回、資料庫結構變更或每日報表工作更新。Render設定未變更；依使用者指示略過其登入與自動部署查核，不能據此聲稱Render自動部署已關閉。

## 測試

Python 3.11 或 3.12：`python -X utf8 run_offline_tests.py`。
這組 109 項測試不需正式憑證，不會連正式資料庫、Google Sheets 或 LINE。
官方 LINE SDK 序列化另以 `python -X utf8 -m unittest -q test_line_buttons.py` 驗證，所有發送介面皆 mock。

PostgreSQL 整合測試須在隔離 Linux 測試環境執行 `python run_isolated_tests.py`。
需要 PostgreSQL 16 工具及 psycopg2；執行器只建立 `/tmp/group-credit-pg-*` Unix socket 的拋棄式資料庫，測試拒絕正式連線字串。
不能直接把 test_postgres.py 指向正式資料庫。

禁止直接 import app.py 當離線檢查：啟動程式可能讀取真實服務。
requirements.txt 保留正式版本原內容；尚未建立相依套件版本鎖檔，重建映像不保證套件版本與舊映像完全相同。

## 客人自助拿卡記帳小卡

自助群拿卡成功後，卡片資料仍以可複製文字傳送，最後附上無按鈕Flex記帳小卡：商品計算式、原欠款、本次金額、目前欠款、剩餘額度及訂單編號。沿用已提交的同一筆群組帳款，不另加帳，不影響一般管理群發卡。帳況標示為本筆完成時快照。
小卡本機建構失敗時改回原文字摘要；API發送逾時則保留既有uncertain狀態，不重發、不退款。

## 維護

主程式 app.py；群組權限、欠款及撤回：group_credit*.py；正式文字分流：text_commands.py。bottom_quick.py 已停用快捷，bot_ui.py 的舊選單流程僅保留作回溯參考。
daily_report.py 是獨立每日報表工作的程式，不代表每次機器人更新都應更新報表工作。
操作說明見 UI_GUIDE.md。管理群沿用既有文字出入庫指令與 authorized_users 權限；所有按鈕已停用。客人文字拿卡仍依原核准群組及定價、額度限制；自助撤回改為文字確認碼，保留原本期限、身分綁定與防重複檢查。

機密透過環境變數、Secret Manager 與既有金鑰掛載提供；不要提交 .env、JSON 金鑰、Token、資料庫連線密碼、卡密、正式資料匯出或 *.private.*。
.dockerignore 使用檔案白名單，不把測試、文件、私密設定複製到映像。

## 與最初正式快照的差異

- 正式程式的卡號／密碼說明範例改為明確假資料，未改入出庫邏輯。
- 測試路徑改為獨立目錄可執行；舊版函式的 AST 雜湊取代依賴另一份本機舊 app.py。
- 新增維護文件與測試入口，擴充 Git／Docker 排除規則。
- migration*.sql 僅保存已存在功能的結構參考，不是完整正式資料庫備份，也不可在正式環境自動執行。

雲端發佈、查核與部署流程已啟用，見 DEPLOYMENT_SETUP.md、.github/workflows/cloud-control.yml、ops/。另有17項無憑證管道測試：`python -X utf8 -m unittest -q test_cloud_control.py`；連同109單元、9官方SDK mock、56隔離PostgreSQL，共191項。
不自動修改資料庫結構，不發LINE測試訊息；保留原群組、Secret Manager與Webhooks設定。

## 客人測試群（2026-11-04）
- 管理員在指定的新群傳 `/自助綁定 測試`，建立獨立群組帳戶；預設關閉、欠款0、上限未設定。
- 沿用軒的群組共用額度與核准成員規則：`/欠款上限 金額`、`/自助核准 LINE_ID`，最後 `/開放自助`。沒有軒帳戶可供複製的額度時不得猜測，也不會自動核准其他人。
- `/test100*1` 從現存 test100 商品取一張；測試原價定義為1000，9.05折單價905。商品原名「測試商品100」不代表本次售價基準。
- 測試帳戶僅可拿 test100，軒帳戶僅可拿既有MY商品；後端即使是管理員也不能繞過商品隔離。庫存查詢依帳戶過濾。
- 測試訂單與欠款仍保留於資料庫，標示測試客戶；不寫入正式銷售試算表或商品銷售欄。撤回同樣不碰正式試算表，仍需原有兩步文字確認。
- 沒有自動辨識LINE群組名稱，必須由管理員在正確群組綁定。改群名不會改變帳戶模式。不新增、清空或重置任何測試序號。
