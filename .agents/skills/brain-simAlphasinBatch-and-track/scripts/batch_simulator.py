import concurrent.futures
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time
import threading
import subprocess
import uuid
from pathlib import Path
import pandas as pd
import requests
from typing import List, Dict, Any

# Add brain-shared to path before local imports
_BRAIN_SHARED = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "brain-shared", "scripts"))
if _BRAIN_SHARED not in sys.path:
    sys.path.insert(0, _BRAIN_SHARED)

try:
    import ace_lib
except ModuleNotFoundError:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    app_dir = os.path.abspath(os.path.join(current_dir, "..", "..", "..", "APP"))
    if app_dir not in sys.path:
        sys.path.append(app_dir)
    import ace_lib

from credentials import apply_brain_env, load_credentials

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("BatchSimulator")
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
TERMINAL_SIM_STATUSES = {"COMPLETED", "COMPLETE", "ERROR", "FAIL", "FAILED"}
SIM_STATUS_FETCH_RETRIES = 3
SIM_STATUS_FETCH_RETRY_DELAY = 5.0


def _is_pid_running(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            return str(pid) in output and "No tasks are running" not in output
        except Exception:
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


def _tail_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    keep = max(1, int(max_lines))
    return lines[-keep:]


def _print_task_status(tasks_dir: Path, task_id: str, tail_lines: int) -> int:
    task_dir = (tasks_dir / task_id).resolve()
    meta_file = task_dir / "meta.json"
    if not meta_file.exists():
        logger.error(f"Task not found: {task_id}")
        logger.error(f"Checked: {task_dir}")
        return 1

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to parse task meta: {meta_file}. {exc}")
        return 1

    pid = meta.get("pid")
    alive = _is_pid_running(pid if isinstance(pid, int) else None)
    state = "running" if alive else "exited"
    stdout_log = Path(meta.get("stdout_log") or (task_dir / "stdout.log"))
    stderr_log = Path(meta.get("stderr_log") or (task_dir / "stderr.log"))

    print("=" * 70)
    print("Detached Task Status")
    print(f"task_id: {meta.get('task_id', task_id)}")
    print(f"pid: {pid}")
    print(f"state: {state}")
    print(f"started_at: {meta.get('started_at')}")
    print(f"task_dir: {task_dir}")
    print(f"stdout_log: {stdout_log}")
    print(f"stderr_log: {stderr_log}")
    print("=" * 70)

    stdout_tail = _tail_lines(stdout_log, tail_lines)
    stderr_tail = _tail_lines(stderr_log, tail_lines)

    print(f"--- stdout (last {max(1, int(tail_lines))} lines) ---")
    if stdout_tail:
        for line in stdout_tail:
            print(line)
    else:
        print("<empty>")

    print(f"--- stderr (last {max(1, int(tail_lines))} lines) ---")
    if stderr_tail:
        for line in stderr_tail:
            print(line)
    else:
        print("<empty>")

    return 0


def _build_detached_child_cmd(script_path: Path, raw_argv: List[str]) -> List[str]:
    value_flags = {"--task-id", "--tasks-dir", "--status", "--tail-lines"}
    bool_flags = {"--detached"}
    filtered: List[str] = []
    i = 0
    while i < len(raw_argv):
        token = str(raw_argv[i])
        if token in bool_flags:
            i += 1
            continue
        if token in value_flags:
            i += 2
            continue
        filtered.append(token)
        i += 1
    return [sys.executable, str(script_path)] + filtered


def _launch_detached(cmd: List[str], cwd: Path, task_id: str, tasks_dir: Path, mode: str) -> tuple[int, Path]:
    task_dir = (tasks_dir / task_id).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)

    stdout_log = task_dir / "stdout.log"
    stderr_log = task_dir / "stderr.log"
    meta_file = task_dir / "meta.json"

    popen_kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    with stdout_log.open("a", encoding="utf-8") as out, stderr_log.open("a", encoding="utf-8") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_kwargs,
        )

    meta = {
        "task_id": task_id,
        "pid": proc.pid,
        "status": "running",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "command": cmd,
        "cwd": str(cwd),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return proc.pid, task_dir


def load_credentials_from_config(config_path: str) -> bool:
    """Load credentials from config, ~/secrets, or env and set process env vars."""
    data = {}
    path_obj = Path(config_path) if config_path else None
    if path_obj is not None and not path_obj.is_absolute():
        path_obj = SKILL_ROOT / path_obj
    if path_obj is not None and path_obj.exists():
        with open(path_obj, "r", encoding="utf-8") as file:
            data = json.load(file)

    creds = load_credentials(data, require_brain=True, require_llm=False)
    apply_brain_env(creds)
    logger.info("Loaded BRAIN credentials from config, secret file, or environment")
    return True


def resolve_input_path(candidate_path: str, legacy_name: str) -> Path:
    """Resolve input file path with backward-compatible fallback."""
    path_obj = Path(candidate_path)
    if not path_obj.is_absolute():
        path_obj = SKILL_ROOT / path_obj
    if path_obj.exists():
        return path_obj

    legacy_path = SKILL_ROOT / legacy_name
    if legacy_path.exists():
        logger.info(f"Path not found: {path_obj}. Fallback to legacy file: {legacy_path}")
        return legacy_path

    return path_obj


def resolve_output_path(candidate_path: str) -> Path:
    """Resolve output CSV path and ensure parent directory exists."""
    path_obj = Path(candidate_path)
    if not path_obj.is_absolute():
        path_obj = SKILL_ROOT / path_obj
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    return path_obj


def build_default_output_csv_path(alpha_json_path: Path) -> Path:
    """Build deterministic output CSV path from alpha JSON filename."""
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in alpha_json_path.stem).strip("_")
    if not safe_stem:
        safe_stem = "alpha_list"
    return resolve_output_path(f"outputs/{safe_stem}_simulation_status.csv")

def get_alpha_fingerprint(alpha_data: dict) -> str:
    """
    Generates a unique MD5 hash for an alpha configuration data.
    Keys are sorted to ensure consistent hashing.
    """
    # Create a copy to avoid modifying original
    data_copy = alpha_data.copy()
    # Serialize with sorted keys for consistency
    alpha_json = json.dumps(data_copy, sort_keys=True)
    return hashlib.md5(alpha_json.encode('utf-8')).hexdigest()


_BRAIN_SIMULATION_TOP_LEVEL_KEYS = {
    "type",
    "settings",
    "regular",
    "selection",
    "combo",
}


def brain_submission_payload(alpha_data: dict) -> dict:
    """Return only fields accepted by the BRAIN simulations endpoint."""
    payload = {
        key: value
        for key, value in alpha_data.items()
        if key in _BRAIN_SIMULATION_TOP_LEVEL_KEYS and value is not None
    }
    if "type" not in payload:
        payload["type"] = "REGULAR"
    if "settings" not in payload or not isinstance(payload["settings"], dict):
        payload["settings"] = {}
    return payload


def _normalize_slot_limits(
    region: str,
    batch_size: int | None,
    concurrency: int | None,
) -> tuple[int, int, int, int, int, int]:
    """Return safe batch/concurrency values, defaults, and region-specific maxima."""
    region_norm = str(region or "").upper()
    max_concurrency = 4 if region_norm == "GLB" else 8
    max_batch_size = 10
    default_concurrency = 4 if region_norm == "GLB" else 8
    default_batch_size = 4 if region_norm == "GLB" else 10
    requested_concurrency = default_concurrency if concurrency is None else int(concurrency)
    requested_batch_size = default_batch_size if batch_size is None else int(batch_size)
    safe_concurrency = min(max(1, requested_concurrency), max_concurrency)
    safe_batch_size = min(max(1, requested_batch_size), max_batch_size)
    return safe_batch_size, safe_concurrency, default_batch_size, default_concurrency, max_batch_size, max_concurrency

def lookINTO_SimError_message(session: requests.Session, locations: List[str]) -> List[dict]:
    """
    Fetches simulation status from a list of location URLs to extract detailed error messages.
    """
    errors = []
    
    for loc in locations:
        if not loc:
            continue
            
        try:
            # Handle full URL or relative path
            target_url = loc if loc.startswith("http") else f"{ace_lib.brain_api_url}{loc}"
            res = session.get(target_url)
            
            if res.status_code == 200:
                data = res.json()
                # Check for various error fields common in Brain API
                error_msg = data.get("message") or data.get("error") or "Unknown error"
                status = data.get("status", "UNKNOWN")
                
                # Only collect if it's actually an error or failed state
                if status in ["ERROR", "FAIL", "FAILED"]:
                    errors.append({
                        "location": loc,
                        "status": status,
                        "message": error_msg,
                        "raw": data
                    })
        except Exception as e:
            logger.error(f"Failed to fetch error details for {loc}: {e}")
            errors.append({"location": loc, "error": str(e)})
            
    return errors

class BatchSimulator:
    def __init__(self, session: ace_lib.SingleSession, output_csv="alpha_simulation_status.csv"):
        self.session = session
        self.output_csv = output_csv
        self.completed_fingerprints = set()
        self.csv_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats = {
            "skipped": 0,
            "submitted": 0,
            "completed": 0,
            "failed": 0,
        }
        self.csv_columns = [
            "fingerprint",
            "alpha_type",
            "regular_expression",
            "selection_expression",
            "combo_expression",
            "settings_json",
            "simulate_data_json",
            "sim_id",
            "status",
            "timestamp",
            "alpha_id",
            "pnl",
            "sharpe",
            "turnover",
            "fitness",
            "error",
            "error_details",
        ]
        
        # Initialize or load state
        self._load_state()

        # Progress tracking
        self.total_alphas = 0
        self.processed_alphas = 0
        self.active_batches = 0
        self.max_concurrency = 2
        self.heartbeat_seconds = 60
        self.parent_wait_seconds = 30 * 60
        self.child_wait_seconds = 60 * 60
        self.stale_healthcheck_seconds = 15 * 60
        self.session_lock = threading.Lock()

    def _reset_run_stats(self):
        with self.stats_lock:
            self.stats = {
                "skipped": 0,
                "submitted": 0,
                "completed": 0,
                "failed": 0,
            }

    def _inc_stat(self, key: str, value: int = 1):
        with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + value

    @staticmethod
    def _alpha_metadata(alpha_data: dict) -> dict:
        """Build persistent metadata for traceability in CSV."""
        settings = alpha_data.get("settings", {})
        simulate_payload = {k: v for k, v in alpha_data.items() if k != "settings"}
        return {
            "alpha_type": alpha_data.get("type", ""),
            "regular_expression": alpha_data.get("regular", ""),
            "selection_expression": alpha_data.get("selection", ""),
            "combo_expression": alpha_data.get("combo", ""),
            "settings_json": json.dumps(settings, ensure_ascii=False, sort_keys=True),
            "simulate_data_json": json.dumps(simulate_payload, ensure_ascii=False, sort_keys=True),
        }

    def _load_state(self):
        """Loads existing simulation state from CSV to support resuming."""
        try:
            if not pd.io.common.file_exists(self.output_csv):
                 logger.info(f"No existing state file found at {self.output_csv}. Starting fresh.")
                 return

            df = pd.read_csv(self.output_csv, on_bad_lines="skip")
            # Assuming 'fingerprint' and 'status' columns exist
            if 'fingerprint' in df.columns and 'status' in df.columns:
                status_norm = df['status'].astype(str).str.upper().str.strip()
                # Same CSV auto-resume rule: any successful row for a fingerprint marks it done.
                completed = df[status_norm.isin(['COMPLETED', 'COMPLETE'])]
                self.completed_fingerprints = set(completed['fingerprint'].tolist())
                logger.info(f"Loaded {len(self.completed_fingerprints)} completed alphas from {self.output_csv}")
        except Exception as e:
            logger.warning(f"Could not load state file: {e}")

    def _submit_with_retry(self, submit_payload, max_retries: int = 8):
        """Submit simulation batch with backoff on platform concurrency/rate limits and connection errors."""
        import urllib3

        _SUBMIT_CONNECTION_ERRORS = (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.SSLError,
        )

        for attempt in range(max_retries):
            try:
                resp = ace_lib.start_simulation(self.session, submit_payload)
            except _SUBMIT_CONNECTION_ERRORS as exc:
                if attempt < max_retries - 1:
                    delay = min(2 ** attempt, 60)
                    logger.warning(
                        f"Batch submission connection error (attempt {attempt + 1}/{max_retries}): "
                        f"{type(exc).__name__}: {exc}. Retry in {delay}s"
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    f"Batch submission connection error after {max_retries} attempts: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise

            if resp.status_code in [200, 201, 202]:
                return resp

            if resp.status_code == 429:
                retry_after = 0
                try:
                    retry_after = int(float(resp.headers.get("Retry-After", 0)))
                except Exception:
                    retry_after = 0
                detail_text = ""
                try:
                    detail_text = (resp.json() or {}).get("detail", "")
                except Exception:
                    detail_text = resp.text

                # For platform concurrency limit, wait progressively longer.
                wait_seconds = retry_after if retry_after > 0 else min(20 + attempt * 10, 120)
                logger.warning(
                    f"Batch submission throttled (attempt {attempt + 1}/{max_retries}): {detail_text}. "
                    f"Retry in {wait_seconds}s"
                )
                time.sleep(wait_seconds)
                continue

            return resp

        return resp

    def _save_result(self, result_dict: dict):
        """Thread-safe write to CSV."""
        with self.csv_lock:
            try:
                normalized = {key: result_dict.get(key, "") for key in self.csv_columns}
                df = pd.DataFrame([normalized], columns=self.csv_columns)
                
                # Append to CSV, create header if file doesn't exist
                write_header = not pd.io.common.file_exists(self.output_csv)
                df.to_csv(self.output_csv, mode='a', header=write_header, index=False)
            except Exception as e:
                logger.error(f"Failed to write result to CSV: {e}")

    @staticmethod
    def _retry_after_seconds(response, default: float = 5.0) -> float:
        value = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if value is None or value == "":
            return default
        try:
            return max(0.0, float(value))
        except Exception:
            return default

    def _fetch_simulation_status(self, url: str, *, context: str, max_retries: int = SIM_STATUS_FETCH_RETRIES):
        """Fetch simulation status JSON with bounded retries for transient read failures.

        Returns:
            ``(response, data, error_string)`` where ``error_string`` uses the prefix
            ``"connection_lost:"`` for transport-level failures (socket drop, timeout)
            so callers can trigger session refresh; ``"rate_limited:"`` for 429; and
            ``"status fetch failed"`` for other non-connection errors.
        """
        last_error = ""
        for attempt in range(max(1, int(max_retries))):
            response, conn_err = ace_lib.resilient_get(self.session, url, max_retries=1)
            if conn_err:
                last_error = conn_err
                logger.warning(
                    f"{context} status fetch connection error "
                    f"(attempt {attempt + 1}/{max_retries}): {conn_err}"
                )
                if attempt < max_retries - 1:
                    time.sleep(SIM_STATUS_FETCH_RETRY_DELAY)
                continue

            if response.status_code == 429:
                wait_seconds = self._retry_after_seconds(response, default=SIM_STATUS_FETCH_RETRY_DELAY)
                return response, None, f"rate_limited:{wait_seconds}"

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    f"{context} status fetch returned {response.status_code} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
            else:
                try:
                    data = response.json() if response.text else {}
                except Exception as exc:
                    last_error = f"invalid JSON: {exc}"
                    logger.warning(
                        f"{context} status JSON parse failed "
                        f"(attempt {attempt + 1}/{max_retries}): {exc}"
                    )
                else:
                    if isinstance(data, dict) and data:
                        return response, data, ""
                    last_error = "empty response JSON"
                    logger.warning(
                        f"{context} status response was empty "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )

            if attempt < max_retries - 1:
                time.sleep(SIM_STATUS_FETCH_RETRY_DELAY)

        if last_error and "connection_error" in last_error:
            return None, None, f"connection_lost: {last_error}"
        return None, None, last_error or "status fetch failed"

    def _maybe_run_stale_healthcheck(self, marker: dict, *, context: str, status_summary: str = "", force: bool = False):
        """Refresh authentication during long platform waits without marking alphas failed.

        When ``force=True`` the time-based threshold is skipped and the session is
        refreshed immediately — used by polling loops that detect persistent
        connection errors.
        """
        threshold = int(getattr(self, "stale_healthcheck_seconds", 0) or 0)
        if threshold <= 0 and not force:
            return
        now = time.time()
        next_at = float(marker.get("next_at") or 0)
        if not force and now < next_at:
            return
        marker["next_at"] = now + threshold if threshold > 0 else now + 900
        marker["count"] = int(marker.get("count") or 0) + 1
        if force:
            logger.warning(
                "[BRAIN reconnect] Persistent connection errors detected; refreshing BRAIN session. "
                f"context={context} elapsed={int(now - float(marker.get('started_at') or now))}s "
                f"check_count={marker['count']} {status_summary}".strip()
            )
        else:
            logger.warning(
                "[BRAIN healthcheck] Long-running simulation wait detected; refreshing BRAIN session. "
                f"context={context} elapsed={int(now - float(marker.get('started_at') or now))}s "
                f"check_count={marker['count']} {status_summary}".strip()
            )
        try:
            with self.session_lock:
                self.session = ace_lib.start_session()
            logger.info(f"[BRAIN healthcheck] Session refresh succeeded for {context}; continue polling.")
        except Exception as exc:
            logger.warning(f"[BRAIN healthcheck] Session refresh failed for {context}: {exc}; continue polling defensively.")

    def _poll_single_simulation(self, progress_url: str, alpha_input: dict, alpha_meta: dict):
        """
        Poll a single simulation location directly.

        A single alpha POST returns a simulation location, but it does not spawn child simulations.
        Treating that location as a batch parent creates false BATCH_SPAWN_FAILED rows.
        """
        fp = get_alpha_fingerprint(alpha_input)
        sim_id = progress_url.rstrip("/").split("/")[-1]
        wait_started = time.time()
        stale_marker = {"started_at": wait_started, "next_at": wait_started + self.stale_healthcheck_seconds, "count": 0}
        wait_limit_seconds = 60 * 60
        last_heartbeat = wait_started
        poll_count = 0
        rate_limit_count = 0
        retry_after_count = 0
        last_status = "UNKNOWN"
        last_progress = ""
        last_data = {}

        consecutive_conn_errors = 0

        logger.info(f"Polling single simulation directly ({progress_url})...")

        while time.time() - wait_started <= wait_limit_seconds:
            poll_count += 1
            self._maybe_run_stale_healthcheck(
                stale_marker,
                context=f"single:{sim_id}",
                status_summary=f"last_status={last_status} last_progress={last_progress}",
            )
            sim_resp, sim_data, fetch_error = self._fetch_simulation_status(
                progress_url,
                context=f"Single simulation {sim_id}",
            )

            if fetch_error.startswith("rate_limited:") and sim_resp is not None:
                rate_limit_count += 1
                consecutive_conn_errors = 0
                wait_seconds = self._retry_after_seconds(sim_resp, default=5.0)
                now = time.time()
                if now - last_heartbeat >= self.heartbeat_seconds:
                    elapsed = int(now - wait_started)
                    logger.warning(
                        "[BRAIN wait] Single simulation is rate-limited by the platform. "
                        f"sim_id={sim_id} elapsed={elapsed}s retry_after={wait_seconds}s "
                        f"polls={poll_count} rate_limits={rate_limit_count}. Still waiting."
                    )
                    last_heartbeat = now
                time.sleep(wait_seconds)
                continue

            if sim_data is None:
                if fetch_error.startswith("connection_lost:"):
                    consecutive_conn_errors += 1
                    logger.warning(
                        f"Single simulation {sim_id} connection lost "
                        f"(consecutive={consecutive_conn_errors}/3): {fetch_error}"
                    )
                    if consecutive_conn_errors >= 3:
                        self._maybe_run_stale_healthcheck(
                            stale_marker,
                            context=f"single:{sim_id}",
                            status_summary=f"last_status={last_status} consecutive_conn_errors={consecutive_conn_errors}",
                            force=True,
                        )
                        consecutive_conn_errors = 0
                else:
                    logger.warning(f"Single simulation {sim_id} status unavailable after retries: {fetch_error}")
                time.sleep(5)
                continue

            consecutive_conn_errors = 0

            last_data = sim_data
            status = sim_data.get("status")
            alpha_id = sim_data.get("alpha")
            progress = sim_data.get("progress", "")
            if status:
                last_status = status
            if progress != "":
                last_progress = progress

            retry_after_header = sim_resp.headers.get("Retry-After") or sim_resp.headers.get("retry-after")
            retry_after = self._retry_after_seconds(sim_resp, default=5.0)
            terminal = status in TERMINAL_SIM_STATUSES
            completed_with_alpha = bool(alpha_id) and (terminal or retry_after_header is None or retry_after <= 0)

            if terminal or completed_with_alpha:
                sim_status = status or ("COMPLETE" if alpha_id else "UNKNOWN")
                record = {
                    "fingerprint": fp,
                    **alpha_meta,
                    "sim_id": sim_id,
                    "status": sim_status,
                    "timestamp": time.time(),
                    "alpha_id": alpha_id,
                    "pnl": 0,
                    "sharpe": 0,
                    "turnover": 0,
                    "fitness": 0,
                }

                if sim_status in ["COMPLETED", "COMPLETE"] and alpha_id:
                    try:
                        full_stats = ace_lib.get_simulation_result_json(self.session, alpha_id)
                        if full_stats:
                            is_stats = full_stats.get("is", {})
                            record["pnl"] = is_stats.get("pnl", 0)
                            record["sharpe"] = is_stats.get("sharpe", 0)
                            record["turnover"] = is_stats.get("turnover", 0)
                            record["fitness"] = is_stats.get("fitness", 0)
                    except Exception as exc:
                        logger.error(f"Failed to fetch stats for alpha {alpha_id}: {exc}")
                elif sim_status in ["ERROR", "FAIL", "FAILED"]:
                    error_details = lookINTO_SimError_message(self.session, [f"/simulations/{sim_id}"])
                    if error_details:
                        record["error_details"] = error_details[0].get("message", "")

                self._save_result(record)
                if sim_status in ["COMPLETED", "COMPLETE"]:
                    self.completed_fingerprints.add(fp)
                    self._inc_stat("completed", 1)
                else:
                    self._inc_stat("failed", 1)
                return

            if retry_after_header is not None:
                retry_after_count += 1

            now = time.time()
            if now - last_heartbeat >= self.heartbeat_seconds:
                elapsed = int(now - wait_started)
                logger.info(
                    "[BRAIN wait] Single simulation is still running or queued on the platform. "
                    f"sim_id={sim_id} status={status} progress={progress} elapsed={elapsed}s "
                    f"retry_after={retry_after}s polls={poll_count} "
                    f"rate_limits={rate_limit_count} retry_after_responses={retry_after_count}. Still waiting."
                )
                last_heartbeat = now
            time.sleep(retry_after if retry_after_header is not None else 5)

        logger.error(
            "Single simulation timeout reached; marking simulation as timeout. "
            f"sim_id={sim_id} last_status={last_status} last_progress={last_progress}"
        )
        self._save_result(
            {
                "fingerprint": fp,
                **alpha_meta,
                "sim_id": sim_id,
                "status": "TIMEOUT",
                "timestamp": time.time(),
                "alpha_id": last_data.get("alpha", ""),
                "error": f"Single simulation timeout; last_status={last_status}; last_progress={last_progress}",
            }
        )
        self._inc_stat("failed", 1)

    def process_batch(self, alpha_batch: List[dict]):
        """
        Submits a batch of alphas, polls their individual status (avoiding multisimulation_progress),
        and records results.
        """
        # 1. Filter batch for duplicates
        clean_batch = []
        alpha_meta_by_fp = {}
        skipped_in_batch = 0
        
        for alpha in alpha_batch:
            fp = get_alpha_fingerprint(alpha)
            if fp in self.completed_fingerprints:
                skipped_in_batch += 1
                continue
            
            clean_batch.append(alpha)
            alpha_meta_by_fp[fp] = self._alpha_metadata(alpha)

        if skipped_in_batch > 0:
            self._inc_stat("skipped", skipped_in_batch)
            logger.info(f"Resume detected: skipped {skipped_in_batch} already completed alpha(s) in this batch.")

        if not clean_batch:
            with self.stats_lock:
                self.processed_alphas += len(alpha_batch)
            return

        with self.stats_lock:
            self.active_batches += 1
            active = self.active_batches
        logger.info(
            f"[Progress] Start batch | Active slots: {active}/{self.max_concurrency} | "
            f"Processed alphas: {self.processed_alphas}/{self.total_alphas}"
        )

        try:
            logger.info(f"Submitting batch of {len(clean_batch)} alphas...")
            self._inc_stat("submitted", len(clean_batch))
            # ace_lib.start_simulation handles the POST to /simulations
            submission_batch = [brain_submission_payload(alpha) for alpha in clean_batch]
            submit_payload = submission_batch[0] if len(submission_batch) == 1 else submission_batch
            resp = self._submit_with_retry(submit_payload)
            
            if resp.status_code not in [200, 201, 202]:
                logger.error(f"Batch submission failed: {resp.status_code} - {resp.text}")
                # Record failure for all in batch
                for alpha in clean_batch:
                    fp = get_alpha_fingerprint(alpha)
                    self._save_result({
                        "fingerprint": fp,
                        **alpha_meta_by_fp.get(fp, {}),
                        "status": "SUBMISSION_FAILED", 
                        "error": resp.text,
                        "timestamp": time.time()
                    })
                self._inc_stat("failed", len(clean_batch))
                return

            # 3. Get batch progress URL and initial Poll
            # The response to a list submission usually points to a parent simulation monitor
            progress_url = resp.headers.get("Location")
            if not progress_url:
                logger.error("No Location header in batch submission response.")
                return

            if len(clean_batch) == 1:
                alpha_input = clean_batch[0]
                fp = get_alpha_fingerprint(alpha_input)
                self._poll_single_simulation(progress_url, alpha_input, alpha_meta_by_fp.get(fp, {}))
                return

            # Wait for the batch to initialize and spawn children
            # We need to poll the PARENT to get the list of CHILDREN IDs
            children_ids = []
            max_wait_seconds = self.parent_wait_seconds
            started_at = time.time()
            parent_stale_marker = {
                "started_at": started_at,
                "next_at": started_at + self.stale_healthcheck_seconds,
                "count": 0,
            }
            parent_status = "UNKNOWN"
            last_parent_data = {}
            last_parent_heartbeat = started_at
            parent_poll_count = 0
            parent_rate_limit_count = 0
            
            logger.info(f"Waiting for batch to spawn children ({progress_url})...")
            while time.time() - started_at <= max_wait_seconds:
                parent_poll_count += 1
                self._maybe_run_stale_healthcheck(
                    parent_stale_marker,
                    context="batch_parent",
                    status_summary=f"parent_status={parent_status} polls={parent_poll_count}",
                )
                parent_resp, parent_data, fetch_error = self._fetch_simulation_status(
                    progress_url,
                    context="Batch parent",
                )
                if fetch_error.startswith("rate_limited:") and parent_resp is not None:
                    parent_rate_limit_count += 1
                    retry_after = self._retry_after_seconds(parent_resp, default=5.0)
                    now = time.time()
                    if now - last_parent_heartbeat >= self.heartbeat_seconds:
                        elapsed = int(now - started_at)
                        logger.warning(
                            "[BRAIN wait] Batch parent is rate-limited while spawning children. "
                            f"elapsed={elapsed}s retry_after={retry_after}s polls={parent_poll_count} "
                            f"rate_limits={parent_rate_limit_count}. This is usually platform queueing; still waiting."
                        )
                        last_parent_heartbeat = now
                    time.sleep(retry_after)
                    continue

                if parent_data is not None:
                    last_parent_data = parent_data
                    parent_status = parent_data.get("status")
                    
                    # Brain API typically returns a 'children' list
                    children = parent_data.get("children", [])
                    if children:
                        children_ids = children
                        break
                    
                    # While parent is in progress, API may return only {"progress": ...} + Retry-After
                    retry_after = parent_resp.headers.get("Retry-After") or parent_resp.headers.get("retry-after")
                    if retry_after:
                        now = time.time()
                        if now - last_parent_heartbeat >= self.heartbeat_seconds:
                            elapsed = int(now - started_at)
                            progress = parent_data.get("progress", "")
                            logger.info(
                                "[BRAIN wait] Batch parent has not spawned children yet. "
                                f"status={parent_status} progress={progress} elapsed={elapsed}s "
                                f"retry_after={retry_after}s polls={parent_poll_count}. Still waiting."
                            )
                            last_parent_heartbeat = now
                        time.sleep(float(retry_after))
                        continue

                    # If completed/error but still no children, stop waiting
                    if parent_status in TERMINAL_SIM_STATUSES and not children:
                        break
                elif fetch_error:
                    logger.warning(f"Batch parent status unavailable after retries: {fetch_error}")

                now = time.time()
                if now - last_parent_heartbeat >= self.heartbeat_seconds:
                    elapsed = int(now - started_at)
                    logger.info(
                        "[BRAIN wait] Batch parent is still initializing. "
                        f"status={parent_status} elapsed={elapsed}s polls={parent_poll_count}. Still waiting."
                    )
                    last_parent_heartbeat = now
                time.sleep(2)
            
            if not children_ids:
                # Single simulation mode: COMPLETE without children is valid.
                if len(clean_batch) == 1 and parent_status in ["COMPLETED", "COMPLETE", "ERROR", "FAIL", "FAILED"]:
                    alpha_input = clean_batch[0]
                    fp = get_alpha_fingerprint(alpha_input)
                    sim_id = progress_url.rstrip("/").split("/")[-1]
                    sim_status = last_parent_data.get("status", parent_status)
                    alpha_id = last_parent_data.get("alpha")

                    record = {
                        "fingerprint": fp,
                        **alpha_meta_by_fp.get(fp, {}),
                        "sim_id": sim_id,
                        "status": sim_status,
                        "timestamp": time.time(),
                        "alpha_id": alpha_id,
                        "pnl": 0,
                        "sharpe": 0,
                        "turnover": 0,
                        "fitness": 0,
                    }

                    if sim_status in ["COMPLETED", "COMPLETE"] and alpha_id:
                        try:
                            full_stats = ace_lib.get_simulation_result_json(self.session, alpha_id)
                            if full_stats:
                                is_stats = full_stats.get("is", {})
                                record["pnl"] = is_stats.get("pnl", 0)
                                record["sharpe"] = is_stats.get("sharpe", 0)
                                record["turnover"] = is_stats.get("turnover", 0)
                                record["fitness"] = is_stats.get("fitness", 0)
                        except Exception as e:
                            logger.error(f"Failed to fetch stats for alpha {alpha_id}: {e}")
                    elif sim_status in ["ERROR", "FAIL", "FAILED"]:
                        error_details = lookINTO_SimError_message(self.session, [f"/simulations/{sim_id}"])
                        if error_details:
                            record["error_details"] = error_details[0].get("message", "")

                    self._save_result(record)
                    if sim_status in ["COMPLETED", "COMPLETE"]:
                        self.completed_fingerprints.add(fp)
                        self._inc_stat("completed", 1)
                    else:
                        self._inc_stat("failed", 1)
                    return

                logger.error(f"Failed to retrieve children IDs. Parent status: {parent_status}")
                # Log failure for checking
                for alpha in clean_batch:
                    fp = get_alpha_fingerprint(alpha)
                    self._save_result({
                        "fingerprint": fp,
                        **alpha_meta_by_fp.get(fp, {}),
                        "status": "BATCH_SPAWN_FAILED",
                        "error": f"Parent status: {parent_status}",
                        "timestamp": time.time()
                    })
                self._inc_stat("failed", len(clean_batch))
                return

            logger.info(f"Batch spawned {len(children_ids)} children. Polling individually...")

            # 4. Poll Children Individually
            # Store mapping of sim_id -> index in clean_batch to map back to inputs
            if len(children_ids) != len(clean_batch):
                logger.warning(f"Count mismatch: sent {len(clean_batch)}, got {len(children_ids)} children.")
                # If mismatch, we can't reliably map back to clean_batch by index if strictly relying on position.
                # However, usually Brain preserves order. We proceed with index mapping.
            
            # active_sims: map sim_id -> index in clean_batch
            active_sims = {child_id: idx for idx, child_id in enumerate(children_ids)}
            results = {} # Store final results by sim_id
            
            # Polling loop (bounded wait to avoid infinite hangs)
            child_wait_started = time.time()
            child_stale_marker = {
                "started_at": child_wait_started,
                "next_at": child_wait_started + self.stale_healthcheck_seconds,
                "count": 0,
            }
            child_wait_limit_seconds = self.child_wait_seconds
            last_child_heartbeat = child_wait_started
            child_poll_count = 0
            child_rate_limit_count = 0
            child_retry_after_count = 0
            child_consecutive_conn_errors = 0
            last_status_by_sim = {sim_id: "UNKNOWN" for sim_id in active_sims}
            while active_sims:
                if time.time() - child_wait_started > child_wait_limit_seconds:
                    logger.error("Child polling timeout reached; marking remaining simulations as timeout.")
                    for sim_id in list(active_sims.keys()):
                        results[sim_id] = {"status": "TIMEOUT", "alpha": None}
                    active_sims.clear()
                    break

                self._maybe_run_stale_healthcheck(
                    child_stale_marker,
                    context="batch_children",
                    status_summary=f"remaining={len(active_sims)} polls={child_poll_count}",
                )

                # Copy keys to iterate while modifying
                current_sim_ids = list(active_sims.keys())
                any_success_this_cycle = False

                for sim_id in current_sim_ids:
                    sim_url = f"{ace_lib.brain_api_url}/simulations/{sim_id}"

                    try:
                        child_poll_count += 1
                        sim_resp, sim_data, fetch_error = self._fetch_simulation_status(
                            sim_url,
                            context=f"Child simulation {sim_id}",
                        )

                        # Handle rate limits or temporary issues
                        if fetch_error.startswith("rate_limited:") and sim_resp is not None:
                            child_rate_limit_count += 1
                            continue

                        if sim_data is None:
                            if fetch_error.startswith("connection_lost:"):
                                child_consecutive_conn_errors += 1
                                logger.warning(
                                    f"Child simulation {sim_id} connection lost "
                                    f"(consecutive={child_consecutive_conn_errors}/3): {fetch_error}"
                                )
                            else:
                                logger.warning(f"Sim {sim_id} status unavailable after retries: {fetch_error}")
                            continue

                        any_success_this_cycle = True

                        status = sim_data.get("status")
                        if status:
                            last_status_by_sim[sim_id] = status

                        if status in TERMINAL_SIM_STATUSES:
                            # Simulation finished
                            results[sim_id] = sim_data
                            # Remove from active set
                            if sim_id in active_sims:
                                del active_sims[sim_id]
                            continue

                        if sim_resp is not None and ("Retry-After" in sim_resp.headers or "retry-after" in sim_resp.headers):
                            child_retry_after_count += 1
                            continue 
                            
                    except Exception as e:
                        logger.error(f"Error polling child {sim_id}: {e}")

                if any_success_this_cycle:
                    child_consecutive_conn_errors = 0
                elif child_consecutive_conn_errors >= 3:
                    self._maybe_run_stale_healthcheck(
                        child_stale_marker,
                        context="batch_children",
                        status_summary=f"remaining={len(active_sims)} consecutive_conn_errors={child_consecutive_conn_errors}",
                        force=True,
                    )
                    child_consecutive_conn_errors = 0

                if active_sims:
                    now = time.time()
                    if now - last_child_heartbeat >= self.heartbeat_seconds:
                        elapsed = int(now - child_wait_started)
                        remaining_statuses = {}
                        for sim_id in active_sims:
                            status = last_status_by_sim.get(sim_id, "UNKNOWN")
                            remaining_statuses[status] = remaining_statuses.get(status, 0) + 1
                        logger.info(
                            "[BRAIN wait] Child simulations are still running or queued on the platform. "
                            f"remaining={len(active_sims)} elapsed={elapsed}s polls={child_poll_count} "
                            f"rate_limits={child_rate_limit_count} retry_after_responses={child_retry_after_count} "
                            f"last_status_counts={remaining_statuses}. Still waiting; this usually means BRAIN is busy."
                        )
                        last_child_heartbeat = now
                    time.sleep(3) # Wait before next cycle

            # 5. Process and Save Results
            # We iterate through indices of clean_batch to ensure we save a result for every input alpha
            for i, alpha_input in enumerate(clean_batch):
                fp = get_alpha_fingerprint(alpha_input)
                
                # Try to get the corresponding sim_id if available
                child_id = children_ids[i] if i < len(children_ids) else None
                
                if child_id and child_id in results:
                    res_data = results[child_id]
                    sim_status = res_data.get("status", "UNKNOWN")
                    alpha_id = res_data.get("alpha")
                    
                    record = {
                        "fingerprint": fp,
                        **alpha_meta_by_fp.get(fp, {}),
                        "sim_id": child_id,
                        "status": sim_status,
                        "timestamp": time.time(),
                        "alpha_id": alpha_id,
                        "pnl": 0,
                        "sharpe": 0,
                        "turnover": 0,
                        "fitness": 0
                    }
                    
                    # If successful, fetch detailed result
                    if sim_status in ["COMPLETED", "COMPLETE"] and alpha_id:
                        try:
                            # Use ace_lib logic to get full result
                            full_stats = ace_lib.get_simulation_result_json(self.session, alpha_id)
                            if full_stats:
                                is_stats = full_stats.get("is", {})
                                record["pnl"] = is_stats.get("pnl", 0)
                                record["sharpe"] = is_stats.get("sharpe", 0)
                                record["turnover"] = is_stats.get("turnover", 0)
                                record["fitness"] = is_stats.get("fitness", 0)
                                
                        except Exception as e:
                            logger.error(f"Failed to fetch stats for alpha {alpha_id}: {e}")

                    # If failed, look into error
                    elif sim_status in ["ERROR", "FAIL"]:
                        error_details = lookINTO_SimError_message(self.session, [f"/simulations/{child_id}"])
                        if error_details:
                            # Save first error found
                            record["error_details"] = error_details[0].get("message", "")
                            # record["raw_error"] = str(error_details[0]) # Optional: too verbose for CSV usually

                    self._save_result(record)
                    # Mark as completed in memory so we don't re-run in this session
                    if sim_status in ["COMPLETED", "COMPLETE"]:
                        self.completed_fingerprints.add(fp)
                        self._inc_stat("completed", 1)
                    else:
                        self._inc_stat("failed", 1)
                else:
                    # Case where we didn't get a result for this index (e.g. child count mismatch or lost)
                    self._save_result({
                        "fingerprint": fp,
                        **alpha_meta_by_fp.get(fp, {}),
                        "status": "MISSING_RESULT",
                        "error": "No child simulation found for this index",
                        "timestamp": time.time()
                    })
                    self._inc_stat("failed", 1)

        except Exception as e:
            logger.error(f"Critical error in batch processing: {e}", exc_info=True)
        finally:
            with self.stats_lock:
                self.active_batches -= 1
                self.processed_alphas += len(clean_batch) + skipped_in_batch
                active = self.active_batches
            logger.info(
                f"[Progress] Finish batch | Active slots: {active}/{self.max_concurrency} | "
                f"Processed alphas: {self.processed_alphas}/{self.total_alphas} | "
                f"Completed: {self.stats['completed']} | Failed: {self.stats['failed']}"
            )

    def run(self, alpha_list: List[dict], batch_size: int | None = None, concurrency: int | None = None):
        """
        Main entry point to run the simulation manager.
        """
        self._reset_run_stats()
        region = ""
        if alpha_list:
            first_settings = alpha_list[0].get("settings", {}) if isinstance(alpha_list[0], dict) else {}
            region = str(first_settings.get("region") or "").upper()
        (
            safe_batch_size,
            safe_concurrency,
            default_batch_size,
            default_concurrency,
            max_batch_size,
            max_concurrency,
        ) = _normalize_slot_limits(
            region,
            batch_size,
            concurrency,
        )
        if concurrency is None:
            concurrency = safe_concurrency
            logger.info(
                f"Using default concurrency={concurrency} for region={region or 'UNKNOWN'} "
                f"(default={default_concurrency}, max={max_concurrency})."
            )
        elif concurrency != safe_concurrency:
            logger.warning(
                f"Requested concurrency={concurrency} is outside conservative BRAIN limits for region={region or 'UNKNOWN'}; "
                f"using concurrency={safe_concurrency} (max={max_concurrency})."
            )
            concurrency = safe_concurrency
        if batch_size is None:
            batch_size = safe_batch_size
            logger.info(
                f"Using default batch_size={batch_size} for region={region or 'UNKNOWN'} "
                f"(default={default_batch_size}, max={max_batch_size})."
            )
        elif batch_size != safe_batch_size:
            logger.warning(
                f"Requested batch_size={batch_size} is outside conservative BRAIN limits for region={region or 'UNKNOWN'}; "
                f"using batch_size={safe_batch_size} (max={max_batch_size})."
            )
            batch_size = safe_batch_size
        total = len(alpha_list)
        input_fingerprints = {get_alpha_fingerprint(alpha) for alpha in alpha_list}
        already_completed_in_input = len(input_fingerprints & self.completed_fingerprints)
        pending_in_input = len(input_fingerprints - self.completed_fingerprints)

        self.total_alphas = total
        self.max_concurrency = concurrency
        logger.info(f"Starting simulation run for {total} alphas. Batch size: {batch_size}, Workers: {concurrency}")
        logger.info(
            f"Resume check: recognized {already_completed_in_input}/{len(input_fingerprints)} completed from {self.output_csv}; "
            f"pending {pending_in_input}."
        )
        
        # Split into batches
        batches = [alpha_list[i:i + batch_size] for i in range(0, total, batch_size)]
        
        # Run batches in parallel using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(self.process_batch, batch) for batch in batches]
            
            # Wait for all to complete
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Worker exception: {e}")

        logger.info(
            f"All batches processed. Run summary -> skipped: {self.stats['skipped']}, "
            f"submitted: {self.stats['submitted']}, completed: {self.stats['completed']}, failed: {self.stats['failed']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Batch simulate alphas with resume support.")
    parser.add_argument("--alpha-json", default="data/alpha_list.json", help="Path to alpha list JSON file")
    parser.add_argument(
        "--output-csv",
        default=None,
        help="Path to status CSV file. If omitted, auto uses outputs/<alpha_json_filename>_simulation_status.csv",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Number of alphas per submission batch. Default: 10, or 4 for GLB. Max: 10.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of concurrent batches. Default: 4. Max: 8, or 4 for GLB.",
    )
    parser.add_argument("--config", default="configs/config.json", help="Path to credential config JSON")
    parser.add_argument("--detached", action="store_true", help="Launch batch simulation in background and return immediately")
    parser.add_argument("--task-id", default=None, help="Optional task id for detached mode")
    parser.add_argument("--tasks-dir", default="outputs/tasks", help="Task directory root for detached mode")
    parser.add_argument("--status", default=None, help="Show detached task status by task id and exit")
    parser.add_argument("--tail-lines", type=int, default=40, help="Tail lines for --status output")
    parser.add_argument("--heartbeat-seconds", type=int, default=60, help="Seconds between BRAIN platform wait log messages")
    parser.add_argument("--parent-wait-minutes", type=int, default=30, help="Minutes to wait for a batch parent to spawn children")
    parser.add_argument("--child-wait-minutes", type=int, default=60, help="Minutes to wait for child simulations to finish")
    parser.add_argument(
        "--stale-healthcheck-minutes",
        type=int,
        default=15,
        help="Minutes of continuous platform wait before refreshing the BRAIN session; set 0 to disable",
    )
    parser.add_argument(
        "--auth-retries",
        type=int,
        default=None,
        help="Authentication transport retry attempts before batchSim startup fails. Default: BRAIN_AUTH_MAX_RETRIES or 8.",
    )
    parser.add_argument(
        "--auth-max-delay",
        type=float,
        default=None,
        help="Maximum seconds between authentication transport retries. Default: BRAIN_AUTH_MAX_DELAY or 60.",
    )
    parser.add_argument(
        "--auth-timeout",
        type=float,
        default=None,
        help="Seconds to wait for each authentication request. Default: BRAIN_AUTH_TIMEOUT_SECONDS or 30.",
    )
    args = parser.parse_args()

    tasks_dir = Path(args.tasks_dir)
    if not tasks_dir.is_absolute():
        tasks_dir = (SKILL_ROOT / tasks_dir).resolve()
    else:
        tasks_dir = tasks_dir.resolve()

    if args.status and str(args.status).strip():
        raise SystemExit(_print_task_status(tasks_dir=tasks_dir, task_id=str(args.status).strip(), tail_lines=args.tail_lines))

    if args.detached:
        task_id = args.task_id.strip() if args.task_id and args.task_id.strip() else f"sim_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        child_cmd = _build_detached_child_cmd(Path(__file__).resolve(), sys.argv[1:])
        mode = f"batch_bs{args.batch_size if args.batch_size is not None else 'auto'}_cc{args.concurrency if args.concurrency is not None else 'auto'}"
        try:
            pid, task_dir = _launch_detached(cmd=child_cmd, cwd=SKILL_ROOT, task_id=task_id, tasks_dir=tasks_dir, mode=mode)
        except Exception as exc:
            logger.error(f"Failed to launch detached process: {exc}")
            raise SystemExit(2)

        print("Detached task launched.")
        print(f"task_id={task_id}")
        print(f"pid={pid}")
        print(f"task_dir={task_dir}")
        print(f"stdout_log={task_dir / 'stdout.log'}")
        print(f"stderr_log={task_dir / 'stderr.log'}")
        raise SystemExit(0)

    resolved_config = resolve_input_path(args.config, "config.json")
    load_credentials_from_config(str(resolved_config))

    alpha_json_path = resolve_input_path(args.alpha_json, "alpha_list.json")
    if args.output_csv:
        output_csv_path = resolve_output_path(args.output_csv)
    else:
        output_csv_path = build_default_output_csv_path(alpha_json_path)
    logger.info(f"Using output CSV: {output_csv_path}")

    with open(alpha_json_path, "r", encoding="utf-8") as file:
        alpha_list = json.load(file)

    if not isinstance(alpha_list, list):
        raise ValueError(f"alpha json must be a list, got: {type(alpha_list)}")

    try:
        session = ace_lib.start_session(
            max_auth_retries=args.auth_retries,
            auth_max_delay=args.auth_max_delay,
            auth_timeout=args.auth_timeout,
        )
    except Exception as exc:
        logger.error(
            "Unable to authenticate with BRAIN after transport retries: "
            f"{type(exc).__name__}: {exc}. If this is an SSL EOF or DNS error, "
            "verify the terminal is routed through the same VPN/TUN/proxy path as the browser."
        )
        raise
    simulator = BatchSimulator(session, output_csv=str(output_csv_path))
    simulator.heartbeat_seconds = max(10, int(args.heartbeat_seconds))
    simulator.parent_wait_seconds = max(5, int(args.parent_wait_minutes)) * 60
    simulator.child_wait_seconds = max(10, int(args.child_wait_minutes)) * 60
    simulator.stale_healthcheck_seconds = max(0, int(args.stale_healthcheck_minutes)) * 60
    simulator.run(alpha_list, batch_size=args.batch_size, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
