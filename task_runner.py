from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .utils import now_iso


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    pid: int
    task_dir: Path
    stdout_path: Path
    stderr_path: Path


class TaskRunner:
    def __init__(self, tasks_dir: str | Path):
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def start(self, adapter: str, cmd: list[str], cwd: str | Path) -> TaskHandle:
        task_id = f"{adapter}_{uuid.uuid4().hex[:10]}"
        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        popen_kwargs = {"start_new_session": True} if os.name != "nt" else {}
        with stdout_path.open("a", encoding="utf-8") as out, stderr_path.open("a", encoding="utf-8") as err:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=out,
                stderr=err,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
        meta = {
            "task_id": task_id,
            "adapter": adapter,
            "pid": proc.pid,
            "cmd": cmd,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "created_at": now_iso(),
        }
        (task_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return TaskHandle(task_id, proc.pid, task_dir, stdout_path, stderr_path)

    @staticmethod
    def is_pid_running(pid: int | None) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @classmethod
    def infer_status(cls, pid: int | None, current_status: str) -> str:
        if current_status in {"completed", "failed", "cancelled"}:
            return current_status
        return "running" if cls.is_pid_running(pid) else "exited"

    @staticmethod
    def cancel(pid: int | None) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            if os.name != "nt":
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
                return True
            except OSError:
                return False

    def run_foreground(self, cmd: list[str], cwd: str | Path, stdout_path: Path, stderr_path: Path) -> int:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("a", encoding="utf-8") as out, stderr_path.open("a", encoding="utf-8") as err:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=out,
                stderr=err,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        return int(proc.returncode)

    def run_tracked(
        self,
        adapter: str,
        cmd: list[str],
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        on_start: Callable[[TaskHandle], None] | None = None,
        on_progress: Callable[[TaskHandle], None] | None = None,
        progress_interval_seconds: int = 30,
    ) -> tuple[TaskHandle, int]:
        task_id = f"{adapter}_{uuid.uuid4().hex[:10]}"
        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        meta = {
            "task_id": task_id,
            "adapter": adapter,
            "pid": None,
            "cmd": cmd,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "created_at": now_iso(),
        }
        with stdout_path.open("a", encoding="utf-8") as out, stderr_path.open("a", encoding="utf-8") as err:
            popen_kwargs = {"start_new_session": True} if os.name != "nt" else {}
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=out,
                stderr=err,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                **popen_kwargs,
            )
            meta["pid"] = proc.pid
            (task_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            handle = TaskHandle(task_id, int(meta["pid"] or 0), task_dir, stdout_path, stderr_path)
            if on_start:
                on_start(handle)
            if on_progress:
                while proc.poll() is None:
                    on_progress(handle)
                    try:
                        proc.wait(timeout=max(1, int(progress_interval_seconds)))
                    except subprocess.TimeoutExpired:
                        continue
                returncode = proc.returncode
            else:
                returncode = proc.wait()
        return handle, int(returncode)


def python_cmd(script: Path, *args: str) -> list[str]:
    return [sys.executable, str(script), *args]
