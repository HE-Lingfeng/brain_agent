from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import now_iso


DEFAULT_PLATFORM_SIMULATION_SLOTS = 80
DEFAULT_LEASE_STALE_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class SimulationLease:
    lease_id: str
    slots: int
    active_slots: int
    max_slots: int


class SimulationLeasePool:
    """Cross-process slot leases for BRAIN batch simulations."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        max_slots: int = DEFAULT_PLATFORM_SIMULATION_SLOTS,
        stale_seconds: int = DEFAULT_LEASE_STALE_SECONDS,
    ):
        self.runtime_root = Path(runtime_root)
        self.max_slots = max(1, int(max_slots))
        self.stale_seconds = max(60, int(stale_seconds))
        self.path = self.runtime_root / "simulation_leases.json"
        self.lock_path = self.runtime_root / "simulation_leases.lock"

    def acquire(
        self,
        *,
        run_id: str,
        requested_slots: int,
        wait_seconds: int = 30,
        shutdown_requested: Any | None = None,
        log_prefix: str = "[sim-lease]",
    ) -> SimulationLease:
        requested = max(1, min(int(requested_slots), self.max_slots))
        while True:
            with self._locked_state() as state:
                state = self._prune_inactive(state)
                active = self._active_slots(state)
                available = max(0, self.max_slots - active)
                if available > 0:
                    slots = min(requested, available)
                    lease_id = uuid.uuid4().hex
                    state.setdefault("leases", []).append(
                        {
                            "lease_id": lease_id,
                            "run_id": run_id,
                            "pid": os.getpid(),
                            "slots": slots,
                            "created_at": now_iso(),
                        }
                    )
                    self._write_state(state)
                    return SimulationLease(
                        lease_id=lease_id,
                        slots=slots,
                        active_slots=active + slots,
                        max_slots=self.max_slots,
                    )

            if shutdown_requested is not None and shutdown_requested():
                raise RuntimeError("shutdown requested while waiting for simulation slots")
            print(f"{log_prefix} platform slots full ({self.max_slots}/{self.max_slots}); waiting {wait_seconds}s")
            time.sleep(max(1, int(wait_seconds)))

    def release(self, lease: SimulationLease) -> None:
        with self._locked_state() as state:
            leases = state.get("leases") or []
            state["leases"] = [item for item in leases if item.get("lease_id") != lease.lease_id]
            self._write_state(state)

    def snapshot(self) -> dict[str, Any]:
        with self._locked_state() as state:
            state = self._prune_inactive(state)
            self._write_state(state)
            return {
                "max_slots": self.max_slots,
                "active_slots": self._active_slots(state),
                "leases": list(state.get("leases") or []),
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
            return {"version": 1, "leases": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "leases": []}
        if not isinstance(data, dict):
            return {"version": 1, "leases": []}
        leases = data.get("leases")
        if not isinstance(leases, list):
            data["leases"] = []
        data.setdefault("version", 1)
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _prune_inactive(self, state: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        kept: list[dict[str, Any]] = []
        for item in state.get("leases") or []:
            pid = int(item.get("pid") or 0)
            created_at = self._timestamp(item.get("created_at"))
            if pid and not self._pid_alive(pid):
                continue
            if created_at and now - created_at > self.stale_seconds:
                continue
            kept.append(item)
        state["leases"] = kept
        return state

    def _active_slots(self, state: dict[str, Any]) -> int:
        return sum(max(0, int(item.get("slots") or 0)) for item in state.get("leases") or [])

    def _timestamp(self, value: Any) -> float | None:
        if not value:
            return None
        try:
            from datetime import datetime

            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return None

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
