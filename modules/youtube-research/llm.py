"""Minimal LLM completion helper.

Reuses AgeniusDesk's saved assistant provider config (provider, model, API key)
so the operator does not configure a key twice, but issues a direct, tool-free
completion with a large output budget (turning a long transcript into a
multi-thousand-token breakdown needs more than the assistant's chat defaults).

Importing the host's assistant module is allowed for a community module (it runs
in-process); the scanner surfaces it as an INFO host-import. The actual network
egress is to the configured provider host (api.openai.com / api.anthropic.com /
openrouter.ai), all declared in the manifest.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

MAX_TOKENS = 8000
_FALLBACK_FLOORS = [4096, 2048]
TIMEOUT = 300.0


class LLMError(RuntimeError):
    """An LLM completion failure with an operator-facing message."""


def _config() -> dict:
    """Resolve the saved assistant provider config from the host app.

    When the global assistant has no stored key (operators commonly set the key
    as a per-area $REF in Models instead), fall back to the conventional
    provider secret ($OPEN_ROUTER_KEY / $OPEN_AI_KEY / $ANTHROPIC_KEY), the same
    convention the assistant's own per-area resolution uses.
    """
    try:
        from backend.modules.assistant.providers import get_assistant_config
    except Exception as e:  # pragma: no cover - host without the assistant module
        raise LLMError(f"AgeniusDesk assistant config is unavailable: {e}") from e
    cfg = dict(get_assistant_config())
    provider = cfg.get("provider", "")
    if provider != "ollama" and not cfg.get("api_key"):
        try:
            from backend.config import decrypt_value
            from backend.modules.assistant.providers import PROVIDER_KEY_MAP
            name = PROVIDER_KEY_MAP.get(provider)
            if name:
                resolved = decrypt_value(f"${name}")
                if resolved and resolved != name:
                    cfg["api_key"] = resolved
        except Exception:  # pragma: no cover - fall through to the empty-key error
            pass
    return cfg


def _parse_supported_max(body: str) -> int | None:
    m = re.search(r"(?:at most|maximum(?: of)?|up to)\s+(\d{3,6})", body, re.I)
    if m:
        return int(m.group(1))
    nums = [int(n) for n in re.findall(r"\b(\d{3,6})\b", body)]
    return min(nums) if nums else None


async def complete(system: str, user: str, *, max_tokens: int = MAX_TOKENS, model: str = "") -> str:
    """Run one tool-free completion against the configured provider.

    `model` overrides the saved default for this call. If the provider rejects
    max_tokens as too large, retry with the model's actual ceiling (parsed from
    the error) or a fallback floor. Raises LLMError on misconfiguration or an
    unrecoverable provider failure.
    """
    cfg = _config()
    provider = cfg["provider"]
    model = model or cfg["model"]
    api_key = cfg.get("api_key") or ""

    if provider != "ollama" and not api_key:
        raise LLMError("No AI provider configured. Add an API key in Settings > AI.")

    tried: set[int] = set()
    attempt = max_tokens
    while True:
        tried.add(attempt)
        try:
            return await _dispatch(provider, cfg, system, user, model, api_key, attempt)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response is not None else ""
            status = e.response.status_code if e.response is not None else 0
            if status == 400 and "max_tokens" in body.lower() and "large" in body.lower():
                ceiling = _parse_supported_max(body)
                nxt = ceiling if ceiling and ceiling < attempt else next(
                    (f for f in _FALLBACK_FLOORS if f < attempt and f not in tried), None
                )
                if nxt and nxt not in tried:
                    logger.info("max_tokens %d too large; retrying with %d", attempt, nxt)
                    attempt = nxt
                    continue
            raise LLMError(f"Provider HTTP {status}: {body[:300]}") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Provider timed out after {TIMEOUT}s") from e


async def _dispatch(provider, cfg, system, user, model, api_key, max_tokens) -> str:
    if provider == "anthropic":
        return await _anthropic(system, user, model, api_key, max_tokens)
    if provider == "ollama":
        return await _ollama(system, user, model, cfg.get("ollama_url", ""), max_tokens)
    if provider == "openai":
        return await _openai_compat(
            system, user, model, api_key, max_tokens,
            "https://api.openai.com/v1/chat/completions",
        )
    return await _openai_compat(
        system, user, model, api_key, max_tokens,
        "https://openrouter.ai/api/v1/chat/completions",
        extra_headers={
            "HTTP-Referer": "https://github.com/Mfrostbutter/ageniusdesk-community-modules",
            "X-Title": "AgeniusDesk YouTube Research",
        },
    )


async def _openai_compat(system, user, model, api_key, max_tokens, url, extra_headers=None) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise LLMError("Provider returned an empty completion.")
    return content


async def _anthropic(system, user, model, api_key, max_tokens) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0.4,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text:
        raise LLMError("Anthropic returned an empty completion.")
    return text


async def _ollama(system, user, model, ollama_url, max_tokens) -> str:
    if not ollama_url:
        raise LLMError("Ollama selected but no Ollama URL is configured.")
    url = f"{ollama_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = data.get("message", {}).get("content", "")
    if not content:
        raise LLMError("Ollama returned an empty completion.")
    return content
