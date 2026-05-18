"""Recommendation service for generating route recommendations."""

import copy
import json
import logging
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
from enum import Enum
from pathlib import Path

if TYPE_CHECKING:
    from .routing_service import RoutingService

logger = logging.getLogger(__name__)

# Soft target for total flaring exposure in a generated plan.
# This is used for warnings and penalty scaling (not as a hard infeasibility cut).
FLARING_SOFT_LIMIT_HOURS = 10.0

from ..models import (
    Site,
    Container,
    Truck,
    OperationalConfig,
    Route,
    RouteStop,
    SwapOperation,
    Recommendation,
    RecommendationStatus,
    ContainerMove,
)
from ..models.site import SiteType
from ..utils.conversions import kg_to_mwh, kg_to_pressure, pressure_to_kg, effective_pressure_bar, get_normalized_kg, BASE_PRESSURE_BAR
from .vrp_solver import VRPSolver, InfeasibleRoutingError
from .risk_calculator import RiskCalculator, RiskLevel
from .scenario_evaluator import ScenarioEvaluator, ScenarioCost
from .physics import validate_move, validate_state
from .ai_coordinator import (
    AICandidateSummary,
    AICoordinatorInput,
    AICoordinatorSiteSnapshot,
    AICoordinatorStrategy,
    AICoordinatorTruckSnapshot,
    AIPlannerCoordinator,
    planner_candidate_sort_key,
)

# Path to recommendations history file
RECOMMENDATIONS_FILE = Path(__file__).parent.parent / "data" / "recommendations.json"


class ObjectiveFunction(str, Enum):
    """Optimization objective."""
    TIME = "time"
    FLARING = "flaring"
    BALANCED = "balanced"


class RecommendationService:
    """
    Service for generating route recommendations.

    Combines VRP solver and risk calculator to produce
    actionable recommendations for the operator.
    """

    def __init__(
        self,
        sites: Dict[str, Site],
        containers: Dict[str, Container],
        trucks: Dict[str, Truck],
        distance_matrix: Dict[str, Dict[str, float]],
        config: OperationalConfig,
        routing_service: Optional["RoutingService"] = None,
    ):
        self.sites = sites
        self.containers = containers
        self.trucks = trucks
        self.distance_matrix = distance_matrix
        self.config = config
        self.routing_service = routing_service

        # If a routing service is available, build road-based matrices
        self.time_matrix_minutes: Optional[Dict[str, Dict[str, float]]] = None
        self._use_road_routing = False
        if self.routing_service:
            try:
                road_dist, road_time = self.routing_service.build_site_matrices(sites)
                self.distance_matrix = road_dist
                self.time_matrix_minutes = road_time
                self._use_road_routing = True
                logger.info("Using road-network distances (%d sites)", len(sites))
            except Exception as e:
                logger.warning("Road routing unavailable, using static matrix: %s", e)

        self.vrp_solver = VRPSolver(
            sites, self.distance_matrix, config,
            time_matrix_minutes=self.time_matrix_minutes,
            allow_symmetric_fallback=not self._use_road_routing,
        )
        self.risk_calculator = RiskCalculator(config)
        self.ai_coordinator = AIPlannerCoordinator()

        # Virtual sites: custom map points added by the operator.
        # Keyed by custom-point ID; included as non-demand nodes in the VRP solver
        # so trucks can start/end there with correct road distances.
        self._virtual_sites: Dict[str, Site] = {}

        self._last_trace: Optional[dict] = None

        # Store recommendation history (load from file if exists)
        self._history: List[Recommendation] = []
        self._load_history()

    def _apply_rate_overrides(self, rate_overrides: dict) -> None:
        """
        Apply rate overrides to sites for risk calculations.
        Keys may be site_id or site_name (case-insensitive); unknown keys are silently skipped.
        """
        flaring_costs = rate_overrides.get('flaring_costs') or {}
        consumption_rates = rate_overrides.get('consumption_rates') or {}

        # Build name->id map for fuzzy matching (name or id both accepted)
        name_to_id = {s.name.lower(): sid for sid, s in self.sites.items()}

        def _resolve(key: str) -> Optional[str]:
            """Return site_id for a given key (id or name), or None."""
            if key in self.sites:
                return key
            return name_to_id.get(key.lower())

        for key, flaring_cost in flaring_costs.items():
            site_id = _resolve(key)
            if site_id:
                self.sites[site_id].flaring_cost_eur_mwh = flaring_cost

        for key, consumption_rate in consumption_rates.items():
            site_id = _resolve(key)
            if site_id:
                self.sites[site_id].consumption_rate_kg_hour = consumption_rate

    def generate_recommendation(
        self,
        objective: ObjectiveFunction = ObjectiveFunction.BALANCED,
        truck_ids: List[str] = None,
        site_ids: List[str] = None,
        max_search_seconds: int = 30,
        traffic_mode: str = 'normal',
        avg_speed_kmh: float = None,
        horizon_days: int = 1,
        fleet_config: List[dict] = None,
        rate_overrides: dict = None,
        debug_trace: bool = False,
        custom_points: List[dict] = None,
        persist_history: bool = True,
        fill_remaining_time: bool = False,
        allow_transfers: bool = False,
        cost_per_km_override: float = None,
        max_driver_hours_override: float = None,
        swap_time_min_override: float = None,
        max_containers_override: int = None,
        optimize_days_mode: bool = False,
        auto_restrict_horizon: bool = True,
        _risk_penalty_multiplier: float = 1.0,  # internal: boosted on risk-reopt pass
        force_exact_days: bool = False,
        ai_api_key: Optional[str] = None,
        _ai_strategy_override: Optional[AICoordinatorStrategy] = None,
        _ai_coordination_depth: int = 0,
    ) -> Recommendation:
        """
        Generate a route recommendation.

        Args:
            objective: Optimization objective (cost, risk, balanced)
            truck_ids: Optional specific trucks to use (default: all available). Deprecated.
            site_ids: Optional specific sites to serve (default: auto from risk)
            max_search_seconds: Max time for VRP solver
            traffic_mode: 'normal' or 'heavy' - affects travel time calculations
            horizon_days: Planning horizon in days (1-4)
            fleet_config: Fleet configuration with custom start locations
            rate_overrides: Optional dict with 'flaring_costs' and 'consumption_rates' overrides
            debug_trace: If True, build structured calculation trace
            custom_points: Custom points with coordinates [{id, label, latitude, longitude}]
            persist_history: If False, skip appending to _history and writing to disk.
                             Set to False during sensitivity sweeps to prevent history pollution.

        Returns:
            Recommendation object with routes and cost breakdown
        """
        try:
            return self._generate_recommendation_impl(
                objective=objective,
                truck_ids=truck_ids,
                site_ids=site_ids,
                max_search_seconds=max_search_seconds,
                traffic_mode=traffic_mode,
                avg_speed_kmh=avg_speed_kmh,
                horizon_days=horizon_days,
                fleet_config=fleet_config,
                rate_overrides=rate_overrides,
                debug_trace=debug_trace,
                custom_points=custom_points,
                persist_history=persist_history,
                fill_remaining_time=fill_remaining_time,
                allow_transfers=allow_transfers,
                cost_per_km_override=cost_per_km_override,
                max_driver_hours_override=max_driver_hours_override,
                swap_time_min_override=swap_time_min_override,
                max_containers_override=max_containers_override,
                optimize_days_mode=optimize_days_mode,
                auto_restrict_horizon=auto_restrict_horizon,
                _risk_penalty_multiplier=_risk_penalty_multiplier,
                force_exact_days=force_exact_days,
                ai_api_key=ai_api_key,
                _ai_strategy_override=_ai_strategy_override,
                _ai_coordination_depth=_ai_coordination_depth,
            )
        except Exception as _top_exc:
            import traceback as _tb_mod
            print("\n=== BACKEND ERROR ===")
            print(str(_top_exc))
            _tb_mod.print_exc()
            print("====================\n")
            raise

    def _generate_recommendation_impl(
        self,
        objective: ObjectiveFunction = ObjectiveFunction.BALANCED,
        truck_ids: List[str] = None,
        site_ids: List[str] = None,
        max_search_seconds: int = 30,
        traffic_mode: str = 'normal',
        avg_speed_kmh: float = None,
        horizon_days: int = 1,
        fleet_config: List[dict] = None,
        rate_overrides: dict = None,
        debug_trace: bool = False,
        custom_points: List[dict] = None,
        persist_history: bool = True,
        fill_remaining_time: bool = False,
        allow_transfers: bool = False,
        cost_per_km_override: float = None,
        max_driver_hours_override: float = None,
        swap_time_min_override: float = None,
        max_containers_override: int = None,
        optimize_days_mode: bool = False,
        auto_restrict_horizon: bool = True,
        _risk_penalty_multiplier: float = 1.0,
        force_exact_days: bool = False,
        ai_api_key: Optional[str] = None,
        _ai_strategy_override: Optional[AICoordinatorStrategy] = None,
        _ai_coordination_depth: int = 0,
    ) -> "Recommendation":

        # ── Horizon guard ─────────────────────────────────────────────────────────
        # Clamp horizon_days to a valid range rather than asserting so that the
        # call never hard-crashes on bad input.  assert is disabled by -O and is
        # wrong for API-layer validation; a clamp + warning is recoverable.
        if horizon_days < 1:
            logger.warning(
                "horizon_days=%d is invalid (<1); clamping to 1.", horizon_days
            )
            horizon_days = 1

        # ── Stateless guard ───────────────────────────────────────────────────────
        # Each call operates on deep-copied inputs.  No mutation leaks to the
        # canonical service state between runs (sites, distance_matrix, vrp_solver,
        # _virtual_sites are all restored before this method returns).
        _orig_sites = self.sites
        _orig_dm = self.distance_matrix
        _orig_vrp = self.vrp_solver
        _orig_virtual = self._virtual_sites
        _orig_cost_per_km = self.config.cost_per_km_eur
        _orig_max_driver_hours = self.config.max_driver_hours
        _orig_swap_time_hours = self.config.swap_time_hours
        self.sites = copy.deepcopy(_orig_sites)
        self.distance_matrix = copy.deepcopy(_orig_dm)
        self._virtual_sites = {}
        # Apply constraint overrides to config so the solver uses them
        if cost_per_km_override is not None:
            self.config.cost_per_km_eur = cost_per_km_override
        if max_driver_hours_override is not None:
            self.config.max_driver_hours = max_driver_hours_override
        if swap_time_min_override is not None:
            self.config.swap_time_hours = swap_time_min_override / 60.0
        # ─────────────────────────────────────────────────────────────────────────

        trace = {} if debug_trace else None

        # Apply rate overrides if provided
        if rate_overrides:
            self._apply_rate_overrides(rate_overrides)

        # ── Optimize-days preprocessing: availability_days must not restrict the solve ──
        # When called from the optimal_days loop (optimize_days_mode=True) the caller
        # already overrides availability_days = trial_days, but guard here too so any
        # direct call with optimize_days_mode=True is also safe.
        if optimize_days_mode and fleet_config:
            _overridden = [
                fc for fc in fleet_config if fc.get("availability_days", horizon_days) != horizon_days
            ]
            if _overridden:
                logger.info(
                    "[VALIDATION] optimize mode → availability overridden to horizon_days=%d "
                    "for %d truck(s): %s",
                    horizon_days,
                    len(_overridden),
                    [fc.get("truck_id") for fc in _overridden],
                )
            fleet_config = [
                {**fc, "availability_days": horizon_days} for fc in fleet_config
            ]

        # Select and configure trucks
        if fleet_config:
            selected_trucks = self._configure_fleet(fleet_config, horizon_days=horizon_days)
        elif truck_ids:
            selected_trucks = [self.trucks[tid] for tid in truck_ids if tid in self.trucks]
        else:
            selected_trucks = list(self.trucks.values())

        if not selected_trucks:
            self._last_trace = trace
            self.sites, self.distance_matrix, self.vrp_solver, self._virtual_sites = _orig_sites, _orig_dm, _orig_vrp, _orig_virtual
            self.config.cost_per_km_eur = _orig_cost_per_km
            self.config.max_driver_hours = _orig_max_driver_hours
            self.config.swap_time_hours = _orig_swap_time_hours
            return self._create_empty_recommendation("No trucks available")

        # ── Horizon / availability consistency check ───────────────────────────
        # availability_days is a fleet_config dict key, not a Truck attribute.
        # Only meaningful when fleet_config is provided; without it every truck
        # implicitly covers the full horizon_days.
        if fleet_config:
            _max_truck_days = max(
                (fc.get("availability_days", 1) for fc in fleet_config),
                default=1,
            )
            if horizon_days > _max_truck_days:
                _msg = (
                    f"Planning horizon ({horizon_days}d) exceeds the maximum "
                    f"truck availability ({_max_truck_days}d). Days beyond "
                    f"availability will have no trucks and produce no routes."
                )
                logger.warning("[HorizonCheck] %s", _msg)
                print(f"[HorizonCheck] WARNING: {_msg}")
                # Stored in solution_feedback after recommendation is built (see below).
                _horizon_warning = _msg
            else:
                _horizon_warning = None
        else:
            _horizon_warning = None

        # Apply max_containers_override as an absolute per-truck capacity override.
        # This is an operator scenario control (not just a cap): when set to 3,
        # trucks must be allowed to carry up to 3 containers even if source data
        # has stale lower capacities.
        if max_containers_override is not None:
            _override_capacity = max(1, min(5, int(max_containers_override)))
            for truck in selected_trucks:
                truck.capacity = _override_capacity
                if truck.initial_load > truck.capacity:
                    truck.initial_load = truck.capacity

        # Include custom points with coordinates in routing matrix
        if custom_points:
            self._include_custom_points(custom_points)

        # Precompute extra-point distances if routing is available
        # (handles trucks with coordinate-based custom starts)
        if self.routing_service and fleet_config:
            self._precompute_extra_points(fleet_config)

        # Determine sites to serve based on objective
        print(f"[STEP] entering _determine_demand_sites  objective={objective.value} horizon={horizon_days}d")
        demand_site_meta: Dict[str, Dict[str, Any]] = {}
        if site_ids:
            demand_sites = site_ids
        else:
            # Preventive lookahead should help "tomorrow posture" without
            # flooding a 1-day solve with the whole network. Keep at least 48h
            # of visibility, and expand further only when the planning horizon
            # itself is longer.
            _demand_horizon_h = max(horizon_days * 24.0, 48.0)
            demand_sites, demand_site_meta = self._determine_demand_sites(
                objective,
                _demand_horizon_h,
                current_assessments=self.risk_calculator.assess_all_sites(self.sites),
            )
        print(f"[STEP] exiting  _determine_demand_sites  demand_sites={len(demand_sites)} ids={demand_sites}")

        if not demand_sites:
            self._last_trace = trace
            print(
                f"[DemandSummary] demand_sites=0 -> Skipping VRP,"
                f" returning NO_ACTION_NEEDED (objective={objective.value},"
                f" horizon={horizon_days}d)"
            )
            logger.info(
                "generate_recommendation: no demand sites for objective=%s — "
                "all sites within safe thresholds, returning NO_ACTION_NEEDED",
                objective.value,
            )
            rec = self._create_empty_recommendation(
                "All sites are operating within safe parameters. No service required."
            )
            rec.status = RecommendationStatus.NO_ACTION_NEEDED
            rec.feasibility_level = "NO_ACTION_NEEDED"
            if persist_history:
                self._history.append(rec)
                self._save_history()
            self.sites, self.distance_matrix, self.vrp_solver, self._virtual_sites = _orig_sites, _orig_dm, _orig_vrp, _orig_virtual
            self.config.cost_per_km_eur = _orig_cost_per_km
            self.config.max_driver_hours = _orig_max_driver_hours
            self.config.swap_time_hours = _orig_swap_time_hours
            return rec

        # ── Pre-solve feasibility gate ─────────────────────────────────────────
        # Validate that the routing matrix covers all demand sites AND that truck
        # start positions are present.  Failing here is cheaper than starting a
        # 15-second VRP solve that can never produce a valid route.
        _pre_missing = [
            sid for sid in demand_sites
            if sid not in self.distance_matrix
            and not any(sid in row for row in self.distance_matrix.values())
        ]
        if _pre_missing:
            _names = [
                (self.sites[s].name if s in self.sites else s)
                for s in _pre_missing[:5]
            ]
            _msg = (
                f"No feasible plan: distance matrix missing for "
                f"{len(_pre_missing)} demand site(s): {', '.join(_names)}"
                + (f" (+{len(_pre_missing)-5} more)" if len(_pre_missing) > 5 else "") + ". "
                "Cannot start solver with incomplete data."
            )
            logger.error("[PreSolve] MISSING_DISTANCES — %s", _msg)
            self.sites, self.distance_matrix, self.vrp_solver, self._virtual_sites = (
                _orig_sites, _orig_dm, _orig_vrp, _orig_virtual)
            self.config.cost_per_km_eur = _orig_cost_per_km
            self.config.max_driver_hours = _orig_max_driver_hours
            self.config.swap_time_hours = _orig_swap_time_hours
            rec = self._create_infeasible_recommendation(
                reason_code="MISSING_DISTANCES",
                reason_message=_msg,
                objective=objective,
                horizon_days=horizon_days,
            )
            rec.feasibility_level = "INFEASIBLE"
            if persist_history:
                self._history.append(rec)
                self._save_history()
            return rec

        if trace is not None:
            trace["request_snapshot"] = {
                "objective": objective.value,
                "traffic_mode": traffic_mode,
                "horizon_days": horizon_days,
                "max_search_seconds": max_search_seconds,
            }
            trace["trucks_selected"] = [
                {"id": t.id, "capacity": t.capacity, "home_site_id": t.home_site_id}
                for t in selected_trucks
            ]
            assessments = self.risk_calculator.assess_all_sites(self.sites)
            trace["risk_assessments"] = [
                {
                    "site_id": a.site_id,
                    "site_type": a.site_type,
                    "risk_level": a.risk_level.value,
                    "hours_to_critical": round(a.hours_to_critical, 2),
                    "usable_kg": round(a.usable_kg, 1),
                    "consumption_rate_kg_h": round(a.consumption_rate_kg_hour, 2),
                    "production_rate_kg_h": round(a.production_rate_kg_hour, 2),
                    "risk_score": round(a.risk_score, 1),
                }
                for a in assessments
            ]
            trace["demand_sites_selected"] = demand_sites
            trace["demand_site_priority"] = {
                sid: demand_site_meta.get(sid, {})
                for sid in demand_sites
            }

        # Compute effective speed and traffic time multiplier
        effective_speed = avg_speed_kmh if avg_speed_kmh is not None else (60.0 if traffic_mode == 'heavy' else 80.0)
        effective_speed = max(20.0, min(120.0, effective_speed))
        # Multiplier relative to config.avg_speed_kmph (base speed, typically 80)
        traffic_time_multiplier = self.config.avg_speed_kmph / effective_speed

        # Build two layers of urgency:
        # 1. current risk/hours maps for hard operational priority and warnings
        # 2. horizon-aware projected scores for softer preventive bias
        _planning_horizon_h = horizon_days * 24.0

        def _refresh_priority_maps(current_assessments: Optional[List[Any]] = None) -> Dict[str, Any]:
            return self._build_priority_maps(
                planning_horizon_h=_planning_horizon_h,
                current_assessments=current_assessments,
            )

        _priority_maps = _refresh_priority_maps()
        risk_map = dict(_priority_maps["current_level_map"])
        risk_score_map = dict(_priority_maps["solver_priority_score_map"])
        hours_to_critical_map = dict(_priority_maps["current_htc_map"])
        ai_strategy: Optional[AICoordinatorStrategy] = _ai_strategy_override
        ai_strategy_feedback: List[Dict[str, Any]] = []
        planner_base_risk_map = dict(risk_map)
        planner_base_hours_to_critical_map = dict(hours_to_critical_map)

        # Per-objective urgency factor: scales how much risk_score discounts arc costs.
        #   FLARING   → 1.5× (producer flaring prevention is highest priority)
        #   TIME      → 1.0× (standard supply-security urgency)
        #   BALANCED  → 0.75× (moderate; balances cost and urgency)
        if objective == ObjectiveFunction.FLARING:
            effective_urgency_factor_m = int(self.config.urgency_factor_m * 1.5)
        elif objective == ObjectiveFunction.BALANCED:
            effective_urgency_factor_m = int(self.config.urgency_factor_m * 0.75)
        else:  # TIME
            effective_urgency_factor_m = self.config.urgency_factor_m

        _ai_coordination_enabled = bool(
            (optimize_days_mode or force_exact_days or _ai_strategy_override is not None)
            and selected_trucks
        )
        if _ai_coordination_enabled and ai_strategy is None:
            ai_strategy = self.ai_coordinator.plan_strategy(
                self._build_ai_coordinator_input(
                    demand_sites=demand_sites,
                    selected_trucks=selected_trucks,
                    fleet_config=fleet_config,
                    horizon_days=horizon_days,
                    optimize_days_mode=optimize_days_mode,
                    force_exact_days=force_exact_days,
                    risk_map=planner_base_risk_map,
                    hours_to_critical_map=planner_base_hours_to_critical_map,
                ),
                api_key=ai_api_key,
            )
        if ai_strategy is not None:
            risk_score_map = self._apply_ai_priority_bias(risk_score_map, ai_strategy)
            effective_urgency_factor_m = max(
                0,
                int(round(effective_urgency_factor_m * ai_strategy.urgency_multiplier)),
            )
            _risk_penalty_multiplier = max(
                _risk_penalty_multiplier,
                float(ai_strategy.risk_penalty_multiplier or 1.0),
            )
            ai_strategy_feedback.append(ai_strategy.to_feedback_dict("AI_COORDINATOR_PLAN"))

        # ── Scenario evaluation: decide WHEN to act ──────────────────────────
        # Compares ACT_NOW, WAIT_12H, WAIT_24H by projecting site evolution
        # forward and computing total cost (stockout + flaring + routing estimate).
        # Only active for single-day solves (multi-day loop has its own per-day
        # WAIT logic in _select_decision_mode; pre-loop evaluation would conflict).
        _scenario_result: Optional[ScenarioCost] = None
        _scenario_wait_applied: float = 0.0

        if horizon_days == 1:
            _scenario_evaluator = ScenarioEvaluator(self.config)
            print(
                f"[ScenarioEval] evaluating timing scenarios"
                f" for {len(demand_sites)} demand site(s)"
            )
            _scenario_result = _scenario_evaluator.evaluate(
                sites=self.sites,
                trucks=selected_trucks,
                distance_matrix=self.distance_matrix,
                demand_sites=demand_sites,
                hours_to_critical_map=hours_to_critical_map,
            )

            if _scenario_result.name == ScenarioEvaluator.NO_ACTION_NAME:
                # ScenarioEvaluator decided all routing scenarios are infeasible.
                # Return immediately with no routes — no VRP call.
                _no_action_reason = _scenario_result.invalid_reason or "NO_ACTION_ALL_ROUTING_INVALID"
                print(
                    "[ScenarioEval] NO_ACTION selected"
                    " — all routing scenarios were infeasible; returning empty recommendation"
                )
                rec = self._create_empty_recommendation(
                    f"All routing scenarios are infeasible; routing skipped ({_no_action_reason})."
                )
                rec.status = RecommendationStatus.NO_ACTION_NEEDED
                rec.feasibility_level = "NO_ACTION_ROUTING_INFEASIBLE"
                rec.reason_code = _no_action_reason
                rec.reason_message = (
                    "No action selected only because timing scenarios could not produce a routable plan. "
                    "Review matrix reachability, time feasibility, and capacity constraints."
                )
                rec.solution_feedback = [{
                    "type": "error",
                    "code": _no_action_reason,
                    "message": rec.reason_message,
                }, {
                    "type": "info",
                    "code": "ROUTES_ZERO_DIAGNOSTICS",
                    "message": (
                        f"demand_in={len(demand_sites)} active_demand={len(demand_sites)} "
                        "raw_routes=0 routes_after_prune=0"
                    ),
                }]
                rec.routes = []
                self.sites, self.distance_matrix, self.vrp_solver, self._virtual_sites = _orig_sites, _orig_dm, _orig_vrp, _orig_virtual
                self.config.cost_per_km_eur = _orig_cost_per_km
                self.config.max_driver_hours = _orig_max_driver_hours
                self.config.swap_time_hours = _orig_swap_time_hours
                if persist_history:
                    self._history.append(rec)
                    self._save_history()
                return rec

            elif _scenario_result.wait_hours > 0:
                # Apply time evolution to self.sites (in-place, on the already-copied state)
                # and rebuild demand + risk maps so the VRP operates on the evolved state.
                print(
                    f"[ScenarioEval] applying {_scenario_result.wait_hours:.0f}h"
                    f" evolution before routing"
                )
                self._apply_time_evolution(delta_time_hours=_scenario_result.wait_hours)
                _scenario_wait_applied = _scenario_result.wait_hours
                # Rebuild demand and risk maps on evolved state
                _priority_maps = _refresh_priority_maps()
                risk_map = dict(_priority_maps["current_level_map"])
                risk_score_map = dict(_priority_maps["solver_priority_score_map"])
                hours_to_critical_map = dict(_priority_maps["current_htc_map"])
                if ai_strategy is not None:
                    risk_score_map = self._apply_ai_priority_bias(risk_score_map, ai_strategy)
                if not site_ids:
                    demand_sites, demand_site_meta = self._determine_demand_sites(
                        objective,
                        max(horizon_days * 24.0, 96.0),
                        current_assessments=_priority_maps["current_assessments"],
                    )
                    print(
                        f"[ScenarioEval] demand sites after evolution:"
                        f" {len(demand_sites)} (was {len(demand_sites)})"
                    )
        else:
            print(
                f"[ScenarioEval] skipped — multi-day horizon ({horizon_days}d);"
                f" per-day WAIT logic active in _select_decision_mode"
            )

        # ── Fill-remaining-time: expand candidate pool ────────────────────────
        # When enabled, add consumer sites outside the planning horizon as
        # optional VRP nodes (penalty=0 disjunctions).  The solver visits them
        # only when the time budget is not exhausted by primary demand.
        def _compute_fill_sites(current_demand: List[str]) -> List[str]:
            if not fill_remaining_time or site_ids:
                return []
            fill_horizon = max(horizon_days * 24.0 * 3, 96.0)
            all_candidates, _ = self._determine_demand_sites(objective, fill_horizon)
            return [sid for sid in all_candidates if sid not in set(current_demand)]

        # ── Transfer hubs: production sites as optional relay nodes ───────────
        # When enabled, production sites with low utilization (capacity to spare)
        # are added as optional 15-min stops where containers can be exchanged.
        def _compute_transfer_sites(current_demand: List[str]) -> List[str]:
            if not allow_transfers:
                return []
            demand_set = set(current_demand)
            return [
                sid for sid, site in self.sites.items()
                if site.site_type == "production"
                and sid not in demand_set
                and site.utilization_percentage < 40.0
            ]

        fill_sites = _compute_fill_sites(demand_sites)
        transfer_sites_list = _compute_transfer_sites(demand_sites)
        if ai_strategy is not None:
            transfer_sites_list = self._merge_ai_preferred_hubs(transfer_sites_list, ai_strategy)

        print(
            f"[RoutingFlags] fill_remaining_time={fill_remaining_time}"
            f" allow_transfers={allow_transfers}"
            f" primary_demand={len(demand_sites)}"
            f" fill_sites={len(fill_sites)}"
            f" transfer_hubs={len(transfer_sites_list)}"
        )
        if fill_sites:
            print(f"[FillTime] fill_sites={len(fill_sites)}: {fill_sites}")
        if transfer_sites_list:
            print(f"[Transfers] transfer_hubs={len(transfer_sites_list)}: {transfer_sites_list}")

        # ── Multi-day rolling-horizon solve ──────────────────────────────

        all_routes: List[Route] = []
        feasibility_level = "STRICT"
        fallback_warnings: List[str] = []
        days_used = 0
        cumulative_flaring_h = 0.0  # accumulated flaring hours across all solved days
        _last_raw_routes_count = 0
        _last_routes_after_prune = 0
        _last_active_demand_count = 0

        # Simulation state: track per-day state for debug output
        day_states = []
        prev_day_signature = None  # For detecting identical days

        # Belt-and-suspenders: explicit truck start positions for Day 2+.
        # Keyed by truck_id → site_id. Set after each day from last route stop.
        # This guarantees Day N+1 starts from Day N's end regardless of mutation order.
        truck_day_starts: Dict[str, str] = {}
        # Keyed by truck_id → int. Records Day N last_stop.load_full_after so the
        # continuity assert on Day N+1 can verify initial_load == previous end load.
        truck_day_end_full: Dict[str, int] = {}

        # Wait-vs-act: track whether we already skipped a day this horizon.
        # Prevents back-to-back waits that could starve demand indefinitely.
        _waited_this_horizon = False

        # Last active demand actually sent to the solver (filtered set, not full demand_sites).
        # Used for infeasibility diagnosis so messages reflect solver input, not raw demand.
        _last_solver_demand = demand_sites  # fallback; updated each time solver is called

        # PARTIAL_ACT: collect excluded sites across days for post-loop feedback.
        _partial_act_exclusions: List[tuple] = []  # (day, site_id)

        for day in range(1, horizon_days + 1):
            # Day 2+: explicitly re-apply previous day's end location as truck start.
            # This is the authoritative source — _update_truck_states_for_next_day
            # already does this, but we re-apply here as a hard guarantee in case
            # any code path between days resets truck.start.
            if day > 1:
                from ..models import SiteLocation as _SiteLocation
                for truck in selected_trucks:
                    if truck.id in truck_day_starts:
                        new_start = truck_day_starts[truck.id]
                        truck.start = _SiteLocation(site_id=new_start)
                        logger.debug(
                            "Day %d: Truck %s start explicitly enforced → %s",
                            day, truck.id, new_start,
                        )
                    # Continuity assert: initial_load must equal previous day's end load.
                    if truck.id in truck_day_end_full:
                        _expected_il = truck_day_end_full[truck.id]
                        if truck.initial_load != _expected_il:
                            logger.warning(
                                "[CONTINUITY] day=%d truck=%s initial_load=%d != prev_end_full=%d "
                                "— forcing correction",
                                day, truck.id, truck.initial_load, _expected_il,
                            )
                            truck.initial_load = _expected_il

            # [DayStart] — log actual start position for every truck this day
            for truck in selected_trucks:
                start_id = truck.effective_start_site_id or truck.home_site_id
                print(f"[DayStart] day={day} truck={truck.id} start={start_id}")
                logger.info(
                    "[DayStart] day=%d truck=%s start=%s", day, truck.id, start_id
                )

            # Snapshot state before this day's planning
            state_before = self._snapshot_state(selected_trucks)

            # CRITICAL: Recalculate demand EVERY day, even if site_ids was provided
            # (bay inventories change, so demand must be reassessed)
            if day > 1:
                # Always recalculate demand based on current state
                demand_sites, demand_site_meta = self._determine_demand_sites(
                    objective,
                    max(horizon_days * 24.0, 96.0),
                )
                if not demand_sites:
                    # No remaining demand — the previous day(s) satisfied all needs.
                    # This is normal early termination, not a failure.
                    # Return the accumulated plan without appending any warning.
                    logger.info(
                        "Day %d: no more demand sites after day %d operations — "
                        "plan complete, stopping early",
                        day, day - 1,
                    )
                    break
                # Refresh risk maps with current bay inventories
                _priority_maps = _refresh_priority_maps()
                risk_map = dict(_priority_maps["current_level_map"])
                risk_score_map = dict(_priority_maps["solver_priority_score_map"])
                hours_to_critical_map = dict(_priority_maps["current_htc_map"])
                if ai_strategy is not None:
                    risk_score_map = self._apply_ai_priority_bias(risk_score_map, ai_strategy)
                # Refresh optional site pools based on updated state
                fill_sites = _compute_fill_sites(demand_sites)
                transfer_sites_list = _compute_transfer_sites(demand_sites)
                if ai_strategy is not None:
                    transfer_sites_list = self._merge_ai_preferred_hubs(transfer_sites_list, ai_strategy)

            # Rebuild VRP solver with current (possibly mutated) site state.
            # Include virtual sites (custom map points) so trucks can start/end there.
            if day > 1:
                _prev_hint_routes = getattr(self.vrp_solver, "_last_hint_routes", None)
                all_sites = {**self.sites, **self._virtual_sites}
                self.vrp_solver = VRPSolver(
                    all_sites, self.distance_matrix, self.config,
                    time_matrix_minutes=self.time_matrix_minutes,
                    allow_symmetric_fallback=getattr(self.vrp_solver, 'allow_symmetric_fallback', True),
                )
                if _prev_hint_routes:
                    self.vrp_solver._last_hint_routes = copy.deepcopy(_prev_hint_routes)
                logger.debug(
                    "Day %d: VRP solver rebuilt with updated site state (hash: %s)",
                    day, state_before["state_hash"][:12]
                )

            # ── Pre-VRP decision: FULL_ACT / PARTIAL_ACT / WAIT ─────────────────
            print(f"[STEP] entering _select_decision_mode  day={day} demand={len(demand_sites)} trucks={len(selected_trucks)}")
            _decision = self._select_decision_mode(
                day=day,
                demand_sites=demand_sites,
                selected_trucks=selected_trucks,
                risk_map=risk_map,
                hours_to_critical_map=hours_to_critical_map,
                urgency_factor_m=effective_urgency_factor_m,
                traffic_time_multiplier=traffic_time_multiplier,
                max_search_seconds=max_search_seconds,
                horizon_days=horizon_days,
                waited_this_horizon=_waited_this_horizon,
                fleet_config=fleet_config,
            )
            print(f"[STEP] exiting  _select_decision_mode  mode={_decision.get('mode')} active={len(_decision.get('active_demand', []))}")
            _decision_mode = _decision["mode"]
            _active_demand = _decision["active_demand"]
            _last_active_demand_count = len(_active_demand)
            _overflow_demand = _decision.get("overflow_demand", [])
            print(
                f"[Decision] day={day} mode={_decision_mode}"
                f" sites={len(_active_demand)}/{len(demand_sites)}"
                f" J={_decision['J']:.0f}EUR | {_decision['explanation']}"
            )
            logger.info(
                "[Decision] day=%d mode=%s active_demand=%d/%d J=%.0f",
                day, _decision_mode, len(_active_demand), len(demand_sites), _decision["J"],
            )
            if _decision_mode == "WAIT":
                _waited_this_horizon = True
                fallback_warnings.append(f"[WAIT_DAY] Day {day}: {_decision['explanation']}")
                print(f"[TIME] day={day} hours=0 (WAIT — no routing)")
                print(f"[TIME] skipped due to trivial route")
                _priority_maps = _refresh_priority_maps()
                risk_map = dict(_priority_maps["current_level_map"])
                risk_score_map = dict(_priority_maps["solver_priority_score_map"])
                hours_to_critical_map = dict(_priority_maps["current_htc_map"])
                continue
            elif _decision_mode == "PARTIAL_ACT":
                for _excl_sid in _decision.get("excluded_sites", []):
                    _partial_act_exclusions.append((day, _excl_sid))
                fallback_warnings.append(
                    f"[PARTIAL_ACT] Day {day}: {_decision['explanation']}"
                )

            # ── Per-day initial load ──────────────────────────────────────────────
            # Day 1: initial_load from fleet config (explicit UI setting).
            # Day 2+: initial_load was set by _update_truck_states_for_next_day to
            #         last_stop.load_full_after — physical carry-over, no reset.
            #
            # IMPORTANT: Do NOT auto-preload empty trucks from producer starts.
            # If a truck starts empty at a producer and needs full containers,
            # that pickup must appear explicitly as a swap operation at start
            # (with service time), not as a hidden preload.

            for _il_truck in selected_trucks:
                logger.info(
                    "[Continuity] day=%d truck=%s initial_load=%d",
                    day, _il_truck.id, _il_truck.initial_load,
                )

            # Guaranteed hub sites: ALL producers from self.sites regardless of
            # demand status.  The VRP solver's loading hub block will add any
            # producer not already in the model as a penalty=0 disjunction,
            # ensuring trucks always have at least one accessible loading point.
            # This prevents NO_FEASIBLE_ROUTES when demand selection yields
            # a consumer-only active_demand list.
            _truck_start_sites = {
                t.effective_start_site_id or t.home_site_id
                for t in selected_trucks
                if (t.effective_start_site_id or t.home_site_id)
            }
            _guaranteed_hubs = [
                sid for sid, site in self.sites.items()
                if site.is_producer and sid not in _truck_start_sites
            ]

            relief_consumers = self._compute_relief_consumers(
                _active_demand,
                risk_map,
                hours_to_critical_map,
                selected_trucks,
            )
            solver_demand = list(dict.fromkeys(
                sid for sid in (_active_demand + relief_consumers)
                if sid not in _truck_start_sites
                or not (self.sites.get(sid) and self.sites[sid].is_producer)
            ))

            _last_solver_demand = solver_demand  # track actual set sent to solver
            _planner_min_active_trucks = self._planner_min_active_trucks_for_day(
                strategy=ai_strategy,
                active_demand=_active_demand,
                risk_map=risk_map,
                selected_trucks=selected_trucks,
                force_exact_days=force_exact_days,
            )
            print(f"[STEP] entering VRPSolver.solve  day={day} active_demand={len(_active_demand)} solver_demand={len(solver_demand)} trucks={len(selected_trucks)}")
            print(f"[DEBUG] demand_sites: {solver_demand}")
            print(f"[DEBUG] distance_matrix size: {len(self.distance_matrix)} keys")
            print(f"[DEBUG] truck starts: {[t.effective_start_site_id or t.home_site_id for t in selected_trucks]}")
            print(f"[DEBUG] truck initial_loads: {[t.initial_load for t in selected_trucks]}")
            print(f"[DEBUG] truck capacities: {[t.capacity for t in selected_trucks]}")
            if _planner_min_active_trucks:
                print(f"[Planner] day={day} min_active_trucks={_planner_min_active_trucks}")
            assert len(selected_trucks) >= 1, f"[ASSERT] No trucks — selected_trucks is empty"
            assert len(self.distance_matrix) > 0, f"[ASSERT] distance_matrix is empty"
            for _t in selected_trucks:
                _start = _t.effective_start_site_id or _t.home_site_id
                assert _start, f"[ASSERT] Truck {_t.id} has no start site (effective_start_site_id and home_site_id both falsy)"
            try:
                day_routes = self.vrp_solver.solve(
                    trucks=selected_trucks,
                    demand_sites=solver_demand,
                    max_search_seconds=max_search_seconds,
                    traffic_time_multiplier=traffic_time_multiplier,
                    risk_map=risk_map,
                    risk_score_map=risk_score_map,
                    urgency_factor_m=effective_urgency_factor_m,
                    hours_to_critical_map=hours_to_critical_map,
                    current_day=day,
                    fill_sites=fill_sites or None,
                    transfer_sites=transfer_sites_list or None,
                    overflow_sites=_overflow_demand or None,
                    guaranteed_hub_sites=_guaranteed_hubs or None,
                    _is_final_day=(day == horizon_days),
                    cumulative_flaring_hours=cumulative_flaring_h,
                    risk_penalty_multiplier=_risk_penalty_multiplier,
                    optimize_days_mode=optimize_days_mode,
                    force_exact_days=force_exact_days,
                    min_active_vehicles=_planner_min_active_trucks,
                )
            except ValueError as _vrp_cfg_err:
                # Hard model-config error (e.g. no producers in model): surface
                # immediately rather than silently returning empty routes.
                _err_msg = str(_vrp_cfg_err)
                logger.error("[VRPConfigError] day=%d: %s", day, _err_msg)
                fallback_warnings.append(f"[VRP_CONFIG_ERROR] Day {day}: {_err_msg}")
                feasibility_level = "INFEASIBLE"
                break
            except InfeasibleRoutingError as _infeasible_err:
                # OR-Tools found no feasible solution for the current model input.
                # Treat this as a graceful infeasible planning result, not a 500.
                _err_msg = str(_infeasible_err)
                logger.warning("[VRPInfeasible] day=%d: %s", day, _err_msg)
                fallback_warnings.append(f"[VRP_INFEASIBLE] Day {day}: {_err_msg}")
                feasibility_level = "INFEASIBLE"
                day_routes = []
                break

            print(f"[STEP] exiting  VRPSolver.solve  day={day} routes={len(day_routes) if day_routes else 0}")

            # ── [DecisionMismatch] pre-VRP decision vs solver outcome ────────
            if day_routes:
                _solver_served = {
                    stop.site_id for r in day_routes for stop in r.stops
                    if stop.site_id in set(_active_demand) and stop.sequence > 0
                }
                _expected_served = set(_active_demand)
                _mismatch_dropped = _expected_served - _solver_served
                if _mismatch_dropped:
                    _mismatch_names = [
                        self.sites[s].name if s in self.sites else s
                        for s in _mismatch_dropped
                    ]
                    print(
                        f"[DecisionMismatch] day={day} mode={_decision_mode}:"
                        f" decision expected {len(_expected_served)} sites,"
                        f" solver served {len(_solver_served)},"
                        f" dropped {len(_mismatch_dropped)}: {_mismatch_names}"
                    )
                    logger.warning(
                        "[DecisionMismatch] day=%d mode=%s: %d/%d sites dropped by solver: %s",
                        day, _decision_mode, len(_mismatch_dropped), len(_expected_served),
                        _mismatch_names,
                    )

            if not day_routes:
                if day == 1:
                    # First day can't route at all → positioning warning
                    fallback_warnings.append(
                        f"[NO_ROUTES_DAY] Day {day}: VRP solver found no routes"
                    )
                break

            # Assign swap operations: strict first, fallback if needed
            raw_day = copy.deepcopy(day_routes)
            _last_raw_routes_count = len(raw_day)
            for route in raw_day:
                route.day_index = day

            raw_day = self._extend_routes_with_rescue_loops(
                raw_day,
                list(_active_demand),
                relief_consumers,
                hours_to_critical_map,
            )
            raw_day = self._inject_idle_truck_relief_routes(
                raw_day,
                selected_trucks,
                list(_active_demand),
                risk_map,
                hours_to_critical_map,
                day,
            )
            day_routes = copy.deepcopy(raw_day)

            # ── Teleportation fix: capture physical end positions from the raw VRP
            # output BEFORE swap assignment or pruning alters the route list.
            # This guarantees that even on positioning-only days (no service stops),
            # each truck's last visited node is recorded as its Day N+1 start.
            for route in raw_day:
                if route.stops:
                    end_site = route.stops[-1].site_id
                    truck_day_starts[route.truck_id] = end_site
                    print(f"[DayEnd] day={day} truck={route.truck_id} end={end_site}")
                    logger.info(
                        "[DayEnd] day=%d truck=%s end=%s", day, route.truck_id, end_site
                    )

            # Extend demand pool with any fill/transfer sites the solver chose to visit
            effective_demand = list(solver_demand)
            for sid in (fill_sites or []) + (transfer_sites_list or []):
                if sid not in set(effective_demand):
                    effective_demand.append(sid)

            for route in day_routes:
                route.day_index = day
            print(f"[STEP] entering _assign_swap_operations  day={day} routes={len(day_routes)}")
            day_routes = self._assign_swap_operations(day_routes, effective_demand)
            print(f"[STEP] exiting  _assign_swap_operations  day={day} routes={len(day_routes)}")
            # ── Multi-trip extension for underutilized trucks ─────────────────
            # Trucks with < 60% time utilization and remaining > 2h get extra
            # producer→consumer legs appended greedily.
            day_routes = self._extend_routes_multi_trip(
                day_routes, list(demand_sites), day, hours_to_critical_map
            )
            day_routes = self._close_routes_to_empty(
                day_routes, effective_demand, hours_to_critical_map
            )
            print(f"[STEP] entering _prune_noop_stops  day={day} routes={len(day_routes)}")
            day_routes = self._prune_noop_stops(day_routes)
            print(f"[STEP] exiting  _prune_noop_stops  day={day} routes={len(day_routes)}")
            _last_routes_after_prune = len(day_routes)
            _strict_quality_warnings: List[str] = []
            day_routes = self._filter_trivial_routes(day_routes, _strict_quality_warnings)
            for w in _strict_quality_warnings:
                fallback_warnings.append(f"Day {day}: {w}")

            strict_ok = bool(day_routes)
            if strict_ok:
                is_valid, _, _, _ = self._validate_routes_strict(day_routes)
                strict_ok = is_valid

            print(f"[SWAP_METRICS] day={day} strict_routes={len(day_routes) if strict_ok else 0}")

            if not strict_ok:
                day_warnings: List[str] = []
                if raw_day and not day_routes:
                    day_warnings.append(
                        "[SWAP_ASSIGNMENT_ZERO_SERVICE] strict assignment produced 0 actionable routes;"
                        " retrying with relaxed swap rules"
                    )
                day_routes = self._assign_swap_operations_relaxed(
                    copy.deepcopy(raw_day), effective_demand, day_warnings,
                )
                day_routes = self._close_routes_to_empty(
                    day_routes, effective_demand, hours_to_critical_map
                )
                day_routes = self._prune_noop_stops(day_routes)
                _last_routes_after_prune = len(day_routes)
                _relaxed_quality_warnings: List[str] = []
                day_routes = self._filter_trivial_routes(day_routes, _relaxed_quality_warnings)
                day_warnings.extend(_relaxed_quality_warnings)
                for w in day_warnings:
                    fallback_warnings.append(f"Day {day}: {w}")
                print(f"[SWAP_METRICS] day={day} relaxed_routes={len(day_routes)}")
                if day_routes:
                    print(f"[LOW_QUALITY_PLAN] day={day} engine=relaxed")
                    feasibility_level = "FALLBACK"
                    fallback_warnings.append(
                        f"[CONSTRAINT_RELAXED] Day {day}: operational constraints were relaxed"
                        f" to produce this plan — review before approving"
                    )
                    # Determine which relaxation level was needed and log it
                    relaxation_level = self._determine_feasibility_level(day_routes, day_warnings)
                    relaxed_codes = sorted({
                        w.split("]")[0].lstrip("[")
                        for w in day_warnings if w.startswith("[")
                    })
                    _fallback_served = {
                        s.site_id for r in day_routes for s in r.stops
                        if s.site_id in set(demand_sites)
                    }
                    _fallback_infeasible = len(demand_sites) - len(_fallback_served)
                    print(
                        f"[FallbackDebug] day={day} level={relaxation_level}"
                        f" relaxed_constraints={relaxed_codes or ['none_identified']}"
                        f" sites_infeasible={_fallback_infeasible}"
                    )
                    logger.warning(
                        "Day %d: strict swap assignment failed; using fallback level=%s"
                        " (relaxed: %s, infeasible: %d)",
                        day, relaxation_level, relaxed_codes or ["none_identified"],
                        _fallback_infeasible,
                    )
                else:
                    # Both strict and relaxed produced 0 routes.
                    # Last resort: minimal delivery-only swap assignment.
                    _minimal_warnings: List[str] = []
                    _minimal_routes = self._assign_swap_operations_minimal(
                        copy.deepcopy(raw_day), effective_demand, _minimal_warnings,
                    )
                    _minimal_routes = self._close_routes_to_empty(
                        _minimal_routes, effective_demand, hours_to_critical_map
                    )
                    _minimal_routes = self._prune_noop_stops(_minimal_routes)
                    _minimal_qw: List[str] = []
                    _minimal_routes = self._filter_trivial_routes(_minimal_routes, _minimal_qw)
                    for w in _minimal_warnings + _minimal_qw:
                        fallback_warnings.append(f"Day {day}: {w}")
                    print(f"[SWAP_METRICS] day={day} minimal_routes={len(_minimal_routes)}")
                    if _minimal_routes:
                        print(f"[LOW_QUALITY_PLAN] day={day} engine=minimal")
                        day_routes = _minimal_routes
                        feasibility_level = "FALLBACK"
                        fallback_warnings.append(
                            f"[MINIMAL_SWAP_FALLBACK] Day {day}: delivery-only swaps used"
                            f" — review before approving"
                        )
                        print(
                            f"[FallbackDebug] day={day} level=MINIMAL"
                            f" sites_served={len({s.site_id for r in day_routes for s in r.stops if s.site_id in set(demand_sites)})}"
                        )
                    else:
                        day_warnings.append(
                            "[SWAP_ASSIGNMENT_ZERO_SERVICE] relaxed assignment also produced 0 actionable routes"
                        )
                        print(
                            f"[FallbackDebug] day={day} level=L3_DEADHEAD"
                            f" relaxed_constraints=all sites_infeasible={len(demand_sites)}"
                        )

            if not day_routes:
                fallback_warnings.append(
                    f"[NO_ACTIONABLE_SWAPS_DAY] Day {day}: no actionable swaps (positioning day)"
                )
                positioning_routes = self._prune_noop_stops(copy.deepcopy(raw_day))
                if positioning_routes:
                    for route in positioning_routes:
                        route.day_index = day
                        if route.stops:
                            route.start_site_id = route.stops[0].site_id
                            route.end_site_id = route.stops[-1].site_id
                            if day > 1:
                                route.start_label = "In transit from previous day"
                            else:
                                route.start_label = self._resolve_start_label(
                                    route.truck_id,
                                    route.stops[0].site_id,
                                    route.stops[0].site_name,
                                    fleet_config,
                                )
                    all_routes.extend(positioning_routes)
                    days_used = day
                # Positioning day: no service, but trucks may have moved physically.
                # Propagate their raw-VRP end positions into truck state so Day N+1
                # starts from the correct location (not the original depot).
                if day < horizon_days:
                    self._update_truck_states_for_next_day(raw_day, selected_trucks)
                    # Record end load (0 — positioning days carry no containers)
                    for _pos_route in raw_day:
                        if _pos_route.stops:
                            truck_day_end_full[_pos_route.truck_id] = 0
                continue

            day_routes = self._augment_day_routes_with_idle_relief(
                routes=day_routes,
                selected_trucks=selected_trucks,
                demand_sites=effective_demand,
                risk_map=risk_map,
                hours_to_critical_map=hours_to_critical_map,
                day_index=day,
                warnings=fallback_warnings,
            )

            # Tag routes with day info
            for route in day_routes:
                route.day_index = day
                if route.stops:
                    route.start_site_id = route.stops[0].site_id
                    route.end_site_id = route.stops[-1].site_id
                    if day > 1:
                        # Day 2+: truck continues from previous day's end — no teleport.
                        route.start_label = "In transit from previous day"
                    else:
                        route.start_label = self._resolve_start_label(
                            route.truck_id,
                            route.stops[0].site_id,
                            route.stops[0].site_name,
                            fleet_config,
                        )

            all_routes.extend(day_routes)
            days_used = day

            # ── Item 6: multi-day auto-restrict ──────────────────────────────
            if optimize_days_mode and auto_restrict_horizon and day == 1 and horizon_days > 1:
                _fleet_budget_h = len(selected_trucks) * self.config.max_driver_hours
                _used_h = sum(r.total_time_hours for r in day_routes)
                _util_ratio = _used_h / _fleet_budget_h if _fleet_budget_h > 0 else 0.0
                if _util_ratio < 0.50:
                    print(
                        f"[AutoRestrict] Day 1 fleet utilization={_util_ratio:.0%}"
                        f" < 50% (used={_used_h:.1f}h budget={_fleet_budget_h:.1f}h)"
                        f" — restricting horizon from {horizon_days} to 1 day"
                    )
                    horizon_days = 1

            # ── Accumulate cumulative flaring exposure for next-day penalty escalation ──
            # Compute which sites were served this day (had actual container swaps)
            _served_this_day = {
                stop.site_id for route in day_routes for stop in route.stops
                if stop.swap_operation and (
                    stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
                )
            }
            _day_assessments = self.risk_calculator.assess_all_sites(self.sites)
            _day_flaring = self._compute_flaring_exposure(_day_assessments, _served_this_day)
            cumulative_flaring_h += _day_flaring["total_h"]
            if cumulative_flaring_h > 0:
                print(
                    f"[FlaringCumul] after day={day} cumulative={cumulative_flaring_h:.1f}h"
                    f" (this_day={_day_flaring['total_h']:.1f}h"
                    f" over_limit={_day_flaring['over_limit']})"
                )

            # ── Record truck end positions and loads for next day ─────────────────
            for route in day_routes:
                if route.stops:
                    _end_stop = route.stops[-1]
                    truck_day_starts[route.truck_id] = _end_stop.site_id
                    truck_day_end_full[route.truck_id] = _end_stop.load_full_after
                    logger.debug(
                        "Day %d: Truck %s ended at %s full=%d (recorded for Day %d start)",
                        day, route.truck_id, _end_stop.site_id,
                        _end_stop.load_full_after, day + 1,
                    )

            # ── Apply day's operations to state for next day ──
            if day < horizon_days:
                day_runtime_hours = sum(r.total_time_hours for r in day_routes) if day_routes else 0.0
                print(f"[TIME] day={day} hours={day_runtime_hours:.2f}")
                if day_runtime_hours < 1:
                    print(f"[TIME] skipped due to trivial route")
                    self._update_truck_states_for_next_day(day_routes, selected_trucks)
                else:
                    self._apply_day_operations_to_state(day_routes)
                    print(f"[ORDER] swaps -> time evolution executed")
                    self._apply_time_evolution(delta_time_hours=day_runtime_hours)
                    self._update_truck_states_for_next_day(day_routes, selected_trucks)
                    # ── Post-day state validation ──────────────────────────────────
                    for _sid, _site in self.sites.items():
                        _total_kg = sum(pressure_to_kg(b.pressure_bar) for b in _site.bays)
                        print(f"[STATE] {_sid} kg={_total_kg:.0f}")

            # Snapshot state after this day's operations
            state_after = self._snapshot_state(selected_trucks)

            # Compute delta summary for debugging
            delta_summary = self._compute_state_delta(state_before, state_after)

            # Create day signature for duplicate detection
            day_signature = self._compute_day_signature(day_routes)

            day_states.append({
                "day": day,
                "state_before": state_before,
                "state_after": state_after,
                "delta_summary": delta_summary,
                "day_signature": day_signature,
            })

            # GUARDRAIL: Detect if this day is identical to previous day
            if day > 1 and prev_day_signature is not None:
                if (state_before["state_hash"] == day_states[day-2]["state_after"]["state_hash"] and
                    day_signature == prev_day_signature):
                    # State didn't change AND routes are identical → stop early
                    logger.warning(
                        "Day %d: Identical to day %d (state unchanged, routes identical) — stopping early",
                        day, day - 1
                    )
                    fallback_warnings.append(
                        f"[NO_STATE_CHANGE_STOPPED] Day {day}: Identical to day {day-1}, "
                        f"state_hash={state_before['state_hash'][:12]}... — stopped early to avoid duplicates"
                    )
                    break

            prev_day_signature = day_signature

        # ── Multi-day distribution check ─────────────────────────────────────────
        # If user requested >1 day, verify work is not entirely concentrated in day 1.
        if horizon_days > 1 and days_used >= 1 and all_routes:
            _routes_by_day: dict = {}
            for _rd in all_routes:
                _routes_by_day.setdefault(_rd.day_index, []).append(_rd)
            _days_with_routes = [d for d, r in _routes_by_day.items() if r]
            if len(_days_with_routes) == 1 and _days_with_routes[0] == 1:
                fallback_warnings.append(
                    f"[SINGLE_DAY_CONCENTRATION] All work concentrated in day 1"
                    f" despite horizon_days={horizon_days} — consider reducing horizon"
                )
                print(
                    f"[MultiDay] WARNING: all routes on day 1 only"
                    f" (horizon_days={horizon_days} days_used={days_used})"
                )

        # ── Restore canonical state; discard all solver mutations ────────────────
        self.sites = _orig_sites
        self.distance_matrix = _orig_dm
        self.vrp_solver = _orig_vrp
        self._virtual_sites = _orig_virtual
        self.config.cost_per_km_eur = _orig_cost_per_km
        self.config.max_driver_hours = _orig_max_driver_hours
        self.config.swap_time_hours = _orig_swap_time_hours

        # ── Solution validity: reject plans with zero operational service ────────
        # Routes that survived _prune_noop_stops MUST each have ≥1 service stop.
        # If all_routes is non-empty but every stop across all routes has no swap,
        # the plan contains only empty movements — it has no operational value and
        # must be rejected the same way as a fully empty solution.
        if all_routes:
            _total_service_stops = sum(
                1 for r in all_routes for s in r.stops
                if s.swap_operation and (
                    s.swap_operation.containers_dropped or s.swap_operation.containers_picked
                )
            )
            if _total_service_stops == 0:
                _msg = (
                    f"Solution rejected: no operational value — "
                    f"{len(all_routes)} route(s) generated but 0 service stops performed "
                    f"(no containers moved across {len(demand_sites)} demand site(s)). "
                    "All routes are empty movements without container swaps."
                )
                print(f"[SolutionValidity] {_msg}")
                logger.warning("[SolutionValidity] %s", _msg)
                all_routes = []
                fallback_warnings.append(f"[ZERO_SERVICE] {_msg}")

        # ── Post-loop: diagnose why no routes were generated ──────────────────
        if not all_routes:
            self._last_trace = trace
            diagnosis = self._diagnose_infeasibility(
                demand_sites=_last_solver_demand,  # filtered set — what solver actually received
                trucks=selected_trucks,
                horizon_days=horizon_days,
                allow_transfers=allow_transfers,
                fill_remaining_time=fill_remaining_time,
                fleet_config=fleet_config,
                fallback_warnings=fallback_warnings,
                hours_to_critical_map=hours_to_critical_map,
            )
            # Build full explanation: root cause + specifics + suggestions
            full_explanation = diagnosis["explanation"]
            if diagnosis["details"]:
                full_explanation += " Details: " + " ".join(diagnosis["details"])
            if diagnosis["suggestions"]:
                full_explanation += " Suggested actions: " + " ".join(diagnosis["suggestions"])
            logger.warning(
                "[NoRoutes] reason_code=%s demand=%d (filtered from %d) trucks=%d horizon=%d | %s",
                diagnosis["reason_code"], len(_last_solver_demand), len(demand_sites),
                len(selected_trucks), horizon_days, full_explanation,
            )
            rec = self._create_infeasible_recommendation(
                reason_code=diagnosis["reason_code"],
                reason_message=full_explanation,
                objective=objective,
                horizon_days=horizon_days,
            )
            rec.feasibility_level = "INFEASIBLE"
            rec.warnings = (fallback_warnings or []) + diagnosis["details"]
            rec.infeasibility_diagnostics = diagnosis.get("diagnostics_payload")
            rec.solution_feedback = [{
                "type": "error",
                "code": diagnosis["reason_code"],
                "message": full_explanation,
            }, {
                "type": "info",
                "code": "ROUTES_ZERO_DIAGNOSTICS",
                "message": (
                    f"demand_in={len(demand_sites)} active_demand={_last_active_demand_count} "
                    f"raw_routes={_last_raw_routes_count} routes_after_prune={_last_routes_after_prune}"
                ),
            }]
            if persist_history:
                self._history.append(rec)
                self._save_history()
            return rec

        # P0-6 (downgraded): if any day used constraint relaxation, keep the
        # best feasible plan and surface it as a warning instead of rejecting.
        if any("[CONSTRAINT_RELAXED]" in w for w in fallback_warnings):
            logger.warning(
                "[PHYSICS-P0-6] CONSTRAINT_RELAXED in solution — keeping best-effort plan"
            )

        if trace is not None:
            trace["vrp"] = {
                "num_nodes": len(self.sites),
                "num_vehicles": len(selected_trucks),
                "max_time_minutes": int(self.config.max_driver_hours * 60),
                "service_time_minutes": int(self.config.swap_time_hours * 60),
                "traffic_multiplier": traffic_time_multiplier,
                "routes_raw_count": len(all_routes),
                "horizon_days": horizon_days,
                "days_used": days_used,
            }
            trace["validation"] = {
                "passed": True,
                "feasibility_level": feasibility_level,
                "fallback_warnings": fallback_warnings,
            }
            # Include per-day state transitions for debugging rolling horizon
            trace["day_states"] = day_states

        # Build legs for each route
        for route in all_routes:
            route.build_legs_from_stops(effective_speed)

        # Enrich legs with road-following geometry from GraphHopper
        self._enrich_legs_with_geometry(all_routes)

        # Calculate costs and metrics
        recommendation = self._build_recommendation(all_routes, objective, demand_sites, horizon_days, fleet_config=fleet_config, cost_per_km_override=cost_per_km_override)
        recommendation.feasibility_level = feasibility_level
        recommendation.warnings.extend(fallback_warnings)

        # ── Mass balance validation ───────────────────────────────────────────
        # Invariant: containers_picked (from producers) - containers_dropped (at consumers)
        # must equal the net load remaining on all trucks at end of plan.
        _mb_picked = sum(
            len(s.swap_operation.containers_picked)
            for r in all_routes for s in r.stops
            if s.swap_operation
        )
        _mb_dropped = sum(
            len(s.swap_operation.containers_dropped)
            for r in all_routes for s in r.stops
            if s.swap_operation
        )
        _mb_truck_end_load = sum(
            r.stops[-1].truck_load_after
            for r in all_routes if r.stops
        )
        _mb_system_delta = _mb_picked - _mb_dropped
        print(
            f"[MASS_BALANCE] picked={_mb_picked} dropped={_mb_dropped}"
            f" net_on_trucks={_mb_truck_end_load} delta={_mb_system_delta}"
        )
        if _mb_system_delta != _mb_truck_end_load:
            _mb_msg = (
                f"Mass balance mismatch: picked={_mb_picked} dropped={_mb_dropped}"
                f" → net_delta={_mb_system_delta} but trucks carry {_mb_truck_end_load}"
                f" containers at end of plan"
            )
            print(f"[MASS_BALANCE_WARN] {_mb_msg}")
            logger.warning("[MASS_BALANCE_WARN] %s", _mb_msg)
            recommendation.warnings.append(f"[MASS_BALANCE_WARN] {_mb_msg}")

        # ── Force exact days: penalize routes with no real work ───────────────
        if force_exact_days:
            for _fe_route in all_routes:
                _fe_has_real_work = False
                for _fe_stop in _fe_route.stops:
                    if _fe_stop.swap_operation:
                        _picked = len(_fe_stop.swap_operation.containers_picked or [])
                        _dropped = len(_fe_stop.swap_operation.containers_dropped or [])
                        if _picked > 0 or _dropped > 0:
                            _fe_has_real_work = True
                            break
                if not _fe_has_real_work:
                    print(f"[ForceExact] vehicle {_fe_route.truck_id} has NO real work → penalized")
                    recommendation.total_cost_eur += 100000

        # ── High-risk unmitigated check ───────────────────────────────────────
        # After building the recommendation, check that critical/warning demand sites
        # were actually served.  Unserved high-risk sites indicate the solver paid
        # penalties instead of routing — surface this explicitly to the operator.
        _served_site_ids = {
            s.site_id for r in all_routes for s in r.stops
            if s.swap_operation and (
                s.swap_operation.containers_dropped or s.swap_operation.containers_picked
            )
        }
        self._append_unserved_demand_feedback(
            recommendation=recommendation,
            demand_sites=demand_sites,
            served_site_ids=_served_site_ids,
            risk_map=risk_map,
            demand_site_meta=demand_site_meta,
            selected_trucks=selected_trucks,
            all_routes=all_routes,
        )

        # ── PARTIAL_ACT feedback ──────────────────────────────────────────────
        if _partial_act_exclusions:
            _excl_names = [
                f"Day {d}: {self.sites[s].name if s in self.sites else s}"
                for d, s in _partial_act_exclusions
            ]
            _partial_msg = (
                f"Partial service selected on {len({d for d, _ in _partial_act_exclusions})} "
                f"day(s): {len(_partial_act_exclusions)} normal-risk site(s) excluded because "
                f"estimated routing cost exceeded risk value. Excluded: {_excl_names}."
            )
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            recommendation.solution_feedback.append({
                "type": "info",
                "code": "PARTIAL_ACT",
                "message": _partial_msg,
            })

        # ── Horizon / availability mismatch feedback ─────────────────────────
        if _horizon_warning:
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            recommendation.solution_feedback.append({
                "type": "warning",
                "code": "HORIZON_EXCEEDS_AVAILABILITY",
                "horizon_days": horizon_days,
                "max_truck_availability_days": _max_truck_days,
                "message": _horizon_warning,
            })

        # ── Scenario evaluation result feedback ──────────────────────────────
        if _scenario_result is not None:
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            _sc_msg = (
                f"Timing scenario selected: {_scenario_result.name}"
                f" (wait={_scenario_result.wait_hours:.0f}h)"
                f" — total_cost={_scenario_result.total_cost:.0f}EUR"
                f" [stockout={_scenario_result.stockout_cost:.0f}"
                f" + flaring={_scenario_result.flaring_cost:.0f}"
                f" + routing={_scenario_result.routing_cost:.0f}]"
            )
            recommendation.solution_feedback.append({
                "type": "info",
                "code": "SCENARIO_SELECTION",
                "scenario": _scenario_result.name,
                "wait_hours": _scenario_result.wait_hours,
                "stockout_cost_eur": _scenario_result.stockout_cost,
                "flaring_cost_eur":  _scenario_result.flaring_cost,
                "routing_cost_eur":  _scenario_result.routing_cost,
                "total_cost_eur":    _scenario_result.total_cost,
                "message": _sc_msg,
                # All evaluated scenarios for sensitivity display in frontend
                "all_scenarios": [
                    {
                        "name": s.name,
                        "wait_hours": s.wait_hours,
                        "stockout_cost_eur": s.stockout_cost,
                        "flaring_cost_eur": s.flaring_cost,
                        "routing_cost_eur": s.routing_cost,
                        "total_cost_eur": s.total_cost,
                        "valid": s.valid,
                    }
                    for s in _scenario_evaluator.last_all_results
                ],
            })
            if _scenario_wait_applied > 0:
                recommendation.warnings.append(
                    f"[SCENARIO] Routing performed on state evolved"
                    f" +{_scenario_wait_applied:.0f}h forward"
                    f" (scenario={_scenario_result.name})"
                )

        _RISK_HARD_LIMIT = 4.0
        _risk_score = recommendation.solution_risk_score or 0.0
        planner_summary = self._build_ai_candidate_summary(
            recommendation=recommendation,
            demand_sites=demand_sites,
            risk_map=planner_base_risk_map,
            hours_to_critical_map=planner_base_hours_to_critical_map,
            selected_trucks=selected_trucks,
            horizon_days=horizon_days,
        )
        recommendation.solution_feedback.extend(ai_strategy_feedback)
        recommendation.solution_feedback.append(
            self._planner_summary_feedback(planner_summary)
        )

        _allow_full_horizon_repair = (
            _ai_coordination_depth == 0
            and not force_exact_days
        )

        if ai_strategy is not None and _allow_full_horizon_repair:
            _needs_repair = self._should_attempt_ai_repair(
                summary=planner_summary,
                risk_score=_risk_score,
            )
            if _needs_repair:
                try:
                    repair_strategy = self.ai_coordinator.repair_strategy(
                        coordinator_input=self._build_ai_coordinator_input(
                            demand_sites=demand_sites,
                            selected_trucks=selected_trucks,
                            fleet_config=fleet_config,
                            horizon_days=horizon_days,
                            optimize_days_mode=optimize_days_mode,
                            force_exact_days=force_exact_days,
                            risk_map=planner_base_risk_map,
                            hours_to_critical_map=planner_base_hours_to_critical_map,
                        ),
                        candidate_summary=planner_summary,
                        current_strategy=ai_strategy,
                        api_key=ai_api_key,
                    )
                    repair_rec = self.generate_recommendation(
                        objective=objective,
                        truck_ids=truck_ids,
                        site_ids=site_ids,
                        max_search_seconds=max_search_seconds,
                        traffic_mode=traffic_mode,
                        avg_speed_kmh=avg_speed_kmh,
                        horizon_days=horizon_days,
                        fleet_config=fleet_config,
                        rate_overrides=rate_overrides,
                        debug_trace=False,
                        custom_points=custom_points,
                        persist_history=False,
                        fill_remaining_time=fill_remaining_time,
                        allow_transfers=allow_transfers,
                        cost_per_km_override=cost_per_km_override,
                        max_driver_hours_override=max_driver_hours_override,
                        swap_time_min_override=swap_time_min_override,
                        max_containers_override=max_containers_override,
                        optimize_days_mode=optimize_days_mode,
                        _risk_penalty_multiplier=max(
                            _risk_penalty_multiplier,
                            float(repair_strategy.risk_penalty_multiplier or 1.0),
                        ),
                        force_exact_days=force_exact_days,
                        ai_api_key=ai_api_key,
                        _ai_strategy_override=repair_strategy,
                        _ai_coordination_depth=_ai_coordination_depth + 1,
                    )
                    repair_summary = self._build_ai_candidate_summary(
                        recommendation=repair_rec,
                        demand_sites=demand_sites,
                        risk_map=planner_base_risk_map,
                        hours_to_critical_map=planner_base_hours_to_critical_map,
                        selected_trucks=selected_trucks,
                        horizon_days=horizon_days,
                    )
                    if planner_candidate_sort_key(repair_summary) < planner_candidate_sort_key(planner_summary):
                        print(
                            "[AIRepair] adopting repaired candidate"
                            f" critical_unserved {planner_summary.critical_unserved} -> {repair_summary.critical_unserved}"
                            f" future_unserved {planner_summary.future_unserved} -> {repair_summary.future_unserved}"
                            f" imbalance {planner_summary.end_imbalance} -> {repair_summary.end_imbalance}"
                            f" cost {planner_summary.total_cost_eur:.0f} -> {repair_summary.total_cost_eur:.0f}"
                        )
                        repair_rec.solution_feedback = (repair_rec.solution_feedback or []) + [
                            repair_strategy.to_feedback_dict("AI_COORDINATOR_REPAIR"),
                        ]
                        recommendation = repair_rec
                        planner_summary = repair_summary
                        _risk_score = recommendation.solution_risk_score or 0.0
                    else:
                        recommendation.solution_feedback.append({
                            "type": "info",
                            "code": "AI_COORDINATOR_REPAIR_SKIPPED",
                            "message": "Repair pass ran, but the original candidate remained better under planner-style ordering.",
                        })
                except Exception as _repair_err:
                    logger.warning("[AIRepair] repair pass failed: %s", _repair_err)
        elif _allow_full_horizon_repair and _risk_penalty_multiplier == 1.0:
            _RISK_REOPT_THRESHOLD = 3.5
            _critical_first_trigger = bool(
                planner_summary.critical_unserved > 0 and planner_summary.idle_trucks > 0
            )
            if _risk_score > _RISK_REOPT_THRESHOLD or _critical_first_trigger:
                _target_risk_multiplier = 4.0 if _critical_first_trigger else 2.0
                try:
                    reopt_rec = self.generate_recommendation(
                        objective=objective,
                        truck_ids=truck_ids,
                        site_ids=site_ids,
                        max_search_seconds=max_search_seconds,
                        traffic_mode=traffic_mode,
                        avg_speed_kmh=avg_speed_kmh,
                        horizon_days=horizon_days,
                        fleet_config=fleet_config,
                        rate_overrides=rate_overrides,
                        debug_trace=False,
                        custom_points=custom_points,
                        persist_history=False,
                        fill_remaining_time=fill_remaining_time,
                        allow_transfers=allow_transfers,
                        cost_per_km_override=cost_per_km_override,
                        max_driver_hours_override=max_driver_hours_override,
                        swap_time_min_override=swap_time_min_override,
                        max_containers_override=max_containers_override,
                        optimize_days_mode=optimize_days_mode,
                        _risk_penalty_multiplier=_target_risk_multiplier,
                        force_exact_days=force_exact_days,
                        ai_api_key=ai_api_key,
                        _ai_coordination_depth=_ai_coordination_depth + 1,
                    )
                    reopt_score = reopt_rec.solution_risk_score or 0.0
                    if reopt_score < _risk_score:
                        recommendation = reopt_rec
                        _risk_score = reopt_score
                except Exception as _reopt_err:
                    logger.warning("[RiskReOpt] reopt failed: %s — keeping original solution", _reopt_err)

        # ── Risk hard limit warning ───────────────────────────────────────────
        if _risk_score > _RISK_HARD_LIMIT:
            _risk_msg = (
                f"Solution risk score {_risk_score:.1f} exceeds acceptable limit {_RISK_HARD_LIMIT}. "
                "Critical sites remain at risk. Review truck fleet size and availability."
            )
            recommendation.warnings.insert(0, f"[RISK_ABOVE_THRESHOLD] {_risk_msg}")
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            recommendation.solution_feedback.insert(0, {
                "type": "error",
                "code": "RISK_ABOVE_THRESHOLD",
                "message": _risk_msg,
            })
            print(f"[RiskHardLimit] {_risk_msg}")
        recommendation.fill_sites_count = len(fill_sites)
        recommendation.transfer_hubs_count = len(transfer_sites_list)

        # ── Count how many fill / transfer nodes were actually visited ────────
        _all_visited_site_ids: set = {
            stop.site_id
            for route in all_routes
            for stop in route.stops
        }
        _fill_set = set(fill_sites)
        _transfer_set = set(transfer_sites_list)
        recommendation.fill_sites_visited = len(_all_visited_site_ids & _fill_set)
        recommendation.transfer_hubs_visited = len(_all_visited_site_ids & _transfer_set)

        if (fill_remaining_time or allow_transfers) and (
            recommendation.fill_sites_visited == 0 and recommendation.transfer_hubs_visited == 0
            and (len(fill_sites) > 0 or len(transfer_sites_list) > 0)
        ):
            logger.warning(
                "Routing enhancements enabled but not used in solution "
                "(fill_sites=%d, transfer_hubs=%d — all dropped by solver).",
                len(fill_sites), len(transfer_sites_list),
            )
            print(
                f"[RoutingEnhancements] WARNING: enhancements enabled but none visited "
                f"(fill_sites={len(fill_sites)}, transfer_hubs={len(transfer_sites_list)})"
            )

        # ── [ModelWarning] — detect resource-vs-output anomalies ─────────────
        # If more resources (vehicle-days) were available than sites served,
        # the model may have structural issues (penalty misconfiguration, time
        # infeasibility, or over-constrained capacity).
        _total_vehicle_days = sum(
            fc.get("availability_days", 1) for fc in (fleet_config or [])
        ) if fleet_config else (len(selected_trucks) * horizon_days)
        _total_routes_with_demand = sum(
            1 for r in all_routes
            if any(s.site_id in set(demand_sites) for s in r.stops)
        )
        _total_demand_served = len({
            s.site_id
            for r in all_routes
            for s in r.stops
            if s.site_id in set(demand_sites)
        })
        if _total_vehicle_days > 1 and _total_demand_served < len(selected_trucks):
            print(
                f"[ModelWarning] UNDERPERFORMANCE: {_total_vehicle_days} vehicle-days"
                f" available but only {_total_demand_served} unique demand site(s) served"
                f" across {len(all_routes)} route(s). Check penalties, time budgets, and"
                f" site reachability."
            )
            logger.warning(
                "ModelWarning: %d vehicle-days available, %d demand sites served — "
                "possible misconfiguration.",
                _total_vehicle_days, _total_demand_served,
            )

        # ── Unused truck-day feedback ─────────────────────────────────────────
        # If fleet_config was provided, each truck has an explicit availability_days.
        # When the solver needs fewer days than configured, explain why so the
        # operator understands the plan is complete, not an error.
        if fleet_config:
            # Count distinct days each truck was actually routed
            truck_days_used: Dict[str, int] = {}
            for route in all_routes:
                truck_days_used[route.truck_id] = (
                    truck_days_used.get(route.truck_id, 0) + 1
                )
            for fc in fleet_config:
                tid = fc.get("truck_id")
                available = fc.get("availability_days", 1)
                used = truck_days_used.get(tid, 0)
                if used < available:
                    recommendation.solution_feedback.append({
                        "type": "info",
                        "code": "UNUSED_TRUCK_DAYS",
                        "truck_id": tid,
                        "message": (
                            f"{tid} had {available} available day{'s' if available != 1 else ''} "
                            f"but only {used} {'were' if used != 1 else 'was'} needed to satisfy "
                            f"demand under the current objective."
                        ),
                    })

            # If the configured horizon is materially longer than the workload needs,
            # call this out explicitly so forced multi-day plans are not mistaken for
            # compact optimums.
            _fleet_size = max(len(fleet_config), 1)
            _total_route_time_h = sum(r.total_time_hours for r in all_routes)
            _min_days_by_capacity = max(
                1,
                math.ceil(_total_route_time_h / max(_fleet_size * self.config.max_driver_hours, 1e-6)),
            )
            if horizon_days > _min_days_by_capacity:
                recommendation.solution_feedback.append({
                    "type": "warning" if force_exact_days else "info",
                    "code": "COMPRESSIBLE_PLAN",
                    "message": (
                        f"Configured horizon is {horizon_days} day(s), but the current workload fits in about "
                        f"{_min_days_by_capacity} working day(s) for this fleet. "
                        + (
                            "Warning: this could be done in fewer days, but force exact days keeps the work distributed."
                            if force_exact_days
                            else "Extra days are valid under the current constraint, but they are not the most compact plan."
                        )
                    ),
                })

        if trace is not None:
            trace["routes"] = self._trace_routes(all_routes)
            picked = sum(
                len(s.swap_operation.containers_picked) for r in all_routes for s in r.stops
                if s.swap_operation and self.sites.get(s.site_id) and self.sites[s.site_id].is_producer
            )
            returned = sum(
                len(s.swap_operation.containers_dropped) for r in all_routes for s in r.stops
                if s.swap_operation and self.sites.get(s.site_id) and self.sites[s.site_id].is_producer
            )
            trace["global_metrics"] = {
                "picked_from_plants": picked,
                "returned_to_plants": returned,
                "unreturned_containers": recommendation.unreturned_containers,
            }
            trace["objective_breakdown"] = {
                "objective": objective.value,
                "transport_cost_eur": round(recommendation.transport_cost_eur, 2),
                "handling_cost_eur": round(recommendation.handling_cost_eur, 2),
                "contingency_multiplier": self.config.contingency_multiplier,
                "total_cost_eur": round(recommendation.total_cost_eur, 2),
                "total_mwh_moved": round(recommendation.total_mwh_moved, 1),
                "eur_per_mwh": round(recommendation.eur_per_mwh, 2) if recommendation.eur_per_mwh else None,
            }

        self._last_trace = trace

        # Store in history and persist (skipped during sensitivity sweeps).
        if persist_history:
            self._history.append(recommendation)
            self._save_history()

        return recommendation

    def generate_all_objectives(
        self,
        truck_ids: List[str] = None,
        site_ids: List[str] = None,
        max_search_seconds: int = 30,
        traffic_mode: str = 'normal',
        avg_speed_kmh: float = None,
        horizon_days: int = 1,
        fleet_config: List[dict] = None,
        rate_overrides: dict = None,
        debug_trace: bool = False,
        custom_points: List[dict] = None,
        persist_history: bool = True,
    ) -> Dict[str, Recommendation]:
        """
        Generate recommendations for all active objectives in a single call.

        Returns a dict keyed by objective string ('time', 'flaring', 'balanced'),
        each containing a Recommendation.

        If any single objective fails, the entire call fails (no partial results).
        """
        results: Dict[str, Recommendation] = {}

        active_objectives = list(ObjectiveFunction)
        logger.info(
            "generate_all_objectives: computing %d objectives: %s",
            len(active_objectives),
            [o.value for o in active_objectives],
        )

        for objective in active_objectives:
            # Deep-copy mutable state that generate_recommendation may mutate
            # (rate overrides modify self.sites in-place)
            original_sites = {
                sid: site.model_copy(deep=True) for sid, site in self.sites.items()
            }

            try:
                rec = self.generate_recommendation(
                    objective=objective,
                    truck_ids=truck_ids,
                    site_ids=site_ids,
                    max_search_seconds=max_search_seconds,
                    traffic_mode=traffic_mode,
                    avg_speed_kmh=avg_speed_kmh,
                    horizon_days=horizon_days,
                    fleet_config=fleet_config,
                    rate_overrides=rate_overrides,
                    debug_trace=debug_trace,
                    custom_points=custom_points,
                    persist_history=persist_history,
                    optimize_days_mode=False,
                )
                results[objective.value] = rec
            finally:
                # Restore sites to avoid cross-contamination between objectives.
                # IMPORTANT: update values IN-PLACE rather than replacing self.sites
                # with a new dict.  Replacing the dict breaks the shared reference
                # between service.sites and loader._sites, which means that
                # apply_recommendation() would mutate the wrong object and
                # loader.save_to_json() would persist stale (pre-approval) data.
                for sid, site in original_sites.items():
                    self.sites[sid] = site
                # Rebuild VRP solver with restored sites
                self.vrp_solver = VRPSolver(
                    self.sites, self.distance_matrix, self.config,
                    time_matrix_minutes=self.time_matrix_minutes,
                    allow_symmetric_fallback=not self._use_road_routing,
                )
                self.risk_calculator = RiskCalculator(self.config)

        return results

    @staticmethod
    def _resolve_start_label(
        truck_id: str,
        start_site_id: str,
        default_site_name: str,
        fleet_config: Optional[List[dict]],
    ) -> str:
        """Determine human-readable start label for a route.

        Uses the custom-point or in-transit label ONLY when the route's actual
        first stop matches the configured custom start (i.e. Day 1).  On Day 2+
        the truck starts from a real site and the actual site name is returned.
        """
        if not fleet_config:
            return default_site_name

        fc = next((c for c in fleet_config if c.get('truck_id') == truck_id), None)
        if not fc:
            return default_site_name

        start = fc.get('start') or {}
        start_mode = fc.get('start_mode', 'site')

        if start_mode == 'custom':
            configured_id = start.get('custom_id')
            # Only show custom label when this route actually starts at the custom point
            if configured_id and configured_id == start_site_id:
                label = start.get('label') or configured_id or 'Custom Point'
                return f"Custom Point: {label}"
            # Day 2+ — truck started from a real site
            return default_site_name

        if start_mode == 'in_transit':
            # Only label as in-transit on the first day (truck.start was the transit point)
            configured_id = (start.get('to_point') or {}).get('site_id') or \
                            (start.get('to_point') or {}).get('custom_id')
            if configured_id and configured_id == start_site_id:
                return "In Transit"
            return default_site_name

        return default_site_name

    def _configure_fleet(self, fleet_config: List[dict], horizon_days: int = 1) -> List['Truck']:
        """Configure fleet with custom start locations from request.

        Args:
            fleet_config: List of FleetConfigTruck dicts from API

        Returns:
            List of configured Truck objects
        """
        from ..models import SiteLocation, ForceEnd

        configured_trucks = []
        for config in fleet_config:
            truck_id = config.get('truck_id')
            if truck_id not in self.trucks:
                continue

            truck = self.trucks[truck_id].model_copy(deep=True)
            truck.force_end = None

            # Configure start location — fleet config uses nested 'start' object
            start_mode = config.get('start_mode', 'site')
            start_obj = config.get('start') or {}
            if start_mode == 'site':
                site_id = start_obj.get('site_id') or truck.home_site_id
                truck.start = SiteLocation(site_id=site_id)
            elif start_mode == 'custom':
                # Custom map point — custom_id is a virtual site node
                custom_id = start_obj.get('custom_id')
                if custom_id:
                    truck.start = SiteLocation(site_id=custom_id)
            elif start_mode == 'in_transit':
                # Treat destination as the effective start for this day
                to_point = start_obj.get('to_point') or {}
                if to_point.get('kind') == 'site':
                    site_id = to_point.get('site_id')
                    if site_id:
                        truck.start = SiteLocation(site_id=site_id)
                elif to_point.get('kind') == 'custom':
                    custom_id = to_point.get('custom_id')
                    if custom_id:
                        truck.start = SiteLocation(site_id=custom_id)
            elif truck.start is None:
                truck.start = SiteLocation(site_id=truck.home_site_id)

            # Configure force end — fleet config uses nested 'force_end_point' object
            if config.get('force_end_enabled'):
                force_end_day = config.get('force_end_day', None)
                force_end_point = config.get('force_end_point') or {}
                force_end_kind = force_end_point.get('kind')
                if force_end_day is None:
                    force_end_day = min(
                        int(config.get('availability_days', horizon_days) or horizon_days),
                        int(horizon_days or 1),
                    )
                if force_end_day and force_end_kind == 'site':
                    force_site_id = force_end_point.get('site_id')
                    if force_site_id:
                        truck.force_end = ForceEnd(day_index=force_end_day, site_id=force_site_id)
                elif force_end_day and force_end_kind == 'custom':
                    force_custom_id = force_end_point.get('custom_id')
                    if force_custom_id:
                        truck.force_end = ForceEnd(day_index=force_end_day, site_id=force_custom_id)

            # Set initial load count
            truck.initial_load = max(0, min(3, config.get('initial_load', 0)))
            resolved_start = truck.effective_start_site_id or truck.home_site_id
            truck.current_location_site_id = resolved_start
            truck.current_load = [
                f"__config_load_{truck.id}_{idx}"
                for idx in range(truck.initial_load)
            ]

            configured_trucks.append(truck)

        return configured_trucks

    def _include_custom_points(self, custom_points: List[dict]) -> None:
        """Add custom points with coordinates to the routing matrix.

        Tries GraphHopper road routing first; falls back to haversine when
        GraphHopper is unavailable or fails. Also creates virtual Site stubs so
        the VRP solver can start/end at these points with correct distances.
        """
        from backend.utils import haversine_distance_km

        extra_points: Dict[str, tuple] = {}
        self._virtual_sites = {}
        for cp in custom_points:
            lat = cp.get('latitude')
            lon = cp.get('longitude')
            if lat is not None and lon is not None:
                cp_id = cp['id']
                extra_points[cp_id] = (float(lat), float(lon))
                # Create a minimal non-demand virtual site for VRP routing
                self._virtual_sites[cp_id] = Site(
                    id=cp_id,
                    name=cp.get('label') or "Custom Point",
                    site_type=SiteType.PRODUCTION,   # is_producer=True → never a swap target
                    latitude=float(lat),
                    longitude=float(lon),
                    bays_fixed=0,
                    bays=[],
                    consumption_rate_kg_hour=0.0,
                    flaring_cost_eur_mwh=0.0,
                )

        if not extra_points:
            return

        all_sites = {**self.sites, **self._virtual_sites}

        # Try GraphHopper road routing first
        if self.routing_service:
            try:
                road_dist, road_time = self.routing_service.build_site_matrices(
                    self.sites, extra_points=extra_points
                )
                self.distance_matrix = road_dist
                self.time_matrix_minutes = road_time
                self.vrp_solver = VRPSolver(
                    all_sites, self.distance_matrix, self.config,
                    time_matrix_minutes=self.time_matrix_minutes,
                    allow_symmetric_fallback=not self._use_road_routing,
                )
                logger.info(
                    "Rebuilt matrices with %d custom points via GraphHopper",
                    len(extra_points),
                )
                return
            except Exception as e:
                logger.warning(
                    "GraphHopper failed for custom points (%s); falling back to haversine", e
                )

        # Haversine fallback: inject custom-point distances into existing matrix
        for cp_id, (cp_lat, cp_lon) in extra_points.items():
            self.distance_matrix.setdefault(cp_id, {})
            for site_id, site in all_sites.items():
                if site_id == cp_id:
                    self.distance_matrix[cp_id][site_id] = 0.0
                    continue
                dist = haversine_distance_km(cp_lat, cp_lon, site.latitude, site.longitude)
                self.distance_matrix[cp_id][site_id] = dist
                self.distance_matrix.setdefault(site_id, {})[cp_id] = dist

        self.vrp_solver = VRPSolver(
            all_sites, self.distance_matrix, self.config,
            time_matrix_minutes=self.time_matrix_minutes,
            allow_symmetric_fallback=True,  # haversine is symmetric
        )
        logger.info(
            "Rebuilt VRP solver with %d custom points via haversine fallback",
            len(extra_points),
        )

    def _precompute_extra_points(self, fleet_config: List[dict]) -> None:
        """Rebuild distance/time matrices with extra coordinate points from fleet config.

        When trucks start from coordinate-based custom locations, we add those
        coordinates as virtual nodes so the VRP solver can route through them.
        """
        extra_points: Dict[str, tuple] = {}
        for cfg in fleet_config:
            start = cfg.get('start')
            if not start:
                continue
            # Handle the new TruckStart schema format (kind='custom' with coords)
            if start.get('kind') == 'coord' or cfg.get('start_mode') == 'coord':
                lat = start.get('lat') or cfg.get('start_lat')
                lon = start.get('lon') or cfg.get('start_lon')
                if lat is not None and lon is not None:
                    virtual_id = f"__custom_start_{cfg.get('truck_id')}"
                    extra_points[virtual_id] = (float(lat), float(lon))

        if not extra_points:
            return

        try:
            road_dist, road_time = self.routing_service.build_site_matrices(
                self.sites, extra_points=extra_points
            )
            self.distance_matrix = road_dist
            self.time_matrix_minutes = road_time
            self.vrp_solver = VRPSolver(
                self.sites, self.distance_matrix, self.config,
                time_matrix_minutes=self.time_matrix_minutes,
                allow_symmetric_fallback=not self._use_road_routing,
            )
            logger.info(
                "Rebuilt matrices with %d extra points for custom truck starts",
                len(extra_points),
            )
        except Exception as e:
            logger.warning("Failed to precompute extra point distances: %s", e)

    @staticmethod
    def _risk_level_name(level: Any) -> str:
        if hasattr(level, "value"):
            return str(level.value)
        return str(level)

    @classmethod
    def _demand_priority_bucket(cls, current_risk: str, projected_risk: str) -> int:
        """Current operational risk always outranks projected horizon risk."""
        if current_risk == "critical":
            return 0
        if current_risk == "warning":
            return 1
        if projected_risk == "critical":
            return 2
        if projected_risk == "warning":
            return 3
        return 4

    @classmethod
    def _solver_priority_score(
        cls,
        current_risk: str,
        current_score: float,
        projected_risk: str,
        projected_score: float,
    ) -> float:
        """Bounded score with hard separation between current and preventive demand."""
        bucket = cls._demand_priority_bucket(current_risk, projected_risk)
        if bucket == 0:
            return 100.0
        if bucket == 1:
            return max(75.0, min(94.0, float(current_score)))
        if bucket == 2:
            return min(74.0, max(55.0, float(projected_score) * 0.85))
        if bucket == 3:
            return min(59.0, max(35.0, float(projected_score) * 0.75))
        return min(34.0, max(float(current_score), float(projected_score) * 0.50))

    def _build_priority_maps(
        self,
        planning_horizon_h: float,
        current_assessments: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Build current-risk maps plus softer horizon-aware scores for solver guidance."""
        current_assessments = current_assessments or self.risk_calculator.assess_all_sites(self.sites)
        projected_assessments = self.risk_calculator.assess_all_sites(
            self.sites,
            horizon_hours=planning_horizon_h,
        )

        current_lookup = {a.site_id: a for a in current_assessments}
        projected_lookup = {a.site_id: a for a in projected_assessments}

        current_level_map = {
            sid: self._risk_level_name(a.risk_level)
            for sid, a in current_lookup.items()
        }
        current_score_map = {
            sid: float(a.risk_score)
            for sid, a in current_lookup.items()
        }
        current_htc_map = {
            sid: float(a.hours_to_critical)
            for sid, a in current_lookup.items()
        }

        projected_level_map = {
            sid: self._risk_level_name(a.risk_level)
            for sid, a in projected_lookup.items()
        }
        projected_score_map = {
            sid: float(a.risk_score)
            for sid, a in projected_lookup.items()
        }
        projected_htc_map = {
            sid: float(a.hours_to_critical)
            for sid, a in projected_lookup.items()
        }

        solver_priority_score_map = {
            sid: self._solver_priority_score(
                current_risk=current_level_map.get(sid, "safe"),
                current_score=current_score_map.get(sid, 0.0),
                projected_risk=projected_level_map.get(sid, "safe"),
                projected_score=projected_score_map.get(sid, 0.0),
            )
            for sid in self.sites
        }

        return {
            "current_assessments": current_assessments,
            "projected_assessments": projected_assessments,
            "current_level_map": current_level_map,
            "current_score_map": current_score_map,
            "current_htc_map": current_htc_map,
            "projected_level_map": projected_level_map,
            "projected_score_map": projected_score_map,
            "projected_htc_map": projected_htc_map,
            "solver_priority_score_map": solver_priority_score_map,
        }

    def _determine_demand_sites(
        self,
        objective: ObjectiveFunction,
        horizon_hours: float = 48.0,
        current_assessments: Optional[List[Any]] = None,
        planning_assessments: Optional[List[Any]] = None,
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """Determine which sites need service based on objective and planning horizon.

        Inclusion rule: site is included if its projected depletion time is within
        the planning horizon (depletion_h < horizon_hours).  This ensures sites that
        become critical during the horizon window are captured even if they are
        currently classified as NORMAL by the static 24/48-hour thresholds.
        """
        assessments = current_assessments or self.risk_calculator.assess_all_sites(self.sites)
        projected_assessments = planning_assessments or self.risk_calculator.assess_all_sites(
            self.sites,
            horizon_hours=horizon_hours,
        )
        projected_lookup = {a.site_id: a for a in projected_assessments}

        demand: List[str] = []
        demand_meta: Dict[str, Dict[str, Any]] = {}
        n_current_critical = 0
        n_current_warning = 0
        n_preventive = 0

        for a in assessments:
            depletion_h = a.hours_to_critical
            projected = projected_lookup.get(a.site_id, a)
            current_risk = self._risk_level_name(a.risk_level)
            projected_risk = self._risk_level_name(projected.risk_level)

            # FLARING objective: only production sites with a flaring cost matter
            if objective == ObjectiveFunction.FLARING and a.flaring_cost_eur_mwh <= 0:
                logger.debug(
                    "[DemandFilter] Site=%-20s usable=%.1fMWh horizon=%.0fh"
                    " depletion=%.1fh -> EXCLUDED (no flaring cost)",
                    a.site_name, a.total_mwh, horizon_hours, depletion_h,
                )
                continue

            # P0-5: DEMAND SPLIT — explicit demand formula per site type
            _usable_kg = a.usable_kg  # gas above operational floor (accessible)

            # BALANCED: expand the screening horizon for high-value sites so
            # expensive producers (and high-consumption consumers) are included
            # proactively before they reach the standard warning window.
            # A producer at 100 EUR/MWh gets ~1.67× the normal lookahead.
            # TIME: use the horizon as-is (pure urgency, no economic lookahead).
            if objective == ObjectiveFunction.BALANCED:
                if a.site_type == "production" and a.flaring_cost_eur_mwh > 0:
                    _cost_factor = 1.0 + min(1.0, a.flaring_cost_eur_mwh / 150.0)
                    _effective_horizon = horizon_hours * _cost_factor
                elif a.site_type != "production" and a.consumption_rate_kg_hour > 0:
                    # Scale consumers by their hourly flow value relative to a
                    # 500 kg/h reference — bigger consumers get a modest boost.
                    _cost_factor = 1.0 + min(0.5, a.consumption_rate_kg_hour / 500.0)
                    _effective_horizon = horizon_hours * _cost_factor
                else:
                    _effective_horizon = horizon_hours
            else:
                _effective_horizon = horizon_hours

            if a.site_type == "production":
                # Producer: demand = excess above safe threshold
                # capacity_limit = max_usable_kg - production_rate * horizon
                # (equivalent to: site will overflow within the horizon)
                _prod_rate = a.production_rate_kg_hour
                _max_usable_kg = a.bays_fixed * 2697.4  # USABLE_KG_PER_BAY
                _capacity_limit = _max_usable_kg - _prod_rate * _effective_horizon
                demand_kg = max(0.0, _usable_kg - _capacity_limit)
            else:
                # Consumer: demand = projected shortage over the horizon
                _cons_rate = a.consumption_rate_kg_hour
                demand_kg = max(0.0, _cons_rate * _effective_horizon - _usable_kg)

            if demand_kg > 0:
                demand.append(a.site_id)
                priority_bucket = self._demand_priority_bucket(current_risk, projected_risk)
                if priority_bucket == 0:
                    n_current_critical += 1
                elif priority_bucket == 1:
                    n_current_warning += 1
                else:
                    n_preventive += 1
                demand_meta[a.site_id] = {
                    "current_risk": current_risk,
                    "projected_risk": projected_risk,
                    "current_hours_to_critical": float(a.hours_to_critical),
                    "projected_hours_to_critical": float(projected.hours_to_critical),
                    "priority_bucket": priority_bucket,
                    "solver_priority_score": self._solver_priority_score(
                        current_risk=current_risk,
                        current_score=float(a.risk_score),
                        projected_risk=projected_risk,
                        projected_score=float(projected.risk_score),
                    ),
                    "selection_reason": "projected_shortage_or_overflow",
                    "projected_demand_kg": float(demand_kg),
                    "posture_gap_eur": 0.0,
                }
                logger.info(
                    "[DemandFilter] Site=%-20s usable=%.1fMWh horizon=%.0fh"
                    " demand_kg=%.1f -> INCLUDED",
                    a.site_name, a.total_mwh, horizon_hours, demand_kg,
                )
            else:
                site_obj = self.sites.get(a.site_id)
                posture_gap_eur = self._compute_future_buffer_gap_eur(site_obj)
                if posture_gap_eur > self.config.handling_fee_eur:
                    demand.append(a.site_id)
                    priority_bucket = self._demand_priority_bucket(current_risk, projected_risk)
                    if priority_bucket == 0:
                        n_current_critical += 1
                    elif priority_bucket == 1:
                        n_current_warning += 1
                    else:
                        n_preventive += 1
                    demand_meta[a.site_id] = {
                        "current_risk": current_risk,
                        "projected_risk": projected_risk,
                        "current_hours_to_critical": float(a.hours_to_critical),
                        "projected_hours_to_critical": float(projected.hours_to_critical),
                        "priority_bucket": priority_bucket,
                        "solver_priority_score": self._solver_priority_score(
                            current_risk=current_risk,
                            current_score=float(a.risk_score),
                            projected_risk=projected_risk,
                            projected_score=float(projected.risk_score),
                        ),
                        "selection_reason": "future_buffer_gap",
                        "projected_demand_kg": 0.0,
                        "posture_gap_eur": float(posture_gap_eur),
                    }
                    logger.info(
                        "[DemandFilter] Site=%-20s demand_kg=0 but posture_gap=%.1fEUR"
                        " -> INCLUDED for tomorrow buffer",
                        a.site_name, posture_gap_eur,
                    )
                else:
                    logger.debug(
                        "[DemandFilter] Site=%-20s usable=%.1fMWh horizon=%.0fh"
                        " demand_kg=0 -> EXCLUDED (no projected shortage/overflow)",
                        a.site_name, a.total_mwh, horizon_hours,
                    )

        # Sort demand list by objective priority so the solver sees the most
        # important sites first (matters when capacity forces sites to be dropped).
        if demand:
            def _sort_key(sid: str) -> tuple:
                meta = demand_meta.get(sid, {})
                bucket = int(meta.get("priority_bucket", 99))
                htc = float(meta.get("current_hours_to_critical", 99999.0))
                solver_score = float(meta.get("solver_priority_score", 0.0))
                if objective == ObjectiveFunction.TIME:
                    return (bucket, htc, -solver_score)
                return (bucket, -solver_score, htc)

            demand.sort(key=_sort_key)

        print(
            f"[DemandSummary] candidates={len(demand)}"
            f" current_critical={n_current_critical}"
            f" current_warning={n_current_warning}"
            f" preventive={n_preventive}"
        )
        logger.info(
            "[DemandSummary] candidates=%d current_critical=%d current_warning=%d preventive=%d"
            " (horizon=%.0fh, objective=%s)",
            len(demand), n_current_critical, n_current_warning, n_preventive,
            horizon_hours, objective.value,
        )

        return demand, demand_meta

    # Bay pressure thresholds
    FULL_BAR = 200                        # bay is "full" if pressure >= 200 bar
    EMPTY_BAR = BASE_PRESSURE_BAR        # 20 — operational floor (never go below)

    # Smart-swap thresholds
    # Always eligible for pickup — container has less than ~half a tank.
    LOW_KG_THRESHOLD = 658.0       # kg at 100 bar effective
    # Eligible for pickup when site is NOT critical — container still has meaningful gas
    # but the site can afford to lose it.
    MEDIUM_KG_THRESHOLD = 1500.0   # kg just above the half-full mark (~175 bar effective)
    # Site is treated as critical below this many hours remaining.
    MIN_SAFE_HOURS = 4.0
    # Minimum priority-weighted kg gain required to execute a delivery swap.
    # gain = (truck_container.kg − site_container.kg) × (1 / max(h2e, 1))
    # A small value filters near-zero-benefit swaps while still allowing urgent sites
    # to trigger on almost any positive gain.
    MIN_SWAP_GAIN = 50.0
    # Minimum pressure (bar) for a bay to be pickable at a producer site.
    # Allows near-full partial bays (e.g., 198 bar) to be picked up.
    PRODUCER_PICKUP_MIN_BAR = 80
    # Forward-looking operating posture: keep enough gas / free bay room so the
    # network is healthy tomorrow, not only "not critical today".
    USABLE_KG_PER_BAY = 2697.4
    TARGET_BUFFER_HOURS = 24.0
    TARGET_BUFFER_WEIGHT = 0.35
    # Keep a small reserve of service slots when there is no critical site so a
    # truck-day is not planned at 100% fragility.
    RESERVE_STOPS_PER_TRUCK = 1

    # ── Route quality thresholds (post-solve filter) ─────────────────────────
    # Routes that fall below these thresholds are considered trivial and are
    # either dropped (hard fail) or warned (soft fail).
    # Hard drop:  containers_delivered == 0  (route does nothing useful)
    #             containers_delivered  < MIN_CONTAINERS_HARD  AND  op_stops < MIN_OP_STOPS_HARD
    # Soft warn:  total_distance_km < MIN_ROUTE_DISTANCE_WARN  (short but maybe valid)
    MIN_CONTAINERS_HARD  = 2   # routes delivering fewer containers are trivial
    MIN_OP_STOPS_HARD    = 2   # operational stop count (excl. start depot) below which trivial
    MIN_ROUTE_DISTANCE_WARN = 30.0  # km; routes shorter than this generate a warning

    def _filter_trivial_routes(
        self,
        routes: List[Route],
        warnings: List[str],
    ) -> List[Route]:
        """Flag and discard routes below quality thresholds.

        Routes with BOTH zero pickups and zero deliveries are discarded —
        they carry no operational value.  Other low-quality routes are kept
        but flagged as warnings for the operator.

        Warning levels:
          [TRIVIAL_ROUTE]  zero containers delivered, or fewer than
                           MIN_CONTAINERS_HARD with fewer than MIN_OP_STOPS_HARD
                           operational stops — operator should review.
          [SHORT_ROUTE]    total distance < MIN_ROUTE_DISTANCE_WARN — may be
                           valid for a nearby critical site, but worth noting.
          [ChainRoute]     (INFO log only) delivered ≥ 2 across ≥ 2 op stops —
                           confirms the route meets the high-value target.
        """
        kept_routes = []
        for route in routes:
            containers_delivered = sum(
                len(stop.swap_operation.containers_dropped)
                for stop in route.stops
                if stop.swap_operation
            )
            containers_picked = sum(
                len(stop.swap_operation.containers_picked)
                for stop in route.stops
                if stop.swap_operation
            )
            op_stops = sum(
                1 for stop in route.stops
                if stop.swap_operation and (
                    stop.swap_operation.containers_dropped
                    or stop.swap_operation.containers_picked
                )
                and stop.sequence > 0  # exclude start depot
            )
            dist_km = route.total_distance_km

            # Discard routes with no pickups AND no deliveries — useless
            if containers_delivered == 0 and containers_picked == 0:
                msg = (
                    f"[TRIVIAL_ROUTE] truck={route.truck_id} day={route.day_index}"
                    f" delivered=0 picked=0 op_stops={op_stops} dist={dist_km:.1f}km"
                    f" — discarded (no operations)"
                )
                warnings.append(msg)
                logger.warning(msg)
                continue

            kept_routes.append(route)

            # Zero deliveries (has pickups but no drops — e.g. repositioning with load)
            if containers_delivered == 0:
                msg = (
                    f"[TRIVIAL_ROUTE] truck={route.truck_id} day={route.day_index}"
                    f" delivered=0 picked={containers_picked} op_stops={op_stops} dist={dist_km:.1f}km"
                    f" (pickup-only route — kept)"
                )
                warnings.append(msg)
                logger.warning(msg)

            # Single-container micro-route
            elif containers_delivered < self.MIN_CONTAINERS_HARD and op_stops < self.MIN_OP_STOPS_HARD:
                msg = (
                    f"[TRIVIAL_ROUTE] truck={route.truck_id} day={route.day_index}"
                    f" delivered={containers_delivered} op_stops={op_stops}"
                    f" dist={dist_km:.1f}km (below min quality — kept)"
                )
                warnings.append(msg)
                logger.warning(msg)

            # Suspiciously short distance
            if dist_km < self.MIN_ROUTE_DISTANCE_WARN:
                msg = (
                    f"[SHORT_ROUTE] truck={route.truck_id} day={route.day_index}"
                    f" dist={dist_km:.1f}km < {self.MIN_ROUTE_DISTANCE_WARN:.0f}km"
                    f" (delivered={containers_delivered} op_stops={op_stops})"
                )
                warnings.append(msg)
                logger.info(msg)

            # Chain-route confirmation
            if containers_delivered >= self.MIN_CONTAINERS_HARD and op_stops >= self.MIN_OP_STOPS_HARD:
                logger.info(
                    "[ChainRoute] truck=%s day=%d delivered=%d op_stops=%d dist=%.1fkm",
                    route.truck_id, route.day_index, containers_delivered, op_stops, dist_km,
                )

        return kept_routes

    @staticmethod
    def _bay_state(pressure_bar: int) -> str:
        if pressure_bar >= RecommendationService.FULL_BAR:
            return "full"
        if pressure_bar <= RecommendationService.EMPTY_BAR:
            return "empty"
        return "partial"

    def _build_site_bay_inventory(self) -> Dict[str, Dict[str, List[str]]]:
        """Build per-site bay inventory classified by state.

        Returns dict of site_id -> {"full": [bay_ids], "partial": [bay_ids], "empty": [bay_ids]}
        """
        inv: Dict[str, Dict[str, List[str]]] = {}
        for site_id, site in self.sites.items():
            full, partial, empty = [], [], []
            for b in site.bays:
                state = self._bay_state(b.pressure_bar)
                if state == "full":
                    full.append(b.bay_id)
                elif state == "empty":
                    empty.append(b.bay_id)
                else:
                    partial.append(b.bay_id)
            inv[site_id] = {"full": full, "partial": partial, "empty": empty}
        return inv

    @staticmethod
    def _bay_operational_label(pressure_bar: int) -> str:
        """Operational label for UI/explanations; never replaces continuous kg logic."""
        if pressure_bar >= RecommendationService.FULL_BAR:
            return "full"
        if pressure_bar <= RecommendationService.EMPTY_BAR:
            return "empty"
        return "medium"

    def _compute_future_buffer_gap_eur(
        self,
        site: Optional[Site],
        horizon_hours: float = TARGET_BUFFER_HOURS,
    ) -> float:
        """EUR value of being below the desired "healthy tomorrow" posture."""
        if site is None:
            return 0.0

        floor = self.config.usable_floor_bar
        usable_kg = sum(
            get_normalized_kg(effective_pressure_bar(b.pressure_bar), floor)
            for b in site.bays
        )

        if site.is_consumer:
            cons_rate = site.consumption_rate_kg_hour or 0.0
            if cons_rate <= 0.0:
                return 0.0
            target_kg = cons_rate * horizon_hours
            gap_kg = max(0.0, target_kg - usable_kg)
            return gap_kg * 2.0 * self.TARGET_BUFFER_WEIGHT

        if site.is_producer:
            prod_rate = (
                site.production.effective_kg_per_h
                if site.production and site.production.effective_kg_per_h is not None
                else 0.0
            )
            if prod_rate <= 0.0:
                return 0.0
            cap_kg = site.bays_fixed * self.USABLE_KG_PER_BAY
            free_capacity_kg = max(0.0, cap_kg - usable_kg)
            target_free_kg = prod_rate * horizon_hours
            gap_kg = max(0.0, target_free_kg - free_capacity_kg)
            _flaring_eur_per_kg = (
                site.flaring_cost_eur_mwh * 15.2 / 1000
                if site.flaring_cost_eur_mwh > 0
                else 0.5
            )
            return gap_kg * _flaring_eur_per_kg * self.TARGET_BUFFER_WEIGHT

        return 0.0

    def _compute_route_cycle_break_penalty(self, routes: List[Route]) -> float:
        """Soft operational penalty for routes that leave awkward next-day posture."""
        penalty = 0.0
        for route in routes:
            if not route.stops:
                continue
            last_stop = route.stops[-1]
            if (last_stop.load_full_after or 0) > 0:
                penalty += (last_stop.load_full_after or 0) * self.config.handling_fee_eur * 2.0
            if (last_stop.load_empty_after or 0) > 0:
                penalty += (last_stop.load_empty_after or 0) * self.config.handling_fee_eur * 0.5
        return penalty

    def _summarize_bay_mix(self) -> str:
        """Compact operational summary of container states in the current network."""
        counts = {"full": 0, "medium": 0, "empty": 0}
        for site in self.sites.values():
            for bay in site.bays:
                counts[self._bay_operational_label(bay.pressure_bar)] += 1
        return (
            f"Bay mix now: {counts['full']} full, "
            f"{counts['medium']} medium, {counts['empty']} empty."
        )

    def _distance_between_sites(self, from_site_id: str, to_site_id: str) -> float:
        """Best-effort distance lookup with symmetric fallback."""
        if from_site_id == to_site_id:
            return 0.0
        direct = self.distance_matrix.get(from_site_id, {}).get(to_site_id)
        if direct is not None:
            return direct
        reverse = self.distance_matrix.get(to_site_id, {}).get(from_site_id)
        if reverse is not None:
            return reverse
        return float("inf")

    def _compute_relief_consumers(
        self,
        current_demand: List[str],
        risk_map: Dict[str, str],
        hours_to_critical_map: Dict[str, float],
        selected_trucks: List[Truck],
    ) -> List[str]:
        """Add safe consumer receivers when a producer is critical.

        This lets the planner relieve an overfull plant by delivering to consumers
        that are not yet critical but have physical empty capacity and would
        benefit from preventive replenishment.
        """
        urgent_producers = [
            sid for sid in current_demand
            if self.sites.get(sid)
            and self.sites[sid].is_producer
            and risk_map.get(sid) in ("critical", "warning")
        ]
        if not urgent_producers:
            return []

        max_aux_sites = max(2, sum(max(1, t.capacity) for t in selected_trucks))
        candidates: List[tuple[str, float, float, int, float]] = []
        for sid, site in self.sites.items():
            if sid in current_demand or not site.is_consumer or not site.swap_allowed:
                continue

            empty_slots = sum(
                1 for bay in site.bays
                if self._bay_state(bay.pressure_bar) in ("empty", "partial")
            )
            if empty_slots <= 0:
                continue

            htc = hours_to_critical_map.get(sid, float("inf"))
            if htc <= self.config.critical_hours_threshold:
                continue

            posture_gap = self._compute_future_buffer_gap_eur(site, self.TARGET_BUFFER_HOURS)
            nearest_urgent_producer = min(
                self._distance_between_sites(prod_sid, sid)
                for prod_sid in urgent_producers
            )
            candidates.append((sid, posture_gap, htc, empty_slots, nearest_urgent_producer))

        candidates.sort(
            key=lambda item: (
                -item[1],   # highest preventive value first
                item[4],    # closest to urgent producer
                item[2],    # lower htc next
                -item[3],   # more available slots
            )
        )
        return [sid for sid, *_ in candidates[:max_aux_sites]]

    def _append_stop_to_route(
        self,
        route: Route,
        site_id: str,
        site_name: str,
        avg_speed_kmh: float,
    ) -> None:
        """Append a raw stop to a route, allowing repeated site visits."""
        prev_stop = route.stops[-1] if route.stops else None
        prev_site_id = prev_stop.site_id if prev_stop else site_id
        dist_km = self._distance_between_sites(prev_site_id, site_id)
        if not math.isfinite(dist_km):
            return
        travel_h = dist_km / max(avg_speed_kmh, 1.0)
        arrival_h = (prev_stop.departure_time_hours if prev_stop else 0.0) + travel_h
        route.stops.append(
            RouteStop(
                sequence=len(route.stops),
                site_id=site_id,
                site_name=site_name,
                arrival_time_hours=arrival_h,
                distance_from_previous_km=dist_km if prev_stop else 0.0,
                cumulative_distance_km=(prev_stop.cumulative_distance_km if prev_stop else 0.0) + (dist_km if prev_stop else 0.0),
                service_time_hours=0.0,
            )
        )

    def _extend_routes_with_rescue_loops(
        self,
        routes: List[Route],
        primary_demand: List[str],
        relief_consumers: List[str],
        hours_to_critical_map: Optional[Dict[str, float]] = None,
    ) -> List[Route]:
        """Append controlled revisit loops around urgent producers.

        This is a post-VRP heuristic that compensates for the single-visit node
        model in OR-Tools. It can build patterns like:
          donor consumer -> urgent producer -> preventive receiver consumer(s)
        and may revisit the same consumer later in the same truck-day.
        """
        if not routes:
            return routes

        urgent_producers = [
            sid for sid in primary_demand
            if self.sites.get(sid) and self.sites[sid].is_producer
        ]
        if not urgent_producers or not relief_consumers:
            return routes

        avg_speed = max(self.config.avg_speed_kmph, 1.0)
        hours_map = hours_to_critical_map or {}
        inv = self._build_site_bay_inventory()

        def _consumer_slot_count(site_id: str) -> int:
            sb = inv.get(site_id, {"full": [], "partial": [], "empty": []})
            return len(sb["empty"]) + len(sb["partial"])

        def _producer_full_count(site_id: str) -> int:
            sb = inv.get(site_id, {"full": [], "partial": [], "empty": []})
            return len(sb["full"]) + len(sb["partial"])

        def _time_for_path(path: List[str], start_site_id: str) -> float:
            total_h = 0.0
            prev = start_site_id
            for sid in path:
                dist = self._distance_between_sites(prev, sid)
                if not math.isfinite(dist):
                    return float("inf")
                total_h += dist / avg_speed
                total_h += self.config.swap_time_hours
                prev = sid
            return total_h

        for route in routes:
            if not route.stops:
                continue
            truck = self.trucks.get(route.truck_id)
            if not truck:
                continue

            remaining_h = self.config.max_driver_hours - route.total_time_hours
            if remaining_h < (self.config.swap_time_hours * 3):
                continue

            current_site_id = route.stops[-1].site_id
            best_producer = min(
                urgent_producers,
                key=lambda sid: (
                    hours_map.get(sid, float("inf")),
                    self._distance_between_sites(current_site_id, sid),
                ),
            )
            if _producer_full_count(best_producer) <= 0:
                continue

            receiver_pool = [
                sid for sid in relief_consumers
                if _consumer_slot_count(sid) > 0
            ]
            if not receiver_pool:
                continue

            receiver_pool.sort(
                key=lambda sid: (
                    self._distance_between_sites(best_producer, sid),
                    -_consumer_slot_count(sid),
                    hours_map.get(sid, float("inf")),
                )
            )
            cycle_sites = receiver_pool[: max(1, min(truck.capacity, len(receiver_pool)))]
            if not cycle_sites:
                continue

            pre_sites: List[str] = []
            if not (self.sites.get(current_site_id) and self.sites[current_site_id].is_producer):
                pre_sites = sorted(
                    cycle_sites,
                    key=lambda sid: self._distance_between_sites(current_site_id, sid),
                )

            post_sites = sorted(
                cycle_sites,
                key=lambda sid: self._distance_between_sites(best_producer, sid),
            )
            rescue_path = pre_sites + [best_producer] + post_sites
            path_time_h = _time_for_path(rescue_path, current_site_id)
            if path_time_h > remaining_h:
                continue

            for sid in rescue_path:
                site = self.sites.get(sid)
                if site is None:
                    continue
                self._append_stop_to_route(route, sid, site.name, avg_speed)

        return routes

    def _inject_idle_truck_relief_routes(
        self,
        routes: List[Route],
        selected_trucks: List[Truck],
        active_demand: List[str],
        risk_map: Dict[str, str],
        hours_to_critical_map: Dict[str, float],
        day_index: int,
    ) -> List[Route]:
        """Create pragmatic best-effort routes for idle trucks.

        If OR-Tools leaves a truck idle while critical/warning consumers remain
        unserved, synthesize a small producer->consumer route instead of
        accepting idle time. This is intentionally heuristic and operationally
        biased toward "do something useful" behavior.
        """
        if not selected_trucks:
            return routes

        used_trucks = {route.truck_id for route in routes}
        served_sites = {
            stop.site_id
            for route in routes
            for stop in route.stops[1:]
        }
        open_sites = [
            sid for sid in active_demand
            if sid not in served_sites
            and self.sites.get(sid)
            and self.sites[sid].is_consumer
            and risk_map.get(sid) in ("critical", "warning")
        ]
        if not open_sites:
            return routes

        avg_speed = max(self.config.avg_speed_kmph, 1.0)

        def _producer_inventory_score(site_id: str) -> int:
            site = self.sites.get(site_id)
            if site is None:
                return 0
            return sum(1 for bay in site.bays if bay.pressure_bar >= self.FULL_BAR)

        for truck in selected_trucks:
            if truck.id in used_trucks:
                continue

            start_site_id = truck.effective_start_site_id or truck.home_site_id
            start_site = self.sites.get(start_site_id)
            if start_site is None:
                continue

            route = Route(
                truck_id=truck.id,
                day_index=day_index,
                start_site_id=start_site_id,
                start_label=start_site.name,
                stops=[
                    RouteStop(
                        sequence=0,
                        site_id=start_site_id,
                        site_name=start_site.name,
                        arrival_time_hours=0.0,
                        distance_from_previous_km=0.0,
                        cumulative_distance_km=0.0,
                        service_time_hours=0.0,
                    )
                ],
            )

            current_site_id = start_site_id
            remaining_h = self.config.max_driver_hours
            if not start_site.is_producer:
                producer_candidates = [
                    sid for sid, site in self.sites.items()
                    if site.is_producer and _producer_inventory_score(sid) > 0
                ]
                if not producer_candidates:
                    continue
                producer_candidates.sort(
                    key=lambda sid: (
                        self._distance_between_sites(current_site_id, sid),
                        -_producer_inventory_score(sid),
                    )
                )
                producer_sid = producer_candidates[0]
                producer_time = (
                    self._distance_between_sites(current_site_id, producer_sid) / avg_speed
                    + self.config.swap_time_hours
                )
                if not math.isfinite(producer_time) or producer_time > remaining_h:
                    continue
                self._append_stop_to_route(route, producer_sid, self.sites[producer_sid].name, avg_speed)
                current_site_id = producer_sid
                remaining_h -= producer_time

            delivered = 0
            while open_sites and delivered < max(1, truck.capacity):
                open_sites.sort(
                    key=lambda sid: (
                        hours_to_critical_map.get(sid, float("inf")),
                        self._distance_between_sites(current_site_id, sid),
                    )
                )
                next_sid = open_sites[0]
                leg_time = (
                    self._distance_between_sites(current_site_id, next_sid) / avg_speed
                    + self.config.swap_time_hours
                )
                if not math.isfinite(leg_time) or leg_time > remaining_h:
                    break
                self._append_stop_to_route(route, next_sid, self.sites[next_sid].name, avg_speed)
                current_site_id = next_sid
                remaining_h -= leg_time
                delivered += 1
                open_sites.pop(0)

            if len(route.stops) > 1:
                routes.append(route)
                used_trucks.add(truck.id)

        return routes

    def _augment_day_routes_with_idle_relief(
        self,
        routes: List[Route],
        selected_trucks: List[Truck],
        demand_sites: List[str],
        risk_map: Dict[str, str],
        hours_to_critical_map: Dict[str, float],
        day_index: int,
        warnings: List[str],
    ) -> List[Route]:
        """Add extra routes when the day plan leaves critical sites unserved.

        This is a quality-improvement pass, not a feasibility fallback. If a
        daily plan leaves critical consumers unserved while trucks remain idle,
        synthesize additional raw routes for those idle trucks and run swap
        assignment on them before accepting the day as complete.
        """
        if not routes or not selected_trucks:
            return routes

        preserved_routes: List[Route] = []
        reusable_truck_ids: set[str] = set()
        for route in routes:
            served_high_priority = any(
                stop.site_id in demand_sites
                and risk_map.get(stop.site_id) in ("critical", "warning")
                and stop.swap_operation
                and (
                    stop.swap_operation.containers_dropped
                    or stop.swap_operation.containers_picked
                )
                for stop in route.stops
            )
            route_time_h = float(getattr(route, "total_time_hours", 0.0) or 0.0)
            if not served_high_priority and route_time_h < 2.0:
                reusable_truck_ids.add(route.truck_id)
                warnings.append(
                    f"[CRITICAL_RELIEF_REPLACE] Day {day_index}: replacing short non-critical route on {route.truck_id} with critical relief attempt."
                )
                continue
            preserved_routes.append(route)

        served_sites = {
            stop.site_id
            for route in preserved_routes
            for stop in route.stops
            if stop.swap_operation and (
                stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
            )
        }
        idle_trucks = [
            truck for truck in selected_trucks
            if truck.id in reusable_truck_ids
            or not any(route.truck_id == truck.id for route in preserved_routes)
        ]
        unresolved_critical = [
            sid for sid in demand_sites
            if sid not in served_sites
            and self.sites.get(sid)
            and self.sites[sid].is_consumer
            and risk_map.get(sid) == "critical"
        ]
        if not idle_trucks or not unresolved_critical:
            return preserved_routes

        raw_routes = self._inject_idle_truck_relief_routes(
            routes=copy.deepcopy(preserved_routes),
            selected_trucks=selected_trucks,
            active_demand=unresolved_critical,
            risk_map=risk_map,
            hours_to_critical_map=hours_to_critical_map,
            day_index=day_index,
        )
        existing_ids = {route.truck_id for route in preserved_routes}
        extra_raw_routes = [route for route in raw_routes if route.truck_id not in existing_ids]
        if not extra_raw_routes:
            return preserved_routes

        seeded_inventory = self._build_site_bay_inventory()
        for route in preserved_routes:
            for stop in route.stops:
                sw = stop.swap_operation
                if not sw:
                    continue
                sb = seeded_inventory.setdefault(stop.site_id, {"full": [], "partial": [], "empty": []})
                for bid in sw.containers_picked:
                    for bucket in ("full", "partial", "empty"):
                        if bid in sb[bucket]:
                            sb[bucket].remove(bid)
                            break

        strict_extra = self._assign_swap_operations(
            copy.deepcopy(extra_raw_routes),
            demand_sites,
            seed_inventory=seeded_inventory,
        )
        strict_extra = self._close_routes_to_empty(strict_extra, demand_sites, hours_to_critical_map)
        strict_extra = self._prune_noop_stops(strict_extra)
        strict_qw: List[str] = []
        strict_extra = self._filter_trivial_routes(strict_extra, strict_qw)
        valid_extra, _, _, _ = self._validate_routes_strict(strict_extra) if strict_extra else (False, "", "", {})

        if not strict_extra or not valid_extra:
            relaxed_extra_warnings: List[str] = []
            relaxed_extra = self._assign_swap_operations_relaxed(
                copy.deepcopy(extra_raw_routes),
                demand_sites,
                relaxed_extra_warnings,
                seed_inventory=seeded_inventory,
            )
            relaxed_extra = self._close_routes_to_empty(relaxed_extra, demand_sites, hours_to_critical_map)
            relaxed_extra = self._prune_noop_stops(relaxed_extra)
            relaxed_qw: List[str] = []
            relaxed_extra = self._filter_trivial_routes(relaxed_extra, relaxed_qw)
            if relaxed_extra:
                for w in relaxed_extra_warnings + relaxed_qw:
                    warnings.append(f"Day {day_index}: {w}")
                warnings.append(
                    f"[IDLE_RELIEF_FALLBACK] Day {day_index}: added {len(relaxed_extra)} idle-truck relief route(s)"
                )
                return preserved_routes + relaxed_extra
            direct_cycles = self._build_idle_truck_critical_cycles(
                routes=preserved_routes,
                idle_trucks=idle_trucks,
                unresolved_critical=unresolved_critical,
                hours_to_critical_map=hours_to_critical_map,
                day_index=day_index,
            )
            if direct_cycles:
                warnings.append(
                    f"[IDLE_RELIEF_DIRECT] Day {day_index}: added {len(direct_cycles)} direct critical-relief cycle(s)"
                )
                return preserved_routes + direct_cycles
            return preserved_routes

        for w in strict_qw:
            warnings.append(f"Day {day_index}: {w}")
        warnings.append(
            f"[IDLE_RELIEF] Day {day_index}: added {len(strict_extra)} idle-truck relief route(s)"
        )
        return preserved_routes + strict_extra

    def _build_idle_truck_critical_cycles(
        self,
        routes: List[Route],
        idle_trucks: List[Truck],
        unresolved_critical: List[str],
        hours_to_critical_map: Dict[str, float],
        day_index: int,
    ) -> List[Route]:
        """Construct direct producer→critical-consumer→producer cycles.

        This is a last-resort operational rescue for idle trucks. It avoids
        accepting a weak plan when a truck can still run one balanced cycle to
        cover an unserved critical consumer.
        """
        if not idle_trucks or not unresolved_critical:
            return []

        avg_speed = max(self.config.avg_speed_kmph, 1.0)
        bay_lookup = {
            b.bay_id: b
            for site in self.sites.values()
            for b in site.bays
        }
        inv = self._build_site_bay_inventory()
        for route in routes:
            for stop in route.stops:
                sw = stop.swap_operation
                if not sw:
                    continue
                sb = inv.setdefault(stop.site_id, {"full": [], "partial": [], "empty": []})
                for bid in sw.containers_picked:
                    for bucket in ("full", "partial", "empty"):
                        if bid in sb[bucket]:
                            sb[bucket].remove(bid)
                            break

        def _travel_h(a: str, b: str) -> float:
            dist = self._distance_between_sites(a, b)
            if not math.isfinite(dist):
                return float("inf")
            return dist / avg_speed

        def _pickable_producer_bays(site_id: str) -> List[str]:
            site = self.sites.get(site_id)
            if site is None:
                return []
            pressure_map = {b.bay_id: b.pressure_bar for b in site.bays}
            sb = inv.get(site_id, {"full": [], "partial": [], "empty": []})
            bays = [
                bid for bid in (sb["full"] + sb["partial"])
                if pressure_map.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
            ]
            bays.sort(key=lambda bid: pressure_map.get(bid, 0), reverse=True)
            return bays

        def _consumer_return_bays(site_id: str) -> List[str]:
            site = self.sites.get(site_id)
            if site is None:
                return []
            kg_map = {b.bay_id: getattr(b, "kg", 0.0) for b in site.bays}
            sb = inv.get(site_id, {"full": [], "partial": [], "empty": []})
            bays = list(sb["empty"] + sb["partial"])
            bays.sort(key=lambda bid: kg_map.get(bid, 0.0))
            return bays

        def _append_operational_stop(
            route: Route,
            site_id: str,
            site_name: str,
            swap_operation: SwapOperation,
            load_full_after: int,
            load_empty_after: int,
        ) -> None:
            prev_stop = route.stops[-1] if route.stops else None
            prev_site_id = prev_stop.site_id if prev_stop else site_id
            dist_km = self._distance_between_sites(prev_site_id, site_id) if prev_stop else 0.0
            if prev_stop and not math.isfinite(dist_km):
                return
            travel_h = (dist_km / avg_speed) if prev_stop else 0.0
            arrival_h = (prev_stop.departure_time_hours if prev_stop else 0.0) + travel_h
            route.stops.append(
                RouteStop(
                    sequence=len(route.stops),
                    site_id=site_id,
                    site_name=site_name,
                    arrival_time_hours=arrival_h,
                    distance_from_previous_km=dist_km if prev_stop else 0.0,
                    cumulative_distance_km=(prev_stop.cumulative_distance_km if prev_stop else 0.0) + (dist_km if prev_stop else 0.0),
                    service_time_hours=self.config.swap_time_hours,
                    swap_operation=swap_operation,
                    truck_load_after=load_full_after + load_empty_after,
                    load_full_after=load_full_after,
                    load_empty_after=load_empty_after,
                )
            )

        built_routes: List[Route] = []
        unresolved = sorted(
            unresolved_critical,
            key=lambda sid: hours_to_critical_map.get(sid, float("inf")),
        )

        for truck in idle_trucks:
            if not unresolved:
                break
            start_site_id = truck.effective_start_site_id or truck.home_site_id
            start_site = self.sites.get(start_site_id)
            if start_site is None:
                continue

            best_plan = None
            for consumer_sid in list(unresolved):
                consumer_site = self.sites.get(consumer_sid)
                if consumer_site is None or not consumer_site.is_consumer or not consumer_site.swap_allowed:
                    continue
                return_bays = _consumer_return_bays(consumer_sid)
                if not return_bays:
                    continue
                for producer_sid, producer_site in self.sites.items():
                    if not producer_site.is_producer or not producer_site.swap_allowed:
                        continue
                    producer_bays = _pickable_producer_bays(producer_sid)
                    if not producer_bays:
                        continue
                    cycle_h = (
                        _travel_h(start_site_id, producer_sid)
                        + self.config.swap_time_hours
                        + _travel_h(producer_sid, consumer_sid)
                        + self.config.swap_time_hours
                        + _travel_h(consumer_sid, producer_sid)
                        + self.config.swap_time_hours
                    )
                    if not math.isfinite(cycle_h) or cycle_h > self.config.max_driver_hours:
                        continue
                    score = (
                        hours_to_critical_map.get(consumer_sid, float("inf")),
                        cycle_h,
                        self._distance_between_sites(start_site_id, producer_sid),
                    )
                    if best_plan is None or score < best_plan[0]:
                        best_plan = (score, producer_sid, consumer_sid, producer_bays[0], return_bays[0])

            if best_plan is None:
                continue

            _, producer_sid, consumer_sid, full_bay_id, empty_bay_id = best_plan
            producer_site = self.sites[producer_sid]
            consumer_site = self.sites[consumer_sid]
            route = Route(
                truck_id=truck.id,
                day_index=day_index,
                start_site_id=start_site_id,
                start_label=start_site.name,
                stops=[
                    RouteStop(
                        sequence=0,
                        site_id=start_site_id,
                        site_name=start_site.name,
                        arrival_time_hours=0.0,
                        distance_from_previous_km=0.0,
                        cumulative_distance_km=0.0,
                        service_time_hours=0.0,
                    )
                ],
            )

            if start_site_id == producer_sid:
                route.stops[0].service_time_hours = self.config.swap_time_hours
                route.stops[0].swap_operation = SwapOperation(
                    site_id=producer_sid,
                    containers_picked=[full_bay_id],
                    containers_dropped=[],
                )
                route.stops[0].truck_load_after = 1
                route.stops[0].load_full_after = 1
                route.stops[0].load_empty_after = 0
            else:
                _append_operational_stop(
                    route,
                    producer_sid,
                    producer_site.name,
                    SwapOperation(
                        site_id=producer_sid,
                        containers_picked=[full_bay_id],
                        containers_dropped=[],
                    ),
                    load_full_after=1,
                    load_empty_after=0,
                )

            _append_operational_stop(
                route,
                consumer_sid,
                consumer_site.name,
                SwapOperation(
                    site_id=consumer_sid,
                    containers_picked=[empty_bay_id],
                    containers_dropped=[full_bay_id],
                ),
                load_full_after=0,
                load_empty_after=1,
            )
            _append_operational_stop(
                route,
                producer_sid,
                producer_site.name,
                SwapOperation(
                    site_id=producer_sid,
                    containers_picked=[],
                    containers_dropped=[empty_bay_id],
                ),
                load_full_after=0,
                load_empty_after=0,
            )

            for bucket in ("full", "partial"):
                if full_bay_id in inv.setdefault(producer_sid, {"full": [], "partial": [], "empty": []})[bucket]:
                    inv[producer_sid][bucket].remove(full_bay_id)
                    break
            for bucket in ("empty", "partial"):
                if empty_bay_id in inv.setdefault(consumer_sid, {"full": [], "partial": [], "empty": []})[bucket]:
                    inv[consumer_sid][bucket].remove(empty_bay_id)
                    break

            built_routes.append(route)
            unresolved.remove(consumer_sid)

        return built_routes

    def _assign_swap_operations(
        self,
        routes: List[Route],
        demand_sites: List[str],
        seed_inventory: Optional[Dict[str, Dict[str, List[str]]]] = None,
    ) -> List[Route]:
        """
        Assign bay-based swap operations to route stops.

        Closed-loop swap rules:
        1. Two container states: FULL and EMPTY. Truck capacity = max containers.
        2. Consumer stop = SWAP only: deliver k FULL and pick up k EMPTY (k >= 1).
           No picking up empties without delivering the same count of fulls.
        3. Producer stop: drop EMPTY (any amount), pick up FULL (up to capacity).
        4. Before the first consumer, truck must have picked up >= 1 FULL at a producer.
        5. Route must end with load = 0 (no containers left on truck).
        6. EMPTY only dropped at producers; FULL only delivered at consumers.
        """
        # Mutable bay inventory (shared across routes so bays aren't double-counted)
        inv = copy.deepcopy(seed_inventory) if seed_inventory is not None else self._build_site_bay_inventory()

        # bay_id → Bay lookup for kg-aware decisions (covers all real bays)
        bay_lookup: Dict[str, "Bay"] = {  # type: ignore[name-defined]
            b.bay_id: b
            for site in self.sites.values()
            for b in site.bays
        }

        def _bay_kg(bay_id: str) -> float:
            """Return kg for a bay.  Synthetic transit IDs are assumed max-full."""
            b = bay_lookup.get(bay_id)
            if b is not None:
                return b.kg
            return 2829.0  # __transit_* IDs: treat as full (200→250 bar ceiling)

        for route in routes:
            truck = self.trucks.get(route.truck_id)
            if not truck:
                continue

            truck_full: List[str] = []       # full bays on truck
            truck_empty: List[str] = []      # empty/returnable bays on truck

            # Pre-load initial_load containers so stop-0 shows the correct
            # starting state without creating a fake swap operation.
            # If the start site is a producer, deduct those containers from its
            # inventory so they are not picked up again at stop 0.
            _il = truck.initial_load if truck else 0
            if _il > 0 and route.stops:
                _start_sid   = route.stops[0].site_id
                _sb_start    = inv.get(_start_sid, {"full": [], "partial": [], "empty": []})
                _start_site  = self.sites.get(_start_sid)
                for _ in range(_il):
                    if _start_site and _start_site.is_producer and _sb_start["full"]:
                        truck_full.append(_sb_start["full"].pop(0))
                    else:
                        # P0-2: __transit injection forbidden — no real bay available
                        logger.error(
                            "[PHYSICS-P0-2] Truck %s has initial_load=%d but no real bay"
                            " at start site %r — cannot inject __transit container;"
                            " treating remaining initial load as 0",
                            route.truck_id, _il, _start_sid,
                        )
                        break

            for stop in route.stops:
                site = self.sites.get(stop.site_id)
                if not site:
                    continue

                # Stop 0 is the start depot — initial load already in truck_full.
                # Record state.
                # If the truck starts empty at a producer and has future demand,
                # Stop 0 is the start depot. Truck starts with whatever initial_load
                # was explicitly set — no auto-pickup. initial_load=0 → truck starts EMPTY.
                if stop.sequence == 0:
                    start_picked: List[str] = []
                    if site.is_producer and site.swap_allowed:
                        _start_inv = inv.get(stop.site_id, {"full": [], "partial": [], "empty": []})
                        _future_consumers = [
                            s for s in route.stops
                            if s.sequence > stop.sequence
                            and s.site_id in demand_sites
                            and self.sites.get(s.site_id)
                            and self.sites[s.site_id].is_consumer
                        ]
                        if _future_consumers:
                            _avail = truck.capacity - len(truck_full) - len(truck_empty)
                            if _avail > 0:
                                _future_swap_capacity = 0
                                for _fs in _future_consumers:
                                    _finv = inv.get(_fs.site_id, {"full": [], "partial": [], "empty": []})
                                    _future_swap_capacity += len(_finv["empty"]) + len(_finv["partial"])
                                _pickup_cap = max(0, _future_swap_capacity - len(truck_full))
                                _avail = min(_avail, _pickup_cap)
                            if _avail > 0:
                                _site_pressure_map = {b.bay_id: b.pressure_bar for b in site.bays}
                                _pickable = [
                                    bid for bid in (_start_inv["full"] + _start_inv["partial"])
                                    if _site_pressure_map.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
                                ]
                                _pickable_sorted = sorted(
                                    _pickable,
                                    key=lambda bid: _site_pressure_map.get(bid, 0),
                                    reverse=True,
                                )
                                for bay_id in _pickable_sorted[:_avail]:
                                    start_picked.append(bay_id)
                                    truck_full.append(bay_id)
                                    if bay_id in _start_inv["full"]:
                                        _start_inv["full"].remove(bay_id)
                                    else:
                                        _start_inv["partial"].remove(bay_id)
                    stop.service_time_hours = self.config.swap_time_hours if start_picked else 0
                    stop.swap_operation = (
                        SwapOperation(
                            site_id=stop.site_id,
                            containers_dropped=[],
                            containers_picked=start_picked,
                        )
                        if start_picked else None
                    )
                    stop.truck_load_after = len(truck_full) + len(truck_empty)
                    stop.load_full_after = len(truck_full)
                    stop.load_empty_after = len(truck_empty)
                    continue

                sb = inv.get(stop.site_id, {"full": [], "partial": [], "empty": []})
                stop.swap_operation = None  # clear any VRP solver placeholder
                dropped: List[str] = []
                picked: List[str] = []

                _is_last_stop = (stop.sequence == len(route.stops) - 1)

                if site.is_producer and site.swap_allowed:
                    # Always drop empties back to producer (return flow).
                    for bay_id in list(truck_empty):
                        dropped.append(bay_id)
                        truck_empty.remove(bay_id)

                    # Pickup guard — three conditions must ALL be true:
                    # 1. Not the last stop (nowhere to deliver picked containers).
                    # 2. Not a Malmi return-pass: if truck already carries full
                    #    containers and is visiting Malmi, it is on a return leg —
                    #    loading more would create undeliverable excess.
                    # 3. At least one future stop in demand_sites exists to absorb
                    #    the containers (global balance awareness: no pickup if
                    #    nothing to deliver to downstream).
                    _is_malmi_return = (
                        stop.site_id == "helsinki_malmi" and len(truck_full) > 0
                    )
                    _future_demand = any(
                        s.site_id in demand_sites
                        for s in route.stops
                        if s.sequence > stop.sequence
                    )
                    if not _is_last_stop and not _is_malmi_return and _future_demand:
                        avail = truck.capacity - len(truck_full) - len(truck_empty)
                        if avail > 0:
                            # Avoid over-picking: cap pickups to downstream swap capacity.
                            future_swap_capacity = 0
                            for _fs in route.stops:
                                if _fs.sequence <= stop.sequence:
                                    continue
                                _fsite = self.sites.get(_fs.site_id)
                                if not _fsite or not _fsite.is_consumer or not _fsite.swap_allowed:
                                    continue
                                if _fs.site_id not in demand_sites:
                                    continue
                                _finv = inv.get(_fs.site_id, {"full": [], "partial": [], "empty": []})
                                future_swap_capacity += len(_finv["empty"]) + len(_finv["partial"])
                            pickup_cap = max(0, future_swap_capacity - len(truck_full))
                            avail = min(avail, pickup_cap)
                        if avail > 0:
                            _site_pressure_map = {b.bay_id: b.pressure_bar for b in site.bays}
                            _pickable = [
                                bid for bid in (sb["full"] + sb["partial"])
                                if _site_pressure_map.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
                            ]
                            _pickable_sorted = sorted(
                                _pickable,
                                key=lambda bid: _site_pressure_map.get(bid, 0),
                                reverse=True,
                            )
                            for bay_id in _pickable_sorted[:avail]:
                                picked.append(bay_id)
                                truck_full.append(bay_id)
                                if bay_id in sb["full"]:
                                    sb["full"].remove(bay_id)
                                else:
                                    sb["partial"].remove(bay_id)

                elif site.is_consumer and stop.site_id in demand_sites and site.swap_allowed:
                    # Mandatory 1:1 swap at demand sites.
                    # Use site-local kg lookup to avoid bay_id collision across sites.
                    if truck_full:
                        _site_bay_kg_map = {b.bay_id: b.kg for b in site.bays}
                        _site_candidates = sorted(
                            sb["empty"] + sb["partial"],
                            key=lambda bid: _site_bay_kg_map.get(bid, 0.0),
                        )
                        num_swaps = min(
                            len(truck_full),
                            len(_site_candidates),
                            truck.capacity - len(truck_empty),
                        )
                        _truck_sorted = sorted(truck_full, key=_bay_kg, reverse=True)
                        for i in range(num_swaps):
                            truck_bid = _truck_sorted[i]
                            site_bid = _site_candidates[i]
                            dropped.append(truck_bid)
                            truck_full.remove(truck_bid)
                            picked.append(site_bid)
                            truck_empty.append(site_bid)
                            if site_bid in sb["empty"]:
                                sb["empty"].remove(site_bid)
                            else:
                                sb["partial"].remove(site_bid)

                if dropped or picked:
                    stop.swap_operation = SwapOperation(
                        site_id=stop.site_id,
                        containers_dropped=dropped,
                        containers_picked=picked,
                    )

                stop.truck_load_after = len(truck_full) + len(truck_empty)
                stop.load_full_after = len(truck_full)
                stop.load_empty_after = len(truck_empty)

                # [FlowState] per-stop flow log
                _pickup = len(picked)
                _delivery = len(dropped)
                _has_op = _pickup > 0 or _delivery > 0
                logger.info(
                    "[FlowState] node=%s  before: F=%d E=%d  "
                    "pickup=%d delivery=%d  after: F=%d E=%d  "
                    "service_time=%.3fh  pass_through=%s",
                    stop.site_id,
                    stop.load_full_after - _pickup + _delivery,  # full_before
                    stop.load_empty_after - _delivery + _pickup,  # empty_before
                    _pickup,
                    _delivery,
                    stop.load_full_after,
                    stop.load_empty_after,
                    stop.service_time_hours,
                    not _has_op,
                )

                # Validate no containers created or destroyed
                if _has_op:
                    assert _pickup >= 0 and _delivery >= 0, (
                        f"[FLOW ERROR] Negative containers at node {stop.site_id}"
                    )

            # [SanityCheck] total service time sanity
            _total_svc_h = sum(s.service_time_hours for s in route.stops)
            _ops_stops = sum(
                1 for s in route.stops
                if s.swap_operation and (
                    s.swap_operation.containers_dropped or s.swap_operation.containers_picked
                )
            )
            _max_svc_h = _ops_stops * (truck.capacity if truck else 3) * self.config.swap_time_hours
            if _total_svc_h > _max_svc_h + 0.001:
                logger.warning(
                    "[SANITY ERROR] truck=%s total_service_time=%.2fh > expected_max=%.2fh"
                    " (ops_stops=%d) — possible double counting",
                    route.truck_id, _total_svc_h, _max_svc_h, _ops_stops,
                )

        return routes

    def _assign_swap_operations_relaxed(
        self,
        routes: List[Route],
        demand_sites: List[str],
        warnings: List[str],
        seed_inventory: Optional[Dict[str, Dict[str, List[str]]]] = None,
    ) -> List[Route]:
        """
        Relaxed swap assignment — best-effort when strict rules produce no actionable routes.

        Relaxations applied (in order):
        L1: Allow ending with non-zero load (warns END_LOAD_NOT_ZERO).
        L2: Allow picking up empties at consumers without delivering fulls,
            if a producer visit follows later in the route (warns TEMP_EMPTY_PICKUP).
        L3: Allow deadhead legs (travel empty) as penalty (warns DEADHEAD).
        """
        inv = copy.deepcopy(seed_inventory) if seed_inventory is not None else self._build_site_bay_inventory()

        # bay_id → Bay lookup for kg-aware decisions (same helper as strict path)
        bay_lookup_r: Dict[str, "Bay"] = {  # type: ignore[name-defined]
            b.bay_id: b
            for site in self.sites.values()
            for b in site.bays
        }

        def _bay_kg_r(bay_id: str) -> float:
            b = bay_lookup_r.get(bay_id)
            if b is not None:
                return b.kg
            return 2829.0

        for route in routes:
            truck = self.trucks.get(route.truck_id)
            if not truck:
                continue

            truck_full: List[str] = []
            truck_empty: List[str] = []

            # Pre-load initial_load containers (same logic as strict path).
            _il_r = truck.initial_load if truck else 0
            if _il_r > 0 and route.stops:
                _start_sid_r  = route.stops[0].site_id
                _sb_start_r   = inv.get(_start_sid_r, {"full": [], "partial": [], "empty": []})
                _start_site_r = self.sites.get(_start_sid_r)
                for _ in range(_il_r):
                    if _start_site_r and _start_site_r.is_producer and _sb_start_r["full"]:
                        truck_full.append(_sb_start_r["full"].pop(0))
                    else:
                        # P0-2: __transit injection forbidden — no real bay available
                        logger.error(
                            "[PHYSICS-P0-2] Truck %s has initial_load=%d but no real bay"
                            " at start site %r — cannot inject __transit container;"
                            " treating remaining initial load as 0",
                            route.truck_id, _il_r, _start_sid_r,
                        )
                        break

            # Pre-scan: which stop indices are producers? (for L2 look-ahead)
            producer_indices = set()
            for idx, stop in enumerate(route.stops):
                site = self.sites.get(stop.site_id)
                if site and site.is_producer:
                    producer_indices.add(idx)

            for stop_idx, stop in enumerate(route.stops):
                site = self.sites.get(stop.site_id)
                if not site:
                    continue

                # Stop 0 is the start depot — no swap operation.
                # Stop 0 is the start depot. Truck starts with whatever initial_load
                # was explicitly set — no auto-pickup. initial_load=0 → truck starts EMPTY.
                if stop.sequence == 0:
                    start_picked_r: List[str] = []
                    if site.is_producer and site.swap_allowed:
                        _start_inv_r = inv.get(stop.site_id, {"full": [], "partial": [], "empty": []})
                        _future_consumers_r = [
                            s for s in route.stops
                            if s.sequence > stop.sequence
                            and s.site_id in demand_sites
                            and self.sites.get(s.site_id)
                            and self.sites[s.site_id].is_consumer
                        ]
                        if _future_consumers_r:
                            _avail_r = truck.capacity - len(truck_full) - len(truck_empty)
                            if _avail_r > 0:
                                _future_swap_capacity_r = 0
                                for _fs in _future_consumers_r:
                                    _finv_r = inv.get(_fs.site_id, {"full": [], "partial": [], "empty": []})
                                    _future_swap_capacity_r += len(_finv_r["empty"]) + len(_finv_r["partial"])
                                _pickup_cap_r = max(0, _future_swap_capacity_r - len(truck_full))
                                _avail_r = min(_avail_r, _pickup_cap_r)
                            if _avail_r > 0:
                                _site_pressure_map_r = {b.bay_id: b.pressure_bar for b in site.bays}
                                _pickable_r = [
                                    bid for bid in (_start_inv_r["full"] + _start_inv_r["partial"])
                                    if _site_pressure_map_r.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
                                ]
                                _pickable_sorted_r = sorted(
                                    _pickable_r,
                                    key=lambda bid: _site_pressure_map_r.get(bid, 0),
                                    reverse=True,
                                )
                                for bay_id in _pickable_sorted_r[:_avail_r]:
                                    start_picked_r.append(bay_id)
                                    truck_full.append(bay_id)
                                    if bay_id in _start_inv_r["full"]:
                                        _start_inv_r["full"].remove(bay_id)
                                    else:
                                        _start_inv_r["partial"].remove(bay_id)
                    stop.service_time_hours = self.config.swap_time_hours if start_picked_r else 0
                    stop.swap_operation = (
                        SwapOperation(
                            site_id=stop.site_id,
                            containers_dropped=[],
                            containers_picked=start_picked_r,
                        )
                        if start_picked_r else None
                    )
                    stop.truck_load_after = len(truck_full) + len(truck_empty)
                    stop.load_full_after = len(truck_full)
                    stop.load_empty_after = len(truck_empty)
                    continue

                sb = inv.get(stop.site_id, {"full": [], "partial": [], "empty": []})
                stop.swap_operation = None  # clear any VRP solver placeholder
                dropped: List[str] = []
                picked: List[str] = []

                _is_last_stop_r = (stop.sequence == len(route.stops) - 1)

                if site.is_producer and site.swap_allowed:
                    # Drop all empties back to producer.
                    for bay_id in list(truck_empty):
                        dropped.append(bay_id)
                        truck_empty.remove(bay_id)
                    # Same guards as strict path: no last stop, no Malmi return, future demand.
                    _is_malmi_return_r = (
                        stop.site_id == "helsinki_malmi" and len(truck_full) > 0
                    )
                    _future_demand_r = any(
                        s.site_id in demand_sites
                        for s in route.stops
                        if s.sequence > stop.sequence
                    )
                    if not _is_last_stop_r and not _is_malmi_return_r and _future_demand_r:
                        avail = truck.capacity - len(truck_full) - len(truck_empty)
                        if avail > 0:
                            # Avoid over-picking in relaxed mode as well.
                            future_swap_capacity_r = 0
                            for _fs in route.stops:
                                if _fs.sequence <= stop.sequence:
                                    continue
                                _fsite = self.sites.get(_fs.site_id)
                                if not _fsite or not _fsite.is_consumer or not _fsite.swap_allowed:
                                    continue
                                if _fs.site_id not in demand_sites:
                                    continue
                                _finv = inv.get(_fs.site_id, {"full": [], "partial": [], "empty": []})
                                future_swap_capacity_r += len(_finv["empty"]) + len(_finv["partial"])
                            pickup_cap_r = max(0, future_swap_capacity_r - len(truck_full))
                            avail = min(avail, pickup_cap_r)
                        if avail > 0:
                            _site_pressure_map_r = {b.bay_id: b.pressure_bar for b in site.bays}
                            _pickable_r = [
                                bid for bid in (sb["full"] + sb["partial"])
                                if _site_pressure_map_r.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
                            ]
                            _pickable_sorted_r = sorted(
                                _pickable_r,
                                key=lambda bid: _site_pressure_map_r.get(bid, 0),
                                reverse=True,
                            )
                            for bay_id in _pickable_sorted_r[:avail]:
                                picked.append(bay_id)
                                truck_full.append(bay_id)
                                if bay_id in sb["full"]:
                                    sb["full"].remove(bay_id)
                                else:
                                    sb["partial"].remove(bay_id)

                elif site.is_consumer and stop.site_id in demand_sites and site.swap_allowed:
                    # Mandatory 1:1 swap at demand sites.
                    # Use site-local kg lookup to avoid bay_id collision across sites.
                    if truck_full:
                        _site_bay_kg_map_r = {b.bay_id: b.kg for b in site.bays}
                        _site_candidates_r = sorted(
                            sb["empty"] + sb["partial"],
                            key=lambda bid: _site_bay_kg_map_r.get(bid, 0.0),
                        )
                        num_swaps_r = min(
                            len(truck_full),
                            len(_site_candidates_r),
                            truck.capacity - len(truck_empty),
                        )
                        _truck_sorted_r = sorted(truck_full, key=_bay_kg_r, reverse=True)
                        for i in range(num_swaps_r):
                            truck_bid = _truck_sorted_r[i]
                            site_bid = _site_candidates_r[i]
                            dropped.append(truck_bid)
                            truck_full.remove(truck_bid)
                            picked.append(site_bid)
                            truck_empty.append(site_bid)
                            if site_bid in sb["empty"]:
                                sb["empty"].remove(site_bid)
                            else:
                                sb["partial"].remove(site_bid)
                    else:
                        # L2: No fulls — pick up empties if a producer follows later
                        has_later_producer = any(i > stop_idx for i in producer_indices)
                        if has_later_producer:
                            empties = list(sb["empty"] + sb["partial"])
                            avail = truck.capacity - len(truck_full) - len(truck_empty)
                            pickup_count = min(len(empties), avail)
                            if pickup_count > 0:
                                for bay_id in empties[:pickup_count]:
                                    picked.append(bay_id)
                                    truck_empty.append(bay_id)
                                    if bay_id in sb["empty"]:
                                        sb["empty"].remove(bay_id)
                                    else:
                                        sb["partial"].remove(bay_id)
                                warnings.append(
                                    f"[TEMP_EMPTY_PICKUP] {route.truck_id} picks up {pickup_count} "
                                    f"empty bay(s) at {site.name} without delivering fulls "
                                    f"(will rebalance at next producer)"
                                )

                if dropped or picked:
                    stop.swap_operation = SwapOperation(
                        site_id=stop.site_id,
                        containers_dropped=dropped,
                        containers_picked=picked,
                    )

                stop.truck_load_after = len(truck_full) + len(truck_empty)
                stop.load_full_after = len(truck_full)
                stop.load_empty_after = len(truck_empty)

            # L1: Check end-of-route load
            end_load = len(truck_full) + len(truck_empty)
            if end_load > 0:
                warnings.append(
                    f"[END_LOAD_NOT_ZERO] {route.truck_id} ends with {len(truck_full)} full + "
                    f"{len(truck_empty)} empty container(s) still on truck"
                )

        return routes

    def _extend_routes_multi_trip(
        self,
        routes: List[Route],
        demand_sites: List[str],
        day: int,
        hours_to_critical_map: Optional[Dict[str, float]] = None,
    ) -> List[Route]:
        """
        Greedy multi-trip extension for underutilized trucks.

        After the primary swap assignment, trucks that have used < 60% of their
        time budget and have remaining time > 2h are extended with additional
        producer → consumer legs, provided:
          - There are unserved demand consumer sites with available containers
          - There are producer sites with full containers available for pickup
          - The round-trip fits within the remaining time budget

        Extended stops are appended to the existing route with sequence numbers
        continuing from the last stop.  Swap operations are assigned inline using
        the same pick/drop logic as the main assignment.
        """
        _max_h = self.config.max_driver_hours
        _svc_h = self.config.swap_time_hours
        _speed  = self.config.avg_speed_kmph
        _demand_set = set(demand_sites)
        _dist_matrix = getattr(self, 'distance_matrix', None) or {}

        def _dist_km(a: str, b: str) -> float:
            """Lookup road distance km between two site IDs; fallback to haversine."""
            row = _dist_matrix.get(a, {})
            d = row.get(b)
            if d and d > 0:
                return d
            sa, sb = self.sites.get(a), self.sites.get(b)
            if sa and sb and getattr(sa, 'latitude', None) and getattr(sb, 'latitude', None):
                dlat = math.radians(sb.latitude - sa.latitude)
                dlon = math.radians(sb.longitude - sa.longitude)
                ha = math.sin(dlat / 2) ** 2 + math.cos(math.radians(sa.latitude)) * math.cos(math.radians(sb.latitude)) * math.sin(dlon / 2) ** 2
                return math.asin(math.sqrt(ha)) * 2 * 6371.0 * 1.3  # road factor
            return 999.0

        def _travel_h(a: str, b: str) -> float:
            d = _dist_km(a, b)
            return d / max(_speed, 1.0) if d < 900 else 999.0

        # Build current site bay inventory from scratch
        inv = self._build_site_bay_inventory()
        # Subtract containers already assigned in existing routes
        for route in routes:
            for stop in route.stops:
                sw = stop.swap_operation
                if not sw:
                    continue
                sb = inv.setdefault(stop.site_id, {"full": [], "partial": [], "empty": []})
                for bid in sw.containers_picked:
                    for bucket in ("full", "partial", "empty"):
                        if bid in sb[bucket]:
                            sb[bucket].remove(bid)
                            break

        # Find served consumer sites
        served_consumers = {
            stop.site_id
            for route in routes
            for stop in route.stops
            if stop.swap_operation and stop.swap_operation.containers_dropped
        }

        # All unserved demand consumers that still have empty bays for swap
        unserved_consumers = [
            sid for sid in _demand_set
            if sid not in served_consumers
            and self.sites.get(sid)
            and self.sites[sid].is_consumer
            and self.sites[sid].swap_allowed
            and inv.get(sid, {}).get("empty", []) + inv.get(sid, {}).get("partial", [])
        ]

        if not unserved_consumers:
            return routes  # Nothing to extend

        # Producer sites that still have full containers
        available_producers = [
            sid for sid, site in self.sites.items()
            if site.is_producer and site.swap_allowed
            and inv.get(sid, {}).get("full", []) + inv.get(sid, {}).get("partial", [])
        ]

        extended_count = 0
        for route in routes:
            truck = self.trucks.get(route.truck_id)
            if not truck:
                continue

            time_used_h = route.total_time_hours
            remaining_h = _max_h - time_used_h

            # Only extend trucks with meaningful remaining time (> 2h)
            if remaining_h < 2.0:
                continue
            if time_used_h / _max_h > 0.60:
                continue  # Already well-utilized

            # Current truck position = last stop
            last_stop = route.stops[-1] if route.stops else None
            if not last_stop:
                continue
            cur_pos = last_stop.site_id
            truck_capacity = truck.capacity

            # Greedy loop: keep extending while time allows
            trip_count = 0
            while remaining_h >= 2.0 and unserved_consumers and available_producers:
                # Find best producer→consumer pair within time budget
                best = None
                best_score = 999.0
                for prod_id in available_producers:
                    t_to_prod = _travel_h(cur_pos, prod_id) + _svc_h
                    if t_to_prod >= remaining_h:
                        continue
                    for cons_id in unserved_consumers:
                        t_prod_to_cons = _travel_h(prod_id, cons_id) + _svc_h
                        total_t = t_to_prod + t_prod_to_cons
                        if total_t >= remaining_h:
                            continue
                        # Score: prefer most urgent consumer (lowest htc → served first)
                        site_htc = (hours_to_critical_map or {}).get(cons_id, 999.0)
                        score = site_htc + total_t
                        if score < best_score:
                            best_score = score
                            best = (prod_id, cons_id, t_to_prod, t_prod_to_cons)

                if not best:
                    break

                prod_id, cons_id, t_to_prod, t_to_cons = best
                prod_site = self.sites.get(prod_id)
                cons_site = self.sites.get(cons_id)
                if not prod_site or not cons_site:
                    break

                # Pick up one full container from producer
                prod_inv = inv.setdefault(prod_id, {"full": [], "partial": [], "empty": []})
                _pmap = {b.bay_id: b.pressure_bar for b in prod_site.bays}
                _pickable = [
                    bid for bid in (prod_inv["full"] + prod_inv["partial"])
                    if _pmap.get(bid, 0) >= self.PRODUCER_PICKUP_MIN_BAR
                ]
                if not _pickable:
                    available_producers.remove(prod_id)
                    continue
                pick_bay = max(_pickable, key=lambda bid: _pmap.get(bid, 0))
                for bucket in ("full", "partial"):
                    if pick_bay in prod_inv[bucket]:
                        prod_inv[bucket].remove(pick_bay)
                        break

                # Drop one empty container at consumer
                cons_inv = inv.setdefault(cons_id, {"full": [], "partial": [], "empty": []})
                _cmap = {b.bay_id: b.kg for b in cons_site.bays}
                _empty_cands = sorted(cons_inv["empty"] + cons_inv["partial"], key=lambda bid: _cmap.get(bid, 0))
                if not _empty_cands:
                    unserved_consumers.remove(cons_id)
                    continue
                drop_bay = _empty_cands[0]
                for bucket in ("empty", "partial"):
                    if drop_bay in cons_inv[bucket]:
                        cons_inv[bucket].remove(drop_bay)
                        break

                # Calculate cumulative distance up to this point
                prev_dist = last_stop.cumulative_distance_km if last_stop else 0.0
                prod_d = _dist_km(cur_pos, prod_id)
                cons_d = _dist_km(prod_id, cons_id)

                # Append producer stop
                seq_base = last_stop.sequence + 1 if last_stop else len(route.stops)
                prod_stop = RouteStop(
                    sequence=seq_base,
                    site_id=prod_id,
                    site_name=prod_site.name,
                    arrival_time_hours=time_used_h + t_to_prod - _svc_h,
                    distance_from_previous_km=prod_d,
                    cumulative_distance_km=prev_dist + prod_d,
                    service_time_hours=_svc_h,
                    truck_load_after=1,
                    load_full_after=1,
                    load_empty_after=0,
                    swap_operation=SwapOperation(
                        site_id=prod_id,
                        containers_picked=[pick_bay],
                        containers_dropped=[],
                    ),
                )
                route.stops.append(prod_stop)

                # Append consumer stop
                cons_arrival_h = time_used_h + t_to_prod + t_to_cons - _svc_h
                cons_stop = RouteStop(
                    sequence=seq_base + 1,
                    site_id=cons_id,
                    site_name=cons_site.name,
                    arrival_time_hours=cons_arrival_h,
                    distance_from_previous_km=cons_d,
                    cumulative_distance_km=prev_dist + prod_d + cons_d,
                    service_time_hours=_svc_h,
                    truck_load_after=0,
                    load_full_after=0,
                    load_empty_after=1,
                    swap_operation=SwapOperation(
                        site_id=cons_id,
                        containers_picked=[drop_bay],
                        containers_dropped=[pick_bay],
                    ),
                )
                route.stops.append(cons_stop)

                # Update state
                time_used_h += t_to_prod + t_to_cons
                remaining_h = _max_h - time_used_h
                cur_pos = cons_id
                last_stop = cons_stop
                trip_count += 1
                extended_count += 1
                served_consumers.add(cons_id)
                if cons_id in unserved_consumers:
                    unserved_consumers.remove(cons_id)
                if not (prod_inv["full"] + prod_inv["partial"]):
                    if prod_id in available_producers:
                        available_producers.remove(prod_id)

            if trip_count > 0:
                print(
                    f"[MultiTrip] truck={route.truck_id}"
                    f" extended with {trip_count} extra producer→consumer leg(s)"
                    f" remaining_time={remaining_h:.1f}h"
                )

        if extended_count > 0:
            print(f"[MultiTrip] day={day} total extra container moves={extended_count}")

        return routes

    def _close_routes_to_empty(
        self,
        routes: List[Route],
        demand_sites: List[str],
        hours_to_critical_map: Optional[Dict[str, float]] = None,
    ) -> List[Route]:
        """Use remaining time to finish routes with zero load when possible.

        Operational preference:
        1. If a truck ends with full containers, deliver them to a consumer that
           still has a removable empty/partial bay.
        2. If a truck ends with empty containers, return them to a producer.

        This is a post-assignment heuristic. It keeps the current solve, but
        tries to "close the loop" before the end of the day so plans look more
        like what an operator would actually approve.
        """
        if not routes:
            return routes

        avg_speed = max(self.config.avg_speed_kmph, 1.0)
        demand_set = set(demand_sites)
        hours_map = hours_to_critical_map or {}
        served_demand_sites = {
            stop.site_id
            for route in routes
            for stop in route.stops
            if stop.site_id in demand_set
            and stop.swap_operation
            and (
                stop.swap_operation.containers_dropped
                or stop.swap_operation.containers_picked
            )
        }
        unresolved_high_priority = {
            sid for sid in demand_set
            if sid not in served_demand_sites
            and hours_map.get(sid, float("inf")) < float(self.config.warning_hours_threshold)
        }
        bay_lookup = {
            b.bay_id: b
            for site in self.sites.values()
            for b in site.bays
        }

        def _bay_kg(bay_id: str) -> float:
            bay = bay_lookup.get(bay_id)
            return bay.kg if bay is not None else 2829.0

        def _site_inventory_after_plan() -> Dict[str, Dict[str, List[str]]]:
            inv = self._build_site_bay_inventory()
            for route in routes:
                for stop in route.stops:
                    sw = stop.swap_operation
                    if not sw:
                        continue
                    sb = inv.setdefault(stop.site_id, {"full": [], "partial": [], "empty": []})
                    for bid in sw.containers_picked:
                        for bucket in ("full", "partial", "empty"):
                            if bid in sb[bucket]:
                                sb[bucket].remove(bid)
                                break
            return inv

        inv = _site_inventory_after_plan()

        for route in routes:
            truck = self.trucks.get(route.truck_id)
            if not truck or not route.stops:
                continue

            truck_full: List[str] = []
            truck_empty: List[str] = []
            for stop in route.stops:
                sw = stop.swap_operation
                if not sw:
                    continue
                stop_site = self.sites.get(stop.site_id)
                for bid in sw.containers_dropped:
                    if bid in truck_full:
                        truck_full.remove(bid)
                    elif bid in truck_empty:
                        truck_empty.remove(bid)
                for bid in sw.containers_picked:
                    if stop_site and stop_site.is_consumer:
                        truck_empty.append(bid)
                    else:
                        truck_full.append(bid)

            if not truck_full and not truck_empty:
                continue

            remaining_h = self.config.max_driver_hours - route.total_time_hours
            if remaining_h <= self.config.swap_time_hours:
                continue

            current_site_id = route.stops[-1].site_id

            while remaining_h > self.config.swap_time_hours and (truck_full or truck_empty):
                progressed = False

                if truck_full:
                    consumer_candidates = []
                    for sid, site in self.sites.items():
                        if not site.is_consumer or not site.swap_allowed:
                            continue
                        if unresolved_high_priority and sid not in unresolved_high_priority:
                            continue
                        sb = inv.get(sid, {"full": [], "partial": [], "empty": []})
                        removable = list(sb["empty"] + sb["partial"])
                        if not removable:
                            continue
                        dist = self._distance_between_sites(current_site_id, sid)
                        if not math.isfinite(dist):
                            continue
                        travel_h = dist / avg_speed
                        total_h = travel_h + self.config.swap_time_hours
                        if total_h > remaining_h:
                            continue
                        consumer_candidates.append((
                            0 if sid in demand_set else 1,
                            hours_map.get(sid, float("inf")),
                            dist,
                            -len(removable),
                            sid,
                        ))

                    consumer_candidates.sort()
                    if consumer_candidates:
                        chosen_sid = consumer_candidates[0][-1]
                        chosen_site = self.sites[chosen_sid]
                        self._append_stop_to_route(route, chosen_sid, chosen_site.name, avg_speed)
                        stop = route.stops[-1]
                        sb = inv.setdefault(chosen_sid, {"full": [], "partial": [], "empty": []})
                        site_candidates = sorted(
                            sb["empty"] + sb["partial"],
                            key=lambda bid: _bay_kg(bid),
                        )
                        truck_sorted = sorted(truck_full, key=_bay_kg, reverse=True)
                        swap_count = min(len(truck_sorted), len(site_candidates))
                        dropped = truck_sorted[:swap_count]
                        picked = site_candidates[:swap_count]
                        for bid in dropped:
                            truck_full.remove(bid)
                        for bid in picked:
                            truck_empty.append(bid)
                            if bid in sb["empty"]:
                                sb["empty"].remove(bid)
                            else:
                                sb["partial"].remove(bid)
                        stop.service_time_hours = self.config.swap_time_hours if swap_count > 0 else 0.0
                        stop.swap_operation = (
                            SwapOperation(
                                site_id=chosen_sid,
                                containers_dropped=dropped,
                                containers_picked=picked,
                            )
                            if swap_count > 0 else None
                        )
                        stop.truck_load_after = len(truck_full) + len(truck_empty)
                        stop.load_full_after = len(truck_full)
                        stop.load_empty_after = len(truck_empty)
                        remaining_h -= (
                            stop.distance_from_previous_km / avg_speed
                            + stop.service_time_hours
                        )
                        current_site_id = chosen_sid
                        progressed = swap_count > 0

                if truck_empty and remaining_h > self.config.swap_time_hours:
                    producer_candidates = []
                    preferred_end = truck.effective_start_site_id or truck.home_site_id
                    for sid, site in self.sites.items():
                        if not site.is_producer or not site.swap_allowed:
                            continue
                        dist = self._distance_between_sites(current_site_id, sid)
                        if not math.isfinite(dist):
                            continue
                        travel_h = dist / avg_speed
                        total_h = travel_h + self.config.swap_time_hours
                        if total_h > remaining_h:
                            continue
                        producer_candidates.append((
                            0 if sid == preferred_end else 1,
                            dist,
                            sid,
                        ))
                    producer_candidates.sort()
                    if producer_candidates:
                        chosen_sid = producer_candidates[0][-1]
                        chosen_site = self.sites[chosen_sid]
                        self._append_stop_to_route(route, chosen_sid, chosen_site.name, avg_speed)
                        stop = route.stops[-1]
                        dropped = list(truck_empty)
                        truck_empty.clear()
                        stop.service_time_hours = self.config.swap_time_hours if dropped else 0.0
                        stop.swap_operation = (
                            SwapOperation(
                                site_id=chosen_sid,
                                containers_dropped=dropped,
                                containers_picked=[],
                            )
                            if dropped else None
                        )
                        stop.truck_load_after = len(truck_full) + len(truck_empty)
                        stop.load_full_after = len(truck_full)
                        stop.load_empty_after = len(truck_empty)
                        remaining_h -= (
                            stop.distance_from_previous_km / avg_speed
                            + stop.service_time_hours
                        )
                        current_site_id = chosen_sid
                        progressed = True

                if not progressed:
                    break

        return routes

    def _assign_swap_operations_minimal(
        self,
        routes: List[Route],
        demand_sites: List[str],
        warnings: List[str],
    ) -> List[Route]:
        """
        Last-resort swap assignment when both strict and relaxed paths fail.

        Rules:
        - Producer stop: pick available full/partial bays without pressure threshold.
          No synthetic injection — if inventory is empty, skip pickup.
        - Consumer stop (in demand_sites): drop exactly 1 full container ONLY if:
            (a) truck_full > 0, AND
            (b) site has at least one empty or partial bay (physical slot available).
          No pickup required, no kg matching.
        - Goal: at least one valid delivery per route, physically consistent.
        """
        inv = self._build_site_bay_inventory()

        for route in routes:
            truck = self.trucks.get(route.truck_id)
            if not truck:
                continue

            truck_full: List[str] = []
            truck_empty: List[str] = []

            for stop in route.stops:
                site = self.sites.get(stop.site_id)
                if not site:
                    continue

                sb = inv.get(stop.site_id, {"full": [], "partial": [], "empty": []})
                stop.swap_operation = None
                dropped: List[str] = []
                picked: List[str] = []

                if site.is_producer:
                    # Return empties
                    for bay_id in list(truck_empty):
                        dropped.append(bay_id)
                        truck_empty.remove(bay_id)
                    # Pick up fulls — no pressure threshold, no synthetic injection
                    avail = truck.capacity - len(truck_full) - len(truck_empty)
                    for bay_id in (sb["full"] + sb["partial"])[:avail]:
                        picked.append(bay_id)
                        truck_full.append(bay_id)
                        if bay_id in sb["full"]:
                            sb["full"].remove(bay_id)
                        else:
                            sb["partial"].remove(bay_id)

                elif site.is_consumer and stop.site_id in demand_sites:
                    # Drop 1 full only if truck is loaded AND site has a physical slot
                    _has_slot = len(sb["empty"]) > 0 or len(sb["partial"]) > 0
                    if truck_full and _has_slot:
                        bay_id = truck_full.pop(0)
                        dropped.append(bay_id)

                # Depot stop: no swap_operation even if containers were pre-staged
                if stop.sequence == 0:
                    stop.service_time_hours = 0
                    stop.swap_operation = None
                else:
                    if dropped or picked:
                        stop.swap_operation = SwapOperation(
                            site_id=stop.site_id,
                            containers_dropped=dropped,
                            containers_picked=picked,
                        )

                stop.truck_load_after = len(truck_full) + len(truck_empty)
                stop.load_full_after = len(truck_full)
                stop.load_empty_after = len(truck_empty)

        warnings.append(
            "[MINIMAL_SWAP_FALLBACK] strict and relaxed assignment both failed;"
            " delivery-only swaps applied"
        )
        return routes

    @staticmethod
    def _determine_feasibility_level(
        routes: List[Route], warnings: List[str]
    ) -> str:
        """Determine feasibility level from warning codes."""
        codes = {w.split("]")[0].lstrip("[") for w in warnings if w.startswith("[")}
        if "TEMP_EMPTY_PICKUP" in codes:
            return "L2"
        if "END_LOAD_NOT_ZERO" in codes:
            return "L1"
        return "STRICT"

    def _build_recommendation(
        self,
        routes: List[Route],
        objective: ObjectiveFunction,
        demand_sites: List[str],
        horizon_days: int = 1,
        fleet_config: Optional[List[dict]] = None,
        cost_per_km_override: Optional[float] = None,
    ) -> Recommendation:
        """Build a complete recommendation from routes."""
        # Calculate totals
        total_distance = sum(r.total_distance_km for r in routes)
        total_stops = sum(r.num_stops for r in routes)

        # Cost breakdown: apply minimum billed distance per route (50 km minimum)
        cost_per_km = cost_per_km_override if cost_per_km_override is not None else self.config.cost_per_km_eur
        transport_cost = sum(
            max(r.total_distance_km, self.config.min_billed_km) * cost_per_km
            for r in routes
        )
        handling_cost = total_stops * self.config.handling_fee_eur
        total_cost = (transport_cost + handling_cost) * self.config.contingency_multiplier

        # Calculate MWh moved (energy delivered to consumers)
        total_mwh_moved, energy_moved_debug = self._calculate_mwh_moved(routes)
        eur_per_mwh = None
        if total_mwh_moved > 0:
            eur_per_mwh = total_cost / total_mwh_moved

        # Risk metrics
        assessments = self.risk_calculator.assess_all_sites(self.sites)

        sites_served = len(set(
            stop.site_id for route in routes
            for stop in route.stops
            if stop.swap_operation and (
                stop.swap_operation.containers_dropped or
                stop.swap_operation.containers_picked
            )
        ))

        critical_sites_in_routes = len([
            a for a in assessments
            if a.risk_level == RiskLevel.CRITICAL and a.site_id in demand_sites
        ])

        # Estimate risk reduction (simplified)
        risk_reduction = min(100, (sites_served / max(1, len(demand_sites))) * 100)

        # Compute solution risk score (0–10)
        solution_risk_score = self._compute_solution_risk_score(assessments, demand_sites, routes)

        # Flaring exposure and end-of-horizon imbalance for dashboard
        served_ids_for_flaring = {
            stop.site_id for route in routes for stop in route.stops
            if stop.swap_operation and (
                stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
            )
        }
        flaring_info = self._compute_flaring_exposure(assessments, served_ids_for_flaring)
        flaring_exposure_hours = flaring_info["total_h"] if flaring_info["total_h"] > 0 else None
        end_of_horizon_imbalance = self._compute_end_of_horizon_imbalance(routes)

        # [BALANCE] Balance is enforced inside the solver via the ContainerBalance
        # dimension soft bounds (_BALANCE_PENALTY_STRONG).  Do NOT add it again here
        # — double-counting causes cost explosion (225 000 EUR × imbalance per run).
        logger.info("[BALANCE] imbalance=%d", end_of_horizon_imbalance)

        # ── Post-solve sanity checks (warnings only, never raise) ───────────────
        # 1. Cost sanity: flag suspiciously large total cost
        _max_reasonable_cost = (
            len(routes) * self.config.max_driver_hours * 80.0 * self.config.cost_per_km_eur
            * self.config.contingency_multiplier
        )
        if total_cost > _max_reasonable_cost:
            logger.warning(
                "[SANITY] total_cost_eur=%.0f exceeds reasonable max %.0f EUR "
                "(routes=%d, max_driver_hours=%.1f, cost_per_km=%.2f). "
                "Check for double-penalty or cost-scale bugs.",
                total_cost, _max_reasonable_cost, len(routes),
                self.config.max_driver_hours, self.config.cost_per_km_eur,
            )
        # 2. Balance sanity: warn if optimize-days plan ends imbalanced
        if horizon_days > 1 and end_of_horizon_imbalance > 0:
            logger.warning(
                "[SANITY] optimize_days imbalance=%d — solver Capacity end-bounds "
                "did not fully enforce return. Check _BALANCE_PENALTY_STRONG scaling.",
                end_of_horizon_imbalance,
            )
        # 3. Start-load sanity:
        # stop-0 load_full_after should equal initial_load + explicit producer pickup at start.
        for _route in routes:
            if not _route.stops:
                continue
            _stop0 = _route.stops[0]
            _fleet_truck = next(
                (t for t in self.trucks.values() if t.id == _route.truck_id), None
            )
            if _fleet_truck is not None:
                _expected_il = _fleet_truck.initial_load
                _start_pick = 0
                if _stop0.swap_operation is not None:
                    _s0_site = self.sites.get(_stop0.site_id)
                    if _s0_site and _s0_site.is_producer:
                        _start_pick = len(_stop0.swap_operation.containers_picked or [])
                if _stop0.load_full_after != _expected_il:
                    if _stop0.load_full_after != (_expected_il + _start_pick):
                        logger.warning(
                            "[SANITY] truck=%s stop-0 load_full_after=%d != "
                            "initial_load(%d)+start_pick(%d)",
                            _route.truck_id, _stop0.load_full_after, _expected_il, _start_pick,
                        )

        # Build explanation
        explanation = self._build_explanation(
            routes, objective, total_distance, total_cost,
            sites_served, critical_sites_in_routes, total_mwh_moved, eur_per_mwh,
            assessments=assessments, demand_sites=demand_sites,
            solution_risk_score=solution_risk_score,
        )

        # Build warnings
        warnings = self._build_warnings(routes)

        # Add container balance validation warnings
        _, balance_warnings = self.validate_container_balance(routes)
        warnings.extend(balance_warnings)

        # Compute unreturned containers metric
        unreturned = self._compute_unreturned_containers(routes)
        if end_of_horizon_imbalance > 0:
            warnings.append(
                f"[FINAL_TRUCK_LOAD] Plan ends with {end_of_horizon_imbalance} container(s) still on trucks. "
                "This is permitted, but trucks should ideally finish empty by the end of the horizon."
            )

        # Build container moves enriched with serial numbers
        container_moves = self._build_container_moves(routes)

        return Recommendation(
            status=RecommendationStatus.READY,
            objective_function=objective.value,
            horizon_days=horizon_days,
            routes=routes,
            total_distance_km=total_distance,
            transport_cost_eur=transport_cost,
            handling_cost_eur=handling_cost,
            total_cost_eur=total_cost,
            total_mwh_moved=total_mwh_moved,
            eur_per_mwh=eur_per_mwh,
            energy_moved_debug=energy_moved_debug,
            sites_served=sites_served,
            critical_sites_addressed=critical_sites_in_routes,
            risk_reduction_score=risk_reduction,
            solution_risk_score=solution_risk_score,
            flaring_exposure_hours=flaring_exposure_hours,
            end_of_horizon_imbalance=end_of_horizon_imbalance,
            explanation=explanation,
            warnings=warnings,
            unreturned_containers=unreturned,
            container_moves=container_moves,
            fleet_config=fleet_config,
        )

    def _append_unserved_demand_feedback(
        self,
        recommendation: Recommendation,
        demand_sites: List[str],
        served_site_ids: set[str],
        risk_map: Dict[str, str],
        demand_site_meta: Optional[Dict[str, Dict[str, Any]]] = None,
        selected_trucks: Optional[List[Truck]] = None,
        all_routes: Optional[List[Route]] = None,
    ) -> None:
        """Separate true operational misses from deferred preventive opportunities."""
        demand_site_meta = demand_site_meta or {}
        current_critical = [
            sid for sid in demand_sites
            if sid not in served_site_ids
            and risk_map.get(sid) == "critical"
        ]
        current_warning = [
            sid for sid in demand_sites
            if sid not in served_site_ids
            and risk_map.get(sid) == "warning"
        ]
        preventive_deferred = [
            sid for sid in demand_sites
            if sid not in served_site_ids
            and risk_map.get(sid) not in ("critical", "warning")
        ]

        if current_critical or current_warning:
            missed_names = [
                self.sites[s].name if s in self.sites else s
                for s in (current_critical + current_warning)
            ]
            msg = (
                "Plan leaves current operational risk unresolved: "
                f"{len(current_critical)} critical + {len(current_warning)} warning site(s) "
                f"unserved: {missed_names}. "
                "These sites are already in the active risk window and should be reviewed before approval."
            )
            logger.warning("[RiskUnmitigated] %s", msg)
            recommendation.warnings.insert(0, f"[RISK_UNMITIGATED] {msg}")
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            recommendation.solution_feedback.insert(0, {
                "type": "error" if current_critical else "warning",
                "code": "RISK_UNMITIGATED",
                "message": msg,
            })

        if preventive_deferred:
            deferred_names = [
                self.sites[s].name if s in self.sites else s
                for s in preventive_deferred
            ]
            msg = (
                f"Plan defers {len(preventive_deferred)} preventive site(s) within the planning horizon: "
                f"{deferred_names}. They remain soft candidates for tomorrow-posture improvement, "
                "but they are not yet in the active risk window."
            )
            recommendation.warnings.append(f"[PREVENTIVE_DEFERRED] {msg}")
            if recommendation.solution_feedback is None:
                recommendation.solution_feedback = []
            recommendation.solution_feedback.append({
                "type": "info",
                "code": "PREVENTIVE_DEFERRED",
                "message": msg,
            })

        if current_critical and selected_trucks:
            used_truck_ids = {route.truck_id for route in (all_routes or [])}
            idle_trucks = [truck for truck in selected_trucks if truck.id not in used_truck_ids]
            if idle_trucks:
                msg = (
                    f"[PHYSICS-P0-7] Warning: {len(current_critical)} current critical site(s) "
                    f"remain unserved while {len(idle_trucks)} truck(s) sit idle. "
                    f"Idle: {[t.id for t in idle_trucks]}. "
                    f"Unserved critical: {[self.sites[s].name if s in self.sites else s for s in current_critical]}."
                )
                logger.warning(msg)
                recommendation.warnings.insert(0, f"[IDLE_TRUCK_WITH_CRITICAL_UNSERVED] {msg}")
                if recommendation.solution_feedback is None:
                    recommendation.solution_feedback = []
                recommendation.solution_feedback.insert(0, {
                    "type": "warning",
                    "code": "IDLE_TRUCK_WITH_CRITICAL_UNSERVED",
                    "message": msg,
                })

    def _build_ai_coordinator_input(
        self,
        demand_sites: List[str],
        selected_trucks: List[Truck],
        fleet_config: Optional[List[dict]],
        horizon_days: int,
        optimize_days_mode: bool,
        force_exact_days: bool,
        risk_map: Dict[str, str],
        hours_to_critical_map: Dict[str, float],
    ) -> AICoordinatorInput:
        preferred_hubs = [
            sid for sid, site in self.sites.items()
            if site.name.lower() == "malmi"
        ]
        sites: List[AICoordinatorSiteSnapshot] = []
        for sid in demand_sites:
            site = self.sites.get(sid)
            if not site:
                continue
            sites.append(
                AICoordinatorSiteSnapshot(
                    site_id=sid,
                    site_name=site.name,
                    site_type=site.site_type.value if hasattr(site.site_type, "value") else str(site.site_type),
                    risk_level=risk_map.get(sid, "safe"),
                    hours_to_critical=float(hours_to_critical_map.get(sid, 99999.0)),
                    risk_score=float(self.risk_calculator.assess_site(site).risk_score),
                    projected_unserved_impact_eur=self._estimate_unserved_impact_eur(
                        site=site,
                        hours_to_critical=float(hours_to_critical_map.get(sid, 99999.0)),
                        horizon_days=horizon_days,
                    ),
                    flaring_loss_eur_per_h=float(site.flaring_loss_eur_per_h or 0.0),
                )
            )

        fleet_map = {
            (cfg or {}).get("truck_id"): (cfg or {})
            for cfg in (fleet_config or [])
        }
        trucks = [
            AICoordinatorTruckSnapshot(
                truck_id=truck.id,
                home_site_id=truck.home_site_id,
                capacity=int(truck.capacity or 0),
                availability_days=int(fleet_map.get(truck.id, {}).get("availability_days", horizon_days) or horizon_days),
                force_end_enabled=bool(fleet_map.get(truck.id, {}).get("force_end_enabled")),
                force_end_day=fleet_map.get(truck.id, {}).get("force_end_day"),
            )
            for truck in selected_trucks
        ]

        return AICoordinatorInput(
            mode="optimize_days" if optimize_days_mode else ("force_exact_days" if force_exact_days else "standard"),
            objective_order=["critical_coverage", "future_risk", "end_balance", "cost"],
            horizon_days=horizon_days,
            optimize_days_mode=optimize_days_mode,
            force_exact_days=force_exact_days,
            available_trucks=trucks,
            sites=sites,
            demand_site_ids=list(demand_sites),
            preferred_hubs=preferred_hubs,
            hard_rules=[
                "Never use zero trucks when trucks are available.",
                "If critical demand remains while selected trucks are idle, repair with more active trucks.",
                "Prefer Malmi-style hub circulation when it improves coverage or end balance.",
            ],
        )

    def _apply_ai_priority_bias(
        self,
        risk_score_map: Dict[str, float],
        strategy: AICoordinatorStrategy,
    ) -> Dict[str, float]:
        adjusted = dict(risk_score_map)
        for sid in strategy.future_site_ids:
            if sid in adjusted:
                adjusted[sid] = min(100.0, adjusted[sid] * 1.15)
        for sid in strategy.critical_site_ids:
            if sid in adjusted:
                adjusted[sid] = min(100.0, adjusted[sid] * 1.35)
        return adjusted

    def _merge_ai_preferred_hubs(
        self,
        transfer_sites_list: List[str],
        strategy: AICoordinatorStrategy,
    ) -> List[str]:
        merged = list(transfer_sites_list)
        for sid in strategy.prefer_hubs:
            site = self.sites.get(sid)
            if site and site.is_producer and sid not in merged:
                merged.append(sid)
        return merged

    def _planner_min_active_trucks_for_day(
        self,
        strategy: Optional[AICoordinatorStrategy],
        active_demand: List[str],
        risk_map: Dict[str, str],
        selected_trucks: List[Truck],
        force_exact_days: bool,
    ) -> Optional[int]:
        if force_exact_days or strategy is None or not selected_trucks:
            return None
        critical_today = [sid for sid in active_demand if risk_map.get(sid) == "critical"]
        if not critical_today:
            return None
        target = min(
            len(selected_trucks),
            max(1, min(strategy.min_active_trucks, len(critical_today))),
        )
        return target if target > 1 else None

    def _estimate_unserved_impact_eur(
        self,
        site: Site,
        hours_to_critical: float,
        horizon_days: int,
    ) -> float:
        horizon_h = max(1.0, float(horizon_days) * 24.0)
        exposure_window_h = max(0.0, horizon_h - max(0.0, hours_to_critical))
        if site.is_producer:
            hourly_impact = float(site.flaring_loss_eur_per_h or 0.0)
            return round(hourly_impact * max(1.0, exposure_window_h), 2)
        if site.name == "Takkula":
            hourly_impact = 1_000_000.0 / 24.0
        elif hours_to_critical <= 5.0:
            hourly_impact = 5_000.0
        else:
            hourly_impact = 1_000.0
        return round(hourly_impact * max(1.0, exposure_window_h), 2)

    def _build_ai_candidate_summary(
        self,
        recommendation: Recommendation,
        demand_sites: List[str],
        risk_map: Dict[str, str],
        hours_to_critical_map: Dict[str, float],
        selected_trucks: List[Truck],
        horizon_days: int,
        ) -> AICandidateSummary:
        served_site_ids = {
            stop.site_id
            for route in recommendation.routes
            for stop in route.stops
            if stop.swap_operation and (
                stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
            )
        }
        critical_unserved = 0
        critical_impact = 0.0
        future_unserved = 0
        future_impact = 0.0
        active_truck_days = 0
        short_active_days = 0
        underused_drive_hours = 0.0
        for sid in demand_sites:
            if sid in served_site_ids:
                continue
            site = self.sites.get(sid)
            if not site:
                continue
            impact = self._estimate_unserved_impact_eur(
                site=site,
                hours_to_critical=float(hours_to_critical_map.get(sid, 99999.0)),
                horizon_days=horizon_days,
            )
            if risk_map.get(sid) == "critical":
                critical_unserved += 1
                critical_impact += impact
            else:
                future_unserved += 1
                future_impact += impact
        for route in recommendation.routes:
            route_hours = float(getattr(route, "total_time_hours", 0.0) or 0.0)
            if route_hours <= 0:
                continue
            active_truck_days += 1
            unused_h = max(0.0, float(self.config.max_driver_hours) - route_hours)
            underused_drive_hours += unused_h
            if route_hours < 7.5:
                short_active_days += 1
        used_trucks = len({route.truck_id for route in recommendation.routes})
        idle_trucks = max(0, len(selected_trucks) - used_trucks)
        return AICandidateSummary(
            critical_unserved=critical_unserved,
            critical_unserved_impact_eur=round(critical_impact, 2),
            future_unserved=future_unserved,
            future_unserved_impact_eur=round(future_impact, 2),
            active_truck_days=active_truck_days,
            short_active_days=short_active_days,
            underused_drive_hours=round(underused_drive_hours, 2),
            idle_trucks=idle_trucks,
            used_trucks=used_trucks,
            end_imbalance=int(getattr(recommendation, "end_of_horizon_imbalance", 0) or 0),
            total_cost_eur=float(getattr(recommendation, "total_cost_eur", 0.0) or 0.0),
        )

    def _planner_summary_feedback(self, summary: AICandidateSummary) -> Dict[str, Any]:
        return {
            "type": "info",
            "code": "AI_COORDINATOR_SUMMARY",
            "critical_unserved": summary.critical_unserved,
            "critical_unserved_impact_eur": summary.critical_unserved_impact_eur,
            "future_unserved": summary.future_unserved,
            "future_unserved_impact_eur": summary.future_unserved_impact_eur,
            "active_truck_days": summary.active_truck_days,
            "short_active_days": summary.short_active_days,
            "underused_drive_hours": summary.underused_drive_hours,
            "idle_trucks": summary.idle_trucks,
            "used_trucks": summary.used_trucks,
            "end_imbalance": summary.end_imbalance,
            "total_cost_eur": summary.total_cost_eur,
            "message": (
                f"Planner summary: critical_unserved={summary.critical_unserved}, "
                f"future_unserved={summary.future_unserved}, active_truck_days={summary.active_truck_days}, "
                f"short_active_days={summary.short_active_days}, underused_drive_hours={summary.underused_drive_hours:.1f}, "
                f"idle_trucks={summary.idle_trucks}, end_imbalance={summary.end_imbalance}, total_cost_eur={summary.total_cost_eur:.0f}"
            ),
        }

    @staticmethod
    def _should_attempt_ai_repair(
        summary: AICandidateSummary,
        risk_score: float,
    ) -> bool:
        return bool(
            summary.critical_unserved > 0
            or (summary.critical_unserved > 0 and (
                summary.short_active_days > 0
                or summary.underused_drive_hours > 2.0
            ))
            or (summary.future_unserved > 0 and summary.idle_trucks > 0)
            or summary.end_imbalance > 0
            or risk_score > 3.5
        )

    def _build_container_moves(self, routes: List[Route]) -> List[ContainerMove]:
        """
        Build a flat list of ContainerMove records from all route swap operations.
        Each dropped bay at a stop represents a container moved FROM the previous
        pickup site TO the current stop site.
        """
        # self.sites is Dict[str, Site] — use .values() to get Site objects
        site_lookup: Dict[str, Any] = self.sites  # already keyed by site_id

        # Build bay serial lookup: site_id -> {bay_id -> serial_number}
        bay_serial_lookup: Dict[str, Dict[str, Optional[str]]] = {}
        for site in self.sites.values():
            bay_serial_lookup[site.id] = {
                bay.bay_id: bay.serial_number
                for bay in site.bays
            }

        moves: List[ContainerMove] = []

        for route in routes:
            # Track where containers were picked up (bay_id -> from_site_id)
            picked_from: Dict[str, str] = {}

            for stop in route.stops:
                if not stop.swap_operation:
                    continue

                swap = stop.swap_operation
                stop_site = site_lookup.get(stop.site_id)
                stop_site_name = stop_site.name if stop_site else stop.site_id

                # Each dropped container moved FROM its pickup site TO this site.
                # Process drops BEFORE picks so same-stop pick+drop of same bay_id
                # doesn't overwrite picked_from before the drop can read it.
                for bay_id in swap.containers_dropped:
                    from_site_id = picked_from.pop(bay_id, None)

                    if from_site_id is None:
                        print(f"[ContainerMoves] missing origin for bay {bay_id} → skip")
                        continue

                    from_site = site_lookup.get(from_site_id)
                    from_site_name = from_site.name if from_site else from_site_id

                    # Look up serial number from the from_site's bays
                    serial = bay_serial_lookup.get(from_site_id, {}).get(bay_id)

                    # Determine reason
                    if stop_site and stop_site.site_type == 'production':
                        reason = 'empty return'
                    else:
                        reason = 'full delivery'

                    # Safety: skip self-loops (should not occur with proper origin tracking)
                    if from_site_id == stop.site_id:
                        print(f"[ContainerMoves] skipping self-loop bay={bay_id} site={stop.site_id}")
                        continue

                    _move = ContainerMove(
                        from_site_id=from_site_id,
                        from_site_name=from_site_name,
                        to_site_id=stop.site_id,
                        to_site_name=stop_site_name,
                        bay_id=bay_id,
                        bay_serial_number=serial,
                        reason=reason,
                        truck_id=route.truck_id,
                        day_index=route.day_index,
                    )
                    # P0-1 + P0-2: enforce no self-loops and no fake containers
                    validate_move(_move)
                    moves.append(_move)

                # Record where each picked-up container came from (after drops)
                for bay_id in swap.containers_picked:
                    picked_from[bay_id] = stop.site_id

        return moves

    def _snapshot_state(self, trucks: List[Truck]) -> dict:
        """Snapshot current simulation state for debug output.

        Returns a dict with:
        - site_bay_counts: per-site bay count summary (full/partial/empty)
        - truck_states: per-truck location and load summary
        - state_hash: checksum to detect state changes across days
        """
        import hashlib
        import json

        # Site bay inventory summary
        site_bay_counts = {}
        for site_id, site in self.sites.items():
            full = sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "full")
            partial = sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "partial")
            empty = sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "empty")
            site_bay_counts[site_id] = {"full": full, "partial": partial, "empty": empty}

        # Truck state summary
        truck_states = {}
        for truck in trucks:
            truck_states[truck.id] = {
                "location": truck.effective_start_site_id or truck.current_location_site_id or truck.home_site_id,
                "load_count": len(truck.current_load),
                "load_ids": list(truck.current_load),
            }

        # Compute state hash for change detection
        state_str = json.dumps({"sites": site_bay_counts, "trucks": truck_states}, sort_keys=True)
        state_hash = hashlib.md5(state_str.encode()).hexdigest()

        return {
            "site_bay_counts": site_bay_counts,
            "truck_states": truck_states,
            "state_hash": state_hash,
        }

    def _compute_state_delta(self, state_before: dict, state_after: dict) -> dict:
        """Compute delta summary between two states.

        Returns human-readable summary of changes:
        - sites_changed: list of sites with bay count changes
        - trucks_moved: list of trucks that changed location
        - total_bay_changes: count of bay state transitions
        """
        sites_changed = []
        for site_id in state_before["site_bay_counts"]:
            before = state_before["site_bay_counts"][site_id]
            after = state_after["site_bay_counts"].get(site_id, before)
            if before != after:
                delta = {
                    "full": after["full"] - before["full"],
                    "partial": after["partial"] - before["partial"],
                    "empty": after["empty"] - before["empty"],
                }
                sites_changed.append({
                    "site_id": site_id,
                    "before": before,
                    "after": after,
                    "delta": delta,
                })

        trucks_moved = []
        for truck_id in state_before["truck_states"]:
            before = state_before["truck_states"][truck_id]
            after = state_after["truck_states"].get(truck_id, before)
            if before["location"] != after["location"] or before["load_count"] != after["load_count"]:
                trucks_moved.append({
                    "truck_id": truck_id,
                    "from_location": before["location"],
                    "to_location": after["location"],
                    "load_before": before["load_count"],
                    "load_after": after["load_count"],
                })

        total_bay_changes = sum(
            abs(change["delta"]["full"]) + abs(change["delta"]["empty"])
            for change in sites_changed
        )

        return {
            "sites_changed": sites_changed,
            "trucks_moved": trucks_moved,
            "total_bay_changes": total_bay_changes,
        }

    def _compute_day_signature(self, day_routes: List[Route]) -> str:
        """Compute a signature for a day's routes to detect duplicates.

        Signature includes: truck IDs, stop sequences, and distances.
        """
        import hashlib
        import json

        signature_data = []
        for route in day_routes:
            route_sig = {
                "truck_id": route.truck_id,
                "stops": [s.site_id for s in route.stops],
                "distance_km": round(route.total_distance_km, 1),
            }
            signature_data.append(route_sig)

        sig_str = json.dumps(signature_data, sort_keys=True)
        return hashlib.md5(sig_str.encode()).hexdigest()

    def _update_truck_states_for_next_day(self, day_routes: List[Route], trucks: List[Truck]) -> None:
        """Update truck start locations and loads for next day based on where they ended today.

        For each truck used in today's routes:
        1. Set start location to the last stop's site_id
        2. Set current_load to the truck's load at the last stop (from swap operations)

        This ensures next day's planning starts from the end state of today.
        """
        from ..models import SiteLocation

        # Build a map of truck_id -> end state from routes
        truck_end_states = {}
        for route in day_routes:
            if not route.stops:
                continue

            last_stop = route.stops[-1]
            # Authoritative end-of-day load counts come from stop fields already
            # computed by swap assignment.
            end_full = max(0, int(last_stop.load_full_after or 0))
            end_empty = max(0, int(last_stop.load_empty_after or 0))
            # Persist stable synthetic IDs for carry-over so counts never vanish
            # between days even when IDs are not serial-tracked.
            truck_full = [f"__carry_full_{route.truck_id}_{i}" for i in range(end_full)]
            truck_empty = [f"__carry_empty_{route.truck_id}_{i}" for i in range(end_empty)]

            truck_end_states[route.truck_id] = {
                "end_site_id": last_stop.site_id,
                "end_load": truck_full + truck_empty,
                "end_full": end_full,
            }

        # Update truck objects for next day
        for truck in trucks:
            old_start = truck.effective_start_site_id or truck.home_site_id
            if truck.id in truck_end_states:
                end_state = truck_end_states[truck.id]
                truck.start = SiteLocation(site_id=end_state["end_site_id"])
                truck.current_load = end_state["end_load"]
                truck.current_location_site_id = end_state["end_site_id"]
                # Carry the physical end-of-day full-container count into
                # initial_load so Day N+1 starts with the correct pre-loaded state.
                truck.initial_load = end_state["end_full"]
                logger.info(
                    "Day complete: Truck %s moved %s → %s  end_full=%d → initial_load_next=%d",
                    truck.id, old_start, end_state["end_site_id"],
                    end_state["end_full"], truck.initial_load,
                )
            else:
                # Truck not used today — keep location and load state unchanged.
                logger.debug(
                    "Truck %s was not used, stays at %s (initial_load=%d, load_count=%d)",
                    truck.id, old_start, truck.initial_load, len(truck.current_load),
                )

    def _apply_day_operations_to_state(self, day_routes: List[Route]) -> None:
        """Apply a day's swap operations to site bay inventories for next-day planning.

        For each swap operation:
        - Picked bays at producers: mark as removed (pressure → 0, representing gone)
        - Dropped bays at consumers: fill an empty bay to 250 bar (full delivery)
        - Dropped empties at producers: set a zero-pressure bay (empty return)

        This mutates self.sites in place so the next day's VRP and demand
        calculation use updated inventories.
        """
        for route in day_routes:
            for stop in route.stops:
                op = stop.swap_operation
                if not op:
                    continue
                site = self.sites.get(stop.site_id)
                if not site:
                    continue

                # Process picks: bays leaving this site → set to operational floor.
                # (kg and mwh are computed fields that auto-derive from pressure_bar)
                for bay_id in op.containers_picked:
                    for bay in site.bays:
                        if bay.bay_id == bay_id:
                            bay.pressure_bar = self.config.usable_floor_bar
                            break

                # Process drops at consumer sites: fill an empty bay to 250 bar
                if site.is_consumer and op.containers_dropped:
                    empty_bays = sorted(
                        [b for b in site.bays if b.pressure_bar <= self.EMPTY_BAR],
                        key=lambda b: b.bay_id,
                    )
                    for bay in empty_bays[:len(op.containers_dropped)]:
                        bay.pressure_bar = 250

        # Debug: log bay assignments per site after swaps
        if logger.isEnabledFor(logging.DEBUG):
            for sid, site in self.sites.items():
                bay_summary = [(b.bay_id, b.pressure_bar) for b in site.bays]
                logger.debug("  POST-SWAP [%s]: %s", site.name, bay_summary)

        logger.info(
            "Applied day operations: %d routes, state updated for next day",
            len(day_routes),
        )

    def _apply_time_evolution(self, delta_time_hours: float = 24.0) -> None:
        """Simulate gas consumption (consumers) and production (producers) over delta_time_hours.

        This is called after swap operations are applied so that the *new* bay
        assignments drive the consumption/production, not the stale pre-swap state.

        Pipeline per day:
          1. _apply_day_operations_to_state  ← swaps update physical bay locations
          2. _apply_time_evolution           ← THIS METHOD: time passes, gas moves
          3. _update_truck_states_for_next_day

        Consumer depletion per bay:
          new_kg = max(0, pressure_to_kg(bay.pressure_bar) - consumption_rate_kg_h * dt)
          bay.pressure_bar = round(kg_to_pressure(new_kg))  [clamped 0-250]

        Producer refill per bay:
          new_kg = min(pressure_to_kg(250), pressure_to_kg(bay.pressure_bar) + production_rate_kg_h * dt)
          bay.pressure_bar = round(kg_to_pressure(new_kg))  [clamped 0-250]

        Args:
            delta_time_hours: Hours of real time to simulate (default 24 = one full day).
        """
        import logging as _logging
        debug = logger.isEnabledFor(_logging.DEBUG)
        max_kg = pressure_to_kg(250)  # 2829 kg — capacity cap
        floor_bar = self.config.usable_floor_bar
        min_kg = pressure_to_kg(floor_bar)

        is_rewind = delta_time_hours < 0
        dt_abs = abs(delta_time_hours)

        # P0-3+P0-4: snapshot all bay IDs and per-bay kg before evolution
        # Use (site_id, bay_id) tuples so bays with the same name at different sites are distinct
        _all_bay_ids = [
            (sid, b.bay_id) for sid, site in self.sites.items() for b in site.bays
        ]
        _bay_kg_snap: dict = {
            (sid, b.bay_id): pressure_to_kg(b.pressure_bar)
            for sid, site in self.sites.items()
            for b in site.bays
        }
        _before_total_kg = sum(_bay_kg_snap.values())

        def _bay_order_key(bay) -> tuple:
            # Deterministic physical order: Bay1, Bay2, ..., Bay10
            bid = getattr(bay, "bay_id", "") or ""
            digits = "".join(ch for ch in bid if ch.isdigit())
            return (0, int(digits), bid) if digits else (1, 0, bid)

        for site in self.sites.values():
            if site.is_consumer:
                rate_kg_h = site.consumption_rate_kg_hour
                if rate_kg_h <= 0:
                    continue
                pool_kg = rate_kg_h * dt_abs

                if not is_rewind:
                    # ── Advance: drain one bay at a time in fixed bay order ──
                    # Real operation model requested by operators:
                    # Bay1 drains to (near) empty before Bay2 starts draining.
                    remaining = pool_kg
                    for bay in sorted(site.bays, key=_bay_order_key):
                        if remaining <= 0.0:
                            break
                        old_p = bay.pressure_bar
                        cur_kg = pressure_to_kg(old_p)
                        available_kg = max(0.0, cur_kg - min_kg)
                        taken = min(available_kg, remaining)
                        new_kg = max(min_kg, min(max_kg, cur_kg - taken))
                        assert new_kg >= 0, f"[ASSERT] consumer kg<0 site={site.id} bay={bay.bay_id}"
                        new_p = max(0, min(250, round(kg_to_pressure(new_kg))))
                        bay.pressure_bar = new_p
                        remaining -= taken
                        print(f"[CONS] site={site.id} bay={bay.bay_id} before={cur_kg:.0f} after={new_kg:.0f}")
                        if debug:
                            logger.debug("  CONSUME [%s] bay %s: %d→%d bar (−%.1f kg)", site.name, bay.bay_id, old_p, new_p, taken)
                else:
                    # ── Rewind: inverse of drain order (last bay first) ──
                    remaining = pool_kg
                    for bay in sorted(site.bays, key=_bay_order_key, reverse=True):
                        if remaining <= 0.0:
                            break
                        old_p = bay.pressure_bar
                        cur_kg = pressure_to_kg(old_p)
                        space = max_kg - cur_kg
                        if space <= 0.0:
                            continue
                        added = min(space, remaining)
                        new_p = max(0, min(250, round(kg_to_pressure(min(max_kg, cur_kg + added)))))
                        bay.pressure_bar = new_p
                        remaining -= added
                        if debug:
                            logger.debug("  REWIND-CONSUMER [%s] bay %s: %d→%d bar (+%.1f kg)", site.name, bay.bay_id, old_p, new_p, added)

            elif site.is_producer:
                if site.production is None or site.production.effective_kg_per_h is None:
                    continue
                rate_kg_h = site.production.effective_kg_per_h
                if rate_kg_h <= 0:
                    continue
                pool_kg = rate_kg_h * dt_abs

                if not is_rewind:
                    # ── Advance: fill one bay at a time in fixed bay order ──
                    # Bay1 fills to full before Bay2 starts filling.
                    remaining = pool_kg
                    for bay in sorted(site.bays, key=_bay_order_key):
                        if remaining <= 0.0:
                            break
                        old_p = bay.pressure_bar
                        cur_kg = pressure_to_kg(old_p)
                        space = max_kg - cur_kg
                        if space <= 0.0:
                            continue
                        added = min(space, remaining)
                        new_kg = max(0.0, min(max_kg, cur_kg + added))
                        assert new_kg >= 0, f"[ASSERT] producer kg<0 site={site.id} bay={bay.bay_id}"
                        new_p = max(0, min(250, round(kg_to_pressure(new_kg))))
                        bay.pressure_bar = new_p
                        remaining -= added
                        print(f"[PROD] site={site.id} bay={bay.bay_id} new_kg={new_kg:.0f} capped={new_kg >= max_kg}")
                        if debug:
                            logger.debug("  PRODUCE [%s] bay %s: %d→%d bar (+%.1f kg)", site.name, bay.bay_id, old_p, new_p, added)
                else:
                    # ── Rewind: inverse of fill order (last bay first) ──
                    remaining = pool_kg
                    for bay in sorted(site.bays, key=_bay_order_key, reverse=True):
                        if remaining <= 0.0:
                            break
                        old_p = bay.pressure_bar
                        cur_kg = pressure_to_kg(old_p)
                        available_kg = max(0.0, cur_kg - min_kg)
                        taken = min(available_kg, remaining)
                        new_p = max(0, min(250, round(kg_to_pressure(max(min_kg, cur_kg - taken)))))
                        bay.pressure_bar = new_p
                        remaining -= taken
                        if debug:
                            logger.debug("  REWIND-PRODUCER [%s] bay %s: %d→%d bar (−%.1f kg)", site.name, bay.bay_id, old_p, new_p, taken)

        # P0-3+P0-4: compute REAL production/consumption by site type (signed).
        # Only count gas moved by production/consumption rates — ignore swaps.
        # For production sites:  produced_kg += site_delta_kg  (positive=advance, negative=rewind)
        # For consumer sites:    consumed_kg += -site_delta_kg (positive=advance, negative=rewind)
        # Identity: produced_kg - consumed_kg == after_total_kg - before_total_kg
        _after_total_kg = sum(
            pressure_to_kg(b.pressure_bar)
            for site in self.sites.values()
            for b in site.bays
        )
        _produced_kg: float = 0.0
        _consumed_kg: float = 0.0
        for sid, site in self.sites.items():
            site_delta = sum(
                pressure_to_kg(b.pressure_bar) - _bay_kg_snap.get((sid, b.bay_id), 0.0)
                for b in site.bays
            )
            if site.site_type.value == "production":
                _produced_kg += site_delta
            elif site.site_type.value in ("traffic", "industry"):
                _consumed_kg += -site_delta
        validate_state(_all_bay_ids, _before_total_kg, _after_total_kg, _produced_kg, _consumed_kg)

        logger.info(
            "Time evolution applied: dt=%.1fh across %d sites (consumers deplete, producers fill)",
            delta_time_hours, len(self.sites),
        )

    def _enrich_legs_with_geometry(self, routes: List[Route]) -> None:
        """Fetch road-following geometry for each route via GraphHopper.

        Sets both:
          - route.geometry: full multi-stop polyline for map rendering
          - leg.geometry: per-leg polyline for accurate segment-label midpoints
        """
        if not self.routing_service:
            return

        # Build coordinate lookup: site_id -> (lat, lon)
        # Include real sites and virtual sites (custom map points)
        coord_lookup: dict[str, tuple[float, float]] = {}
        for sid, site in self.sites.items():
            coord_lookup[sid] = (site.latitude, site.longitude)
        for vid, vsite in self._virtual_sites.items():
            coord_lookup[vid] = (vsite.latitude, vsite.longitude)

        enriched = 0
        for route in routes:
            # ── Full route geometry (multi-stop) ─────────────────────────────
            waypoints: list[tuple[float, float]] = []
            for stop in route.stops:
                coords = coord_lookup.get(stop.site_id)
                if coords:
                    waypoints.append(coords)

            if len(waypoints) < 2:
                continue

            pts = self.routing_service.route_multi_stop(waypoints)
            if len(pts) > len(waypoints):
                route.geometry = pts
                enriched += 1
                logger.debug(
                    "Route %s (%s): %d waypoints → %d geometry points",
                    route.id, route.truck_id, len(waypoints), len(pts),
                )

            # ── Per-leg geometry (point-to-point for each leg) ────────────────
            # Used by routeLabels.ts to place segment-number badges at the true
            # road midpoint of each leg rather than a straight-line midpoint.
            for leg in route.legs:
                from_coords = coord_lookup.get(leg.from_site_id)
                to_coords = coord_lookup.get(leg.to_site_id)
                if from_coords and to_coords:
                    result = self.routing_service.route(
                        from_coords[0], from_coords[1],
                        to_coords[0], to_coords[1],
                    )
                    leg_pts = result.decoded_geometry
                    if leg_pts:
                        leg.geometry = leg_pts

        if enriched > 0:
            logger.info("Enriched %d routes with road geometry (multi-stop + per-leg)", enriched)

    def _calculate_mwh_moved(self, routes: List[Route]) -> tuple:
        """Calculate total MWh delivered to consumer sites with full breakdown.

        Sums energy of full bays dropped at consumer sites using bay pressure → kg → MWh.

        Args:
            routes: List of routes with swap operations

        Returns:
            Tuple of (total_mwh, debug_dict) where debug_dict contains the full breakdown.
        """
        # Build bay lookup: bay_id -> Bay object (across all sites)
        bay_lookup = {}
        for site in self.sites.values():
            for b in site.bays:
                bay_lookup[b.bay_id] = b

        gross_dropped_mwh = 0.0  # all bays dropped everywhere
        net_delivered_mwh = 0.0  # bays dropped at consumers only (current meaning)
        per_route = []
        assumptions = [
            "energy_moved counts only bays DROPPED at consumer sites (traffic/industry)",
            "bay energy uses snapshot pressure at plan time: pressure_bar → kg (piecewise linear) → MWh = (kg/1000)*15.2",
            "picked-up (empty) bays are NOT subtracted; this is gross delivered energy",
        ]

        for route in routes:
            route_mwh = 0.0
            legs = []
            prev_site_id = None
            for stop in route.stops:
                if not stop.swap_operation:
                    prev_site_id = stop.site_id
                    continue
                site = self.sites.get(stop.site_id)
                dropped_ids = stop.swap_operation.containers_dropped
                picked_ids = stop.swap_operation.containers_picked

                stop_dropped_mwh = 0.0
                bay_details = []
                for bay_id in dropped_ids:
                    bay = bay_lookup.get(bay_id)
                    bay_mwh = bay.mwh if bay else 0.0
                    stop_dropped_mwh += bay_mwh
                    gross_dropped_mwh += bay_mwh
                    bay_details.append({
                        "bay_id": bay_id,
                        "pressure_bar": bay.pressure_bar if bay else None,
                        "mwh": round(bay_mwh, 3),
                    })

                is_consumer = site.is_consumer if site else False
                if is_consumer:
                    net_delivered_mwh += stop_dropped_mwh
                    route_mwh += stop_dropped_mwh

                leg_entry = {
                    "from": prev_site_id or "(start)",
                    "to": stop.site_id,
                    "to_name": site.name if site else stop.site_id,
                    "site_type": site.site_type.value if site else "unknown",
                    "is_consumer": is_consumer,
                    "bays_dropped": len(dropped_ids),
                    "bays_picked": len(picked_ids),
                    "dropped_mwh": round(stop_dropped_mwh, 3),
                    "counted_toward_total": is_consumer,
                    "bay_details": bay_details,
                }
                if stop.distance_from_previous_km:
                    leg_entry["distance_km"] = round(stop.distance_from_previous_km, 1)

                legs.append(leg_entry)
                prev_site_id = stop.site_id

            per_route.append({
                "route_id": route.id,
                "truck_id": route.truck_id,
                "moved_mwh": round(route_mwh, 3),
                "legs": legs,
            })

        debug = {
            "definition": "Sum of MWh in bays dropped at consumer sites (traffic + industry). "
                          "Uses bay pressure at plan-creation time.",
            "totals": {
                "gross_dropped_mwh": round(gross_dropped_mwh, 3),
                "net_delivered_mwh": round(net_delivered_mwh, 3),
            },
            "per_route": per_route,
            "assumptions": assumptions,
        }

        return net_delivered_mwh, debug

    def validate_container_balance(self, routes: List[Route]) -> tuple[bool, List[str]]:
        """Validate bay balance constraints for plants.

        Rules:
        1. Plants cannot go below minimum operational bays
        2. Bays picked from plants but not returned are reported as warning

        Args:
            routes: List of routes to validate

        Returns:
            Tuple of (is_valid, list of warnings)
        """
        warnings = []
        is_valid = True

        # Track bay movements per plant
        plant_bay_balance = {}  # plant_id -> net change (negative = bays removed)
        bays_from_plants = 0
        bays_to_plants = 0

        for route in routes:
            for stop in route.stops:
                if not stop.swap_operation:
                    continue
                site = self.sites.get(stop.site_id)
                if not site or not site.is_producer:
                    continue

                picked = len(stop.swap_operation.containers_picked)
                dropped = len(stop.swap_operation.containers_dropped)
                net_change = dropped - picked

                plant_id = stop.site_id
                plant_bay_balance[plant_id] = plant_bay_balance.get(plant_id, 0) + net_change
                bays_from_plants += picked
                bays_to_plants += dropped

        # Check minimum bay constraint at each plant
        min_containers = self.config.min_containers_at_plant
        for plant_id, net_change in plant_bay_balance.items():
            if net_change < 0:
                site = self.sites.get(plant_id)
                current_count = len(site.bays) if site else 0
                final_count = current_count + net_change

                if final_count < min_containers:
                    warnings.append(
                        f"Plant {plant_id} would have {final_count} bays after plan, "
                        f"below minimum of {min_containers}"
                    )
                    is_valid = False

        # Check bay return balance (warning only)
        if bays_from_plants > bays_to_plants:
            deficit = bays_from_plants - bays_to_plants
            warnings.append(
                f"Bay imbalance: {bays_from_plants} picked from plants, "
                f"only {bays_to_plants} returned. {deficit} bays not returned to plants."
            )

        return is_valid, warnings

    def _prune_noop_stops(self, routes: List[Route]) -> List[Route]:
        """Remove stops without meaningful swap operations and discard empty routes.

        Always keeps:
        - sequence == 0 (start depot)
        - stops with actual container swaps
        - the final stop (truck's physical end location — needed for day continuity
          and to preserve forced-end and break-overnight positions)

        A route is only included in the result if it has ≥ 1 actual service stop
        (a stop with containers dropped or picked up).  Routes that only have
        [start, end] with no swaps are pure positioning moves and are discarded —
        they carry no operational value and must not appear in the plan.
        """
        pruned_routes = []
        bay_kg = {b.bay_id: b.kg for site in self.sites.values() for b in site.bays}
        for route in routes:
            stops = route.stops
            n = len(stops)
            kept = []
            has_service = False
            for i, stop in enumerate(stops):
                is_first = stop.sequence == 0
                is_last = i == n - 1
                pickup_kg = sum(bay_kg.get(bid, 0.0) for bid in ((stop.swap_operation.containers_picked) if stop.swap_operation else []))
                delivery_kg = sum(bay_kg.get(bid, 0.0) for bid in ((stop.swap_operation.containers_dropped) if stop.swap_operation else []))
                has_swap = (pickup_kg > 0.0 or delivery_kg > 0.0)
                if has_swap:
                    has_service = True
                if is_first or has_swap or is_last:
                    kept.append(stop)
            for i, stop in enumerate(kept):
                stop.sequence = i
            route.stops = kept
            # Require ≥ 1 actual service stop. [start, end]-only routes are
            # positional movements with no operational value — discard them.
            if has_service:
                pruned_routes.append(route)
            elif len(kept) > 1 and kept[0].site_id != kept[-1].site_id:
                pruned_routes.append(route)
                logger.debug(
                    "Truck %s: preserving positioning route %s -> %s",
                    route.truck_id,
                    kept[0].site_id,
                    kept[-1].site_id,
                )
            elif len(kept) > 1:
                logger.debug(
                    "Truck %s: pruned route has no service stops — discarded"
                    " (pure positioning, no containers moved)",
                    route.truck_id,
                )
                print(
                    f"[PruneNoop] truck={route.truck_id} route discarded:"
                    f" {len(kept)} stops but 0 service stops (no containers moved)"
                )
        return pruned_routes

    def _validate_routes_strict(
        self, routes: List[Route]
    ) -> tuple:
        """
        Strict validation of routes.

        Returns (is_valid, reason_code, reason_message, debug_metrics).
        """
        for route in routes:
            truck = self.trucks.get(route.truck_id)
            truck_load = 0
            for stop in route.stops:
                if stop.sequence == 0 and not stop.swap_operation:
                    continue
                # Rule 1: every non-depot stop must have >= 1 container moved
                if not stop.swap_operation:
                    return (
                        False,
                        "NOOP_STOP",
                        f"Route {route.truck_id} stop#{stop.sequence} at {stop.site_id} has no swap operation",
                        {"truck_id": route.truck_id, "site_id": stop.site_id},
                    )
                moved = len(stop.swap_operation.containers_dropped) + len(
                    stop.swap_operation.containers_picked
                )
                if moved == 0:
                    return (
                        False,
                        "NOOP_STOP",
                        f"Route {route.truck_id} stop#{stop.sequence} at {stop.site_id} moves 0 containers",
                        {"truck_id": route.truck_id, "site_id": stop.site_id},
                    )
                # Track truck load
                truck_load += len(stop.swap_operation.containers_picked)
                truck_load -= len(stop.swap_operation.containers_dropped)
                # Rule 2: truck load never negative
                if truck_load < 0:
                    return (
                        False,
                        "NEGATIVE_TRUCK_LOAD",
                        f"Route {route.truck_id} has negative load ({truck_load}) after stop at {stop.site_id}",
                        {"truck_id": route.truck_id, "site_id": stop.site_id, "load": truck_load},
                    )
                # Rule 3: truck capacity not exceeded
                if truck and truck_load > truck.capacity:
                    return (
                        False,
                        "TRUCK_OVERCAPACITY",
                        f"Route {route.truck_id} exceeds capacity ({truck_load}/{truck.capacity}) at {stop.site_id}",
                        {
                            "truck_id": route.truck_id,
                            "site_id": stop.site_id,
                            "load": truck_load,
                            "capacity": truck.capacity,
                        },
                    )
            if truck_load != 0:
                return (
                    False,
                    "END_LOAD_NOT_ZERO",
                    f"Route {route.truck_id} ends with {truck_load} container(s) still on truck",
                    {"truck_id": route.truck_id, "end_load": truck_load},
                )

        return (True, "", "", {})

    def _create_infeasible_recommendation(
        self,
        reason_code: str,
        reason_message: str,
        objective: ObjectiveFunction = ObjectiveFunction.BALANCED,
        horizon_days: int = 1,
        debug: dict = None,
    ) -> Recommendation:
        """Return an infeasible recommendation with reason."""
        return Recommendation(
            status=RecommendationStatus.INFEASIBLE,
            objective_function=objective.value,
            horizon_days=horizon_days,
            routes=[],
            total_distance_km=0,
            transport_cost_eur=0,
            handling_cost_eur=0,
            total_cost_eur=0,
            total_mwh_moved=0,
            eur_per_mwh=None,
            sites_served=0,
            critical_sites_addressed=0,
            risk_reduction_score=0,
            explanation=reason_message,
            warnings=[],
            reason_code=reason_code,
            reason_message=reason_message,
        )

    def _trace_routes(self, routes: List[Route]) -> list:
        """Build per-stop calculation trace for debug output (bay-aware)."""
        # Build bay lookup for pressure info
        bay_lookup = {}
        for site in self.sites.values():
            for b in site.bays:
                bay_lookup[b.bay_id] = b

        traced = []
        for route in routes:
            truck = self.trucks.get(route.truck_id)
            stops_trace = []
            truck_load = 0
            load_full = 0
            load_empty = 0
            for stop in route.stops:
                load_before = truck_load
                full_before = load_full
                empty_before = load_empty
                picked, dropped = [], []
                if stop.swap_operation:
                    picked = list(stop.swap_operation.containers_picked)
                    dropped = list(stop.swap_operation.containers_dropped)
                    truck_load += len(picked) - len(dropped)
                load_full = stop.load_full_after
                load_empty = stop.load_empty_after
                site = self.sites.get(stop.site_id)
                travel_h = (
                    stop.distance_from_previous_km / self.config.avg_speed_kmph
                    if self.config.avg_speed_kmph > 0 else 0
                )

                # Bay state detail
                picked_detail = []
                for bay_id in picked:
                    bay = bay_lookup.get(bay_id)
                    picked_detail.append({
                        "bay_id": bay_id,
                        "pressure_bar": bay.pressure_bar if bay else None,
                        "state": self._bay_state(bay.pressure_bar) if bay else "unknown",
                        "mwh": round(bay.mwh, 2) if bay else 0,
                    })
                dropped_detail = []
                for bay_id in dropped:
                    bay = bay_lookup.get(bay_id)
                    dropped_detail.append({
                        "bay_id": bay_id,
                        "pressure_bar": bay.pressure_bar if bay else None,
                        "state": self._bay_state(bay.pressure_bar) if bay else "unknown",
                        "mwh": round(bay.mwh, 2) if bay else 0,
                    })

                # Site bay summary
                site_bay_summary = None
                if site:
                    site_bay_summary = {
                        "total_bays": len(site.bays),
                        "bays_fixed": site.bays_fixed,
                        "full": sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "full"),
                        "partial": sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "partial"),
                        "empty": sum(1 for b in site.bays if self._bay_state(b.pressure_bar) == "empty"),
                    }

                stops_trace.append({
                    "seq": stop.sequence,
                    "site_id": stop.site_id,
                    "site_type": site.site_type.value if site else "unknown",
                    "distance_km": round(stop.distance_from_previous_km, 1),
                    "travel_time_h": round(travel_h, 3),
                    "service_time_h": round(stop.service_time_hours, 3),
                    "containers_picked": picked,
                    "containers_dropped": dropped,
                    "picked_detail": picked_detail,
                    "dropped_detail": dropped_detail,
                    "site_bays": site_bay_summary,
                    "truck_load_before": load_before,
                    "truck_load_after": truck_load,
                    "load_full_before": full_before,
                    "load_full_after": load_full,
                    "load_empty_before": empty_before,
                    "load_empty_after": load_empty,
                })
            transport_cost = route.total_distance_km * self.config.cost_per_km_eur
            handling_cost = route.num_stops * self.config.handling_fee_eur
            traced.append({
                "truck_id": route.truck_id,
                "truck_capacity": truck.capacity if truck else None,
                "stops": stops_trace,
                "totals": {
                    "distance_km": round(route.total_distance_km, 1),
                    "time_hours": round(route.total_time_hours, 2),
                    "num_swaps": sum(1 for s in stops_trace if s["containers_picked"] or s["containers_dropped"]),
                    "transport_cost_eur": round(transport_cost, 2),
                    "handling_cost_eur": round(handling_cost, 2),
                    "total_cost_eur": round(
                        (transport_cost + handling_cost) * self.config.contingency_multiplier, 2
                    ),
                },
            })
        return traced

    def _compute_unreturned_containers(self, routes: List[Route]) -> int:
        """Count containers picked from plants but not returned to any plant."""
        picked = 0
        returned = 0
        for route in routes:
            for stop in route.stops:
                if not stop.swap_operation:
                    continue
                site = self.sites.get(stop.site_id)
                if site and site.is_producer:
                    picked += len(stop.swap_operation.containers_picked)
                    returned += len(stop.swap_operation.containers_dropped)
        return max(0, picked - returned)

    def _compute_flaring_exposure(
        self,
        assessments: list,
        served_site_ids: set,
        planning_horizon_h: float = 24.0,
    ) -> dict:
        """Compute cumulative flaring exposure for the planning horizon.

        Returns:
            dict with keys:
              'total_h': total expected flaring hours across all unserved producers
              'over_limit': True if total_h > FLARING_SOFT_LIMIT_HOURS
              'penalty_factor': quadratic scaling factor (1.0 = within limit, >1 = exponential growth)
              'site_hours': {site_id: expected_flaring_h} for each producer
        """
        site_hours: dict = {}
        total_h = 0.0
        for a in assessments:
            site = self.sites.get(a.site_id)
            if not site or not site.is_producer:
                continue
            if a.site_id in served_site_ids:
                continue  # served — no expected flaring from this plan
            # Hours until flaring starts = hours_to_critical (0 means already flaring)
            hours_until_flaring = max(0.0, a.hours_to_critical)
            # Expected flaring within horizon if NOT served
            expected_flaring_h = max(0.0, planning_horizon_h - hours_until_flaring)
            if expected_flaring_h > 0:
                site_hours[a.site_id] = expected_flaring_h
                total_h += expected_flaring_h

        FLARING_SOFT_LIMIT_H = FLARING_SOFT_LIMIT_HOURS
        over_limit = total_h > FLARING_SOFT_LIMIT_H
        # Quadratic penalty factor: (total_h / limit)² when over limit
        if over_limit and total_h > 0:
            penalty_factor = (total_h / FLARING_SOFT_LIMIT_H) ** 2
        else:
            penalty_factor = 1.0

        return {
            "total_h": round(total_h, 2),
            "over_limit": over_limit,
            "penalty_factor": round(penalty_factor, 2),
            "site_hours": site_hours,
        }

    def _compute_end_of_horizon_imbalance(self, routes: List[Route]) -> int:
        """Count containers still on trucks at end of the full plan (all days).

        Evaluates across all routes: tracks pickups minus drops per truck.
        Returns total containers remaining across all trucks.
        """
        truck_load: dict = {}
        for route in sorted(routes, key=lambda r: r.day_index):
            tid = route.truck_id
            if tid not in truck_load:
                truck_load[tid] = 0
            for stop in route.stops:
                if not stop.swap_operation:
                    continue
                truck_load[tid] += len(stop.swap_operation.containers_picked)
                truck_load[tid] -= len(stop.swap_operation.containers_dropped)
        return sum(max(0, v) for v in truck_load.values())

    def _compute_solution_risk_score(
        self,
        assessments: list,
        demand_sites: List[str],
        routes: List[Route],
    ) -> float:
        """Compute solution risk score 0–10.

        Reflects residual stock-out and flaring exposure after the planned routes.
        Consumer (stock-out) risk is weighted higher than producer (flaring) risk.
        Flaring exposure beyond the configured soft limit receives a quadratic penalty boost.
        End-of-horizon container imbalance adds additional risk.
        Target acceptable range: 3–4.
        Score 0 = no risk, 10 = maximum exposure.
        """
        served_site_ids = {
            stop.site_id for route in routes for stop in route.stops
            if stop.swap_operation and (
                stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
            )
        }
        total_score = 0.0
        max_score = 0.0
        producer_score = 0.0
        producer_max = 0.0
        for a in assessments:
            site = self.sites.get(a.site_id)
            if not site:
                continue
            # Exposure: 0–1 from individual risk_score (0–100)
            exposure = a.risk_score / 100.0
            # Reduce exposure if site is served in this plan
            if a.site_id in served_site_ids:
                exposure *= 0.2  # 80% reduction for served sites
            weight = 1.0 if site.is_consumer else 0.7
            total_score += exposure * weight
            max_score += weight
            if site.is_producer:
                producer_score += exposure * weight
                producer_max += weight

        if max_score == 0:
            return 0.0

        # Apply quadratic flaring penalty factor to producer portion of score
        flaring = self._compute_flaring_exposure(assessments, served_site_ids)
        if flaring["over_limit"] and producer_max > 0:
            boosted_producer = producer_score * flaring["penalty_factor"]
            total_score = (total_score - producer_score) + boosted_producer

        # Add a soft imbalance penalty.
        # Operator policy prefers "some solution with imbalance" over "no solution",
        # so keep this weight modest and let stockout/flaring dominate the score.
        imbalance = self._compute_end_of_horizon_imbalance(routes)
        total_score += imbalance * 0.02 * max_score  # scale with max_score for normalization

        return round(min(10.0, (total_score / max_score) * 10.0), 1)

    def _build_explanation(
        self,
        routes: List[Route],
        objective: ObjectiveFunction,
        total_distance: float,
        total_cost: float,
        sites_served: int,
        critical_addressed: int,
        total_mwh_moved: float = 0.0,
        eur_per_mwh: float = None,
        assessments: list = None,
        demand_sites: List[str] = None,
        solution_risk_score: float = None,
    ) -> str:
        """Build human-readable explanation with prioritization rationale and trade-offs."""
        lines = []

        if objective == ObjectiveFunction.TIME:
            lines.append("Optimized for stock-out prevention (lowest remaining hours first).")
        elif objective == ObjectiveFunction.FLARING:
            lines.append("Optimized to reduce gas flaring at production sites.")
        else:
            lines.append("Balanced optimization: stock-out prevention first, then flaring reduction, then cost.")

        lines.append(
            "Stock-out priority uses 1000 EUR/h for the first 5h of outage and "
            f"5000 EUR/h after 5h. Flaring target remains below {FLARING_SOFT_LIMIT_HOURS:.0f}h per plan, with bay circulation "
            "used as a secondary preference rather than a hard blocker."
        )
        lines.append(
            f"Forward-looking posture keeps roughly {self.TARGET_BUFFER_HOURS:.0f}h of consumer buffer "
            "or producer free-space when economically reasonable, so the plan stays healthier tomorrow."
        )

        lines.append(
            f"Recommendation uses {len(routes)} truck(s) to serve {sites_served} site(s)."
        )
        lines.append(self._summarize_bay_mix())

        if critical_addressed > 0:
            lines.append(f"Addresses {critical_addressed} critical site(s) (< {self.config.critical_hours_threshold:.0f}h remaining).")

        # Prioritization rationale: list top-3 urgent demand sites by hours_to_critical
        if assessments and demand_sites:
            demand_set = set(demand_sites)
            urgent = sorted(
                [a for a in assessments if a.site_id in demand_set],
                key=lambda a: a.hours_to_critical,
            )[:3]
            if urgent:
                prio_parts = []
                for a in urgent:
                    site = self.sites.get(a.site_id)
                    name = site.name if site else a.site_id
                    special = " [ABSOLUTE PRIORITY]" if name == "Takkula" else ""
                    prio_parts.append(f"{name} ({a.hours_to_critical:.1f}h{special})")
                lines.append(f"Priority order (by hours remaining): {', '.join(prio_parts)}.")

        # Cost vs risk trade-off
        lines.append(
            f"Total distance: {total_distance:.1f} km "
            f"(min. billed {self.config.min_billed_km:.0f} km/route). "
            f"Estimated cost: {total_cost:.2f} EUR (incl. {int((self.config.contingency_multiplier - 1) * 100)}% contingency)."
        )

        if total_mwh_moved > 0:
            eur_per_mwh_str = f"{eur_per_mwh:.2f}" if eur_per_mwh else "N/A"
            lines.append(
                f"Energy delivered: {total_mwh_moved:.1f} MWh. "
                f"Cost efficiency: {eur_per_mwh_str} EUR/MWh."
            )

        total_time = sum(r.total_time_hours for r in routes)
        lines.append(f"Estimated total time: {total_time:.1f} hours.")

        # Flaring exposure report
        if assessments:
            served_ids = {
                stop.site_id for route in routes for stop in route.stops
                if stop.swap_operation and (
                    stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked
                )
            }
            flaring = self._compute_flaring_exposure(assessments, served_ids)
            if flaring["total_h"] > 0:
                msg = f"Expected flaring exposure: {flaring['total_h']:.1f}h across unserved producers."
                if flaring["over_limit"]:
                    msg += (
                        f" WARNING: Exceeds {FLARING_SOFT_LIMIT_HOURS:.0f}h soft limit "
                        f"(penalty factor {flaring['penalty_factor']:.1f}x)."
                    )
                lines.append(msg)

        # End-of-horizon container imbalance
        imbalance = self._compute_end_of_horizon_imbalance(routes)
        if imbalance > 0:
            lines.append(
                f"End-of-horizon imbalance: {imbalance} container(s) still on trucks after final day. "
                "This is allowed when needed to keep service feasible, but the planner now penalizes it strongly to finish globally balanced by the end of the horizon."
            )

        cycle_break_penalty = self._compute_route_cycle_break_penalty(routes)
        if cycle_break_penalty > 0:
            lines.append(
                f"Cycle integrity penalty applied: {cycle_break_penalty:.0f} EUR equivalent for routes that would leave containers awkwardly positioned for the next day."
            )

        # Solution risk score
        if solution_risk_score is not None:
            risk_band = "acceptable" if 3.0 <= solution_risk_score <= 4.0 else (
                "low" if solution_risk_score < 3.0 else "elevated"
            )
            lines.append(
                f"Solution risk score: {solution_risk_score:.1f}/10 ({risk_band}; target 3–4)."
            )

        return " ".join(lines)

    def _build_warnings(self, routes: List[Route]) -> List[str]:
        """Build list of warnings for operator."""
        warnings = []

        for route in routes:
            # Check for long routes
            if route.total_time_hours > self.config.max_driver_hours * 0.9:
                warnings.append(
                    f"Route for {route.truck_id} is {route.total_time_hours:.1f}h, "
                    f"close to {self.config.max_driver_hours}h limit."
                )
            if route.stops:
                last_stop = route.stops[-1]
                if (last_stop.load_full_after or 0) > 0:
                    warnings.append(
                        f"[CYCLE_BREAK] {route.truck_id} ends with {last_stop.load_full_after} full container(s) on board; this weakens next-day balance."
                    )

            # Check for empty swaps
            empty_swap_stops = [
                s for s in route.stops
                if s.swap_operation and
                not s.swap_operation.containers_dropped and
                not s.swap_operation.containers_picked
            ]
            if empty_swap_stops:
                warnings.append(
                    f"Route for {route.truck_id} has stops with no container swaps."
                )

            # Validate no swaps at non-designated locations
            for stop in route.stops:
                site = self.sites.get(stop.site_id)
                if site and not site.swap_allowed and stop.swap_operation:
                    if stop.swap_operation.containers_dropped or stop.swap_operation.containers_picked:
                        warnings.append(
                            f"[ILLEGAL_SWAP] {route.truck_id} attempted swap at non-designated site {site.name}."
                        )

        # Flaring exposure warnings (soft target for this planning run)
        for site in self.sites.values():
            if site.is_producer and site.is_full and site.flaring_loss_eur_per_h is not None:
                warnings.append(
                    f"[FLARING_RISK] {site.name} is at full pressure — currently flaring "
                    f"({site.flaring_loss_eur_per_h:.0f} EUR/h). Target is to keep total flaring near or below "
                    f"{FLARING_SOFT_LIMIT_HOURS:.0f}h per plan."
                )

        return warnings

    def _create_empty_recommendation(self, reason: str) -> Recommendation:
        """Create an empty recommendation with explanation."""
        return Recommendation(
            status=RecommendationStatus.READY,
            routes=[],
            total_distance_km=0,
            transport_cost_eur=0,
            handling_cost_eur=0,
            total_cost_eur=0,
            total_mwh_moved=0,
            eur_per_mwh=None,
            sites_served=0,
            critical_sites_addressed=0,
            risk_reduction_score=0,
            explanation=reason,
            warnings=[],
        )

    # ── Wait-vs-Act helpers ───────────────────────────────────────────────────

    def _compute_route_cost(self, routes: List[Route]) -> float:
        """Compute the economic cost of a set of routes (transport + handling, no contingency)."""
        total = 0.0
        for route in routes:
            stops = route.stops
            for i in range(len(stops) - 1):
                a, b = stops[i].site_id, stops[i + 1].site_id
                dist = (
                    self.distance_matrix.get(a, {}).get(b)
                    or self.distance_matrix.get(b, {}).get(a)
                    or 0.0
                )
                total += dist * self.config.cost_per_km_eur
            for stop in stops:
                if stop.swap_operation and (
                    stop.swap_operation.containers_dropped
                    or stop.swap_operation.containers_picked
                ):
                    total += self.config.handling_fee_eur
        total += self._compute_route_cycle_break_penalty(routes)
        return total

    def _estimate_waiting_risk_cost(self, demand_sites: List[str]) -> float:
        """EUR equivalent of risk exposure accumulated by NOT serving demand sites today."""
        assessments = {a.site_id: a for a in self.risk_calculator.assess_all_sites(self.sites)}
        risk_cost = 0.0
        for sid in demand_sites:
            a = assessments.get(sid)
            site = self.sites.get(sid)
            if not a or not site:
                continue
            htc = a.hours_to_critical
            is_producer = getattr(site, 'is_producer', False) or str(site.site_type) == 'production'
            if is_producer:
                # Estimate flaring volume if we wait 24h past the critical point
                flaring_cost = getattr(site, 'flaring_cost_eur_mwh', None) or 50.0
                overflow_duration = max(0.0, 24.0 - max(0.0, htc))
                if overflow_duration > 0:
                    overflow_kg = a.production_rate_kg_hour * overflow_duration
                    risk_cost += kg_to_mwh(overflow_kg) * flaring_cost
            else:
                # Consumer: penalty proportional to urgency (full at htc=0, zero at htc=96h)
                urgency = max(0.0, (96.0 - htc) / 96.0)
                if urgency > 0:
                    risk_cost += urgency * 800.0
        return risk_cost

    def _evaluate_wait_vs_act(
        self,
        day: int,
        today_routes: List[Route],
        demand_sites: List[str],
        selected_trucks: List[Truck],
        max_search_seconds: int,
        traffic_time_multiplier: float,
        risk_map: dict,
        risk_score_map: dict,
        urgency_factor_m: int,
        hours_to_critical_map: dict,
        fleet_config: Optional[list],
    ) -> dict:
        """
        Compare routing today vs waiting one day.

        Simulates 24h of time evolution, runs a quick VRP on tomorrow's state,
        and returns whether the wait-then-route plan is cheaper in total EUR.

        Returns dict keys:
            should_wait: bool
            today_cost: float
            tomorrow_cost: float
            risk_penalty: float
            explanation: str
        """
        today_cost = self._compute_route_cost(today_routes)
        if today_cost <= 0:
            return {
                "should_wait": False,
                "today_cost": 0.0,
                "tomorrow_cost": 0.0,
                "risk_penalty": 0.0,
                "explanation": "Today's plan has zero cost — acting is always preferred.",
            }

        risk_penalty = self._estimate_waiting_risk_cost(demand_sites)

        # Simulate tomorrow by deep-copying state, evolving 24h, then restoring.
        saved_sites = copy.deepcopy(self.sites)
        saved_virt = copy.deepcopy(self._virtual_sites)
        try:
            self._apply_time_evolution(delta_time_hours=24.0)

            tomorrow_demand, _ = self._determine_demand_sites(ObjectiveFunction.BALANCED, 48.0)
            if not tomorrow_demand:
                return {
                    "should_wait": False,
                    "today_cost": today_cost,
                    "tomorrow_cost": 0.0,
                    "risk_penalty": risk_penalty,
                    "explanation": "No demand exists tomorrow; acting today is correct.",
                }

            tmrw_assessments = self.risk_calculator.assess_all_sites(self.sites)
            tmrw_risk_map = {a.site_id: a.risk_level.value for a in tmrw_assessments}
            tmrw_score_map = {a.site_id: a.risk_score for a in tmrw_assessments}
            tmrw_htc_map = {a.site_id: a.hours_to_critical for a in tmrw_assessments}

            tomorrow_solver = VRPSolver(
                {**self.sites, **self._virtual_sites},
                self.distance_matrix,
                self.config,
                time_matrix_minutes=self.time_matrix_minutes,
                allow_symmetric_fallback=getattr(
                    self.vrp_solver, 'allow_symmetric_fallback', True
                ),
            )
            tomorrow_routes = tomorrow_solver.solve(
                trucks=selected_trucks,
                demand_sites=tomorrow_demand,
                max_search_seconds=max_search_seconds,
                traffic_time_multiplier=traffic_time_multiplier,
                risk_map=tmrw_risk_map,
                risk_score_map=tmrw_score_map,
                urgency_factor_m=urgency_factor_m,
                hours_to_critical_map=tmrw_htc_map,
                current_day=day + 1,
                fill_sites=None,
                transfer_sites=None,
                _is_final_day=False,
                cumulative_flaring_hours=0.0,
                risk_penalty_multiplier=1.0,
            )

            if not tomorrow_routes:
                return {
                    "should_wait": False,
                    "today_cost": today_cost,
                    "tomorrow_cost": float("inf"),
                    "risk_penalty": risk_penalty,
                    "explanation": "Tomorrow's simulation found no routes; acting today is safer.",
                }

            tomorrow_cost = self._compute_route_cost(tomorrow_routes)
            total_wait_cost = tomorrow_cost + risk_penalty

            # Require ≥15% saving to overcome solver noise and avoid oscillation
            WAIT_THRESHOLD = 0.85
            should_wait = total_wait_cost < today_cost * WAIT_THRESHOLD

            more_sites = len(tomorrow_demand) - len(demand_sites)
            if should_wait:
                savings_pct = (1.0 - total_wait_cost / today_cost) * 100.0
                explanation = (
                    f"Waiting 1 day improves route efficiency by {savings_pct:.0f}%"
                    f" (today={today_cost:.0f} EUR → wait+tomorrow={total_wait_cost:.0f} EUR"
                    f" [{tomorrow_cost:.0f} routing + {risk_penalty:.0f} risk],"
                    f" {more_sites:+d} additional sites tomorrow)."
                )
            else:
                explanation = (
                    f"Acting today is optimal:"
                    f" wait+tomorrow={total_wait_cost:.0f} EUR"
                    f" ≥ {WAIT_THRESHOLD*100:.0f}% of today={today_cost:.0f} EUR"
                    f" ({more_sites:+d} sites tomorrow, risk_penalty={risk_penalty:.0f} EUR)."
                )

            return {
                "should_wait": should_wait,
                "today_cost": today_cost,
                "tomorrow_cost": tomorrow_cost,
                "risk_penalty": risk_penalty,
                "explanation": explanation,
            }
        finally:
            # Restore state unconditionally — simulation must never bleed into the main loop
            self.sites = saved_sites
            self._virtual_sites = saved_virt

    def _select_decision_mode(
        self,
        day: int,
        demand_sites: List[str],
        selected_trucks: List,
        risk_map: dict,
        hours_to_critical_map: dict,
        urgency_factor_m: int,
        traffic_time_multiplier: float,
        max_search_seconds: int,
        horizon_days: int,
        waited_this_horizon: bool,
        fleet_config: Optional[list] = None,
    ) -> dict:
        """
        Pre-VRP three-mode decision: FULL_ACT, PARTIAL_ACT, or WAIT.

        Formal objective: J(m) = RoutingCost(m) + RiskCost(m)
        Decision rule: argmin J(m) over feasible modes.

        WAIT is infeasible when any demand site is critical within 28 h.
        PARTIAL_ACT excludes normal-risk sites whose solo routing cost > risk value.
        WAIT requires ≥15% savings over the best ACT option to avoid oscillation.

        Returns:
            mode: 'FULL_ACT' | 'PARTIAL_ACT' | 'WAIT'
            active_demand: List[str] — demand sites to pass to the VRP solver
            J: float — estimated objective value for chosen mode (EUR)
            J_full: float, J_partial: float, J_wait: float — all mode estimates
            excluded_sites: List[str] — sites dropped under PARTIAL_ACT
            explanation: str
        """
        WAIT_THRESHOLD = 0.85  # require ≥15% saving to prefer waiting

        # Shortage penalty rate — EUR per MWh of unmet consumer demand.
        # Aligned with VRPSolver.SHORTAGE_PENALTY_EUR_MWH.
        SHORTAGE_PENALTY_EUR_MWH = 200.0
        # Tiered stockout rates — aligned with VRPSolver constants.
        _STOCKOUT_RATE_EARLY_EUR_H = 1_000   # first 5h of stockout
        _STOCKOUT_RATE_LATE_EUR_H  = 5_000   # beyond 5h of stockout
        _STOCKOUT_BREAK_HOURS      = 5.0
        # Usable kg capacity per bay (20–250 bar range).
        _USABLE_KG_PER_BAY = 2697.4
        # KG → MWh conversion (must match VRPSolver._KG_TO_MWH)
        _KG_TO_MWH = 15.2 / 1000.0

        def _flow_value_eur(sid: str, delta_hours: float = None) -> float:
            """EUR economic loss of NOT serving site sid during the current shift.

            Aligned with VRPSolver flow value logic:
              Δt_h = min(hours_to_critical, max_driver_hours)  if htc > 0
                   = max_driver_hours                           if htc ≤ 0
                   = projected hours in crisis if site is skipped today.

            When delta_hours is provided (e.g. for WAIT simulation), it is used
            directly as the exposure window instead of the per-site Δt.
            """
            site = self.sites.get(sid)
            if not site:
                return 0.0
            max_h = self.config.max_driver_hours
            if delta_hours is not None:
                # Explicit window (WAIT simulation): compute over given horizon
                _Dt_h = delta_hours
            else:
                # Per-site urgency window: time available to act before crisis
                _site_htc = hours_to_critical_map.get(sid, 0.0)
                if _site_htc <= 0.0:
                    _Dt_h = max_h
                else:
                    _Dt_h = min(_site_htc, max_h)
            if _Dt_h <= 0.0:
                return 0.0
            # Usable kg above floor bar
            _floor = self.config.usable_floor_bar
            _usable_kg = sum(
                get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
                for b in site.bays
            )
            is_prod = getattr(site, 'is_producer', False) or str(site.site_type) == 'production'
            if is_prod:
                prod_rate = (
                    site.production.effective_kg_per_h
                    if site.production and site.production.effective_kg_per_h is not None
                    else 0.0
                )
                if prod_rate <= 0:
                    return 0.0
                flaring_rate = site.flaring_cost_eur_mwh or 50.0
                _cap_kg = site.bays_fixed * _USABLE_KG_PER_BAY
                overflow_kg = max(0.0, _usable_kg + prod_rate * _Dt_h - _cap_kg)
                return (
                    overflow_kg * _KG_TO_MWH * flaring_rate
                    + self._compute_future_buffer_gap_eur(site, self.TARGET_BUFFER_HOURS)
                )
            else:
                cons_rate = site.consumption_rate_kg_hour or 0.0
                if cons_rate <= 0:
                    return 0.0
                shortage_kg = max(0.0, cons_rate * _Dt_h - _usable_kg)
                if shortage_kg <= 0.0:
                    return self._compute_future_buffer_gap_eur(site, self.TARGET_BUFFER_HOURS)
                # Tiered stockout penalty
                _stockout_h = shortage_kg / cons_rate
                return (
                    min(_stockout_h, _STOCKOUT_BREAK_HOURS) * _STOCKOUT_RATE_EARLY_EUR_H
                    + max(0.0, _stockout_h - _STOCKOUT_BREAK_HOURS) * _STOCKOUT_RATE_LATE_EUR_H
                ) + self._compute_future_buffer_gap_eur(site, self.TARGET_BUFFER_HOURS)

        def _solo_cost(sid: str) -> float:
            """Marginal cost estimate: nearest-truck distance + 1 handling fee."""
            best_d = float("inf")
            for truck in selected_trucks:
                start_id = (truck.start.site_id if truck.start else None) or truck.home_site_id
                if not start_id:
                    continue
                d = (
                    self.distance_matrix.get(start_id, {}).get(sid)
                    or self.distance_matrix.get(sid, {}).get(start_id)
                    or 0.0
                )
                if d < best_d:
                    best_d = d
            return best_d * self.config.cost_per_km_eur + self.config.handling_fee_eur

        def _greedy_fleet_cost(sites: List[str]) -> float:
            """
            Greedy nearest-truck assignment cost estimate.
            Each truck starts at its configured position; sites are assigned
            round-robin to the nearest truck; running cost is accumulated.
            Underestimates actual VRP cost but preserves relative ordering.
            """
            if not sites:
                return 0.0
            positions: Dict[str, Optional[str]] = {
                t.id: (t.start.site_id if t.start else t.home_site_id)
                for t in selected_trucks
            }
            remaining = list(sites)
            total = 0.0
            while remaining:
                best_tid, best_sid, best_d = None, None, float("inf")
                for tid, pos in positions.items():
                    if pos is None:
                        continue
                    for sid in remaining:
                        d = (
                            self.distance_matrix.get(pos, {}).get(sid)
                            or self.distance_matrix.get(sid, {}).get(pos)
                            or 0.0
                        )
                        if d < best_d:
                            best_d, best_tid, best_sid = d, tid, sid
                if best_sid is None:
                    break
                total += best_d * self.config.cost_per_km_eur + self.config.handling_fee_eur
                positions[best_tid] = best_sid
                remaining.remove(best_sid)
            return total

        def _site_priority_key(sid: str) -> tuple:
            """Operational priority used before fleet-cap truncation.

            Order:
            1. Critical sites
            2. Warning sites
            3. Producers under flaring pressure
            4. Lower hours-to-critical first
            5. Higher economic risk value first
            """
            site = self.sites.get(sid)
            risk_level = risk_map.get(sid, "normal")
            if risk_level == "critical":
                risk_rank = 0
            elif risk_level == "warning":
                risk_rank = 1
            else:
                risk_rank = 2
            is_producer = bool(site and site.is_producer)
            htc = hours_to_critical_map.get(sid, 999.0)
            producer_rank = 0 if (is_producer and htc < 48.0) else 1
            return (
                risk_rank,
                producer_rank,
                htc,
                -_flow_value_eur(sid),
            )

        # ── 1. WAIT feasibility check ─────────────────────────────────────────
        urgent_sites = [
            sid for sid in demand_sites
            if hours_to_critical_map.get(sid, 999.0) < 28.0
        ]
        critical_present = any(risk_map.get(sid) == "critical" for sid in demand_sites)
        wait_eligible = (
            not urgent_sites
            and not critical_present
            and day < horizon_days
            and horizon_days > 1
            and not waited_this_horizon
        )

        n_demand = len(demand_sites)

        # ── 2. PARTIAL_ACT subset: include site only when flow_value ≥ routing cost ──
        # Critical/warning sites always included (high urgency).
        # Normal sites included only when economic gain > marginal routing cost.
        partial_demand: List[str] = []
        excluded: List[tuple] = []  # (site_id, flow_value_eur, solo_cost_eur)
        for sid in demand_sites:
            site = self.sites.get(sid)
            rl = risk_map.get(sid, "normal")
            htc_val = hours_to_critical_map.get(sid, 999.0)
            if rl in ("critical", "warning"):
                # Actively critical or warning — always include.
                partial_demand.append(sid)
            elif htc_val < 96.0:
                # Preventive: site will be in danger within 4 days.
                # Always include so trucks can serve it on the way rather than
                # making a dedicated trip tomorrow when it becomes critical.
                partial_demand.append(sid)
            else:
                flow_val = _flow_value_eur(sid)
                cost = _solo_cost(sid)
                # Producers contribute to bay circulation as well as site economics.
                # Use a lower activation threshold so the planner keeps enough
                # source nodes to refill and recover containers instead of
                # over-pruning to pure consumer-only demand.
                activation_threshold = cost * (0.5 if (site and site.is_producer) else 1.0)
                if flow_val >= activation_threshold:
                    partial_demand.append(sid)
                else:
                    excluded.append((sid, flow_val, cost))

        # ── 2b. Capacity cap: limit active demand to what the fleet can physically serve ──
        # Sort by urgency (htc ascending — most critical first), then cap at
        # max_serviceable = floor(shift_hours / service_time_hours) × num_trucks.
        # Sites beyond the cap are demoted to overflow_demand: they get a lower
        # skip penalty in the VRP so the solver always has a feasible exit.
        _cap_shift_h = self.config.max_driver_hours
        _cap_svc_h   = max(self.config.swap_time_hours, 0.1)
        _sites_per_truck  = max(1, int(_cap_shift_h / _cap_svc_h))
        _max_serviceable  = _sites_per_truck * max(len(selected_trucks), 1)
        _has_critical_pre = any(risk_map.get(sid) == "critical" for sid in demand_sites)
        if horizon_days > 1 and not _has_critical_pre:
            _reserve_capacity = self.RESERVE_STOPS_PER_TRUCK * max(len(selected_trucks), 1)
            _max_serviceable = max(len(selected_trucks), _max_serviceable - _reserve_capacity)

        partial_demand.sort(key=_site_priority_key)
        if len(partial_demand) > _max_serviceable:
            _overflow_demand: List[str] = partial_demand[_max_serviceable:]
            partial_demand = partial_demand[:_max_serviceable]
            print(
                f"[CapacityCap] Fleet capacity={_max_serviceable} sites"
                f" ({_sites_per_truck} stops/truck × {len(selected_trucks)} trucks)."
                f" Demand={len(partial_demand) + len(_overflow_demand)}:"
                f" {len(partial_demand)} mandatory, {len(_overflow_demand)} overflow."
            )
        else:
            _overflow_demand = []

        # ── 3. J(m) = Routing_Cost + Flow_Imbalance_Cost (aligned with solver) ──
        #
        # FULL_ACT:    J = routing_cost(all)
        # PARTIAL_ACT: J = routing_cost(partial) + Σ flow_value(excluded)
        # WAIT:        J = Σ flow_value(all, 24h) + routing_cost_tomorrow
        #                    + lookahead_penalty (sites that would become critical tomorrow)
        #
        # No service-level bonus: the marginal value condition already ensures
        # we only serve sites where flow_value > routing_cost.

        full_demand = sorted(demand_sites, key=_site_priority_key)
        _routing_full = _greedy_fleet_cost(full_demand)
        J_full = _routing_full  # pure routing cost; no artificial service bonus

        if excluded:
            _flow_excl = sum(v for _, v, _ in excluded)
            J_partial = _greedy_fleet_cost(partial_demand) + _flow_excl
        else:
            J_partial = J_full  # degenerate: no exclusions

        J_wait = float("inf")
        wait_detail = ""
        if wait_eligible:
            saved_sites = copy.deepcopy(self.sites)
            saved_virt = copy.deepcopy(self._virtual_sites)
            try:
                # Flow imbalance from NOT serving all sites today (per-site Δt)
                _flow_today = sum(_flow_value_eur(sid) for sid in demand_sites)
                self._apply_time_evolution(24.0)
                tmrw_demand, _ = self._determine_demand_sites(ObjectiveFunction.BALANCED, 48.0)
                _routing_tmrw = _greedy_fleet_cost(tmrw_demand or [])
                # Rolling horizon lookahead: penalize states that create new critical
                # sites tomorrow (htc < critical_threshold after 24h evolution)
                _tmrw_assessments = self.risk_calculator.assess_all_sites(self.sites)
                _tmrw_htc = {a.site_id: a.hours_to_critical for a in _tmrw_assessments}
                _lookahead_penalty = sum(
                    _flow_value_eur(sid, self.config.max_driver_hours)
                    for sid in (tmrw_demand or [])
                    if _tmrw_htc.get(sid, 999.0) < self.config.critical_hours_threshold
                )
                J_wait = _flow_today + _routing_tmrw + _lookahead_penalty
                wait_detail = (
                    f"flow_today={_flow_today:.0f}EUR"
                    f" + routing_tmrw={_routing_tmrw:.0f}EUR"
                    f" + lookahead={_lookahead_penalty:.0f}EUR"
                )
            except Exception as _we:
                logger.warning("[Decision] WAIT simulation failed: %s", _we)
            finally:
                self.sites = saved_sites
                self._virtual_sites = saved_virt

        # ── 4. Decision: argmin J(m) with stability margin for WAIT ──────────

        # Safety fallback: high-priority demand must never disappear due to
        # economic filtering. If critical/warning exists, keep them active.
        _high_priority = [sid for sid in demand_sites if risk_map.get(sid) in ("critical", "warning")]
        if not partial_demand and _high_priority:
            partial_demand = sorted(_high_priority, key=_site_priority_key)
            print(
                f"[Decision] restored {len(partial_demand)} high-priority site(s)"
                " after economic filter emptied active demand"
            )

        if critical_present:
            best_act_mode = "FULL_ACT"
            best_act_demand = full_demand
            best_act_J = J_full
        elif J_partial < J_full:
            best_act_mode = "PARTIAL_ACT"
            best_act_demand = partial_demand
            best_act_J = J_partial
        else:
            best_act_mode = "FULL_ACT"
            best_act_demand = full_demand
            best_act_J = J_full

        _has_critical = any(risk_map.get(sid) == "critical" for sid in demand_sites)
        if wait_eligible and not (horizon_days == 1 and _has_critical) and J_wait < best_act_J * WAIT_THRESHOLD:
            mode = "WAIT"
            active_demand = []
            J_chosen = J_wait
            explanation = (
                f"WAIT: J(WAIT)={J_wait:.0f}EUR ({wait_detail})"
                f" saves >{(1 - WAIT_THRESHOLD)*100:.0f}% vs"
                f" J({best_act_mode})={best_act_J:.0f}EUR."
            )
        elif best_act_mode == "PARTIAL_ACT":
            mode = "PARTIAL_ACT"
            active_demand = partial_demand
            J_chosen = J_partial
            excl_names = [
                self.sites[s].name if s in self.sites else s
                for s, _, _ in excluded
            ]
            explanation = (
                f"PARTIAL_ACT: {len(partial_demand)}/{len(demand_sites)} sites"
                f" (J={J_partial:.0f}EUR < J(FULL)={J_full:.0f}EUR)."
                f" Excluded (flow_value < solo_cost): {excl_names}."
            )
        else:
            mode = "FULL_ACT"
            active_demand = best_act_demand
            J_chosen = J_full
            excl_names_full = [
                self.sites[s].name if s in self.sites else s
                for s, _, _ in excluded
            ]
            explanation = (
                f"FULL_ACT: {len(active_demand)}/{len(demand_sites)} active node(s)"
                f" (J={J_full:.0f}EUR routing_cost)."
            )
            if critical_present:
                explanation += " Economic filtering disabled because critical demand is present."
            elif excluded:
                explanation += f" Filtered out (flow_value < solo_cost): {excl_names_full}."
            if not wait_eligible:
                if urgent_sites:
                    explanation += f" WAIT blocked: {len(urgent_sites)} site(s) critical within 28h."
                elif critical_present:
                    explanation += " WAIT blocked: critical demand present."
                elif waited_this_horizon:
                    explanation += " WAIT blocked: already waited this horizon."
                elif horizon_days <= 1:
                    explanation += " WAIT blocked: single-day horizon."

        print(
            f"[Decision] J(FULL)={J_full:.0f} J(PARTIAL)={J_partial:.0f}"
            f" J(WAIT)={J_wait if J_wait < float('inf') else 'N/A'}"
            f" → {mode}"
        )
        logger.info(
            "[Decision] day=%d J(FULL)=%.0f J(PARTIAL)=%.0f J(WAIT)=%s → %s",
            day, J_full, J_partial,
            f"{J_wait:.0f}" if J_wait < float("inf") else "N/A",
            mode,
        )

        return {
            "mode": mode,
            "active_demand": active_demand,
            "overflow_demand": _overflow_demand,
            "J": J_chosen,
            "J_full": J_full,
            "J_partial": J_partial,
            "J_wait": J_wait,
            "excluded_sites": [s for s, _, _ in excluded],
            "explanation": explanation,
        }

    def _diagnose_infeasibility(
        self,
        demand_sites: List[str],
        trucks: List,
        horizon_days: int,
        allow_transfers: bool,
        fill_remaining_time: bool,
        fleet_config: List[dict] = None,
        fallback_warnings: List[str] = None,
        hours_to_critical_map: dict = None,
    ) -> dict:
        """Identify the root cause of why the solver produced no routes.

        Returns a dict with:
          reason_code   – machine-readable tag
          explanation   – single human-readable sentence stating the root cause
          details       – list of specific findings, each tied to an actual bottleneck
          suggestions   – constraint-aware actions (never mentions disabled features)
        """
        details: List[str] = []
        suggestions: List[str] = []

        max_time_min = self.config.max_driver_hours * 60.0
        service_time_min = self.config.swap_time_hours * 60.0
        total_capacity = sum(t.capacity for t in trucks)

        # ── 1. Distance-matrix completeness ───────────────────────────────────
        missing_matrix = [
            sid for sid in demand_sites
            if sid not in self.distance_matrix
            and not any(sid in row for row in self.distance_matrix.values())
        ]
        if missing_matrix:
            names = [
                (self.sites[sid].name if sid in self.sites else sid)
                for sid in missing_matrix[:5]
            ]
            suffix = f" (+{len(missing_matrix) - 5} more)" if len(missing_matrix) > 5 else ""
            details.append(
                f"Distance matrix missing entries for: {', '.join(names)}{suffix}. "
                "Cannot route to these sites."
            )

        # ── 2. Truck start validity ────────────────────────────────────────────
        bad_starts: List[str] = []
        for truck in trucks:
            start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
            in_dist = start_id in self.distance_matrix
            in_time = (
                self.time_matrix_minutes is None
                or start_id in self.time_matrix_minutes
            )
            if not in_dist and not in_time:
                bad_starts.append(f"{truck.id} (start={start_id!r})")
        if bad_starts:
            details.append(
                f"Truck start position(s) not in routing matrix: {', '.join(bad_starts)}. "
                "VRP solver cannot place these trucks."
            )

        # ── 3. Time feasibility per demand site ───────────────────────────────
        unreachable: List[str] = []
        for sid in demand_sites:
            if sid not in self.sites:
                continue
            reachable = False
            for truck in trucks:
                start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
                # Try road-time matrix first, fall back to distance/speed estimate
                if self.time_matrix_minutes and start_id in self.time_matrix_minutes:
                    t = self.time_matrix_minutes[start_id].get(sid)
                    if t is None and sid in self.time_matrix_minutes:
                        t = self.time_matrix_minutes[sid].get(start_id)
                else:
                    dist = self.distance_matrix.get(start_id, {}).get(sid)
                    if dist is None:
                        dist = self.distance_matrix.get(sid, {}).get(start_id)
                    t = (dist / self.config.avg_speed_kmph * 60.0) if dist is not None else None
                if t is not None and (t + service_time_min) <= max_time_min:
                    reachable = True
                    break
            if not reachable:
                name = self.sites[sid].name if sid in self.sites else sid
                unreachable.append(name)

        if unreachable:
            names_str = ", ".join(unreachable[:5])
            suffix = f" (+{len(unreachable) - 5} more)" if len(unreachable) > 5 else ""
            details.append(
                f"{len(unreachable)} demand site(s) unreachable within "
                f"{self.config.max_driver_hours:.1f}h shift: {names_str}{suffix}."
            )

        # ── 4. Flow-cycle feasibility ─────────────────────────────────────────
        # In the flow-based model trucks reuse container capacity across multiple
        # producer→consumer cycles, so total_capacity < len(demand_sites) is NOT
        # an infeasibility condition (time is the binding constraint, not slots).
        # What IS infeasible is having only producers or only consumers in demand
        # with no matching partner site to complete the swap cycle.
        _n_prod_demand = sum(1 for sid in demand_sites if self.sites.get(sid) and self.sites[sid].is_producer)
        _n_cons_demand = sum(1 for sid in demand_sites if self.sites.get(sid) and self.sites[sid].is_consumer)
        _n_prod_total = sum(1 for site in self.sites.values() if site.is_producer)
        _n_cons_total = sum(1 for site in self.sites.values() if site.is_consumer)
        # NOTE:
        # Consumer-only demand is not automatically infeasible in this system,
        # because the solver receives guaranteed producer hubs separately and can
        # route to them even if they are not in demand_sites.
        if _n_prod_demand == 0 and _n_cons_demand > 0 and _n_prod_total == 0:
            details.append(
                f"Demand contains {_n_cons_demand} consumer site(s), and the network has no producers. "
                "Trucks need at least one producer to load full containers before serving consumers."
            )
        elif _n_cons_demand == 0 and _n_prod_demand > 0 and _n_cons_total == 0:
            details.append(
                f"Demand contains {_n_prod_demand} producer site(s), and the network has no consumers. "
                "Trucks need at least one consumer to deliver full containers and complete the swap cycle."
            )

        # ── 5. Determine root cause and compose explanation ───────────────────
        if missing_matrix:
            reason_code = "MISSING_DISTANCES"
            explanation = (
                f"No feasible plan found: the routing matrix is incomplete for "
                f"{len(missing_matrix)} demand site(s). "
                f"The solver cannot compute paths to these sites."
            )
        elif bad_starts:
            reason_code = "INVALID_TRUCK_STARTS"
            explanation = (
                f"No feasible plan found: {len(bad_starts)} truck(s) have start positions "
                "that are absent from the routing matrix. "
                "Verify custom start coordinates or reset trucks to their home sites."
            )
        elif unreachable and len(unreachable) == len(demand_sites):
            reason_code = "ALL_SITES_UNREACHABLE"
            explanation = (
                f"No feasible plan found: all {len(demand_sites)} demand site(s) are "
                f"unreachable within the {self.config.max_driver_hours:.1f}h shift budget "
                f"(including {service_time_min:.0f} min service time per stop). "
                "Consider extending the shift limit or reducing service time."
            )
            # Only suggest extending horizon if trucks genuinely lack time — not if
            # it would contradict the user's fleet config.
            total_truck_days = (
                sum(tc.get("availability_days", 1) for tc in fleet_config)
                if fleet_config else len(trucks) * horizon_days
            )
            # Time-based estimate: trucks serve ~(shift_hours / service_time_hours) sites/day
            # Container slots are reused across producer→consumer cycles, so capacity is
            # not the binding factor — shift time is.
            _sites_per_truck_day = max(1, int(self.config.max_driver_hours / max(self.config.swap_time_hours, 0.1)))
            _total_sites_per_day = _sites_per_truck_day * max(len(trucks), 1)
            min_days_needed = max(1, len(demand_sites) // max(_total_sites_per_day, 1))
            if total_truck_days < min_days_needed:
                suggestions.append(
                    f"Extend truck availability (currently {total_truck_days} truck-day(s); "
                    f"at least {min_days_needed} needed to serve all demand)."
                )
        elif unreachable:
            reason_code = "PARTIAL_UNREACHABLE"
            explanation = (
                f"No feasible plan found: {len(unreachable)} of {len(demand_sites)} demand site(s) "
                f"could not be reached within the shift budget, leaving no viable multi-stop route."
            )
        else:
            reason_code = "NO_FEASIBLE_ROUTES"
            explanation = (
                f"No feasible plan found for {len(demand_sites)} demand site(s) with "
                f"{len(trucks)} truck(s) over {horizon_days} day(s). "
                "The VRP solver exhausted its time budget without finding a valid route."
            )
            # Constraint-aware fallback suggestions — only mention what is actually possible.
            # Note: capacity < demand_sites is NOT a constraint violation in the flow-based
            # model — trucks reuse container slots across producer→consumer cycles.
            # Suggest extending time or horizon if time is the actual limiting factor.
            total_truck_days = (
                sum(tc.get("availability_days", 1) for tc in fleet_config)
                if fleet_config else len(trucks) * horizon_days
            )
            _sites_per_truck_day = max(1, int(self.config.max_driver_hours / max(self.config.swap_time_hours, 0.1)))
            _total_sites_per_day = _sites_per_truck_day * max(len(trucks), 1)
            min_days_needed = max(1, len(demand_sites) // max(_total_sites_per_day, 1))
            if total_truck_days < min_days_needed:
                suggestions.append(
                    f"Extend truck availability (currently {total_truck_days} truck-day(s); "
                    f"at least {min_days_needed} needed)."
                )

        # ── Constraint-aware guard: never suggest disabled features ───────────
        # allow_transfers=False → container swaps are operator-disabled; do not mention them.
        # fill_remaining_time already True → no need to suggest enabling it.
        # (No positive suggestions about these; they are silently omitted.)

        # ── Build rich diagnostics payload ────────────────────────────────────
        diag_trucks = []
        for truck in trucks:
            start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
            start_name = self.sites[start_id].name if start_id in self.sites else start_id
            _config_initial_load = getattr(truck, "initial_load", 0)
            _actual_current_load = len(getattr(truck, "current_load", []))
            diag_trucks.append({
                "truck_id": truck.id,
                "start_site_id": start_id,
                "start_site_name": start_name,
                "config_initial_load": _config_initial_load,
                "actual_current_load": _actual_current_load,
                "capacity": truck.capacity,
                "availability_days": horizon_days,
            })

        _htc_map = hours_to_critical_map or {}
        diag_demand = []
        for sid in demand_sites:
            if sid not in self.sites:
                continue
            site = self.sites[sid]
            rows: list[dict] = []
            for truck in trucks:
                start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
                dist = self.distance_matrix.get(start_id, {}).get(sid) or self.distance_matrix.get(sid, {}).get(start_id)
                if self.time_matrix_minutes and start_id in self.time_matrix_minutes:
                    drive_min = self.time_matrix_minutes[start_id].get(sid) or (self.time_matrix_minutes.get(sid, {}).get(start_id))
                else:
                    drive_min = (dist / self.config.avg_speed_kmph * 60.0) if dist is not None else None
                rows.append({
                    "truck_id": truck.id,
                    "dist_km": round(dist, 1) if dist is not None else None,
                    "drive_min": round(drive_min, 1) if drive_min is not None else None,
                    "fits_direct": (drive_min + service_time_min) <= max_time_min if drive_min is not None else None,
                })
            # Swappable bays: any bay at consumer that is NOT at FULL_BAR (can be picked up after delivering full)
            _swappable_bays = [b for b in site.bays if b.pressure_bar < self.FULL_BAR]
            _htc = _htc_map.get(sid)
            diag_demand.append({
                "site_id": sid,
                "site_name": site.name,
                "site_type": site.site_type,
                "risk_level": getattr(site, "risk_level", "?"),
                "hours_to_critical": round(_htc, 1) if _htc is not None else None,
                "bays_fixed": site.bays_fixed,
                "swappable_bays": len(_swappable_bays),
                "trucks": rows,
            })

        # Producer hubs (supply source for empty trucks)
        diag_producers = []
        for sid, site in self.sites.items():
            if not site.is_producer:
                continue
            full_bays = [b for b in site.bays if b.pressure_bar >= self.FULL_BAR]
            near_full_bays = [b for b in site.bays if 150 <= b.pressure_bar < self.FULL_BAR]
            if not full_bays and not near_full_bays:
                continue  # nothing to pick up
            dist_rows: list[dict] = []
            for truck in trucks:
                start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
                dist = self.distance_matrix.get(start_id, {}).get(sid) or self.distance_matrix.get(sid, {}).get(start_id)
                if self.time_matrix_minutes and start_id in self.time_matrix_minutes:
                    drive_min = self.time_matrix_minutes[start_id].get(sid) or (self.time_matrix_minutes.get(sid, {}).get(start_id))
                else:
                    drive_min = (dist / self.config.avg_speed_kmph * 60.0) if dist is not None else None
                dist_rows.append({
                    "truck_id": truck.id,
                    "dist_km": round(dist, 1) if dist is not None else None,
                    "drive_min": round(drive_min, 1) if drive_min is not None else None,
                })
            diag_producers.append({
                "site_id": sid,
                "site_name": site.name,
                "full_bays": len(full_bays),
                "near_full_bays": len(near_full_bays),
                "trucks": dist_rows,
            })
        # Sort producers by distance to first truck
        def _prod_dist(p):
            rows = p.get("trucks", [])
            if not rows or rows[0].get("dist_km") is None:
                return 9999
            return rows[0]["dist_km"]
        diag_producers.sort(key=_prod_dist)

        # Flow analysis: for each truck starting empty, estimate min round-trip
        # start → nearest_producer → nearest_consumer → back_to_start
        diag_flow = []
        for truck in trucks:
            if len(getattr(truck, "current_load", [])) > 0:
                diag_flow.append({"truck_id": truck.id, "note": "truck already loaded — no producer stop needed"})
                continue
            start_id = getattr(truck, "effective_start_site_id", None) or truck.home_site_id
            # Find nearest producer with full bays
            best_prod = None
            best_prod_min = None
            for p in diag_producers:
                for row in p["trucks"]:
                    if row["truck_id"] == truck.id and row["drive_min"] is not None:
                        if best_prod_min is None or row["drive_min"] < best_prod_min:
                            best_prod = p
                            best_prod_min = row["drive_min"]
                        break
            if best_prod is None:
                diag_flow.append({"truck_id": truck.id, "note": "no producer with full containers found in network"})
                continue
            # For each demand site, estimate start→producer→consumer→start
            flow_rows = []
            for ds in demand_sites:
                if ds not in self.sites:
                    continue
                prod_id = best_prod["site_id"]
                dist_pc = self.distance_matrix.get(prod_id, {}).get(ds) or self.distance_matrix.get(ds, {}).get(prod_id)
                dist_cs = self.distance_matrix.get(ds, {}).get(start_id) or self.distance_matrix.get(start_id, {}).get(ds)
                if self.time_matrix_minutes:
                    t_sp = best_prod_min
                    t_pc_min = self.time_matrix_minutes.get(prod_id, {}).get(ds) or self.time_matrix_minutes.get(ds, {}).get(prod_id)
                    t_cs_min = self.time_matrix_minutes.get(ds, {}).get(start_id) or self.time_matrix_minutes.get(start_id, {}).get(ds)
                else:
                    t_sp = best_prod_min
                    t_pc_min = (dist_pc / self.config.avg_speed_kmph * 60.0) if dist_pc is not None else None
                    t_cs_min = (dist_cs / self.config.avg_speed_kmph * 60.0) if dist_cs is not None else None
                if t_sp is None or t_pc_min is None or t_cs_min is None:
                    flow_rows.append({
                        "demand_site": self.sites[ds].name,
                        "route": f"start→{best_prod['site_name']}→{self.sites[ds].name}→start",
                        "total_drive_min": None,
                        "service_min": round(service_time_min * 2, 1),  # producer + consumer
                        "total_min": None,
                        "budget_min": round(max_time_min, 1),
                        "feasible": None,
                        "note": "missing distances in matrix",
                    })
                    continue
                total_drive = t_sp + t_pc_min + t_cs_min
                total_with_service = total_drive + service_time_min * 2  # producer + consumer stops
                flow_rows.append({
                    "demand_site": self.sites[ds].name,
                    "route": f"start→{best_prod['site_name']}→{self.sites[ds].name}→start",
                    "total_drive_min": round(total_drive, 1),
                    "service_min": round(service_time_min * 2, 1),
                    "total_min": round(total_with_service, 1),
                    "budget_min": round(max_time_min, 1),
                    "feasible": total_with_service <= max_time_min,
                })
            diag_flow.append({
                "truck_id": truck.id,
                "nearest_producer": best_prod["site_name"],
                "drive_to_producer_min": round(best_prod_min, 1),
                "routes": flow_rows,
            })

        diagnostics_payload = {
            "constraints": {
                "max_driver_hours": self.config.max_driver_hours,
                "max_driver_min": round(max_time_min, 1),
                "service_time_min": round(service_time_min, 1),
                "avg_speed_kmph": self.config.avg_speed_kmph,
            },
            "trucks": diag_trucks,
            "demand_sites": diag_demand,
            "producer_hubs": diag_producers,
            "flow_analysis": diag_flow,
            "fallback_warnings": fallback_warnings or [],
        }

        return {
            "reason_code": reason_code,
            "explanation": explanation,
            "details": details,
            "suggestions": suggestions,
            "diagnostics_payload": diagnostics_payload,
        }

    def get_history(self) -> List[Recommendation]:
        """Get recommendation history."""
        return self._history

    def _compute_total_full(self) -> int:
        """Count total full bays (pressure >= FULL_BAR) across all sites.

        Used for the invariant check: total_full must be conserved across
        an apply_recommendation call (containers are moved, not created/destroyed).
        """
        return sum(
            1 for site in self.sites.values()
            for bay in site.bays
            if bay.pressure_bar >= self.FULL_BAR
        )

    def apply_recommendation(self, rec: Recommendation, save_sites_fn=None) -> None:
        """
        Apply a recommendation's swap operations to canonical site bay pressures.

        self.sites IS the canonical dict (same Python object as loader._sites —
        confirmed at service construction time and never replaced).  All bay
        mutations happen directly on the live Site/Bay Pydantic objects inside
        that dict before save_to_json() is called.

        For each stop with a swap_operation:
        - Picked bays: set pressure_bar to 0 (container leaves the site)
        - Dropped bays at consumer sites: fill an empty bay to 250 bar (full delivery)
        - Dropped bays at producer sites: no-op (empty returnables stay at 0 bar)

        Args:
            save_sites_fn: Optional callback to persist site data BEFORE history
                           is saved (important when uvicorn --reload is active).
        """
        # ── Identity check — confirm self.sites is the canonical in-memory dict ──
        # self.sites was assigned once in __init__ as the same dict object passed
        # by get_recommendation_service() via loader.sites (= loader._sites).
        # generate_recommendation() only REPLACES VALUES inside this dict
        # (self.sites[sid] = snapshot_copy) — it never does self.sites = new_dict.
        # So id(self.sites) is stable across the service's lifetime.
        logger.info(
            "[APPROVE] apply_recommendation start — rec_id=%s  "
            "canonical dict id=%d  site_count=%d",
            rec.id, id(self.sites), len(self.sites),
        )

        # ── Per-bay pressure snapshot BEFORE ─────────────────────────────────
        # Capture every bay pressure so we can diff against it after mutations.
        bay_before: dict = {}   # (site_id, bay_id) -> pressure_bar
        for sid, site in self.sites.items():
            for bay in site.bays:
                bay_before[(sid, bay.bay_id)] = bay.pressure_bar

        total_full_before = self._compute_total_full()
        logger.info(
            "[APPROVE] BEFORE apply — total_full=%d | rec_id=%s",
            total_full_before, rec.id,
        )
        for sid, site in self.sites.items():
            full   = sum(1 for b in site.bays if b.pressure_bar >= self.FULL_BAR)
            partial = sum(1 for b in site.bays if self.EMPTY_BAR < b.pressure_bar < self.FULL_BAR)
            empty  = sum(1 for b in site.bays if b.pressure_bar <= self.EMPTY_BAR)
            logger.info(
                "[APPROVE]   BEFORE  site_id=%-20s  %-30s  full=%d partial=%d empty=%d",
                sid, site.name, full, partial, empty,
            )

        # ── Apply operations day-by-day with interleaved time evolution ─────────
        # For multi-day recommendations, correct physics requires:
        #   apply_day_1_swaps → evolve_24h → apply_day_2_swaps → evolve_24h → ...
        # This matches the planning simulation in generate_recommendation().
        # Single-day recommendations execute exactly one swap+evolve cycle.
        actions_log = []

        # Group routes by day_index (1-based), preserving order within each day.
        from collections import defaultdict
        routes_by_day: dict = defaultdict(list)
        for route in rec.routes:
            routes_by_day[route.day_index].append(route)
        ordered_days = sorted(routes_by_day.keys())

        for day_idx in ordered_days:
            logger.info("[APPROVE] Processing day %d (intra-day time evolution per stop)", day_idx)
            for route in routes_by_day[day_idx]:
                current_time = 0.0
                for stop in route.stops:
                    # Apply time evolution for the segment before this stop arrives
                    delta = stop.arrival_time_hours - current_time
                    if delta > 0:
                        print(f"[TIME] day={day_idx} truck={route.truck_id} delta={delta:.2f}h")
                        self._apply_time_evolution(delta_time_hours=delta)
                    current_time = stop.arrival_time_hours

                    op = stop.swap_operation
                    if not op:
                        continue
                    site = self.sites.get(stop.site_id)
                    if not site:
                        continue

                    # Process picks: bays leaving this site → set to floor (20 bar).
                    for bay_id in op.containers_picked:
                        for bay in site.bays:
                            if bay.bay_id == bay_id:
                                old_p = bay.pressure_bar
                                bay.pressure_bar = self.config.usable_floor_bar
                                actions_log.append((
                                    "PICK", day_idx, stop.site_id, site.name,
                                    bay_id, old_p, self.config.usable_floor_bar,
                                ))
                                break

                    # Process drops: bays arriving at this site.
                    # Only meaningful at consumer sites (full container = 250 bar).
                    #
                    # IMPORTANT: Use <= EMPTY_BAR as the threshold for finding
                    # an available empty slot — NOT == 0.  The swap assignment
                    # considers any bay with pressure <= EMPTY_BAR as "empty/available",
                    # so we must use the same criterion here.
                    if site.is_consumer and op.containers_dropped:
                        empty_bays = sorted(
                            [b for b in site.bays if b.pressure_bar <= self.EMPTY_BAR],
                            key=lambda b: b.bay_id,
                        )
                        filled = 0
                        for bay in empty_bays[: len(op.containers_dropped)]:
                            old_p = bay.pressure_bar
                            bay.pressure_bar = 250
                            filled += 1
                            actions_log.append((
                                "DROP", day_idx, stop.site_id, site.name,
                                bay.bay_id, old_p, 250,
                            ))
                        missed = len(op.containers_dropped) - filled
                        if missed > 0:
                            logger.warning(
                                "[APPROVE] %s day %d: %d container(s) dropped but no empty bay "
                                "found (dropped=%d, empty_slots=%d, EMPTY_BAR=%d bar).",
                                site.name, day_idx, missed, len(op.containers_dropped),
                                len(empty_bays), self.EMPTY_BAR,
                            )

                # Print state after each route completes
                for _sid, _site in self.sites.items():
                    _total_kg = sum(
                        max(0.0, min(pressure_to_kg(250), pressure_to_kg(b.pressure_bar)))
                        for b in _site.bays
                    )
                    print(f"[STATE] {_sid} kg={_total_kg:.0f}")

            logger.info("[APPROVE] Day %d: intra-day evolution complete", day_idx)

        # ── Log every bay mutation with site_id / bay_id / before / after ────
        if actions_log:
            logger.info("[APPROVE] Bay mutations applied (%d):", len(actions_log))
            for op_type, day, site_id, site_name, bay_id, p_before, p_after in actions_log:
                logger.info(
                    "[APPROVE]   %-4s  day=%d  site_id=%-20s  %-28s  bay=%-12s  %3d bar → %3d bar",
                    op_type, day, site_id, site_name, bay_id, p_before, p_after,
                )
        else:
            logger.warning("[APPROVE] No bay mutations applied — recommendation may have no swap ops")

        # ── Verify mutations are visible in self.sites BEFORE save ───────────
        # Re-read each affected bay directly from self.sites and confirm it matches
        # the target pressure.  This proves mutations happened on canonical objects,
        # not on temporary copies.
        verification_errors = 0
        for op_type, day, site_id, site_name, bay_id, p_before, p_after in actions_log:
            site = self.sites.get(site_id)
            if site is None:
                logger.error(
                    "[APPROVE][VERIFY] site_id=%s not found in self.sites — cannot verify",
                    site_id,
                )
                verification_errors += 1
                continue
            bay_obj = next((b for b in site.bays if b.bay_id == bay_id), None)
            if bay_obj is None:
                logger.error(
                    "[APPROVE][VERIFY] bay_id=%s not found in site_id=%s — cannot verify",
                    bay_id, site_id,
                )
                verification_errors += 1
                continue
            actual = bay_obj.pressure_bar
            if actual != p_after:
                logger.error(
                    "[APPROVE][VERIFY] MISMATCH site_id=%-20s bay=%-12s "
                    "expected=%d bar  actual=%d bar — mutation did NOT persist on canonical obj",
                    site_id, bay_id, p_after, actual,
                )
                verification_errors += 1
            else:
                logger.info(
                    "[APPROVE][VERIFY] OK  site_id=%-20s bay=%-12s "
                    "%d→%d bar confirmed in self.sites (canonical)",
                    site_id, bay_id, p_before, actual,
                )
        if verification_errors == 0 and actions_log:
            logger.info(
                "[APPROVE][VERIFY] All %d bay mutations confirmed in self.sites "
                "(canonical dict id=%d) — safe to persist",
                len(actions_log), id(self.sites),
            )
        elif verification_errors > 0:
            logger.error(
                "[APPROVE][VERIFY] %d verification error(s) — "
                "some mutations may not be on canonical objects!",
                verification_errors,
            )

        # ── Per-bay pressure diff AFTER ───────────────────────────────────────
        total_full_after = self._compute_total_full()
        logger.info(
            "[APPROVE] AFTER apply  — total_full=%d | delta=%+d",
            total_full_after, total_full_after - total_full_before,
        )
        for sid, site in self.sites.items():
            full   = sum(1 for b in site.bays if b.pressure_bar >= self.FULL_BAR)
            partial = sum(1 for b in site.bays if self.EMPTY_BAR < b.pressure_bar < self.FULL_BAR)
            empty  = sum(1 for b in site.bays if b.pressure_bar <= self.EMPTY_BAR)
            # Show per-bay changes only for sites that were touched
            touched_bays = [
                (bay.bay_id, bay_before.get((sid, bay.bay_id), bay.pressure_bar), bay.pressure_bar)
                for bay in site.bays
                if bay.pressure_bar != bay_before.get((sid, bay.bay_id), bay.pressure_bar)
            ]
            if touched_bays:
                logger.info(
                    "[APPROVE]   AFTER   site_id=%-20s  %-30s  full=%d partial=%d empty=%d  "
                    "CHANGED bays: %s",
                    sid, site.name, full, partial, empty,
                    ", ".join(f"{b}:{bf}→{ba}" for b, bf, ba in touched_bays),
                )
            else:
                logger.info(
                    "[APPROVE]   AFTER   site_id=%-20s  %-30s  full=%d partial=%d empty=%d  (no change)",
                    sid, site.name, full, partial, empty,
                )

        # Invariant check: total full bays must be conserved across all days.
        if total_full_before != total_full_after:
            logger.error(
                "[APPROVE] INVARIANT VIOLATION: total_full changed %d → %d (delta=%+d). "
                "A container was created or destroyed — check swap logic.",
                total_full_before, total_full_after,
                total_full_after - total_full_before,
            )
        else:
            logger.info(
                "[APPROVE] Invariant OK: total_full conserved at %d", total_full_after
            )

        # ── Persist site data BEFORE saving history ───────────────────────────
        # _save_history writes recommendations.json which can trigger uvicorn --reload,
        # so sites.json must be written first to avoid losing bay mutations.
        if save_sites_fn:
            logger.info(
                "[APPROVE] Calling save_sites_fn() — persisting %d sites "
                "with confirmed bay mutations (dict id=%d)",
                len(self.sites), id(self.sites),
            )
            save_sites_fn()
            logger.info("[APPROVE] save_sites_fn() complete — sites.json updated")

        # ── Final container balance check ─────────────────────────────────────
        # Ensure pickups - deliveries == containers still on trucks (imbalance reported only)
        total_picked = sum(
            len(s.swap_operation.containers_picked)
            for route in rec.routes
            for s in route.stops
            if s.swap_operation
        )
        total_dropped = sum(
            len(s.swap_operation.containers_dropped)
            for route in rec.routes
            for s in route.stops
            if s.swap_operation
        )
        on_trucks = total_picked - total_dropped
        if on_trucks != 0:
            logger.warning("[CHECK] container imbalance: picked=%d dropped=%d on_trucks=%d", total_picked, total_dropped, on_trucks)
        else:
            logger.info("[CHECK] container balance OK: picked=%d dropped=%d on_trucks=0", total_picked, total_dropped)
        print("[CHECK] system validated")

        rec.status = RecommendationStatus.EXECUTED
        self._save_history()

    def approve_recommendation(self, recommendation_id: str) -> Optional[Recommendation]:
        """Mark a recommendation as approved."""
        for rec in self._history:
            if rec.id == recommendation_id:
                rec.status = RecommendationStatus.APPROVED
                self._save_history()
                return rec
        return None

    def reject_recommendation(self, recommendation_id: str) -> Optional[Recommendation]:
        """Mark a recommendation as rejected."""
        for rec in self._history:
            if rec.id == recommendation_id:
                rec.status = RecommendationStatus.REJECTED
                self._save_history()
                return rec
        return None

    def _load_history(self) -> None:
        """Load recommendation history from file."""
        if not RECOMMENDATIONS_FILE.exists():
            return

        try:
            with open(RECOMMENDATIONS_FILE, 'r') as f:
                data = json.load(f)

            self._history = []
            for rec_data in data.get('recommendations', []):
                try:
                    rec = Recommendation.model_validate(rec_data)
                    self._history.append(rec)
                except Exception as e:
                    # Skip invalid recommendations
                    print(f"Warning: Could not load recommendation: {e}")

            print(f"Loaded {len(self._history)} recommendations from history")
        except Exception as e:
            print(f"Warning: Could not load recommendations file: {e}")
            self._history = []

    def _save_history(self) -> None:
        """Save recommendation history to file."""
        try:
            # Keep only the last 50 recommendations to prevent unbounded growth
            recent_history = self._history[-50:] if len(self._history) > 50 else self._history

            data = {
                'recommendations': [
                    rec.model_dump(mode='json') for rec in recent_history
                ]
            }

            # Ensure directory exists
            RECOMMENDATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

            with open(RECOMMENDATIONS_FILE, 'w') as f:
                json.dump(data, f, indent=2, default=str)

        except Exception as e:
            print(f"Warning: Could not save recommendations history: {e}")
