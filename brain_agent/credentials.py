from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_SECRET_PATH = Path("~/secrets/worldquant-brain.json").expanduser()


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
    result: dict[str, str] = {
        "moonshot_base_url": "https://api.moonshot.cn/v1",
        "moonshot_model": "kimi-k2.5",
    }
    secret = Path(secret_path or os.environ.get("WQ_BRAIN_SECRET_FILE") or DEFAULT_SECRET_PATH).expanduser()
    data = _read_secret(secret)

    brain = data.get("brain") if isinstance(data.get("brain"), dict) else {}
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    _put(result, "brain_email", brain.get("email") or brain.get("username"))
    _put(result, "brain_password", brain.get("password"))
    _put(result, "moonshot_api_key", llm.get("moonshot_api_key") or llm.get("api_key"))
    _put(result, "moonshot_base_url", llm.get("moonshot_base_url") or llm.get("base_url"))
    _put(result, "moonshot_model", llm.get("moonshot_model") or llm.get("model"))

    _put(result, "brain_email", os.environ.get("BRAIN_EMAIL") or os.environ.get("BRAIN_USERNAME"))
    _put(result, "brain_password", os.environ.get("BRAIN_PASSWORD"))
    _put(result, "moonshot_api_key", os.environ.get("MOONSHOT_API_KEY"))
    _put(result, "moonshot_base_url", os.environ.get("MOONSHOT_BASE_URL"))
    _put(result, "moonshot_model", os.environ.get("MOONSHOT_MODEL"))

    missing: list[str] = []
    if require_brain and not result.get("brain_email"):
        missing.append("BRAIN_EMAIL")
    if require_brain and not result.get("brain_password"):
        missing.append("BRAIN_PASSWORD")
    if require_llm and not result.get("moonshot_api_key"):
        missing.append("MOONSHOT_API_KEY")
    if missing:
        raise ValueError("Missing required credentials: " + ", ".join(missing) + f". Set env vars or create {secret}.")
    return result
