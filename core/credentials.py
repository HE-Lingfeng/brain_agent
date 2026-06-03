from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_SECRET_PATH = Path("~/secrets/worldquant-brain.json").expanduser()


LLM_PROVIDER_DEFAULTS = {
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2.5"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"},
}

_DEFAULT_LLM_BASE_URLS = {item["base_url"] for item in LLM_PROVIDER_DEFAULTS.values()}
_DEFAULT_LLM_MODELS = {item["model"] for item in LLM_PROVIDER_DEFAULTS.values()}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    lower = text.lower()
    if lower in {"xxx", "xxxx", "your_email", "your_password", "your-api-key", "sk-xxxxxxx"}:
        return ""
    if lower.startswith("your_") or (lower.startswith("<") and lower.endswith(">")):
        return ""
    return text


def _read_secret(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Credential file must contain a JSON object: {path}")
    return data


def _put(target: dict[str, str], key: str, value: Any) -> None:
    cleaned = _clean(value)
    if cleaned:
        target[key] = cleaned


def load_credentials(
    *,
    secret_path: str | Path | None = None,
    require_brain: bool = True,
    require_llm: bool = False,
) -> dict[str, str]:
    """Load credentials with precedence: defaults < secret file < environment."""
    provider = _clean(os.environ.get("BRAIN_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER")) or "moonshot"
    defaults = LLM_PROVIDER_DEFAULTS.get(provider.lower(), LLM_PROVIDER_DEFAULTS["moonshot"])
    result: dict[str, str] = {
        "llm_provider": provider.lower(),
        "llm_base_url": defaults["base_url"],
        "llm_model": defaults["model"],
    }
    secret = Path(secret_path or os.environ.get("WQ_BRAIN_SECRET_FILE") or DEFAULT_SECRET_PATH).expanduser()
    data = _read_secret(secret)

    brain = data.get("brain") if isinstance(data.get("brain"), dict) else {}
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    _put(result, "llm_provider", llm.get("provider") or llm.get("llm_provider"))
    if result.get("llm_provider"):
        provider_defaults = LLM_PROVIDER_DEFAULTS.get(result["llm_provider"].lower())
        if provider_defaults:
            result["llm_base_url"] = provider_defaults["base_url"]
            result["llm_model"] = provider_defaults["model"]
    _put(result, "brain_email", brain.get("email") or brain.get("username"))
    _put(result, "brain_password", brain.get("password"))
    _put(result, "llm_api_key", llm.get("api_key") or llm.get("llm_api_key") or llm.get("moonshot_api_key"))
    _put(result, "llm_base_url", llm.get("base_url") or llm.get("llm_base_url") or llm.get("moonshot_base_url"))
    _put(result, "llm_model", llm.get("model") or llm.get("llm_model") or llm.get("moonshot_model"))

    _put(result, "brain_email", os.environ.get("BRAIN_EMAIL") or os.environ.get("BRAIN_USERNAME"))
    _put(result, "brain_password", os.environ.get("BRAIN_PASSWORD"))
    _put(result, "llm_provider", os.environ.get("BRAIN_LLM_PROVIDER") or os.environ.get("LLM_PROVIDER"))
    _put(
        result,
        "llm_api_key",
        os.environ.get("LLM_API_KEY")
        or os.environ.get("MOONSHOT_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY"),
    )
    _put(result, "llm_base_url", os.environ.get("LLM_BASE_URL") or os.environ.get("MOONSHOT_BASE_URL"))
    _put(result, "llm_model", os.environ.get("LLM_MODEL") or os.environ.get("MOONSHOT_MODEL"))

    provider_defaults = LLM_PROVIDER_DEFAULTS.get(result.get("llm_provider", "").lower())
    if provider_defaults:
        if not result.get("llm_base_url") or result.get("llm_base_url") in _DEFAULT_LLM_BASE_URLS:
            result["llm_base_url"] = provider_defaults["base_url"]
        if not result.get("llm_model") or result.get("llm_model") in _DEFAULT_LLM_MODELS:
            result["llm_model"] = provider_defaults["model"]

    # Backward-compatible aliases for legacy scripts that still speak in Moonshot names.
    if result.get("llm_api_key"):
        result["moonshot_api_key"] = result["llm_api_key"]
    if result.get("llm_base_url"):
        result["moonshot_base_url"] = result["llm_base_url"]
    if result.get("llm_model"):
        result["moonshot_model"] = result["llm_model"]

    missing: list[str] = []
    if require_brain and not result.get("brain_email"):
        missing.append("BRAIN_EMAIL")
    if require_brain and not result.get("brain_password"):
        missing.append("BRAIN_PASSWORD")
    if require_llm and not result.get("llm_api_key"):
        missing.append("LLM_API_KEY")
    if missing:
        raise ValueError("Missing required credentials: " + ", ".join(missing) + f". Set env vars or create {secret}.")
    return result
