# 審核 AI 建議的作品與情緒

`catalog/wallpapers.yaml` 裡的 `works`、`emotion` 一開始是 AI 的建議（`reviewed: false`，附 `confidence` h/m/l）。
審核後會變成 `reviewed: true`，並移除 `confidence` 和 `note`。

## 1. 在網站上審核

打開 <https://afterimage.skyhong.tw/?review=1>（本機：`cd site && npm run dev` 後開 `/?review=1`）。

- 只有加了 `?review=1` 才會出現審核介面，一般訪客看不到。離開用 `?review=0`。
- 你的修改**只存在這個瀏覽器**（localStorage），可以分好幾次做。
- 頂端的篩選可以只看「未審核」「未審核・把握度低」「未審核・把握度中以下」。建議先處理低把握度的。

| 按鍵 | 動作 |
|---|---|
| `1`–`9` | 設定情緒（喜悅、憂傷、憤怒、恐懼、厭惡、焦慮、羨慕、無聊、尷尬） |
| `W` | 輸入作品名稱，Enter 加入。名稱不在登錄表時會問你類型並建立新作品 |
| `X` | 清除作品（標為未歸類） |
| `M` | 切換「個人照片」 |
| `Enter` | 確認這張，跳到下一張未審核的 |
| `N` | 不確認，直接跳到下一張未審核的 |
| `←` `→` | 上一張 / 下一張（依目前篩選） |

## 2. 匯出

按頂端的「匯出審核」：會下載 `afterimage-review-*.json`，並複製到剪貼簿。

## 3. 套用到 catalog

```bash
uv run scripts/apply_review.py path/to/afterimage-review-*.json --dry-run   # 先看摘要
uv run scripts/apply_review.py path/to/afterimage-review-*.json
uv run scripts/build_site_data.py
```

腳本會驗證情緒、作品 id 和圖片 id（有錯就整批不寫入），把新作品加進 `catalog/works.yaml`，
並把這個檔案備份到 `catalog/reviews/`。之後再 commit、部署。

已經 `reviewed: true` 的項目不會被 `scripts/apply_labels.py` 覆蓋。
