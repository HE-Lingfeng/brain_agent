from __future__ import annotations

import contextlib
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DailySimulationUsage:
    """Runtime-level local ledger for alphas submitted through brain_agent."""

    def __init__(self, runtime_root: str | Path):
        self.runtime_root = Path(runtime_root)
        self.path = self.runtime_root / "daily_simulation_usage.json"
        self.lock_path = self.runtime_root / "daily_simulation_usage.lock"

    def record_submission(
        self,
        *,
        run_id: str,
        batch_number: int,
        submitted_count: int,
        succeeded: int = 0,
        failed: int = 0,
        retryable: int = 0,
        status: str = "",
    ) -> dict[str, Any]:
        count = max(0, int(submitted_count))
        now = datetime.now(timezone.utc)
        day = self._local_day(now)
        event = {
            "timestamp": now.isoformat(),
            "run_id": str(run_id),
            "batch_number": int(batch_number),
            "submitted_count": count,
            "succeeded": max(0, int(succeeded)),
            "failed": max(0, int(failed)),
            "retryable": max(0, int(retryable)),
            "status": str(status or ""),
        }
        with self._locked_state() as state:
            day_state = self._day_state(state, day)
            day_state["submitted_count"] = int(day_state.get("submitted_count") or 0) + count
            day_state["succeeded"] = int(day_state.get("succeeded") or 0) + int(event["succeeded"])
            day_state["failed"] = int(day_state.get("failed") or 0) + int(event["failed"])
            day_state["retryable"] = int(day_state.get("retryable") or 0) + int(event["retryable"])
            day_state.setdefault("events", []).append(event)
            self._write_state(state)
            return dict(day_state)

    def summary(self, date: str | None = None) -> dict[str, Any]:
        day = date or self._local_day(datetime.now(timezone.utc))
        with self._locked_state() as state:
            day_state = self._day_state(state, day, create=False)
            if day_state is None:
                result = {
                    "date": day,
                    "submitted_count": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "retryable": 0,
                    "events": [],
                }
            else:
                result = dict(day_state)
        return self._with_unledgered_worker_stats(result, day)

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
            return {"version": 1, "days": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "days": {}}
        if not isinstance(data, dict):
            return {"version": 1, "days": {}}
        if not isinstance(data.get("days"), dict):
            data["days"] = {}
        data.setdefault("version", 1)
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _day_state(self, state: dict[str, Any], day: str, *, create: bool = True) -> dict[str, Any] | None:
        days = state.setdefault("days", {})
        existing = days.get(day)
        if isinstance(existing, dict):
            existing.setdefault("date", day)
            existing.setdefault("submitted_count", 0)
            existing.setdefault("succeeded", 0)
            existing.setdefault("failed", 0)
            existing.setdefault("retryable", 0)
            existing.setdefault("events", [])
            return existing
        if not create:
            return None
        days[day] = {
            "date": day,
            "submitted_count": 0,
            "succeeded": 0,
            "failed": 0,
            "retryable": 0,
            "events": [],
        }
        return days[day]

    def _local_day(self, now_utc: datetime) -> str:
        return now_utc.astimezone().date().isoformat()

    def _with_unledgered_worker_stats(self, summary: dict[str, Any], day: str) -> dict[str, Any]:
        result = {
            "date": summary.get("date") or day,
            "submitted_count": int(summary.get("submitted_count") or 0),
            "succeeded": int(summary.get("succeeded") or 0),
            "failed": int(summary.get("failed") or 0),
            "retryable": int(summary.get("retryable") or 0),
            "events": list(summary.get("events") or []),
        }
        ledger_run_ids = {str(event.get("run_id") or "") for event in result["events"] if isinstance(event, dict)}
        for stats_path in sorted((self.runtime_root / "runs").glob("*/worker_stats/worker_stats.json")):
            run_id = stats_path.parents[1].name
            if run_id in ledger_run_ids:
                continue
            try:
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(stats, dict):
                continue
            if str(stats.get("end_time") or ""):
                continue
            start_time = str(stats.get("start_time") or "")
            if not start_time or self._day_from_iso(start_time) != day:
                continue
            submitted = max(0, int(stats.get("total_submitted") or 0))
            if submitted <= 0:
                continue
            event = {
                "timestamp": str(stats.get("end_time") or stats.get("start_time") or ""),
                "run_id": run_id,
                "batch_number": None,
                "submitted_count": submitted,
                "succeeded": max(0, int(stats.get("total_succeeded") or 0)),
                "failed": max(0, int(stats.get("total_failed") or 0)),
                "retryable": max(0, int(stats.get("total_retryable") or 0)),
                "status": "active_worker_stats",
                "source": str(stats_path),
            }
            result["events"].append(event)
            result["submitted_count"] += int(event["submitted_count"])
            result["succeeded"] += int(event["succeeded"])
            result["failed"] += int(event["failed"])
            result["retryable"] += int(event["retryable"])
        return result

    def _day_from_iso(self, value: str) -> str | None:
        try:
            return datetime.fromisoformat(value).astimezone().date().isoformat()
        except ValueError:
            return None
