from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from .repository import Repository
from .task_runner import TaskRunner


TERMINAL_STATUSES = {"COMPLETE", "COMPLETED", "ERROR", "FAIL", "FAILED", "TIMEOUT", "SUBMISSION_FAILED", "BATCH_SPAWN_FAILED"}
SUCCESS_STATUSES = {"COMPLETE", "COMPLETED"}


def build_simulation_progress(repo: Repository, run_id: str, run_dir: Path) -> dict[str, Any]:
    """Build a compact status snapshot for the current or latest batch simulation."""
    tasks = [row for row in repo.list_rows("tasks", run_id) if str(row.get("adapter") or "") == "batchSim"]
    latest_task = _latest_task(tasks)
    task_running = False
    if latest_task:
        task_running = TaskRunner.infer_status(latest_task.get("pid"), str(latest_task.get("status") or "")) == "running"

    alpha_json = _task_arg_path(latest_task, "--alpha-json") if latest_task else None
    output_csv = _task_arg_path(latest_task, "--output-csv") if latest_task else None
    default_output_csv = run_dir / "artifacts" / "03_simulate" / "simulation_status.csv"
    if output_csv is None or not output_csv.exists() or (default_output_csv.exists() and not _is_relative_to(output_csv, run_dir)):
        output_csv = default_output_csv if default_output_csv.exists() else (_latest_artifact_path(repo, run_id, "simulation_status") or default_output_csv)
    if alpha_json is not None and not alpha_json.exists():
        alpha_json = None

    total = _count_alpha_json(alpha_json) if alpha_json else 0
    csv_summary = _summarize_status_csv(output_csv)
    if total <= 0:
        total = csv_summary["unique_fingerprints"] or csv_summary["rows"]

    terminal = csv_summary["terminal"]
    remaining = max(total - terminal, 0) if total else 0
    batch_size = _task_int_arg(latest_task, "--batch-size") if latest_task else 0
    concurrency = _task_int_arg(latest_task, "--concurrency") if latest_task else 0
    capacity = batch_size * concurrency if batch_size and concurrency else 0
    running_now = _active_children_from_logs(latest_task) if latest_task else 0
    if running_now <= 0 and task_running and remaining:
        running_now = min(remaining, concurrency or remaining)
    percent = round((terminal / total) * 100, 1) if total else 0.0

    return {
        "task_id": latest_task.get("task_id") if latest_task else "",
        "task_status": TaskRunner.infer_status(latest_task.get("pid"), str(latest_task.get("status") or "")) if latest_task else "",
        "total": total,
        "done": terminal,
        "remaining": remaining,
        "running_now": running_now,
        "batch_size": batch_size,
        "concurrency": concurrency,
        "capacity": capacity,
        "percent": percent,
        "status_counts": csv_summary["status_counts"],
        "completed": csv_summary["completed"],
        "failed": csv_summary["failed"],
        "output_csv": str(output_csv) if output_csv else "",
        "alpha_json": str(alpha_json) if alpha_json else "",
        "latest_wait": _latest_wait_line(latest_task) if latest_task else "",
    }


def render_simulation_progress(progress: dict[str, Any]) -> str:
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    running = int(progress.get("running_now") or 0)
    percent = float(progress.get("percent") or 0.0)
    bar = _progress_bar(done, total)
    parts = [
        f"simulation: {bar} {done}/{total} ({percent:.1f}%)",
        f"running={running}",
        f"remaining={int(progress.get('remaining') or 0)}",
        f"completed={int(progress.get('completed') or 0)}",
        f"failed={int(progress.get('failed') or 0)}",
    ]
    capacity = int(progress.get("capacity") or 0)
    if capacity:
        parts.insert(2, f"slots={int(progress.get('concurrency') or 0)}x{int(progress.get('batch_size') or 0)}")
        parts.insert(3, f"capacity={capacity}")
    task_id = str(progress.get("task_id") or "")
    if task_id:
        parts.append(f"task={task_id}")
    latest_wait = str(progress.get("latest_wait") or "")
    if latest_wait:
        parts.append(f"wait='{latest_wait}'")
    return " | ".join(parts)


def _latest_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not tasks:
        return None
    return sorted(tasks, key=lambda row: str(row.get("created_at") or row.get("updated_at") or ""))[-1]


def _task_meta(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task:
        return {}
    stdout_path = Path(str(task.get("stdout_path") or ""))
    meta_path = stdout_path.parent / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _task_arg_path(task: dict[str, Any] | None, flag: str) -> Path | None:
    meta = _task_meta(task)
    cmd = meta.get("cmd") or meta.get("command") or []
    if not isinstance(cmd, list):
        return None
    for idx, token in enumerate(cmd[:-1]):
        if str(token) == flag:
            path = Path(str(cmd[idx + 1]))
            cwd = Path(str(meta.get("cwd") or "."))
            return path if path.is_absolute() else (cwd / path).resolve()
    return None


def _task_int_arg(task: dict[str, Any] | None, flag: str) -> int:
    meta = _task_meta(task)
    cmd = meta.get("cmd") or meta.get("command") or []
    if not isinstance(cmd, list):
        return 0
    for idx, token in enumerate(cmd[:-1]):
        if str(token) == flag:
            try:
                return int(cmd[idx + 1])
            except Exception:
                return 0
    return 0


def _latest_artifact_path(repo: Repository, run_id: str, kind: str) -> Path | None:
    rows = [row for row in repo.list_rows("artifacts", run_id) if row.get("kind") == kind]
    if not rows:
        return None
    return Path(str(rows[-1].get("path") or ""))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _count_alpha_json(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("alphas", "alpha_list", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def _summarize_status_csv(path: Path | None) -> dict[str, Any]:
    status_by_fp: dict[str, str] = {}
    rows = 0
    if path and path.exists():
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    rows += 1
                    fp = row.get("fingerprint") or row.get("simulation_fingerprint") or row.get("sim_id") or str(rows)
                    status_by_fp[str(fp)] = str(row.get("status") or "UNKNOWN").upper()
        except Exception:
            pass
    counts: dict[str, int] = {}
    for status in status_by_fp.values():
        counts[status] = counts.get(status, 0) + 1
    completed = sum(count for status, count in counts.items() if status in SUCCESS_STATUSES)
    failed = sum(count for status, count in counts.items() if status in TERMINAL_STATUSES and status not in SUCCESS_STATUSES)
    terminal = sum(count for status, count in counts.items() if status in TERMINAL_STATUSES)
    return {
        "rows": rows,
        "unique_fingerprints": len(status_by_fp),
        "status_counts": counts,
        "completed": completed,
        "failed": failed,
        "terminal": terminal,
    }


def _latest_wait_line(task: dict[str, Any] | None) -> str:
    if not task:
        return ""
    for path_key in ("stdout_path", "stderr_path"):
        path = Path(str(task.get(path_key) or ""))
        for line in reversed(_tail_lines(path, 80)):
            if "[BRAIN wait]" in line:
                return line.strip()
    return ""


def _active_children_from_logs(task: dict[str, Any] | None) -> int:
    line = _latest_wait_line(task)
    match = re.search(r"active_children=(\d+)", line)
    if match:
        return int(match.group(1))
    return 0


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return []


def _progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + "." * width + "]"
    filled = min(width, max(0, round(width * done / total)))
    return "[" + "#" * filled + "." * (width - filled) + "]"
