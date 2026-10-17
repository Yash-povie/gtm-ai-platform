"""Provider-neutral single-turn completion: Anthropic Messages API or any OpenAI-compatible endpoint.

LLM_PROVIDER=anthropic (default) | openai_compat (NVIDIA NIM, vLLM, OpenRouter, ...).
Callers always have a deterministic fallback, so a provider outage degrades quality, never safety.
"""

import re
from typing import Any

import httpx

from gtm_core.config import get_settings

_THINK = re.compile(r"<think>.*?</think>", re.S)


def llm_enabled() -> bool:
    s = get_settings()
    key = s.llm_api_key if s.llm_provider == "openai_compat" else s.anthropic_api_key
    return key not in ("", "mock")


async def complete(*, system: str, user: str, model: str, max_tokens: int = 800, client: Any = None,
                   http: httpx.AsyncClient | None = None, json_mode: bool = False) -> str:
    """Return the assistant text. `client` is an Anthropic-style client (used by tests to inject fakes)."""
    s = get_settings()
    if client is not None or s.llm_provider == "anthropic":
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=s.anthropic_api_key)
        resp = await client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                            messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in resp.content)

    payload: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "temperature": 0.2,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if s.llm_disable_thinking:  # reasoning models otherwise burn the token budget before answering
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    own = http is None
    http = http or httpx.AsyncClient(timeout=120.0)
    try:
        url = f"{s.llm_base_url.rstrip('/')}/chat/completions"
        r = await http.post(url, json=payload, headers=headers)
        if r.status_code == 400 and json_mode:  # server does not support response_format: retry plain
            payload.pop("response_format")
            r = await http.post(url, json=payload, headers=headers)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
    finally:
        if own:
            await http.aclose()
    text = _THINK.sub("", text).strip()  # drop reasoning blocks from thinking models
    if not text:
        raise ValueError("empty completion")
    return text
