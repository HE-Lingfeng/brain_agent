from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RUNTIME_ROOT = Path("~/.claude/brain_runtime").expanduser()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    run_id: str
    run_dir: Path
    db_path: Path
    artifacts_dir: Path
    logs_dir: Path
    tasks_dir: Path


def new_run_id(dataset: str, region: str) -> str:
    return f"{dataset}_{region}_{uuid.uuid4().hex[:10]}"


def get_runtime_paths(run_id: str, runtime_root: str | Path | None = None) -> RuntimePaths:
    root = Path(runtime_root or os.environ.get("BRAIN_AGENT_RUNTIME_ROOT") or DEFAULT_RUNTIME_ROOT).expanduser().resolve()
    run_dir = root / "runs" / run_id
    return RuntimePaths(
        root=root,
        run_id=run_id,
        run_dir=run_dir,
        db_path=run_dir / "brain_agent.sqlite3",
        artifacts_dir=run_dir / "artifacts",
        logs_dir=run_dir / "logs",
        tasks_dir=run_dir / "tasks",
    )


def ensure_runtime(paths: RuntimePaths) -> None:
    for path in (paths.run_dir, paths.artifacts_dir, paths.logs_dir, paths.tasks_dir):
        path.mkdir(parents=True, exist_ok=True)
