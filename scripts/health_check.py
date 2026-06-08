#!/usr/bin/env python3
"""Pre-flight health check for BRAIN platform and LLM API connectivity.
Usage:
  PYTHONPATH=.. python3 scripts/health_check.py
  PYTHONPATH=.. python3 scripts/health_check.py --brain-only
  PYTHONPATH=.. python3 scripts/health_check.py --llm-only
"""
from __future__ import annotations

import json
import os
import sys
import time

BRAIN_OK = False
LLM_OK = False


def _check_brain() -> dict:
    """Test actual BRAIN platform API connectivity by authenticating."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".agents", "skills", "brain-shared", "scripts"))
    try:
        import ace_lib
    except Exception as exc:
        return {"name": "brain_connectivity", "ok": False, "detail": f"import ace_lib failed: {exc}"}

    try:
        s = ace_lib.SingleSession()
        s.auth = ace_lib.get_credentials()
        t0 = time.monotonic()
        resp = s.post(ace_lib.brain_api_url + "/authentication")
        elapsed = time.monotonic() - t0
        if resp.status_code in (200, 201):
            token_info = resp.json().get("token", {})
            expiry = token_info.get("expiry", "unknown")
            return {
                "name": "brain_connectivity",
                "ok": True,
                "detail": f"authenticated (status={resp.status_code}, expiry={expiry}s, latency={elapsed:.1f}s)",
            }
        elif resp.status_code == 401:
            detail = resp.json()
            if resp.headers.get("WWW-Authenticate") == "persona":
                return {"name": "brain_connectivity", "ok": True, "detail": "server reachable; biometric auth needed"}
            return {"name": "brain_connectivity", "ok": False, "detail": f"auth failed (status=401): {detail}"}
        else:
            return {"name": "brain_connectivity", "ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"name": "brain_connectivity", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _check_llm() -> dict:
    """Test LLM API connectivity by sending a minimal chat request."""
    provider = os.environ.get("BRAIN_LLM_PROVIDER", os.environ.get("LLM_PROVIDER", "")).lower()
    api_key = os.environ.get("BRAIN_LLM_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
    base_url = os.environ.get("BRAIN_LLM_BASE_URL") or os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
    model = os.environ.get("BRAIN_LLM_MODEL") or os.environ.get("LLM_MODEL") or os.environ.get("ANTHROPIC_MODEL") or ""

    if not api_key:
        return {"name": "llm_connectivity", "ok": False, "detail": "no LLM API key found in environment"}

    try:
        import requests as _requests
    except Exception as exc:
        return {"name": "llm_connectivity", "ok": False, "detail": f"import requests failed: {exc}"}

    # Build request based on detected provider
    if base_url and "deepseek" in base_url:
        # DeepSeek Anthropic-compatible endpoint
        url = base_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model or "deepseek-v4-pro[1m]",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "ping"}],
        }
    elif base_url and "openai" in base_url:
        url = base_url.rstrip("/") + "/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        payload = {"model": model or "gpt-3.5-turbo", "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]}
    elif base_url:
        # Anthropic-compatible (generic)
        url = base_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model or "claude-sonnet-4-6",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "ping"}],
        }
    else:
        return {"name": "llm_connectivity", "ok": False, "detail": "no LLM base URL configured"}

    try:
        t0 = time.monotonic()
        resp = _requests.post(url, headers=headers, json=payload, timeout=15)
        elapsed = time.monotonic() - t0
        if resp.status_code in (200, 201):
            return {
                "name": "llm_connectivity",
                "ok": True,
                "detail": f"provider={provider or 'auto'} url={url} status={resp.status_code} latency={elapsed:.1f}s",
            }
        else:
            return {
                "name": "llm_connectivity",
                "ok": False,
                "detail": f"HTTP {resp.status_code} latency={elapsed:.1f}s: {resp.text[:200]}",
            }
    except Exception as exc:
        return {"name": "llm_connectivity", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    brain_only = "--brain-only" in sys.argv
    llm_only = "--llm-only" in sys.argv

    checks = []
    if not llm_only:
        checks.append(_check_brain())
    if not brain_only:
        checks.append(_check_llm())

    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
