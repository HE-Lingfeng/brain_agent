from __future__ import annotations

import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import BatchSimAdapter, InspectRawTemplateAdapter, MakeSomeGemAdapter
from ..core.daily_usage import DailySimulationUsage
from ..core.models import CandidateStatus, RunConfig
from ..core.models import RunStage
from .optimization import run_optimization_pass
from .quota_allocator import allocate_simulation_quota
from ..core.repository import Repository
from ..core.runtime import RuntimePaths
from ..core.utils import now_iso, write_json
from ..core.platform_state import PlatformSimulationState, SimulationPolicy


@dataclass
class WorkerStats:
    batches_completed: int = 0
    total_submitted: int = 0
    total_succeeded: int = 0
    total_failed: int = 0
    total_retryable: int = 0
    total_retired: int = 0
    optimization_passes: int = 0
    optimization_variants: int = 0
    start_time: str = ""
    end_time: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batches_completed": self.batches_completed,
            "total_submitted": self.total_submitted,
            "total_succeeded": self.total_succeeded,
            "total_failed": self.total_failed,
            "total_retryable": self.total_retryable,
            "total_retired": self.total_retired,
            "optimization_passes": self.optimization_passes,
            "optimization_variants": self.optimization_variants,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "elapsed_seconds": self.elapsed_seconds(),
            "throughput_per_hour": self.throughput_per_hour(),
            "errors": self.errors[-10:],
        }

    def elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        try:
            start = datetime.fromisoformat(self.start_time)
        except ValueError:
            return 0.0
        end_str = self.end_time or now_iso()
        try:
            end = datetime.fromisoformat(end_str)
        except ValueError:
            return 0.0
        return max(0.0, (end - start).total_seconds())

    def throughput_per_hour(self) -> float:
        elapsed = self.elapsed_seconds()
        if elapsed <= 0:
            return 0.0
        return (self.total_submitted / elapsed) * 3600.0


class SimulationWorker:
    def __init__(
        self,
        repo: Repository,
        run_id: str,
        paths: RuntimePaths,
        config: RunConfig,
    ):
        self._repo = repo
        self._run_id = run_id
        self._paths = paths
        self._config = config
        self._inspect = InspectRawTemplateAdapter(repo, run_id, paths.run_dir)
        self._batch = BatchSimAdapter(repo, run_id, paths.run_dir)
        self._usage = DailySimulationUsage(paths.root)
        self._platform_state = PlatformSimulationState(paths.root)
        self.stats = WorkerStats()
        self._shutdown_requested = False
        self._batch_counter = 0
        self._worker_id = f"worker_{uuid.uuid4().hex[:10]}"
        self._active_policy = SimulationPolicy(
            batch_size=int(config.batch_size),
            concurrency=int(config.concurrency),
            mode="fixed",
            reason="not evaluated yet",
        )
        self._stats_dir = paths.run_dir / "worker_stats"
        self._stats_dir.mkdir(parents=True, exist_ok=True)

    # ── public API ──────────────────────────────────────────────

    def run_drain(
        self,
        idle_sleep: int = 60,
        max_runtime_hours: float | None = None,
        max_batches: int | None = None,
        max_total_alphas: int | None = None,
        max_retries: int = 3,
        max_candidates_per_batch: int = 0,
        refill_on_empty: bool = False,
        max_empty_refills: int | None = None,
        optimize_every_alphas: int = 500,
        optimize_max_parents: int = 20,
        optimize_max_variants: int = 100,
    ) -> WorkerStats:
        self.stats.start_time = now_iso()
        self._install_signal_handlers()
        deadline = time.monotonic() + max_runtime_hours * 3600.0 if max_runtime_hours else None

        parts = [
            f"run={self._run_id}",
            f"batch_size={self._config.batch_size}",
            f"concurrency={self._config.concurrency}",
            f"max_retries={max_retries}",
        ]
        if max_total_alphas:
            parts.append(f"max_total_alphas={max_total_alphas}")
        if max_candidates_per_batch:
            parts.append(f"max_candidates_per_batch={max_candidates_per_batch}")
        if max_runtime_hours:
            parts.append(f"max_runtime_hours={max_runtime_hours}")
        if max_batches:
            parts.append(f"max_batches={max_batches}")
        if refill_on_empty:
            parts.append("refill_on_empty=true")
            if max_empty_refills is not None:
                parts.append(f"max_empty_refills={max_empty_refills}")
        if optimize_every_alphas:
            parts.append(f"optimize_every_alphas={optimize_every_alphas}")
        print(f"[worker] drain started  {'  '.join(parts)}")

        empty_refills = 0
        next_optimization_at = int(optimize_every_alphas) if int(optimize_every_alphas or 0) > 0 else None
        try:
            while not self._shutdown_requested:
                if deadline is not None and time.monotonic() >= deadline:
                    print("[worker] max runtime reached, stopping")
                    break
                if max_batches is not None and self.stats.batches_completed >= max_batches:
                    print(f"[worker] max batches ({max_batches}) reached, stopping")
                    break
                if max_total_alphas is not None and self.stats.total_submitted >= max_total_alphas:
                    print(f"[worker] worker submission limit reached ({max_total_alphas} submitted), stopping")
                    break

                effective_config, policy = self._effective_config()
                effective_capacity = max(1, int(effective_config.batch_size) * int(effective_config.concurrency))
                batch_limit = max_candidates_per_batch
                if batch_limit <= 0:
                    batch_limit = effective_capacity
                else:
                    batch_limit = min(batch_limit, effective_capacity)
                if max_total_alphas is not None:
                    remaining = max(0, int(max_total_alphas) - int(self.stats.total_submitted))
                    if remaining <= 0:
                        print(f"[worker] worker submission limit reached ({max_total_alphas} submitted), stopping")
                        break
                    batch_limit = min(batch_limit, remaining) if batch_limit > 0 else remaining
                candidates = self._find_pending_candidates(
                    max_retries=max_retries,
                    max_results=batch_limit,
                    claim=True,
                    policy=policy.to_dict(),
                )
                if not candidates:
                    if refill_on_empty and (max_empty_refills is None or empty_refills < max_empty_refills):
                        empty_refills += 1
                        refill = self._refill_pending_candidates(empty_refills)
                        if refill.get("candidate_count", 0) > 0:
                            continue
                        error = refill.get("error_summary") or ""
                        if error:
                            self.stats.errors.append(error)
                    if self.stats.batches_completed == 0:
                        print("[worker] queue is empty, waiting for candidates...")
                    self._sleep_or_shutdown(idle_sleep)
                    continue

                self._batch_counter += 1
                batch_info = self._submit_one_batch(candidates, effective_config, policy)
                self._update_stats(batch_info)
                self._write_stats()
                self._print_batch_line(batch_info)
                if next_optimization_at is not None and self.stats.total_submitted >= next_optimization_at:
                    self._run_periodic_optimization(
                        threshold=next_optimization_at,
                        max_parents=optimize_max_parents,
                        max_variants=optimize_max_variants,
                    )
                    while next_optimization_at <= self.stats.total_submitted:
                        next_optimization_at += int(optimize_every_alphas)

        finally:
            self.stats.end_time = now_iso()
            self._write_stats()
            self._print_summary()

        return self.stats

    def run_once(
        self,
        max_retries: int = 3,
        max_candidates_per_batch: int = 0,
        refill_on_empty: bool = False,
    ) -> WorkerStats:
        self.stats.start_time = now_iso()
        self._install_signal_handlers()

        effective_config, policy = self._effective_config()
        effective_capacity = max(1, int(effective_config.batch_size) * int(effective_config.concurrency))
        limit = max_candidates_per_batch if max_candidates_per_batch > 0 else effective_capacity
        limit = min(limit, effective_capacity)
        candidates = self._find_pending_candidates(
            max_retries=max_retries,
            max_results=limit,
            claim=True,
            policy=policy.to_dict(),
        )
        if not candidates and refill_on_empty:
            refill = self._refill_pending_candidates(1)
            if refill.get("error_summary"):
                self.stats.errors.append(str(refill["error_summary"]))
            effective_config, policy = self._effective_config()
            effective_capacity = max(1, int(effective_config.batch_size) * int(effective_config.concurrency))
            limit = max_candidates_per_batch if max_candidates_per_batch > 0 else effective_capacity
            limit = min(limit, effective_capacity)
            candidates = self._find_pending_candidates(
                max_retries=max_retries,
                max_results=limit,
                claim=True,
                policy=policy.to_dict(),
            )
        if not candidates:
            print("[worker] no pending or retryable candidates")
            self.stats.end_time = now_iso()
            return self.stats

        self._batch_counter += 1
        batch_info = self._submit_one_batch(candidates, effective_config, policy)
        self._update_stats(batch_info)
        self._write_stats()
        self._print_batch_line(batch_info)

        self.stats.end_time = now_iso()
        self._write_stats()
        self._print_summary()
        return self.stats

    # ── internals ───────────────────────────────────────────────

    def _refill_pending_candidates(self, refill_number: int) -> dict[str, Any]:
        """Generate and inspect a new candidate batch when the simulation queue is empty."""
        before_ids = {int(row["candidate_id"]) for row in self._repo.list_rows("candidates", self._run_id)}
        before_artifact_ids = {int(row["artifact_id"]) for row in self._repo.list_rows("artifacts", self._run_id)}
        optimized = self._refill_from_existing_repairable(before_ids)
        if optimized.get("candidate_count", 0) > 0:
            return optimized
        print(f"[worker] queue empty; refill #{refill_number} starting generate/inspect")

        self._repo.update_run_stage(self._run_id, RunStage.GENERATE.value)
        generated = MakeSomeGemAdapter(self._repo, self._run_id, self._paths.run_dir).run_real(self._config)
        if generated.status != "ok":
            message = generated.error_summary or "worker refill GENERATE failed"
            print(f"[worker] refill #{refill_number} failed: {message}")
            return {"candidate_count": 0, "error_summary": message}

        alpha_lists: list[Path] = []
        if generated.candidates_delta:
            generated_alpha_list = self._inspect.write_alpha_list_for_candidates(
                generated.candidates_delta,
                self._config,
                name=f"alpha_list_refill{refill_number}_generated.json",
            )
            alpha_lists.append(generated_alpha_list)

        new_idea_files: list[Path] = []
        for artifact in self._repo.list_rows("artifacts", self._run_id):
            if int(artifact.get("artifact_id") or 0) in before_artifact_ids:
                continue
            if artifact.get("kind") != "idea_file":
                continue
            path = Path(str(artifact.get("path") or ""))
            if path.exists():
                new_idea_files.append(path)

        self._repo.update_run_stage(self._run_id, RunStage.INSPECT.value)
        for idea in new_idea_files:
            inspected = self._inspect.run_real(idea, self._config)
            if inspected.status != "ok":
                message = inspected.error_summary or f"worker refill INSPECT failed for {idea}"
                print(f"[worker] refill #{refill_number} failed: {message}")
                return {"candidate_count": 0, "error_summary": message}
            for artifact in inspected.artifacts:
                if artifact.get("kind") == "alpha_list":
                    path = Path(str(artifact.get("path") or ""))
                    if path.exists():
                        alpha_lists.append(path)

        field_factory = self._inspect.write_field_factory_alpha_list(self._config)
        if field_factory.status != "ok":
            message = field_factory.error_summary or "worker refill Field Factory failed"
            print(f"[worker] refill #{refill_number} failed: {message}")
            return {"candidate_count": 0, "error_summary": message}
        for artifact in field_factory.artifacts:
            if artifact.get("kind") == "alpha_list_field_factory":
                path = Path(str(artifact.get("path") or ""))
                if path.exists():
                    alpha_lists.append(path)

        if alpha_lists:
            self._inspect.write_combined_alpha_list(alpha_lists)

        new_pending = [
            row
            for row in self._repo.list_rows("candidates", self._run_id)
            if int(row.get("candidate_id") or 0) not in before_ids
            and row.get("status") in {CandidateStatus.SIM_PENDING.value, CandidateStatus.SIM_RETRYABLE.value}
        ]
        print(f"[worker] refill #{refill_number} added {len(new_pending)} pending candidate(s)")
        return {"candidate_count": len(new_pending)}

    def _refill_from_existing_repairable(self, before_ids: set[int]) -> dict[str, Any]:
        try:
            payload = run_optimization_pass(
                self._repo,
                self._run_id,
                self._paths.run_dir,
                self._config,
                max_parents=10,
                max_variants=max(20, int(self._config.batch_size) * int(self._config.concurrency)),
                exclude_tagged=True,
                source_prefix="refill_optimize",
            )
        except Exception as exc:
            return {"candidate_count": 0, "error_summary": f"refill optimization failed: {type(exc).__name__}: {exc}"}
        if payload.get("status") != "ok":
            return {"candidate_count": 0, "error_summary": str(payload.get("error_summary") or "")}
        new_pending = [
            row
            for row in self._repo.list_rows("candidates", self._run_id)
            if int(row.get("candidate_id") or 0) not in before_ids
            and row.get("status") in {CandidateStatus.SIM_PENDING.value, CandidateStatus.SIM_RETRYABLE.value}
        ]
        if new_pending:
            print(f"[worker] refill optimization added {len(new_pending)} pending candidate(s)")
        return {"candidate_count": len(new_pending)}

    def _run_periodic_optimization(self, *, threshold: int, max_parents: int, max_variants: int) -> None:
        print(f"[worker] optimization checkpoint reached at {threshold} submitted; scanning repairable candidates")
        try:
            payload = run_optimization_pass(
                self._repo,
                self._run_id,
                self._paths.run_dir,
                self._config,
                max_parents=max_parents,
                max_variants=max_variants,
                exclude_tagged=True,
                source_prefix="auto_optimize",
            )
        except Exception as exc:
            message = f"periodic optimization failed: {type(exc).__name__}: {exc}"
            print(f"[worker] {message}")
            self.stats.errors.append(message)
            return

        if payload.get("status") != "ok":
            message = str(payload.get("error_summary") or "periodic optimization returned non-ok status")
            print(f"[worker] periodic optimization failed: {message}")
            self.stats.errors.append(message)
            return

        self.stats.optimization_passes += 1
        variants = int(payload.get("variant_count") or 0)
        self.stats.optimization_variants += variants
        parents = int(payload.get("selected_parent_count") or 0)
        if variants:
            print(f"[worker] periodic optimization added {variants} variant candidate(s) from {parents} parent(s)")
        else:
            print(f"[worker] periodic optimization found no new variants (parents={parents})")

    def _effective_config(self) -> tuple[RunConfig, SimulationPolicy]:
        policy = self._platform_state.recommend_policy(
            batch_size=int(self._config.batch_size),
            concurrency=int(self._config.concurrency),
            min_concurrency=int(self._config.min_concurrency or 1),
            enabled=bool(self._config.adaptive_sim_policy),
        )
        self._active_policy = policy
        if policy.batch_size == self._config.batch_size and policy.concurrency == self._config.concurrency:
            return self._config, policy
        return replace(self._config, batch_size=policy.batch_size, concurrency=policy.concurrency), policy

    def _find_pending_candidates(
        self,
        max_retries: int = 3,
        max_results: int = 0,
        *,
        claim: bool = False,
        policy: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._repo.prune_expired_candidate_reservations()
        rows = self._repo.find_candidates_by_status(
            self._run_id,
            [CandidateStatus.SIM_PENDING.value, CandidateStatus.SIM_RETRYABLE.value],
        )
        if not rows or max_retries <= 0:
            return rows[:max_results] if max_results > 0 else rows

        sim_counts = self._repo.count_retryable_sim_results_by_candidate(self._run_id)
        active: list[dict[str, Any]] = []
        retired = 0
        for r in rows:
            cid = int(r.get("candidate_id") or 0)
            if cid and sim_counts.get(cid, 0) >= max_retries:
                self._repo.update_candidate_status(
                    self._run_id, str(r.get("fingerprint") or ""), CandidateStatus.SIM_FAILED.value
                )
                retired += 1
            else:
                active.append(r)

        if retired:
            print(f"[worker] retired {retired} candidate(s) after {max_retries} retries (marked SIM_FAILED)")

        active.sort(
            key=lambda r: (
                int(r.get("queue_priority") or 0),
                float(r.get("selection_score") or 0),
                int(r.get("candidate_id") or 0),
            ),
            reverse=True,
        )
        if max_results > 0 and len(active) > max_results:
            active = self._allocate_candidate_batch(active, max_results)
            skipped = len(rows) - len(active)
            print(f"[worker] capping batch to {max_results} candidates ({skipped} deferred to next batch)")
        if claim and active:
            claimed = self._repo.claim_candidates(
                self._run_id,
                active,
                owner=self._worker_id,
                limit=max_results if max_results > 0 else len(active),
                ttl_seconds=6 * 60 * 60,
                metadata={"policy": policy or {}},
            )
            if len(claimed) < len(active):
                print(f"[worker] reserved {len(claimed)}/{len(active)} candidate(s); others are claimed by another worker")
            active = claimed
        return active

    def _allocate_candidate_batch(self, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0 or len(candidates) <= limit:
            return candidates
        rows = []
        for row in candidates:
            expression = str(row.get("expression") or "")
            if expression:
                rows.append({"regular": expression, "settings": {}})
        selected_rows, _report = allocate_simulation_quota(rows, candidates, limit=limit)
        selected_exprs = [str(row.get("regular") or "") for row in selected_rows if isinstance(row, dict)]
        by_expr = {str(row.get("expression") or ""): row for row in candidates}
        selected = [by_expr[expr] for expr in selected_exprs if expr in by_expr]
        if len(selected) < limit:
            selected_ids = {int(row.get("candidate_id") or 0) for row in selected}
            for row in candidates:
                if int(row.get("candidate_id") or 0) in selected_ids:
                    continue
                selected.append(row)
                if len(selected) >= limit:
                    break
        return selected[:limit]

    def _submit_one_batch(
        self,
        candidates: list[dict[str, Any]],
        config: RunConfig,
        policy: SimulationPolicy,
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        count = len(candidates)
        batch_n = self._batch_counter
        name = f"alpha_list_worker_batch{batch_n}.json"
        alpha_list = self._inspect.write_alpha_list_for_candidates(candidates, config, name=name)
        candidate_ids = [int(row.get("candidate_id") or 0) for row in candidates if int(row.get("candidate_id") or 0)]
        try:
            result = self._batch.run_real(alpha_list, config)
        finally:
            self._repo.release_candidate_reservations(
                self._run_id,
                candidate_ids=candidate_ids,
                owner=self._worker_id,
            )
        elapsed = time.monotonic() - t0
        submitted_count = _submitted_alpha_count(result, fallback=count)

        succeeded = 0
        failed = 0
        retryable = 0
        for item in result.metrics_delta:
            sim_status = str(item.get("status") or "").upper()
            tags = item.get("failure_tags") or []
            if sim_status in ("COMPLETE", "COMPLETED"):
                succeeded += 1
            elif "sim_retryable" in tags:
                retryable += 1
            else:
                failed += 1

        return {
            "batch_number": batch_n,
            "candidate_count": count,
            "submitted_count": submitted_count,
            "succeeded": succeeded,
            "failed": failed,
            "retryable": retryable,
            "elapsed_seconds": elapsed,
            "status": result.status,
            "error_summary": result.error_summary,
            "policy": policy.to_dict(),
        }

    def _update_stats(self, batch_info: dict[str, Any]) -> None:
        self.stats.batches_completed += 1
        self.stats.total_submitted += int(batch_info.get("submitted_count") or 0)
        self.stats.total_succeeded += int(batch_info.get("succeeded") or 0)
        self.stats.total_failed += int(batch_info.get("failed") or 0)
        self.stats.total_retryable += int(batch_info.get("retryable") or 0)
        err = batch_info.get("error_summary") or ""
        if err:
            self.stats.errors.append(err)
        if int(batch_info.get("submitted_count") or 0) > 0:
            day = self._usage.record_submission(
                run_id=self._run_id,
                batch_number=int(batch_info.get("batch_number") or 0),
                submitted_count=int(batch_info.get("submitted_count") or 0),
                succeeded=int(batch_info.get("succeeded") or 0),
                failed=int(batch_info.get("failed") or 0),
                retryable=int(batch_info.get("retryable") or 0),
                status=str(batch_info.get("status") or ""),
            )
            batch_info["daily_submitted_count"] = int(day.get("submitted_count") or 0)

    def _write_stats(self) -> None:
        try:
            payload = self.stats.to_dict()
            payload["current_policy"] = self._active_policy.to_dict()
            payload["platform"] = self._platform_state.snapshot()
            write_json(self._stats_dir / "worker_stats.json", payload)
        except OSError:
            pass

    def _print_batch_line(self, batch_info: dict[str, Any]) -> None:
        n = batch_info.get("batch_number", 0)
        ok = batch_info.get("succeeded", 0)
        fail = batch_info.get("failed", 0)
        retry = batch_info.get("retryable", 0)
        elapsed = batch_info.get("elapsed_seconds", 0)
        rate = self.stats.throughput_per_hour()
        parts = [f"batch {n}: {ok} ok"]
        if fail:
            parts.append(f"{fail} fail")
        if retry:
            parts.append(f"{retry} requeued")
        parts.append(f"({elapsed:.0f}s)")
        parts.append(f"total: {self.stats.total_submitted} submitted, {rate:.0f}/hr")
        daily = batch_info.get("daily_submitted_count")
        if daily is not None:
            parts.append(f"today(brain_agent): {int(daily)} submitted")
        policy = batch_info.get("policy") if isinstance(batch_info.get("policy"), dict) else {}
        if policy:
            parts.append(
                "policy="
                f"{policy.get('mode')}:{policy.get('concurrency')}x{policy.get('batch_size')}"
            )
        print(f"[worker] {' / '.join(parts)}")

    def _print_summary(self) -> None:
        s = self.stats
        print("=" * 54)
        print("WORKER SUMMARY")
        print("=" * 54)
        print(f"  Run ID:         {self._run_id}")
        print(f"  Batches:        {s.batches_completed}")
        print(f"  Submitted:      {s.total_submitted}")
        print(f"  Succeeded:      {s.total_succeeded}")
        print(f"  Failed:         {s.total_failed}")
        print(f"  Requeued:       {s.total_retryable}")
        print(f"  Retired:        {s.total_retired}")
        if s.optimization_passes or s.optimization_variants:
            print(f"  Opt Passes:     {s.optimization_passes}")
            print(f"  Opt Variants:   {s.optimization_variants}")
        print(f"  Elapsed:        {s.elapsed_seconds():.0f}s")
        print(f"  Throughput:     {s.throughput_per_hour():.0f}/hr")
        if s.errors:
            print(f"  Errors:         {len(s.errors)}")
        print("=" * 54)

    # ── signal handling ─────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        if self._shutdown_requested:
            print("\n[worker] second interrupt, forcing exit")
            sys.exit(1)
        self._shutdown_requested = True
        name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n[worker] {name} received, finishing current batch then exiting...")

    def _sleep_or_shutdown(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._shutdown_requested:
            time.sleep(1)


def _submitted_alpha_count(result: Any, *, fallback: int) -> int:
    for item in getattr(result, "candidates_delta", []) or []:
        if isinstance(item, dict) and "submitted_alpha_count" in item:
            return max(0, int(item.get("submitted_alpha_count") or 0))
    return max(0, int(fallback))
