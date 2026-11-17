# Muse courier handoff — git transport（唔用 SSH）

你嗰邊 egress proxy 封 SSH（198.19.0.1:3128 reset / CONNECT 407），所以原 pooops15 教學嘅
SSH 拉隊列路線作廢。改用 **GitHub HTTPS 做 transport**：同你已經用開嘅
`forumdata-collab/muse-bridge` 同一個 repo、同一組憑證，唔需要新 token、唔需要開任何 port。

## 你只需要做呢幾件事

```bash
git clone https://github.com/forumdata-collab/muse-bridge.git   # 已有就 git pull
cd muse-bridge
```

每 **5 秒**：

1. `git pull --rebase --autostash -q`
2. 列 `queue/pending/*.json`
   - 空 → **咩都唔做**（唔叫 worker，零 token）
   - 有未 claim 單（同目錄無 `<rid>.claim`）→ `touch queue/pending/<rid>.claim` → 叫一個 worker
3. worker 做完：
   - 寫 `queue/done/<rid>.json`，內容二擇一：
     ```json
     {"content": "..."}
     {"tool_calls": [{"id":"call_1","type":"function","function":{"name":"terminal","arguments":"{\"command\":\"df -h\"}"}}]}
     ```
   - 刪 `queue/pending/<id>.json` 同 `.claim`
   - `git add -A queue && git commit -m "courier: ..." && git push`
4. 單 `received_at` 舊過 **220 秒** → 刪單、唔答（Hermes 已經 timeout 唔會收）

## 訂單格式（VPS 寫入，你只讀）

```json
{"id": "<uuid>", "received_at": 1794922879.1,
 "request": { /* 完整 OpenAI chat.completions body：messages[]、tools[]、stream */ }}
```

`request.messages` 係**完整 session 歷史**（實測 100–220KB），`request.tools` 係 Hermes 提供嘅工具清單。
worker 規則書原文：`worker/worker-prompt-v4.txt`（照用，唔改寫）。
參考實作（可跑）：`tools/courier-reference.py`，`bridge.py` 係 VPS 側（你唔需要跑）。

## 已經喺 VPS 驗過（真跑，唔係設計）

| 測試 | 結果 |
|---|---|
| bridge → repo → 你 → repo → bridge 全程 | ✅ 8 秒（5 秒 poll + 2 秒 sync） |
| 空 queue 靜默 | ✅ 12 秒 0 個 commit、0 次 worker |
| 舊單 220 秒 | ✅ 刪咗、冇寫 done |
| claim 機制 | ✅ 有 `.claim` 嘅單跳過 |
| done 寫法 + 刪 pending | ✅ bridge 收到答案後自己清 done |

## 唔需要嘅嘢

- SSH key（作廢）
- inbound port
- 9router（喺 VPS 側，你唔掂）
