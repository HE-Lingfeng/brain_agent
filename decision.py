from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .credentials import load_credentials
from .models import CandidateStatus
from .prompting import DEFAULT_DECISION_PROMPT_VERSION
from .scoring import score_candidates


@dataclass(frozen=True)
class EnhanceAction:
    mode: str
    style: str
    candidate_ids: list[int]
    reason: str
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "style": self.style,
            "candidate_ids": self.candidate_ids,
            "reason": self.reason,
            "source": self.source,
        }


class DecisionEngine:
    """Select enhancement actions from candidate state.

    The hard submission gate remains outside this engine. LLM output can suggest
    enhancement actions only; it cannot mark candidates submit-ready.
    """

    def __init__(self, *, max_actions: int = 4, use_llm: bool = False, prompt_version: str = DEFAULT_DECISION_PROMPT_VERSION):
        self.max_actions = max(1, int(max_actions))
        self.use_llm = bool(use_llm)
        self.prompt_version = prompt_version or DEFAULT_DECISION_PROMPT_VERSION

    def decide(self, candidates: list[dict[str, Any]]) -> list[EnhanceAction]:
        pool = score_candidates(self._eligible(candidates))
        if not pool:
            return []
        if self.use_llm:
            llm_actions = self._decide_with_llm(pool)
            if llm_actions:
                return llm_actions[: self.max_actions]
        return self._decide_with_rules(pool)[: self.max_actions]

    def _eligible(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = {CandidateStatus.PROMISING.value, CandidateStatus.NEEDS_ENHANCE.value}
        return [c for c in candidates if c.get("status") in allowed and c.get("candidate_id") is not None]

    def _decide_with_rules(self, candidates: list[dict[str, Any]]) -> list[EnhanceAction]:
        ranked = sorted(
            candidates,
            key=lambda c: (float(c.get("selection_score") or 0), int(c.get("candidate_id") or 0)),
            reverse=True,
        )
        actions: list[EnhanceAction] = []
        for candidate in ranked:
            if len(actions) >= self.max_actions:
                break
            tags = _listish(candidate.get("failure_tags"))
            objectives = _listish(candidate.get("repair_objectives"))
            if "cand_neg" not in tags and "test_short_flip" not in objectives:
                continue
            actions.append(
                EnhanceAction(
                    mode="single",
                    style="short_flip",
                    candidate_ids=[int(candidate["candidate_id"])],
                    reason=(
                        "Rule fallback detected a strong negative signal and selected it for short-flip enhancement "
                        f"(score={float(candidate.get('selection_score') or 0):.3f})."
                    ),
                )
            )
        if len(ranked) >= 2:
            actions.append(
                EnhanceAction(
                    mode="cross",
                    style="balanced",
                    candidate_ids=[int(ranked[0]["candidate_id"]), int(ranked[1]["candidate_id"])],
                    reason=(
                        "Rule fallback paired the top two scored promising or improvable candidates "
                        f"(scores={float(ranked[0].get('selection_score') or 0):.3f}, "
                        f"{float(ranked[1].get('selection_score') or 0):.3f})."
                    ),
                )
            )
        for candidate in ranked:
            if len(actions) >= self.max_actions:
                break
            status = str(candidate.get("status") or "")
            style = "conservative" if status == CandidateStatus.PROMISING.value else "balanced"
            actions.append(
                EnhanceAction(
                    mode="single",
                    style=style,
                    candidate_ids=[int(candidate["candidate_id"])],
                    reason=(
                        f"Rule fallback selected {status} candidate for {style} single enhancement "
                        f"with score={float(candidate.get('selection_score') or 0):.3f}."
                    ),
                )
            )
        return actions

    def _decide_with_llm(self, candidates: list[dict[str, Any]]) -> list[EnhanceAction]:
        try:
            creds = load_credentials(require_brain=False, require_llm=True)
            payload = _build_llm_payload(candidates, self.max_actions, creds, self.prompt_version)
            req = urllib.request.Request(
                url=creds["moonshot_base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {creds['moonshot_api_key']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            raw_actions = _parse_llm_actions(content)
        except (KeyError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError):
            return []

        valid_ids = {int(c["candidate_id"]) for c in candidates if c.get("candidate_id") is not None}
        actions: list[EnhanceAction] = []
        for item in raw_actions if isinstance(raw_actions, list) else []:
            if not isinstance(item, dict):
                continue
            ids = []
            for raw_id in item.get("candidate_ids", []):
                try:
                    candidate_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if candidate_id in valid_ids:
                    ids.append(candidate_id)
            mode = str(item.get("mode") or "single")
            if mode not in {"single", "cross"} or not ids:
                continue
            if mode == "single":
                ids = ids[:1]
            style = str(item.get("style") or "balanced")
            if style not in {"conservative", "balanced", "aggressive", "ultra-aggressive"}:
                style = "balanced"
            actions.append(
                EnhanceAction(
                    mode=mode,
                    style=style,
                    candidate_ids=ids,
                    reason=str(item.get("reason") or "LLM selected this enhancement action."),
                    source="llm",
                )
            )
        return actions


def _parse_llm_actions(content: str) -> list[dict[str, Any]]:
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for marker in ("[", "{"):
            start = raw.find(marker)
            while start != -1:
                try:
                    parsed, _ = decoder.raw_decode(raw[start:])
                    break
                except json.JSONDecodeError:
                    start = raw.find(marker, start + 1)
            else:
                continue
            break
        else:
            raise
    if isinstance(parsed, dict):
        for key in ("actions", "items", "results", "data"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _build_llm_payload(
    candidates: list[dict[str, Any]],
    max_actions: int,
    creds: dict[str, str],
    prompt_version: str = DEFAULT_DECISION_PROMPT_VERSION,
) -> dict[str, Any]:
    compact = [
        {
            "candidate_id": c.get("candidate_id"),
            "status": c.get("status"),
            "alpha_id": c.get("alpha_id"),
            "expression": c.get("expression"),
            "source": c.get("source"),
            "selection_score": c.get("selection_score"),
            "score_breakdown": c.get("score_breakdown"),
            "failure_tags": c.get("failure_tags"),
            "repair_objectives": c.get("repair_objectives"),
        }
        for c in candidates[:20]
    ]
    system = (
        "You choose WorldQuant BRAIN alpha enhancement actions. Return ONLY JSON array. "
        "Each item: mode single|cross, style conservative|balanced|aggressive|ultra-aggressive|short_flip, "
        "candidate_ids array, reason string. Do not mark submit readiness."
    )
    return {
        "model": os.environ.get("MOONSHOT_MODEL") or creds.get("moonshot_model", "kimi-k2.5"),
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    {"prompt_version": prompt_version, "max_actions": max_actions, "candidates": compact},
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.3,
    }


def _listish(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,|]", value) if item.strip()]
    return []
