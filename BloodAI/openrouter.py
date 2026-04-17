import os
import time
import logging
from dotenv import load_dotenv
from config import CONFIG

log = logging.getLogger("blood.openrouter")

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = CONFIG.get("deepseek_model", "deepseek-chat")

MODELS = CONFIG["models"]

# ── Rate limit tracker ────────────────────────────────────────────────────────
# Groq returns these headers on every response. We store the last known values
# per model so !debug limits can show a status bar without an extra API call.
_rate_limits: dict[str, dict] = {}

def get_rate_limits() -> dict[str, dict]:
    """Return last known rate limit state for all models."""
    return dict(_rate_limits)

def _parse_headers(model: str, headers):
    """Extract and store Groq rate limit headers with robust fallback."""
    # Convert all headers to lowercase for reliable matching
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
    # Pass 1: collect valid tool_call_ids that have a tool response
    tool_response_ids = set()
    for m in msgs:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_response_ids.add(m["tool_call_id"])

    clean = []
    for m in msgs:
        if m.get("role") == "tool":
            # Keep tool message only if it traces back to an assistant with tool_calls
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
            # Check if ALL tool_calls in this message have matching tool responses
            calls = m["tool_calls"]
            all_have_responses = all(
                tc.get("id") in tool_response_ids for tc in calls
            )
            if all_have_responses:
                clean.append(m)
            elif m.get("content"):
                # Keep the text content but strip the tool_calls
                clean.append({"role": "assistant", "content": m["content"]})
            # else: drop entirely — orphaned tool_calls with no text
        else:
            clean.append(m)
    return clean


async def call_ai(system: str, messages: list[dict], tools: list[dict] | None = None) -> dict:
    import aiohttp

    last_err = None
    fallbacks = []
    
    for model in MODELS:
        # ── Smart Selection ───────────────────────────────────────────────────
        # Skip if we already know (from recent pulse) that this model is empty.
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
                async with session.post(BASE_URL, headers=HEADERS, json=payload) as resp:
                    _parse_headers(model, resp.headers)
                    
                    if resp.status == 429:
                        fallbacks.append(f"{model.split('/')[-1]}: Busy")
                        continue
                    if resp.status == 400:
                        err_body = await resp.text()
                        log.warning("400 from %s: %s", model, err_body[:300])
                        # Retry same model with stripped payload
                        slim_payload = {
                            "model": model,
                            "messages": [{'role': 'system', 'content': system}] + _sanitize_messages(messages[-3:] if len(messages) > 3 else messages),
                            "temperature": CONFIG["main_temperature"],
                            "max_tokens": CONFIG["main_max_tokens"],
                        }
                        try:
                            async with aiohttp.ClientSession() as s2:
                                async with s2.post(BASE_URL, headers=HEADERS, json=slim_payload) as r2:
                                    _parse_headers(model, r2.headers)
                                    if r2.status == 200:
                                        data = await r2.json()
                                        choice = data["choices"][0]
                                        ai_message = choice.get("message", {})
                                        finish_reason = choice.get("finish_reason", "stop")
                                        usage = data.get("usage", {})
                                        usage["model_used"] = model
                                        usage["fallbacks"] = fallbacks + [f"{model.split('/')[-1]}: retried-slim"]
                                        return {"finish_reason": finish_reason, "message": ai_message, "usage": usage}
                        except Exception:
                            pass
                        fallbacks.append(f"{model.split('/')[-1]}: Bad request")
                        if len(messages) > 4:
                            messages = messages[-3:]
                        continue
                    if resp.status == 413:
                        # Request too large — trim history for remaining models
                        fallbacks.append(f"{model.split('/')[-1]}: Too large")
                        if len(messages) > 4:
                            messages = messages[-3:]
                        elif len(messages) > 1:
                            messages = messages[-1:]
                        continue
                    if resp.status == 503 or resp.status == 502:
                        fallbacks.append(f"{model.split('/')[-1]}: Overloaded")
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        fallbacks.append(f"{model.split('/')[-1]}: Error {resp.status}")
                        last_err = RuntimeError(f"Groq error {resp.status}: {text}")
                        continue
                    
                    data = await resp.json()

            choice = data["choices"][0]
            ai_message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")
            if ai_message.get("tool_calls"):
                finish_reason = "tool_calls"
            usage = data.get("usage", {})
            usage["model_used"] = model
            usage["fallbacks"] = fallbacks
            return {"finish_reason": finish_reason, "message": ai_message, "usage": usage}

        except Exception as e:
            fallbacks.append(f"{model.split('/')[-1]}: Failed")
            last_err = e
            continue

    # ── Last resort: DeepSeek (paid API) ──────────────────────────────────
    if DEEPSEEK_API_KEY:
        try:
            import aiohttp
            ds_headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
            # Use trimmed messages, strip orphaned tool messages
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
                        choice = data["choices"][0]
                        ai_message = choice.get("message", {})
                        finish_reason = choice.get("finish_reason", "stop")
                        if ai_message.get("tool_calls"):
                            finish_reason = "tool_calls"
                        usage = data.get("usage", {})
                        usage["model_used"] = DEEPSEEK_MODEL
                        usage["fallbacks"] = fallbacks + ["groq-exhausted"]
                        return {"finish_reason": finish_reason, "message": ai_message, "usage": usage}
                    else:
                        err_body = await resp.text()
                        log.warning("DeepSeek %s: %s", resp.status, err_body[:300])
                        # Retry without tools
                        if tools:
                            ds_payload.pop("tools", None)
                            ds_payload.pop("tool_choice", None)
                            async with aiohttp.ClientSession() as s2:
                                async with s2.post(DEEPSEEK_BASE_URL, headers=ds_headers, json=ds_payload,
                                                   timeout=aiohttp.ClientTimeout(total=30)) as r2:
                                    if r2.status == 200:
                                        data = await r2.json()
                                        choice = data["choices"][0]
                                        ai_message = choice.get("message", {})
                                        finish_reason = choice.get("finish_reason", "stop")
                                        usage = data.get("usage", {})
                                        usage["model_used"] = DEEPSEEK_MODEL
                                        usage["fallbacks"] = fallbacks + ["groq-exhausted", "deepseek-slim"]
                                        return {"finish_reason": finish_reason, "message": ai_message, "usage": usage}
                        fallbacks.append(f"deepseek: {resp.status}")
        except Exception as e:
            fallbacks.append(f"deepseek: {e}")

    # Graceful fallback if everything including DeepSeek fails
    fb_str = ", ".join(fallbacks) if fallbacks else "unknown"
    return {
        "finish_reason": "error",
        "message": {"role": "assistant", "content": f"all models are cooked rn. ({fb_str})"},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "model_used": "none", "fallbacks": fallbacks},
    }

async def call_vision(image_url: str, prompt: str) -> str:
    """Specialized endpoint call for Vision capabilities using Llama 3.2 11B Vision."""
    import aiohttp
    
    model = CONFIG["vision_model"]
    
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
            }
        ],
        "temperature": CONFIG["vision_temperature"],
        "max_tokens": CONFIG["vision_max_tokens"],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(BASE_URL, headers=HEADERS, json=payload) as resp:
                _parse_headers(model, resp.headers)
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Vision API Error {resp.status}: {text}")
                data = await resp.json()
                
                usage = data.get("usage", {})
                usage["model_used"] = model
                ai_message = data["choices"][0]["message"]
                
                return {"message": ai_message, "usage": usage}
    except Exception as e:
        return {"message": {"content": f"Error analyzing image: {str(e)}"}, "usage": {}}

async def call_ai_fast(system: str, prompt: str) -> str:
    """Minimalist, cheap, and fast AI call for Pass 2 (meme checking). Uses Scout model."""
    import aiohttp
    model = CONFIG["fast_model"]
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": CONFIG["fast_temperature"],
        "max_tokens": CONFIG["fast_max_tokens"],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(BASE_URL, headers=HEADERS, json=payload, timeout=aiohttp.ClientTimeout(total=CONFIG["fast_timeout_sec"])) as resp:
                _parse_headers(model, resp.headers)
                if resp.status != 200:
                    return "NONE"
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return "NONE"