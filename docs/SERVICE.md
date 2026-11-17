# afterimage 服務

私人桌布收藏的網站服務：公開的畫廊，加上只有你能用的審核與發掘。
FastAPI + SQLite（`service/afterimage`），前端是 Astro 建出來的靜態頁（`site/`），由同一個服務提供。

## 怎麼用

- **看畫廊**：打開 <https://afterimage.skyhong.tw>，不需要登入。個人照片（`personal`）訪客看不到。
- **登入**：<https://afterimage.skyhong.tw/login>。密碼在這台機器的 `~/.config/afterimage/admin-password`。
  登入後右上角會出現「發掘」「審核」「登出」。
- **審核**：`/?review=1`。每次修改立刻存進伺服器，不用匯出。
  `1`–`9` 設定情緒、`W` 輸入作品、`X` 清除作品、`M` 個人照片、`Enter` 確認並跳下一張、`N` 跳過、`←` `→` 前後移動。
- **發掘**：`/discover`。輸入作品名稱，同時查多個來源，結果會標示收藏裡已經有的，按「收進來」就下載、產生縮圖、寫入資料庫。
  「近期還沒有桌布」那排是從 Infovore 即時抓的缺口。「收件匣」是批次發掘（依缺口自動找）的候選。

## 資料在哪

`/data`（伺服器上是 `/home/ubuntu/apps/afterimage-svc/data`）：

| 路徑 | 內容 |
|---|---|
| `afterimage.sqlite` | 圖片、作品、審核紀錄（`edits`）、候選 |
| `originals/` | 原圖，檔名是內容雜湊 |
| `thumbs/` `view/` | AVIF 縮圖（720px）與預覽（2560px） |
| `backups/` | 資料庫每天備份一份（留 14 天）和最後一次異地備份的狀態 |

git 只放程式。`catalog/` 是搬進資料庫之前的 YAML 快照，只作為封存，`service/afterimage/migrate.py` 是用它匯入的。
異地備份見下一節。

## 外部來源

| 來源 | 內容 | 需要 |
|---|---|---|
| Steam | 官方截圖、商店背景（遊戲） | 無 |
| Wallhaven | 社群桌布，只收 SFW | 無 |
| Danbooru | 插畫，只收 general 分級，記錄作者 | 無 |
| AniList | 動畫封面 | 無 |
| TMDB | 電影、影集劇照，**只能預覽和開連結，不能存入收藏** | `TMDB_TOKEN`（免費申請的 API Read Access Token，v4 那串長的） |

**TMDB 的規則**（來自它的 API 使用條款，2023-10-20 版）：只限非商業用途；快取上限 6 個月，終止時要全部刪除；
禁止用在 AI/機器學習相關的應用；網站要顯示 TMDB 標誌和「This website uses TMDB and the TMDB APIs but is not endorsed,
certified, or otherwise approved by TMDB.」。所以：TMDB 的結果 `importable=false`，伺服器端拒絕匯入（不是只有前端隱藏按鈕），
批次發掘也不會把它放進收件匣，首頁和 `/discover` 頁尾有標誌與聲明。**不要把 TMDB 的資料交給 Hermes 或任何模型。**

Pixiv 沒有公開 API，沒有接。每個來源失敗時只會在那一區顯示錯誤，不影響其他來源。
下載只允許已知的圖片主機（見 `sources/base.py`），重新導向也會逐跳檢查，大小上限 30 MB。

## 去重

用 64 位元的差異雜湊（dHash）比對。距離 ≤ 10 判定為「已收藏」並拒絕匯入（可強制），≤ 12 顯示「疑似」。
門檻來自實測：縮小、重新壓縮、調亮、裁邊 4% 的複本距離 ≤ 12；不同的圖最近 5（近乎全黑的圖），第 1 百分位 18。
**抓不到**：裁掉 10% 以上的複本，以及同一幅畫但尺寸與裁切都不同的圖。近乎全黑的圖不算雜湊，只比對完全相同的檔案。

## 異地備份

每天 04:30（台北時間）伺服器把資料推到**這個 repo 的 `backup` 分支**（公開）：

- `snapshot.json`：作品、圖片、作品順序、相似圖、候選、編輯紀錄，可讀的 JSON，沒有時間戳，所以沒變更就不會產生 commit。
- `originals/`：所有**沒有標為個人照片**的原圖。已經在 `master` 裡的 282 張不會重複上傳（git 依內容去重）。
- **個人照片不推**：快照裡只剩 `{id, personal}`，檔案只留在伺服器。之後才把某張標成個人，它會從分支最新內容移除，但仍留在 git 歷史裡。
- 縮圖和預覽不備份，還原時重新產生。
- 這是孤兒分支，和程式碼完全分開，不會動到 `master`。

**怎麼運作**：伺服器 crontab 跑 `python -m afterimage.backup run`（只用標準庫），用只能存取這個 repo 的 deploy key
`~/.ssh/afterimage_backup`（GitHub 上叫 "afterimage-svc backup (skyhong-blog)"，讀寫）。工作目錄是
`/home/ubuntu/apps/afterimage-svc/backup-repo`，紀錄在 `data/backups/backup.log`。網站登入後 `GET /api/admin/backup`
可以看最後一次的結果。

**大小保護**：repo 超過 1000 MB 會警告，超過 2000 MB 拒絕推送並記錄錯誤（GitHub 建議低於 1 GB，目前約 682 MB）。
快到上限時，新圖要改放別處（例如物件儲存）。

**還原**（2026-11-17 實際演練過：271 張，約 3 分鐘，8 核心）：

```bash
git clone --branch backup --single-branch https://github.com/skyhong2002/afterimage.git /tmp/restore-src
uv sync --group service
uv run --directory service python -m afterimage.backup restore --repo /tmp/restore-src --data /path/to/EMPTY/data
```

還原會建立資料庫、複製原圖、平行重建縮圖與預覽（要求目標目錄是空的）。演練時逐項比對：6 張資料表和線上完全相同、
271 張原圖逐位元相同、還原出來的服務回給 Infovore 的 `/api/wallpapers.json` 與線上一致。
個人照片不在還原範圍：原本那 11 張仍在 `master` 的 `wallpaper/` 與封存的 `catalog/`（用 `migrate` 匯入），之後新增的個人照片只存在伺服器。
然後照「部署」一節放好 `.env` 與 compose，網址會繼續一樣。

**限制**：備份是每天一次，兩次之間新增的圖可能遺失；`backup` 分支是公開的；deploy key 對這個 repo 有寫入權限
（伺服器被入侵時等同可寫入 repo，所以 `master` 建議設分支保護）。

## 設定（環境變數）

| 變數 | 說明 |
|---|---|
| `AFTERIMAGE_DATA` | 資料目錄，預設 `./data` |
| `AFTERIMAGE_FRONTEND` | 前端建置結果，不設就只提供 API |
| `AFTERIMAGE_PUBLIC_URL` | 公開網址，`https://` 開頭時 cookie 會加 `Secure` |
| `AFTERIMAGE_ADMIN_PASSWORD_HASH` | `python -m afterimage.auth` 產生的雜湊（用 `:` 分隔，不會被 shell 展開） |
| `AFTERIMAGE_SESSION_SECRET` | 簽 session cookie 用的隨機字串 |
| `AFTERIMAGE_API_TOKEN` | 給腳本和 Hermes 的 Bearer token，空白就停用 |
| `INFOVORE_MCP_URL` | 取得缺口的 Infovore MCP 位址 |
| `TMDB_TOKEN` | 啟用 TMDB 來源 |

換密碼：`cd service && uv run python -m afterimage.auth`，把輸出的雜湊放進伺服器的 `.env` 後重啟容器。

## API（需要登入或 Bearer token 的標 🔒）

公開：`GET /api/catalog`、`GET /api/wallpapers.json`（Infovore 讀的格式，不含個人照片）、`/thumbs/<id>.avif`、`/view/<id>.avif`、`/original/<id>`、`/healthz`。

🔒 `PATCH /api/images/<id>`、`POST /api/works`、`POST /api/works/<id>/resolve-links`、
`GET /api/discover/search`、`POST /api/discover/import`、`GET /api/discover/gaps`、
`POST /api/discover/run`、`GET /api/discover/inbox`、`POST /api/discover/inbox/<id>/accept|reject`。

## 開發與測試

```bash
uv sync --group service --group dev
uv run pytest service/tests                       # 不連網路，外部服務都是假的
cd site && npm ci && npm run build                # 前端
AFTERIMAGE_DATA=/tmp/data AFTERIMAGE_FRONTEND=site/dist uv run --directory service uvicorn afterimage.asgi:app
uv run --directory service python -m afterimage.migrate --repo .. --data /tmp/data   # 從封存的 YAML 匯入
```

## 部署（skyhong-blog）

`/home/ubuntu/apps/afterimage-svc/`：`docker-compose.yml`（Traefik 標籤，跟 freshrss 同一個模式）、`.env`（金鑰，權限 600）、
`data/`（volume）、`src/`（repo 的 sparse clone）。更新：

```bash
cd /home/ubuntu/apps/afterimage-svc/src && git fetch --depth 1 origin master && git checkout -B master FETCH_HEAD
cd .. && docker compose up -d --build
```

## 尚未做

- 新匯入的圖沒有「相似畫面」（CLIP 向量要 torch，不放在伺服器上），聯想會用「同作品」「同情緒」「相近色調」。
- 備份超過 1 GB 之後的去處（見上方大小保護）。
- 由 Hermes 定期呼叫 `POST /api/discover/run`，見 [HERMES.md](HERMES.md)。
