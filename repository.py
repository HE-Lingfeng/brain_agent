from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import RunConfig, RunStage
from .utils import now_iso


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  stage TEXT NOT NULL,
  stop_reason TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  pid INTEGER,
  status TEXT NOT NULL,
  stdout_path TEXT,
  stderr_path TEXT,
  retry_count INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  source_stage TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  expression TEXT NOT NULL,
  idea_file TEXT,
  alpha_id TEXT,
  status TEXT NOT NULL,
  source TEXT,
  selection_score REAL DEFAULT 0,
  score_breakdown TEXT DEFAULT '{}',
  parent_candidate_id INTEGER,
  variant_strategy TEXT DEFAULT '',
  variant_params TEXT DEFAULT '{}',
  lineage_json TEXT DEFAULT '{}',
  thesis_json TEXT DEFAULT '{}',
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS sim_results (
  sim_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  candidate_id INTEGER,
  fingerprint TEXT NOT NULL,
  simulation_fingerprint TEXT,
  alpha_id TEXT,
  sim_id TEXT,
  status TEXT,
  pnl REAL,
  sharpe REAL,
  turnover REAL,
  fitness REAL,
  returns REAL,
  drawdown REAL,
  error TEXT,
  failure_tags TEXT,
  repair_objectives TEXT,
  diagnosis_json TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_checks (
  gate_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  candidate_id INTEGER,
  alpha_id TEXT,
  submission_check TEXT,
  self_corr_check TEXT,
  prod_corr_check TEXT,
  weight_check TEXT,
  subuniverse_check TEXT,
  gate_status TEXT DEFAULT 'complete',
  error_type TEXT DEFAULT '',
  incomplete_checks TEXT DEFAULT '',
  error TEXT DEFAULT '',
  passed INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  iteration INTEGER NOT NULL,
  action TEXT NOT NULL,
  reason TEXT,
  input_candidates_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_tags (
  tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  candidate_id INTEGER NOT NULL,
  tag TEXT NOT NULL,
  source TEXT DEFAULT '',
  metadata_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(run_id, candidate_id, tag, source)
);
"""


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._ensure_column("sim_results", "simulation_fingerprint", "TEXT")
        self._ensure_column("sim_results", "failure_tags", "TEXT")
        self._ensure_column("sim_results", "repair_objectives", "TEXT")
        self._ensure_column("sim_results", "diagnosis_json", "TEXT")
        self._ensure_column("candidates", "selection_score", "REAL DEFAULT 0")
        self._ensure_column("candidates", "score_breakdown", "TEXT DEFAULT '{}'")
        self._ensure_column("candidates", "parent_candidate_id", "INTEGER")
        self._ensure_column("candidates", "variant_strategy", "TEXT DEFAULT ''")
        self._ensure_column("candidates", "variant_params", "TEXT DEFAULT '{}'")
        self._ensure_column("candidates", "lineage_json", "TEXT DEFAULT '{}'")
        self._ensure_column("candidates", "thesis_json", "TEXT DEFAULT '{}'")
        self._ensure_column("gate_checks", "gate_status", "TEXT DEFAULT 'complete'")
        self._ensure_column("gate_checks", "error_type", "TEXT DEFAULT ''")
        self._ensure_column("gate_checks", "incomplete_checks", "TEXT DEFAULT ''")
        self._ensure_column("gate_checks", "error", "TEXT DEFAULT ''")
        self._ensure_column("candidate_tags", "source", "TEXT DEFAULT ''")
        self._ensure_column("candidate_tags", "metadata_json", "TEXT DEFAULT '{}'")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row["name"]) for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def create_run(self, run_id: str, config: RunConfig) -> None:
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO runs(run_id, config_json, stage, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, json.dumps(config.to_dict(), ensure_ascii=False), RunStage.INIT.value, ts, ts),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def update_run_stage(self, run_id: str, stage: str, stop_reason: str | None = None) -> None:
        if stop_reason is None:
            self.conn.execute("UPDATE runs SET stage = ?, updated_at = ? WHERE run_id = ?", (stage, now_iso(), run_id))
        else:
            self.conn.execute(
                "UPDATE runs SET stage = ?, stop_reason = ?, updated_at = ? WHERE run_id = ?",
                (stage, stop_reason, now_iso(), run_id),
            )
        self.conn.commit()

    def add_artifact(self, run_id: str, kind: str, path: str | Path, sha256: str = "", source_stage: str = "") -> None:
        existing = self.conn.execute(
            """
            SELECT 1 FROM artifacts
            WHERE run_id = ? AND kind = ? AND path = ? AND COALESCE(sha256, '') = ? AND COALESCE(source_stage, '') = ?
            LIMIT 1
            """,
            (run_id, kind, str(path), sha256 or "", source_stage or ""),
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            "INSERT INTO artifacts(run_id, kind, path, sha256, source_stage, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, kind, str(path), sha256, source_stage, now_iso()),
        )
        self.conn.commit()

    def create_task(
        self,
        run_id: str,
        task_id: str,
        adapter: str,
        *,
        pid: int | None = None,
        status: str = "running",
        stdout_path: str | Path = "",
        stderr_path: str | Path = "",
    ) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO tasks(task_id, run_id, adapter, pid, status, stdout_path, stderr_path, retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              pid=excluded.pid,
              status=excluded.status,
              stdout_path=excluded.stdout_path,
              stderr_path=excluded.stderr_path,
              updated_at=excluded.updated_at
            """,
            (task_id, run_id, adapter, pid, status, str(stdout_path), str(stderr_path), ts, ts),
        )
        self.conn.commit()

    def update_task_status(self, task_id: str, status: str) -> None:
        self.conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?", (status, now_iso(), task_id))
        self.conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def upsert_candidate(
        self,
        run_id: str,
        expression: str,
        fingerprint: str,
        *,
        status: str,
        idea_file: str = "",
        alpha_id: str = "",
        source: str = "",
        parent_candidate_id: int | None = None,
        variant_strategy: str = "",
        variant_params: dict[str, Any] | None = None,
        lineage: dict[str, Any] | None = None,
        thesis: dict[str, Any] | None = None,
    ) -> int:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO candidates(
              run_id, expression, idea_file, alpha_id, status, source,
              parent_candidate_id, variant_strategy, variant_params, lineage_json, thesis_json,
              fingerprint, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, fingerprint) DO UPDATE SET
              expression=excluded.expression,
              idea_file=COALESCE(NULLIF(excluded.idea_file, ''), candidates.idea_file),
              alpha_id=COALESCE(NULLIF(excluded.alpha_id, ''), candidates.alpha_id),
              status=excluded.status,
              source=COALESCE(NULLIF(excluded.source, ''), candidates.source),
              parent_candidate_id=COALESCE(excluded.parent_candidate_id, candidates.parent_candidate_id),
              variant_strategy=COALESCE(NULLIF(excluded.variant_strategy, ''), candidates.variant_strategy),
              variant_params=COALESCE(NULLIF(excluded.variant_params, '{}'), candidates.variant_params),
              lineage_json=COALESCE(NULLIF(excluded.lineage_json, '{}'), candidates.lineage_json),
              thesis_json=COALESCE(NULLIF(excluded.thesis_json, '{}'), candidates.thesis_json),
              updated_at=excluded.updated_at
            """,
            (
                run_id,
                expression,
                idea_file,
                alpha_id,
                status,
                source,
                parent_candidate_id,
                variant_strategy,
                json.dumps(variant_params or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(lineage or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(thesis or {}, ensure_ascii=False, sort_keys=True),
                fingerprint,
                ts,
                ts,
            ),
        )
        row = self.conn.execute(
            "SELECT candidate_id FROM candidates WHERE run_id = ? AND fingerprint = ?", (run_id, fingerprint)
        ).fetchone()
        self.conn.commit()
        return int(row["candidate_id"])

    def update_candidate_status(self, run_id: str, fingerprint: str, status: str, alpha_id: str = "") -> None:
        if alpha_id:
            self.conn.execute(
                "UPDATE candidates SET status = ?, alpha_id = ?, updated_at = ? WHERE run_id = ? AND fingerprint = ?",
                (status, alpha_id, now_iso(), run_id, fingerprint),
            )
        else:
            self.conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ? WHERE run_id = ? AND fingerprint = ?",
                (status, now_iso(), run_id, fingerprint),
            )
        self.conn.commit()

    def update_candidate_score(
        self,
        run_id: str,
        candidate_id: int,
        score: float,
        breakdown: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            UPDATE candidates
            SET selection_score = ?, score_breakdown = ?, updated_at = ?
            WHERE run_id = ? AND candidate_id = ?
            """,
            (float(score), json.dumps(breakdown, ensure_ascii=False), now_iso(), run_id, int(candidate_id)),
        )
        self.conn.commit()

    def add_sim_result(self, run_id: str, result: dict[str, Any]) -> None:
        candidate_id = result.get("candidate_id")
        simulation_fingerprint = result.get("simulation_fingerprint", result.get("fingerprint", ""))
        if self.has_sim_result(
            run_id,
            str(simulation_fingerprint or ""),
            str(result.get("sim_id") or ""),
            str(result.get("alpha_id") or ""),
        ):
            return
        self.conn.execute(
            """
            INSERT INTO sim_results(
              run_id, candidate_id, fingerprint, simulation_fingerprint, alpha_id, sim_id, status, pnl, sharpe, turnover,
              fitness, returns, drawdown, error, failure_tags, repair_objectives, diagnosis_json, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                candidate_id,
                result.get("fingerprint", ""),
                simulation_fingerprint,
                result.get("alpha_id", ""),
                result.get("sim_id", ""),
                result.get("status", ""),
                result.get("pnl"),
                result.get("sharpe"),
                result.get("turnover"),
                result.get("fitness"),
                result.get("returns"),
                result.get("drawdown"),
                result.get("error", ""),
                ",".join(result.get("failure_tags") or []),
                ",".join(result.get("repair_objectives") or []),
                json.dumps(result.get("diagnosis") or {}, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()

    def has_sim_result(self, run_id: str, simulation_fingerprint: str, sim_id: str, alpha_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM sim_results
            WHERE run_id = ? AND simulation_fingerprint = ? AND sim_id = ? AND alpha_id = ?
            LIMIT 1
            """,
            (run_id, simulation_fingerprint, sim_id, alpha_id),
        ).fetchone()
        return row is not None

    def add_gate_check(self, run_id: str, result: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO gate_checks(
              run_id, candidate_id, alpha_id, submission_check, self_corr_check, prod_corr_check,
              weight_check, subuniverse_check, gate_status, error_type, incomplete_checks, error,
              passed, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.get("candidate_id"),
                result.get("alpha_id", ""),
                result.get("submission_check", ""),
                result.get("self_corr_check", ""),
                result.get("prod_corr_check", ""),
                result.get("weight_check", ""),
                result.get("subuniverse_check", ""),
                result.get("gate_status", "complete"),
                result.get("error_type", ""),
                ",".join(result.get("incomplete_checks") or []),
                result.get("error", ""),
                1 if result.get("passed") else 0,
                json.dumps(result, ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()

    def add_decision(self, run_id: str, iteration: int, action: str, reason: str, inputs: Iterable[dict[str, Any]]) -> None:
        self.conn.execute(
            "INSERT INTO decisions(run_id, iteration, action, reason, input_candidates_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, iteration, action, reason, json.dumps(list(inputs), ensure_ascii=False), now_iso()),
        )
        self.conn.commit()

    def add_candidate_tag(
        self,
        run_id: str,
        candidate_id: int,
        tag: str,
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO candidate_tags(run_id, candidate_id, tag, source, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                int(candidate_id),
                str(tag),
                str(source or ""),
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                now_iso(),
            ),
        )
        self.conn.commit()

    def list_candidate_tags(self, run_id: str, candidate_id: int | None = None) -> list[dict[str, Any]]:
        if candidate_id is None:
            rows = self.conn.execute(
                "SELECT * FROM candidate_tags WHERE run_id = ? ORDER BY tag_id",
                (run_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM candidate_tags WHERE run_id = ? AND candidate_id = ? ORDER BY tag_id",
                (run_id, int(candidate_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_sim_results_by_candidate(self, run_id: str) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM sim_results
            WHERE run_id = ? AND candidate_id IS NOT NULL
            ORDER BY sim_result_id
            """,
            (run_id,),
        ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            result[int(item["candidate_id"])] = item
        return result

    def list_rows(self, table: str, run_id: str) -> list[dict[str, Any]]:
        if table not in {"runs", "tasks", "artifacts", "candidates", "sim_results", "gate_checks", "decisions", "candidate_tags"}:
            raise ValueError(f"Unsupported table: {table}")
        if table == "runs":
            rows = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchall()
        else:
            rows = self.conn.execute(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY 1", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def find_candidates_by_status(self, run_id: str, statuses: list[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM candidates WHERE run_id = ? AND status IN ({placeholders}) ORDER BY candidate_id",
            (run_id, *statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_artifacts(self, run_id: str, kind: str | None = None, source_stage: str | None = None) -> list[dict[str, Any]]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if source_stage:
            clauses.append("source_stage = ?")
            params.append(source_stage)
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE " + " AND ".join(clauses) + " ORDER BY artifact_id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def count_candidates(self, run_id: str, status: str | None = None) -> int:
        if status:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE run_id = ? AND status = ?", (run_id, status)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM candidates WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["n"])

    def count_sim_results_by_candidate(self, run_id: str) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT candidate_id, COUNT(*) AS cnt FROM sim_results WHERE run_id = ? AND candidate_id IS NOT NULL GROUP BY candidate_id",
            (run_id,),
        ).fetchall()
        return {int(row["candidate_id"]): int(row["cnt"]) for row in rows}

    def count_retryable_sim_results_by_candidate(self, run_id: str) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT candidate_id, COUNT(*) AS cnt FROM sim_results WHERE run_id = ? AND candidate_id IS NOT NULL AND failure_tags LIKE '%sim_retryable%' GROUP BY candidate_id",
            (run_id,),
        ).fetchall()
        return {int(row["candidate_id"]): int(row["cnt"]) for row in rows}
