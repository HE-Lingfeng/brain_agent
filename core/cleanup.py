from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SMOKE_RUN_MARKERS = (
    "smoke",
    "dry",
    "dryrun",
    "flow_check",
    "doctor",
    "optimizer",
    "variant",
    "thesis",
)

CACHE_DIR_NAMES = {"__pycache__", ".pytest_cache"}
SQLITE_SUFFIXES = ("sqlite3", "db")


@dataclass(frozen=True)
class CleanupOptions:
    runtime_root: Path
    repo_root: Path
    dry_run: bool = True
    include_cache: bool = False
    include_smoke_runs: bool = False
    include_failed_runs: bool = False
    older_than_days: int | None = None
    run_ids: tuple[str, ...] = ()
    keep_recent: int = 0
    include_legacy_outputs: bool = False
    vacuum: bool = False
    force_running: bool = False


def build_cleanup_plan(options: CleanupOptions) -> dict[str, Any]:
    root = options.runtime_root.expanduser().resolve()
    repo_root = options.repo_root.expanduser().resolve()
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if options.include_cache:
        items.extend(_cache_items(repo_root))
    if options.include_legacy_outputs:
        items.extend(_legacy_output_items(repo_root))

    runs_dir = root / "runs"
    run_infos = _run_infos(runs_dir)
    selected_run_ids = set(options.run_ids)
    recent_keep = {info["run_id"] for info in _recent_runs(run_infos, options.keep_recent)}
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(0, int(options.older_than_days)))
        if options.older_than_days is not None
        else None
    )

    for info in run_infos:
        run_id = str(info["run_id"])
        reasons: list[str] = []
        if run_id in selected_run_ids:
            reasons.append("run_id")
        if options.include_smoke_runs and _is_smoke_run(run_id):
            reasons.append("smoke")
        if options.include_failed_runs and str(info.get("stage") or "").upper() == "FAILED":
            reasons.append("failed")
        if cutoff is not None and _run_timestamp(info) < cutoff:
            reasons.append(f"older_than_{options.older_than_days}d")

        if not reasons:
            continue
        if run_id in recent_keep:
            skipped.append(_skip(info["path"], "kept_by_keep_recent", info))
            continue
        if info.get("has_running_tasks") and not options.force_running:
            skipped.append(_skip(info["path"], "has_running_tasks", info))
            continue

        items.append(
            {
                "kind": "run_dir",
                "path": str(info["path"]),
                "bytes": info["bytes"],
                "reason": ",".join(reasons),
                "run_id": run_id,
                "stage": info.get("stage") or "",
                "updated_at": info.get("updated_at") or "",
            }
        )

    if options.vacuum:
        items.extend(_vacuum_items(root))

    return {
        "runtime_root": str(root),
        "dry_run": bool(options.dry_run),
        "items": sorted(items, key=lambda item: (str(item["kind"]), str(item["path"]))),
        "skipped": skipped,
        "total_bytes": sum(int(item.get("bytes") or 0) for item in items if item.get("kind") != "vacuum"),
    }


def execute_cleanup_plan(plan: dict[str, Any]) -> dict[str, Any]:
    removed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for item in plan.get("items", []):
        path = Path(str(item.get("path") or ""))
        kind = str(item.get("kind") or "")
        try:
            if kind == "vacuum":
                _vacuum_sqlite(path)
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            removed.append(item)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    result = dict(plan)
    result["dry_run"] = False
    result["removed"] = removed
    result["errors"] = errors
    result["removed_bytes"] = sum(int(item.get("bytes") or 0) for item in removed if item.get("kind") != "vacuum")
    return result


def render_cleanup_summary(plan: dict[str, Any]) -> str:
    mode = "dry-run" if plan.get("dry_run", True) else "applied"
    lines = [
        f"cleanup_mode: {mode}",
        f"runtime_root: {plan.get('runtime_root')}",
        f"items: {len(plan.get('items', []))}",
        f"reclaimable: {_format_bytes(int(plan.get('total_bytes') or 0))}",
    ]
    if plan.get("removed") is not None:
        lines.append(f"removed: {len(plan.get('removed', []))}")
        lines.append(f"removed_bytes: {_format_bytes(int(plan.get('removed_bytes') or 0))}")
    if plan.get("skipped"):
        lines.append(f"skipped: {len(plan.get('skipped', []))}")
    if plan.get("errors"):
        lines.append(f"errors: {len(plan.get('errors', []))}")
    return "\n".join(lines) + "\n"


def _cache_items(repo_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in repo_root.rglob("*"):
        if path.name in CACHE_DIR_NAMES and path.is_dir():
            items.append(_item("cache", path, "python_cache"))
        elif path.is_file() and path.suffix == ".pyc":
            items.append(_item("cache_file", path, "python_bytecode"))
    return _dedupe_items(items)


def _legacy_output_items(repo_root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    agents = repo_root / ".agents"
    if not agents.exists():
        return items
    for path in agents.rglob("*"):
        if not path.is_dir():
            continue
        if path.name == "outputs" or path.name == ".brain_runtime":
            items.append(_item("legacy_output", path, "legacy_skill_output"))
    return _dedupe_items(items)


def _run_infos(runs_dir: Path) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    infos = []
    for path in runs_dir.iterdir():
        if not path.is_dir():
            continue
        info = {
            "run_id": path.name,
            "path": path,
            "bytes": _path_size(path),
            "stage": "",
            "created_at": "",
            "updated_at": "",
            "has_running_tasks": False,
        }
        db = path / "brain_agent.sqlite3"
        if db.exists():
            info.update(_read_run_db(db, path.name))
        infos.append(info)
    return infos


def _read_run_db(db_path: Path, run_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT stage, created_at, updated_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        running = conn.execute(
            "SELECT 1 FROM tasks WHERE run_id = ? AND status IN ('running', 'pending') LIMIT 1",
            (run_id,),
        ).fetchone()
        return {
            "stage": str(run["stage"] or "") if run else "",
            "created_at": str(run["created_at"] or "") if run else "",
            "updated_at": str(run["updated_at"] or "") if run else "",
            "has_running_tasks": bool(running),
        }
    except sqlite3.DatabaseError:
        return {}
    finally:
        conn.close()


def _recent_runs(run_infos: list[dict[str, Any]], keep_recent: int) -> list[dict[str, Any]]:
    if keep_recent <= 0:
        return []
    return sorted(run_infos, key=_run_timestamp, reverse=True)[:keep_recent]


def _run_timestamp(info: dict[str, Any]) -> datetime:
    for key in ("updated_at", "created_at"):
        value = str(info.get(key) or "")
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    path = Path(str(info["path"]))
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _is_smoke_run(run_id: str) -> bool:
    name = run_id.lower()
    return any(marker in name for marker in SMOKE_RUN_MARKERS)


def _vacuum_items(runtime_root: Path) -> list[dict[str, Any]]:
    items = []
    for path in runtime_root.rglob("*"):
        if path.is_file() and path.suffix.lstrip(".") in SQLITE_SUFFIXES:
            items.append({"kind": "vacuum", "path": str(path), "bytes": 0, "reason": "sqlite_vacuum"})
    return items


def _vacuum_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def _item(kind: str, path: Path, reason: str) -> dict[str, Any]:
    return {"kind": kind, "path": str(path), "bytes": _path_size(path), "reason": reason}


def _skip(path: Path, reason: str, info: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(path),
        "reason": reason,
        "run_id": str(info.get("run_id") or ""),
        "stage": str(info.get("stage") or ""),
    }


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for item in sorted(items, key=lambda x: len(str(x["path"]))):
        path = str(item["path"])
        if any(path.startswith(parent + "/") for parent in seen):
            continue
        seen.add(path)
        deduped.append(item)
    return deduped


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"
