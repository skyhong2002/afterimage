# afterimage 服務

私人桌布收藏的網站服務：公開的畫廊，加上只有你能用的審核與發掘。
FastAPI + SQLite（`service/afterimage`），前端是 Astro 建出來的靜態頁（`site/`），由同一個服務提供。

## 怎麼用

- **看畫廊**：打開 <https://afterimage.skyhong.tw>，不需要登入。個人照片（`personal`）訪客看不到。
- **登入**：<https://afterimage.skyhong.tw/login>。密碼在這台機器的 `~/.config/afterimage/admin-password`。
  登入後右上角會出現「發掘」「審核」「登出」。
- **審核**：`/?review=1`。每次修改立刻存進伺服器，不用匯出。
  `1`–`9` 設定情緒、`W` 輸入作品、`X` 清除作品、`M` 個人照片、`Enter` 確認並跳下一張、`N` 跳過、`←` `→` 前後移動。
- **發掘**：`/discover` 一打開就是「推薦」：從你**所有看過的作品**（不是只有近期）找來的圖，跨作品混合、依你多可能想要排序，
  往下捲會一直載入。每張可以 `✓ 加入` 或 `✗`；點圖開大圖，`A` 加入、`R` 不要、`← →` 切換。
  可以按作品類型、來源、裝置篩選，或只看「還沒有桌布的作品」，點作品名稱只看那一部。
  頁面上方是掃描進度。「搜尋」分頁是手動查一部作品（含 TMDB 劇照預覽）。

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

## 發掘的運作

1. **作品清單**：從 Infovore 的 `GET /api/activities.json` 讀出全部歷史（遊戲、電影、影集、動畫、漫畫、書，音樂不算），
   每部作品一筆。只有「排隊中」（`planned`、`plantowatch`、`to-read`）而沒看過的不算。2026-11-17 是 635 部。
   動畫和漫畫各只有 20 部，因為 Infovore 的 Kitsu 來源本身只抓每種狀態最近 10 筆。
2. **背景掃描**（`feed.py` 的 `Scanner`，隨服務啟動，`AFTERIMAGE_SCANNER=0` 可關）：依優先度（評分、最近玩過、玩幾次）
   由高到低，每部作品查 Steam（遊戲）、Wallhaven、Danbooru（遊戲和動畫），標題找不到再試去掉副標題和版本字樣的寫法
   （`Persona 5 Royal` → `Persona 5`），把可匯入的圖存成候選。每部約 4–12 秒，全部約 1–2 小時；可以中途重啟，
   從資料庫接續。每 21 天重掃，每 12 小時重讀歷史。太泛的單字標題（例如電影 `Escape`）會略過。
   一部作品連續失敗 3 次就放棄（寫進 `targets.note`），不會卡住後面的。
3. **推薦排序**：作品的想要程度（評分、最近、玩幾次，已經有越多張桌布越低）加上圖的人氣（在自己來源裡的排名）和解析度，
   同一部作品每多出現一張就扣分，所以不會一整排都是同一部。候選圖如果後來進了收藏（相似度 ≤ 12）會自動消失。
4. **圖片代理**：Danbooru 的圖片主機拒絕別的網站直接引用，所以那些圖走 `GET /api/discover/img`（只有你登入能用、
   只允許白名單主機、只回傳真的圖片，不會把挑戰頁當圖傳）。伺服器自己下載時用誠實的 User-Agent 就可以；
   **假裝成瀏覽器的請求反而會被 Cloudflare 擋下**。

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
- `originals/`：所有**沒有標為個人照片**的原圖。已經在 `main` 裡的 282 張不會重複上傳（git 依內容去重）。
- **個人照片不推**：快照裡只剩 `{id, personal}`，檔案只留在伺服器。之後才把某張標成個人，它會從分支最新內容移除，但仍留在 git 歷史裡。
- 縮圖和預覽不備份，還原時重新產生。
- 這是孤兒分支，和程式碼完全分開，不會動到 `main`。

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
個人照片不在還原範圍：原本那 11 張仍在 `main` 的 `wallpaper/` 與封存的 `catalog/`（用 `migrate` 匯入），之後新增的個人照片只存在伺服器。
然後照「部署」一節放好 `.env` 與 compose，網址會繼續一樣。

**限制**：備份是每天一次，兩次之間新增的圖可能遺失；`backup` 分支是公開的；deploy key 對這個 repo 有寫入權限
（伺服器被入侵時等同可寫入 repo，所以 `main` 建議設分支保護）。

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
`GET /api/discover/feed`（`limit` `offset` `kind` `source` `device` `work` `uncovered`）、`GET /api/discover/progress`、
`POST /api/discover/run`（`?refresh=true` 叫掃描器立刻醒來並重讀歷史）、`GET /api/discover/img?u=`、
`POST /api/discover/feed/<id>/accept|reject`、`GET /api/admin/backup`。

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
cd /home/ubuntu/apps/afterimage-svc/src && git fetch --depth 1 origin main && git checkout -B main FETCH_HEAD
cd .. && docker compose up -d --build
```

## 尚未做

- 新匯入的圖沒有「相似畫面」（CLIP 向量要 torch，不放在伺服器上），聯想會用「同作品」「同情緒」「相近色調」。
- 備份超過 1 GB 之後的去處（見上方大小保護）。
- 由 Hermes 定期呼叫 `POST /api/discover/run`，見 [HERMES.md](HERMES.md)。
