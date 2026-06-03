from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from brain_agent.core.models import RunConfig
from brain_agent.core.progress import build_simulation_progress, render_simulation_progress
from brain_agent.core.repository import Repository
from brain_agent.core.runtime import ensure_runtime, get_runtime_paths
from brain_agent.core.utils import expression_fingerprint, write_json


class SimulationProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = get_runtime_paths("progress_run", self.root)
        ensure_runtime(self.paths)
        self.repo = Repository(self.paths.db_path)
        self.repo.create_run(
            "progress_run",
            RunConfig(
                dataset="analyst7",
                region="USA",
                delay=1,
                universe="TOP3000",
                data_type="MATRIX",
            ),
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.tmp.cleanup()

    def test_current_batch_progress_filters_cumulative_status_csv(self) -> None:
        settings = {"region": "USA", "delay": 1}
        current_rows = [{"type": "REGULAR", "settings": settings, "regular": f"rank(current_{idx})"} for idx in range(60)]
        alpha_json = self.paths.artifacts_dir / "03_simulate" / "input" / "alpha_list_worker_batch7.json"
        write_json(alpha_json, current_rows)

        output_csv = self.paths.artifacts_dir / "03_simulate" / "simulation_status.csv"
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["fingerprint", "regular_expression", "settings_json", "status"]
        with output_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for idx in range(781):
                expression = f"rank(old_{idx})"
                writer.writerow(
                    {
                        "fingerprint": expression_fingerprint(expression),
                        "regular_expression": expression,
                        "settings_json": json.dumps(settings),
                        "status": "COMPLETE",
                    }
                )
            for row in current_rows[:12]:
                expression = row["regular"]
                writer.writerow(
                    {
                        "fingerprint": expression_fingerprint(expression),
                        "regular_expression": expression,
                        "settings_json": json.dumps(settings),
                        "status": "COMPLETE",
                    }
                )

        task_dir = self.paths.run_dir / "tasks" / "task_batch7"
        task_dir.mkdir(parents=True)
        stdout_path = task_dir / "stdout.log"
        stdout_path.write_text("", encoding="utf-8")
        (task_dir / "meta.json").write_text(
            json.dumps(
                {
                    "cmd": [
                        "batch_simulator.py",
                        "--alpha-json",
                        str(alpha_json),
                        "--output-csv",
                        str(output_csv),
                        "--batch-size",
                        "10",
                        "--concurrency",
                        "8",
                    ],
                    "cwd": str(self.root),
                }
            ),
            encoding="utf-8",
        )
        self.repo.create_task(
            "progress_run",
            "task_batch7",
            "batchSim",
            pid=None,
            status="completed",
            stdout_path=stdout_path,
            stderr_path=task_dir / "stderr.log",
        )

        progress = build_simulation_progress(self.repo, "progress_run", self.paths.run_dir)
        self.assertEqual(60, progress["total"])
        self.assertEqual(12, progress["done"])
        self.assertEqual(20.0, progress["percent"])
        self.assertIn("12/60 (20.0%)", render_simulation_progress(progress))


if __name__ == "__main__":
    unittest.main()
