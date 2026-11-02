"""Lets kubemedic use a self-hosted model on any OpenAI-compatible server (such as vLLM) as well as Bedrock, by translating between the two message formats.

Everything else in kubemedic speaks Bedrock's Converse format: messages made of content blocks
({"text"}, {"toolUse"}, {"toolResult"}). Open models are usually served behind the OpenAI chat
API instead (vLLM, llama.cpp, Ollama, TGI all offer it), where tool calls and tool results are
shaped differently. OpenAICompatModel translates on the way out and back, so the one-shot
diagnoser, the agent, the evals and the fix flow work unchanged with either kind of model.

Choosing a model is still just an ID:
    us.amazon.nova-lite-v1:0     -> Bedrock (as before)
    openai:Qwen/Qwen3-8B         -> the model "Qwen/Qwen3-8B" on an OpenAI-compatible server

The server's address comes from KUBEMEDIC_OPENAI_BASE_URL (default http://localhost:8000/v1, i.e.
a `kubectl port-forward` to vLLM); KUBEMEDIC_OPENAI_API_KEY is sent if set. Only the standard
library is used, so no new dependency.

Settings match the Bedrock path where the API allows: temperature 0, the same output-token caps,
tool_choice "required" (= Converse toolChoice "any") and a named tool for the forced last step.

One deliberate difference: one-shot replies use JSON mode (constrained decoding), so the server
only lets the model produce syntactically valid JSON. Without it, Qwen3-8B returned answers that
were right but not JSON (key-value pairs inside a list) on its first live test. JSON mode fixes
the syntax only: whether the answer has the right fields, a real action and real citations is
still the model's job, checked by the same contract as Bedrock's answers. Agent tool calls are
already constrained by tool_choice.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .diagnose import AGENT_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS

OPENAI_PREFIX = "openai:"
DEFAULT_BASE_URL = "http://localhost:8000/v1"
# Some open models (Qwen3, DeepSeek-R1 style) write out their reasoning between these tags first.
# It's never part of the answer, so it is removed before anything parses the reply.
THINK = re.compile(r"<think>.*?(</think>|$)", re.S)


class ModelServerError(RuntimeError):
    """The model server couldn't be reached or refused the request."""


def make_model(model_id: str):
    """openai:<name> -> a self-hosted model on an OpenAI-compatible server; anything else -> Bedrock."""
    if model_id.startswith(OPENAI_PREFIX):
        return OpenAICompatModel(model_id)
    from .diagnose import BedrockModel
    return BedrockModel(model_id)


# ---- translation: Converse -> OpenAI --------------------------------------------------------

def _tool_result_text(result: dict) -> str:
    parts = [json.dumps(b["json"]) if "json" in b else b.get("text", "") for b in result.get("content", [])]
    return "\n".join(parts)


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        blocks = m["content"]
        text = "".join(b["text"] for b in blocks if "text" in b)
        if m["role"] == "assistant":
            msg = {"role": "assistant", "content": text or None}
            calls = [b["toolUse"] for b in blocks if "toolUse" in b]
            if calls:
                msg["tool_calls"] = [{"id": c["toolUseId"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                     for c in calls]
            out.append(msg)
            continue
        # A Converse user turn can carry tool results AND text; OpenAI wants one "tool" message per
        # result (straight after the assistant's calls), then any text as a normal user message.
        for b in blocks:
            if "toolResult" in b:
                out.append({"role": "tool", "tool_call_id": b["toolResult"]["toolUseId"],
                            "content": _tool_result_text(b["toolResult"])})
        if text:
            out.append({"role": "user", "content": text})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["toolSpec"]["name"],
                                              "description": t["toolSpec"]["description"],
                                              "parameters": t["toolSpec"]["inputSchema"]["json"]}}
            for t in tools]


# ---- translation: OpenAI -> Converse --------------------------------------------------------

def strip_thinking(text: str | None) -> str:
    return THINK.sub("", text or "").strip()


def from_openai_message(msg: dict) -> list[dict]:
    blocks = []
    text = strip_thinking(msg.get("content"))
    if text:
        blocks.append({"text": text})
    for i, call in enumerate(msg.get("tool_calls") or []):
        raw = call["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            # Pass it on rather than crash: the toolbox's schema check rejects it and the agent is
            # told why, exactly as for any other malformed call.
            args = {"invalid_json_arguments": raw[:200]}
        blocks.append({"toolUse": {"toolUseId": call.get("id") or f"call_{i}", "name": call["function"]["name"],
                                   "input": args if isinstance(args, dict) else {"arguments": args}}})
    return blocks


def _usage(resp: dict) -> dict:
    u = resp.get("usage") or {}
    return {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


# ---- the model ------------------------------------------------------------------------------

class OpenAICompatModel:
    def __init__(self, model_id: str, base_url: str | None = None, api_key: str | None = None,
                 timeout_s: float = 300, chat_template_kwargs: dict | None = None):
        self.model_id = model_id                                    # kept whole, e.g. "openai:Qwen/Qwen3-8B"
        self.served_name = model_id.removeprefix(OPENAI_PREFIX)     # what the server calls it
        self.base_url = (base_url or os.environ.get("KUBEMEDIC_OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("KUBEMEDIC_OPENAI_API_KEY")
        self.timeout_s = timeout_s
        # Turns Qwen3's thinking mode off (vLLM passes this to the chat template; templates that
        # don't use it ignore it). Thinking costs many tokens and seconds; the Bedrock models we
        # compare against don't do it either, so it would not be a like-for-like comparison.
        self.chat_template_kwargs = {"enable_thinking": False} if chat_template_kwargs is None else chat_template_kwargs

    def _post(self, payload: dict) -> dict:
        payload = {"model": self.served_name, "temperature": 0, **payload}
        if self.chat_template_kwargs:
            payload["chat_template_kwargs"] = self.chat_template_kwargs
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            raise ModelServerError(f"{self.base_url} refused the request (HTTP {e.code}): {body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ModelServerError(f"can't reach the model server at {self.base_url} ({e}); "
                                   "is the port-forward running?") from e

    def converse(self, system: str, messages: list[dict]) -> tuple[str, dict]:
        # Every caller of converse (the one-shot diagnoser) expects one JSON object back
        resp = self._post({"messages": to_openai_messages(system, messages), "max_tokens": MAX_OUTPUT_TOKENS,
                           "response_format": {"type": "json_object"}})
        return strip_thinking(resp["choices"][0]["message"].get("content")), _usage(resp)

    def converse_tools(self, system: str, messages: list[dict], tools: list[dict],
                       force_tool: str | None = None) -> tuple[list[dict], dict]:
        choice = {"type": "function", "function": {"name": force_tool}} if force_tool else "required"
        resp = self._post({"messages": to_openai_messages(system, messages), "tools": to_openai_tools(tools),
                           "tool_choice": choice, "max_tokens": AGENT_MAX_OUTPUT_TOKENS})
        return from_openai_message(resp["choices"][0]["message"]), _usage(resp)
