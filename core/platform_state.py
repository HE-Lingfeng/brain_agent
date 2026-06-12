from __future__ import annotations

import contextlib
import fcntl
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.utils import now_iso


DEFAULT_PLATFORM_STATE_WINDOW = 100


@dataclass(frozen=True)
class SimulationPolicy:
    batch_size: int
    concurrency: int
    mode: str
    reason: str
    cooldown_until: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "mode": self.mode,
            "reason": self.reason,
            "cooldown_until": self.cooldown_until,
        }


class PlatformSimulationState:
    """Runtime-wide BRAIN platform pressure signal store."""

    def __init__(self, runtime_root: Path, *, window: int = DEFAULT_PLATFORM_STATE_WINDOW):
        self.runtime_root = Path(runtime_root)
        self.window = max(10, int(window))
        self.path = self.runtime_root / "simulation_platform_state.json"
        self.lock_path = self.runtime_root / "simulation_platform_state.lock"

    def record_batch(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._locked_state() as state:
            events = [item for item in state.get("events") or [] if isinstance(item, dict)]
            event = dict(summary)
            event.setdefault("created_at", now_iso())
            event["pressure_score"] = _pressure_score(event)
            events.append(event)
            state["events"] = events[-self.window :]
            state["backpressure"] = self._backpressure_state(state["events"])
            state["updated_at"] = now_iso()
            self._write_state(state)
            return self.snapshot_from_state(state)

    def recommend_policy(
        self,
        *,
        batch_size: int,
        concurrency: int,
        min_concurrency: int = 1,
        enabled: bool = True,
    ) -> SimulationPolicy:
        batch_size = max(1, int(batch_size))
        concurrency = max(1, int(concurrency))
        min_concurrency = max(1, min(int(min_concurrency), concurrency))
        if not enabled:
            return SimulationPolicy(batch_size, concurrency, "fixed", "adaptive policy disabled")

        with self._locked_state() as state:
            events = [item for item in state.get("events") or [] if isinstance(item, dict)]
            backpressure = self._backpressure_state(events)
            state["backpressure"] = backpressure
            state["updated_at"] = now_iso()
            self._write_state(state)

        recent = events[-5:]
        cooldown_until = str(backpressure.get("cooldown_until") or "")
        if _iso_in_future(cooldown_until):
            reduced_concurrency = max(min_concurrency, concurrency // 2)
            reduced_batch_size = max(1, batch_size // 2)
            return SimulationPolicy(
                reduced_batch_size,
                reduced_concurrency,
                "adaptive",
                str(backpressure.get("reason") or "recent platform pressure"),
                cooldown_until,
            )
        if recent and sum(_pressure_score(item) for item in recent) >= 2:
            return SimulationPolicy(
                max(1, batch_size // 2),
                max(min_concurrency, concurrency // 2),
                "adaptive",
                "recent batch pressure",
            )
        return SimulationPolicy(batch_size, concurrency, "adaptive", "healthy or insufficient pressure signal")

    def snapshot(self) -> dict[str, Any]:
        with self._locked_state() as state:
            state["backpressure"] = self._backpressure_state(state.get("events") or [])
            self._write_state(state)
            return self.snapshot_from_state(state)

    def snapshot_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        events = [item for item in state.get("events") or [] if isinstance(item, dict)]
        return {
            "updated_at": state.get("updated_at", ""),
            "backpressure": state.get("backpressure") or {},
            "recent_events": events[-10:],
        }

    def _backpressure_state(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        recent = events[-5:]
        pressure = sum(_pressure_score(item) for item in recent)
        healthy = [
            item
            for item in recent[-3:]
            if _pressure_score(item) == 0 and float(item.get("success_rate") or 0.0) >= 0.8
        ]
        if pressure <= 0:
            return {
                "level": "healthy" if len(healthy) >= 3 else "unknown",
                "pressure_score": 0,
                "reason": "no recent platform pressure",
                "cooldown_until": "",
            }
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=min(30, 10 + pressure * 2))).isoformat()
        reasons = []
        for key in ("rate_limit_events", "retry_after_events", "timeout_count", "spawn_failed_count"):
            count = sum(int(item.get(key) or 0) for item in recent)
            if count:
                reasons.append(f"{key}={count}")
        return {
            "level": "congested" if pressure >= 2 else "watch",
            "pressure_score": pressure,
            "reason": ", ".join(reasons) or "low success rate",
            "cooldown_until": cooldown_until,
        }

    @contextlib.contextmanager
    def _locked_state(self):
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read_state()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "events": [], "backpressure": {}, "updated_at": ""}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "events": [], "backpressure": {}, "updated_at": ""}
        if not isinstance(data, dict):
            return {"version": 1, "events": [], "backpressure": {}, "updated_at": ""}
        data.setdefault("version", 1)
        data.setdefault("events", [])
        data.setdefault("backpressure", {})
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _pressure_score(event: dict[str, Any]) -> int:
    score = 0
    score += min(3, int(event.get("rate_limit_events") or 0))
    score += min(3, int(event.get("retry_after_events") or 0))
    score += 2 * min(3, int(event.get("timeout_count") or 0))
    score += 2 * min(3, int(event.get("spawn_failed_count") or 0))
    submitted = int(event.get("submitted_count") or 0)
    success_rate = float(event.get("success_rate") or 0.0)
    if submitted >= 5 and success_rate < 0.5:
        score += 1
    return score


def _iso_in_future(value: str) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value).timestamp() > time.time()
    except ValueError:
        return False
