# 發掘新桌布（夢境製作所）

目標：找你近期玩、看過但**還沒有桌布**的作品的新圖，經你決定後放進收藏。

```
infovore get_wallpaper_gaps ─► scripts/discover.py ─► catalog/inbox/<年>-W<週>.json
        (近期缺口)             Wallhaven 搜尋 + CLIP 去重          │
                                                                  ▼
 收藏 ◄── scripts/accept_candidates.py ◄── 決定檔 ◄── https://afterimage.skyhong.tw/discover
```

## 1. 找候選

```bash
uv run --group enrich scripts/discover.py                       # 目標來自 infovore 的缺口
uv run --group enrich scripts/discover.py --work "Celeste" --work "Hades II"
uv run scripts/discover.py --no-clip --dry-run                  # 不需要 torch，只印結果、不寫檔
```

- 來源：Wallhaven 公開 API（只收 SFW、不需要金鑰，每分鐘 45 次，腳本自己限速）。
- 桌面：解析度 ≥ 1920×1080，長寬比 16:10 到 21:9（排除 4:1 之類的橫幅）。`--device phone` 找直式。
- 已經接受或拒絕過、或已經在收件匣裡的不會再出現（`catalog/discover-state.yaml`）。
- **去重**：用 CLIP 和 `catalog/embeddings.npz` 比對。門檻 0.87，依據是實測：改過的複本（縮小、重新壓縮、裁邊、調亮）
  相似度 0.888–0.99，同作品但不同的圖最高 0.85。介於 0.80 和門檻之間的會保留，並在 `/discover` 顯示最像的收藏圖讓你判斷。
- 自動目標會略過太泛的單字名稱（例如電影「Escape」會搜到不相干的圖）。想找的話用 `--work`。
  4 個字母以內的單字遊戲名稱也會被略過（保守作法），同樣用 `--work`。
- 找不到的很常見：冷門遊戲和現場演出在 Wallhaven 上幾乎沒有圖。

候選只寫進 `catalog/inbox/`，**不會**下載圖片，也不會動收藏。

## 2. 決定

打開 `/discover`（先 `uv run scripts/build_site_data.py` 並部署，頁面才看得到新的候選）。

| 按鍵 | 動作 |
|---|---|
| `A` | 接受 |
| `R` | 拒絕（之後不會再出現） |
| `S` / `→` | 跳過 |
| `←` | 上一個 |

決定只存在這個瀏覽器，按「匯出決定」會下載並複製 JSON。

## 3. 套用

```bash
uv run --group enrich scripts/accept_candidates.py afterimage-decisions-*.json --dry-run
uv run --group enrich scripts/accept_candidates.py afterimage-decisions-*.json
```

接受的圖會下載到 `wallpaper/`（直式則 `phone wallpaper/`），檔名 `wallhaven-<id>`，catalog 記下 `source` 連結和作品
（作品不存在時自動建立）。接著自動重算色票、相似圖，並產生縮圖與預覽。新圖是 `reviewed: false`，沒有情緒，
可以再用 `/?review=1` 補上。單張超過 30 MB 的會被跳過並回報。

## 4. 交給 Hermes

見 [HERMES.md](HERMES.md)。
