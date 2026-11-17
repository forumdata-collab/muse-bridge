#!/usr/bin/env python3
"""Courier + worker 參考實作（transport = git，唔用 SSH）。

放喺 Muse 沙盒跑：每 5 秒 `git pull`，睇 queue/pending/*.json。
- 空 → 靜默（唔叫 worker，零 token）
- 有未 claim 單 → 建 pending/<rid>.claim → 叫一個 worker
- worker 答完 → 寫 done/<rid>.json、刪 pending/<rid>.json 同 .claim → `git push`
- 舊過 220 秒 → 刪單唔答

用法：python3 courier-reference.py [--once]
WORKER 部分係 stub（answer()），真部署時改成叫你嘅 agent；
worker-prompt-v4.txt 原文要作為 system prompt 餵入。
"""
import json, os, pathlib, subprocess, sys, time

REPO = pathlib.Path(os.environ.get("MUSE_QUEUE_REPO", "/path/to/muse-bridge/repo"))
QUEUE = REPO / "queue"
PENDING, DONE = QUEUE / "pending", QUEUE / "done"
STALE = 220
POLL = 5


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, timeout=120)


def answer(order):
    """Worker stub：讀訂單 → 回覆。真實版本要：載入 worker-prompt-v4.txt、
    先睇圖、有 tool 結果就照實答、需要真機就用 tool_calls。"""
    msgs = order["request"].get("messages") or []
    last = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "")
    return {"content": f"[stub worker] 收到：{str(last)[:200]}"}


def tick(wake):
    git("pull", "--rebase", "--autostash", "-q")
    changed = False
    for pf in sorted(PENDING.glob("*.json")):
        rid = pf.stem
        claim = PENDING / f"{rid}.claim"
        try:
            order = json.loads(pf.read_text())
        except Exception:
            continue
        if time.time() - float(order.get("received_at") or 0) > STALE:
            pf.unlink(missing_ok=True); claim.unlink(missing_ok=True)
            changed = True
            continue
        if claim.exists():
            continue                       # 已經有 worker 做緊
        claim.touch()
        changed = True
        result = answer(order)              # ← 唯一會燒 token 嘅位
        (DONE / f"{rid}.json").write_text(json.dumps(result, ensure_ascii=False))
        pf.unlink(missing_ok=True); claim.unlink(missing_ok=True)
        changed = True
    if changed:
        git("add", "-A", "queue")
        if git("commit", "-q", "-m", f"courier: {time.strftime('%H:%M:%S')}").returncode == 0:
            git("push", "-q", "origin", "HEAD")


if __name__ == "__main__":
    once = "--once" in sys.argv
    while True:
        try:
            tick(True)
        except Exception as e:
            print("tick error:", e, flush=True)
        if once:
            break
        time.sleep(POLL)
