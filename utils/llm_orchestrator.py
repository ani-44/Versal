import json
import logging
import os
import time
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()

_GROQ_FAIL_UNTIL = 0.0


def _now() -> float:
    return time.time()


def _safe_json(resp: requests.Response) -> Dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _with_backoff_sleep(attempt: int) -> None:
    time.sleep(min(2.0, 0.4 * (2 ** max(0, attempt - 1))))


def _call_groq(messages: List[Dict], max_tokens: int, temperature: float, timeout: int) -> str:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    global _GROQ_FAIL_UNTIL
    if _now() < _GROQ_FAIL_UNTIL:
        raise RuntimeError("Groq circuit open")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.7,
        "stream": False,
    }

    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"Groq transient status {resp.status_code}")
                _with_backoff_sleep(attempt)
                continue
            resp.raise_for_status()
            data = _safe_json(resp)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not content:
                raise RuntimeError("Groq returned empty response")
            return content
        except requests.Timeout as e:
            last_err = e
            _with_backoff_sleep(attempt)
        except Exception as e:
            last_err = e
            if attempt < 3:
                _with_backoff_sleep(attempt)

    _GROQ_FAIL_UNTIL = _now() + 45
    raise RuntimeError(f"Groq failed after retries: {last_err}")


def _call_ollama(messages: List[Dict], max_tokens: int, temperature: float, timeout: int) -> str:
    if not OLLAMA_BASE_URL:
        raise RuntimeError("OLLAMA_BASE_URL is not configured")
    if not OLLAMA_MODEL:
        raise RuntimeError("OLLAMA_MODEL is not configured")

    prompt_parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        prompt_parts.append(f"{role.upper()}: {content}")
    prompt = "\n\n".join(prompt_parts) + "\n\nASSISTANT:"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

    endpoint = f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate"
    last_err = None
    for attempt in range(1, 3):
        try:
            resp = requests.post(endpoint, json=payload, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"Ollama transient status {resp.status_code}")
                _with_backoff_sleep(attempt)
                continue
            resp.raise_for_status()
            data = _safe_json(resp)
            content = (data.get("response") or "").strip()
            if not content:
                raise RuntimeError("Ollama returned empty response")
            return content
        except Exception as e:
            last_err = e
            if attempt < 2:
                _with_backoff_sleep(attempt)

    raise RuntimeError(f"Ollama failed after retries: {last_err}")


def call_ai_with_fallback(messages: List[Dict], max_tokens: int = 512, temperature: float = 0.2, timeout: int = 35) -> str:
    try:
        return _call_groq(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
    except Exception as groq_err:
        # Use Ollama only when it is explicitly configured.
        # This avoids secondary failures when users run Groq-only setup.
        if OLLAMA_BASE_URL and OLLAMA_MODEL:
            logger.warning("Groq failed, switching to Ollama fallback: %s", groq_err)
            return _call_ollama(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)

        # No fallback configured: surface Groq error clearly to caller.
        raise RuntimeError(f"Groq request failed and Ollama fallback is not configured: {groq_err}")


def groq_health_check() -> Dict:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return {"ok": False, "configured": False, "provider": "groq", "error": "GROQ_API_KEY is not configured."}
    try:
        _call_groq([{"role": "user", "content": "ping"}], max_tokens=8, temperature=0.0, timeout=15)
        return {"ok": True, "configured": True, "provider": "groq", "model": GROQ_MODEL}
    except Exception as e:
        return {"ok": False, "configured": True, "provider": "groq", "model": GROQ_MODEL, "error": str(e)}


def llm_health() -> Dict:
    groq = groq_health_check()
    ollama_ok = False
    ollama_error = None
    try:
        _call_ollama([{"role": "user", "content": "ping"}], max_tokens=8, temperature=0.0, timeout=15)
        ollama_ok = True
    except Exception as e:
        ollama_error = str(e)

    return {
        "primary": groq,
        "fallback": {
            "provider": "ollama",
            "model": OLLAMA_MODEL,
            "ok": ollama_ok,
            "error": ollama_error,
        },
    }
