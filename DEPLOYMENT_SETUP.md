# 雲端發佈與部署管道

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
