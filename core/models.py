from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CandidateStatus(StrEnum):
    GENERATED = "generated"
    SIM_PENDING = "sim_pending"
    SIM_FAILED = "sim_failed"
    SIM_RETRYABLE = "sim_retryable"
    PROMISING = "promising"
    NEEDS_ENHANCE = "needs_enhance"
    SUBMIT_READY = "submit_ready"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"


class RunStage(StrEnum):
    INIT = "INIT"
    GENERATE = "GENERATE"
    INSPECT = "INSPECT"
    SIMULATE = "SIMULATE"
    DECIDE = "DECIDE"
    VARIANT_SEARCH = "VARIANT_SEARCH"
    ENHANCE = "ENHANCE"
    SUBMIT_GATE = "SUBMIT_GATE"
    REPORT = "REPORT"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RunConfig:
    dataset: str
    region: str
    delay: int
    universe: str
    data_type: str
    decay: int = 10
    truncation: float = 0.08
    neutralization: str = "INDUSTRY"
    max_trade: bool = False
    target_ready: int = 4
    max_iterations: int = 6
    batch_size: int = 10
    concurrency: int = 8
    dry_run: bool = False
    max_fields: int | None = None
    max_operators: int | None = None
    max_sim_alphas: int | None = None
    max_variant_alphas: int = 20
    max_variants_per_alpha: int = 4
    use_llm_decide: bool = False
    max_enhance_actions: int = 4
    make_prompt_version: str = "make-v1"
    enhance_prompt_version: str = "enhance-v1"
    decision_prompt_version: str = "decision-v1"
    prompt_experiment: str = ""
    adaptive_sim_policy: bool = False
    min_concurrency: int = 1
    max_platform_slots: int = 80

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "region": self.region,
            "delay": self.delay,
            "universe": self.universe,
            "data_type": self.data_type,
            "decay": self.decay,
            "truncation": self.truncation,
            "neutralization": self.neutralization,
            "max_trade": self.max_trade,
            "target_ready": self.target_ready,
            "max_iterations": self.max_iterations,
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "dry_run": self.dry_run,
            "max_fields": self.max_fields,
            "max_operators": self.max_operators,
            "max_sim_alphas": self.max_sim_alphas,
            "max_variant_alphas": self.max_variant_alphas,
            "max_variants_per_alpha": self.max_variants_per_alpha,
            "use_llm_decide": self.use_llm_decide,
            "max_enhance_actions": self.max_enhance_actions,
            "make_prompt_version": self.make_prompt_version,
            "enhance_prompt_version": self.enhance_prompt_version,
            "decision_prompt_version": self.decision_prompt_version,
            "prompt_experiment": self.prompt_experiment,
            "adaptive_sim_policy": self.adaptive_sim_policy,
            "min_concurrency": self.min_concurrency,
            "max_platform_slots": self.max_platform_slots,
        }


@dataclass
class AdapterResult:
    status: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    candidates_delta: list[dict[str, Any]] = field(default_factory=list)
    metrics_delta: list[dict[str, Any]] = field(default_factory=list)
    error_summary: str = ""
