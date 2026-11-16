# 讓 Hermes 定期發掘（提案，尚未安裝）

**狀態：尚未安裝任何東西。** Hermes 是你私人、正在運作的 agent，它的每個排程都是你明確要求才加的
（`AGENTS.md` 還特別註明 `hourly-personal-discovery` 是你刪掉的，不可重建）。所以這裡只是提案，
要不要裝由你決定。

## 分工

- **確定性的部分**留在這個 repo：`scripts/discover.py` 挑目標、搜尋、去重、寫收件匣。可手動執行，可測試。
- **Hermes** 只做兩件事：每週呼叫一次 discover，然後在 Discord 通知你「收件匣有 N 張新候選」並附上連結。
  遵照 Hermes 既有的模式：「gate 腳本（確定性）→ 模型（最小工具集）→ Discord」。
  沒有新候選時 gate 直接結束，不會喚醒模型。
- **決定權在你**：Hermes 不接受、不拒絕、不下載、不改收藏。

## 提議的 gate 腳本行為

1. `cd` 到 afterimage 的 checkout，`git pull --ff-only`。
2. 執行 `uv run --group enrich scripts/discover.py`。
3. 若 `catalog/inbox/` 有新增的 `status: new` 候選，輸出「N 張新候選，來自 X、Y 作品」；否則什麼都不輸出。
4. 提交收件匣並部署（讓 `/discover` 看得到）；這一步要不要自動化也由你決定。

## 還沒解決、需要你決定的

1. **在哪裡執行。** CLIP 需要 torch（約 930 MB 的 venv）和 afterimage 的 checkout。Mint 磁碟已 96%（剩約 9 GB）。
   可以只用 `--no-clip` 省掉 torch，代價是沒有視覺去重，重複的圖會進到收件匣由你判斷。
2. **Hermes 的帳號權限。** Hermes 在隔離的 `hermes` 帳號下，afterimage 的 checkout 在 `sky` 帳號。
   要讓它存取，得用 `AGENTS.md` 提到的 `fleet-mint_t3` 路徑，或另外 clone 一份到 `/home/hermes`。
3. **頻率與頻道。** 建議每週一次、發到 `#for-you`。
4. **是否自動 commit / 部署收件匣。** 建議先不要：先讓 Hermes 只通知，你確認流程順了再說。

## 不會做的事

- 不改 Hermes 的設定、persona、記憶或既有排程。
- 不把任何金鑰放進這個 repo（Wallhaven 的公開搜尋不需要金鑰）。
