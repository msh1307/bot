"""Pi 1.4 compaction integration against a local fake provider; never invokes EVO inference."""
import hashlib
import ast
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "sc117-flash-next-iq3_xxs"
runner_path = Path(sys.argv[1])
RUNTIME = str(runner_path.parent / 'bin/pi-1.4.0')
output_path = Path(sys.argv[2])
overflow = "--overflow" in sys.argv[3:]
payloads = []
frames = []
errors = []
GOAL = "SAMPLE-GOAL: preserve the requested invariant audit; SAMPLE-NEXT: inspect the ownership check."


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        payloads.append(body)
        summary = body.get("final_output_max_tokens") == 8192
        if overflow and len(payloads) == 2:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps({"type": "invalid_request_error", "message":
                "request exceeds the context window: prompt (135465 tokens) + max tokens (1) exceeds the context (131072); requests are never truncated."}).encode())
            return
        text = GOAL if summary else "SAMPLE-NEXT confirmed."
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        prompt_tokens = 2000 if overflow or summary or len(payloads) == 1 else 120000
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 20,
                 "total_tokens": prompt_tokens + 20}
        chunks = [
            {"id": "fixture", "object": "chat.completion.chunk", "model": MODEL,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
            {"id": "fixture", "object": "chat.completion.chunk", "model": MODEL,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
        ]
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix="evo-pi-compaction-") as tmp:
            home = Path(tmp)
            profile = home / ".pi/agent"
            profile.mkdir(parents=True)
            workspace = home / "workspace"
            workspace.mkdir()
            live_config = (runner_path.parent / 'config.yml').read_text()
            assert "reserveTokens: 28000" in live_config, "live reserve changed; recheck configuration"
            (profile / "config.yml").write_text(live_config.replace("keepRecentTokens: 12000", "keepRecentTokens: 100"))
            provider = {"providers": {"evo": {"baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                "api": "openai-completions", "apiKey": "test-only", "models": [{"id": MODEL,
                "name": MODEL, "reasoning": True, "input": ["text"], "contextWindow": 131072,
                "maxTokens": 8192, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}
            (profile / "models.yml").write_text(json.dumps(provider))
            (home / "instructions.txt").write_text("Answer briefly. Preserve the user's goal while summarizing.")
            code = runner_path.read_text().replace("const home = '/home/agent';", "const home = " + json.dumps(tmp) + ";")
            code = code.replace("from './pi-tools.mjs'", 'from ' + json.dumps(str(runner_path.parent / 'pi-tools.mjs')))
            code = code.replace("'/workspace'", json.dumps(str(workspace)))
            test_runner = home / "runner.mjs"
            test_runner.write_text(code)
            env = dict(os.environ, EVO_PI_RUNTIME=RUNTIME)
            proc = subprocess.Popen(["node", str(test_runner), MODEL], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            received = queue.Queue()

            def read_frames():
                for line in proc.stdout:
                    try:
                        frame = json.loads(line)
                        frames.append(frame)
                        received.put(frame)
                    except Exception as e:
                        errors.append(str(e))

            threading.Thread(target=read_frames, daemon=True).start()

            def wait_for(predicate):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        frame = received.get(timeout=1)
                    except queue.Empty:
                        if proc.poll() is not None:
                            raise AssertionError("Pi runner exited: " + proc.stderr.read()[-3000:])
                        continue
                    if predicate(frame):
                        return frame
                raise AssertionError("Timed out: " + json.dumps(frames[-5:]) + "; requests=" + str(len(payloads)))

            def send(frame):
                proc.stdin.write(json.dumps(frame) + "\n")
                proc.stdin.flush()

            wait_for(lambda x: x.get("type") == "ready")
            send({"type": "set_host_tools", "id": "tools", "tools": []})
            init = wait_for(lambda x: x.get("id") == "tools")
            assert init.get("success"), init
            send({"type": "set_event_filter", "id": "filter", "events": ["auto_compaction_start", "auto_compaction_end", "message_end"]})
            wait_for(lambda x: x.get("id") == "filter")
            send({"type": "prompt", "id": "setup", "message": GOAL})
            setup = wait_for(lambda x: x.get("type") == "prompt_result")
            assert setup.get("status") == "completed", setup
            send({"type": "prompt", "id": "turn", "message": "Continue the requested check.\n" + "fixture data " * 500})
            done = wait_for(lambda x: x.get("type") == "prompt_result")
            assert done.get("status") == "completed", done
            output_path.with_suffix(".diagnostic.json").write_text(json.dumps({"frames": frames, "payloads": payloads}, indent=2))
            starts = [x for x in frames if x.get("type") == "auto_compaction_start"]
            ends = [x for x in frames if x.get("type") == "auto_compaction_end"]
            assert len(starts) == len(ends) == 1, (starts, ends)
            assert starts[0].get("reason") == ("overflow" if overflow else "threshold"), starts
            assert ends[0].get("willRetry") is overflow, ends
            assert not ends[0].get("errorMessage"), ends[0]
            summaries = [p for p in payloads if p.get("final_output_max_tokens") == 8192]
            assert len(summaries) >= 1, [list(p) for p in payloads]
            summary = summaries[0]
            assert all(p["max_tokens"] == 0 and p["reasoning_budget_tokens"] == 0 for p in summaries), summaries
            assert all(p["chat_template_kwargs"]["enable_thinking"] is True for p in summaries), summaries
            assert "SAMPLE-GOAL" in json.dumps([p["messages"] for p in summaries]), "summary lost input goal"
            result = ends[0]["result"]
            assert result["tokensAfter"] == result["estimatedTokensAfter"] > 0, result
            assert "SAMPLE-GOAL" in result["summary"], result
            assert_count = sum(isinstance(n, ast.Assert) for n in ast.walk(ast.parse(Path(__file__).read_text())))
            report = {"passed": assert_count, "failed": 0, "runtime": "Pi 1.4.0", "liveReserveTokens": 28000,
                "effectiveThresholdTokens": 103072, "mode": "local fake provider with actual installed Pi; EVO inference requests=0",
                "autoCompactionStarts": len(starts), "autoCompactionEnds": len(ends),
                "reason": starts[0].get("reason"), "summaryPayload": {k: summary[k] for k in (
                    "max_tokens", "final_output_max_tokens", "reasoning_budget_tokens", "chat_template_kwargs")},
                "goalPreserved": True, "tokensAfter": result["tokensAfter"], "retryAfterOverflow": overflow,
                "providerRequests": len(payloads), "summaryRequests": len(summaries),
                "runnerSha256": hashlib.sha256(runner_path.read_bytes()).hexdigest()}
            output_path.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
