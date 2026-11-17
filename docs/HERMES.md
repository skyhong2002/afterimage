# 讓 Hermes 定期發掘（提案，尚未安裝）

**狀態：沒有安裝任何東西。** Hermes 是你私人、運作中的 agent，它的每個排程都是你明確要求才加的
（`AGENTS.md` 還特別註明 `hourly-personal-discovery` 是你刪掉的，不可重建）。所以這裡只是提案，要不要裝由你決定。

## 現在的樣子

發掘的核心已經在服務裡（[SERVICE.md](SERVICE.md)）。Hermes 不需要自己搜尋或下載，只要定期呼叫一個 API：

```bash
curl -s -X POST -H "Authorization: Bearer $AFTERIMAGE_API_TOKEN" \
  "https://afterimage.skyhong.tw/api/discover/run?max_works=6&per_work=6"
```

回傳每個目標作品新增了幾張候選。候選會出現在 `/discover` 的「收件匣」，由你決定。

## 提議的分工

- **服務**：依 Infovore 的缺口挑目標、搜尋多個來源、用雜湊去重、把候選存進資料庫。
- **Hermes**：每週呼叫一次上面的 API；只有「新增 > 0」才喚醒模型，發一則 Discord 訊息到 `#for-you`：
  「收件匣有 N 張新候選，來自 X、Y」附連結。沒有新候選時什麼都不做。
  遵照 Hermes 既有的模式：gate 腳本（確定性）→ 模型（最小工具集）→ Discord。
- **你**：決定接受或拒絕。Hermes 不接受、不拒絕、不匯入、不改收藏。

## 需要你決定的

1. **要不要裝。** 手動打開 `/discover` 就能用；排程只是少一個你要記得做的動作。
2. **Token 怎麼交給 Hermes。** token 在這台機器的 `~/.config/afterimage/api-token`。Hermes 在隔離的 `hermes` 帳號下，
   不能直接讀 `sky` 的檔案，要由你放進 Hermes 的設定，我不會代為搬動。這個 token 能做服務裡所有「需要登入」的事，
   範圍比搜尋大，所以建議之後視需要再縮成只能呼叫 `/api/discover/run` 的專用 token。
3. **頻率與頻道。** 建議每週一次、`#for-you`。

## 不會做的事

- 不改 Hermes 的設定、persona、記憶或既有排程。
- 不把任何金鑰放進這個 repo。
