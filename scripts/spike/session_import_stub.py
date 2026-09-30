"""抓 opencode 送給模型的請求位元組（OpenAI-compatible stub provider）。

只用自己造的測試 session。把 opencode 指向本 stub，它每次呼叫模型時
的完整 request body 就會存成 req-NNN.json；回固定文字，不調外部模型。

用法：
  python3 session_import_stub.py --port 18080 --dir CAPDIR --reply "STUB-OK"
  # 另開終端（隔離資料目錄，不要碰真實 Session）：
  export XDG_DATA_HOME=... XDG_CONFIG_HOME=... XDG_STATE_HOME=... XDG_CACHE_HOME=...
  # XDG_CONFIG_HOME/opencode/opencode.json 內加 custom provider：
  # {"provider": {"stub": {"npm": "@ai-sdk/openai-compatible",
  #   "name": "Stub (local)", "options": {"baseURL": "http://127.0.0.1:18080/v1"},
  #   "models": {"stub-echo": {"name": "Stub Echo"}}}}}
  opencode run -m stub/stub-echo -s <session> "探針文字"
"""

import argparse
import http.server
import json
import socketserver
import threading

ARGS = None
COUNTER = 0
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps(
                {"object": "list", "data": [{"id": "stub-echo", "object": "model"}]}
            ).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        global COUNTER
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        with LOCK:
            COUNTER += 1
            n = COUNTER
        with open(f"{ARGS.dir}/req-{n:03d}.json", "wb") as f:
            f.write(raw)
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        stream = req.get("stream") is True
        reply = ARGS.reply
        if stream:
            chunk = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "stub-echo",
                "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
            }
            done = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "stub-echo",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            body = (
                ("data: " + json.dumps(chunk) + "\n\n").encode()
                + ("data: " + json.dumps(done) + "\n\n").encode()
                + b"data: [DONE]\n\n"
            )
            self._send(200, "text/event-stream", body)
        else:
            body = json.dumps(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "stub-echo",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self._send(200, "application/json", body)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--reply", default="STUB-OK")
    ARGS = ap.parse_args()
    import os

    os.makedirs(ARGS.dir, exist_ok=True)
    with socketserver.TCPServer(("127.0.0.1", ARGS.port), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"stub listening on 127.0.0.1:{ARGS.port}, dir={ARGS.dir}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
