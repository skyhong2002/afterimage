# afterimage 殘影 — 視覺品味資料庫 規劃

> 狀態：草案（2026-11-17）。repo：`skyhong2002/afterimage`（原 `image-collection`，2026-11-17 改名；T3 project：afterimage）

## 目標

1. **分類**：每張 wallpaper 知道「它屬於什麼」：作品／系列、角色、畫師、來源連結、色調、氛圍。
2. **推薦／發掘**：結合 infovore.skyhong.tw 記錄的生活資料（Kitsu 動畫、Backloggd 遊戲、Simkl 影劇、stats.fm 音樂……），推薦新的 wallpaper 和新的視覺風格。

## 現況

- 284 張：`wallpaper/` 248 張（309 MB）、`phone wallpaper/` 36 張（22 MB），jpg 和 png 混用，最後更新在 2022 年。
- 檔名是**個人註解**（`El Psy Congroo.jpg`、`the cake is a lie.png`、`but it refused.jpg`），本身就有意義，不能當作資料直接覆蓋掉。
- 內容大多是遊戲、動畫、電影和影集（Persona、FF7、Steins;Gate、Undertale、Sherlock、Nolan 電影……），剛好和 infovore 的 Backloggd、Kitsu、Simkl 資料重疊。這就是可以串起來的地方。

## 世界觀：Inside Out 名詞

對外叫 afterimage（殘影：移開視線後仍留在眼裡的影像）；系統內部的名詞借用《腦筋急轉彎》。

| 電影 | 系統 |
|---|---|
| 記憶球 Memory Orb | 一張桌布（catalog 裡的一筆） |
| 球的顏色＝情緒 | `emotion` 欄位：joy / sadness / anger / fear / disgust / anxiety / envy / ennui / embarrassment |
| 核心記憶 Core Memories | 最愛、長期使用的桌布（`core: true`） |
| 個性島 Islands of Personality | 作品或系列（`works.yaml`） |
| 長期記憶區 Long Term Memory | gallery 網站 |
| 思緒列車 Train of Thought | CLIP 相似圖串 |
| 總部 Headquarters | 讀 infovore 的總控台 |
| 夢境製作所 Dream Productions | discover 推薦 |
| 回想管 Recall Tube | 每週的 inbox |
| 記憶垃圾場 The Memory Dump | 拒絕的候選（負回饋） |
| 遺忘工人 Forgetters | 去重、清掉低解析度的圖 |
| 自我意識 Sense of Self | 從 infovore 匯總的品味檔案 |

情緒判斷以 CLIP 或 LLM 讀圖片內容為主，配色當輔助，檔名註解（例如 `but it refused`、`I loved you`）也是很強的訊號。可以拿來做情緒平衡推薦，以及 Wrapped 裡「今年的記憶球顏色分布」。

## 架構原則

```
afterimage (這個 repo)                           infovore
┌──────────────────────────────┐   catalog.json  ┌──────────────────────┐
│ images/  (原圖，不改名)       │ ─────────────▶ │ sources/wallpaper.ts │
│ catalog/ (YAML，人可編輯)     │                │  → MCP: search_wallpapers
│ scripts/ (enrich / discover) │ ◀───────────── │ /api/activities.json │
│ site/    (靜態 gallery)      │  品味訊號        │ /api/wrapped, MCP    │
└──────────────────────────────┘                └──────────────────────┘
          │ discover (GitHub Action 排程)
          ▼
   Wallhaven / Danbooru / Pixiv 候選 → inbox → 接受 = PR 加圖，拒絕 = 負回饋
```

- **資料放在 git**：catalog 存成 YAML，人改得到、diff 看得到。`catalog.json` 是由 YAML 產生的檔案。
- **repo 負責 catalog 和 discovery**。infovore 照它原本的 `sources → data → output` 分層，只把 wallpaper 加成一個新的 source，並透過 MCP 開放查詢。推薦邏輯不塞進 infovore，以免把 lifelog 弄髒。
- 自動標註只是**建議**，最後由人確認（`reviewed: true`）。

## Schema（草案）

`catalog/works.yaml`：作品實體，對應到 infovore 已有的外部 ID

```yaml
- id: persona-5
  title: Persona 5
  kind: game            # 和 infovore MediaKind 對齊：game/anime/movie/show/book/music
  ids: { backloggd: persona-5, igdb: 1234, kitsu: null, tmdb: null }
```

`catalog/wallpapers.yaml`：一張圖一筆

```yaml
- id: 3f9a…            # 內容 sha256 前 12 碼（穩定，改檔名也不會變）
  path: wallpaper/take your heart.png
  caption: take your heart   # 原檔名，保留個人註解
  device: desktop | phone
  size: [3840, 2160]
  works: [persona-5]
  characters: [Joker]
  artist: { name: …, url: … }
  source: https://…           # SauceNAO / 手動
  tags: [...]                 # WD tagger
  palette: ["#c8102e", "#111", ...]
  emotion: { primary: anger, secondary: [joy] }   # 記憶球顏色
  mood: [rebellious, neon]
  core: false                 # 核心記憶
  similar: [id, id, id]       # CLIP 近鄰，離線算好
  reviewed: false
```

## 分階段實作

### Phase 0：整理（半天）
- 刪除 `.DS_Store`，整理 `.gitignore`，搬成 `images/desktop`、`images/phone`（或維持原路徑）。
- 決定要不要改用 Git LFS。目前 330 MB 不用 LFS 也可以；預期大量加圖的話再改。
- `scripts/` 用 Python + `uv`。

### Phase 1：自動 enrichment（1–2 天）
| 步驟 | 工具 | 產出 |
|---|---|---|
| 基本資料 | Pillow | size、device、sha id |
| 色彩 | k-means（Pillow + sklearn）或 node-vibrant | palette，並對齊到 Wallhaven 的固定色票 |
| 找出處 | SauceNAO API（透過 PicImageSearch） | artist、source、Danbooru/Pixiv ID；結果快取，只跑一次 |
| 動畫風標籤 | WD EVA02-Large Tagger v3（ONNX，本機） | 角色、作品（copyright）、畫風 tags |
| 語意向量 | open_clip（或 rclip） | `similar` top-k；embedding 存在 `catalog/embeddings.npy` |
| 歸類到作品 | LLM（caption + tags + SauceNAO 結果）→ 建議 `works` / `mood` | 人工 review |

### Phase 2：Gallery（1 天）
- Astro 靜態網站，從 `catalog.json` 產生頁面：依作品、kind、色彩、mood 瀏覽，每張圖都有「相似的圖」。
- 部署到 GitHub Pages，或 `afterimage.skyhong.tw`（Dokploy，公開）。
- 選配：瀏覽器端用 transformers.js 做文字查詢的 CLIP 搜尋（例如輸入「雨夜霓虹」）。

### Phase 3：接上 infovore（1–2 天）
- **wallpaper → infovore**：新增 `src/sources/wallpaper.ts`，從 GitHub raw 拉 `catalog.json`。新增 MCP tool `search_wallpapers` 和 `get_wallpapers_for_work`，讓 AI 回答得出「我有哪些 P5 的桌布？」。
- **infovore → 品味訊號**：discover 腳本讀 `/api/activities.json` 和各 source snapshot，算出：
  - 正在玩、看、聽的作品（近 30 天）
  - 評分高的作品
  - 已經有 wallpaper 的作品，代表長期品味

### Phase 4：Discover 推薦（2–3 天）
GitHub Action 每週跑一次，產生候選清單：
1. **熱度缺口**：近期在玩或在看、評分高，但還沒有 wallpaper 的作品 → 用作品 tag 搜 Wallhaven 或 Danbooru。
2. **相似延伸**：已有的圖 → 找到 Wallhaven ID 後用 `q=like:<id>` 搜；或用 CLIP 向量比對候選的縮圖。
3. **新視覺**：從自己的 palette、mood 分布找出相近但沒看過的畫師或風格（Danbooru 畫師作品、Pixiv related，用 pixivpy），刻意保留一定比例的「探索」。
4. 候選先和 catalog 去重（CLIP 相似度門檻），產出 `inbox/YYYY-WW.json`，網站上做一頁 /discover，也輸出成 RSS。
5. 接受 → Action 自動開 PR，把圖下載進來並附上 metadata；拒絕 → 記錄到 `catalog/rejected.yaml`，作為負回饋。

## 先決定的事

- [x] repo 改名為 `afterimage`（GitHub 保留 `image-collection` 的 redirect）
- [ ] gallery 要公開嗎？（圖片多為版權作品，目前 repo 本身已是 public）
- [ ] discover 的 inbox 放在這個 repo 的網站，還是做成 infovore 的一個頁面？
- [ ] SauceNAO API key（免費帳號就夠用）

## 參考實作

- 最接近的模式：[icco/wallpapers](https://github.com/icco/wallpapers)（Gemini 標籤 + k-means 色彩 → JSON）、[fahim-foysal-097/wallpapers](https://github.com/fahim-foysal-097/wallpapers)（腳本產生 JSON 和靜態網站）、[Awesome_Wallpapers](https://github.com/AlexandrosLiaskos/Awesome_Wallpapers)
- CLIP 搜尋：[rclip](https://github.com/yurijmikhalevich/rclip)、[wallclip](https://github.com/Syagros/wallclip)（pgvector，比較重）
- 動畫標註：[WD EVA02 tagger](https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3)、[PicImageSearch](https://github.com/kitUIN/PicImageSearch)（SauceNAO/IQDB/Ascii2D/trace.moe）
- 推薦來源：[Wallhaven API](https://wallhaven.cc/help/api)（`like:`、tag、colors）、Danbooru API、[pixivpy](https://github.com/upbit/pixivpy)
- 比較重的現成方案（只參考概念）：Immich smart search、Hydrus Network
