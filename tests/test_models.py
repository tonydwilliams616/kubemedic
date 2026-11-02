"""Checks the OpenAI-compatible adapter translates messages and tool calls both ways, talks to a real (local, fake) HTTP server correctly, and lets the agent run end to end on it."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kubemedic.agent import SUBMIT, investigate
from kubemedic.models import (ModelServerError, OpenAICompatModel, from_openai_message, make_model,
                              strip_thinking, to_openai_messages, to_openai_tools)
from kubemedic.tools import Toolbox
from test_agent import diagnosis, kubectl
from test_tools import NS


# ---- translation ---------------------------------------------------------------------------

def test_agent_conversation_translates_to_openai_shape():
    converse = [
        {"role": "user", "content": [{"text": "investigate"}]},
        {"role": "assistant", "content": [{"text": "look at pods"},
                                          {"toolUse": {"toolUseId": "t1", "name": "list_pods", "input": {}}},
                                          {"toolUse": {"toolUseId": "t2", "name": "get_logs", "input": {"pod": "a"}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"json": {"pods": []}}], "status": "success"}},
                                     {"toolResult": {"toolUseId": "t2", "content": [{"text": "boom"}], "status": "error"}},
                                     {"text": "step 3 of 12"}]},
    ]
    out = to_openai_messages("SYS", converse)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert out[2]["content"] == "look at pods"
    assert out[2]["tool_calls"][1] == {"id": "t2", "type": "function",
                                       "function": {"name": "get_logs", "arguments": '{"pod": "a"}'}}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": '{"pods": []}'}
    assert out[4]["content"] == "boom" and out[5] == {"role": "user", "content": "step 3 of 12"}


def test_assistant_with_only_tool_calls_has_null_content():
    out = to_openai_messages("S", [{"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "x", "name": "list_pods", "input": {}}}]}])
    assert out[1]["content"] is None and len(out[1]["tool_calls"]) == 1


def test_tool_specs_translate():
    spec = {"toolSpec": {"name": "get_logs", "description": "d", "inputSchema": {"json": {"type": "object"}}}}
    assert to_openai_tools([spec]) == [{"type": "function", "function": {
        "name": "get_logs", "description": "d", "parameters": {"type": "object"}}}]


def test_reply_with_tool_calls_translates_back_to_converse_blocks():
    blocks = from_openai_message({"content": "<think>hmm</think>checking", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_logs", "arguments": '{"pod": "a"}'}},
        {"type": "function", "function": {"name": "list_pods", "arguments": ""}}]})
    assert blocks == [{"text": "checking"},
                      {"toolUse": {"toolUseId": "c1", "name": "get_logs", "input": {"pod": "a"}}},
                      {"toolUse": {"toolUseId": "call_1", "name": "list_pods", "input": {}}}]


def test_malformed_tool_arguments_are_passed_on_not_crashed_on():
    [block] = from_openai_message({"content": None, "tool_calls": [
        {"id": "c", "function": {"name": "get_logs", "arguments": '{"pod": '}}]})
    assert block["toolUse"]["input"] == {"invalid_json_arguments": '{"pod": '}


def test_thinking_is_stripped_even_if_unclosed():
    assert strip_thinking('<think>a\nb</think>\n{"x": 1}') == '{"x": 1}'
    assert strip_thinking("<think>ran out of tokens") == ""
    assert strip_thinking(None) == ""


def test_make_model_routes_openai_ids():
    m = make_model("openai:Qwen/Qwen3-8B")
    assert isinstance(m, OpenAICompatModel)
    assert m.model_id == "openai:Qwen/Qwen3-8B" and m.served_name == "Qwen/Qwen3-8B"


# ---- over HTTP ------------------------------------------------------------------------------

class FakeServer:
    """A tiny OpenAI-compatible server on localhost: replies from a script, records every request."""

    def __init__(self, *replies, status=200):
        self.replies, self.requests, self.status = list(replies), [], status
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                server.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                        "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                body = json.dumps(server.replies.pop(0) if server.status == 200 else {"error": "bad model"}).encode()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reply(content=None, tool_calls=None, tokens=(100, 10)):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}
                             for i, (name, args) in enumerate(tool_calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


@contextmanager
def serve(*replies, **kw):
    s = FakeServer(*replies, **kw)
    try:
        yield s
    finally:
        s.close()


def test_converse_sends_the_same_settings_as_bedrock():
    with serve(reply("<think>x</think>{\"ok\": true}", tokens=(1200, 80))) as s:
        text, usage = OpenAICompatModel("openai:m", base_url=s.url, api_key="k").converse("SYS", [
            {"role": "user", "content": [{"text": "hi"}]}])
        assert text == '{"ok": true}' and usage == {"input_tokens": 1200, "output_tokens": 80}
        req = s.requests[0]
        assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer k"
        body = req["body"]
        assert body["model"] == "m" and body["temperature"] == 0 and body["max_tokens"] == 1000
        assert body["chat_template_kwargs"] == {"enable_thinking": False} and "tools" not in body
        assert body["response_format"] == {"type": "json_object"}       # syntax guaranteed; content still checked


def test_tool_choice_required_or_one_named_tool():
    with serve(reply(tool_calls=[("list_pods", {})]), reply(tool_calls=[(SUBMIT, {})])) as s:
        m = OpenAICompatModel("openai:m", base_url=s.url, api_key="")
        spec = [{"toolSpec": {"name": "list_pods", "description": "d", "inputSchema": {"json": {"type": "object"}}}}]
        m.converse_tools("S", [{"role": "user", "content": [{"text": "go"}]}], spec)
        m.converse_tools("S", [{"role": "user", "content": [{"text": "go"}]}], spec, force_tool=SUBMIT)
        assert s.requests[0]["body"]["tool_choice"] == "required" and s.requests[0]["auth"] is None
        assert s.requests[1]["body"]["tool_choice"] == {"type": "function", "function": {"name": SUBMIT}}


def test_server_errors_are_clear():
    with serve(status=400) as s:
        with pytest.raises(ModelServerError, match="HTTP 400.*bad model"):
            OpenAICompatModel("openai:m", base_url=s.url).converse("S", [{"role": "user", "content": [{"text": "x"}]}])
        with pytest.raises(ModelServerError, match="port-forward"):
            OpenAICompatModel("openai:m", base_url="http://127.0.0.1:9/v1", timeout_s=2).converse(
                "S", [{"role": "user", "content": [{"text": "x"}]}])


def test_agent_investigates_end_to_end_over_http():
    """The real agent loop, unchanged, driving a self-hosted model through the adapter."""
    with serve(reply("checking deployments", tool_calls=[("list_deployments", {})]),
               reply(tool_calls=[("get_deployment", {"name": "worker"}), ("list_pods", {})]),
               reply(tool_calls=[(SUBMIT, diagnosis())])) as s:
        r = investigate(Toolbox(NS, kubectl()), OpenAICompatModel("openai:qwen", base_url=s.url))
        assert r.valid and r.diagnosis["proposed_action"]["action"] == "rollback"
        assert [c["tool"] for c in r.tool_calls] == ["list_deployments", "get_deployment", "list_pods"]
        assert r.model_id == "openai:qwen" and r.input_tokens == 300
        # The third request carries the whole conversation in OpenAI form: the calls, then their results.
        roles = [m["role"] for m in s.requests[2]["body"]["messages"]]
        assert roles[:2] == ["system", "user"] and roles.count("tool") == 3
