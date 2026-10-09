# DS920 部署與維護

固定設定：NAS `192.168.0.2`，SSH 帳號 `cp296944`，SSH 埠 22，資料目錄 `/volume3/islevet`，網站埠 7788。

## 1. DSM 準備

在套件中心安裝並啟動 Container Manager（舊 DSM 為 Docker），控制台 → 終端機與 SNMP → 啟用 SSH，埠 22。使用有管理員資格的 `cp296944` 登入。

## 2. 電腦連線

Windows PowerShell：

```powershell
ssh cp296944@192.168.0.2
```

輸入 NAS 密碼，畫面不會顯示字元。若出現主機金鑰變更警告，先確認 NAS 的主機身分，不略過驗證。

登入後：

```sh
sudo -i
```

再次輸入 NAS 密碼，下面所有安裝指令都在 NAS root shell 執行。

## 3. 安裝

```sh
cd /volume3/islevet
curl -fL --retry 3 https://raw.githubusercontent.com/cp296944/islevetah/main/deploy/install.sh -o /tmp/islevetah-install.sh
sh /tmp/islevetah-install.sh
```

腳本核對目錄、Docker 與埠號，下載 GitHub main 的固定版本，保留現有資料與 .env，建置兩個容器並啟動。首次需下載 Python 映像與 Docker SDK，時間依網路及 NAS 負載而定。成功結尾會顯示 `Website: http://192.168.0.2:7788`。

安裝腳本只管理嶼地專案，Compose 專案名稱為 `islevetah`，不重建 KRSYS 容器。腳本可能保留暫存原始碼於 `/tmp/islevetah-install.*`；正常重啟後可清除，不包含帳戶資料。

## 4. 建立第一位管理員

```sh
docker exec -it islevetah-app python server.py --init-admin admin
```

輸入密碼兩次，至少 4 個字元，純數字可以。管理員帳號為 `admin`。只做一次；已有管理員會拒絕重建。

## 5. 使用

打開 `http://192.168.0.2:7788`，用 admin 與剛設定的密碼登入。員工點「申請帳號」，填寫姓名、Email、手機及密碼。管理員在帳戶管理點「查看 / 管理」，配置權限後按「核准申請」。

權限：查詢庫存與耗用量、登記盤點、管理商品。管理授權會自動包含查詢；管理員擁有全部功能與 OTA。

## 6. OTA

管理員 → OTA 更新 → 檢查新版 → 輸入目前管理員密碼 → 勾選已通知盤點人員 → 套用新版。

更新頁會顯示進度，網站重啟時自動重試連線。更新前備份在 `/volume3/islevet/data/backups/`；停止的舊容器以 `islevetah-app-rollback-...` 保留。備份與舊映像沒有自動清理政策，確認新版及備份可用後才人工清理。

更新 OTA 引擎、Compose 或部署設定時，重做第 3 步。已有管理員不必重做第 4 步。

## 7. 檢查與排錯

```sh
docker ps --filter name=islevetah
curl -fsS http://127.0.0.1:7788/api/health
docker logs --tail 60 islevetah-app
docker logs --tail 60 islevetah-ota
```

兩個容器應 healthy；health API 回應 `{"ok": true}`。

- SSH 連不上：檢查 DSM SSH、22 埠與同區網連線。
- 7788 被占用：先查 `docker ps` 或 `netstat -lnt`，不要停止不明服務。
- `Permission denied /app/server.py`：舊映像未正規化 NAS 原始碼權限。下載最新 Dockerfile 並只重建 app；新版映像會將程式目錄設定 755、程式檔 644，仍用 UID 10001 執行。
- Docker 不存在：安裝／啟動 Container Manager；檢查 `docker compose version` 或 `docker-compose version`。
- 網頁連不上但 health 正常：檢查 DSM 防火牆允許院內網路連入 TCP 7788；不需設定路由器對外轉發。
- 帳號不能登入：檢查已核准、啟用與密碼。暫時密碼會要求先修改。登入失敗達限制須等待 15 分鐘。
- OTA 無法下載：確認 NAS 可連線 GitHub 與 Docker Hub；來源儲存庫須保持公開。
- 不分享 `.env`、完整 `docker inspect` 或 `docker compose config` 輸出，這些可能包含內部 token。

## 8. 備份與回復

要做完整離線備份，先停止本專案兩個容器，複製 `data`、`ota-state` 與 `.env`，完成後再啟動：

```sh
cd /volume3/islevet
docker stop islevetah-app islevetah-ota
# 透過 File Station 備份 data、ota-state 與 .env 到受限資料夾。
docker start islevetah-ota islevetah-app
```

OTA 自動回復失敗時，先查看容器清單與更新狀態，再依實際舊容器名稱操作。不要直接刪除資料或重建空資料庫。由新版啟動後產生的紀錄，在還原更新前備份時會失去；正常 OTA 的健康檢查失敗會立即回復。

在 SSH 查閱安全的更新結果（不含密碼）：

```sh
cat /volume3/islevet/ota-state/job.json
```

第一次部署尚未在你的 NAS 實機執行，完成以上步驟後以實際 health 與登入結果驗收。
