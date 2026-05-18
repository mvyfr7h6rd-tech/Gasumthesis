"""AI coordination layer for planner-style recommendation guidance.

The coordinator never produces routes directly. It emits a bounded strategy
that nudges the solver toward human-like priorities while preserving hard
solver constraints.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AICoordinatorSiteSnapshot:
    site_id: str
    site_name: str
    site_type: str
    risk_level: str
    hours_to_critical: float
    risk_score: float
    projected_unserved_impact_eur: float
    flaring_loss_eur_per_h: float = 0.0


@dataclass
class AICoordinatorTruckSnapshot:
    truck_id: str
    home_site_id: str
    capacity: int
    availability_days: int = 1
    force_end_enabled: bool = False
    force_end_day: Optional[int] = None


@dataclass
class AICoordinatorInput:
    mode: str
    objective_order: List[str]
    horizon_days: int
    optimize_days_mode: bool
    force_exact_days: bool
    available_trucks: List[AICoordinatorTruckSnapshot]
    sites: List[AICoordinatorSiteSnapshot]
    demand_site_ids: List[str]
    preferred_hubs: List[str] = field(default_factory=list)
    hard_rules: List[str] = field(default_factory=list)


@dataclass
class AICandidateSummary:
    critical_unserved: int
    critical_unserved_impact_eur: float
    future_unserved: int
    future_unserved_impact_eur: float
    active_truck_days: int
    short_active_days: int
    underused_drive_hours: float
    idle_trucks: int
    used_trucks: int
    end_imbalance: int
    total_cost_eur: float


@dataclass
class AICoordinatorStrategy:
    source: str
    min_active_trucks: int
    max_active_trucks: int
    prefer_hubs: List[str] = field(default_factory=list)
    critical_site_ids: List[str] = field(default_factory=list)
    future_site_ids: List[str] = field(default_factory=list)
    risk_penalty_multiplier: float = 1.0
    urgency_multiplier: float = 1.0
    balance_penalty_multiplier: float = 1.0
    rationale: List[str] = field(default_factory=list)

    def to_feedback_dict(self, code: str) -> Dict[str, Any]:
        return {
            "type": "info",
            "code": code,
            "source": self.source,
            "min_active_trucks": self.min_active_trucks,
            "max_active_trucks": self.max_active_trucks,
            "prefer_hubs": self.prefer_hubs,
            "critical_site_ids": self.critical_site_ids,
            "future_site_ids": self.future_site_ids,
            "risk_penalty_multiplier": self.risk_penalty_multiplier,
            "urgency_multiplier": self.urgency_multiplier,
            "balance_penalty_multiplier": self.balance_penalty_multiplier,
            "message": "; ".join(self.rationale) if self.rationale else "AI coordinator strategy applied.",
        }


def planner_candidate_sort_key(summary: AICandidateSummary) -> tuple:
    """Lexicographic planner-style ordering. Lower is better."""
    unresolved_work = summary.critical_unserved > 0 or summary.future_unserved > 0
    return (
        int(summary.critical_unserved),
        round(float(summary.critical_unserved_impact_eur or 0.0), 2),
        int(summary.active_truck_days),
        int(summary.short_active_days),
        round(float(summary.underused_drive_hours or 0.0), 2),
        int(summary.future_unserved),
        round(float(summary.future_unserved_impact_eur or 0.0), 2),
        int(summary.end_imbalance),
        int(summary.idle_trucks if unresolved_work else 0),
        round(float(summary.total_cost_eur or 0.0), 2),
    )


class AIPlannerCoordinator:
    """Planner-style strategy layer with optional DeepSeek backing."""

    def __init__(self, model: str = "deepseek-chat"):
        self.model = model

    def plan_strategy(
        self,
        coordinator_input: AICoordinatorInput,
        api_key: Optional[str] = None,
    ) -> AICoordinatorStrategy:
        heuristic = self._heuristic_plan_strategy(coordinator_input)
        return self._model_strategy(
            coordinator_input=coordinator_input,
            api_key=api_key,
            prompt_kind="initial",
            fallback=heuristic,
        )

    def repair_strategy(
        self,
        coordinator_input: AICoordinatorInput,
        candidate_summary: AICandidateSummary,
        current_strategy: AICoordinatorStrategy,
        api_key: Optional[str] = None,
    ) -> AICoordinatorStrategy:
        heuristic = self._heuristic_repair_strategy(
            coordinator_input=coordinator_input,
            candidate_summary=candidate_summary,
            current_strategy=current_strategy,
        )
        return self._model_strategy(
            coordinator_input=coordinator_input,
            api_key=api_key,
            prompt_kind="repair",
            fallback=heuristic,
            candidate_summary=candidate_summary,
            current_strategy=current_strategy,
        )

    def _heuristic_plan_strategy(self, coordinator_input: AICoordinatorInput) -> AICoordinatorStrategy:
        truck_count = len(coordinator_input.available_trucks)
        critical_sites = sorted(
            [s for s in coordinator_input.sites if s.risk_level == "critical"],
            key=lambda s: (-s.projected_unserved_impact_eur, s.hours_to_critical, -s.risk_score),
        )
        future_sites = sorted(
            [s for s in coordinator_input.sites if s.risk_level != "critical"],
            key=lambda s: (-s.projected_unserved_impact_eur, s.hours_to_critical, -s.risk_score),
        )

        min_active = 1 if truck_count > 0 else 0
        if coordinator_input.force_exact_days:
            min_active = max(1, truck_count)
        elif truck_count > 1 and critical_sites:
            min_active = min(truck_count, max(2, min(len(critical_sites), truck_count)))
        elif truck_count > 1 and coordinator_input.optimize_days_mode and len(future_sites) >= 3:
            min_active = 2

        prefer_hubs = list(dict.fromkeys(coordinator_input.preferred_hubs))
        rationale = []
        if critical_sites and min_active > 1:
            rationale.append(
                f"Use at least {min_active} trucks because critical demand spans multiple sites and should not be compacted into one beautiful route."
            )
        if prefer_hubs:
            rationale.append(
                f"Prefer {', '.join(prefer_hubs)} as circulation hub for full-out empty-back loops when that improves coverage or balance."
            )
        if not rationale:
            rationale.append("Keep one active truck unless horizon risk or critical coverage clearly benefits from more.")

        risk_multiplier = 1.0 + min(3.0, 0.6 * len(critical_sites) + 0.2 * len(future_sites))
        urgency_multiplier = 1.0 + min(0.8, 0.2 * len(critical_sites))
        balance_multiplier = 1.2 if prefer_hubs else 1.0

        return AICoordinatorStrategy(
            source="heuristic",
            min_active_trucks=max(1, min_active) if truck_count else 0,
            max_active_trucks=max(1, truck_count) if truck_count else 0,
            prefer_hubs=prefer_hubs,
            critical_site_ids=[s.site_id for s in critical_sites],
            future_site_ids=[s.site_id for s in future_sites[:8]],
            risk_penalty_multiplier=max(1.0, round(risk_multiplier, 2)),
            urgency_multiplier=max(1.0, round(urgency_multiplier, 2)),
            balance_penalty_multiplier=max(1.0, round(balance_multiplier, 2)),
            rationale=rationale,
        )

    def _heuristic_repair_strategy(
        self,
        coordinator_input: AICoordinatorInput,
        candidate_summary: AICandidateSummary,
        current_strategy: AICoordinatorStrategy,
    ) -> AICoordinatorStrategy:
        truck_count = len(coordinator_input.available_trucks)
        min_active = current_strategy.min_active_trucks or 1
        risk_multiplier = current_strategy.risk_penalty_multiplier
        urgency_multiplier = current_strategy.urgency_multiplier
        balance_multiplier = current_strategy.balance_penalty_multiplier
        rationale = ["Repair pass triggered after first candidate left planner-style gaps."]

        if candidate_summary.critical_unserved > 0 and candidate_summary.idle_trucks > 0:
            min_active = truck_count
            risk_multiplier = max(risk_multiplier, 4.0)
            urgency_multiplier = max(urgency_multiplier, 1.6)
            rationale.append(
                "Critical demand remained unserved while trucks stayed idle, so the repair pass should activate the full selected fleet."
            )
        elif candidate_summary.critical_unserved > 0:
            severe_underuse = (
                candidate_summary.short_active_days > 0
                or candidate_summary.underused_drive_hours > 2.0
            )
            risk_multiplier = max(risk_multiplier, 6.0 if severe_underuse else 3.0)
            urgency_multiplier = max(urgency_multiplier, 1.8 if severe_underuse else 1.4)
            if severe_underuse:
                rationale.append(
                    "Critical demand stayed unserved while route-days remained short, so the repair pass should spend more hours and accept higher cost."
                )
            else:
                rationale.append("Increase critical-site pressure before accepting cost savings.")

        if candidate_summary.future_unserved > 0:
            risk_multiplier = max(risk_multiplier, 2.5)
            rationale.append("Preserve more tomorrow-posture for sites that become critical later in the horizon.")

        if candidate_summary.end_imbalance > 0:
            balance_multiplier = max(balance_multiplier, 1.5)
            rationale.append("Push harder for end-of-horizon container balance on the repair pass.")

        return AICoordinatorStrategy(
            source="heuristic_repair",
            min_active_trucks=max(1, min(min_active, truck_count)) if truck_count else 0,
            max_active_trucks=max(1, truck_count) if truck_count else 0,
            prefer_hubs=current_strategy.prefer_hubs,
            critical_site_ids=current_strategy.critical_site_ids,
            future_site_ids=current_strategy.future_site_ids,
            risk_penalty_multiplier=max(1.0, round(risk_multiplier, 2)),
            urgency_multiplier=max(1.0, round(urgency_multiplier, 2)),
            balance_penalty_multiplier=max(1.0, round(balance_multiplier, 2)),
            rationale=rationale,
        )

    def _model_strategy(
        self,
        coordinator_input: AICoordinatorInput,
        api_key: Optional[str],
        prompt_kind: str,
        fallback: AICoordinatorStrategy,
        candidate_summary: Optional[AICandidateSummary] = None,
        current_strategy: Optional[AICoordinatorStrategy] = None,
    ) -> AICoordinatorStrategy:
        resolved_key = (api_key or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not resolved_key:
            return fallback

        try:
            prompt = self._build_prompt(
                coordinator_input=coordinator_input,
                prompt_kind=prompt_kind,
                fallback=fallback,
                candidate_summary=candidate_summary,
                current_strategy=current_strategy,
            )
            content = self._call_deepseek(prompt=prompt, api_key=resolved_key)
            parsed = self._parse_strategy_json(content, fallback=fallback, truck_count=len(coordinator_input.available_trucks))
            parsed.source = "deepseek"
            return parsed
        except Exception as exc:
            logger.warning("[AIPlannerCoordinator] Falling back to heuristic strategy: %s", exc)
            return fallback

    def _call_deepseek(self, prompt: str, api_key: str) -> str:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "max_tokens": 900,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a logistics planning coordinator. "
                                "Never return prose outside JSON. "
                                "Never invent impossible routes. "
                                "Only return bounded strategy fields."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        response.raise_for_status()
        payload = response.json()
        return payload.get("choices", [{}])[0].get("message", {}).get("content", "")

    def _build_prompt(
        self,
        coordinator_input: AICoordinatorInput,
        prompt_kind: str,
        fallback: AICoordinatorStrategy,
        candidate_summary: Optional[AICandidateSummary],
        current_strategy: Optional[AICoordinatorStrategy],
    ) -> str:
        payload = {
            "coordinator_input": asdict(coordinator_input),
            "current_strategy": asdict(current_strategy) if current_strategy else None,
            "candidate_summary": asdict(candidate_summary) if candidate_summary else None,
            "fallback_strategy": asdict(fallback),
        }
        return (
            f"Task: produce a {prompt_kind} planner strategy for a constrained truck-routing solver.\n"
            "Priorities in order:\n"
            "1. Cover critical sites.\n"
            "2. Avoid future critical sites during the horizon.\n"
            "3. Reduce end-of-horizon imbalance.\n"
            "4. Cost last.\n"
            "Rules:\n"
            "- Never use zero trucks when trucks are available.\n"
            "- Respect force_exact_days when true.\n"
            "- If trucks are idle while critical sites remain, prefer using more trucks.\n"
            "- Prefer Malmi-like hub circulation when it improves coverage or balance.\n"
            "Return JSON only with this schema:\n"
            "{"
            "\"min_active_trucks\": int, "
            "\"max_active_trucks\": int, "
            "\"prefer_hubs\": [str], "
            "\"critical_site_ids\": [str], "
            "\"future_site_ids\": [str], "
            "\"risk_penalty_multiplier\": float, "
            "\"urgency_multiplier\": float, "
            "\"balance_penalty_multiplier\": float, "
            "\"rationale\": [str]"
            "}\n"
            f"Input JSON:\n{json.dumps(payload, indent=2)}"
        )

    def _parse_strategy_json(
        self,
        content: str,
        fallback: AICoordinatorStrategy,
        truck_count: int,
    ) -> AICoordinatorStrategy:
        raw = content.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI coordinator did not return JSON")
        payload = json.loads(raw[start:end + 1])

        min_active = int(payload.get("min_active_trucks", fallback.min_active_trucks or 1))
        max_active = int(payload.get("max_active_trucks", fallback.max_active_trucks or max(1, truck_count)))
        min_active = max(1, min(min_active, max(1, truck_count))) if truck_count else 0
        max_active = max(min_active, min(max_active, max(1, truck_count))) if truck_count else 0

        return AICoordinatorStrategy(
            source="deepseek",
            min_active_trucks=min_active,
            max_active_trucks=max_active,
            prefer_hubs=[str(x) for x in (payload.get("prefer_hubs") or fallback.prefer_hubs or [])],
            critical_site_ids=[str(x) for x in (payload.get("critical_site_ids") or fallback.critical_site_ids or [])],
            future_site_ids=[str(x) for x in (payload.get("future_site_ids") or fallback.future_site_ids or [])],
            risk_penalty_multiplier=max(1.0, float(payload.get("risk_penalty_multiplier", fallback.risk_penalty_multiplier))),
            urgency_multiplier=max(1.0, float(payload.get("urgency_multiplier", fallback.urgency_multiplier))),
            balance_penalty_multiplier=max(1.0, float(payload.get("balance_penalty_multiplier", fallback.balance_penalty_multiplier))),
            rationale=[str(x) for x in (payload.get("rationale") or fallback.rationale or [])],
        )
