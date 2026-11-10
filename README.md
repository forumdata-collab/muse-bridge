# Muse ↔ Hermes 檔案信箱

一個 **GitHub repo 做信箱**，兩邊都寫／讀同一個 repo。冇新帳號、冇 Drive 授權、有版本紀錄。

- Repo：`https://github.com/forumdata-collab/muse-bridge`
- Muse → Hermes：`inbox/<id>.json`
- Hermes → Muse：`status/<id>.json`
- Hermes 每 **2 分鐘** poll 一次（cron），有結果會 Telegram 通知用戶

## 1. Muse 寫入請求

Push 一個檔案去 `inbox/<id>.json`（`id` 只准 `A-Za-z0-9._-`，≤64 字）。

**連線測試**
```json
{ "id": "ping-001", "type": "ping" }
```
→ Hermes 回 `status/ping-001.json`：`{"state":"pong"}`

**交付部署（只做 diff，唔會自動部署）**
```json
{
  "id": "v97-20261110",
  "type": "deploy",
  "project": "manmotemple",
  "zip_url": "https://<公開可下載的 zip，index.html 在 zip 根或一層內>",
  "note": "v97：修了鹿角間隙"
}
```

## 2. Hermes 回覆

`status/<id>.json`：
```json
{
  "id": "v97-20261110",
  "state": "diffed",
  "at": "2026-11-10T09:12:00Z",
  "project": "manmotemple",
  "zip_files": 142,
  "files": {
    "added": [], "changed": ["js/deer.js"], "removed": [],
    "removed_real": [], "removed_junk": []
  },
  "sw_cache": {"current": "manmo-v96", "incoming": "manmo-v97"},
  "warnings": [],
  "deployment_url": null,
  "message": "diff 完成，等你喺 Telegram 批准先部署"
}
```

`state` 可能值：`pong` / `diffed` / `rejected` / `failed` / `deployed`。

部署批准後 Hermes 會覆寫同一個 `status/<id>.json`，`state` 變 `deployed` 並填 `deployment_url`、`http_code`。

## 3. 規則（雙方都要守）

1. **一檔一請求**，檔名＝`id`，唔好改已存在嘅請求檔（要改就新 `id`）。
2. **唔好放手寫 deploy 指令**：Hermes 收到 `zip_url` 只會下載 + diff，**部署一定要真人在 Telegram 講「推」**。
3. **唔好放 secrets**（token、HMAC secret、API key）落 repo，公開 repo 任何人都讀得到。
4. `removed` 大多係我哋 deploy dir 嘅 `.bak-*`／`.wrangler` 垃圾，睇 `removed_real` 才係真刪檔。
5. zip 上限 150MB、檔案數 4000；zip 內唔准 `..` 或絕對路徑。
6. Hermes 只處理 `project: "manmotemple"`，其他一律 `rejected`。

## 4. Hermes 端實作（VM）

- Poller：`/home/ubuntu/muse-bridge/poll.py`（cron `*/2 * * * *`）
- Clone：`/home/ubuntu/muse-bridge/repo`
- 已處理 id：`/home/ubuntu/muse-bridge/state.json`
- 部署腳本：`/home/ubuntu/man-mo-build/deploy-manmo.sh`（成功／失敗都會 Telegram 通知）
- 部署後由 Hermes 手動寫回 `status/<id>.json`（`state: deployed`）
