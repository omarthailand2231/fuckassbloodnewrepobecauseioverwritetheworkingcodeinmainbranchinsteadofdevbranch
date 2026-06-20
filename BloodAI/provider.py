import os
import json
import time
import logging
import asyncio
from dotenv import load_dotenv
from config import CONFIG, USE_GROQ_API, USE_MIMO_API, USE_GATEWAY_API

log = logging.getLogger("blood.provider")

load_dotenv()

# ── Groq (legacy) ────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = CONFIG.get("deepseek_model", "deepseek-chat")

GROQ_MODELS = CONFIG["models"]

# ── Moonshot (Kimi K2.5) ─────────────────────────────────────────────────────
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")
MOONSHOT_BASE_URL = CONFIG["moonshot_base_url"]
MOONSHOT_MODEL = CONFIG["moonshot_model"]

# ── Xiaomi MiMo (OpenAI-compatible) ──────────────────────────────────────────
MIMO_API_KEY = os.getenv("MIMO_API_KEY")
MIMO_BASE_URL = CONFIG["mimo_base_url"]
MIMO_MODEL = CONFIG["mimo_model"]

# ── 9arm Gateway (OpenAI-compatible, Bearer auth) ────────────────────────────
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY")
GATEWAY_BASE_URL = CONFIG["gateway_base_url"]
GATEWAY_MODEL = CONFIG["gateway_model"]

# Global semaphore — max 3 concurrent chat calls to avoid rate limit storms
_api_semaphore = asyncio.Semaphore(3)

# ── Rate limit tracker ────────────────────────────────────────────────────────
_rate_limits: dict[str, dict] = {}

def get_rate_limits() -> dict[str, dict]:
    """Return last known rate limit state for all models."""
    return dict(_rate_limits)

def _parse_groq_headers(model: str, headers):
    """Extract and store Groq rate limit headers with robust fallback."""
    h = {k.lower(): v for k, v in headers.items()}

    def _get(*keys):
        for k in keys:
            if k in h: return h[k]
        return None

    _rate_limits[model] = {
        "limit_tokens_minute":      _get("x-ratelimit-limit-tokens", "x-ratelimit-limit-tokens-minute"),
        "remaining_tokens_minute":  _get("x-ratelimit-remaining-tokens", "x-ratelimit-remaining-tokens-minute"),
        "limit_tokens_day":         _get("x-ratelimit-limit-tokens-day", "x-ratelimit-limit-tokens-day"),
        "remaining_tokens_day":     _get("x-ratelimit-remaining-tokens-day", "x-ratelimit-remaining-tokens-day"),
        "last_updated":             time.time()
    }


def _sanitize_messages(msgs: list[dict]) -> list[dict]:
    """Remove orphaned tool messages AND orphaned assistant tool_calls without matching tool responses."""
    tool_response_ids = set()
    for m in msgs:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_response_ids.add(m["tool_call_id"])

    clean = []
    for m in msgs:
        if m.get("role") == "tool":
            has_parent = False
            for c in reversed(clean):
                if c.get("role") == "tool":
                    continue
                if c.get("role") == "assistant" and c.get("tool_calls"):
                    has_parent = True
                break
            if has_parent:
                clean.append(m)
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            calls = m["tool_calls"]
            all_have_responses = all(
                tc.get("id") in tool_response_ids for tc in calls
            )
            if all_have_responses:
                clean.append(m)
            elif m.get("content"):
                clean.append({"role": "assistant", "content": m["content"]})
        else:
            clean.append(m)
    return clean


def _extract_response(data: dict, model: str, fallbacks: list) -> dict:
    """Normalize a ChatCompletion response dict into our internal format."""
    choice = data["choices"][0]
    ai_message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    if ai_message.get("tool_calls"):
        finish_reason = "tool_calls"
    usage = data.get("usage", {})
    usage["model_used"] = model
    usage["fallbacks"] = fallbacks
    return {"finish_reason": finish_reason, "message": ai_message, "usage": usage}


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI-compatible chat helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _call_openai_compatible_chat(
    *,
    label: str,
    system: str,
    messages: list[dict],
    model: str,
    base_url: str,
    headers: dict,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    max_tokens_field: str = "max_tokens",
    max_attempts: int = 5,
) -> dict:
    """Call an OpenAI-compatible chat endpoint with retries and streaming."""
    import aiohttp

    effective_max = max_tokens or CONFIG["main_max_tokens"]
    use_stream = effective_max > 4096

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + _sanitize_messages(messages),
        "temperature": temperature or CONFIG["main_temperature"],
        max_tokens_field: effective_max,
    }
    if use_stream:
        payload["stream"] = True
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{base_url}/chat/completions"

    for attempt in range(max_attempts):
        try:
            async with _api_semaphore:
                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=120 if use_stream else 60)
                    async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                        if resp.status == 429:
                            if attempt >= max_attempts - 1:
                                raise RuntimeError(f"{label} 429: rate limited after {max_attempts} attempts")
                            retry_after = float(resp.headers.get("Retry-After", 2))
                            backoff = min(retry_after * (1.5 ** attempt), 30)
                            log.warning("%s 429 (attempt %d/%d), retrying after %.1fs", label, attempt + 1, max_attempts, backoff)
                            await asyncio.sleep(backoff)
                            continue
                        if resp.status in (500, 502, 503):
                            # On the final attempt, raise immediately instead of
                            # sleeping then giving up — lets call_ai fail over to
                            # the next provider fast when this one is down.
                            if attempt >= max_attempts - 1:
                                raise RuntimeError(f"{label} {resp.status}: upstream error after {max_attempts} attempts")
                            backoff = 2 * (2 ** attempt)
                            log.warning("%s %d (attempt %d/%d), retrying after %.1fs", label, resp.status, attempt + 1, max_attempts, backoff)
                            await asyncio.sleep(backoff)
                            continue
                        if resp.status == 400:
                            err_body = await resp.text()
                            log.warning("%s 400: %s", label, err_body[:500])
                            payload["messages"] = (
                                [{"role": "system", "content": system}]
                                + _sanitize_messages(messages[-3:] if len(messages) > 3 else messages)
                            )
                            if tools:
                                payload.pop("tools", None)
                                payload.pop("tool_choice", None)
                            continue
                        if resp.status == 413:
                            log.warning("%s 413 payload too large", label)
                            payload["messages"] = (
                                [{"role": "system", "content": system}]
                                + _sanitize_messages(messages[-3:] if len(messages) > 3 else messages)
                            )
                            if tools:
                                payload.pop("tools", None)
                                payload.pop("tool_choice", None)
                            continue
                        if resp.status != 200:
                            text = await resp.text()
                            raise RuntimeError(f"{label} error {resp.status}: {text[:300]}")

                        if not use_stream:
                            data = await resp.json()
                            return _extract_response(data, model, [])

                        content_parts = []
                        tool_calls_map: dict[int, dict] = {}
                        finish_reason = "stop"
                        usage = {}

                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices", [])
                            if not choices:
                                if chunk.get("usage"):
                                    usage = chunk["usage"]
                                continue
                            delta = choices[0].get("delta", {})
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                            if delta.get("content"):
                                content_parts.append(delta["content"])

                            for tc in delta.get("tool_calls", []):
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        "id": tc.get("id", ""),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_map[idx]
                                if tc.get("id"):
                                    entry["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    entry["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    entry["function"]["arguments"] += fn["arguments"]

                            if chunk.get("usage"):
                                usage = chunk["usage"]

                        message: dict = {"role": "assistant"}
                        full_content = "".join(content_parts)
                        if full_content:
                            message["content"] = full_content
                        if tool_calls_map:
                            message["tool_calls"] = [tool_calls_map[k] for k in sorted(tool_calls_map)]
                            finish_reason = "tool_calls"

                        usage["model_used"] = model
                        usage["fallbacks"] = []
                        return {"finish_reason": finish_reason, "message": message, "usage": usage}
        except RuntimeError:
            raise
        except Exception as e:
            if attempt >= max_attempts - 1:
                raise RuntimeError(f"{label} failed after {max_attempts} attempts: {e}")
            backoff = 2 * (2 ** attempt)
            log.warning("%s exception (attempt %d/%d): %s — retrying in %.1fs", label, attempt + 1, max_attempts, e, backoff)
            await asyncio.sleep(backoff)

    raise RuntimeError(f"{label}: max retries exceeded")


async def _moonshot_call(system: str, messages: list[dict],
                         tools: list[dict] | None = None,
                         temperature: float | None = None,
                         max_tokens: int | None = None,
                         max_attempts: int = 5) -> dict:
    """Call Kimi K2.5 via OpenAI-compatible API."""
    if not MOONSHOT_API_KEY:
        raise RuntimeError("MOONSHOT_API_KEY not set")

    return await _call_openai_compatible_chat(
        label="Moonshot",
        system=system,
        messages=messages,
        model=MOONSHOT_MODEL,
        base_url=MOONSHOT_BASE_URL,
        headers={
            "Authorization": f"Bearer {MOONSHOT_API_KEY}",
            "Content-Type": "application/json",
        },
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        max_tokens_field="max_tokens",
        max_attempts=max_attempts,
    )


async def _mimo_call(system: str, messages: list[dict],
                     tools: list[dict] | None = None,
                     temperature: float | None = None,
                     max_tokens: int | None = None,
                     max_attempts: int = 5) -> dict:
    """Call Xiaomi MiMo via its OpenAI-compatible Token Plan endpoint."""
    if not MIMO_API_KEY:
        raise RuntimeError("MIMO_API_KEY not set")

    return await _call_openai_compatible_chat(
        label="MiMo",
        system=system,
        messages=messages,
        model=MIMO_MODEL,
        base_url=MIMO_BASE_URL,
        headers={
            "api-key": MIMO_API_KEY,
            "Content-Type": "application/json",
        },
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        max_tokens_field="max_completion_tokens",
        max_attempts=max_attempts,
    )


async def _gateway_call(system: str, messages: list[dict],
                        tools: list[dict] | None = None,
                        temperature: float | None = None,
                        max_tokens: int | None = None,
                        max_attempts: int = 5) -> dict:
    """Call the 9arm gateway (OpenAI-compatible, Bearer auth)."""
    if not GATEWAY_API_KEY:
        raise RuntimeError("GATEWAY_API_KEY not set")

    return await _call_openai_compatible_chat(
        label="Gateway",
        system=system,
        messages=messages,
        model=GATEWAY_MODEL,
        base_url=GATEWAY_BASE_URL,
        headers={
            "Authorization": f"Bearer {GATEWAY_API_KEY}",
            "Content-Type": "application/json",
        },
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        max_tokens_field="max_tokens",
        max_attempts=max_attempts,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GROQ PATH — fallback chain (legacy)
# ═══════════════════════════════════════════════════════════════════════════════

async def _groq_call(system: str, messages: list[dict],
                     tools: list[dict] | None = None) -> dict:
    """Groq fallback chain with DeepSeek last resort."""
    import aiohttp

    last_err = None
    fallbacks = []

    for model in GROQ_MODELS:
        limit_info = _rate_limits.get(model, {})
        rem = limit_info.get("remaining_tokens_day") or limit_info.get("remaining_tokens_minute")
        if rem is not None and str(rem) == "0":
            fallbacks.append(f"{model.split('/')[-1]}: Limited")
            continue

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + _sanitize_messages(messages),
            "temperature": CONFIG["main_temperature"],
            "max_tokens": CONFIG["main_max_tokens"],
            "frequency_penalty": CONFIG.get("frequency_penalty", 0.0),
            "presence_penalty": CONFIG.get("presence_penalty", 0.0),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(GROQ_BASE_URL, headers=GROQ_HEADERS, json=payload) as resp:
                    _parse_groq_headers(model, resp.headers)

                    if resp.status == 429:
                        fallbacks.append(f"{model.split('/')[-1]}: Busy")
                        continue
                    if resp.status == 400:
                        err_body = await resp.text()
                        log.warning("400 from %s: %s", model, err_body[:300])
                        slim_payload = {
                            "model": model,
                            "messages": [{'role': 'system', 'content': system}] + _sanitize_messages(messages[-3:] if len(messages) > 3 else messages),
                            "temperature": CONFIG["main_temperature"],
                            "max_tokens": CONFIG["main_max_tokens"],
                        }
                        try:
                            async with aiohttp.ClientSession() as s2:
                                async with s2.post(GROQ_BASE_URL, headers=GROQ_HEADERS, json=slim_payload) as r2:
                                    _parse_groq_headers(model, r2.headers)
                                    if r2.status == 200:
                                        data = await r2.json()
                                        return _extract_response(data, model, fallbacks + [f"{model.split('/')[-1]}: retried-slim"])
                        except Exception:
                            pass
                        fallbacks.append(f"{model.split('/')[-1]}: Bad request")
                        if len(messages) > 4:
                            messages = messages[-3:]
                        continue
                    if resp.status == 413:
                        fallbacks.append(f"{model.split('/')[-1]}: Too large")
                        if len(messages) > 4:
                            messages = messages[-3:]
                        elif len(messages) > 1:
                            messages = messages[-1:]
                        continue
                    if resp.status in (502, 503):
                        fallbacks.append(f"{model.split('/')[-1]}: Overloaded")
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        fallbacks.append(f"{model.split('/')[-1]}: Error {resp.status}")
                        last_err = RuntimeError(f"Groq error {resp.status}: {text}")
                        continue

                    data = await resp.json()

            return _extract_response(data, model, fallbacks)

        except Exception as e:
            fallbacks.append(f"{model.split('/')[-1]}: Failed")
            last_err = e
            continue

    # ── Last resort: DeepSeek (paid API) ──────────────────────────────────
    if DEEPSEEK_API_KEY:
        try:
            ds_headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
            ds_msgs = _sanitize_messages(messages[-3:] if len(messages) > 3 else messages)
            ds_payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "system", "content": system}] + ds_msgs,
                "temperature": CONFIG["main_temperature"],
                "max_tokens": 4096,
            }
            if tools:
                ds_payload["tools"] = tools
                ds_payload["tool_choice"] = "auto"
            async with aiohttp.ClientSession() as session:
                async with session.post(DEEPSEEK_BASE_URL, headers=ds_headers, json=ds_payload,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return _extract_response(data, DEEPSEEK_MODEL, fallbacks + ["groq-exhausted"])
                    else:
                        err_body = await resp.text()
                        log.warning("DeepSeek %s: %s", resp.status, err_body[:300])
                        if tools:
                            ds_payload.pop("tools", None)
                            ds_payload.pop("tool_choice", None)
                            async with aiohttp.ClientSession() as s2:
                                async with s2.post(DEEPSEEK_BASE_URL, headers=ds_headers, json=ds_payload,
                                                   timeout=aiohttp.ClientTimeout(total=30)) as r2:
                                    if r2.status == 200:
                                        data = await r2.json()
                                        return _extract_response(data, DEEPSEEK_MODEL, fallbacks + ["groq-exhausted", "deepseek-slim"])
                        fallbacks.append(f"deepseek: {resp.status}")
        except Exception as e:
            fallbacks.append(f"deepseek: {e}")

    fb_str = ", ".join(fallbacks) if fallbacks else "unknown"
    return {
        "finish_reason": "error",
        "message": {"role": "assistant", "content": f"all models are cooked rn. ({fb_str})"},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "model_used": "none", "fallbacks": fallbacks},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — dispatches to Moonshot or Groq based on toggle
# ═══════════════════════════════════════════════════════════════════════════════

async def call_ai(system: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> dict:
    """Primary chat entrypoint with provider failover.

    Tries each configured provider in priority order; if one is down (e.g. the
    9arm gateway returning 502s) it fails over to the next so the bot keeps
    answering. Non-final providers get a short retry budget so failover is fast;
    the last provider gets the full budget since there's nothing after it.
    """
    chain = []
    if USE_GATEWAY_API or GATEWAY_API_KEY:
        chain.append(("Gateway", _gateway_call))
    if USE_MIMO_API or MIMO_API_KEY:
        chain.append(("MiMo", _mimo_call))
    if MOONSHOT_API_KEY:
        chain.append(("Moonshot", _moonshot_call))

    last_err = None
    for i, (name, fn) in enumerate(chain):
        is_last = (i == len(chain) - 1)
        attempts = 5 if is_last else 2  # primaries fail over fast; last one tries hard
        try:
            result = await fn(system, messages, tools, max_tokens=max_tokens, max_attempts=attempts)
            if i > 0:
                log.info("call_ai: primary unavailable — served by fallback provider %s", name)
            return result
        except Exception as e:
            last_err = e
            log.warning("call_ai: provider %s failed: %s%s",
                        name, e, " — trying next" if not is_last else "")

    # Legacy Groq fallback chain (only when explicitly enabled).
    if USE_GROQ_API:
        try:
            return await _groq_call(system, messages, tools)
        except Exception as e:
            last_err = e
            log.warning("call_ai: Groq fallback failed: %s", e)

    log.error("call_ai: ALL providers failed (%s)", last_err)
    return {
        "finish_reason": "error",
        "message": {"role": "assistant",
                    "content": "all my model providers are down right now — give it a sec and try again."},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "model_used": "none",
                  "fallbacks": [str(last_err)[:200] if last_err else "no providers configured"]},
    }


async def _groq_vision(system: str, image_url: str, prompt: str, *, max_tokens: int, temperature: float) -> dict:
    """Vision via Groq's dedicated multimodal model (llama-4-scout)."""
    import aiohttp
    model = CONFIG["vision_model"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(GROQ_BASE_URL, headers=GROQ_HEADERS, json=payload,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            _parse_groq_headers(model, resp.headers)
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Groq vision {resp.status}: {text[:200]}")
            data = await resp.json()
            usage = data.get("usage", {})
            usage["model_used"] = model
            return {"message": data["choices"][0]["message"], "usage": usage}


async def _vision_chat(system: str, image_url: str, prompt: str, *,
                       max_tokens: int, temperature: float | None = None) -> dict:
    """Shared vision path: try the gateway (qwen3.6 is multimodal — same provider
    as chat) first, then fall back to Groq's llama-4-scout. MiMo is intentionally
    NOT used for vision — its token-plan quota is exhausted (HTTP 429)."""
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }]
    temp = temperature if temperature is not None else CONFIG["vision_temperature"]
    last_err = None

    if USE_GATEWAY_API or GATEWAY_API_KEY:
        try:
            return await _gateway_call(system, messages, temperature=temp,
                                       max_tokens=max_tokens, max_attempts=2)
        except Exception as e:
            last_err = e
            log.warning("Vision via gateway failed: %s — falling back to Groq", e)

    if GROQ_API_KEY:
        try:
            return await _groq_vision(system, image_url, prompt, max_tokens=max_tokens, temperature=temp)
        except Exception as e:
            last_err = e
            log.warning("Vision via Groq failed: %s", e)

    return {"message": {"content": f"Error analyzing image: {last_err or 'no vision provider configured'}"},
            "usage": {"model_used": "none"}}


async def call_fast_vision(image_url: str, prompt: str) -> dict:
    """Fast + cheap vision for gaming/fastimg — coords only, no prose."""
    return await _vision_chat(
        "You analyze game screenshots. Output ONLY clickable elements with (x,y) coordinates. Be extremely concise. No prose.",
        image_url, prompt,
        max_tokens=CONFIG["fast_vision_max_tok"],
        temperature=CONFIG.get("fast_vision_temp"),
    )


async def call_vision(image_url: str, prompt: str) -> dict:
    """Vision analysis — gateway (qwen3.6, multimodal) primary, Groq fallback."""
    return await _vision_chat(
        "You analyze images accurately and concisely.",
        image_url, prompt,
        max_tokens=CONFIG["vision_max_tokens"],
        temperature=CONFIG["vision_temperature"],
    )
