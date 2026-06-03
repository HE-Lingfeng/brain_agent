from __future__ import annotations

import json
from pathlib import Path

from .adapters import (
    BatchSimAdapter,
    EnhanceTemplateAdapter,
    InspectRawTemplateAdapter,
    MakeSomeGemAdapter,
    SubmissionGateAdapter,
    VariantSearchAdapter,
)
from .decision import DecisionEngine
from ..analysis.memory import AlphaMemory, memory_path_for_run_dir
from ..core.models import CandidateStatus, RunConfig, RunStage
from ..analysis.reporting import write_report
from ..core.repository import Repository
from ..core.runtime import RuntimePaths
from ..analysis.scoring import score_candidate, score_candidates


class BatchLoopController:
    def __init__(self, repo: Repository, run_id: str, paths: RuntimePaths):
        self.repo = repo
        self.run_id = run_id
        self.paths = paths

    def run(self, config: RunConfig, *, resume: bool = False) -> str:
        try:
            if not config.dry_run:
                return self._run_real(config, resume=resume)

            for iteration in range(1, config.max_iterations + 1):
                self.repo.update_run_stage(self.run_id, RunStage.GENERATE.value)
                MakeSomeGemAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run(config)

                self.repo.update_run_stage(self.run_id, RunStage.INSPECT.value)
                InspectRawTemplateAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run(config)

                self.repo.update_run_stage(self.run_id, RunStage.SIMULATE.value)
                BatchSimAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run()
                self.repo.update_run_stage(self.run_id, RunStage.VARIANT_SEARCH.value)
                VariantSearchAdapter(self.repo, self.run_id, self.paths.run_dir).run(
                    self._latest_artifact_path(kind="alpha_list_combined", stage="INSPECT")
                    or self.paths.artifacts_dir / "02_inspect" / "alpha_list.json",
                    config,
                    iteration=iteration,
                )

                ready = self.repo.count_candidates(self.run_id, CandidateStatus.SUBMIT_READY.value)
                if ready >= config.target_ready:
                    return self._finish(f"target_ready reached before gate: {ready}")

                self.repo.update_run_stage(self.run_id, RunStage.DECIDE.value)
                enhance_inputs = self._enhance_inputs()
                actions = DecisionEngine(
                    max_actions=config.max_enhance_actions,
                    use_llm=config.use_llm_decide,
                    prompt_version=config.decision_prompt_version,
                ).decide(enhance_inputs)
                if actions:
                    source = "LLM" if any(a.source == "llm" for a in actions) else "Rule-based"
                    self.repo.add_decision(
                        self.run_id,
                        iteration,
                        json.dumps([a.to_dict() for a in actions], ensure_ascii=False),
                        f"{source} dry-run enhancement selection; hard gate checks remain authoritative.",
                        enhance_inputs,
                    )

                self.repo.update_run_stage(self.run_id, RunStage.ENHANCE.value)
                if enhance_inputs:
                    EnhanceTemplateAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run(limit=2)

                self.repo.update_run_stage(self.run_id, RunStage.SIMULATE.value)
                BatchSimAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run()

                self.repo.update_run_stage(self.run_id, RunStage.SUBMIT_GATE.value)
                SubmissionGateAdapter(self.repo, self.run_id, self.paths.run_dir).dry_run()

                ready = self.repo.count_candidates(self.run_id, CandidateStatus.SUBMIT_READY.value)
                if ready >= config.target_ready:
                    return self._finish(f"target_ready reached: {ready}")

            ready = self.repo.count_candidates(self.run_id, CandidateStatus.SUBMIT_READY.value)
            return self._finish(f"max_iterations reached with submit_ready={ready}")
        except Exception as exc:
            run = self.repo.get_run(self.run_id) or {}
            stage = run.get("stage") or "UNKNOWN"
            return self._fail(f"Unhandled run error in {stage}: {type(exc).__name__}: {exc}")

    def _run_real(self, config: RunConfig, *, resume: bool = False) -> str:
        run = self.repo.get_run(self.run_id)
        if resume and run and run.get("stage") == RunStage.DONE.value:
            write_report(self.repo, self.run_id, self.paths.run_dir)
            return run.get("stop_reason") or "already done"

        idea_files = self._artifact_paths(kind="idea_file", stage="GENERATE") if resume else []
        if not idea_files:
            self.repo.update_run_stage(self.run_id, RunStage.GENERATE.value)
            generate = MakeSomeGemAdapter(self.repo, self.run_id, self.paths.run_dir).run_real(config)
            if generate.status != "ok":
                return self._fail(generate.error_summary or "GENERATE failed")
            idea_files = self._artifact_paths(kind="idea_file", stage="GENERATE")
        if not idea_files:
            return self._fail("GENERATE completed but no idea files were recorded")

        inspect_adapter = InspectRawTemplateAdapter(self.repo, self.run_id, self.paths.run_dir)
        combined_existing = self._latest_artifact_path(kind="alpha_list_combined", stage="INSPECT") if resume else None
        if combined_existing:
            combined_alpha_list = combined_existing
        else:
            self.repo.update_run_stage(self.run_id, RunStage.INSPECT.value)
            alpha_lists: list[Path] = []
            for idea in idea_files:
                inspected = inspect_adapter.run_real(idea, config)
                if inspected.status != "ok":
                    return self._fail(inspected.error_summary or f"INSPECT failed for {idea}")
                for artifact in inspected.artifacts:
                    if artifact.get("kind") == "alpha_list":
                        path = Path(str(artifact.get("path")))
                        if path.exists():
                            alpha_lists.append(path)
            field_factory = inspect_adapter.write_field_factory_alpha_list(config)
            if field_factory.status != "ok":
                return self._fail(field_factory.error_summary or "Field Factory failed")
            for artifact in field_factory.artifacts:
                if artifact.get("kind") == "alpha_list_field_factory":
                    path = Path(str(artifact.get("path")))
                    if path.exists():
                        alpha_lists.append(path)
            if not alpha_lists:
                return self._fail("INSPECT completed but no alpha_list artifacts were recorded")
            combined_alpha_list = inspect_adapter.write_combined_alpha_list(alpha_lists)

        for iteration in range(1, config.max_iterations + 1):
            existing_sim = self._latest_artifact_path(kind="simulation_status", stage="SIMULATE") if resume and iteration == 1 else None
            if existing_sim:
                self.repo.update_run_stage(self.run_id, RunStage.SIMULATE.value)
                simulated = BatchSimAdapter(self.repo, self.run_id, self.paths.run_dir).parse_simulation_status(existing_sim)
            else:
                self.repo.update_run_stage(self.run_id, RunStage.SIMULATE.value)
                simulated = BatchSimAdapter(self.repo, self.run_id, self.paths.run_dir).run_real(combined_alpha_list, config)
            if simulated.status != "ok":
                return self._fail(simulated.error_summary or "SIMULATE failed")

            if iteration < config.max_iterations and int(config.max_variant_alphas) > 0:
                self.repo.update_run_stage(self.run_id, RunStage.VARIANT_SEARCH.value)
                variant_result = VariantSearchAdapter(self.repo, self.run_id, self.paths.run_dir).run(
                    combined_alpha_list,
                    config,
                    iteration=iteration,
                )
                if variant_result.status != "ok":
                    return self._fail(variant_result.error_summary or "VARIANT_SEARCH failed")
                variant_alpha_list = self._latest_artifact_path(kind="alpha_list_variants", stage="VARIANT_SEARCH")
                if variant_result.candidates_delta and variant_alpha_list and _path_has_rows(variant_alpha_list):
                    self.repo.add_decision(
                        self.run_id,
                        iteration,
                        json.dumps(
                            {
                                "type": "variant_search",
                                "variant_count": len(variant_result.candidates_delta),
                                "alpha_list": str(variant_alpha_list),
                            },
                            ensure_ascii=False,
                        ),
                        "Generated local variants from simulated candidates with weak or repairable signal.",
                        variant_result.candidates_delta,
                    )
                    self.repo.update_run_stage(self.run_id, RunStage.SIMULATE.value)
                    variant_simulated = BatchSimAdapter(self.repo, self.run_id, self.paths.run_dir).run_real(
                        variant_alpha_list,
                        config,
                    )
                    if variant_simulated.status != "ok":
                        return self._fail(variant_simulated.error_summary or "SIMULATE variants failed")

            self.repo.update_run_stage(self.run_id, RunStage.DECIDE.value)
            enhance_inputs = self._enhance_inputs()
            actions = DecisionEngine(
                max_actions=config.max_enhance_actions,
                use_llm=config.use_llm_decide,
                prompt_version=config.decision_prompt_version,
            ).decide(enhance_inputs)
            if actions:
                source = "LLM" if any(a.source == "llm" for a in actions) else "Rule-based"
                self.repo.add_decision(
                    self.run_id,
                    iteration,
                    json.dumps([a.to_dict() for a in actions], ensure_ascii=False),
                    f"{source} selected promising/needs_enhance candidates for template enhancement.",
                    enhance_inputs,
                )

            if actions and iteration < config.max_iterations:
                self.repo.update_run_stage(self.run_id, RunStage.ENHANCE.value)
                selected_ids = actions[0].candidate_ids
                selected_inputs = [c for c in enhance_inputs if int(c.get("candidate_id") or 0) in selected_ids]
                candidate_idea_files = self._idea_files_for_enhance(selected_inputs, idea_files)
                before_ids = {int(c["candidate_id"]) for c in self.repo.list_rows("candidates", self.run_id)}
                before_artifact_ids = {int(a["artifact_id"]) for a in self.repo.list_rows("artifacts", self.run_id)}
                enhanced = EnhanceTemplateAdapter(self.repo, self.run_id, self.paths.run_dir).run_real(
                    candidate_idea_files[:2],
                    config,
                    style=actions[0].style,
                    candidates_context=selected_inputs,
                )
                if enhanced.status != "ok":
                    return self._fail(enhanced.error_summary or "ENHANCE failed")
                produced_artifacts = [
                    a
                    for a in self.repo.list_rows("artifacts", self.run_id)
                    if int(a.get("artifact_id") or 0) not in before_artifact_ids
                ]
                self.repo.add_decision(
                    self.run_id,
                    iteration,
                    json.dumps(
                        {
                            "type": "enhance_result",
                            "action": actions[0].to_dict(),
                            "produced_artifacts": [
                                {"kind": a.get("kind"), "path": a.get("path"), "sha256": a.get("sha256")}
                                for a in produced_artifacts
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    "Recorded artifacts produced by enhancement.",
                    selected_inputs,
                )
                enhanced_candidates = [
                    c
                    for c in self.repo.list_rows("candidates", self.run_id)
                    if int(c.get("candidate_id") or 0) not in before_ids
                ]
                if enhanced_candidates:
                    combined_alpha_list = inspect_adapter.write_alpha_list_for_candidates(
                        enhanced_candidates,
                        config,
                        name=f"alpha_list_enhanced_iter{iteration}.json",
                    )

            self.repo.update_run_stage(self.run_id, RunStage.SUBMIT_GATE.value)
            gated = SubmissionGateAdapter(self.repo, self.run_id, self.paths.run_dir).run_real()
            if gated.status != "ok":
                return self._fail(gated.error_summary or "SUBMIT_GATE failed")
            ready = self.repo.count_candidates(self.run_id, CandidateStatus.SUBMIT_READY.value)
            if ready >= config.target_ready:
                return self._finish(f"target_ready reached: {ready}")

        ready = self.repo.count_candidates(self.run_id, CandidateStatus.SUBMIT_READY.value)
        return self._finish(f"max_iterations reached with submit_ready={ready}")

    def resume(self) -> str:
        run = self.repo.get_run(self.run_id)
        if not run:
            raise ValueError(f"Run not found: {self.run_id}")
        config = RunConfig(**json.loads(run["config_json"]))
        return self.run(config, resume=True)

    def _finish(self, reason: str) -> str:
        self.repo.update_run_stage(self.run_id, RunStage.REPORT.value, reason)
        self.repo.update_run_stage(self.run_id, RunStage.DONE.value, reason)
        write_report(self.repo, self.run_id, self.paths.run_dir)
        return reason

    def _fail(self, reason: str) -> str:
        self.repo.update_run_stage(self.run_id, RunStage.FAILED.value, reason)
        write_report(self.repo, self.run_id, self.paths.run_dir)
        return reason

    @staticmethod
    def _idea_files_for_enhance(candidates: list[dict], fallback_ideas: list[Path]) -> list[Path]:
        paths: list[Path] = []
        for candidate in candidates:
            idea = candidate.get("idea_file")
            if idea and Path(str(idea)).exists():
                paths.append(Path(str(idea)))
        return paths or fallback_ideas

    def _enhance_inputs(self) -> list[dict]:
        latest_sim = self.repo.latest_sim_results_by_candidate(self.run_id)
        latest_gate = _latest_gate_by_candidate(self.repo, self.run_id)
        rows = [
            c
            for c in self.repo.list_rows("candidates", self.run_id)
            if c.get("status") in {CandidateStatus.PROMISING.value, CandidateStatus.NEEDS_ENHANCE.value}
        ]
        enriched: list[dict] = []
        for candidate in rows:
            item = dict(candidate)
            sim = latest_sim.get(int(candidate.get("candidate_id") or 0))
            gate = latest_gate.get(int(candidate.get("candidate_id") or 0))
            if sim:
                item["latest_sim_result"] = sim
                item["failure_tags"] = [
                    tag.strip() for tag in str(sim.get("failure_tags") or "").split(",") if tag.strip()
                ]
                item["repair_objectives"] = [
                    obj.strip() for obj in str(sim.get("repair_objectives") or "").split(",") if obj.strip()
                ]
                try:
                    item["diagnosis"] = json.loads(sim.get("diagnosis_json") or "{}")
                except json.JSONDecodeError:
                    item["diagnosis"] = {}
            if gate:
                item["latest_gate_check"] = gate
            score = score_candidate(item, sim, self._memory_context(), gate)
            item["selection_score"] = score.score
            item["score_breakdown"] = score.breakdown
            self.repo.update_candidate_score(self.run_id, int(candidate.get("candidate_id") or 0), score.score, score.breakdown)
            enriched.append(item)
        return score_candidates(enriched, latest_sim, self._memory_context(), latest_gate)

    def _artifact_paths(self, *, kind: str, stage: str) -> list[Path]:
        paths = []
        for artifact in self.repo.list_artifacts(self.run_id, kind=kind, source_stage=stage):
            path = Path(str(artifact.get("path") or ""))
            if path.exists():
                paths.append(path)
        return paths

    def _latest_artifact_path(self, *, kind: str, stage: str) -> Path | None:
        paths = self._artifact_paths(kind=kind, stage=stage)
        return paths[-1] if paths else None

    def _memory_context(self) -> dict:
        path = memory_path_for_run_dir(self.paths.run_dir)
        if not path.exists():
            return {}
        run = self.repo.get_run(self.run_id)
        if not run:
            return {}
        try:
            config = json.loads(run.get("config_json") or "{}")
        except json.JSONDecodeError:
            return {}
        memory = AlphaMemory(path)
        try:
            return memory.scoring_context(
                dataset=config.get("dataset"),
                region=config.get("region"),
                universe=config.get("universe"),
                delay=config.get("delay"),
                data_type=config.get("data_type"),
                neutralization=config.get("neutralization"),
            )
        finally:
            memory.close()


def load_config_from_run(repo: Repository, run_id: str) -> RunConfig:
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    return RunConfig(**json.loads(run["config_json"]))


def _path_has_rows(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, list) and bool(data)


def _latest_gate_by_candidate(repo: Repository, run_id: str) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for row in repo.list_rows("gate_checks", run_id):
        candidate_id = int(row.get("candidate_id") or 0)
        if candidate_id:
            latest[candidate_id] = row
    return latest
