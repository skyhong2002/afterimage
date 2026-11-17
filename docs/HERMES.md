# 讓 Hermes 定期發掘（提案，尚未安裝）

**狀態：沒有安裝任何東西。** Hermes 是你私人、運作中的 agent，它的每個排程都是你明確要求才加的
（`AGENTS.md` 還特別註明 `hourly-personal-discovery` 是你刪掉的，不可重建）。所以這裡只是提案，要不要裝由你決定。

## 現在的樣子

服務自己就會持續掃描（[SERVICE.md](SERVICE.md)「發掘的運作」），候選一直累積在 `/discover`。所以 Hermes **不需要**自己搜尋或下載，
也不一定需要排程：你打開 `/discover` 就有東西。Hermes 能多做的只有「提醒」：

```bash
curl -s -H "Authorization: Bearer $AFTERIMAGE_API_TOKEN" https://afterimage.skyhong.tw/api/discover/progress
# {"candidates": 1204, "works_scanned": 635, ...}
```

## 提議的分工

- **服務**：掃描、去重、排序、存候選。
- **Hermes（可選）**：每週讀一次 `progress`，如果 `candidates` 比上次多很多，在 `#for-you` 發一則：
  「推薦頁有 N 張新的圖」附連結。沒有新增就什麼都不做（gate 腳本決定，不喚醒模型）。
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
