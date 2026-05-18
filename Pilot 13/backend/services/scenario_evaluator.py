"""Scenario evaluation layer: decides WHEN to act (and WHETHER to act) before routing.

The evaluator compares four scenarios:
  ACT_NOW   — route immediately on the current state
  WAIT_12H  — simulate 12 h of consumption/production, then route
  WAIT_24H  — simulate 24 h of consumption/production, then route
  NO_ACTION — do not route; selected only when all routing scenarios fail

Each routing scenario (ACT_NOW / WAIT_12H / WAIT_24H) calls the VRP solver once.
If the solver raises InfeasibleRoutingError, the scenario is marked INVALID.
NO_ACTION is always valid and is the sole fallback when all routing scenarios fail.

  TOTAL_COST(scenario) =
      accumulated_stockout_cost   (EUR accrued while waiting)
    + accumulated_flaring_cost    (EUR accrued while waiting)
    + routing_cost                (real VRP cost on evolved state; inf if infeasible)

The scenario with the lowest TOTAL_COST among VALID scenarios is selected.
If all three routing scenarios are invalid, NO_ACTION is returned.

Hard safety rule:
  If any consumer has htc < URGENT_THRESHOLD_H (5 h), force ACT_NOW regardless
  of cost comparison — waiting is never safe when supply runs out imminently.

Evolution model:
  Consumer:  inventory -= consumption_rate * dt  (floored at 0)
  Producer:  inventory += production_rate  * dt  (capped at 250 bar / max kg)

Penalty model (closed-form, no timestep integration):
  Stockout start:  t_s = usable_kg / consumption_rate
  Flaring  start:  t_f = remaining_capacity_kg / production_rate
  Stockout EUR (consumer, not Takkula):
      early_h = min(wait_h − t_s, STOCKOUT_BREAK_H)  [1 000 EUR/h]
      late_h  = max(0, wait_h − t_s − STOCKOUT_BREAK_H)  [5 000 EUR/h]
  Stockout EUR (Takkula CNG station):
      (wait_h − t_s) × (1 000 000 EUR/day ÷ 24)
  Flaring EUR:
      (wait_h − t_f) × production_rate × KG_TO_MWH × flaring_cost_eur_mwh
"""

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..models import Site, OperationalConfig
from ..utils.conversions import (
    pressure_to_kg,
    kg_to_pressure,
    get_normalized_kg,
    effective_pressure_bar,
)

logger = logging.getLogger(__name__)

# ── Physical / economic constants ─────────────────────────────────────────────
_KG_TO_MWH          = 15.2 / 1000.0    # biogas LHV basis
_USABLE_KG_PER_BAY  = 2697.4           # kg in 20–250 bar range per bay
_STOCKOUT_BREAK_H   = 5.0              # tier boundary (hours of consumer outage)
_STOCKOUT_EARLY_H   = 1_000.0          # EUR/h, first STOCKOUT_BREAK_H of outage
_STOCKOUT_LATE_H    = 5_000.0          # EUR/h, beyond STOCKOUT_BREAK_H
_TAKKULA_EUR_DAY    = 1_000_000.0      # EUR/day for CNG station stockout
_TAKKULA_NAME       = "Takkula"
_MALMI_NAME         = "Malmi"          # pipeline-connected, no flaring

# Below this htc (hours), waiting is prohibited regardless of cost comparison.
URGENT_THRESHOLD_H  = 5.0


def _fmt_cost(v: float) -> str:
    """Format a cost value for logging; shows 'inf' for infeasible scenarios."""
    return "inf" if v == float("inf") else f"{v:.0f}EUR"


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class ScenarioCost:
    """Cost breakdown for a single timing scenario."""
    name: str            # "ACT_NOW" | "WAIT_12H" | "WAIT_24H"
    wait_hours: float    # 0 | 12 | 24
    stockout_cost: float  # EUR accumulated during wait
    flaring_cost: float   # EUR accumulated during wait
    routing_cost: float   # real VRP routing cost on evolved state (inf if infeasible)
    total_cost: float     # stockout + flaring + routing

    # Feasibility verdict — a scenario is invalid when the VRP has no feasible
    # solution or leaves critical sites unserved after the wait period.
    valid: bool = field(default=True)
    invalid_reason: str = field(default="")
    # "" | "INFEASIBLE_VRP_SOLVE" | "CRITICAL_SITE_UNSERVED"

    # Per-site detail (optional, populated for debugging)
    stockout_detail: List[dict] = field(default_factory=list)
    flaring_detail:  List[dict] = field(default_factory=list)


# ── Evaluator ─────────────────────────────────────────────────────────────────

class ScenarioEvaluator:
    """
    Decision layer that evaluates WHEN to act before the VRP solver runs.

    Usage::

        evaluator = ScenarioEvaluator(config)
        best = evaluator.evaluate(
            sites, trucks, distance_matrix, demand_sites, hours_to_critical_map
        )
        # best.name  ∈ {"ACT_NOW", "WAIT_12H", "WAIT_24H", "NO_ACTION"}
        # best.wait_hours  ∈ {0, 12, 24}
        # When best.name == "NO_ACTION": skip VRP entirely (all routing infeasible).
        if best.wait_hours > 0:
            evolved_sites = evaluator.simulate_evolution(sites, best.wait_hours)
            # … rebuild demand / risk maps on evolved_sites, then call VRP
    """

    # Routing scenarios — evaluated in order; each runs a VRP solve.
    SCENARIOS: List[Tuple[str, float]] = [
        ("ACT_NOW",   0.0),
        ("WAIT_12H", 12.0),
        ("WAIT_24H", 24.0),
    ]
    # NO_ACTION is never a VRP scenario.  It is the unconditionally-valid
    # fallback returned when every routing scenario is infeasible.
    NO_ACTION_NAME = "NO_ACTION"

    def __init__(self, config: OperationalConfig) -> None:
        self.config = config
        # Cache: (state_hash, wait_hours) → ScenarioCost
        # Avoids re-running the VRP solve for identical inputs within a session.
        self._scenario_cache: Dict[Tuple[int, float], "ScenarioCost"] = {}
        # All evaluated scenarios from the last evaluate() call (for sensitivity display)
        self.last_all_results: List[ScenarioCost] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def evaluate(
        self,
        sites: Dict[str, Site],
        trucks: list,
        distance_matrix: Dict[str, Dict[str, float]],
        demand_sites: List[str],
        hours_to_critical_map: Dict[str, float],
    ) -> ScenarioCost:
        """
        Evaluate all scenarios and return the one with the lowest total cost.

        Hard safety guard: if any consumer has htc < URGENT_THRESHOLD_H,
        ACT_NOW is returned immediately without evaluating other scenarios.

        Prints [ScenarioEval] lines for every scenario plus a SELECTED line.
        """
        # ── NO_ACTION baseline — always valid, no VRP call, zero routing cost ──
        # This is the unconditional fallback when all routing scenarios are
        # infeasible.  It is never entered into the cost comparison; it is only
        # selected when every other scenario raises InfeasibleRoutingError.
        _no_action = ScenarioCost(
            name=self.NO_ACTION_NAME,
            wait_hours=0.0,
            stockout_cost=0.0,
            flaring_cost=0.0,
            routing_cost=0.0,
            total_cost=0.0,
            valid=True,
            invalid_reason="",
        )

        # ── Hard safety guard ─────────────────────────────────────────────────
        # Force ACT_NOW when any consumer is about to stock out OR any producer
        # is about to flare — both are equally time-critical operational events.
        urgent = [
            sid for sid in demand_sites
            if hours_to_critical_map.get(sid, 999.0) < URGENT_THRESHOLD_H
            and (
                self.sites_is_consumer(sites.get(sid))
                or (sites.get(sid) is not None and getattr(sites[sid], "is_producer", False))
            )
        ]
        if urgent:
            urgent_names = [
                (sites[s].name if s in sites else s) for s in urgent[:3]
            ]
            _urgent_types = [
                ("consumer" if self.sites_is_consumer(sites.get(s)) else "producer")
                for s in urgent[:3]
            ]
            print(
                f"[ScenarioEval] FORCE ACT_NOW — {len(urgent)} site(s) with"
                f" htc < {URGENT_THRESHOLD_H:.0f}h: {list(zip(urgent_names, _urgent_types))}"
            )
            # Still compute ACT_NOW cost for logging completeness
            act_now = self._evaluate_one(
                "ACT_NOW", 0.0, sites, trucks, distance_matrix, demand_sites
            )
            self._log_result(act_now)
            self._log_decision(
                act_now, [act_now, _no_action], "FORCED_URGENT",
                hours_to_critical_map, demand_sites,
            )
            return act_now

        # ── Full evaluation ────────────────────────────────────────────────────
        results: List[ScenarioCost] = []
        for name, wait_h in self.SCENARIOS:
            result = self._evaluate_one(
                name, wait_h, sites, trucks, distance_matrix, demand_sites
            )
            results.append(result)
            self._log_result(result)

        # Store all results for external sensitivity display
        self.last_all_results = list(results)

        # ── Validity filter — never select a scenario that leaves the system
        # infeasible or critical sites unserved after the wait period. ─────────
        valid_results = [r for r in results if r.valid]

        if not valid_results:
            # All routing scenarios invalid — select NO_ACTION so the system
            # always returns a decision.  The operator sees the full invalid
            # comparison table and can intervene manually.
            _no_action_allowed = True
            if self._has_connectable_critical_pair(
                sites=sites,
                trucks=trucks,
                distance_matrix=distance_matrix,
                demand_sites=demand_sites,
                hours_to_critical_map=hours_to_critical_map,
            ):
                _no_action_allowed = False

            if _no_action_allowed:
                best = _no_action
                best.invalid_reason = "NO_ACTION_ALL_ROUTING_INVALID"
                reason = "NO_ACTION_ALL_ROUTING_INVALID"
                print(
                    f"[ScenarioEval] WARNING: all routing scenarios INVALID"
                    f" — selecting NO_ACTION as the safe fallback"
                )
            else:
                _routing_candidates = [r for r in results if r.routing_cost < float("inf")]
                if _routing_candidates:
                    best = min(_routing_candidates, key=lambda r: r.total_cost)
                    reason = "NO_ACTION_BLOCKED_CRITICAL_CONNECTABLE"
                    print(
                        "[ScenarioEval] NO_ACTION blocked: critical producer-consumer"
                        " pair is connectable; forcing best routing scenario."
                    )
                else:
                    best = _no_action
                    best.invalid_reason = "NO_ACTION_ONLY_PHYSICAL_INFEASIBILITY"
                    reason = "NO_ACTION_ONLY_PHYSICAL_INFEASIBILITY"
                    print(
                        "[ScenarioEval] NO_ACTION unavoidable: critical pair connectable"
                        " but all routing scenarios are physically infeasible."
                    )
        else:
            best = min(valid_results, key=lambda r: r.total_cost)
            reason = "LOWEST_TOTAL_COST"

        # ── Consistency assertions ─────────────────────────────────────────────
        if best.name == "NO_ACTION":
            assert best.routing_cost == 0.0, (
                f"[ConsistencyCheck] NO_ACTION must have routing_cost=0,"
                f" got {best.routing_cost}"
            )
        else:
            assert best.routing_cost < float("inf"), (
                f"[ConsistencyCheck] {best.name} selected but routing_cost=inf"
            )

        # Build display list for logging: routing scenarios + NO_ACTION column
        all_display = results + [_no_action]
        self._log_decision(best, all_display, reason, hours_to_critical_map, demand_sites)

        act_now_cost = results[0].total_cost if results else float("inf")
        savings_pct = (
            100.0 * (act_now_cost - best.total_cost) / act_now_cost
            if act_now_cost > 0 and act_now_cost != float("inf") else 0.0
        )
        logger.info(
            "[ScenarioEval] best=%s valid=%s total=%.0fEUR "
            "stockout=%.0f flaring=%.0f routing=%.0f | savings_vs_act_now=%.1f%%",
            best.name,
            best.valid,
            best.total_cost if best.total_cost != float("inf") else -1,
            best.stockout_cost,
            best.flaring_cost,
            best.routing_cost if best.routing_cost != float("inf") else -1,
            savings_pct,
        )
        return best

    def simulate_evolution(
        self, sites: Dict[str, Site], hours: float
    ) -> Dict[str, Site]:
        """
        Simulate gas consumption and production over `hours` on a deep copy.

        Consumer: inventory -= consumption_rate * hours  (floored at 0 bar)
        Producer: inventory += production_rate  * hours  (capped at 250 bar)

        Returns the evolved deep copy; original sites are never mutated.
        """
        if hours <= 0.0:
            return copy.deepcopy(sites)

        evolved = copy.deepcopy(sites)
        max_kg = pressure_to_kg(250)
        min_kg = pressure_to_kg(self.config.usable_floor_bar)

        def _bay_order_key(bay) -> tuple:
            bid = getattr(bay, "bay_id", "") or ""
            digits = "".join(ch for ch in bid if ch.isdigit())
            return (0, int(digits), bid) if digits else (1, 0, bid)

        for site in evolved.values():
            if site.is_consumer:
                rate = site.consumption_rate_kg_hour
                if rate <= 0:
                    continue
                remaining = rate * hours
                # Match operational model: consume one bay at a time.
                for bay in sorted(site.bays, key=_bay_order_key):
                    if remaining <= 0.0:
                        break
                    cur_kg = pressure_to_kg(bay.pressure_bar)
                    available_kg = max(0.0, cur_kg - min_kg)
                    taken = min(available_kg, remaining)
                    new_kg = max(min_kg, cur_kg - taken)
                    bay.pressure_bar = max(0, min(250, round(kg_to_pressure(new_kg))))
                    remaining -= taken

            elif site.is_producer:
                if site.production is None or site.production.effective_kg_per_h is None:
                    continue
                rate = site.production.effective_kg_per_h
                if rate <= 0:
                    continue
                remaining = rate * hours
                # Match operational model: fill one bay at a time.
                for bay in sorted(site.bays, key=_bay_order_key):
                    if remaining <= 0.0:
                        break
                    cur_kg = pressure_to_kg(bay.pressure_bar)
                    space = max_kg - cur_kg
                    if space <= 0.0:
                        continue
                    added = min(space, remaining)
                    new_kg = min(max_kg, cur_kg + added)
                    bay.pressure_bar = max(0, min(250, round(kg_to_pressure(new_kg))))
                    remaining -= added

        return evolved

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def sites_is_consumer(site: Optional[Site]) -> bool:
        """Safe consumer check (handles None)."""
        return site is not None and getattr(site, "is_consumer", False)

    def _has_connectable_critical_pair(
        self,
        sites: Dict[str, Site],
        trucks: list,
        distance_matrix: Dict[str, Dict[str, float]],
        demand_sites: List[str],
        hours_to_critical_map: Dict[str, float],
    ) -> bool:
        """True when at least one critical producer→consumer chain is routable from fleet starts."""
        critical = [
            sid for sid in demand_sites
            if hours_to_critical_map.get(sid, 9999.0) <= self.config.critical_hours_threshold
        ]
        producers = [sid for sid in critical if sid in sites and sites[sid].is_producer]
        consumers = [sid for sid in critical if sid in sites and sites[sid].is_consumer]
        if not producers or not consumers:
            return False

        start_sites = [
            (getattr(t, "effective_start_site_id", None) or getattr(t, "home_site_id", None))
            for t in trucks
        ]
        start_sites = [sid for sid in start_sites if sid]
        if not start_sites:
            return False

        def _finite(a: str, b: str) -> bool:
            d = distance_matrix.get(a, {}).get(b)
            if d is None:
                d = distance_matrix.get(b, {}).get(a)
            return d is not None and d < 9_999

        for p in producers:
            if not any(_finite(s, p) for s in start_sites):
                continue
            for c in consumers:
                if _finite(p, c):
                    return True
        return False

    @staticmethod
    def _compute_state_hash(
        sites: Dict[str, Site],
        trucks: list,
        demand_sites: List[str],
    ) -> int:
        """Deterministic integer hash of the inputs that affect a scenario solve.

        Covers:
          - Bay pressures for all sites in the model (drives evolution outcome)
          - Sorted demand site IDs (determines which nodes the VRP must serve)
          - Truck start-site IDs and capacities (determines routing feasibility)

        Does NOT cover wait_hours — that is added as the second cache key element
        so ACT_NOW / WAIT_12H / WAIT_24H produce distinct keys from the same state.
        """
        site_state = tuple(
            (sid, tuple(b.pressure_bar for b in site.bays))
            for sid, site in sorted(sites.items())
        )
        truck_state = tuple(
            sorted(
                (
                    getattr(t, "id", ""),
                    (
                        getattr(t, "effective_start_site_id", None)
                        or getattr(t, "home_site_id", "")
                    ),
                    getattr(t, "capacity", 0),
                )
                for t in trucks
            )
        )
        return hash((site_state, truck_state, tuple(sorted(demand_sites))))

    def _evaluate_one(
        self,
        name: str,
        wait_hours: float,
        sites: Dict[str, Site],
        trucks: list,
        distance_matrix: Dict[str, Dict[str, float]],
        demand_sites: List[str],
    ) -> ScenarioCost:
        """Compute the total cost for a single scenario using a real VRP solve.

        Results are cached by (state_hash, wait_hours).  A cache hit skips the
        VRP solve entirely and returns the previously computed ScenarioCost.
        """
        # ── Cache lookup ───────────────────────────────────────────────────────
        _state_hash = self._compute_state_hash(sites, trucks, demand_sites)
        _cache_key = (_state_hash, wait_hours)
        if _cache_key in self._scenario_cache:
            cached = self._scenario_cache[_cache_key]
            print(
                f"[ScenarioEval] cache hit: scenario={name}"
                f"  wait_h={wait_hours:.0f}"
                f"  total_cost={_fmt_cost(cached.total_cost)}"
            )
            return cached

        # 1. Accumulated penalties during the wait window (closed-form)
        stockout_eur, flaring_eur, s_detail, f_detail = self._accumulated_costs(
            sites, wait_hours
        )

        # 2. Simulate evolution; recompute demand and htc on evolved state
        if wait_hours > 0:
            evolved = self.simulate_evolution(sites, wait_hours)
            evolved_htc = self._compute_htc(evolved)
            # Expand demand: add sites that became warning/critical after evolution
            new_demand = set(demand_sites) | {
                sid
                for sid, htc in evolved_htc.items()
                if htc <= self.config.warning_hours_threshold
                and sid in sites
            }
            evolved_demand = list(new_demand)
        else:
            evolved = sites
            evolved_htc = self._compute_htc(evolved)
            evolved_demand = demand_sites

        # 3. Real VRP solve on the evolved state — raises InfeasibleRoutingError on failure
        from .vrp_solver import InfeasibleRoutingError  # noqa: PLC0415
        invalid_reason = ""
        routes: List[Any] = []
        try:
            routing_eur, routes = self._routing_cost_vrp(
                evolved, trucks, distance_matrix, evolved_demand, evolved_htc
            )
        except InfeasibleRoutingError as exc:
            print(
                f"[ScenarioEval] scenario={name}"
                f"  status=INVALID  reason=INFEASIBLE_VRP_SOLVE"
                f"  detail={exc}"
            )
            routing_eur = float("inf")
            invalid_reason = "INFEASIBLE_VRP_SOLVE"

        # 4. Feasibility check — unserved critical sites also make the scenario invalid
        if not invalid_reason and routes:
            unserved = self._find_critical_unserved(evolved_htc, evolved_demand, routes)
            if unserved:
                unserved_names = [
                    evolved.get(sid, sites.get(sid)) and
                    (evolved.get(sid) or sites.get(sid)).name or sid
                    for sid in unserved[:3]
                ]
                print(
                    f"[ScenarioEval] scenario={name}"
                    f"  status=INVALID"
                    f"  reason=CRITICAL_SITE_UNSERVED"
                    f"  sites={unserved_names}"
                )
                invalid_reason = "CRITICAL_SITE_UNSERVED"

        valid = not bool(invalid_reason)

        if routing_eur == float("inf"):
            total = float("inf")
        else:
            total = stockout_eur + flaring_eur + routing_eur

        result = ScenarioCost(
            name=name,
            wait_hours=wait_hours,
            stockout_cost=round(stockout_eur, 2),
            flaring_cost=round(flaring_eur, 2),
            routing_cost=routing_eur,
            total_cost=total,
            valid=valid,
            invalid_reason=invalid_reason,
            stockout_detail=s_detail,
            flaring_detail=f_detail,
        )
        self._scenario_cache[_cache_key] = result
        return result

    def _accumulated_costs(
        self, sites: Dict[str, Site], wait_hours: float
    ) -> Tuple[float, float, List[dict], List[dict]]:
        """
        Closed-form integration of stockout and flaring costs over [0, wait_hours].

        Returns (stockout_eur, flaring_eur, stockout_detail, flaring_detail).
        """
        if wait_hours <= 0.0:
            return 0.0, 0.0, [], []

        _floor = self.config.usable_floor_bar
        total_stockout = 0.0
        total_flaring = 0.0
        stockout_detail: List[dict] = []
        flaring_detail:  List[dict] = []

        for sid, site in sites.items():
            usable_kg = sum(
                get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
                for b in site.bays
            )

            # ── Consumer: stockout penalty ─────────────────────────────────────
            if site.is_consumer:
                rate = site.consumption_rate_kg_hour
                if rate <= 0:
                    continue
                t_stockout = usable_kg / rate if usable_kg > 0 else 0.0
                if t_stockout >= wait_hours:
                    continue  # won't run out during the wait window

                outage_h = wait_hours - t_stockout

                if site.name == _TAKKULA_NAME:
                    penalty = outage_h * (_TAKKULA_EUR_DAY / 24.0)
                else:
                    early_h = min(outage_h, _STOCKOUT_BREAK_H)
                    late_h  = max(0.0, outage_h - _STOCKOUT_BREAK_H)
                    penalty = early_h * _STOCKOUT_EARLY_H + late_h * _STOCKOUT_LATE_H

                total_stockout += penalty
                stockout_detail.append({
                    "site_id":    sid,
                    "site_name":  site.name,
                    "usable_kg":  round(usable_kg, 1),
                    "rate_kg_h":  round(rate, 1),
                    "t_stockout": round(t_stockout, 2),
                    "outage_h":   round(outage_h, 2),
                    "penalty":    round(penalty, 2),
                })

            # ── Producer: flaring penalty ──────────────────────────────────────
            elif site.is_producer:
                if site.name == _MALMI_NAME:
                    continue  # pipeline-connected, no flaring risk
                if site.production is None or site.production.effective_kg_per_h is None:
                    continue
                rate = site.production.effective_kg_per_h
                if rate <= 0:
                    continue

                cap_kg = site.bays_fixed * _USABLE_KG_PER_BAY
                remaining_cap = max(0.0, cap_kg - usable_kg)
                t_flare = remaining_cap / rate  # hours until full / flaring starts

                if t_flare >= wait_hours:
                    continue

                flaring_h   = wait_hours - t_flare
                flaring_kg  = rate * flaring_h
                flaring_mwh = flaring_kg * _KG_TO_MWH
                # Use site-specific flaring cost.  Fallback = 0.5 EUR/kg expressed
                # in EUR/MWh so it matches VRPSolver._flow_value_eur's fallback
                # (both use 0.5 EUR/kg when no cost is configured).
                flare_rate  = (
                    site.flaring_cost_eur_mwh
                    if site.flaring_cost_eur_mwh > 0
                    else (0.5 / _KG_TO_MWH)   # ≈ 32.9 EUR/MWh
                )
                penalty     = flaring_mwh * flare_rate

                total_flaring += penalty
                flaring_detail.append({
                    "site_id":        sid,
                    "site_name":      site.name,
                    "usable_kg":      round(usable_kg, 1),
                    "cap_kg":         round(cap_kg, 1),
                    "rate_kg_h":      round(rate, 1),
                    "t_flare":        round(t_flare, 2),
                    "flaring_h":      round(flaring_h, 2),
                    "flaring_mwh":    round(flaring_mwh, 3),
                    "flare_rate_eur_mwh": flare_rate,
                    "penalty":        round(penalty, 2),
                })

        return total_stockout, total_flaring, stockout_detail, flaring_detail

    def _compute_htc(self, sites: Dict[str, Site]) -> Dict[str, float]:
        """
        Compute hours_to_critical for each site (on an already-evolved copy).
        """
        _floor = self.config.usable_floor_bar
        htc_map: Dict[str, float] = {}

        for sid, site in sites.items():
            usable_kg = sum(
                get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
                for b in site.bays
            )

            if site.is_consumer:
                rate = site.consumption_rate_kg_hour
                if rate <= 0:
                    htc_map[sid] = 99999.0
                elif usable_kg <= 0:
                    htc_map[sid] = 0.0
                else:
                    htc_map[sid] = usable_kg / rate

            elif site.is_producer:
                if site.production is None or site.production.effective_kg_per_h is None:
                    htc_map[sid] = 99999.0
                    continue
                rate = site.production.effective_kg_per_h
                if rate <= 0:
                    htc_map[sid] = 99999.0
                    continue
                cap_kg = site.bays_fixed * _USABLE_KG_PER_BAY
                remaining_cap = max(0.0, cap_kg - usable_kg)
                htc_map[sid] = remaining_cap / rate if rate > 0 else 99999.0

        return htc_map

    def _routing_cost_vrp(
        self,
        sites: Dict[str, Site],
        trucks: list,
        distance_matrix: Dict[str, Dict[str, float]],
        demand_sites: List[str],
        htc_map: Dict[str, float],
    ) -> Tuple[float, List[Any]]:
        """
        Run a real VRP solve and return (routing_cost_eur, routes).

        Raises InfeasibleRoutingError when OR-Tools cannot find a feasible
        solution — the caller must catch this and mark the scenario invalid.

        Trucks are deep-copied with initial_load=0 so all scenarios start from
        the same clean baseline — preloading happens in the post-decision solve.
        """
        from .vrp_solver import VRPSolver, InfeasibleRoutingError  # noqa: PLC0415

        if not demand_sites or not trucks:
            return 0.0, []

        # Deep-copy trucks with initial_load=0 for a consistent cross-scenario baseline
        clean_trucks = copy.deepcopy(trucks)
        for t in clean_trucks:
            t.initial_load = 0

        # Build simple risk_map from evolved htc
        _crit_h = self.config.critical_hours_threshold
        _warn_h = self.config.warning_hours_threshold
        risk_map = {
            sid: (
                "critical" if htc_map.get(sid, 9999.0) < _crit_h else
                "warning"  if htc_map.get(sid, 9999.0) < _warn_h  else
                "normal"
            )
            for sid in sites
        }

        solver = VRPSolver(
            sites=sites,
            distance_matrix=distance_matrix,
            config=self.config,
        )
        # InfeasibleRoutingError propagates up to _evaluate_one which catches it.
        routes = solver.solve(
            trucks=clean_trucks,
            demand_sites=demand_sites,
            max_search_seconds=8,
            risk_map=risk_map,
            hours_to_critical_map=htc_map,
            _is_final_day=True,
            planning_horizon_h=float(self.config.max_driver_hours),
        )

        # Route has no total_cost_eur field — compute from distance + config rates.
        # This matches how Recommendation.total_cost_eur is assembled by the
        # recommendation service: transport + handling + contingency.
        _cost_per_km    = self.config.cost_per_km_eur
        _handling_fee   = self.config.handling_fee_eur
        _contingency    = self.config.contingency_multiplier
        _min_billed_km  = self.config.min_billed_km
        total_cost = 0.0
        for r in routes:
            dist_km      = r.total_distance_km
            billed_km    = max(dist_km, _min_billed_km)
            n_stops      = r.num_stops
            route_cost   = (billed_km * _cost_per_km + n_stops * _handling_fee) * _contingency
            total_cost  += route_cost
        return total_cost, routes

    def _find_critical_unserved(
        self,
        htc_map: Dict[str, float],
        demand_sites: List[str],
        routes: List[Any],
    ) -> List[str]:
        """
        Return site IDs that are both critical (htc < threshold) AND not visited
        by any stop in the given routes.

        A site appears "served" if it is the site_id of any RouteStop in any route
        (regardless of sequence — depot start counts as served for producers).
        """
        served = {stop.site_id for route in routes for stop in route.stops}
        _crit_h = self.config.critical_hours_threshold
        return [
            sid for sid in demand_sites
            if htc_map.get(sid, 9999.0) < _crit_h and sid not in served
        ]

    def _routing_cost_estimate(
        self,
        sites: Dict[str, Site],
        trucks: list,
        distance_matrix: Dict[str, Dict[str, float]],
        demand_sites: List[str],
    ) -> float:
        """
        Greedy nearest-truck fleet cost estimate (legacy, kept for reference).

        NOTE: This method is no longer used for scenario selection.
        _routing_cost_vrp() is used instead for accurate comparisons.

        Assigns demand sites to trucks round-robin in distance order.
        Consistent across scenarios; underestimates actual VRP cost but
        preserves relative ordering.
        """
        if not demand_sites or not trucks:
            return 0.0

        # Resolve truck start positions
        positions: Dict[str, Optional[str]] = {}
        for t in trucks:
            start = None
            if hasattr(t, "start") and t.start and getattr(t.start, "site_id", None):
                start = t.start.site_id
            elif hasattr(t, "home_site_id"):
                start = t.home_site_id
            positions[t.id] = start

        remaining = [s for s in demand_sites if s in sites]
        total_cost = 0.0

        while remaining:
            best_tid: Optional[str] = None
            best_sid: Optional[str] = None
            best_d = float("inf")

            for tid, pos in positions.items():
                if pos is None:
                    continue
                for sid in remaining:
                    d = (
                        distance_matrix.get(pos, {}).get(sid)
                        or distance_matrix.get(sid, {}).get(pos)
                        or 0.0
                    )
                    if d < best_d:
                        best_d, best_tid, best_sid = d, tid, sid

            if best_sid is None:
                break

            total_cost += (
                best_d * self.config.cost_per_km_eur
                + self.config.handling_fee_eur
            )
            positions[best_tid] = best_sid
            remaining.remove(best_sid)

        return total_cost * self.config.contingency_multiplier

    def _log_decision(
        self,
        best: "ScenarioCost",
        all_results: "List[ScenarioCost]",
        reason: str,
        htc_map: Dict[str, float],
        demand_sites: List[str],
    ) -> None:
        """
        Print a human-readable decision summary for the operator.

        Format::

            [ScenarioEval] selected=WAIT_12H  reason=LOWEST_TOTAL_COST
            [ScenarioEval] comparison:
            [ScenarioEval]   ACT_NOW   =  1 340 EUR  (VALID)
            [ScenarioEval]   WAIT_12H  =  1 180 EUR  (VALID)  ← selected
            [ScenarioEval]   WAIT_24H  =  3 950 EUR  (INVALID: CRITICAL_SITE_UNSERVED)
            [ScenarioEval] No action recommended. System stable for next 18.3 h.
        """
        # ── Header ─────────────────────────────────────────────────────────────
        print(
            f"[ScenarioEval] selected={best.name}"
            f"  reason={reason}"
        )

        # ── Comparison table ───────────────────────────────────────────────────
        print("[ScenarioEval] comparison:")
        for r in all_results:
            if r.name == self.NO_ACTION_NAME:
                status_tag = "VALID (no routing)"
            elif r.valid:
                status_tag = "VALID"
            else:
                status_tag = f"INVALID: {r.invalid_reason}"
            arrow = "  ← selected" if r.name == best.name else ""
            cost_str = _fmt_cost(r.total_cost)
            print(
                f"[ScenarioEval]   {r.name:<10}= {cost_str:>10}  ({status_tag}){arrow}"
            )

        # ── Operator message ───────────────────────────────────────────────────
        if best.wait_hours > 0:
            # Minimum htc across all demand sites: how long until the next site
            # becomes critical (consumer stockout or producer overflow).
            min_htc = min(
                (htc_map[sid] for sid in demand_sites if sid in htc_map),
                default=9999.0,
            )
            stable_h = max(0.0, min_htc)
            print(
                f"[ScenarioEval] No action recommended."
                f" System stable for next {stable_h:.1f} h."
            )

    @staticmethod
    def _log_result(r: ScenarioCost) -> None:
        status = "VALID" if r.valid else f"INVALID  reason={r.invalid_reason}"
        print(
            f"[ScenarioEval]"
            f" scenario={r.name}"
            f"  status={status}"
            f"  wait_h={r.wait_hours:.0f}"
            f"  stockout_cost={_fmt_cost(r.stockout_cost)}"
            f"  flaring_cost={_fmt_cost(r.flaring_cost)}"
            f"  routing_cost={_fmt_cost(r.routing_cost)}"
            f"  total_cost={_fmt_cost(r.total_cost)}"
        )
        # Per-site stockout detail
        for d in r.stockout_detail:
            print(
                f"[ScenarioEval]   stockout  {d['site_name']:<22}"
                f" usable={d['usable_kg']:.0f}kg"
                f" rate={d['rate_kg_h']:.1f}kg/h"
                f" t_event={d['t_stockout']:.1f}h"
                f" outage={d['outage_h']:.1f}h"
                f" penalty={d['penalty']:.0f}EUR"
            )
        # Per-site flaring detail
        for d in r.flaring_detail:
            print(
                f"[ScenarioEval]   flaring   {d['site_name']:<22}"
                f" remaining={d['cap_kg'] - d['usable_kg']:.0f}kg"
                f" rate={d['rate_kg_h']:.1f}kg/h"
                f" t_event={d['t_flare']:.1f}h"
                f" flaring={d['flaring_h']:.1f}h"
                f" penalty={d['penalty']:.0f}EUR"
            )
