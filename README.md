# 嶼地特寵醫院盤點系統

手機與電腦共用的醫院耗材盤點系統。Python 3.12 / SQLite，NAS 使用 Docker 部署。帳戶管理流程參考 [krsys](https://github.com/cp296944/krsys)，嶼地使用獨立帳戶資料庫，不與 KRSYS 共用登入。

## 功能

- 總覽：最後實際盤點數量、日期、人員、每週耗用預估、用盡日期；不足 5 天顯示紅字。
- 商品管理：品名、條碼、固定盤點單位、分類、位置、備註與停用；停用保留歷史。
- 批次盤點：空白略過、0 明確保存、盤點人員取自登入帳號，不能冒填其他人。
- 耗用量：最近最多 6 段庫存持平或減少的區間，總減少量 / 總天數。庫存增加的數量與時間整段排除。
- 帳號申請：帳號、姓名、Email、手機、兩次密碼；未核准前不可登入。
- 帳戶管理：核准／不核准、建立帳號、編輯帳號／姓名／Email／手機、查詢／盤點／商品管理權限、管理員、停用、重設密碼、稽核紀錄。
- 「儲存帳號資料」適用所有審核狀態，不變更密碼或授權；改名撤銷既有登入，修改聯絡資料不撤銷登入。
- 密碼接受 **6 至 256 個字元**，允許純數字，不要求大小寫或符號；使用 scrypt 雜湊保存。
- 管理員建立或重設的暫時密碼，首次登入須更換。修改密碼、停用或更改權限會撤銷登入；至少保留一位啟用管理員。
- 保持登入：勾選 30 天；未勾選為瀏覽器工作階段 Cookie，後端最多 8 小時。SQLite 保存 session，重啟或 OTA 不會自行遺失登入。
- OTA：管理員檢查 GHCR 映像摘要，輸入目前管理員密碼後套用；更新前備份 SQLite，健康檢查失敗時自動還原資料及舊容器。
- CSV 匯出；全部時間使用台灣 UTC+8。

只有一筆盤點或沒有有效區間時不估算；零耗用不推算用盡日期。期間補貨但最後庫存仍下降時無法辨識，請依總覽的最後實際盤點數量人工判斷。

## DS920 安裝

詳見 [NAS 部署說明](deploy/NAS.md)。指定資料夾 `/volume3/islevet`，服務埠 **7788**。

在電腦 SSH 登入 NAS，切換 root 後：

```sh
cd /volume3/islevet
curl -fL --retry 3 https://raw.githubusercontent.com/cp296944/islevetah/main/deploy/install.sh -o /tmp/islevetah-install.sh
sh /tmp/islevetah-install.sh
docker exec -it islevetah-app python server.py --init-admin admin
```

最後一行互動輸入管理員密碼，不會將密碼寫入命令歷史。首次建立即可；已有管理員時拒絕重建。

使用 `http://192.168.0.2:7788`。員工從登入頁申請帳號，管理員核准後配置權限。

## OTA 與資料

- 網站容器：`islevetah-app`，唯一發布的主機埠為 7788。
- 更新容器：`islevetah-ota`，7790 僅在 Docker 內部網路使用，不發布至 NAS。
- `/volume3/islevet/data/inventory.db` 同時保存商品、盤點、帳戶、登入與稽核。
- `/volume3/islevet/.env` 保存隨機內部 OTA token，首次安裝自動產生；重裝保留，不需人工填寫。
- `/volume3/islevet/data/backups/` 保存 OTA 前一致性 SQLite 備份；資料夾及檔案不得公開。
- `/volume3/islevet/ota-state/` 保存更新結果；任務附識別碼、階段、下載量、經過時間與還原結果；上一版映像以 `islevetah-app:rollback-任務ID` 保留。
- 備份、.env、SQLite 與實際員工資料不提交 GitHub。

檢查更新只讀取 `ghcr.io/cp296944/islevetah-app:latest` 的遠端摘要，不下載映像。套用時使用已確認的 `sha256` 摘要下載，下載期間網站可使用。舊映像保留成功後，才停網站、備份 SQLite、啟動新版；必須通過 `/api/health` Docker 健康檢查及首頁入口檢查，才刪除舊容器。失敗還原資料庫與舊容器。

更新成功後只清理有 `io.islevetah.ota.managed=app` 標記且無人工標籤的本系統歷史映像；保留容器使用中映像、上一版 rollback 及人工保留標籤。頁面提供清理預覽與管理員密碼確認。預估容量未扣除共用層，實際釋放可能較少；來源不明映像只列人工確認。不使用強制映像刪除或全域 prune。

這次同時修改 app 與 OTA 引擎，既有 NAS 第一次必須透過 SSH 執行 `deploy/install.sh` 更新引擎（保留資料及 .env），之後網站才走映像 OTA。GHCR 的 app 套件須設定為公開，否則檢查／下載將明確失敗。OTA 引擎自身及 Compose、掛載設定使用獨立 SSH 部署，不由網站 OTA 替換。

GitHub Actions 依 push 的完整差異判定受影響服務：`server.py`、`public/`、app Dockerfile 只發布 app；`ota/` 只發布 updater。發布流程設定變更重建兩者；文件與測試不發布。版本以映像摘要為準，不以整個專案 commit 判斷。

重新整理頁面會重新查詢持久化任務，入口中斷自動重試。更新引擎重新啟動時，把未完成任務標記中斷，禁止直接再次更新或清理。管理員須先在 SSH 確認網站健康、容器及資料庫版本；備份 `ota-state/job.json` 後移走此檔，重啟 OTA 引擎解除阻擋。不要在仍有工作執行時手動解除。

OTA 容器為了替換網站容器，持有 NAS Docker socket 管理權；沒有外部埠，須通過網站管理員權限、CSRF 與內部 token。不要把內部更新服務對外公開。

## 本機開發

網站程式本身無第三方套件：

```powershell
python server.py --init-admin admin
python server.py
```

開啟 `http://127.0.0.1:7788`。本機不啟動 Docker 時，OTA 頁會提示服務尚未設定。

驗證需要 OTA 的 Docker SDK：

```powershell
python -m pip install docker==7.1.0
python -m unittest discover -s tests -v
node --check public/app.js
```

GitHub Actions 另外使用真實 Docker 驗證啟動、固定映像 ID 的 OTA 替換、備份，以及帳戶／session／盤點資料保留。

盤點更正目前需管理者處理 SQLite，後續可加入保留稽核歷史的更正功能。GitHub Pages 無法執行此共用後端；GitHub 用於程式碼與更新來源，實際服務執行於 NAS。

## 映像發布與服務更新

PR 合併至 main 並通過 Inventory checks 後，Publish app image 才會發布網站映像：`ghcr.io/cp296944/islevetah-app:latest` 與 `sha-完整GitSHA`。建置標示同一已驗證 commit，發布紀錄包含映像 digest。

帳號資料編輯與密碼規則這次只需更新 **app / islevetah-app**，OTA 引擎不需更新。本次 OTA 引擎升級後改用固定映像摘要下載。

NAS 若仍因 `/app/server.py` 權限錯誤而無法啟動，先下載最新 Dockerfile 並只重建 app，恢復網站後才能透過 OTA 更新帳戶功能。詳見部署說明與 [更新紀錄](CHANGELOG.md)。
