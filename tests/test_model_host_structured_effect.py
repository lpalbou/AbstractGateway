"""The real Runtime effect path through native RPC, without Core HTTP serving."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

from abstractgateway import model_host as mh
from abstractruntime import Effect, EffectType, RunState
from abstractruntime.integrations.abstractcore.effect_handlers import make_llm_call_handler


SCHEMA = {"type": "object", "properties": {
    "nested": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    "mode": {"type": "string", "enum": ["chat", "tool"]},
    "flag": {"type": "boolean", "default": False}}, "required": ["nested", "mode"]}


def test_structured_effect_keeps_schema_semantics_concurrency_and_restart(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def _json(self, doc):
            body = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self._json({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            requests.append(body)
            text = str(body.get("messages", []))
            if "INVALID_NESTED" in text:
                result = {"nested": {"n": "not-an-integer"}, "mode": "chat"}
            elif "CASE_ALPHA" in text:
                result = {"alpha": "13"}
            elif "CASE_BETA" in text:
                result = {"beta": "hello"}
            else:
                result = {"nested": {"n": "42"}, "mode": "chat"}
            self._json({"id": "fake", "object": "chat.completion", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(result)},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sup = mh.ModelHostSupervisor(tmp_path / "data", nice=0, backoff_s=(.1,)).start()
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor={"provider": "openai-compatible", "model": "fake-model",
            "llm_kwargs": {"base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                           "api_key": "fake", "enable_tracing": False}})

        class Capture:
            def generate(self, **kwargs):
                params = kwargs["params"]
                assert isinstance(params["response_schema"], dict)
                assert "response_model" not in params, "dynamic classes must not cross RPC"
                return client.generate(**kwargs)

        handler = make_llm_call_handler(llm=Capture())

        def call(prompt, schema=SCHEMA):
            run = RunState.new(workflow_id="assistant-routing", entry_node="route_call", vars={})
            effect = Effect(type=EffectType.LLM_CALL, payload={
                "prompt": prompt, "response_schema": schema, "response_schema_name": "LLM_StructuredOutput",
                "structured_output_fallback": False, "params": {"stream": True, "max_tokens": 32}})
            return handler(run, effect, None)

        result = call("VALID_NESTED")
        assert result.status == "completed", result
        assert result.result["data"] == {"nested": {"n": 42}, "mode": "chat", "flag": False}
        invalid = call("INVALID_NESTED")
        assert invalid.status == "failed" and "nested" in str(invalid.error)
        alpha = {"type": "object", "properties": {"alpha": {"type": "integer"}}, "required": ["alpha"]}
        beta = {"type": "object", "properties": {"beta": {"type": "string"}}, "required": ["beta"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(call, "CASE_ALPHA", alpha), pool.submit(call, "CASE_BETA", beta)
            ra, rb = a.result(), b.result()
        assert ra.status == rb.status == "completed"
        assert ra.result["data"] == {"alpha": 13} and rb.result["data"] == {"beta": "hello"}
        assert requests and all(request.get("stream") is False for request in requests)
        old_pid, generation = sup.pid, sup.generation
        sup.kill("test_restart")
        deadline = time.monotonic() + 15
        while not (sup.generation_alive() and sup.generation > generation):
            assert time.monotonic() < deadline, sup.snapshot()
            time.sleep(.05)
        assert sup.pid != old_pid
        after = call("VALID_AFTER_RESTART")
        assert after.status == "completed" and after.result["data"]["nested"]["n"] == 42
        assert not (tmp_path / "data" / "split" / "model_host.json").exists()
    finally:
        sup.stop()
        server.shutdown()
        server.server_close()
