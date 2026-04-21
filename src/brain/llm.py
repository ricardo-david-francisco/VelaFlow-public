"""LLM integration with Gemini (primary, multi-model fallback) and Groq.

Fallback chain:
  1. Gemini Pro (best reasoning for task planning)
  2. Gemini Flash (fast, cost-efficient)
  3. Gemini Flash-Lite (cheapest, avoids quota exhaustion)
  4. Groq (external fallback)
  5. Raw text (no LLM available)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket

import requests

from brain.config import Settings
from brain.security.sanitization import sanitize_for_llm

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GOOGLE_AI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def polish_digest(settings: Settings, raw_text: str, system_prompt: str) -> str:
    """Send raw digest text to LLM for polishing.

    Fallback chain (Zero-Trust Proxy first if configured):
      0. LiteLLM proxy  (demo mode / LXC handover — no real keys in container)
      1. Gemini Pro      (direct, best reasoning)
      2. Gemini Flash    (fast, cost-efficient)
      3. Gemini Flash-Lite (cheapest)
      4. Groq            (external fallback)
      5. Raw text        (no LLM available)
    """
    # Sanitize user content before sending to any LLM
    sanitized_text = sanitize_for_llm(raw_text, context="polish_digest")

    # ── Zero-Trust Proxy (demo / LXC) ────────────────────────────────────────
    # When LITELLM_PROXY_URL is configured, route ALL calls through the proxy.
    # Real API keys stay on your VPS — only a budget-capped demo token lives here.
    if settings.litellm_proxy_url and settings.litellm_proxy_token:
        result = _call_proxy(settings, sanitized_text, system_prompt)
        if result:
            logger.info("LLM response from LiteLLM proxy.")
            return result
        logger.warning("Proxy call failed. %s",
                       "Falling back to direct keys." if not settings.demo_mode
                       else "demo_mode=True so no further fallback.")
        if settings.demo_mode:
            logger.warning("No LLM available in demo mode. Using raw digest text.")
            return raw_text

    # ── Direct Google AI Studio (Gemini) ─────────────────────────────────────
    if settings.google_ai_api_key:
        for model in _gemini_model_chain(settings):
            result = _call_google_ai(settings, sanitized_text, system_prompt, model=model)
            if result:
                logger.info("LLM response from %s.", model)
                return result
            logger.info("%s failed or rate-limited. Trying next model...", model)

    # ── External fallback: Groq ───────────────────────────────────────────────
    if settings.groq_api_key:
        result = _call_groq(settings, sanitized_text, system_prompt)
        if result:
            logger.info("LLM response from Groq (%s).", settings.groq_model)
            return result
        logger.info("Groq also failed.")

    logger.warning("No LLM available. Using raw digest text.")
    return raw_text


def call_llm(
    settings: Settings,
    user_text: str,
    system_prompt: str,
    *,
    prefer_quality: bool = False,
) -> str | None:
    """General-purpose LLM call with fallback chain.

    Args:
        prefer_quality: If True, always tries the best model first.
                        If False, starts from the fallback model to save quota.
    """
    # Sanitize user content before sending to any LLM
    user_text = sanitize_for_llm(user_text, context="call_llm")

    # ── Zero-Trust Proxy (demo / LXC) ────────────────────────────────────────
    if settings.litellm_proxy_url and settings.litellm_proxy_token:
        result = _call_proxy(settings, user_text, system_prompt)
        if result:
            return result
        if settings.demo_mode:
            return None  # no real keys in demo container

    # ── Direct Gemini ─────────────────────────────────────────────────────────
    if settings.google_ai_api_key:
        models = _gemini_model_chain(settings)
        if not prefer_quality and len(models) > 1:
            # For non-critical calls, skip the expensive model
            models = models[1:]
        for model in models:
            result = _call_google_ai(settings, user_text, system_prompt, model=model)
            if result:
                return result

    # ── Groq ──────────────────────────────────────────────────────────────────
    if settings.groq_api_key:
        return _call_groq(settings, user_text, system_prompt)

    return None


def _gemini_model_chain(settings: Settings) -> list[str]:
    """Return ordered list of Gemini models to try."""
    models = []
    if settings.google_ai_model:
        models.append(settings.google_ai_model)
    if settings.google_ai_fallback_model:
        models.append(settings.google_ai_fallback_model)
    if settings.google_ai_lite_model:
        models.append(settings.google_ai_lite_model)
    return models


def _call_proxy(settings: Settings, text: str, system_prompt: str) -> str | None:
    """Route an AI request through the LiteLLM proxy (OpenAI-compatible endpoint).

    This is the Zero-Trust demo model: the real API keys live only on your VPS.
    The LXC container holds only a budget-capped, time-limited demo token.

    Args:
        settings: Must have litellm_proxy_url and litellm_proxy_token set.
        text: User message / digest text.
        system_prompt: System instruction sent as a system message.

    Returns:
        Response text, or None on any error (triggers fallback chain).
    """
    proxy_url = settings.litellm_proxy_url.strip()
    if not proxy_url.startswith("https://"):
        logger.error(
            "Proxy URL '%s' does not use HTTPS — request blocked for security.",
            proxy_url[:64],
        )
        return None

    if _resolves_to_private_ip(proxy_url):
        logger.error("Proxy URL resolves to a private/reserved IP — blocked (SSRF guard).")
        return None

    url = proxy_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.litellm_proxy_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.litellm_proxy_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.4,
        "max_tokens": 2500,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=90)
        if resp.status_code == 429:
            logger.warning("Proxy rate-limited (429) — budget may be exhausted.")
            return None
        if resp.status_code == 401:
            logger.warning("Proxy auth failed (401) — demo token invalid or expired.")
            return None
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError):
        logger.exception("LiteLLM proxy call failed.")
        return None


def _call_groq(settings: Settings, text: str, system_prompt: str) -> str | None:
    """Call Groq API (OpenAI-compatible)."""
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.4,
        "max_tokens": 2500,
    }

    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        if resp.status_code == 429:
            logger.warning("Groq rate limited (429).")
            return None
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError):
        logger.exception("Groq API call failed.")
        return None


def _call_google_ai(
    settings: Settings,
    text: str,
    system_prompt: str,
    *,
    model: str | None = None,
) -> str | None:
    """Call Google AI Studio (Gemini).

    Handles 429 (rate limit), 503 (overloaded), and model-not-found gracefully
    so the fallback chain can try the next model.
    """
    model = model or settings.google_ai_model
    url = GOOGLE_AI_URL.format(model=model)
    params = {"key": settings.google_ai_api_key}
    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": text}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2500,
        },
    }

    try:
        resp = requests.post(url, params=params, json=payload, timeout=90)
        if resp.status_code in (429, 503):
            logger.warning("Gemini %s returned %d.", model, resp.status_code)
            return None
        if resp.status_code == 404:
            logger.warning("Gemini model %s not found (404).", model)
            return None
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "")
        return None
    except (requests.RequestException, KeyError, IndexError):
        logger.exception("Gemini %s call failed.", model)
        return None


# ── SSRF Guard ────────────────────────────────────────────────────────────────


def _resolves_to_private_ip(url: str) -> bool:
    """Return True if *url* resolves to a private/reserved/loopback address.

    Prevents SSRF by blocking proxy URLs that point at internal infrastructure
    (127.0.0.1, 10.x, 172.16-31.x, 169.254.x, ::1, fc00::, etc.).
    """
    try:
        from urllib.parse import urlparse

        hostname = urlparse(url).hostname
        if not hostname:
            return True  # malformed URL — block
        for info in socket.getaddrinfo(hostname, None, socket.AF_UNSPEC):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return True
    except (socket.gaierror, ValueError, OSError):
        return True  # DNS failure — block to be safe
    return False
