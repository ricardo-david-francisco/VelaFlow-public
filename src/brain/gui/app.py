"""VelaFlow Self-Service Customization GUI.

A minimal Streamlit app that lets a tenant view their tier and personalise
the fields their subscription allows, by calling the VelaFlow REST API.
This is the v1.0 self-service surface — a richer no-code, n8n-style
visual workflow editor is tracked for v1.2.

Design goals:
- Zero-cost to host: embeds no third-party SaaS; runs next to the API.
- Works for 1 or 1,000 tenants: stateless single-page app, all state lives
  in the API.
- Tier-honest: never shows a control the tenant cannot actually change;
  the API is the authoritative gate (the GUI is defence in depth).
- Copy-paste deployable: ``streamlit run src/brain/gui/app.py``.

Run locally:
    export VELAFLOW_API_URL=http://localhost:8765
    pip install 'velaflow[gui]'
    streamlit run src/brain/gui/app.py

Security:
- JWT is entered in a password field and kept only in Streamlit session
  state — it is NOT persisted to disk.
- The GUI never reads or displays the tenant's stored ``gemini_api_key``;
  only write-through is supported (platform owner cannot read it either,
  by design of the zero-trust BYO-key model).
- All requests use HTTPS if ``VELAFLOW_API_URL`` begins with ``https://``;
  plain HTTP is only accepted for ``localhost`` / ``127.0.0.1``.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st

API_URL = os.environ.get("VELAFLOW_API_URL", "http://localhost:8765").rstrip("/")

# ── Tier matrix (display-only; server is the authoritative gate) ─────
_TIER_MATRIX: dict[str, dict[str, Any]] = {
    "free": {
        "pipeline_runs_per_day": 3,
        "max_tasks": 100,
        "llm_calls_per_day": 5,
        "storage_mb": 50,
        "can_set_digest_time": False,
        "can_set_gemini_key": False,
        "can_enable_rag": False,
        "can_enable_local_llm": False,
    },
    "standard": {
        "pipeline_runs_per_day": 20,
        "max_tasks": 1000,
        "llm_calls_per_day": 50,
        "storage_mb": 500,
        "can_set_digest_time": True,
        "can_set_gemini_key": False,
        "can_enable_rag": False,
        "can_enable_local_llm": False,
    },
    "premium": {
        "pipeline_runs_per_day": 100,
        "max_tasks": 10000,
        "llm_calls_per_day": 200,
        "storage_mb": 5000,
        "can_set_digest_time": True,
        "can_set_gemini_key": True,
        "can_enable_rag": False,  # VIP-only — premium keeps NotebookLM export
        "can_enable_local_llm": True,
    },
    "vip": {
        "pipeline_runs_per_day": 999,
        "max_tasks": 50000,
        "llm_calls_per_day": 999,
        "storage_mb": 10000,
        "can_set_digest_time": True,
        "can_set_gemini_key": True,
        "can_enable_rag": True,
        "can_enable_local_llm": True,
    },
}


def _guard_url(url: str) -> str:
    """Reject plain http:// unless it points to localhost / 127.0.0.1."""
    p = urlparse(url)
    if p.scheme == "https":
        return url
    if p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}:
        return url
    raise RuntimeError(f"Refusing insecure non-localhost URL: {url}")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_me(token: str) -> dict[str, Any]:
    r = requests.get(f"{_guard_url(API_URL)}/api/v1/tenants/me", headers=_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()


def _patch_config(token: str, body: dict[str, Any]) -> requests.Response:
    return requests.patch(
        f"{_guard_url(API_URL)}/api/v1/tenants/me/config",
        headers=_headers(token),
        json=body,
        timeout=10,
    )


# ── UI ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="VelaFlow — My Flow", page_icon="🟦", layout="wide")
st.title("VelaFlow — Customise my flow")
st.caption(
    "Edit the fields allowed by your subscription tier. The API is the "
    "authoritative gate: disallowed fields return 403 with an upgrade link."
)

with st.sidebar:
    st.header("Session")
    st.text_input("API URL", value=API_URL, key="_api_url_display", disabled=True)
    token = st.text_input("JWT access token", type="password", key="jwt")
    if not token:
        st.info("Paste your JWT from `POST /api/v1/tenants/login`.")
        st.stop()

# ── Current tenant ──
try:
    me = _fetch_me(token)
except requests.HTTPError as e:
    st.error(f"Failed to load tenant: HTTP {e.response.status_code} {e.response.text}")
    st.stop()
except Exception as e:  # noqa: BLE001 — surface transport errors to the user
    st.error(f"Failed to reach API at {API_URL}: {e}")
    st.stop()

tier = (me.get("tier") or "free").lower()
matrix = _TIER_MATRIX.get(tier, _TIER_MATRIX["free"])

col1, col2, col3, col4 = st.columns(4)
col1.metric("Tier", tier.upper())
col2.metric("Pipeline runs/day", matrix["pipeline_runs_per_day"])
col3.metric("LLM calls/day", matrix["llm_calls_per_day"])
col4.metric("Storage quota", f"{matrix['storage_mb']} MB")

st.divider()

# ── Editable fields (gated by tier) ──
st.subheader("Preferences")
with st.form("cfg"):
    digest_time = st.text_input(
        "Daily digest delivery time (HH:MM, Europe/Lisbon)",
        value=me.get("config", {}).get("digest_time", "07:00"),
        disabled=not matrix["can_set_digest_time"],
        help=None if matrix["can_set_digest_time"] else "Upgrade to Standard+ to customise.",
    )
    gemini_key = st.text_input(
        "Bring-your-own Gemini API key (Premium/VIP only)",
        type="password",
        disabled=not matrix["can_set_gemini_key"],
        help=(
            "We store this AES-256-GCM encrypted with a per-tenant key. "
            "The platform operator cannot read it."
            if matrix["can_set_gemini_key"]
            else "Upgrade to Premium to use your own Gemini key."
        ),
    )
    rag_enabled = st.checkbox(
        "Enable personal RAG over my documents",
        value=bool(me.get("config", {}).get("rag_enabled", False)),
        disabled=not matrix["can_enable_rag"],
        help=None if matrix["can_enable_rag"] else "Upgrade to VIP to enable native RAG.",
    )
    local_llm = st.checkbox(
        "Prefer local LLM (Ollama) over cloud",
        value=bool(me.get("config", {}).get("use_local_llm", False)),
        disabled=not matrix["can_enable_local_llm"],
        help=None if matrix["can_enable_local_llm"] else "Upgrade to Premium to use local LLM.",
    )
    submitted = st.form_submit_button("Save changes")

if submitted:
    body: dict[str, Any] = {}
    if matrix["can_set_digest_time"]:
        body["digest_time"] = digest_time
    if matrix["can_set_gemini_key"] and gemini_key:
        body["gemini_api_key"] = gemini_key
    if matrix["can_enable_rag"]:
        body["rag_enabled"] = rag_enabled
    if matrix["can_enable_local_llm"]:
        body["use_local_llm"] = local_llm

    if not body:
        st.warning("Nothing to save.")
    else:
        resp = _patch_config(token, body)
        if resp.ok:
            st.success("Saved. New settings apply on the next pipeline run.")
            _fetch_me.clear()
        elif resp.status_code == 403:
            detail = resp.json().get("detail", "forbidden")
            st.error(
                f"403: {detail}. This field is not in your tier. "
                "See the upgrade path shown by the API."
            )
        else:
            st.error(f"API returned {resp.status_code}: {resp.text[:300]}")

st.divider()
st.caption(
    "The API enforces every tier gate — the controls above are a UX hint only. "
    "If the API returns 403, the GUI did the right thing by asking; the backend "
    "did the right thing by refusing."
)
