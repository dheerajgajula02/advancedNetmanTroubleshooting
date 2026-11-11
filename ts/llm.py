"""The one way this project calls a language model. Each job uses the model chosen for it on the AI models page, and nothing else gets through."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import threading
import time
import uuid
import urllib.error
import urllib.request

from ts import aiconfig, capture

SECRETS = aiconfig.OPENCODE_ENV
DEFAULT_MODEL = os.environ.get("TS_LLM_MODEL", "deepseek-v4.1-flash")
USER_AGENT = "network-troubleshooting/1.0"
_OPTIONAL = {"temperature": "temperature", "reasoning_effort": "reasoning_effort"}


class LLMError(RuntimeError):
    pass


_conversation = threading.local()


def start_conversation() -> None:
    """From here on this thread is one conversation. The provider only reuses what it has already read when every turn says it is the same session."""
    _conversation.id = uuid.uuid4().hex


def end_conversation() -> None:
    _conversation.id = None


def _route(role: str, model: str | None) -> aiconfig.Choice:
    """What this call runs on: the job's own choice, unless a run names another model someone has already chosen."""
    try:
        chosen = aiconfig.choice(role)
    except (OSError, KeyError, ValueError) as exc:
        raise LLMError(f"No usable model for {role}: {type(exc).__name__}") from exc
    if model and model != chosen.model:
        if model not in aiconfig.chosen_models():
            raise LLMError(f"{model} has not been chosen on the AI models page")
        return dataclasses.replace(chosen, model=model, effort="")
    return chosen


def _headers(chosen: aiconfig.Choice) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if chosen.key:
        headers["Authorization"] = f"Bearer {chosen.key}"
    if chosen.built_in:  # it refuses a request without one, and only reuses its cache when every turn of a conversation sends the same
        headers["x-opencode-session"] = getattr(_conversation, "id", None) or uuid.uuid4().hex
    return headers


def _relax(body: dict, complaint: str) -> bool:
    """Endpoints differ in what they accept. Take out the optional setting this one objected to, if that is what it did."""
    for field in _OPTIONAL:
        if field in body and field in complaint:
            del body[field]
            return True
    if "max_tokens" in body and "max_completion_tokens" in complaint:
        body["max_completion_tokens"] = body.pop("max_tokens")
        return True
    return False


def _post(body: dict, chosen: aiconfig.Choice, timeout: int, retries: int) -> tuple[dict, float]:
    body = {**body, "model": chosen.model, **({"reasoning_effort": chosen.effort} if chosen.effort else {})}
    last, adjusted = None, 0
    for attempt in range(retries + 1):
        started = time.time()
        request = urllib.request.Request(chosen.base + "/chat/completions", data=json.dumps(body).encode(), headers=_headers(chosen))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
            data["choices"][0]["message"]
            capture.llm_call(body, data, time.time() - started, chosen.role)
            aiconfig.note_usage(chosen, data.get("usage") or {}, time.time() - started)
            return data, round(time.time() - started, 1)
        except urllib.error.HTTPError as exc:
            complaint = exc.read(300).decode(errors="replace")
            last = LLMError(f"HTTP {exc.code}: {complaint}")
            if exc.code == 400 and adjusted < 3 and _relax(body, complaint):
                adjusted += 1
                continue
            if exc.code not in (429, 500, 502, 503, 504):
                raise last from exc
        except (OSError, http.client.HTTPException, KeyError, IndexError, json.JSONDecodeError) as exc:  # includes a connection dropped halfway through the answer
            last = LLMError(f"{type(exc).__name__}: {exc}")
        time.sleep(2 * (attempt + 1))
    raise last


def _reasoning(message: dict) -> str:
    return message.get("reasoning_content") or message.get("reasoning") or ""


def complete(messages: list[dict], model: str | None = None, max_tokens: int = 8000, temperature: float = 0.2,
             timeout: int = 120, retries: int = 2, role: str = "notes") -> dict:
    """Returns {"text", "reasoning", "model", "seconds", "usage"}. Raises LLMError when no answer came back."""
    chosen = _route(role, model)
    last = None
    for _ in range(retries + 1):
        data, seconds = _post({"messages": messages, "max_tokens": max_tokens, "temperature": temperature}, chosen, timeout, retries)
        message = data["choices"][0]["message"]
        text = (message.get("content") or "").strip()
        if text:
            return {"text": text, "reasoning": _reasoning(message), "model": chosen.model, "seconds": seconds, "usage": data.get("usage", {})}
        last = LLMError(f"the model returned no text (finish reason: {data['choices'][0].get('finish_reason')})")
    raise last


def chat(messages: list[dict], tools: list[dict], model: str | None = None, max_tokens: int = 6000, temperature: float = 0.1,
         timeout: int = 150, retries: int = 2, role: str = "support") -> dict:
    """One turn with tools. Returns {"text", "reasoning", "calls": [{"id", "name", "args"}], "message", "model", "seconds"}.
    `message` is what to put back in the conversation."""
    chosen = _route(role, model)
    data, seconds = _post({"messages": messages, "tools": tools, "max_tokens": max_tokens, "temperature": temperature}, chosen, timeout, retries)
    message = data["choices"][0]["message"]
    calls = []
    for call in message.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = None
        calls.append({"id": call["id"], "name": call["function"]["name"], "args": args})
    echoed = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        echoed["tool_calls"] = message["tool_calls"]
    if _reasoning(message):
        echoed["reasoning_content"] = _reasoning(message)
    return {"text": (message.get("content") or "").strip(), "reasoning": _reasoning(message), "calls": calls, "message": echoed, "model": chosen.model, "seconds": seconds,
            "finish": data["choices"][0].get("finish_reason"), "usage": data.get("usage", {})}


def parse_json(text: str) -> dict | None:
    """The first JSON object in a model's answer, or None."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError:
        return None
