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

## 預付款版本上線順序（尚未執行）
1. 執行離線、SDK及隔離 PostgreSQL 測試；隔離庫需驗證 `enable_group_prepay.sql` 升級及原餘額保留。不可用正式帳款或發卡驗證。
2. 確認正式修訂仍為預期版本。先部署相容正負淨額的新程式並完成唯讀健康驗證；此時舊約束仍會拒絕超額收款。
3. 新版健康後，使用受控 migration 套用 `enable_group_prepay.sql`。只調整 `group_credit_accounts_debt_check`，不更動既有帳款、庫存、訂單、額度或成員。唯讀驗證約束與版本；不代使用者補登款項。
4. 一旦帳戶有負淨額，不可回滾至只接受非負欠款的舊程式或還原舊約束；需使用相容預付款的版本修復。若第3步未成功，保留新版並修復約束升級；不可宣稱預付款已開通。
目前 GitHub 連接寫入403、網頁提交受安全政策阻擋；程式及約束均尚未在正式環境變更。
