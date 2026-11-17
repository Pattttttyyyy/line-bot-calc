# 雲端發佈與部署管道

## 啟動完成後才監聽連線（2026-11-17）

22:09正式部署完成：Actions #45（44099419706）通過417項測試（169離線含真實Gunicorn假應用啟動測試、53 SDK/部署、135隔離PostgreSQL、36停用扣點草稿mock、24隔離journal）。source `17e7f52df901edc152309191dfd4704795e5aee7`，修訂 `line-bot-calc-gh-44099419706-1` 接100%流量，HTTP200，映像 `sha256:3620da895e00c0a317028ece95e0828fd1e27a4594446b1cf90916aa68ffb1d4`。原資源及按請求計費通過部署前後核對。

Actions #46（44100172726）唯讀驗證46列服務日誌，未截斷：22:09:19.640新實例開始，22:09:31.450輸出`BOT_APPLICATION_READY_MS 10427`，22:09:31.454才通過TCP啟動檢查，22:09:35首頁健康200。證明應用準備完成先於TCP readiness；10.427秒是初始化耗時，不是正式發卡秒數，沒有據此聲稱真實交易延遲全部消失。沒有LINE試發、正式出入庫/調帳/撤回、IAM、排程、報表映像或常駐計費變更。

21:26的拿卡事件於21:31:58才進入拿卡路徑，`GROUP_SALE_EVENT_LAG_MS=320470`；資料庫段connect=8700ms、transaction=4533ms，LINE段20898ms、total=35700ms，送出後試算表段34364ms。該訂單資料庫建立時間為21:32:05，因此主要約五分鐘等待在拿卡處理之前，無法用送出後同步解釋。同期21:26:38新實例啟動，21:26:39 TCP啟動探測已通過。此證據定位接單前等待，但沒有單獨量測CPU節流或Google初始化各段，不宣稱每一秒原因已證實。

原Docker CMD未preload；Gunicorn可在應用載入前先監聽，預設TCP探測因而過早通過。現在加`--preload`，應用與既有初始化完成後才監聽。仍單worker、8threads、timeout0；原1CPU/512Mi、min0/max1、按請求計費與同步鎖分離保留。新增僅含數字的`BOT_APPLICATION_READY_MS`供後續日誌查核。部署預檢遇command/args覆寫則停止，避免映像CMD修正未生效卻回報成功。

本機真實Gunicorn搭配假WSGI應用的三個測試：舊啟動方式可重現初始化阻塞時TCP已開啟；新方式在初始化完成前不監聽、完成後第一筆HTTP正常；初始化失敗時不開啟連線。測試不import正式app、不連正式Google/LINE/DB。正式啟動本來就會讀既有Google工作表，此修正將該讀取留在啟動階段，不新增資料寫入。

此變更不等於所有慢點已消失，需以後續正常流量核對新啟動及事件時間。冷啟動仍存在，LINE兩秒逾時與長期可靠收件佇列仍需另評估；沒有切換instance-based billing或增加常駐實例。Google官方依據：https://docs.cloud.google.com/run/docs/tips/python 。

## 2026-11-17 延遲事件唯讀查核

管理者已執行 scoped view 設定，Actions #41（44094647559）與 #42（44095802392）實測 log_access=available。#42 通過412項測試，僅讀指定服務兩個固定事件時段共25列，未達500列上限。正式修訂仍為 line-bot-calc-gh-44051022014-1；此次未部署應用或變更計費。

安全摘要顯示：14:54:26 的 /callback 請求 latency=1.687911 秒，14:57:38、14:58:14 的 /callback 亦約1.99秒；發卡分段日誌於14:58:36輸出 connect=9900ms、transaction=3799ms，14:58:45輸出 line_api=6200ms、total=22200ms。既有資料庫唯讀查核該筆訂單 created_at 為14:58:32，故不能把前面的 HTTP 200 或約2秒當作發卡完成秒數。18:24:44及18:29:59兩筆同為 /callback（非健康檢查），latency分別1.765356、1.989511秒；使用者畫面庫存回覆時間為18:30:21及18:30:22。

LINE 官方文件明確指出 webhook 未於2秒內回應會逾時；Google 官方文件指出按請求計費時，請求外CPU會停用或嚴重受限，後續請求可能喚醒原工作。目前 callback 同步處理全部工作，Cloud Run min0/max1、request-based billing。觀察與「LINE連線逾時後尚未完成的處理受CPU限制、後續流量喚醒」一致，但尚無該程序CPU節流或請求關聯追蹤，應標示推論，不能宣稱已證實或修復。禁止僅據HTTP 200宣稱回覆在2秒內完成。

早先備選方案（未採用，22:09已優先部署上述啟動順序修正）：可先在原1CPU/512Mi、min0/max1維持不變下，試行關閉CPU閒置節流（instance-based billing），並以後續正常指令觀察；會改變計費，尚未執行。若單一實例連續運作30天，按公開單價粗估CPU與RAM約US$49.25（未扣免費額度，不含網路、日誌、映像與其他服務），實際min0用量可能較少；不是帳單上限。此試行不取代長期可靠收件佇列及快速HTTP回應的設計。

參考：https://developers.line.biz/en/docs/messaging-api/check-webhook-error-statistics/ 、https://docs.cloud.google.com/run/docs/tips/general 、https://cloud.google.com/run/pricing 。

## 唯讀延遲診斷

2026-11-17 使用者已核准新增「僅小仙女服務日誌」唯讀存取。一次性設定程式 `ops/grant_service_log_view.py` 由專案管理者在已登入的 Cloud Shell 執行，不由CI自我授權。它讀取 `_Default` sink 找到既有日誌bucket位置，建立或核對 `line_bot_calc_latency` view。filter同時限定來源專案、cloud_run_revision、line-bot-calc及asia-east1；現有view範圍不同時停止，不擴大或覆寫。核對服務範圍後，只在該view資源上授予既有line-bot-ci-deploy `roles/logging.viewAccessor`，保留其他binding，最後唯讀核對filter及binding。沒有專案層Logging Viewer、Secret存取、新bucket或新憑證。

診斷只透過該view查詢，不回退到整個專案。`GCP_LOG_VIEW_LOCATION` 是production environment的非機密位置變數，未填預設global；設定程式回傳其他位置時需填入實際值。自動化測試mock所有Google呼叫，不能更動正式IAM。僅準備好程式或測試成功不代表授權已設定；需管理者執行成功並由雲端診斷讀取驗證。

`diagnose` 只讀取現有服務版本、HTTP併發與逾時設定，以及2026-11-17 18:15～18:35台灣時間的指定服務日誌。使用既有production身分及核准，不新增IAM權限。日誌僅在記憶體擷取，公開摘要白名單限時間、HTTP狀態／秒數、啟動標記及固定格式分段耗時，不輸出訊息、卡密、URL、IP、使用者或群組ID。讀取被拒絕時只回傳permission_denied，不自行擴權；此模式不部署、不呼叫Webhook、不發LINE、不讀Secret或資料庫。

## 自助拿卡與試算表同步鎖分離（2026-11-17）

`group_sheet_queue` 的 Google 呼叫不再持有群組帳戶列鎖。同步使用獨立、不等待的 transaction advisory lock，避免同群多個 HTTP 執行緒排隊做相同同步；另一個同步執行中時保留 pending 並立即回傳 busy。僅鎖定正在同步的原交易列，與既有撤回的交易列鎖互斥；先取得交易列鎖，再用新查詢讀取撤回狀態，避免把已撤回或已重售卡寫回舊銷售狀態。交易列已被撤回占用時 NOWAIT 保留 pending，不等待帳戶鎖。

新拿卡仍依既有群組帳戶鎖原子驗證權限、額度、扣庫存及記帳，再回覆 LINE。同一 HTTP 請求的回覆後同步每次最多處理10筆，沒有背景執行緒、新服務或新排程。此設計移除試算表對下一筆拿卡的帳戶鎖阻擋，不代表所有外部延遲均已消除；HTTP 執行緒、冷啟動、資料庫或 LINE 網路仍可能影響速度。忙碌、Google 失敗或達10筆上限時剩餘 pending 由後續拿卡或管理員原群 `/同步發卡紀錄` 重試；沒有獨立自動定時保證。資料庫為庫存與欠款依據。

新增 `GROUP_SALE_EVENT_LAG_MS` 與 `GROUP_SHEET_SYNC_MS`／`GROUP_SHEET_SYNC_BUSY`，只記錄耗時與筆數，不記錄卡密、訊息、群組或使用者ID。隔離 PostgreSQL 測試涵蓋 Google 停滯時下一筆仍能完成、額度拒絕、忙碌保留待補、撤回與重售一致性。正式部署狀態以 Actions 的版本及健康查核為準。

Actions #37（44051022014）通過397項測試（166離線、36 SDK／部署、135隔離PostgreSQL、36停用扣點草稿模擬及24隔離journal）。來源 `1aba9e0369fce81ad19f9e43cf86e45b2a3c5483`；正式修訂 `line-bot-calc-gh-44051022014-1`，100%流量、HTTP200，映像 digest `sha256:4288fbabb681dbd484baadd40f930518c044a3aa93dd58706212f78cde08ab9b`。512Mi／1CPU／min0／max1及原按請求計費保留。沒有結構遷移、正式發卡／撤回／調帳、LINE試發、排程／每日報表映像或Render變更。未取得正式發卡新秒數，不把隔離測試時間當實際使用速度。

## 每日報表工作

每日銷售統計包含所有群組的有效正式訂單及有效手填銷售，收件群組只用於 LINE 發送。測試商品沿用獨立測試帳。未填實售價的訂單會列出未填筆數。

每日報表使用獨立的 Cloud Run 工作 `line-bot-daily-report`。工作流程新增 `report-check` 與 `report-deploy`，都使用 production 核准及現有部署身分。`report-check` 只讀取該工作並輸出映像、執行入口及 generation。`report-deploy` 的 expected_revision 必須填入剛查核的 `report-generation-N`，只更新該工作的映像，保留其他工作設定。部署流程不執行工作。工作本身依既有排程發送。

這兩個模式沒有新增 IAM 權限。現有部署身分若無權讀取或更新報表工作，操作會停止並回報 permission_denied，需由有權管理該工作的維護者完成更新或核准適當的資源範圍授權。

2026-11-03已經使用者核准啟用，GitHub-hosted查核與首次部署皆成功。最新操作結果以Actions工作紀錄為準，歷史版本見README。

## 能做什麼

雲端GPT透過GitHub連接建立分支、改程式、提出PR；經使用者確認後合併。
合併功能程式不會因這份流程自動部署。另更新main的 `ops/cloud-request.json` 才會提出雲端查核或部署請求，也能在GitHub網站手動Run workflow。
目前GitHub連接沒有直接workflow_dispatch工具，且寫入曾回覆403，因此提供「改請求檔案」的網頁入口，不依賴原電腦、Cloud Shell或Google Cloud網頁。若連接仍不可寫，使用已登入的GitHub雲端瀏覽器，不索取Token貼到對話。
首次合併的check請求已成功完成唯讀查核。現有cloud-readonly與production environments分開使用已建立的短期Google身分。

部署須在請求裡明確指定已核准的40碼source_sha、目前正式expected_revision，並把mode改deploy。source_sha必須在main歷史內，不能輸入任意分支或shell指令。
每次會在無正式憑證的job跑測試，再由production environment核准後取得短期身分。雲端新對話可讀取Actions狀態與安全摘要；核准步驟可在手機GitHub頁面完成。

## 已核准並設定的權限

專案 line-bot-sheet-510416，地區asia-east1；只維護既有Cloud Run服務line-bot-calc及Artifact Registry倉庫line-bot。
新增一個Workload Identity Pool `line-bot-github`、一個GitHub OIDC provider，以及兩個不產生JSON金鑰的服務帳號：

|身分|範圍與角色|
|---|---|
|line-bot-ci-view|指定Cloud Run服務的roles/run.viewer，只讀服務中繼資料；固定格式摘要不輸出環境值|
|line-bot-ci-deploy|指定Cloud Run服務的roles/run.developer；指定Artifact Registry倉庫roles/artifactregistry.writer；只對既有runtime服務帳號line-bot-run授予roles/iam.serviceAccountUser|
|GitHub cloud-readonly環境|只可透過roles/iam.workloadIdentityUser模擬line-bot-ci-view|
|GitHub production環境|只可透過roles/iam.workloadIdentityUser模擬line-bot-ci-deploy|

不授予Owner/Editor，不把Secret Accessor或Supabase憑證授予CI；不新增長效金鑰。runtime原有Secret存取保留。
roles/run.developer具有指定服務的管理能力，故必須以production核准及repo/main/workflow信任條件保護，不可把它說成完全沒有刪除能力的自訂角色。若實測CLI需要超出上述範圍的權限，先列明新增需求，不能自行擴權。
要建立WIF、帳號與綁定，需由既有專案管理身分執行；這是新增安全敏感存取，必須由使用者先確認。

## WIF信任限制

- 固定數字repository_id `965158440` 與repository_owner_id `207344761`，啟用時再核對。
- assertion.ref只允許refs/heads/main。
- assertion.workflow_ref只允許 `Pattttttyyyy/line-bot-calc/.github/workflows/cloud-control.yml@refs/heads/main`。
- assertion.sub只允許此repo的environment:cloud-readonly或environment:production；兩個subject分別綁定自己的SA。
- forks、PR工作流程與其他repository沒有雲端身分。映射google.subject=assertion.sub並以repository_id、owner_id/ref/workflow_ref做provider條件。

## GitHub設定

建立cloud-readonly與production environments，只允許main。production使用必須核准的reviewer（使用者本人可核准；只有一位維護者時不開啟禁止自我核准）。不允許管理員略過核准規則。
兩個environment各自設定GCP_WIF_PROVIDER、GCP_CI_SERVICE_ACCOUNT、GCP_PROJECT變數。沒有Token、JSON金鑰、LINE資料庫密碼。
公開庫的Actions紀錄可被讀取，因此只輸出白名單版本／image digest／健康與固定資源值；不印完整服務設定或gcloud錯誤內容。不能上傳未篩選的服務log為artifact。
官方Actions已鎖完整commit SHA；升級前重新核對官方來源。GitHub驗證、WIF交換、資源範圍與首次部署已經線上驗證成功。production要求Pattttttyyyy核准，禁止略過規則，只允許main；cloud-readonly只允許main。

## 部署行為與限制

- 先確認目前100%服務版本與expected_revision一致，資源維持512Mi/1CPU/min0/max1、按請求計費、關閉啟動CPU加速；不同便停止。
- 使用測試通過的SHA建置並推送映像，依digest部署，先--no-traffic；確認執行時設定未變才切100%。
- 切流量後首頁健康失敗時嘗試切回上一版；不刪修訂版本、不碰庫存／訂單，不發LINE測試事件。若回復也失敗，必須人工處理並如實回報。
- 不更新每日報表工作、Secret值、服務帳號權限、LINE Webhook、Render；不自動跑migration。
- 新建置使用既有未鎖版requirements，首次啟用前仍應建立經測試的套件鎖檔；不能把新建置當成與歷史映像逐位元一致。
- 會產生GitHub Actions、映像儲存、網路及Cloud Run執行用量；使用者須核准啟用與第一次實際建置／部署。
- 上傳或合併main之前，必須先唯讀確認Render與其他外部整合的自動部署設定。若會連動正式服務，需告知並納入具體核准範圍，不能逕自停用Render。

## 目前狀態

已設定WIF/IAM與兩個GitHub環境，17項管道測試及首次GitHub-hosted部署成功。min0待命版本以latestCreatedRevisionName及修訂Ready狀態檢查；latestReadyRevisionName在無流量時可能仍指向原服務版本。首次成功版本為line-bot-calc-gh-41648505312-1；以最新Actions查核結果為準。

參考：
- https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines
- https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
- https://github.com/google-github-actions/auth

## 預付款版本上線順序（2026-11-04已完成）
1. 執行離線、SDK及隔離 PostgreSQL 測試；隔離庫需驗證 `enable_group_prepay.sql` 升級及原餘額保留。不可用正式帳款或發卡驗證。
2. 確認正式修訂仍為預期版本。先部署相容正負淨額的新程式並完成唯讀健康驗證；此時舊約束仍會拒絕超額收款。
3. 新版健康後，使用受控 migration 套用 `enable_group_prepay.sql`。只調整 `group_credit_accounts_debt_check`，不更動既有帳款、庫存、訂單、額度或成員。唯讀驗證約束與版本；不代使用者補登款項。
4. 一旦帳戶有負淨額，不可回滾至只接受非負欠款的舊程式或還原舊約束；需使用相容預付款的版本修復。若第3步未成功，保留新版並修復約束升級；不可宣稱預付款已開通。
2026-11-04恢復GitHub寫入授權；Actions #14（41832942467）完成253項測試與部署。正式修訂 line-bot-calc-gh-41832942467-1，100%流量、健康200；程式來源5800148849a996d2bedf8f919a3662f5b2c6c1ab。之後已套用enable_group_prepay_20261104，唯讀確認正負淨額約束有效，RLS保留、匿名庫存存取仍關閉。未代使用者補登款項或做正式發卡測試。

## 撤回上一動更新（2026-11-04）
Actions #15（41835446729）已通過260項測試：135離線、29 SDK/部署、96隔離PostgreSQL。來源 df4177b15c0033a3cf8920227ab8f5ec331a1093；正式修訂 line-bot-calc-gh-41835446729-1，100%流量、健康200。
客人／測試群 /撤回 自動辨識上一筆記帳或自助發卡，兩種均保留原文字確認流程。/撤回上一動 與 /撤回 上一動 為同義指令；重送保留原申請目標，上一筆已是沖回／回收時不自動往前撤其他筆。本次無資料庫結構變更，未執行正式記帳、發卡、回收或LINE試發。

## 直接撤回上一動更新（2026-11-04）
依使用者明確要求，已將客人／測試群主管理員的 /撤回、/撤回上一動、/撤回 上一動 改為立即執行，不再逐筆確認。發卡回收與退款共用同一交易，記帳新增反向沖回，均保留原紀錄及RC／AR撤回編號。仍須由管理員核對卡片兌換情況，系統不宣稱已驗證未使用；指定 /自助撤回 TX 與 /更正記帳 保留原確認流程。
Actions #16（41842749588）通過271項測試（136離線、29 SDK/部署、106隔離PostgreSQL）。來源 b818e5f72e162ef0497466c7e391f6d79e00b9de；正式修訂 line-bot-calc-gh-41842749588-1，100%流量、HTTP200。無結構變更，無正式發卡／回收／調帳／LINE試發；每日報表及Render未改動。

## 不限筆數連續撤回（2026-11-04）
已修正上一版撤回後停止的行為：每個新 /撤回 指令選取最新尚未撤回的原始發卡／手動帳，排除沖回紀錄、已沖回來源及已回收訂單，不限次數直到沒有可撤回資料。同一訊息重送仍不重複退款；每筆保持原子庫存／帳款異動及獨立RC／AR編號。
Actions #17（41845209181）275項測試通過（136離線、29 SDK/部署、110隔離PostgreSQL），包含連續12筆、混合發卡收付款及並發撤回。來源 d9e6aa5d2bae06de237fd657c2939a9ec09974ed；正式修訂 line-bot-calc-gh-41845209181-1，100%流量、HTTP200。未替使用者操作正式撤回或記帳，無資料庫結構變更。

## 測試銷售紀錄（2026-11-04）
Actions #18（41849382042）通過279項測試（140離線、29 SDK/部署、110隔離PostgreSQL），來源89611a95332ce6de08768303c56696efb0e5e821；正式修訂line-bot-calc-gh-41849382042-1，100%流量、HTTP200。
小仙女新增「測試銷售紀錄」（sheetId1743358464），9筆既有測試群訂單已依DB原日期、實售價補入，其中5筆標已撤回。部署後唯讀比對9筆編號與撤回狀態一致、無重複。未重扣庫存或重加帳。後續測試銷售沿用原防重複append機制，撤回僅標記專用頁；不納入正式補單同步或商品卡密頁。


## 標記核准客人拿卡（2026-11-04）
Actions #21（41855215936）通過288項測試（145離線、30 SDK/部署、113隔離PostgreSQL）。來源93fb64d4e4d9f0217d14dc328edf684b6434f3c0；正式修訂line-bot-calc-gh-41855215936-1，100%流量、HTTP200。映像digest sha256:8f5d37a58a6436ef1ab5b183c786509ea6468cc6918fe5843367fe82114cd4b4。
管理員可於已綁定客人／測試群使用 `/自助核准 @成員` 或 `/自助停權 @成員`，必須透過LINE名單選取單一真人標記；原User ID格式仍可用。只修改本群客人拿卡資格，不授予管理員／記帳權，不更動開放狀態、額度或帳款。拒絕全體、機器人、多人及LINE未提供User ID的標記；沿用既有訊息防重複與群組鎖定。
本次無資料庫結構變更，未代使用者核准任何真人、發卡、撤回、記帳或發送LINE測試訊息；每日報表、Render與既有機密設定維持原狀。


## 簡化結帳卡片（2026-11-05）
Actions #22（41989761923）通過288項測試（145離線、30 SDK/部署、113隔離PostgreSQL）。來源5c1cd9e7b14b66f5600a510291b6a94d1b219a83；正式修訂line-bot-calc-gh-41989761923-1，100%流量、HTTP200。
手動記帳及拿卡卡片改為上次金額、本次金額、目前欠款。上次金額為帳款淨額的相反號；本次收款為正、拿卡為負。有預付款時另列正值餘額；查帳保留可拿卡總額，記帳編號、測試標示及同步異常提醒保留。僅顯示調整，未變更金額計算、權限、庫存、資料庫結構、每日報表、Secret或Render；未發送LINE或做正式交易測試。


## 自助群商品別名與分隔符（2026-11-05）
Actions #23（42003964395）通過291項測試（148離線、30 SDK/部署、113隔離PostgreSQL）。來源be80fc2f42cbce90fea10a65a17ab11a8c17d720；正式修訂line-bot-calc-gh-42003964395-1，100%流量、HTTP200。
已綁定自助群的拿卡商品英文不分大小寫，數量接受星號、x、X或空白。1000/1千/一千、3000/3千/三千、5000/5千/五千、10000/1萬/一萬分別正規化為原MY商品代碼，再交既有解析及交易流程。保留多行合併與500張限制；測試群不能透過正式卡別名越權拿卡。無資料庫、價格、額度、權限、卡密或報表變更，未執行正式發卡／記帳或LINE試發。UI_GUIDE已補使用說明。


## 軒自助群MY1000售價更新（2026-11-05）
Actions #25（42017861319）通過292項測試（148離線、30 SDK/部署、114隔離PostgreSQL），包含歷史905元訂單重送不重扣、撤回退905及新單售900。來源ca74d36a199eb3805d2e54f639f85b096df59b60；正式修訂line-bot-calc-gh-42017861319-1，100%流量、HTTP200。
軒MY1000售價改為900（9折）；其餘MY維持0.905，測試test100維持905。僅新自助拿卡採新價；未重算歷史訂單、帳款，未寫正式庫存或LINE試發，環境設定／Secret／每日報表／Render維持既有設定。#24僅因測試預期修正失敗，未部署。

## 自助發卡先回覆再同步試算表（2026-11-05）
Actions #27（42040087413）通過308項測試（154離線、30 SDK/部署、124隔離PostgreSQL），來源68b79dfe2e6ec538306147966d8159475ea31a76；正式修訂line-bot-calc-gh-42040087413-1，100%流量、HTTP200。映像digest sha256:95def34b7793ed9de534ca41ab62fec14421bd62db38a59802be22909ebcbb36。
自助拿卡改為原子DB出庫及記帳成功後先回LINE卡片，然後在同一次HTTP請求內同步試算表。沒有閒置背景執行緒、額外服務或排程。利用既有group_credit_events中sale事件的reason欄位保存sheet:pending:v1／sheet:done:v1狀態；與庫存和帳款同交易新增pending，保留事件原金額、訂單與交付狀態。無結構遷移，舊空白reason事件不自動回補。
每次新發卡處理一筆本群最早pending，失敗保留；管理員原群 /同步發卡紀錄 每次最多10筆。同步只以既有交易為來源、訂單編號防重，不重新發卡或調帳。銷售與商品分頁補寫保留原交易台灣日期；已撤回交易補銷售歷史並標撤回，不重寫已回庫商品卡密狀態。沿用同群帳戶鎖避免與撤回競態，故緊接下一筆同群請求仍可能等待同步；未實測正式發卡秒數。
#26僅測試案例未遵守禁止更換已綁定客戶的規則而失敗，未部署；修正測試資料後#27完整通過。未執行正式發卡、撤回、調帳、試算表補寫指令或LINE試發；512Mi／1CPU／min0／max1、Secret、每日報表及Render維持既有設定。


## 延後 Google 試算表中繼資料讀取（2026-11-17）
23:14 回呼在冷啟動期間 HTTP 499，應用程式約 14.6 秒後才就緒；截至 23:25 的唯讀查詢沒有對應軒訂單；23:29、23:33 另有成功新單，不能由請求取消逕推斷後端永遠不會處理。保留 Gunicorn preload，改將 open_by_key 與帳務表 worksheet 的 Google 中繼資料請求延後至第一次實際使用。初始化失敗可重試，並行首次存取只建立一個完整物件。資料庫發卡及庫存查詢不再因模組載入而先等待 Google。
加入實際 app 匯入測試：以假憑證並阻擋 Google、DB、HTTP 及 socket，確認不連外也能完成初始化、註冊 callback 並通過健康檢查；另驗證延後讀取、並行及失敗重試。未更動交易、價格、額度或同步順序。維持 min0、max1、1CPU、512Mi 及按請求計費；本修正不保證所有冷啟動與 LINE 回覆皆在兩秒內完成，部署後須以啟動日誌驗證，正式拿卡時間仍待真實流量確認。

正式部署 Actions #49（44116291646）通過 422 項測試，來源 636cb78aea2cbcec3390495f9390ca48cb943abf；修訂 line-bot-calc-gh-44116291646-1，100% 流量、HTTP200，原資源及計費模式檢查通過。唯讀驗證 #50（44116956860）：23:39:52 BOT_APPLICATION_READY_MS 3874，隨後 TCP 就緒及健康檢查200。這只驗證應用程式載入改善，非拿卡端到端驗證。部署前 23:29、23:33 的新事件進入 handler 延遲分別628/658ms，但 LINE 回覆階段仍11100/9701ms，整體16665/13274ms；尚有另一段回覆延遲未解決。未回放23:14指令、發LINE測試訊息或進行正式庫存／帳款異動。
