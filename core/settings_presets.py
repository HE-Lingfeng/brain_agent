from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SETTING_KEYS = (
    "dataset",
    "region",
    "delay",
    "universe",
    "data_type",
    "decay",
    "truncation",
    "neutralization",
    "max_trade",
)


@dataclass(frozen=True)
class SettingsPreset:
    name: str
    description: str
    settings: dict[str, Any]
    source: str = "built-in"

    def normalized_settings(self) -> dict[str, Any]:
        values = {key: self.settings.get(key) for key in SETTING_KEYS if key in self.settings}
        if "region" in values:
            values["region"] = str(values["region"]).upper()
        if "universe" in values:
            values["universe"] = str(values["universe"]).upper()
        if "data_type" in values:
            values["data_type"] = str(values["data_type"]).upper()
        if "neutralization" in values:
            values["neutralization"] = str(values["neutralization"]).upper()
        if "delay" in values:
            values["delay"] = int(values["delay"])
        if "decay" in values:
            values["decay"] = int(values["decay"])
        if "truncation" in values:
            values["truncation"] = float(values["truncation"])
        if "max_trade" in values:
            values["max_trade"] = parse_bool(values["max_trade"])
        return values


BUILTIN_PRESETS: tuple[SettingsPreset, ...] = (
    SettingsPreset(
        name="eur_top2500_slow_fast",
        description="EUR delay=1 TOP2500 MATRIX setting with SLOW_AND_FAST neutralization.",
        settings={
            "region": "EUR",
            "delay": 1,
            "universe": "TOP2500",
            "data_type": "MATRIX",
            "decay": 10,
            "truncation": 0.08,
            "neutralization": "SLOW_AND_FAST",
            "max_trade": False,
        },
    ),
    SettingsPreset(
        name="usa_top3000_industry",
        description="USA delay=1 TOP3000 MATRIX setting with INDUSTRY neutralization.",
        settings={
            "region": "USA",
            "delay": 1,
            "universe": "TOP3000",
            "data_type": "MATRIX",
            "decay": 10,
            "truncation": 0.08,
            "neutralization": "INDUSTRY",
            "max_trade": False,
        },
    ),
    SettingsPreset(
        name="usa_top3000_subindustry",
        description="USA delay=1 TOP3000 MATRIX setting with SUBINDUSTRY neutralization.",
        settings={
            "region": "USA",
            "delay": 1,
            "universe": "TOP3000",
            "data_type": "MATRIX",
            "decay": 10,
            "truncation": 0.08,
            "neutralization": "SUBINDUSTRY",
            "max_trade": False,
        },
    ),
    SettingsPreset(
        name="glb_top3000_market",
        description="GLB delay=1 TOP3000 MATRIX setting with MARKET neutralization.",
        settings={
            "region": "GLB",
            "delay": 1,
            "universe": "TOP3000",
            "data_type": "MATRIX",
            "decay": 10,
            "truncation": 0.08,
            "neutralization": "MARKET",
            "max_trade": False,
        },
    ),
)


def presets_path(runtime_root: str | Path | None) -> Path:
    root = Path(runtime_root or ".brain_runtime")
    return root / "settings_presets.json"


def load_settings_presets(runtime_root: str | Path | None = None) -> list[SettingsPreset]:
    presets = list(BUILTIN_PRESETS)
    path = presets_path(runtime_root)
    if not path.exists():
        return presets
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("presets", data if isinstance(data, list) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        if not name or not settings:
            continue
        presets.append(
            SettingsPreset(
                name=name,
                description=str(item.get("description") or "User preset"),
                settings=settings,
                source=str(path),
            )
        )
    return presets


def find_settings_preset(name: str, runtime_root: str | Path | None = None) -> SettingsPreset:
    for preset in load_settings_presets(runtime_root):
        if preset.name == name:
            return preset
    available = ", ".join(p.name for p in load_settings_presets(runtime_root)) or "<none>"
    raise ValueError(f"Unknown settings preset: {name}. Available presets: {available}")


def select_settings_preset(runtime_root: str | Path | None = None) -> SettingsPreset:
    presets = load_settings_presets(runtime_root)
    if not presets:
        raise ValueError("No settings presets are available.")
    print(render_settings_presets(presets))
    while True:
        choice = input("Select preset number or name: ").strip()
        if not choice:
            raise ValueError("No preset selected.")
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(presets):
                return presets[index]
        for preset in presets:
            if preset.name == choice:
                return preset
        print("Invalid preset. Try again.", file=None)


def prompt_dataset(default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"Dataset id{suffix}: ").strip()
    if value:
        return value
    if default:
        return default
    raise ValueError("No dataset selected.")


def render_settings_presets(presets: list[SettingsPreset]) -> str:
    rows = ["#  name                       region delay universe data_type neutralization source"]
    for idx, preset in enumerate(presets, start=1):
        settings = preset.normalized_settings()
        rows.append(
            f"{idx:<2} {preset.name:<26} "
            f"{str(settings.get('region', '')):<6} "
            f"{str(settings.get('delay', '')):<5} "
            f"{str(settings.get('universe', '')):<8} "
            f"{str(settings.get('data_type', '')):<9} "
            f"{str(settings.get('neutralization', '')):<14} "
            f"{preset.source}"
        )
    return "\n".join(rows)


def render_preset_detail(preset: SettingsPreset) -> str:
    lines = [f"{preset.name} ({preset.source})", preset.description, ""]
    for key in SETTING_KEYS:
        if key in preset.normalized_settings():
            lines.append(f"{key}: {preset.normalized_settings()[key]}")
    return "\n".join(lines)


def settings_command_fragment(settings: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in SETTING_KEYS:
        if key not in settings:
            continue
        flag = "--" + key.replace("_", "-")
        value = settings[key]
        if isinstance(value, bool):
            value = "True" if value else "False"
        parts.append(f"{flag} {value}")
    return " ".join(parts)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got {value!r}")
