from __future__ import annotations

import csv
import base64
import io
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from brain_agent.adapters import (
    BatchSimAdapter,
    EnhanceTemplateAdapter,
    InspectRawTemplateAdapter,
    MakeSomeGemAdapter,
    SubmissionGateAdapter,
    _field_factory_alpha_rows,
    _limited_alpha_list,
)
from brain_agent.cli import main
from brain_agent.credentials import load_credentials
from brain_agent.decision import DecisionEngine, _parse_llm_actions
from brain_agent.diagnostics import diagnose_sim_result
from brain_agent.forum import analyze_forum_post, analyze_search_results, summarize_forum_learning_with_llm
from brain_agent.knowledge import approve_forum_lesson, load_forum_learning_report, render_approved_lessons_prompt
from brain_agent.memory import AlphaMemory, build_scoring_context, extract_field_families, extract_operators
from brain_agent.models import AdapterResult, CandidateStatus, RunConfig
from brain_agent.optimization import optimization_tags, select_optimization_parents
from brain_agent.prompting import prompt_env
from brain_agent.quota_allocator import allocate_simulation_quota
from brain_agent.reporting import write_report
from brain_agent.repository import Repository
from brain_agent.runtime import ensure_runtime, get_runtime_paths
from brain_agent.scoring import score_candidate
from brain_agent.optimizers import SecondOrderOptimizer
from brain_agent.selection import classify_candidate
from brain_agent.utils import expression_fingerprint, write_json
from brain_agent.variant_search import (
    _eligible_for_second_order,
    _neutralization_decay_cross_sweep,
    _pearson_corr,
    _pnl_prune,
)
from brain_agent.worker import SimulationWorker


def _load_batch_simulator_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".agents" / "skills" / "brain-simAlphasinBatch-and-track" / "scripts" / "batch_simulator.py"
    if not path.exists():
        path = root / ".claude" / "skills" / "brain-simAlphasinBatch-and-track" / "scripts" / "batch_simulator.py"
    spec = importlib.util.spec_from_file_location("batch_simulator_for_tests", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load batch simulator module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_make_pipeline_module():
    root = Path(__file__).resolve().parents[1]
    path = root / ".agents" / "skills" / "brain-makeSomeGem" / "scripts" / "trailSomeAlphas" / "run_pipeline.py"
    if not path.exists():
        path = root / ".claude" / "skills" / "brain-makeSomeGem" / "scripts" / "trailSomeAlphas" / "run_pipeline.py"
    spec = importlib.util.spec_from_file_location("make_pipeline_for_tests", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load make pipeline module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BrainAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = get_runtime_paths("test_run", self.root)
        ensure_runtime(self.paths)
        self.repo = Repository(self.paths.db_path)
        self.config = RunConfig(
            dataset="analyst7",
            region="USA",
            delay=1,
            universe="TOP3000",
            data_type="MATRIX",
            decay=10,
            truncation=0.08,
            neutralization="INDUSTRY",
            max_trade=False,
            target_ready=1,
            max_iterations=1,
            dry_run=True,
        )
        self.repo.create_run("test_run", self.config)

    def tearDown(self) -> None:
        self.repo.close()
        self.tmp.cleanup()

    def test_repository_upsert_deduplicates_by_fingerprint(self) -> None:
        fp = expression_fingerprint("rank(close)")
        first = self.repo.upsert_candidate("test_run", "rank(close)", fp, status="generated")
        second = self.repo.upsert_candidate("test_run", "rank(close)", fp, status="sim_pending")
        self.assertEqual(first, second)
        self.assertEqual(1, self.repo.count_candidates("test_run"))
        row = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("sim_pending", row["status"])

    def test_artifact_hash_is_recorded(self) -> None:
        path = self.paths.artifacts_dir / "final_expressions.json"
        write_json(path, ["rank(close)"])
        result = MakeSomeGemAdapter(self.repo, "test_run", self.paths.run_dir).parse_final_expressions(path)
        self.assertEqual("ok", result.status)
        artifact = self.repo.list_rows("artifacts", "test_run")[0]
        self.assertEqual("final_expressions", artifact["kind"])
        self.assertTrue(artifact["sha256"])
        MakeSomeGemAdapter(self.repo, "test_run", self.paths.run_dir).parse_final_expressions(path)
        artifacts = self.repo.list_rows("artifacts", "test_run")
        self.assertEqual(1, sum(1 for item in artifacts if item["kind"] == "final_expressions"))
        self.assertEqual(1, sum(1 for item in artifacts if item["kind"] == "factor_theses"))

    def test_make_pipeline_extracts_common_llm_markdown_variants(self) -> None:
        module = _load_make_pipeline_module()
        markdown = "\n".join(
            [
                "### **Concept**: Heading Prefix",
                "- **Implementation Example:** `rank({field_a})`",
                "",
                "**Concept:** Bold Colon",
                "- **Implementation Example**: rank({field_b})",
                "",
                "**Concept**: Next Line",
                "- **Implementation Example**:",
                "  `rank({field_c})`",
            ]
        )
        blocks = module.extract_template_blocks(markdown)
        self.assertEqual(["rank({field_a})", "rank({field_b})", "rank({field_c})"], [b["template"] for b in blocks])
        self.assertNotIn("rank({field_c})", blocks[2]["idea"])

    def test_candidate_tiering_rules(self) -> None:
        self.assertEqual(
            CandidateStatus.MANUAL_REVIEW,
            classify_candidate(sim_status="COMPLETE", sharpe=1.3, fitness=1.1, turnover=0.2),
        )
        self.assertEqual(
            CandidateStatus.PROMISING,
            classify_candidate(sim_status="COMPLETE", sharpe=0.9, fitness=0.6, turnover=0.2),
        )
        self.assertEqual(
            CandidateStatus.REJECTED,
            classify_candidate(sim_status="COMPLETE", sharpe=0.1, fitness=0.1, turnover=0.2),
        )
        self.assertEqual(
            CandidateStatus.NEEDS_ENHANCE,
            classify_candidate(sim_status="COMPLETE", sharpe=-1.4, fitness=-0.6, turnover=0.2),
        )

    def test_alpha_list_and_simulation_adapters_write_state(self) -> None:
        alpha_list = self.paths.artifacts_dir / "alpha_list.json"
        write_json(
            alpha_list,
            [
                {
                    "type": "REGULAR",
                    "settings": {"region": "USA", "delay": 1},
                    "regular": "rank(close)",
                }
            ],
        )
        inspect = InspectRawTemplateAdapter(self.repo, "test_run", self.paths.run_dir).parse_alpha_list(alpha_list)
        self.assertEqual("ok", inspect.status)
        fp = expression_fingerprint("rank(close)")

        status_csv = self.paths.artifacts_dir / "simulation_status.csv"
        with status_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fingerprint",
                    "regular_expression",
                    "settings_json",
                    "sim_id",
                    "status",
                    "alpha_id",
                    "pnl",
                    "sharpe",
                    "turnover",
                    "fitness",
                    "error",
                    "error_details",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "fingerprint": fp,
                    "regular_expression": "rank(close)",
                    "settings_json": json.dumps({"region": "USA", "delay": 1}),
                    "sim_id": "sim1",
                    "status": "COMPLETE",
                    "alpha_id": "A1",
                    "pnl": "10",
                    "sharpe": "0.95",
                    "turnover": "0.2",
                    "fitness": "0.7",
                    "error": "",
                    "error_details": "",
                }
            )
        sim = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir).parse_simulation_status(status_csv)
        self.assertEqual("ok", sim.status)
        sim_again = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir).parse_simulation_status(status_csv)
        self.assertEqual("ok", sim_again.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("promising", candidate["status"])
        self.assertEqual("A1", candidate["alpha_id"])
        sim_results = self.repo.list_rows("sim_results", "test_run")
        self.assertEqual(1, len(sim_results))
        self.assertEqual(fp, sim_results[0]["simulation_fingerprint"])
        self.assertEqual("no_obvious_failure", sim_results[0]["failure_tags"])
        self.assertGreater(float(candidate["selection_score"]), 0)
        breakdown = json.loads(candidate["score_breakdown"])
        self.assertIn("quality_score", breakdown)
        self.assertTrue(breakdown["has_metrics"])

    def test_candidate_scoring_prefers_repairable_metric_failures(self) -> None:
        good = score_candidate(
            {"candidate_id": 1, "expression": "ts_zscore(rank(close), 20)", "status": "needs_enhance"},
            {
                "status": "COMPLETE",
                "sharpe": 1.05,
                "fitness": 0.72,
                "turnover": 0.82,
                "failure_tags": "high_turnover",
                "repair_objectives": "reduce_turnover",
            },
        )
        bad = score_candidate(
            {"candidate_id": 2, "expression": "rank(close)", "status": "sim_failed"},
            {
                "status": "FAILED",
                "sharpe": 0.05,
                "fitness": 0.02,
                "turnover": 0.9,
                "failure_tags": "unknown_variable,hard_error",
                "repair_objectives": "fix_syntax",
                "error": "unknown variable",
            },
        )
        self.assertGreater(good.score, bad.score)
        self.assertIn("reduce_turnover", good.breakdown["repair_objectives"])

    def test_candidate_scoring_uses_alpha_memory_context(self) -> None:
        memory_context = {
            "operator_stats": {
                "rank": {"observations": 10, "success_rate": 0.8, "failure_tags": {}},
                "ts_delta": {"observations": 10, "success_rate": 0.1, "failure_tags": {"high_turnover": 8}},
            },
            "field_family_stats": {
                "analyst7": {"observations": 10, "success_rate": 0.8, "failure_tags": {}},
                "model165": {"observations": 10, "success_rate": 0.1, "failure_tags": {"coverage_issue": 7}},
            },
        }
        historically_good = score_candidate(
            {"candidate_id": 1, "status": "sim_pending", "expression": "rank(analyst7_field_a)"},
            memory_context=memory_context,
        )
        historically_bad = score_candidate(
            {"candidate_id": 2, "status": "sim_pending", "expression": "ts_delta(model165_field_a, 5)"},
            memory_context=memory_context,
        )
        self.assertGreater(historically_good.score, historically_bad.score)
        self.assertGreater(historically_good.breakdown["memory_score"], historically_bad.breakdown["memory_score"])
        self.assertGreater(historically_bad.breakdown["memory_risk_penalty"], 0)

    def test_memory_context_separates_non_success_from_hard_failure(self) -> None:
        context = build_scoring_context(
            [
                {
                    "status": "sim_failed",
                    "sim_status": "COMPLETE",
                    "operators_json": json.dumps(["rank"]),
                    "field_families_json": json.dumps(["model165"]),
                    "failure_tags_json": json.dumps([]),
                    "sharpe": 0.1,
                    "fitness": 0.1,
                    "turnover": 0.2,
                    "ingested_at": "2026-05-28T00:00:00+00:00",
                },
                {
                    "status": "sim_failed",
                    "sim_status": "FAILED",
                    "operators_json": json.dumps(["ts_delta"]),
                    "field_families_json": json.dumps(["model165"]),
                    "failure_tags_json": json.dumps(["syntax_error"]),
                    "ingested_at": "2026-05-28T00:00:00+00:00",
                },
            ]
        )

        rank_stats = context["operator_stats"]["rank"]
        delta_stats = context["operator_stats"]["ts_delta"]
        self.assertEqual(1.0, rank_stats["non_success_rate"])
        self.assertEqual(0.0, rank_stats["failure_rate"])
        self.assertEqual(1.0, delta_stats["failure_rate"])

    def test_memory_low_success_without_hard_failures_does_not_add_risk_penalty(self) -> None:
        memory_context = {
            "operator_stats": {
                "rank": {
                    "observations": 10,
                    "success_rate": 0.1,
                    "failure_rate": 0.0,
                    "hard_failure_rate": 0.0,
                    "confidence": 0.8,
                    "failure_tags": {},
                }
            }
        }
        scored = score_candidate(
            {"candidate_id": 1, "status": "sim_pending", "expression": "rank(model165_value)"},
            memory_context=memory_context,
        )
        self.assertEqual(0.0, scored.breakdown["memory_risk_penalty"])

    def test_candidate_scoring_penalizes_correlation_gate_failures(self) -> None:
        candidate = {"candidate_id": 1, "expression": "rank(ts_mean(model165_value, 20))", "status": "promising"}
        sim = {"status": "COMPLETE", "sharpe": 1.2, "fitness": 0.8, "turnover": 0.2, "failure_tags": ""}
        clean = score_candidate(candidate, sim)
        corr_failed = score_candidate(
            candidate,
            sim,
            latest_gate={
                "passed": 0,
                "submission_check": "PASS",
                "self_corr_check": "FAIL",
                "prod_corr_check": "PASS",
                "gate_status": "complete",
            },
        )
        self.assertGreater(clean.score, corr_failed.score)
        self.assertIn("self_corr_high", corr_failed.breakdown["failure_tags"])
        self.assertIn("reduce_self_correlation", corr_failed.breakdown["repair_objectives"])
        self.assertFalse(corr_failed.breakdown["gate_passed"])

    def test_quota_allocator_clusters_by_settings_structure_and_thesis(self) -> None:
        settings = {"region": "USA", "delay": 1, "neutralization": "INDUSTRY"}
        rows = [
            {"regular": "rank(ts_mean(model165_value, 20))", "settings": settings},
            {"regular": "rank(ts_mean(model165_value, 30))", "settings": settings},
            {"regular": "rank(ts_mean(model165_value, 120))", "settings": settings},
            {"regular": "trade_when(volume > adv20, rank(ts_mean(model165_value, 20)), -1)", "settings": settings},
        ]
        candidates = [
            {
                "candidate_id": index + 1,
                "expression": row["regular"],
                "selection_score": 0.9 - index * 0.01,
                "score_breakdown": {"score": 0.9 - index * 0.01, "novelty_score": 0.5},
                "thesis_json": json.dumps({"thesis_type": "valuation"}),
            }
            for index, row in enumerate(rows)
        ]

        selected, report = allocate_simulation_quota(rows, candidates, limit=2)

        self.assertEqual(2, len(selected))
        clusters = list(report["selected_cluster_counts"].keys())
        self.assertEqual(2, len(clusters))
        self.assertTrue(any("w_medium" in cluster for cluster in clusters))
        self.assertTrue(any("w_very_long" in cluster or "conditional" in cluster for cluster in clusters))

    def test_quota_allocator_normalizes_pattern_confidence(self) -> None:
        rows = [
            {"regular": "rank(model165_value)", "settings": {"neutralization": "INDUSTRY"}},
            {"regular": "ts_delta(model165_value, 5)", "settings": {"neutralization": "INDUSTRY"}},
        ]
        candidates = [
            {
                "candidate_id": 1,
                "expression": "rank(model165_value)",
                "selection_score": 0.8,
                "score_breakdown": {
                    "score": 0.8,
                    "memory_score": 0.75,
                    "memory_evidence": [{"confidence": "0.6"}, {"confidence": None}],
                },
            },
            {
                "candidate_id": 2,
                "expression": "ts_delta(model165_value, 5)",
                "selection_score": 0.1,
                "score_breakdown": {"score": 0.1, "memory_score": 0.5, "memory_evidence": []},
            },
        ]

        _, report = allocate_simulation_quota(rows, candidates, limit=1)

        self.assertEqual(0.6, report["selected"][0]["pattern_confidence"])

    def test_diagnose_sim_result_detects_turnover_and_metric_failures(self) -> None:
        diagnosis = diagnose_sim_result(
            {
                "status": "COMPLETE",
                "sharpe": 0.2,
                "fitness": 0.1,
                "turnover": 0.82,
                "error": "",
            }
        )
        self.assertIn("high_turnover", diagnosis["failure_tags"])
        self.assertIn("low_sharpe", diagnosis["failure_tags"])
        self.assertIn("low_fitness", diagnosis["failure_tags"])
        self.assertIn("reduce_turnover", diagnosis["repair_objectives"])

    def test_transient_simulation_failures_are_retryable(self) -> None:
        diagnosis = diagnose_sim_result(
            {
                "status": "BATCH_SPAWN_FAILED",
                "error": "Parent status: None",
            }
        )

        self.assertIn("sim_retryable", diagnosis["failure_tags"])
        self.assertIn("platform_or_rate_limit", diagnosis["failure_tags"])
        self.assertIn("retry_simulation", diagnosis["repair_objectives"])
        self.assertEqual(
            CandidateStatus.SIM_RETRYABLE,
            classify_candidate(
                sim_status="BATCH_SPAWN_FAILED",
                sharpe=None,
                fitness=None,
                turnover=None,
                hard_error="Parent status: None",
            ),
        )
        self.assertEqual(
            CandidateStatus.REJECTED,
            classify_candidate(
                sim_status="SUBMISSION_FAILED",
                sharpe=None,
                fitness=None,
                turnover=None,
                hard_error='[{"factorThesis":["Unexpected property."]}]',
            ),
        )

    def test_diagnose_sim_result_detects_short_flip_candidates(self) -> None:
        diagnosis = diagnose_sim_result(
            {
                "status": "COMPLETE",
                "sharpe": -1.4,
                "fitness": -0.7,
                "turnover": 0.25,
                "error": "",
            }
        )
        self.assertIn("cand_neg", diagnosis["failure_tags"])
        self.assertIn("test_short_flip", diagnosis["repair_objectives"])
        self.assertTrue(any("short-flip" in hint for hint in diagnosis["repair_hints"]))

    def test_simulation_adapter_records_failure_diagnostics(self) -> None:
        status_csv = self.paths.artifacts_dir / "failed_simulation_status.csv"
        with status_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fingerprint",
                    "regular_expression",
                    "settings_json",
                    "sim_id",
                    "status",
                    "alpha_id",
                    "pnl",
                    "sharpe",
                    "turnover",
                    "fitness",
                    "error",
                    "error_details",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "fingerprint": "",
                    "regular_expression": "rank(close)",
                    "settings_json": "{}",
                    "sim_id": "bad_sim",
                    "status": "FAILED",
                    "alpha_id": "",
                    "pnl": "",
                    "sharpe": "0.1",
                    "turnover": "0.9",
                    "fitness": "0.05",
                    "error": "Attempted to use unknown variable apg",
                    "error_details": "",
                }
            )
        result = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir).parse_simulation_status(status_csv)
        self.assertEqual("ok", result.status)
        sim = self.repo.list_rows("sim_results", "test_run")[0]
        self.assertIn("unknown_variable", sim["failure_tags"])
        self.assertIn("high_turnover", sim["failure_tags"])
        self.assertIn("fix_syntax", sim["repair_objectives"])
        diagnosis = json.loads(sim["diagnosis_json"])
        self.assertTrue(diagnosis["repair_hints"])

    def test_simulation_adapter_marks_batch_spawn_failed_as_retryable(self) -> None:
        status_csv = self.paths.artifacts_dir / "retryable_simulation_status.csv"
        with status_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fingerprint",
                    "regular_expression",
                    "settings_json",
                    "sim_id",
                    "status",
                    "alpha_id",
                    "pnl",
                    "sharpe",
                    "turnover",
                    "fitness",
                    "error",
                    "error_details",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "fingerprint": "",
                    "regular_expression": "rank(close)",
                    "settings_json": "{}",
                    "sim_id": "queued_parent",
                    "status": "BATCH_SPAWN_FAILED",
                    "alpha_id": "",
                    "pnl": "",
                    "sharpe": "",
                    "turnover": "",
                    "fitness": "",
                    "error": "Parent status: None",
                    "error_details": "",
                }
            )
        result = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir).parse_simulation_status(status_csv)
        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual(CandidateStatus.SIM_RETRYABLE.value, candidate["status"])
        sim = self.repo.list_rows("sim_results", "test_run")[0]
        self.assertIn("sim_retryable", sim["failure_tags"])
        self.assertIn("retry_simulation", sim["repair_objectives"])

    def test_simulation_adapter_records_short_flip_suggestions(self) -> None:
        status_csv = self.paths.artifacts_dir / "negative_simulation_status.csv"
        with status_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "fingerprint",
                    "regular_expression",
                    "settings_json",
                    "sim_id",
                    "status",
                    "alpha_id",
                    "pnl",
                    "sharpe",
                    "turnover",
                    "fitness",
                    "error",
                    "error_details",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "fingerprint": "",
                    "regular_expression": "rank(close)",
                    "settings_json": "{}",
                    "sim_id": "neg_sim",
                    "status": "COMPLETE",
                    "alpha_id": "NEG001",
                    "pnl": "-1000",
                    "sharpe": "-1.4",
                    "turnover": "0.25",
                    "fitness": "-0.7",
                    "error": "",
                    "error_details": "",
                }
            )
        result = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir).parse_simulation_status(status_csv)
        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual(CandidateStatus.NEEDS_ENHANCE.value, candidate["status"])
        sim = self.repo.list_rows("sim_results", "test_run")[0]
        self.assertIn("cand_neg", sim["failure_tags"])
        diagnosis = json.loads(sim["diagnosis_json"])
        self.assertEqual("multiply(-1, rank(close))", diagnosis["short_flip_suggestions"][0])

    def test_enhance_adapter_writes_diagnostics_context(self) -> None:
        adapter = EnhanceTemplateAdapter(self.repo, "test_run", self.paths.run_dir)
        context = [
            {
                "candidate_id": 7,
                "status": "needs_enhance",
                "expression": "rank(close)" + "x" * 1000,
                "latest_sim_result": {"sharpe": 0.4, "fitness": 0.2, "turnover": 0.8, "error": "e" * 1000},
                "failure_tags": ["high_turnover", "low_fitness"],
                "repair_objectives": ["reduce_turnover", "improve_fitness"],
                "diagnosis": {"diagnosis_reasons": ["Turnover is high." * 100], "repair_hints": ["Use decay." * 100]},
            }
        ]
        idea = self.paths.artifacts_dir / "idea.json"
        write_json(idea, {"template": "rank({close})", "idea": "price rank"})
        with patch.object(EnhanceTemplateAdapter, "run_command", return_value=("task1", 0)):
            with patch("brain_agent.adapters._newest_files", return_value=[]):
                result = adapter.run_real([idea], self.config, candidates_context=context)
        self.assertEqual("ok", result.status)
        artifact = self.repo.list_artifacts("test_run", kind="enhance_diagnostics_context")[0]
        payload = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
        self.assertIn("complexity_budget", payload)
        self.assertIn("batch_diversity", payload["complexity_budget"])
        self.assertEqual(["high_turnover", "low_fitness"], payload["candidates"][0]["failure_tags"])
        self.assertLessEqual(len(payload["candidates"][0]["expression"]), 500)
        self.assertLessEqual(len(payload["candidates"][0]["error"]), 500)
        self.assertLessEqual(len(payload["candidates"][0]["diagnosis_reasons"][0]), 240)

    def test_enhance_adapter_rejects_over_complex_expressions(self) -> None:
        adapter = EnhanceTemplateAdapter(self.repo, "test_run", self.paths.run_dir)
        enhanced_path = self.paths.artifacts_dir / "enhanced_complexity.json"
        write_json(
            enhanced_path,
            [
                {
                    "expression": "rank(ts_mean(valid_field, 20))",
                },
                {
                    "expression": (
                        "rank(add(ts_mean(field_a, 20), ts_delta(field_b, 5), ts_zscore(field_c, 60), "
                        "group_neutralize(ts_rank(field_d, 30), industry), ts_sum(field_e, 10)))"
                    ),
                },
            ],
        )
        filtered_path, rejected_path = adapter._filter_enhanced_expressions_by_complexity(enhanced_path, self.config)
        filtered = json.loads(filtered_path.read_text(encoding="utf-8"))
        rejected = json.loads(rejected_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(filtered))
        self.assertEqual("rank(ts_mean(valid_field, 20))", filtered[0]["expression"])
        self.assertEqual(1, len(rejected))
        self.assertGreater(rejected[0]["complexity"]["datafield_count"], rejected[0]["budget"]["max_datafields"])

    def test_batch_diversity_filter_rejects_common_operator_pileups(self) -> None:
        adapter = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir)
        alpha_list = self.paths.artifacts_dir / "alpha_list_diversity.json"
        rows = [
            {"regular": "rank(close)"},
            {"regular": "rank(open)"},
            {"regular": "rank(high)"},
            {"regular": "group_vector_neut(rank(volume), sector)"},
            {"regular": "trade_when(rank(returns), greater(volume, 0), -1)"},
            {"regular": "zscore(low)"},
        ]
        write_json(alpha_list, rows)

        filtered_path, artifacts = adapter._filter_alpha_list_by_batch_diversity(alpha_list)

        filtered = json.loads(Path(filtered_path).read_text(encoding="utf-8"))
        expressions = [item["regular"] for item in filtered]
        rank_uses = sum(expr.count("rank(") for expr in expressions)
        self.assertLessEqual(rank_uses, 2)
        self.assertIn("group_vector_neut(rank(volume), sector)", expressions)
        self.assertTrue(any(a["kind"] == "alpha_list_diversity_report" for a in artifacts))
        self.assertTrue(any(a["kind"] == "alpha_list_diversity_rejected" for a in artifacts))
        rejected_artifact = next(a for a in artifacts if a["kind"] == "alpha_list_diversity_rejected")
        rejected = json.loads(Path(rejected_artifact["path"]).read_text(encoding="utf-8"))
        self.assertTrue(any(item["reason"] == "common_operator_repeat_limit" for item in rejected))

    def test_batch_simulator_strips_internal_metadata_from_submission_payload(self) -> None:
        module = _load_batch_simulator_module()
        row = {
            "type": "REGULAR",
            "settings": {"region": "USA", "delay": 1},
            "regular": "rank(close)",
            "factorThesis": {"hypothesis": "internal only"},
            "lineage": {"variant_strategy": "unit"},
            "parentCandidateId": 123,
            "parentExpression": "rank(open)",
            "parentMetrics": {"sharpe": 0.4},
            "variantParams": {"window": 20},
            "variantStrategy": "unit",
            "variant_params": {"window": 20},
            "variant_strategy": "unit",
        }

        payload = module.brain_submission_payload(row)

        self.assertEqual(
            {
                "type": "REGULAR",
                "settings": {"region": "USA", "delay": 1},
                "regular": "rank(close)",
            },
            payload,
        )
        self.assertNotIn("factorThesis", payload)
        self.assertNotIn("lineage", payload)
        self.assertNotIn("variant_params", payload)

    def test_batch_simulator_retries_transient_status_fetch_failure(self) -> None:
        module = _load_batch_simulator_module()

        class FakeResponse:
            def __init__(self, status_code: int, payload: dict | None = None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = {}
                self.text = json.dumps(self._payload) if payload is not None else "temporary"

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse(503),
                    FakeResponse(200, {"status": "COMPLETE", "children": ["sim-child-1"]}),
                ]
                self.calls = 0

            def get(self, url):
                self.calls += 1
                return self.responses.pop(0)

        session = FakeSession()
        simulator = module.BatchSimulator(session=session, output_csv=str(self.root / "sim_status.csv"))

        with patch.object(module.time, "sleep", return_value=None):
            response, data, error = simulator._fetch_simulation_status("https://example.test/sim", context="unit")

        self.assertEqual(2, session.calls)
        self.assertEqual(200, response.status_code)
        self.assertEqual(["sim-child-1"], data["children"])
        self.assertEqual("", error)

    def test_write_alpha_list_for_generated_candidates(self) -> None:
        out = InspectRawTemplateAdapter(self.repo, "test_run", self.paths.run_dir).write_alpha_list_for_candidates(
            [{"expression": "rank(close)"}],
            self.config,
            name="generated_alpha_list.json",
        )
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual("rank(close)", data[0]["regular"])
        self.assertEqual("USA", data[0]["settings"]["region"])
        self.assertEqual(10, data[0]["settings"]["decay"])
        self.assertEqual("INDUSTRY", data[0]["settings"]["neutralization"])
        self.assertEqual("OFF", data[0]["settings"]["maxTrade"])

    def test_simulation_alpha_list_is_score_sorted_before_quota_limit(self) -> None:
        settings = {"region": "USA", "delay": 1}
        weak = "rank(price_close)"
        strong = "trade_when(ts_mean(volume_avg, 20), ts_zscore(rank(price_close), 60), -1)"
        alpha_list = self.paths.artifacts_dir / "quota_alpha_list.json"
        write_json(
            alpha_list,
            [
                {"type": "REGULAR", "settings": settings, "regular": weak},
                {"type": "REGULAR", "settings": settings, "regular": strong},
            ],
        )
        inspect = InspectRawTemplateAdapter(self.repo, "test_run", self.paths.run_dir).parse_alpha_list(alpha_list)
        self.assertEqual("ok", inspect.status)
        sorted_path = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir)._score_sorted_alpha_list(alpha_list)
        sorted_rows = json.loads(sorted_path.read_text(encoding="utf-8"))
        self.assertEqual(strong, sorted_rows[0]["regular"])
        limited = self.paths.artifacts_dir / "limited.json"
        write_json(limited, sorted_rows)
        limited_path = _limited_alpha_list(limited, 1)
        limited_rows = json.loads(limited_path.read_text(encoding="utf-8"))
        self.assertEqual([strong], [row["regular"] for row in limited_rows])

    def test_simulation_preflight_skips_fields_unavailable_for_target_universe(self) -> None:
        alpha_list = self.paths.artifacts_dir / "mixed_fields_alpha_list.json"
        write_json(
            alpha_list,
            [
                {"type": "REGULAR", "settings": {"region": "EUR", "universe": "TOP2500", "delay": 1}, "regular": "rank(valid_field)"},
                {"type": "REGULAR", "settings": {"region": "EUR", "universe": "TOP2500", "delay": 1}, "regular": "rank(missing_field)"},
            ],
        )
        InspectRawTemplateAdapter(self.repo, "test_run", self.paths.run_dir).parse_alpha_list(alpha_list)

        adapter = BatchSimAdapter(self.repo, "test_run", self.paths.run_dir)
        with patch.object(
            adapter,
            "_available_datafields",
            return_value=AdapterResult(status="ok", candidates_delta=[{"field_ids": ["valid_field"]}]),
        ):
            result = adapter._preflight_alpha_list_fields(alpha_list, self.config, {})

        self.assertEqual("ok", result.status)
        filtered_path = Path(result.candidates_delta[0]["filtered_alpha_list"])
        filtered_rows = json.loads(filtered_path.read_text(encoding="utf-8"))
        self.assertEqual(["rank(valid_field)"], [row["regular"] for row in filtered_rows])

        precheck_artifact = next(a for a in result.artifacts if a["kind"] == "simulation_precheck_status")
        parsed = adapter.parse_simulation_status(Path(precheck_artifact["path"]))
        self.assertEqual("ok", parsed.status)
        sim = self.repo.list_rows("sim_results", "test_run")[0]
        self.assertEqual("PRECHECK_FAILED", sim["status"])
        self.assertIn("datafield_unavailable", sim["failure_tags"])
        self.assertIn("missing_field", sim["error"])
        candidates = {row["expression"]: row["status"] for row in self.repo.list_rows("candidates", "test_run")}
        self.assertEqual("rejected", candidates["rank(missing_field)"])

    def test_write_alpha_list_honors_custom_simulation_settings(self) -> None:
        config = RunConfig(
            dataset="model165",
            region="EUR",
            delay=1,
            universe="TOP2500",
            data_type="MATRIX",
            decay=10,
            truncation=0.08,
            neutralization="SLOW_AND_FAST",
            max_trade=False,
        )
        out = InspectRawTemplateAdapter(self.repo, "test_run", self.paths.run_dir).write_alpha_list_for_candidates(
            [{"expression": "rank(close)"}],
            config,
            name="custom_settings_alpha_list.json",
        )
        settings = json.loads(out.read_text(encoding="utf-8"))[0]["settings"]
        self.assertEqual("EUR", settings["region"])
        self.assertEqual("TOP2500", settings["universe"])
        self.assertEqual(1, settings["delay"])
        self.assertEqual(10, settings["decay"])
        self.assertEqual(0.08, settings["truncation"])
        self.assertEqual("SLOW_AND_FAST", settings["neutralization"])
        self.assertEqual("OFF", settings["maxTrade"])

    # ── cross sweep ──────────────────────────────────────────

    def test_cross_sweep_is_deterministic(self) -> None:
        settings = {"neutralization": "INDUSTRY", "decay": 10}
        fp = "abc123"
        a = _neutralization_decay_cross_sweep("rank(x)", settings, parent_fingerprint=fp, turnover=0.1)
        b = _neutralization_decay_cross_sweep("rank(x)", settings, parent_fingerprint=fp, turnover=0.1)
        self.assertEqual(len(a), len(b))
        for va, vb in zip(a, b):
            self.assertEqual(va["regular"], vb["regular"])
            self.assertEqual(va["settings"], vb["settings"])

    def test_cross_sweep_excludes_current_neutralization(self) -> None:
        settings = {"neutralization": "INDUSTRY", "decay": 10}
        variants = _neutralization_decay_cross_sweep("rank(x)", settings, parent_fingerprint="test", turnover=0.1)
        for v in variants:
            self.assertNotEqual(
                v["settings"].get("neutralization", "").upper(),
                "INDUSTRY",
            )

    def test_cross_sweep_uses_larger_decays_for_high_turnover(self) -> None:
        settings = {"neutralization": "SECTOR", "decay": 5}
        lo = _neutralization_decay_cross_sweep("rank(x)", settings, parent_fingerprint="lo", turnover=0.05)
        hi = _neutralization_decay_cross_sweep("rank(x)", settings, parent_fingerprint="hi", turnover=0.35)
        lo_decays = {v["settings"]["decay"] for v in lo}
        hi_decays = {v["settings"]["decay"] for v in hi}
        self.assertTrue(any(d > 10 for d in hi_decays) or max(hi_decays) >= max(lo_decays))

    # ── second order eligibility ─────────────────────────────

    def test_second_order_rejects_hard_failure(self) -> None:
        parent = {"status": "promising", "selection_score": 0.8}
        sim = {"failure_tags": ["hard_error"]}
        self.assertFalse(_eligible_for_second_order(parent, sim))

    def test_second_order_rejects_rejected_status(self) -> None:
        parent = {"status": "rejected", "selection_score": 0.8}
        sim = {"failure_tags": []}
        self.assertFalse(_eligible_for_second_order(parent, sim))

    def test_second_order_accepts_promising(self) -> None:
        parent = {"status": "promising", "selection_score": 0.7}
        sim = {"failure_tags": []}
        self.assertTrue(_eligible_for_second_order(parent, sim))

    def test_second_order_accepts_high_score(self) -> None:
        parent = {"status": "needs_enhance", "selection_score": 0.65}
        sim = {"failure_tags": []}
        self.assertTrue(_eligible_for_second_order(parent, sim))

    def test_second_order_optimizer_produces_group_variants(self) -> None:
        opt = SecondOrderOptimizer()
        variants = opt.optimize(
            "ts_mean(close, 20)",
            {"decay": 10},
            sim_result={"sharpe": 1.0, "fitness": 0.7, "turnover": 0.2},
            candidate={"status": "promising", "selection_score": 0.8},
        )
        strategies = {v["variant_strategy"] for v in variants}
        self.assertIn("second_order", strategies)
        expressions = [v["regular"] for v in variants]
        self.assertTrue(any("group_rank" in e for e in expressions))
        self.assertTrue(any("group_zscore" in e for e in expressions))
        self.assertTrue(any("group_neutralize" in e for e in expressions))
        self.assertTrue(any("group_mean" in e for e in expressions))
        self.assertTrue(any("signed_power" in e for e in expressions))

    def test_second_order_optimizer_skips_ineligible(self) -> None:
        opt = SecondOrderOptimizer()
        variants = opt.optimize(
            "x",
            {},
            sim_result={"failure_tags": ["hard_error"]},
            candidate={"status": "rejected", "selection_score": 0.1},
        )
        self.assertEqual(len(variants), 0)

    # ── pnl prune ────────────────────────────────────────────

    def test_pnl_prune_empty_returns_all(self) -> None:
        candidates = [{"alpha_id": "a1", "sharpe": 1.0}]
        result = _pnl_prune(candidates, {})
        self.assertEqual(len(result), 1)

    def test_pnl_prune_single_returns_all(self) -> None:
        candidates = [{"alpha_id": "a1", "sharpe": 1.0}]
        result = _pnl_prune(candidates, {"a1": [0.1, 0.2, 0.3]})
        self.assertEqual(len(result), 1)

    def test_pnl_prune_dedups_highly_correlated(self) -> None:
        candidates = [
            {"alpha_id": "a1", "sharpe": 1.0},
            {"alpha_id": "a2", "sharpe": 0.5},
        ]
        # identical series → corr = 1.0 → second removed
        series = [0.1, 0.2, 0.1, 0.3, 0.1]
        result = _pnl_prune(candidates, {"a1": list(series), "a2": list(series)}, threshold=0.8)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["alpha_id"], "a1")

    def test_pnl_prune_uses_latest_sim_sharpe(self) -> None:
        candidates = [
            {"candidate_id": 1, "alpha_id": "a1"},
            {"candidate_id": 2, "alpha_id": "a2"},
        ]
        series = [0.1, 0.2, 0.1, 0.3, 0.1]
        result = _pnl_prune(
            candidates,
            {"a1": list(series), "a2": list(series)},
            threshold=0.8,
            latest_sim_by_candidate={1: {"sharpe": 0.4}, 2: {"sharpe": 1.2}},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["alpha_id"], "a2")

    def test_pnl_prune_keeps_uncorrelated(self) -> None:
        candidates = [
            {"alpha_id": "a1", "sharpe": 1.0},
            {"alpha_id": "a2", "sharpe": 0.5},
        ]
        result = _pnl_prune(
            candidates,
            {"a1": [0.64, 0.03, 0.28, 0.22, 0.74, 0.68], "a2": [0.22, 0.51, 0.03, 0.20, 0.65, 0.36]},
            threshold=0.8,
        )
        self.assertEqual(len(result), 2)

    def test_field_factory_wraps_vector_fields(self) -> None:
        config = RunConfig(
            dataset="model1",
            region="USA",
            delay=1,
            universe="TOP3000",
            data_type="VECTOR",
        )
        rows = _field_factory_alpha_rows(
            config,
            [{"id": "vec_field", "type": "VECTOR", "coverage": 0.9}],
            max_rows=2,
        )
        expressions = [row["regular"] for row in rows]
        self.assertTrue(expressions)
        self.assertTrue(all("vec_avg(vec_field)" in expr for expr in expressions))

    def test_field_factory_backfills_low_coverage_fields(self) -> None:
        config = RunConfig(
            dataset="model1",
            region="USA",
            delay=1,
            universe="TOP3000",
            data_type="MATRIX",
        )
        rows = _field_factory_alpha_rows(
            config,
            [{"id": "low_cov_field", "type": "MATRIX", "coverage": 0.4}],
            max_rows=1,
        )
        self.assertIn("ts_backfill(low_cov_field, 60)", rows[0]["regular"])

    def test_pearson_corr_perfect_positive(self) -> None:
        x = [1.0, 2.0, 3.0]
        y = [2.0, 4.0, 6.0]
        self.assertAlmostEqual(_pearson_corr(x, y), 1.0)

    def test_pearson_corr_perfect_negative(self) -> None:
        x = [1.0, 2.0, 3.0]
        y = [3.0, 2.0, 1.0]
        self.assertAlmostEqual(_pearson_corr(x, y), -1.0)

    def test_pearson_corr_short_input(self) -> None:
        self.assertIsNone(_pearson_corr([1.0], [1.0]))

    def test_task_runner_records_task_logs(self) -> None:
        adapter = MakeSomeGemAdapter(self.repo, "test_run", self.paths.run_dir)
        task_id, returncode = adapter.run_command(
            "unit_task",
            ["python3", "-c", "print('hello task')"],
            self.root,
        )
        self.assertEqual(0, returncode)
        task = self.repo.list_rows("tasks", "test_run")[0]
        self.assertEqual(task_id, task["task_id"])
        self.assertEqual("completed", task["status"])
        self.assertTrue(Path(task["stdout_path"]).exists())

    def test_runtime_paths_resolve_relative_root(self) -> None:
        paths = get_runtime_paths("relative_run", "relative_runtime")
        self.assertTrue(paths.root.is_absolute())
        self.assertTrue(paths.run_dir.is_absolute())
        self.assertEqual(paths.root / "runs" / "relative_run", paths.run_dir)

    def test_submission_gate_marks_submit_ready_when_all_checks_pass(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="manual_review",
            alpha_id="A123",
        )

        class FakeFrame:
            empty = False

            def __init__(self, records):
                self.records = records

            def to_dict(self, orient):
                self.assert_orient = orient
                return self.records

        fake_ace = types.SimpleNamespace(
            start_session=lambda: object(),
            get_check_submission=lambda session, alpha_id: FakeFrame(
                [
                    {"test": "SHARPE", "result": "PASS"},
                    {"test": "FITNESS", "result": "PASS"},
                    {"test": "WEIGHT", "result": "PASS"},
                    {"test": "SUB_UNIVERSE_SHARPE", "result": "PASS"},
                    {"test": "SELF_CORRELATION", "result": "PENDING"},
                    {"test": "PROD_CORRELATION", "result": "PENDING"},
                ]
            ),
            check_self_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "SELF_CORRELATION", "result": "PASS"}]
            ),
            check_prod_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "PROD_CORRELATION", "result": "PASS"}]
            ),
        )

        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with patch.dict(sys.modules, {"ace_lib": fake_ace}):
                result = SubmissionGateAdapter(self.repo, "test_run", self.paths.run_dir).run_real()

        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("submit_ready", candidate["status"])
        gate = self.repo.list_rows("gate_checks", "test_run")[0]
        self.assertEqual(1, gate["passed"])

    def test_submission_gate_falls_back_to_alpha_detail_checks(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="manual_review",
            alpha_id="A123",
        )

        class EmptyFrame:
            empty = True

            def to_dict(self, orient):
                return []

        class FakeResponse:
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "is": {
                        "checks": [
                            {"name": "SHARPE", "result": "PASS"},
                            {"name": "FITNESS", "result": "PASS"},
                            {"name": "CONCENTRATED_WEIGHT", "result": "PASS"},
                            {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS"},
                            {"name": "IS_LADDER_SHARPE", "result": "PASS"},
                            {"name": "SELF_CORRELATION", "result": "PENDING"},
                            {"name": "PROD_CORRELATION", "result": "PENDING"},
                        ]
                    }
                }

        class FakeSession:
            def get(self, url):
                self.url = url
                return FakeResponse()

        class FakeFrame:
            empty = False

            def __init__(self, records):
                self.records = records

            def to_dict(self, orient):
                return self.records

        fake_session = FakeSession()
        fake_ace = types.SimpleNamespace(
            brain_api_url="https://api.worldquantbrain.com",
            start_session=lambda: fake_session,
            get_check_submission=lambda session, alpha_id: EmptyFrame(),
            check_self_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "SELF_CORRELATION", "result": "PASS"}]
            ),
            check_prod_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "PROD_CORRELATION", "result": "PASS"}]
            ),
        )

        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with patch.dict(sys.modules, {"ace_lib": fake_ace}):
                result = SubmissionGateAdapter(self.repo, "test_run", self.paths.run_dir).run_real()

        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("submit_ready", candidate["status"])
        gate = self.repo.list_rows("gate_checks", "test_run")[0]
        self.assertEqual(1, gate["passed"])
        self.assertEqual("PASS", gate["weight_check"])
        self.assertEqual("PASS", gate["subuniverse_check"])
        self.assertIn("/alphas/A123", fake_session.url)

    def test_submission_gate_uses_dedicated_correlation_checks_over_pending_submission_rows(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="manual_review",
            alpha_id="A123",
        )

        class FakeFrame:
            empty = False

            def __init__(self, records):
                self.records = records

            def to_dict(self, orient):
                return self.records

        fake_ace = types.SimpleNamespace(
            start_session=lambda: object(),
            get_check_submission=lambda session, alpha_id: FakeFrame(
                [
                    {"name": "SHARPE", "result": "PASS"},
                    {"name": "FITNESS", "result": "PASS"},
                    {"name": "CONCENTRATED_WEIGHT", "result": "PASS"},
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS"},
                    {"name": "SELF_CORRELATION", "result": "PENDING"},
                    {"name": "PROD_CORRELATION", "result": "PENDING"},
                ]
            ),
            check_self_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "SELF_CORRELATION", "result": "PASS"}]
            ),
            check_prod_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "PROD_CORRELATION", "result": "PASS"}]
            ),
        )

        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with patch.dict(sys.modules, {"ace_lib": fake_ace}):
                result = SubmissionGateAdapter(self.repo, "test_run", self.paths.run_dir).run_real()

        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("submit_ready", candidate["status"])
        gate = self.repo.list_rows("gate_checks", "test_run")[0]
        self.assertEqual(1, gate["passed"])
        self.assertEqual("PASS", gate["submission_check"])
        self.assertEqual("PASS", gate["self_corr_check"])
        self.assertEqual("PASS", gate["prod_corr_check"])

    def test_submission_gate_writes_correlation_failure_into_score(self) -> None:
        fp = expression_fingerprint("rank(close)")
        cid = self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="promising",
            alpha_id="A123",
        )
        self.repo.add_sim_result(
            "test_run",
            {
                "candidate_id": cid,
                "fingerprint": fp,
                "simulation_fingerprint": fp,
                "alpha_id": "A123",
                "sim_id": "sim1",
                "status": "COMPLETE",
                "sharpe": 1.2,
                "fitness": 0.8,
                "turnover": 0.2,
                "failure_tags": "",
                "repair_objectives": "",
            },
        )

        class FakeFrame:
            empty = False

            def __init__(self, records):
                self.records = records

            def to_dict(self, orient):
                return self.records

        fake_ace = types.SimpleNamespace(
            start_session=lambda: object(),
            get_check_submission=lambda session, alpha_id: FakeFrame(
                [
                    {"test": "SHARPE", "result": "PASS"},
                    {"test": "FITNESS", "result": "PASS"},
                    {"test": "WEIGHT", "result": "PASS"},
                    {"test": "SUB_UNIVERSE_SHARPE", "result": "PASS"},
                ]
            ),
            check_self_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "SELF_CORRELATION", "result": "FAIL"}]
            ),
            check_prod_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "PROD_CORRELATION", "result": "PASS"}]
            ),
        )

        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with patch.dict(sys.modules, {"ace_lib": fake_ace}):
                result = SubmissionGateAdapter(self.repo, "test_run", self.paths.run_dir).run_real()

        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        breakdown = json.loads(candidate["score_breakdown"])
        self.assertIn("self_corr_high", breakdown["failure_tags"])
        self.assertIn("reduce_self_correlation", breakdown["repair_objectives"])
        self.assertFalse(breakdown["gate_passed"])

    def test_submission_gate_rechecks_and_revokes_stale_submit_ready(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="submit_ready",
            alpha_id="A123",
        )

        class FakeFrame:
            empty = False

            def __init__(self, records):
                self.records = records

            def to_dict(self, orient):
                return self.records

        fake_ace = types.SimpleNamespace(
            start_session=lambda: object(),
            get_check_submission=lambda session, alpha_id: FakeFrame(
                [
                    {"test": "SHARPE", "result": "PASS"},
                    {"test": "FITNESS", "result": "FAIL"},
                    {"test": "CONCENTRATED_WEIGHT", "result": "PASS"},
                    {"test": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS"},
                    {"test": "LOW_2Y_SHARPE", "result": "PASS"},
                ]
            ),
            check_self_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "SELF_CORRELATION", "result": "PASS"}]
            ),
            check_prod_corr_test=lambda session, alpha_id: FakeFrame(
                [{"test": "PROD_CORRELATION", "result": "PASS"}]
            ),
        )

        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with patch.dict(sys.modules, {"ace_lib": fake_ace}):
                result = SubmissionGateAdapter(self.repo, "test_run", self.paths.run_dir).run_real()

        self.assertEqual("ok", result.status)
        candidate = self.repo.list_rows("candidates", "test_run")[0]
        self.assertEqual("manual_review", candidate["status"])
        gate = self.repo.list_rows("gate_checks", "test_run")[0]
        self.assertEqual(0, gate["passed"])
        self.assertEqual("FAIL", gate["submission_check"])

    def test_report_excludes_submit_ready_without_latest_gate_pass(self) -> None:
        fp = expression_fingerprint("rank(close)")
        cid = self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="submit_ready",
            alpha_id="A123",
        )
        self.repo.add_gate_check(
            "test_run",
            {
                "candidate_id": cid,
                "alpha_id": "A123",
                "submission_check": "FAIL",
                "self_corr_check": "PASS",
                "prod_corr_check": "PASS",
                "weight_check": "PASS",
                "subuniverse_check": "PASS",
                "gate_status": "complete",
                "passed": False,
            },
        )

        md_path, _ = write_report(self.repo, "test_run", self.paths.run_dir)
        text = md_path.read_text(encoding="utf-8")

        self.assertIn("## Submit Ready Alpha IDs\n- None", text)
        self.assertIn("## Stale Submit Ready Excluded", text)
        self.assertIn("latest_gate_passed=0", text)
        self.assertIn("No candidates are ready for manual submission.", text)

    def test_credentials_env_overrides_secret(self) -> None:
        secret = self.root / "secret.json"
        write_json(secret, {"brain": {"email": "secret@example.com", "password": "secret_pw"}})
        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            creds = load_credentials(secret_path=secret)
        self.assertEqual("env@example.com", creds["brain_email"])
        self.assertEqual("env_pw", creds["brain_password"])

    def test_dry_run_cli_integration(self) -> None:
        with redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "run",
                    "--run-id",
                    "dry_cli",
                    "--dataset",
                    "analyst7",
                    "--region",
                    "USA",
                    "--delay",
                    "1",
                    "--universe",
                    "TOP3000",
                    "--data-type",
                    "Matrix",
                    "--decay",
                    "10",
                    "--truncation",
                    "0.08",
                    "--neutralization",
                    "SLOW_AND_FAST",
                    "--max-trade",
                    "False",
                    "--target-ready",
                    "1",
                    "--max-iterations",
                    "1",
                    "--max-sim-alphas",
                    "2",
                    "--make-prompt-version",
                    "make-test-a",
                    "--enhance-prompt-version",
                    "enhance-test-a",
                    "--decision-prompt-version",
                    "decision-test-a",
                    "--prompt-experiment",
                    "unit-ab",
                    "--dry-run",
                ]
            )
        self.assertEqual(0, code)
        run_dir = self.root / "runs" / "dry_cli"
        self.assertTrue((run_dir / "run_report.md").exists())
        self.assertTrue((run_dir / "run_result.json").exists())
        repo = Repository(run_dir / "brain_agent.sqlite3")
        try:
            run = repo.get_run("dry_cli")
            config = json.loads(run["config_json"])
            artifacts = repo.list_rows("artifacts", "dry_cli")
        finally:
            repo.close()
        self.assertEqual("DONE", run["stage"])
        self.assertEqual(4, config["max_enhance_actions"])
        self.assertFalse(config["use_llm_decide"])
        self.assertEqual("MATRIX", config["data_type"])
        self.assertEqual("SLOW_AND_FAST", config["neutralization"])
        self.assertFalse(config["max_trade"])
        self.assertEqual(2, config["max_sim_alphas"])
        self.assertEqual("make-test-a", config["make_prompt_version"])
        self.assertEqual("enhance-test-a", config["enhance_prompt_version"])
        self.assertEqual("decision-test-a", config["decision_prompt_version"])
        self.assertEqual("unit-ab", config["prompt_experiment"])
        report_text = (run_dir / "run_report.md").read_text(encoding="utf-8")
        self.assertIn("BRAIN Research Log", report_text)
        self.assertIn("Executive Summary", report_text)
        self.assertIn("Prompt Metrics", report_text)
        self.assertIn("Research Timeline", report_text)
        self.assertIn("Decision Journal", report_text)
        self.assertIn("Candidate Lifecycle", report_text)
        self.assertIn("Artifact Ledger", report_text)
        self.assertIn("Lessons And Next Steps", report_text)
        self.assertIn("- Outcome: stage=DONE", report_text)
        enhanced_artifacts = [a for a in artifacts if a["kind"] == "enhanced_expressions"]
        self.assertEqual(1, len(enhanced_artifacts))
        self.assertEqual("ENHANCE", enhanced_artifacts[0]["source_stage"])
        self.assertFalse(
            any(
                a["kind"] == "final_expressions" and "/04_enhance/" in str(a["path"])
                for a in artifacts
            )
        )
        memory_path = self.root / "alpha_memory.sqlite3"
        self.assertTrue(memory_path.exists())
        memory = AlphaMemory(memory_path)
        try:
            summary = memory.summarize(dataset="analyst7", region="USA")
        finally:
            memory.close()
        self.assertEqual(1, summary["run_count"])
        self.assertEqual(10, summary["candidate_observation_count"])
        self.assertEqual(6, summary["generated_observation_count"])
        self.assertEqual(4, summary["learnable_observation_count"])
        self.assertTrue(any(item["key"] == "rank" for item in summary["top_operators"]))

    def test_alpha_memory_cli_ingest_and_summary(self) -> None:
        with redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "run",
                    "--run-id",
                    "memory_cli",
                    "--dataset",
                    "analyst7",
                    "--region",
                    "USA",
                    "--delay",
                    "1",
                    "--universe",
                    "TOP3000",
                    "--data-type",
                    "Matrix",
                    "--target-ready",
                    "1",
                    "--max-iterations",
                    "1",
                    "--dry-run",
                ]
            )
        self.assertEqual(0, code)
        with redirect_stdout(io.StringIO()) as out:
            code = main(["--runtime-root", str(self.root), "memory", "summary", "--format", "json"])
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertGreaterEqual(payload["run_count"], 1)
        self.assertTrue(payload["dataset_settings"])

    def test_alpha_memory_extracts_expression_facts(self) -> None:
        expression = "rank(group_neutralize(ts_zscore(fnd31_score, 60), industry))"
        self.assertEqual(["group_neutralize", "rank", "ts_zscore"], extract_operators(expression))
        self.assertEqual(["fnd31"], extract_field_families(expression))

    def test_prompt_compare_cli_ranks_prompt_versions(self) -> None:
        for run_id, make_version in (("ab_a", "make-a"), ("ab_b", "make-b")):
            with redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--runtime-root",
                        str(self.root),
                        "run",
                        "--run-id",
                        run_id,
                        "--dataset",
                        "analyst7",
                        "--region",
                        "USA",
                        "--delay",
                        "1",
                        "--universe",
                        "TOP3000",
                        "--data-type",
                        "Matrix",
                        "--target-ready",
                        "99",
                        "--max-iterations",
                        "1",
                        "--make-prompt-version",
                        make_version,
                        "--prompt-experiment",
                        "unit-small-ab",
                        "--dry-run",
                    ]
                )
            self.assertEqual(0, code)
        with redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "prompt",
                    "compare",
                    "--run-id",
                    "ab_a",
                    "--run-id",
                    "ab_b",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertIn("winner", payload)
        self.assertEqual(2, len(payload["ranked_runs"]))
        self.assertEqual("unit-small-ab", payload["ranked_runs"][0]["prompt_versions"]["prompt_experiment"])

    def test_doctor_cli_reports_ok_with_env_credentials(self) -> None:
        with patch.dict(os.environ, {"BRAIN_EMAIL": "env@example.com", "BRAIN_PASSWORD": "env_pw"}, clear=False):
            with redirect_stdout(io.StringIO()) as out:
                code = main(["--runtime-root", str(self.root), "doctor"])
        payload = json.loads(out.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(any(check["name"] == "brain_credentials" for check in payload["checks"]))

    def test_gate_cli_dry_run(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status="manual_review",
            alpha_id="DRY0000",
        )
        with redirect_stdout(io.StringIO()):
            code = main(["--runtime-root", str(self.root), "gate", "--run-id", "test_run", "--dry-run"])
        self.assertEqual(0, code)
        self.assertEqual("submit_ready", self.repo.list_rows("candidates", "test_run")[0]["status"])

    def test_decision_engine_rule_fallback_records_cross_and_single(self) -> None:
        actions = DecisionEngine(max_actions=3).decide(
            [
                {"candidate_id": 1, "status": "promising", "expression": "rank(a)"},
                {"candidate_id": 2, "status": "needs_enhance", "expression": "rank(b)"},
                {"candidate_id": 3, "status": "rejected", "expression": "rank(c)"},
            ]
        )
        self.assertEqual("cross", actions[0].mode)
        self.assertEqual([1, 2], actions[0].candidate_ids)
        self.assertTrue(any(a.mode == "single" for a in actions))

    def test_decision_engine_orders_enhance_by_candidate_score(self) -> None:
        actions = DecisionEngine(max_actions=2).decide(
            [
                {"candidate_id": 1, "status": "needs_enhance", "expression": "rank(a)"},
                {
                    "candidate_id": 2,
                    "status": "needs_enhance",
                    "expression": "trade_when(ts_mean(volume, 20), ts_zscore(rank(close), 60), -1)",
                    "latest_sim_result": {
                        "status": "COMPLETE",
                        "sharpe": 1.1,
                        "fitness": 0.75,
                        "turnover": 0.78,
                    },
                    "failure_tags": ["high_turnover"],
                    "repair_objectives": ["reduce_turnover"],
                },
            ]
        )
        self.assertIn(2, actions[0].candidate_ids)

    def test_optimization_tags_identify_repair_targets(self) -> None:
        tags = optimization_tags(
            {"status": "needs_enhance"},
            {
                "sharpe": 0.72,
                "fitness": 0.91,
                "turnover": 0.4,
                "failure_tags": "low_fitness,subuniverse_issue",
                "repair_objectives": "improve_fitness",
            },
            {"weight_check": "FAIL"},
        )
        self.assertIn("repair_low_fitness", tags)
        self.assertIn("repair_low_sharpe", tags)
        self.assertIn("repair_subuniverse", tags)
        self.assertIn("turnover_control_candidate", tags)
        self.assertIn("repair_weight_concentration", tags)

    def test_optimize_candidates_cli_tags_and_enqueues_variants(self) -> None:
        expression = "ts_mean(rank(analyst7_field_a), 20)"
        fp = expression_fingerprint(expression)
        candidate_id = self.repo.upsert_candidate(
            "test_run",
            expression,
            fp,
            status=CandidateStatus.NEEDS_ENHANCE.value,
            source="unit",
        )
        self.repo.add_sim_result(
            "test_run",
            {
                "candidate_id": candidate_id,
                "fingerprint": fp,
                "simulation_fingerprint": fp,
                "alpha_id": "OPT1",
                "sim_id": "SIM1",
                "status": "COMPLETE",
                "sharpe": 0.7,
                "fitness": 0.72,
                "turnover": 0.22,
                "failure_tags": ["low_fitness", "low_sharpe"],
                "repair_objectives": ["improve_fitness", "improve_sharpe"],
            },
        )
        self.repo.update_candidate_score("test_run", candidate_id, 0.82, {"unit": True})

        selected = select_optimization_parents(self.repo, "test_run", max_parents=5)
        self.assertEqual([candidate_id], [int(row["candidate_id"]) for row in selected])

        with redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "optimize-candidates",
                    "--run-id",
                    "test_run",
                    "--max-parents",
                    "5",
                    "--max-variants",
                    "8",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual(1, payload["selected_parent_count"])
        self.assertGreater(payload["variant_count"], 0)

        tag_names = {row["tag"] for row in self.repo.list_candidate_tags("test_run", candidate_id)}
        self.assertIn("repair_low_fitness", tag_names)
        variant_rows = [
            row
            for row in self.repo.list_rows("candidates", "test_run")
            if row.get("parent_candidate_id") == candidate_id
        ]
        self.assertTrue(variant_rows)
        self.assertTrue(all(row["status"] == CandidateStatus.SIM_PENDING.value for row in variant_rows))
        decisions = self.repo.list_rows("decisions", "test_run")
        self.assertTrue(any("manual_optimize_candidates" in row["action"] for row in decisions))

    def test_decision_llm_parser_accepts_fenced_and_wrapped_json(self) -> None:
        fenced = """```json
        [{"mode":"single","style":"balanced","candidate_ids":[2],"reason":"ok"}]
        ```"""
        wrapped = (
            "建议如下："
            "{\"actions\":[{\"mode\":\"cross\",\"style\":\"conservative\",\"candidate_ids\":[1,2],\"reason\":\"pair\"}]}"
        )
        self.assertEqual(2, _parse_llm_actions(fenced)[0]["candidate_ids"][0])
        self.assertEqual("cross", _parse_llm_actions(wrapped)[0]["mode"])

    def test_tasks_cli_refresh_lists_tasks(self) -> None:
        adapter = MakeSomeGemAdapter(self.repo, "test_run", self.paths.run_dir)
        task_id, returncode = adapter.run_command(
            "unit_task",
            ["python3", "-c", "print('task list')"],
            self.root,
        )
        self.assertEqual(0, returncode)
        with redirect_stdout(io.StringIO()) as out:
            code = main(["--runtime-root", str(self.root), "tasks", "--run-id", "test_run", "--refresh"])
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual(task_id, payload[0]["task_id"])

    def test_tasks_cli_cancel_missing_process_marks_exited(self) -> None:
        self.repo.create_task(
            "test_run",
            "missing_task",
            "unit_task",
            pid=99999999,
            status="running",
            stdout_path=self.root / "stdout.log",
            stderr_path=self.root / "stderr.log",
        )
        with redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "tasks",
                    "--run-id",
                    "test_run",
                    "--task-id",
                    "missing_task",
                    "--cancel",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("exited", self.repo.get_task("missing_task")["status"])

    def test_tasks_cli_retry_creates_new_task(self) -> None:
        adapter = MakeSomeGemAdapter(self.repo, "test_run", self.paths.run_dir)
        task_id, returncode = adapter.run_command(
            "unit_task",
            ["python3", "-c", "print('retry ok')"],
            self.root,
        )
        self.assertEqual(0, returncode)
        with redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "tasks",
                    "--run-id",
                    "test_run",
                    "--task-id",
                    task_id,
                    "--retry",
                ]
            )
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual(task_id, payload["previous_task_id"])
        self.assertNotEqual(task_id, payload["new_task_id"])
        self.assertEqual(2, len(self.repo.list_rows("tasks", "test_run")))

    def test_retry_sim_cli_writes_retryable_alpha_list(self) -> None:
        fp = expression_fingerprint("rank(close)")
        self.repo.upsert_candidate(
            "test_run",
            "rank(close)",
            fp,
            status=CandidateStatus.SIM_RETRYABLE.value,
        )

        with redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "retry-sim",
                    "--run-id",
                    "test_run",
                    "--dry-run",
                ]
            )

        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual(1, payload["retryable_count"])
        self.assertFalse(payload["submitted"])
        rows = json.loads(Path(payload["alpha_list"]).read_text(encoding="utf-8"))
        self.assertEqual(["rank(close)"], [row["regular"] for row in rows])
        self.assertEqual("USA", rows[0]["settings"]["region"])

    def test_worker_refill_on_empty_generates_pending_candidates(self) -> None:
        def fake_make(adapter, config):
            idea_path = adapter.artifacts_dir / "01_generate" / "refill_idea.json"
            write_json(idea_path, {"idea": "refill"})
            artifact = adapter.record_artifact("idea_file", idea_path, "GENERATE")
            return AdapterResult(status="ok", artifacts=[artifact])

        def fake_inspect(adapter, idea_file, config):
            path = adapter.write_alpha_list_for_candidates(
                [{"expression": "rank(price_close)"}],
                config,
                name="refill_alpha_list.json",
            )
            return AdapterResult(status="ok", artifacts=[adapter.record_artifact("alpha_list", path, "INSPECT")])

        with patch.object(MakeSomeGemAdapter, "run_real", fake_make), patch.object(
            InspectRawTemplateAdapter, "run_real", fake_inspect
        ), patch.object(
            InspectRawTemplateAdapter,
            "write_field_factory_alpha_list",
            return_value=AdapterResult(status="ok"),
        ), patch.object(
            BatchSimAdapter,
            "run_real",
            return_value=AdapterResult(status="ok", metrics_delta=[{"status": "COMPLETE"}]),
        ):
            stats = SimulationWorker(self.repo, "test_run", self.paths, self.config).run_once(refill_on_empty=True)

        self.assertEqual(1, stats.total_submitted)
        rows = self.repo.find_candidates_by_status("test_run", [CandidateStatus.SIM_PENDING.value])
        self.assertEqual(["rank(price_close)"], [row["expression"] for row in rows])

    def test_worker_refill_uses_generated_expressions_directly(self) -> None:
        def fake_make(adapter, config):
            return AdapterResult(
                status="ok",
                candidates_delta=[
                    {"candidate_id": 1, "expression": "rank(ts_mean(price_close, 20))", "fingerprint": "fp"}
                ],
            )

        with patch.object(MakeSomeGemAdapter, "run_real", fake_make), patch.object(
            InspectRawTemplateAdapter,
            "write_field_factory_alpha_list",
            return_value=AdapterResult(status="ok"),
        ), patch.object(
            BatchSimAdapter,
            "run_real",
            return_value=AdapterResult(status="ok", metrics_delta=[{"status": "COMPLETE"}]),
        ):
            stats = SimulationWorker(self.repo, "test_run", self.paths, self.config).run_once(refill_on_empty=True)

        self.assertEqual(1, stats.total_submitted)
        rows = self.repo.find_candidates_by_status("test_run", [CandidateStatus.SIM_PENDING.value])
        self.assertEqual(["rank(ts_mean(price_close, 20))"], [row["expression"] for row in rows])

    def test_worker_periodic_optimization_enqueues_variants_every_threshold(self) -> None:
        parent_expr = "ts_mean(rank(analyst7_field_a), 20)"
        parent_fp = expression_fingerprint(parent_expr)
        parent_id = self.repo.upsert_candidate(
            "test_run",
            parent_expr,
            parent_fp,
            status=CandidateStatus.NEEDS_ENHANCE.value,
            source="unit",
        )
        self.repo.add_sim_result(
            "test_run",
            {
                "candidate_id": parent_id,
                "fingerprint": parent_fp,
                "simulation_fingerprint": parent_fp,
                "alpha_id": "AUTOOPT1",
                "sim_id": "SIM_AUTOOPT1",
                "status": "COMPLETE",
                "sharpe": 0.8,
                "fitness": 0.72,
                "turnover": 0.2,
                "failure_tags": ["low_fitness", "low_sharpe"],
                "repair_objectives": ["improve_fitness", "improve_sharpe"],
            },
        )
        self.repo.update_candidate_score("test_run", parent_id, 0.86, {"unit": True})
        for idx in range(500):
            expr = f"rank(analyst7_pending_{idx})"
            self.repo.upsert_candidate(
                "test_run",
                expr,
                expression_fingerprint(expr),
                status=CandidateStatus.SIM_PENDING.value,
                source="unit_pending",
            )

        with patch.object(
            BatchSimAdapter,
            "run_real",
            return_value=AdapterResult(status="ok", metrics_delta=[{"status": "COMPLETE"} for _ in range(500)]),
        ):
            stats = SimulationWorker(self.repo, "test_run", self.paths, self.config).run_drain(
                max_batches=1,
                max_candidates_per_batch=500,
                optimize_every_alphas=500,
                optimize_max_parents=5,
                optimize_max_variants=8,
            )

        self.assertEqual(500, stats.total_submitted)
        self.assertEqual(1, stats.optimization_passes)
        self.assertGreater(stats.optimization_variants, 0)
        tag_rows = self.repo.list_candidate_tags("test_run", parent_id)
        self.assertTrue(any(row["source"].startswith("auto_optimize:") for row in tag_rows))
        variants = [
            row
            for row in self.repo.list_rows("candidates", "test_run")
            if row.get("parent_candidate_id") == parent_id
        ]
        self.assertTrue(variants)
        self.assertTrue(all(row["status"] == CandidateStatus.SIM_PENDING.value for row in variants))

    def test_batch_simulator_slot_limits_are_region_aware(self) -> None:
        batch_simulator = _load_batch_simulator_module()
        normalize = batch_simulator._normalize_slot_limits
        simulator = batch_simulator.BatchSimulator(session=object(), output_csv=str(self.root / "sim_status.csv"))

        self.assertEqual((10, 8, 10, 8, 10, 8), normalize("EUR", batch_size=None, concurrency=None))
        self.assertEqual((10, 8, 10, 8, 10, 8), normalize("USA", batch_size=None, concurrency=None))
        self.assertEqual((4, 4, 4, 4, 10, 4), normalize("GLB", batch_size=None, concurrency=None))
        self.assertEqual((10, 8, 10, 8, 10, 8), normalize("EUR", batch_size=99, concurrency=99))
        self.assertEqual((10, 4, 4, 4, 10, 4), normalize("GLB", batch_size=99, concurrency=99))
        self.assertEqual((1, 1, 10, 8, 10, 8), normalize("EUR", batch_size=0, concurrency=0))
        self.assertEqual((1, 1, 4, 4, 10, 4), normalize("GLB", batch_size=-2, concurrency=-3))
        self.assertEqual(30 * 60, simulator.parent_wait_seconds)
        self.assertEqual(60 * 60, simulator.child_wait_seconds)
        self.assertEqual(15 * 60, simulator.stale_healthcheck_seconds)

    def test_batch_simulator_stale_healthcheck_refreshes_session(self) -> None:
        batch_simulator = _load_batch_simulator_module()
        simulator = batch_simulator.BatchSimulator(session="old", output_csv=str(self.root / "sim_status.csv"))
        simulator.stale_healthcheck_seconds = 1
        marker = {"started_at": 100.0, "next_at": 100.0, "count": 0}

        with patch.object(batch_simulator.time, "time", return_value=120.0):
            with patch.object(batch_simulator.ace_lib, "start_session", return_value="new") as start_session:
                simulator._maybe_run_stale_healthcheck(marker, context="unit", status_summary="status=IN_PROGRESS")

        self.assertEqual("new", simulator.session)
        self.assertEqual(1, marker["count"])
        self.assertEqual(121.0, marker["next_at"])
        start_session.assert_called_once()

    def test_prompt_env_exports_template_research_policy(self) -> None:
        env = prompt_env(self.config)
        policy = json.loads(env["BRAIN_AGENT_RESEARCH_POLICY_JSON"])
        self.assertEqual("template_guided", policy["mode"])
        self.assertEqual(8, policy["diversity_targets"]["min_variants_per_batch"])
        self.assertIn("macro", policy["dataset_template_routing"])
        self.assertIn("profit_to_size_ratio", policy["high_signal_template_archetypes"])

        make_pipeline = _load_make_pipeline_module()
        section = make_pipeline.render_research_policy_section(policy)
        self.assertIn("BRAIN Agent Research Policy", section)
        self.assertIn("template_guided", section)
        self.assertIn("dataset_template_routing", section)

    def test_forum_post_analysis_extracts_brain_terms(self) -> None:
        payload = {
            "post": {
                "title": "Turnover and submit tips",
                "author": "mentor",
                "body": (
                    "You should use rank(ts_delta(close, 5)) and check turnover before submit. "
                    "The alpha needs good sharpe and fitness, and avoid high self-correlation."
                ),
            },
            "comments": [
                {
                    "author": "user",
                    "body": "可以尝试 decay 和 neutralization 设置，dataset 字段比如 fnd31_qscore 也要检查。",
                }
            ],
        }
        analysis = analyze_forum_post(payload)
        self.assertIn("alpha", analysis["brain_terms"])
        self.assertIn("turnover", analysis["brain_terms"])
        self.assertIn("ts_delta", analysis["operator_mentions"])
        self.assertIn("fnd31_qscore", analysis["datafield_like_mentions"])
        self.assertTrue(analysis["actionable_takeaways"])

    def test_forum_search_analysis_ranks_by_votes_and_comments(self) -> None:
        analysis = analyze_search_results(
            "turnover",
            [
                {"title": "low vote", "votes": 1, "comments": 0, "snippet": "alpha turnover"},
                {"title": "busy thread", "votes": 0, "comments": 8, "snippet": "submit discussion"},
                {"title": "best", "votes": 3, "comments": 1, "snippet": "sharpe fitness"},
            ],
        )
        self.assertEqual("best", analysis["top_results"][0]["title"])
        self.assertEqual(3, analysis["result_count"])

    def test_forum_cli_search_uses_service_and_outputs_json(self) -> None:
        class FakeForumService:
            def __init__(self, *, secret_path=None):
                self.secret_path = secret_path

            async def search(self, query, *, max_results=20, locale="zh-cn"):
                return {
                    "success": True,
                    "results": [{"title": query, "votes": 1, "comments": 2}],
                    "analysis": {"result_count": 1, "top_results": [{"title": query}]},
                }

        with patch("brain_agent.cli.ForumService", FakeForumService):
            with redirect_stdout(io.StringIO()) as out:
                code = main(["forum", "search", "turnover", "--max-results", "1"])
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual("turnover", payload["results"][0]["title"])

    def test_forum_learning_llm_summary_requires_approval(self) -> None:
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return json.loads(self.text)

            @property
            def text(self):
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "summary_cn": "论坛经验强调先控 turnover 再看 sharpe。",
                                            "experience_lessons": ["低换手和稳定 fitness 更重要。"],
                                            "alpha_research_patterns": ["优先复用低相关数据字段组合。"],
                                            "pitfalls": ["不要直接 submit 未检查相关性的 alpha。"],
                                            "proposed_system_updates": [
                                                {
                                                    "title": "加入论坛经验提示",
                                                    "target": "brain_agent generate prompt",
                                                    "why": "帖子多次提到 turnover",
                                                    "change": "生成前注入低换手约束",
                                                    "risk": "可能降低探索多样性",
                                                    "approval_status": "pending",
                                                }
                                            ],
                                            "approval_required": True,
                                            "questions_for_user": [],
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

        payload = {"query": "turnover", "read_posts": [], "local_analysis": {}}
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "key"}, clear=False):
            with patch("requests.post", return_value=FakeResponse()):
                result = summarize_forum_learning_with_llm(payload)
        self.assertTrue(result["approval_required"])
        self.assertEqual("pending", result["proposed_system_updates"][0]["approval_status"])

    def test_forum_cli_learn_can_save_markdown_report(self) -> None:
        class FakeForumService:
            def __init__(self, *, secret_path=None):
                self.secret_path = secret_path

            async def learn(self, query, *, max_results=10, read_top=3, locale="zh-cn", include_comments=True):
                return {
                    "success": True,
                    "query": query,
                    "llm_learning": {
                        "summary_cn": "总结",
                        "experience_lessons": ["经验"],
                        "alpha_research_patterns": ["模式"],
                        "pitfalls": ["坑"],
                        "proposed_system_updates": [
                            {
                                "title": "优化提示词",
                                "target": "makeSomeGem",
                                "change": "加入论坛经验",
                                "risk": "需审批",
                                "approval_status": "pending",
                            }
                        ],
                    },
                    "approval_required": True,
                }

        out_path = self.root / "forum_learning.md"
        with patch("brain_agent.cli.ForumService", FakeForumService):
            with redirect_stdout(io.StringIO()) as out:
                code = main(["forum", "learn", "turnover", "--output", str(out_path)])
        self.assertEqual(0, code)
        self.assertEqual(str(out_path.resolve()), out.getvalue().strip())
        text = out_path.read_text(encoding="utf-8")
        self.assertIn("Approval Gate", text)
        self.assertIn("No system optimization has been applied", text)
        self.assertIn("brain_agent_forum_learning_payload", text)

    def test_forum_cli_daily_learn_saves_report_to_runtime_by_default(self) -> None:
        class FakeForumService:
            def __init__(self, *, secret_path=None):
                self.secret_path = secret_path

            async def daily_learn(
                self,
                *,
                queries=None,
                max_results_per_query=12,
                read_top=5,
                locale="zh-cn",
                include_comments=True,
                history_path=None,
                reread_after_days=14,
            ):
                Path(history_path).parent.mkdir(parents=True, exist_ok=True)
                Path(history_path).write_text(
                    json.dumps({"link": "https://forum/post-1", "read_at": "2026-04-29T00:00:00+00:00"})
                    + "\n",
                    encoding="utf-8",
                )
                return {
                    "success": True,
                    "mode": "daily_learn",
                    "query": "daily high-quality BRAIN forum learning",
                    "queries": queries,
                    "local_analysis": {
                        "history_path": str(history_path),
                        "reread_after_days": reread_after_days,
                    },
                    "llm_learning": {
                        "summary_cn": "每日总结",
                        "experience_lessons": ["经验"],
                        "alpha_research_patterns": ["模式"],
                        "pitfalls": ["坑"],
                        "proposed_system_updates": [
                            {
                                "title": "每日学习建议",
                                "target": "brain_agent",
                                "change": "等待用户批准后再优化",
                                "risk": "无",
                                "approval_status": "pending",
                            }
                        ],
                    },
                    "approval_required": True,
                }

        with patch("brain_agent.cli.ForumService", FakeForumService):
            with redirect_stdout(io.StringIO()) as out:
                code = main(
                    [
                        "--runtime-root",
                        str(self.root),
                        "forum",
                        "daily-learn",
                        "--query",
                        "turnover",
                        "--query",
                        "submission",
                        "--read-top",
                        "2",
                    ]
                )
        self.assertEqual(0, code)
        report_path = Path(out.getvalue().strip())
        self.assertTrue(report_path.exists())
        self.assertEqual((self.root / "forum_learning").resolve(), report_path.parent)
        text = report_path.read_text(encoding="utf-8")
        self.assertIn("Approval Gate", text)
        self.assertIn("每日学习建议", text)
        self.assertIn("brain_agent_forum_learning_payload", text)
        self.assertTrue((self.root / "forum_learning" / "read_history.jsonl").exists())

    def test_daily_learning_prioritizes_unread_posts_with_history(self) -> None:
        from datetime import datetime

        from brain_agent.forum import _prioritize_unread_posts

        history = {"link-a": {"read_at": datetime.now().astimezone().isoformat()}}
        ranked = [
            {"title": "A", "link": "link-a", "votes": 100, "comments": 100},
            {"title": "B", "link": "link-b", "votes": 10, "comments": 10},
            {"title": "C", "link": "link-c", "votes": 5, "comments": 5},
        ]
        ordered, recent = _prioritize_unread_posts(ranked, history, read_top=2, reread_after_days=14)
        self.assertEqual(["link-b", "link-c"], [item["link"] for item in ordered[:2]])
        self.assertEqual(["link-a"], [item["link"] for item in recent])

    def test_forum_search_click_link_is_canonicalized(self) -> None:
        from brain_agent.forum import _canonical_forum_link

        target = "https://support.worldquantbrain.com/hc/zh-cn/community/posts/33745533142679--SuperAlpha-SELECTION"
        link = "https://support.worldquantbrain.com/hc/zh-cn/search/click?data=abc" + urllib.parse.quote(
            f'urlI"{target}\x06;'
        )
        self.assertEqual(target, _canonical_forum_link(link))
        b64_link = "https://support.worldquantbrain.com/hc/zh-cn/search/click?data=CHVybEki" + base64.b64encode(
            f'{{{target}\x06;'.encode("utf-8")
        ).decode("ascii")
        self.assertEqual(target, _canonical_forum_link(b64_link))

    def test_forum_learning_report_can_be_approved_into_knowledge(self) -> None:
        report = self.root / "forum_learning.md"
        payload = {
            "success": True,
            "query": "turnover",
            "llm_learning": {
                "summary_cn": "先控制 turnover。",
                "experience_lessons": ["用 decay 平滑信号。"],
                "alpha_research_patterns": ["优先检查换手和 fitness。"],
                "pitfalls": ["不要直接把高换手 alpha submit。"],
                "proposed_system_updates": [
                    {
                        "title": "加入 turnover 修复知识",
                        "target": "enhance",
                        "why": "论坛经验反复提到 turnover",
                        "change": "enhance 时优先尝试 decay/hump/trade_when",
                        "risk": "可能牺牲一部分 sharpe",
                        "approval_status": "pending",
                    }
                ],
                "approval_required": True,
            },
            "approval_required": True,
        }
        report.write_text(
            "# BRAIN Forum Learn\n\n## Machine Readable\n```json brain_agent_forum_learning_payload\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n```\n",
            encoding="utf-8",
        )
        result = approve_forum_lesson(
            report,
            title="加入 turnover 修复知识",
            knowledge_dir=self.root / "approved_lessons",
            approved_by="tester",
        )
        lesson_path = Path(result["lesson_path"])
        self.assertTrue(lesson_path.exists())
        text = lesson_path.read_text(encoding="utf-8")
        self.assertIn("brain_agent_approved_forum_lesson", text)
        self.assertIn("approval_status", text)
        self.assertIn("approved", text)
        prompt = render_approved_lessons_prompt(self.root / "approved_lessons")
        self.assertIn("Approved BRAIN Forum Lessons", prompt)
        self.assertIn("加入 turnover 修复知识", prompt)

    def test_approved_lessons_prompt_uses_progressive_disclosure_budget(self) -> None:
        knowledge_dir = self.root / "approved_budget"
        long_text = "x" * 2000
        for idx in range(10):
            lesson = {
                "title": f"lesson {idx}",
                "approved_at": f"2026-04-28T00:00:{idx:02d}+00:00",
                "approved_by": "tester",
                "query": "turnover" if idx == 9 else "other",
                "source_report": "source.md",
                "summary_cn": long_text,
                "experience_lessons": [long_text] * 5,
                "alpha_research_patterns": [long_text] * 5,
                "pitfalls": [long_text] * 5,
                "approved_system_updates": [
                    {"title": f"update {idx}", "target": "enhance", "change": long_text, "risk": long_text}
                ],
                "approval_required": False,
            }
            lesson_path = knowledge_dir / f"lesson_{idx}.md"
            lesson_path.parent.mkdir(parents=True, exist_ok=True)
            lesson_path.write_text(
                "```json brain_agent_approved_forum_lesson\n" + json.dumps(lesson, ensure_ascii=False) + "\n```",
                encoding="utf-8",
            )
            with (knowledge_dir / "index.jsonl").open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "title": lesson["title"],
                            "approved_at": lesson["approved_at"],
                            "query": lesson["query"],
                            "path": str(lesson_path),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        prompt = render_approved_lessons_prompt(knowledge_dir, limit=3, max_chars=2500, query="turnover")
        self.assertLessEqual(len(prompt), 2500)
        self.assertIn("progressive", prompt)
        self.assertIn("lesson 9", prompt)
        self.assertNotIn(long_text, prompt)

    def test_forum_learning_legacy_markdown_report_can_be_loaded(self) -> None:
        report = self.root / "legacy_forum_learning.md"
        report.write_text(
            """# BRAIN Forum Learn

Query: turnover

## Summary
总结

## Experience Lessons
- 经验一

## Alpha Research Patterns
- 模式一

## Pitfalls
- 坑一

## Proposed System Updates
- 优化提示词 [pending]
  target: enhance
  change: 加入低换手约束
  risk: 需要审批

## Approval Gate
- No system optimization has been applied.
""",
            encoding="utf-8",
        )
        payload = load_forum_learning_report(report)
        self.assertEqual("turnover", payload["query"])
        learning = payload["llm_learning"]
        self.assertEqual(["经验一"], learning["experience_lessons"])
        self.assertEqual("优化提示词", learning["proposed_system_updates"][0]["title"])

    def test_forum_learning_report_with_bad_machine_json_falls_back_to_markdown(self) -> None:
        report = self.root / "bad_machine_json_forum_learning.md"
        report.write_text(
            """# BRAIN Forum Learn

Query: alpha template

## Summary
模板总结

## Experience Lessons
- 只使用模板变体。

## Alpha Research Patterns
- 模板填充模式。

## Pitfalls
- 不要裸信号乱试。

## Proposed System Updates
- 集成模板库驱动的自动化流水线 [pending]
  target: generate
  change: 走模板策略
  risk: 模板过拟合

## Machine Readable
```json brain_agent_forum_learning_payload
{"llm_learning": {"summary_cn": "bad
```
""",
            encoding="utf-8",
        )
        payload = load_forum_learning_report(report)
        learning = payload["llm_learning"]
        self.assertEqual("alpha template", payload["query"])
        self.assertEqual(["只使用模板变体。"], learning["experience_lessons"])
        self.assertEqual("集成模板库驱动的自动化流水线", learning["proposed_system_updates"][0]["title"])

    def test_template_library_report_normalizes_generic_machine_json(self) -> None:
        report = self.root / "alpha_templates.md"
        report.write_text(
            """# BRAIN Forum Alpha Template Library

## 实战经验精华
- 经济学时间窗口
- get_operators 权限
- to_nan + ts_quantile
- 厂字形

## Machine Readable (JSON)
```json
{
  "approval_required": true,
  "total_templates": 44,
  "template_categories": {
    "macro": {"count": 1, "range": "TPL-MACRO-001"}
  },
  "key_insights": [
    "Macro数据处理: to_nan + ts_backfill + ts_quantile 优于直接winsorize"
  ],
  "top_rated_templates": [
    "TPL-MACRO-001: Macro泛化"
  ]
}
```
""",
            encoding="utf-8",
        )

        payload = load_forum_learning_report(report)
        learning = payload["llm_learning"]

        self.assertEqual("template_library", learning["source_report_type"])
        self.assertEqual("吸纳高票论坛Alpha模板库", learning["proposed_system_updates"][0]["title"])
        self.assertIn("TPL-MACRO-001: Macro泛化", learning["alpha_research_patterns"])
        self.assertTrue(any("factory-shaped" in item for item in learning["pitfalls"]))

    def test_knowledge_cli_approves_forum_lesson(self) -> None:
        report = self.root / "forum_learning.json"
        write_json(
            report,
            {
                "success": True,
                "query": "submit",
                "llm_learning": {
                    "summary_cn": "总结",
                    "experience_lessons": ["经验"],
                    "alpha_research_patterns": ["模式"],
                    "pitfalls": ["坑"],
                    "proposed_system_updates": [
                        {
                            "title": "submit gate tips",
                            "target": "gate",
                            "change": "记录相关性检查",
                            "risk": "无",
                            "approval_status": "pending",
                        }
                    ],
                    "approval_required": True,
                },
            },
        )
        knowledge_dir = self.root / "knowledge"
        with redirect_stdout(io.StringIO()) as out:
            code = main(
                [
                    "knowledge",
                    "approve-forum-lesson",
                    "--report",
                    str(report),
                    "--title",
                    "submit gate tips",
                    "--knowledge-dir",
                    str(knowledge_dir),
                ]
            )
        self.assertEqual(0, code)
        result = json.loads(out.getvalue())
        self.assertTrue(Path(result["lesson_path"]).exists())
        self.assertTrue((knowledge_dir / "index.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
