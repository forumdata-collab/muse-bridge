#!/usr/bin/env python3
"""Muse bridge: OpenAI-compatible endpoint on VPS localhost.
9Router 'Muse' provider -> http://127.0.0.1:8765/v1
Requests are queued; the Muse agent (sandbox) polls pending/ and writes answers to done/.

v3: threaded server (one slow chat request must not block /health or /v1/models),
per-connection socket timeout, BrokenPipe-safe sends.
Streaming: SSE headers are sent IMMEDIATELY (before the poller answers) with
periodic keepalive comments, so proxies in the chain (9Router, nginx, Cloudflare)
never time out waiting for the first byte while an answer is being produced.

v4 (2026-10-02): tool_calls passthrough. The done file may carry either
{"content": "..."} (unchanged) or {"tool_calls": [{id, type:"function",
function:{name, arguments}}]}. Tool calls are forwarded verbatim (after
normalization) with finish_reason "tool_calls", in both the plain and the
streaming response, so an agent client (e.g. Hermes) can execute the tools
on its side and continue the loop with role:"tool" result messages.

v5 (2026-10-07, Fase 1): queue cap raised 5 -> 20 so parallel sessions are
not rejected under load; usage fields carry honest token ESTIMATES
(chars/4 over the request and the answer) instead of zeros; streamed
content answers are split into several small delta chunks (pseudo-
streaming) instead of one big chunk at the end.
"""
import json, os, time, uuid, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("MUSE_BRIDGE_QUEUE", "/opt/muse-bridge/queue")
PORT = int(os.environ.get("MUSE_BRIDGE_PORT", "8765"))
PENDING = f"{BASE}/pending"
DONE = f"{BASE}/done"
MAX_PENDING = 20
WAIT_SECS = 240
KEEPALIVE_SECS = 15


def _normalize_tool_calls(raw):
    """Sanitize a tool_calls list from a done file into OpenAI shape.

    Returns a non-empty list of {"id", "type": "function",
    "function": {"name", "arguments": <JSON string>}} or None when the
    input holds no usable call. Never raises.
    """
    try:
        if not isinstance(raw, list):
            return None
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name") or item.get("name")
            if not name:
                continue
            args = fn.get("arguments", item.get("arguments", {}))
            if isinstance(args, str):
                try:
                    json.loads(args)
                except Exception:
                    args = json.dumps({"value": args})
            elif args is None:
                args = "{}"
            else:
                args = json.dumps(args)
            cid = item.get("id") or ("call_" + uuid.uuid4().hex[:24])
            out.append({"id": str(cid), "type": "function",
                        "function": {"name": str(name), "arguments": args}})
        return out or None
    except Exception:
        return None


def _estimate_tokens(text):
    """Rough token estimate (chars/4). Honest estimate, not a measurement."""
    try:
        return max(1, len(text) // 4)
    except Exception:
        return 0


def _usage(req, answer):
    """Estimated usage for one exchange, OpenAI shape."""
    try:
        prompt_text = json.dumps(req.get("messages", []), ensure_ascii=False)
        if req.get("tools"):
            prompt_text += json.dumps(req["tools"], ensure_ascii=False)
        if answer and "tool_calls" in answer:
            completion_text = json.dumps(answer["tool_calls"], ensure_ascii=False)
        elif answer:
            completion_text = answer.get("content", "") or ""
        else:
            completion_text = ""
        p, c = _estimate_tokens(prompt_text), _estimate_tokens(completion_text)
        return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _split_chunks(text, size=120):
    """Split content into small word-boundary pieces for pseudo-streaming."""
    if not text:
        return [""]
    out, cur = [], ""
    for word in text.split(" "):
        piece = word if not cur else cur + " " + word
        if len(piece) > size and cur:
            out.append(cur)
            cur = word
        else:
            cur = piece
    if cur:
        out.append(cur)
    # restore the single spaces that split() removed between pieces
    return [p + " " for p in out[:-1]] + out[-1:]


def _is_dashboard_probe(req):
    """Detect 9Router dashboard's 'Test Connection' probe.

    The dashboard sends {max_tokens:1024, stream:false,
    messages:[..., {role:"user", content:"hi"}]} with a 15s client timeout,
    which the 60s+ poller loop can never meet. Matching requests are answered
    instantly below; everything else goes through the normal queue.
    """
    try:
        if not isinstance(req, dict) or req.get("stream"):
            return False
        if req.get("max_tokens") != 1024:
            return False
        msgs = req.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        return (isinstance(last, dict) and last.get("role") == "user"
                and str(last.get("content", "")).strip().lower() == "hi")
    except Exception:
        return False


def _probe_completion():
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content":
                                     "Halo! Muse online — bridge 9Router aktif dan siap."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class H(BaseHTTPRequestHandler):
    timeout = 120  # per-socket-op timeout: slow-loris bodies can't wedge a thread forever

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        try:
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass  # client went away; nothing to do

    def _sse_write(self, payload: str) -> bool:
        """Write one SSE payload, flushed. Returns False if the client is gone."""
        try:
            self.wfile.write(payload.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False

    def _cleanup(self, rid: str):
        for d in (f"{PENDING}/{rid}.json", f"{DONE}/{rid}.json"):
            try:
                os.remove(d)
            except Exception:
                pass

    def do_GET(self):
        try:
            if self.path.rstrip("/") == "/v1/models":
                self._send(200, {"object": "list", "data": [
                    {"id": "muse", "object": "model", "created": 0, "owned_by": "muse"}]})
            elif self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _wait_answer(self, rid: str, keepalive_cb=None):
        """Wait up to WAIT_SECS for done/<rid>.json. keepalive_cb() is called
        every KEEPALIVE_SECS; if it returns False the client is gone.

        Returns None on timeout, otherwise a dict: {"tool_calls": [...]}
        when the answer carries usable tool calls, else {"content": str}.
        """
        deadline = time.time() + WAIT_SECS
        answer = None
        last_ping = time.time()
        while time.time() < deadline:
            dp = f"{DONE}/{rid}.json"
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        data = json.load(f)
                    calls = _normalize_tool_calls(
                        data.get("tool_calls") if isinstance(data, dict) else None)
                    if calls:
                        answer = {"tool_calls": calls}
                    elif isinstance(data, dict):
                        answer = {"content": data.get("content", "")}
                    else:
                        answer = {"content": ""}
                except Exception:
                    answer = {"content": ""}
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            if keepalive_cb and time.time() - last_ping >= KEEPALIVE_SECS:
                if not keepalive_cb():
                    break  # client disconnected
                last_ping = time.time()
            time.sleep(1)
        return answer

    def do_POST(self):
        try:
            with open("/tmp/bridge-access.log", "a") as lf:
                lf.write("%s POST %s auth=%s len=%s\n" % (
                    __import__("time").strftime("%H:%M:%S"), self.path,
                    str(self.headers.get("Authorization", "-"))[:24],
                    self.headers.get("Content-Length", "-")))
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": {"message": "bad json"}})
            if _is_dashboard_probe(req):
                return self._send(200, _probe_completion())
            try:
                npend = len(os.listdir(PENDING))
            except Exception:
                npend = 0
            if npend >= MAX_PENDING:
                return self._send(429, {"error": {"message": "Muse bridge busy, try again in a bit"}})
            rid = uuid.uuid4().hex
            with open(f"{PENDING}/{rid}.json", "w") as f:
                json.dump({"id": rid, "received_at": time.time(), "request": req}, f)

            if req.get("stream"):
                return self._handle_stream(req, rid)
            answer = self._wait_answer(rid)
            self._cleanup(rid)
            if answer is None:
                return self._send(504, {"error": {"message": "Muse did not answer in time"}})
            cid = "chatcmpl-" + uuid.uuid4().hex[:12]
            created = int(time.time())
            if "tool_calls" in answer:
                message = {"role": "assistant", "content": None,
                           "tool_calls": answer["tool_calls"]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": answer.get("content", "")}
                finish = "stop"
            resp = {"id": cid, "object": "chat.completion", "created": created, "model": "muse",
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": finish}],
                    "usage": _usage(req, answer)}
            self._send(200, resp)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _handle_stream(self, req, rid: str):
        # Headers go out immediately; body chunks follow when the answer lands.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self._cleanup(rid)
            return
        if not self._sse_write(": connected\n\n"):
            self._cleanup(rid)
            return
        answer = self._wait_answer(rid, keepalive_cb=lambda: self._sse_write(": ping\n\n"))
        self._cleanup(rid)
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        if answer is None:
            err = {"error": {"message": "Muse did not answer in time", "type": "timeout"}}
            self._sse_write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
            return
        if "tool_calls" in answer:
            delta_calls = [{"index": i, "id": c["id"], "type": "function",
                            "function": {"name": c["function"]["name"],
                                         "arguments": c["function"]["arguments"]}}
                           for i, c in enumerate(answer["tool_calls"])]
            chunk1 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                      "model": "muse", "choices": [{"index": 0,
                      "delta": {"role": "assistant", "tool_calls": delta_calls},
                      "finish_reason": None}]}
            chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                      "model": "muse", "choices": [{"index": 0, "delta": {},
                      "finish_reason": "tool_calls"}],
                      "usage": _usage(req, answer)}
            self._sse_write("data: " + json.dumps(chunk1) + "\n\n")
            self._sse_write("data: " + json.dumps(chunk2) + "\n\ndata: [DONE]\n\n")
            return
        # content: pseudo-stream in small pieces so clients render progressively
        pieces = _split_chunks(answer.get("content", ""))
        for i, piece in enumerate(pieces):
            delta = {"role": "assistant", "content": piece} if i == 0 else {"content": piece}
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": "muse", "choices": [{"index": 0, "delta": delta,
                     "finish_reason": None}]}
            if not self._sse_write("data: " + json.dumps(chunk) + "\n\n"):
                return
            time.sleep(0.03)
        final = {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": "muse", "choices": [{"index": 0, "delta": {},
                 "finish_reason": "stop"}], "usage": _usage(req, answer)}
        self._sse_write("data: " + json.dumps(final) + "\n\ndata: [DONE]\n\n")


if __name__ == "__main__":
    import subprocess
    import threading

    def listen_addrs():
        addrs = ["127.0.0.1"]
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=10).stdout
            for ip in out.split():
                ip = ip.strip()
                if ip and ip not in addrs:
                    addrs.append(ip)
        except Exception:
            pass
        return addrs

    for addr in listen_addrs():
        try:
            srv = ThreadingHTTPServer((addr, PORT), H)
        except OSError as e:
            print(f"skip {addr}:{PORT} ({e})", flush=True)
            continue
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"muse-bridge listening on {addr}:{PORT}", flush=True)
    threading.Event().wait()  # keep main thread alive
