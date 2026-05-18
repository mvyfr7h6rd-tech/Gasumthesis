"""VRP Solver using OR-Tools with swap operation support.

Open-Route Model (multi-day rolling horizon)
─────────────────────────────────────────────
Each daily solve uses an "open route" formulation:
  - Trucks start at their current position (carried forward from previous day).
  - Trucks end at the last site they visited — no forced return to the start depot.
  - A zero-cost virtual "end" node allows OR-Tools to terminate any vehicle at
    any real node without adding a return-trip penalty.

This is correct for multi-day logistics where trucks can reposition overnight.
The full daily time budget (config.max_driver_hours) is used for outbound travel,
not split between outbound and a forced return trip.

Pure Economic Objective
───────────────────────
All costs are expressed in EUR (integer cents).  No heuristic discounts, bonuses,
or artificial incentives are used.  The solver minimises:

  TOTAL_COST =
    transport_cost     — dist_km × cost_per_km × contingency × COST_SCALE
  + handling_cost      — handling_fee_eur per service stop
  + stockout_cost      — time-dependent penalty on consumer arrival after htc
  + flaring_cost       — time-dependent penalty on producer arrival after htc
  + imbalance_cost     — soft penalty for containers not returned by end of horizon
  + overtime_cost      — soft penalty for shift overrun

Arc cost: dist_km × cost_per_km × contingency — no subtraction or discount.
Missing matrix entries return a prohibitive cost (1 000 000 EUR), effectively
forbidding those arcs.

Stockout penalty (consumers, time-dependent via SetCumulVarSoftUpperBound):
  Takkula (CNG station):   1 000 000 EUR/day ≈ 694 EUR/min
  Other, htc ≤ 5h (late):   5 000 EUR/h   ≈  83 EUR/min
  Other, htc >  5h (early):  1 000 EUR/h   ≈  17 EUR/min
  penalty = rate × max(0, arrival_min − htc_min)

Flaring penalty (producers, time-dependent via SetCumulVarSoftUpperBound):
  rate = production_mwh_h × flaring_cost_eur_mwh × 2.0
  floor = 3 000 c/min (30 EUR/min)
  penalty = rate × max(0, arrival_min − htc_min)

Disjunction penalty (skip cost):
  = real economic loss over the planning horizon (flow_value_eur × COST_SCALE)
  ≥ handling_fee_eur (operational floor)

Disjunctions are wrapped per demand node so the solver can drop infeasible sites
while still returning a best-effort plan rather than "no solution".
"""

import logging
import math
from typing import Dict, List, Optional

from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from ..models import (
    Site,
    Container,
    Truck,
    OperationalConfig,
    Route,
    RouteStop,
    SwapOperation,
)
from ..utils.conversions import get_normalized_kg, effective_pressure_bar, pressure_to_kg

logger = logging.getLogger(__name__)


class InfeasibleRoutingError(Exception):
    """Raised by VRPSolver.solve() when OR-Tools cannot find any feasible solution.

    The caller (ScenarioEvaluator) must catch this and mark the scenario as
    INVALID rather than falling back silently.  The solver never swallows this
    condition internally — all retry / fallback logic belongs at the decision layer.
    """


class VRPSolver:
    """
    Vehicle Routing Problem solver for biogas container logistics.

    Uses Google OR-Tools with:
    - Open-route formulation (trucks end anywhere; no forced depot return).
    - Capacity constraints (truck container limits).
    - Per-day time constraints (max driver hours, reset each solve call).
    - Fixed service time per stop (swap_time_hours from config).
    - Flow-value disjunction penalties (real economic cost per dropped node).
    - Raises InfeasibleRoutingError when OR-Tools finds no solution; the
      ScenarioEvaluator (decision layer) handles fallback to NO_ACTION.
    """

    # ── Cost scaling ──────────────────────────────────────────────────────────
    # Arc cost callback returns integer cents (EUR × 100).  All penalties use
    # the same unit so the solver compares everything on one monetary scale.
    COST_SCALE = 100  # cents per EUR

    # ── Unit conversion ───────────────────────────────────────────────────────
    _KG_TO_MWH = 15.2 / 1000.0   # kg of biogas → MWh (LHV basis)

    # ── Stockout penalty rates (EUR/hour of consumer outage) ─────────────────
    # Applied only to Takkula (CNG station policy rate) and as the rate basis
    # for consumer penalty computation.
    STOCKOUT_RATE_EARLY_EUR_H = 1_000   # first STOCKOUT_BREAK_HOURS of outage
    STOCKOUT_RATE_LATE_EUR_H  = 5_000   # beyond STOCKOUT_BREAK_HOURS
    STOCKOUT_BREAK_HOURS      = 5.0     # tier boundary (hours of outage)

    # ── Site names with special penalty rules ─────────────────────────────────
    TAKKULA_SITE_NAME = "Takkula"   # CNG station: absolute priority consumer
    MALMI_SITE_NAME   = "Malmi"     # pipeline-connected: no flaring risk

    # ── Malmi buffer-hub imbalance cost ───────────────────────────────────────
    # Malmi has no flaring risk (pipeline) but acts as a container buffer hub.
    # Skip penalty = MALMI_IMBALANCE_K_EUR_PER_KG × |supply_kg − demand_kg|
    #   supply_kg  = usable kg in "full" bays (pressure ≥ 200 bar)
    #   demand_kg  = kg deficit in "empty" bays (pressure < 50 bar, vs 250 bar max)
    # Calibrated so that 1 full bay of imbalance (≈ 2697 kg) → 40 EUR (≈ handling_fee).
    # Zero penalty when supply and demand are matched; scales continuously with kg
    # rather than counting discrete bays.
    MALMI_IMBALANCE_K_EUR_PER_KG: float = 40.0 / 2697.4  # ≈ 0.01483 EUR/kg

    # ── Vehicle activation cost ────────────────────────────────────────────────
    # One-time fixed cost charged per used vehicle (OR-Tools SetFixedCostOfVehicle).
    # Represents real mobilisation overhead (driver daily fixed wage + truck fixed cost).
    # Steers the solver toward fewer trucks when demand is sparse without blocking
    # genuine economic need for extra vehicles.
    #
    # Sizing rule:
    #   • Must be LARGE enough to prevent spurious single-stop routes that barely
    #     cover their own handling cost.
    #   • Must be SMALL enough that a truck serving 1–2 critical sites is always
    #     deployed (critical-site disjunction penalties are 1 000–50 000 EUR,
    #     so anything below ~500 EUR never blocks a critical dispatch).
    #   • 350 EUR ≈ 8–9 handling fees — calibrated to Finnish daily truck mobilisation
    #     cost.  This is the single authoritative value used by SetFixedCostOfVehicle.
    VEHICLE_ACTIVATION_COST_EUR: float = 350.0  # EUR; single source of truth

    # ── Penalty cap ────────────────────────────────────────────────────────────
    # Hard ceiling on the total EUR value of any single site's soft time-window
    # penalty.  Prevents one extreme-priority site (e.g. Takkula at 694 EUR/min)
    # from monopolising the solver's budget and leaving other critical sites
    # unserved when the fleet is small.
    #
    # 50 000 EUR >> any realistic route cost (~1 000 EUR) so urgency is still
    # strongly expressed, but << 1 M EUR/day (uncapped Takkula) which can
    # produce a total-objective value that dwarfs all other terms combined.
    #
    # The cap is applied to the CPM rate used in SetCumulVarSoftUpperBound:
    #   capped_cpm = min(raw_cpm, cap_cents / max_delay_minutes)
    # so the total penalty never exceeds PENALTY_CAP_EUR regardless of delay.
    PENALTY_CAP_EUR: float = 50_000.0

    # ── Usable kg capacity per bay ────────────────────────────────────────────
    USABLE_KG_PER_BAY = 2697.4
    # Hard daily flaring limit derived from the 2 h/week regulatory cap.
    MAX_FLARING_PER_DAY_H = 2.0 / 7.0   # ≈ 0.286 h per planning day
    # Penalty escalation multiplier when a producer exceeds the daily flaring limit.
    FLARING_LIMIT_PENALTY_FACTOR = 20

    def __init__(
        self,
        sites: Dict[str, Site],
        distance_matrix: Dict[str, Dict[str, float]],
        config: OperationalConfig,
        time_matrix_minutes: Optional[Dict[str, Dict[str, float]]] = None,
        allow_symmetric_fallback: bool = True,
    ):
        self.sites = sites
        self.distance_matrix = distance_matrix
        self.config = config
        self.time_matrix_minutes = time_matrix_minutes
        self.allow_symmetric_fallback = allow_symmetric_fallback

        self.site_ids = list(sites.keys())
        self.site_index = {sid: i for i, sid in enumerate(self.site_ids)}
        self.num_sites = len(self.site_ids)

        self._distance_matrix = self._build_distance_matrix()
        self._time_matrix = self._build_time_matrix()
        self._traffic_time_multiplier = 1.0
        self._verify_connectivity()

        # Hot-start: stores previous solution route assignments so the next
        # call can use them as an OR-Tools first-solution hint.
        # Format: list[v] = [site_id, ...] for each vehicle index v.
        self._last_hint_routes: Optional[List[List[str]]] = None

        # Tracks OR-Tools node indices for which AddDisjunction was called.
        # Populated by _safe_add_disjunction() inside solve(); used by
        # _debug_infeasibility() to check coverage.  Reset at each solve() entry.
        self._nodes_with_disjunction: set = set()

    # ── Penalty helper ────────────────────────────────────────────────────────

    def _compute_site_penalty(
        self, site_obj: Site, hours_to_critical: float,
        horizon_h: Optional[float] = None,
    ) -> int:
        """Compute disjunction penalty in cents for not serving a site today.

        Returns the real economic loss of skipping this site over the planning
        horizon.  No artificial tier floors — all values derived from real
        operational cost rates.

        horizon_h: planning window for flow value computation.
          Defaults to max_driver_hours (9h) for backward compatibility, but
          the disjunction loop MUST pass planning_horizon_h so that sites with
          htc > max_driver_hours (not critical within the shift but critical
          within the planning window) receive a meaningful penalty.

        Special rules:
          Malmi  — pipeline-connected, never flares: penalty = 0.
          Takkula — CNG station: full-horizon stockout at late tier rate.
          Consumers: tiered stockout on overflow within horizon.
          Producers: overflow_kg × KG_TO_MWH × flaring_cost × 2.0.

        Minimum penalty = handling_fee_eur (operational floor).
        """
        _handling_floor = int(self.config.handling_fee_eur * self.COST_SCALE)
        if site_obj is None:
            return _handling_floor

        if site_obj.name == self.MALMI_SITE_NAME:
            return 0  # pipeline-connected: no flaring risk

        _horizon = horizon_h if horizon_h is not None else self.config.max_driver_hours
        _Dt_h = self._compute_Dt(hours_to_critical, _horizon)
        flow_eur = self._flow_value_eur(site_obj, _Dt_h)
        if flow_eur <= 0.0:
            return 0
        return int(flow_eur * self.COST_SCALE)

    # ── Penalty cap helpers ───────────────────────────────────────────────────

    @staticmethod
    def _cap_penalty_cents(penalty_eur: float) -> int:
        """Return penalty_eur capped at PENALTY_CAP_EUR, converted to cents."""
        return int(min(penalty_eur, VRPSolver.PENALTY_CAP_EUR) * VRPSolver.COST_SCALE)

    @staticmethod
    def _capped_cpm(raw_cpm_cents: int, max_delay_minutes: int) -> int:
        """Return a CPM rate that guarantees total penalty ≤ PENALTY_CAP_EUR.

        SetCumulVarSoftUpperBound accumulates raw_cpm × delay_minutes in the
        objective.  To cap the worst-case total at PENALTY_CAP_EUR we need:
            raw_cpm × max_delay ≤ cap_cents
        so the effective rate is min(raw_cpm, cap_cents / max_delay).

        Args:
            raw_cpm_cents: Uncapped penalty rate in cents/minute.
            max_delay_minutes: Maximum possible delay (e.g. max_driver_hours × 60).

        Returns:
            Capped rate in cents/minute (≥ 1 cent/min to avoid zero).
        """
        if max_delay_minutes <= 0:
            return raw_cpm_cents
        cap_cents = int(VRPSolver.PENALTY_CAP_EUR * VRPSolver.COST_SCALE)
        return min(raw_cpm_cents, max(1, cap_cents // max_delay_minutes))

    @staticmethod
    def log_objective_breakdown(cost_breakdown: dict) -> None:
        """Print a structured breakdown of VRP objective components.

        Args:
            cost_breakdown: dict with keys transport_eur, handling_eur,
                stockout_penalty_eur, flaring_penalty_eur, imbalance_penalty_eur.
                Missing keys default to 0.
        """
        transport  = cost_breakdown.get("transport_eur", 0.0)
        handling   = cost_breakdown.get("handling_eur", 0.0)
        stockout   = cost_breakdown.get("stockout_penalty_eur", 0.0)
        flaring    = cost_breakdown.get("flaring_penalty_eur", 0.0)
        imbalance  = cost_breakdown.get("imbalance_penalty_eur", 0.0)
        total      = transport + handling + stockout + flaring + imbalance

        print("\n[ObjectiveBreakdown]")
        print(f"  transport : {transport:>10.2f} EUR")
        print(f"  handling  : {handling:>10.2f} EUR")
        print(f"  stockout  : {stockout:>10.2f} EUR")
        print(f"  flaring   : {flaring:>10.2f} EUR")
        print(f"  imbalance : {imbalance:>10.2f} EUR")
        print(f"  TOTAL     : {total:>10.2f} EUR\n")

    # ── Matrix helpers ────────────────────────────────────────────────────────

    def _resolve_truck_start(self, truck) -> str:
        """Return the best known start site_id for a truck.

        If the truck's effective start is not present in the routing matrix
        (e.g. it was set to a container ID or stale custom-point), fall back to
        home_site_id so time lookups don't raise KeyError / ValueError.
        """
        start_id = truck.effective_start_site_id or truck.home_site_id
        # Check whether start_id is reachable from the routing matrix
        if self.time_matrix_minutes is not None:
            if start_id not in self.time_matrix_minutes:
                fallback = truck.home_site_id
                if fallback != start_id:
                    print(
                        f"[VRP] Truck {truck.id} start={start_id!r} not in routing matrix;"
                        f" falling back to home={fallback!r}"
                    )
                return fallback
        elif start_id not in self.distance_matrix:
            fallback = truck.home_site_id
            if fallback != start_id:
                print(
                    f"[VRP] Truck {truck.id} start={start_id!r} not in distance matrix;"
                    f" falling back to home={fallback!r}"
                )
            return fallback
        return start_id

    # Sentinel distance (km) stored in the matrix for missing entries.
    # The arc callback detects this sentinel and returns _VERY_LARGE_COST_CENTS,
    # making the solver treat missing-entry arcs as effectively forbidden.
    _MISSING_DIST_KM = 9_999.0
    # Corresponding cost returned by the arc callback for missing-entry arcs.
    # 1 000 000 EUR >> any real route cost; solver will never voluntarily use it.
    _VERY_LARGE_COST_CENTS = 100_000_000  # 1 000 000 EUR in cents

    # Road-network circuity factor applied to haversine straight-line distances.
    # 1.3 is the empirical median for Finnish inter-city routing (highways dominate).
    _HAVERSINE_ROAD_FACTOR = 1.3

    # Maximum soft penalty in cents (25 000 EUR).  Used instead of VERY_LARGE_COST
    # for disjunction penalties to prevent hard infeasibility during construction.
    _MAX_SOFT_PENALTY_CENTS = 500_000 * 100  # 50 000 000 cents = 500 000 EUR

    # Maximum containers per truck — used as a fixed _FLOW_OFFSET so the
    # capacity dimension is consistent across solves regardless of fleet size.
    MAX_FLEET_CAPACITY = 3

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in km between two WGS-84 coordinates."""
        R = 6_371.0
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = (math.sin(d_lat / 2) ** 2
             + math.cos(math.radians(lat1))
             * math.cos(math.radians(lat2))
             * math.sin(d_lon / 2) ** 2)
        return R * 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, a))))

    def _haversine_road_km(self, from_id: str, to_id: str) -> Optional[float]:
        """Return haversine × road-factor km if both sites have coordinates, else None."""
        fs = self.sites.get(from_id)
        ts = self.sites.get(to_id)
        if (fs is None or ts is None
                or not hasattr(fs, 'latitude') or not hasattr(ts, 'latitude')):
            return None
        if None in (fs.latitude, fs.longitude, ts.latitude, ts.longitude):
            return None
        hav = self._haversine_km(fs.latitude, fs.longitude, ts.latitude, ts.longitude)
        return max(0.1, hav * self._HAVERSINE_ROAD_FACTOR)  # floor: never 0

    @staticmethod
    def _sanitize(
        dist_ext: List[List[int]],
        time_ext: List[List[float]],
        coords: List[Optional[tuple]],
        speed_kmh: float,
    ) -> int:
        """Patch sentinel entries in dist_ext/time_ext using haversine × 1.3.

        Any pair (i, j) with dist_ext[i][j] >= 9_000_000 metres (9 000 km) is
        treated as missing road data.  If both endpoints have coordinates the
        straight-line haversine distance × 1.3 road factor is substituted.
        Returns the number of pairs patched.
        """
        from math import radians, sin, cos, sqrt, atan2
        R = 6_371.0
        _sentinel = 9_000_000  # metres — below real MISSING_DIST but >> any Finnish route
        _patched = 0
        n = len(coords)
        for i in range(n):
            for j in range(n):
                if i == j or dist_ext[i][j] < _sentinel:
                    continue
                a, b = coords[i], coords[j]
                if not a or not b:
                    continue
                lat1, lon1 = radians(a[0]), radians(a[1])
                lat2, lon2 = radians(b[0]), radians(b[1])
                dlat, dlon = lat2 - lat1, lon2 - lon1
                h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
                km = 2 * R * atan2(sqrt(max(0.0, h)), sqrt(max(0.0, 1 - h))) * 1.3
                dist_ext[i][j] = int(km * 1000)
                time_ext[i][j] = (km / max(1.0, speed_kmh)) * 60.0
                _patched += 1
        return _patched

    @staticmethod
    def _compute_Dt(htc: float, planning_horizon_h: float) -> float:
        """Exposure window for flow value computation.

        Returns planning_horizon_h unconditionally. Crisis timing is handled
        inside _flow_value_eur via htc_actual (usable_kg / rate), so capping
        Dt at htc here would always yield shortage = 0 (since htc = usable/rate
        by construction in the risk calculator).
        """
        return planning_horizon_h

    def _flow_value_eur(self, site_obj: "Site", Dt_h: float) -> float:
        """Raw economic flow value (EUR) for skipping site_obj over Δt hours.

        Does NOT apply the handling_fee floor — call _compute_site_penalty() for that.

        This is the single authoritative penalty computation path for:
          • _compute_site_penalty  (disjunction penalty with floor)
          • _node_flow_cents loop  (arc-discount flow value, no floor)

        Malmi special rule:
          Malmi is pipeline-connected so flaring cost = 0, but it is a buffer hub
          whose skip penalty is the kg-based container imbalance cost:
            MALMI_IMBALANCE_K_EUR_PER_KG × |supply_kg − demand_kg|
          supply_kg  = usable kg in bays with pressure ≥ 200 bar
          demand_kg  = kg deficit in bays with pressure < 50 bar (vs 250 bar max)
          Zero when supply and demand are matched; scales continuously with kg.
        """
        if site_obj is None:
            return 0.0
        if site_obj.name == self.MALMI_SITE_NAME:
            _floor = self.config.usable_floor_bar
            # supply_kg: usable gas in "full" bays — available for delivery
            _supply_kg = sum(
                get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
                for b in site_obj.bays
                if b.pressure_bar >= 200
            )
            # demand_kg: unfilled capacity in "empty" bays — space to receive gas
            _demand_kg = sum(
                pressure_to_kg(250) - pressure_to_kg(b.pressure_bar)
                for b in site_obj.bays
                if b.pressure_bar < 50
            )
            _imbalance_kg = abs(_supply_kg - _demand_kg)
            return _imbalance_kg * self.MALMI_IMBALANCE_K_EUR_PER_KG

        if site_obj.name == self.TAKKULA_SITE_NAME:
            # CNG station: full-horizon stockout at late-tier rate
            return Dt_h * self.STOCKOUT_RATE_LATE_EUR_H

        _floor = self.config.usable_floor_bar
        _usable_kg = sum(
            get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
            for b in site_obj.bays
        )

        if site_obj.is_consumer:
            cons_rate = site_obj.consumption_rate_kg_hour or 0.0
            if cons_rate <= 0.0:
                return 0.0
            shortage_kg = max(0.0, cons_rate * Dt_h - _usable_kg)
            return shortage_kg * 2.0  # €/kg high penalty

        if site_obj.is_producer:
            prod_kg_h = 0.0
            if site_obj.production:
                if site_obj.production.effective_kg_per_h:
                    prod_kg_h = site_obj.production.effective_kg_per_h
                elif site_obj.production.effective_mwh_per_h:
                    prod_kg_h = site_obj.production.effective_mwh_per_h / self._KG_TO_MWH
            if prod_kg_h <= 0.0:
                return 0.0
            _cap_kg = site_obj.bays_fixed * self.USABLE_KG_PER_BAY
            free_capacity_kg = max(0.0, _cap_kg - _usable_kg)
            overflow_kg = max(0.0, prod_kg_h * Dt_h - free_capacity_kg)
            # Use site-specific flaring cost (EUR/MWh → EUR/kg via LHV basis).
            # Fallback to 0.5 EUR/kg only when no flaring cost is configured.
            _flaring_eur_per_kg = (
                site_obj.flaring_cost_eur_mwh * self._KG_TO_MWH
                if site_obj.flaring_cost_eur_mwh > 0
                else 0.5
            )
            return overflow_kg * _flaring_eur_per_kg

        return 0.0

    def _lookup_distance(self, from_id: str, to_id: str) -> float:
        if from_id in self.distance_matrix:
            val = self.distance_matrix[from_id].get(to_id)
            if val is not None:
                return val
        if self.allow_symmetric_fallback:
            if to_id in self.distance_matrix:
                val = self.distance_matrix[to_id].get(from_id)
                if val is not None:
                    return val
        # ── Haversine fallback ────────────────────────────────────────────────
        # Road-routing matrix doesn't cover this pair.  Use straight-line ×
        # circuity factor so the solver can still route through the site rather
        # than treating it as permanently unreachable (VERY_LARGE_COST).
        hav_km = self._haversine_road_km(from_id, to_id)
        if hav_km is not None:
            logger.debug(
                "[HaversineFallback] %s→%s: %.1fkm (road matrix missing)",
                from_id, to_id, hav_km,
            )
            return hav_km
        if not self.allow_symmetric_fallback:
            raise ValueError(
                f"Missing routing pair {from_id}->{to_id}. "
                "Do not assume symmetry with road routing."
            )
        logger.debug(
            "Missing distance matrix entry %s→%s and no coordinates — sentinel %g km",
            from_id, to_id, self._MISSING_DIST_KM,
        )
        return self._MISSING_DIST_KM

    def _build_distance_matrix(self) -> List[List[int]]:
        _fallback = 0
        _sentinel = 0
        matrix = []
        for from_id in self.site_ids:
            row = []
            for to_id in self.site_ids:
                if from_id == to_id:
                    row.append(0)
                    continue
                # Check matrix directly first to detect fallback usage
                _in_matrix = (
                    (from_id in self.distance_matrix
                     and self.distance_matrix[from_id].get(to_id) is not None)
                    or (self.allow_symmetric_fallback
                        and to_id in self.distance_matrix
                        and self.distance_matrix[to_id].get(from_id) is not None)
                )
                dist = self._lookup_distance(from_id, to_id)
                if not _in_matrix:
                    if dist < self._MISSING_DIST_KM:
                        _fallback += 1
                    else:
                        _sentinel += 1
                row.append(int(dist * 1000))  # km → metres
            matrix.append(row)
        if _fallback or _sentinel:
            print(
                f"[HaversineFallback] distance matrix: {_fallback} pair(s) filled"
                f" from haversine, {_sentinel} pair(s) still sentinel (no coordinates)"
            )
        return matrix

    def _lookup_time(self, from_id: str, to_id: str) -> float:
        if self.time_matrix_minutes is not None:
            if from_id in self.time_matrix_minutes:
                val = self.time_matrix_minutes[from_id].get(to_id)
                if val is not None:
                    return val
            if self.allow_symmetric_fallback:
                if to_id in self.time_matrix_minutes:
                    val = self.time_matrix_minutes[to_id].get(from_id)
                    if val is not None:
                        return val
            # Time matrix missing: derive from haversine road distance if available,
            # otherwise fall through to the distance-based path below.
            hav_km = self._haversine_road_km(from_id, to_id)
            if hav_km is not None:
                return hav_km / self.config.avg_speed_kmph * 60
            if not self.allow_symmetric_fallback:
                raise ValueError(
                    f"Missing routing pair {from_id}->{to_id}. "
                    "Do not assume symmetry with road routing."
                )
            return self._MISSING_DIST_KM / self.config.avg_speed_kmph * 60
        dist_km = self._lookup_distance(from_id, to_id)
        return dist_km / self.config.avg_speed_kmph * 60

    def _build_time_matrix(self) -> List[List[int]]:
        matrix = []
        for from_id in self.site_ids:
            row = []
            for to_id in self.site_ids:
                t = 0 if from_id == to_id else int(self._lookup_time(from_id, to_id))
                row.append(t)
            matrix.append(row)
        return matrix

    def _scaled_travel_minutes(
        self,
        from_id: str,
        to_id: str,
        traffic_time_multiplier: float = 1.0,
        effective_speed_kmph: Optional[float] = None,
    ) -> int:
        """Return travel minutes, never faster than distance / effective speed.

        Road-time matrices can occasionally imply unrealistically optimistic
        timings. Clamp every arc to the configured average-speed envelope so
        routes remain physically plausible for trucks.
        """
        if from_id == to_id:
            return 0

        multiplier = traffic_time_multiplier if traffic_time_multiplier > 0 else 1.0
        effective_speed = effective_speed_kmph
        if effective_speed is None or effective_speed <= 0:
            effective_speed = self.config.avg_speed_kmph / multiplier
        effective_speed = max(1.0, effective_speed)

        base_minutes = self._lookup_time(from_id, to_id) * multiplier
        min_minutes_by_speed = (self._lookup_distance(from_id, to_id) / effective_speed) * 60.0
        return int(math.ceil(max(base_minutes, min_minutes_by_speed)))

    def _verify_connectivity(self) -> None:
        """Log any site that has no valid distance to/from any other site.

        Called once at init time after both matrices are built.  A site with
        all-sentinel rows/columns has no coordinates and is absent from the road
        matrix — it will be permanently unreachable and should be investigated.
        """
        _unreachable: List[str] = []
        _sentinel_m = int(self._MISSING_DIST_KM * 1000)
        for i, sid in enumerate(self.site_ids):
            # Unreachable = every outgoing arc is sentinel
            all_out_sentinel = all(
                self._distance_matrix[i][j] >= _sentinel_m
                for j in range(self.num_sites) if j != i
            )
            if all_out_sentinel:
                _unreachable.append(sid)
        if _unreachable:
            for _ur in _unreachable:
                _ur_site = self.sites.get(_ur)
                _ur_name = _ur_site.name if _ur_site else _ur
                _ur_has_coords = (
                    _ur_site is not None
                    and hasattr(_ur_site, 'latitude')
                    and _ur_site.latitude is not None
                )
                print(
                    f"[ConnectivityWarning] site={_ur_name!r}"
                    f" has_coords={_ur_has_coords}"
                    f" — all outgoing arcs are sentinel; site is unreachable"
                )
            logger.warning(
                "[ConnectivityWarning] %d site(s) fully isolated in distance matrix: %s",
                len(_unreachable), _unreachable,
            )
        else:
            print(f"[ConnectivityCheck] OK — all {self.num_sites} sites have at least one valid arc")

    # ── Main solve ────────────────────────────────────────────────────────────

    def solve(
        self,
        trucks: List[Truck],
        demand_sites: List[str] = None,
        max_search_seconds: int = 30,
        traffic_time_multiplier: float = 1.0,
        risk_map: Optional[Dict[str, str]] = None,
        risk_score_map: Optional[Dict[str, float]] = None,
        urgency_factor_m: Optional[int] = None,
        hours_to_critical_map: Optional[Dict[str, float]] = None,
        current_day: int = 1,
        fill_sites: Optional[List[str]] = None,
        transfer_sites: Optional[List[str]] = None,
        _vehicle_fixed_cost: int = None,   # cents; None → derived from config (½ min-billed route)
        _reopt_attempt: bool = False,
        _container_penalty: int = None,   # cents/container; None → derived from config at runtime
        _is_final_day: bool = False,
        cumulative_flaring_hours: float = 0.0,  # total flaring hours accumulated so far in the horizon
        risk_penalty_multiplier: float = 1.0,   # multiplier applied to all disjunction penalties; >1 for risk reopt
        overflow_sites: Optional[List[str]] = None,  # sites beyond fleet capacity budget — optional, urgency-based penalty
        planning_horizon_h: float = 120.0,  # Δt cap for penalty computation (hours); default 5-day window
        guaranteed_hub_sites: Optional[List[str]] = None,  # producers that MUST be in model regardless of demand status
        optimize_days_mode: bool = False,  # True when horizon_days > 1; activates 5× balance penalty
        force_exact_days: bool = False,    # True in force mode: all trucks must be used every day
        min_active_vehicles: Optional[int] = None,  # planner-style lower bound on used trucks
        _solve_layer: int = 1,             # 1=strict, 2=relaxed, 3=haversine_fallback
    ) -> List[Route]:
        """Solve the VRP for one day.

        Time limit = config.max_driver_hours per vehicle (resets every call).
        Open-route: vehicles end at the last site visited — no return to depot.

        Args:
            trucks: Available trucks (start positions already updated for the day).
            demand_sites: Site IDs that need service this day.
            max_search_seconds: OR-Tools search time limit.
            traffic_time_multiplier: Travel time scaling (1.0 = normal, 1.333 = heavy).
            risk_map: Optional mapping of site_id → risk level string ('critical',
                      'warning', 'normal').  Used only for logging and sanity checks;
                      disjunction penalties are derived from real economic flow values.
            risk_score_map: Optional mapping of site_id → float [0-100] risk score.
                      Used to discount incoming arc costs to urgent sites when
                      urgency_factor_m > 0.  Default 0 disables this discount.
            urgency_factor_m: Metres discounted per risk-score point. Overrides
                      config.urgency_factor_m when provided. Set to 0 to disable.
            hours_to_critical_map: Mapping of site_id → float hours until the site
                      reaches its critical threshold (depletion for consumers,
                      overflow/flaring for producers).  Used to compute per-site
                      urgency Δt = max(0, max_driver_hours − hours_to_critical)
                      which drives both arc discounts and disjunction penalties.

        Returns:
            List of Route objects, one per truck that moved.  Never raises — returns
            an empty list only if no vehicle can move at all.
        """
        self._traffic_time_multiplier = traffic_time_multiplier

        if not trucks:
            return []

        if demand_sites is None:
            demand_sites = [
                sid for sid, site in self.sites.items()
                if site.is_consumer and site.utilization_percentage < 60
            ]

        if not demand_sites:
            return []

        # Reset per-solve disjunction tracking (used by _debug_infeasibility).
        self._nodes_with_disjunction = set()

        demand_sites_set = set(demand_sites)
        num_vehicles = len(trucks)

        # ── Feasibility preconditions ─────────────────────────────────────────
        print("[FEASIBILITY DEBUG]")
        print(f"  demand_sites : {len(demand_sites)}")
        print(f"  num_trucks   : {num_vehicles}")
        print(f"  max_time     : {self.config.max_driver_hours}h")
        _total_truck_capacity = 0
        for _t in trucks:
            _t_start = (
                _t.start.site_id
                if getattr(_t, "start", None) and getattr(_t.start, "site_id", None)
                else getattr(_t, "home_site_id", "?")
            )
            _t_load = getattr(_t, "initial_load", 0)
            _t_cap  = getattr(_t, "capacity", 0)
            _total_truck_capacity += max(0, _t_cap - _t_load)
            print(f"  truck {getattr(_t, 'id', '?')}:")
            print(f"    start_site   : {_t_start}")
            print(f"    initial_load : {_t_load}/{_t_cap}  (free slots: {max(0, _t_cap - _t_load)})")
        if _total_truck_capacity < len(demand_sites):
            print(
                f"[FEASIBILITY DEBUG] WARNING: total free truck capacity ({_total_truck_capacity})"
                f" < demand sites ({len(demand_sites)}) — some sites will be dropped"
            )

        # ── Derive config-dependent cost parameters at runtime ───────────────
        # Done here (not at class level) so overrides applied to self.config
        # (e.g. cost_per_km_override from generate_recommendation) are reflected.
        _min_billed_km_rt = self.config.min_billed_km
        _cost_per_km_rt   = self.config.cost_per_km_eur
        _contingency_rt   = self.config.contingency_multiplier
        _min_route_cost_c = int(_min_billed_km_rt * _cost_per_km_rt * _contingency_rt * self.COST_SCALE)

        # Mid-plan container imbalance penalty: 2× minimum-route cost per container.
        # Steers routing toward balance without dominating economic disjunction penalties.
        _swap_imbalance_c = 2 * _min_route_cost_c

        if _container_penalty is None:
            _container_penalty = _swap_imbalance_c

        # End-of-horizon balance: heavily prefer returning bays and finishing
        # trucks empty on the last day, while still allowing an imbalanced
        # solution when the alternative would be serving nothing.
        if _is_final_day:
            _container_penalty = 200 * _swap_imbalance_c
            print(
                f"[VRPSetup] Final day — container penalty elevated to "
                f"{_container_penalty // self.COST_SCALE} EUR/container (end-of-horizon balance)"
                f"  [200× mid-plan={_swap_imbalance_c // self.COST_SCALE}EUR]"
            )

        # ── Open-route via virtual dummy-end node ─────────────────────────────
        # Index `num_sites` is a virtual node that can be reached from any real
        # node at zero distance/time.  By routing all vehicle ends here, trucks
        # are free to stop at any real node — no forced depot return.
        dummy_end = self.num_sites
        num_nodes = self.num_sites + 1   # real nodes + 1 dummy end

        _effective_speed_kmph = (
            self.config.avg_speed_kmph / traffic_time_multiplier
            if traffic_time_multiplier > 0
            else self.config.avg_speed_kmph
        )
        _effective_speed_kmph = max(1.0, _effective_speed_kmph)

        # Extended matrices: dummy-end column/row filled with zeros.
        dist_ext = [row + [0] for row in self._distance_matrix] + [[0] * num_nodes]
        time_ext = []
        for from_id in self.site_ids:
            row = [
                self._scaled_travel_minutes(
                    from_id,
                    to_id,
                    traffic_time_multiplier=traffic_time_multiplier,
                    effective_speed_kmph=_effective_speed_kmph,
                )
                if from_id != to_id else 0
                for to_id in self.site_ids
            ]
            time_ext.append(row + [0])
        time_ext.append([0] * num_nodes)

        # ── Sanitize: patch sentinel entries with haversine (runs every layer) ──
        # Build per-node coordinate list (real nodes + dummy end = None).
        _san_coords: List[Optional[tuple]] = []
        for _sc_sid in self.site_ids:
            _sc_s = self.sites.get(_sc_sid)
            if (_sc_s and getattr(_sc_s, 'latitude', None) is not None
                    and getattr(_sc_s, 'longitude', None) is not None):
                _san_coords.append((_sc_s.latitude, _sc_s.longitude))
            else:
                _san_coords.append(None)
        _san_coords.append(None)  # dummy end node
        _san_n = self._sanitize(dist_ext, time_ext, _san_coords, _effective_speed_kmph)
        if _san_n:
            print(f"[Sanitize] patched {_san_n} sentinel pair(s) with haversine × 1.3")

        starts: List[int] = []
        ends:   List[int] = []
        for truck in trucks:
            start_site_id = self._resolve_truck_start(truck)
            depot_idx = self.site_index.get(start_site_id)
            if depot_idx is None:
                print(f"[ERROR] invalid start_site_id={start_site_id} for truck={truck.id} — falling back to 0")
                depot_idx = 0
            starts.append(depot_idx)
            # In force_exact_days mode, ignore force_end without mutating the truck object.
            local_force_end = None if force_exact_days else truck.force_end
            if force_exact_days and truck.force_end is not None:
                print(f"[ForceExact] disabling force_end for truck={truck.id} (conflicts with force_exact_days)")
            # Enforce force_end only on its configured day; otherwise keep the
            # route open so intermediate days are free to end elsewhere.
            if (
                local_force_end is not None
                and local_force_end.day_index == current_day
                and local_force_end.site_id in self.site_index
            ):
                ends.append(self.site_index[local_force_end.site_id])
            else:
                ends.append(dummy_end)   # ← open route: end at virtual node

        assert all(s is not None for s in starts), f"[ASSERT] None in starts: {starts}"
        assert all(e is not None for e in ends),   f"[ASSERT] None in ends: {ends}"

        # ── [PhantomDepots] Decouple truck start nodes from service nodes ─────────
        # In OR-Tools, a node that is a vehicle start depot has the SAME routing
        # index as its vehicle-start assignment.  _site_routing_index() returns -1
        # for such nodes because every routing index for that physical node is in
        # _start_routing_indices.  This means start-site nodes cannot be added as
        # service waypoints (disjunctions, loading hubs, demand nodes).
        #
        # Consequence: if all producers are truck home depots, zero producers appear
        # in the routing model → [ModelCheck] CRITICAL → NO_FEASIBLE_ROUTES.
        # Similarly, consumer home depots can't be demand nodes (DisjunctionSkip).
        #
        # Fix: replace each affected truck's start with a phantom physical node that
        # has the same distances/times as the real start site.  The real site retains
        # its own physical index and therefore gets a valid non-start service routing
        # index — it can be added as a demand node, loading hub, etc.
        #
        # Which starts need a phantom?
        #   • Producer home depots: needed so they can be visited as loading hubs.
        #   • Consumer/industry home depots that are also in demand_sites: needed so
        #     their demand disjunction can be added (DisjunctionSkip prevention).
        # Compute truck start site_ids here (mirrors _truck_start_ids computed later
        # for disjunction logic, but needed now before the routing model is created).
        _early_truck_start_ids = [self._resolve_truck_start(t) for t in trucks]

        _demand_sites_set_phantom = set(demand_sites)
        _needs_phantom: set = set()
        for _ts_id in _early_truck_start_ids:
            _ts_s = self.sites.get(_ts_id)
            if _ts_s is None or _ts_id not in self.site_index:
                continue
            if _ts_s.is_producer or _ts_id in _demand_sites_set_phantom:
                _needs_phantom.add(_ts_id)

        _phantom_for_start: Dict[str, int] = {}  # real_site_id → phantom physical idx
        if _needs_phantom:
            _very_large_dist_ph = int(self._MISSING_DIST_KM * 1000)  # metres
            _phantom_base = num_nodes  # phantom indices start after dummy_end

            # Assign a phantom index to each unique site needing one
            for _ph_i, _ph_sid in enumerate(sorted(_needs_phantom)):
                _phantom_for_start[_ph_sid] = _phantom_base + _ph_i

            _n_phantoms = len(_phantom_for_start)
            _new_num_nodes = num_nodes + _n_phantoms

            # Expand existing rows: add _n_phantoms columns (unreachable as destination)
            for _row in dist_ext:
                _row += [_very_large_dist_ph] * _n_phantoms
            for _row in time_ext:
                _row += [int(1e7)] * _n_phantoms   # ~infinite minutes

            # Add phantom rows: same outbound distances as the real start site
            for _ph_sid, _ph_idx in sorted(_phantom_for_start.items(), key=lambda x: x[1]):
                _real_idx = self.site_index[_ph_sid]
                # Copy real row (already expanded with _n_phantoms sentinel columns)
                _ph_dist_row = list(dist_ext[_real_idx])
                _ph_time_row = list(time_ext[_real_idx])
                # Phantom → other phantom: never route between phantoms
                for _other_ph_idx in _phantom_for_start.values():
                    _ph_dist_row[_other_ph_idx] = _very_large_dist_ph
                    _ph_time_row[_other_ph_idx] = int(1e7)
                dist_ext.append(_ph_dist_row)
                time_ext.append(_ph_time_row)

            # Update starts[] to use phantom indices for affected sites
            for _vi_ph, _ts_id_ph in enumerate(_early_truck_start_ids):
                if _ts_id_ph in _phantom_for_start:
                    starts[_vi_ph] = _phantom_for_start[_ts_id_ph]

            num_nodes = _new_num_nodes
            print(
                f"[PhantomDepots] Added {_n_phantoms} phantom start node(s) for:"
                f" {sorted(_needs_phantom)}"
                f" → phantom indices {list(_phantom_for_start.values())}"
            )

        print(f"[DEBUG] VRPSolver.solve  starts={starts}  ends={ends}  num_nodes={num_nodes}  num_vehicles={num_vehicles}")
        print(f"[DEBUG] demand_sites ({len(demand_sites)}): {demand_sites}")
        print(f"[DEBUG] distance_matrix keys ({len(self.distance_matrix)}): {list(self.distance_matrix.keys())[:8]}{'...' if len(self.distance_matrix) > 8 else ''}")
        assert num_vehicles >= 1, f"[ASSERT] num_vehicles={num_vehicles} — no trucks passed to solver"
        assert len(self.distance_matrix) > 0, f"[ASSERT] distance_matrix is empty"
        for _vi, (_sn, _truck) in enumerate(zip(starts, trucks)):
            assert 0 <= _sn < num_nodes, (
                f"[ASSERT] truck {_truck.id} start index {_sn} out of range [0, {num_nodes})"
            )
        manager = pywrapcp.RoutingIndexManager(num_nodes, num_vehicles, starts, ends)
        routing = pywrapcp.RoutingModel(manager)
        _start_routing_indices = {routing.Start(v) for v in range(num_vehicles)}
        _end_routing_indices = {routing.End(v) for v in range(num_vehicles)}
        _disjunction_nodes_added: set[int] = set()
        _service_routing_index_cache: Dict[int, int] = {}

        def _site_routing_index(site_id: str, *, prefer_service_node: bool = True) -> int:
            """Resolve a routing index for a physical site.

            When multiple vehicles share the same physical start node, OR-Tools
            may map that node to a vehicle-start routing index. For any normal
            service-node use, we must prefer a non-start/non-end routing index.
            """
            physical_idx = self.site_index.get(site_id)
            if physical_idx is None:
                return -1
            if prefer_service_node and physical_idx in _service_routing_index_cache:
                return _service_routing_index_cache[physical_idx]
            if prefer_service_node:
                for routing_idx in range(routing.Size()):
                    if manager.IndexToNode(routing_idx) != physical_idx:
                        continue
                    if routing_idx in _start_routing_indices or routing_idx in _end_routing_indices:
                        continue
                    _service_routing_index_cache[physical_idx] = routing_idx
                    return routing_idx
                return -1
            return manager.NodeToIndex(physical_idx)

        def _safe_add_disjunction(node_index: int, penalty: int, label: str) -> bool:
            """Guard OR-Tools AddDisjunction against start/end/duplicate nodes.

            OR-Tools aborts the whole process on invalid disjunctions instead of
            raising a Python exception, so these checks must happen in Python first.
            """
            if node_index < 0:
                logger.warning("[DisjunctionSkip] %s has invalid node index %s", label, node_index)
                return False
            if node_index in _start_routing_indices:
                logger.info("[DisjunctionSkip] %s is a vehicle start node", label)
                return False
            if node_index in _end_routing_indices:
                logger.info("[DisjunctionSkip] %s is a vehicle end node", label)
                return False
            if node_index in _disjunction_nodes_added:
                logger.info("[DisjunctionSkip] %s already has a disjunction", label)
                return False
            routing.AddDisjunction([node_index], penalty)
            _disjunction_nodes_added.add(node_index)
            self._nodes_with_disjunction.add(node_index)  # instance-level tracking for debug
            return True

        # ── Arc cost: monetary transport cost in cents ────────────────────────
        # Formula: cost(from→to) = distance_km × cost_per_km_eur × COST_SCALE
        #
        # The objective the solver minimises is now directly proportional to the
        # monetary transport cost reported in Recommendation.transport_cost_eur,
        # expressed as integer cents so OR-Tools can compare it against disjunction
        # penalties and the HandleCost dimension (both also in cents).
        #
        # Urgency is encoded through flow-value-based disjunction penalties,
        # not through arc discounts.  This preserves the metric structure of
        # the distance matrix and ensures triangle inequality holds.
        #
        # The urgency_factor_m parameter is retained for sensitivity experiments;
        # when > 0 it applies an arc discount proportional to risk_score.
        _uf = urgency_factor_m if urgency_factor_m is not None else self.config.urgency_factor_m
        _rsmap = risk_score_map or {}
        _site_ids = self.site_ids
        _num_sites = self.num_sites
        _cost_scale = self.COST_SCALE
        _cost_per_km = self.config.cost_per_km_eur
        _contingency = self.config.contingency_multiplier
        _min_billed_km = self.config.min_billed_km

        print(
            f"[RoutingCost] cost_per_km={_cost_per_km}EUR/km"
            f"  contingency={_contingency}x"
            f"  min_billed_km={_min_billed_km}km (route-level billing only, not per arc)"
        )

        # Pre-compute transfer hub node indices here (also used in time_callback below).
        _arc_transfer_nodes: set = set()
        for _sid in (transfer_sites or []):
            if _sid in self.site_index:
                _arc_transfer_nodes.add(self.site_index[_sid])

        # Pre-compute depot node indices (trucks' home sites) for return-trip reward,
        # and producer/consumer node sets for the container-balance dimension.
        _depot_node_indices: set = {
            self.site_index[t.home_site_id]
            for t in trucks
            if t.home_site_id in self.site_index
        }
        _producer_node_indices: set = {
            i for i, sid in enumerate(self.site_ids)
            if self.sites.get(sid) and self.sites[sid].is_producer
        }
        _consumer_node_indices: set = {
            i for i, sid in enumerate(self.site_ids)
            if self.sites.get(sid) and self.sites[sid].is_consumer
        }

        # ── Pre-compute per-node flow imbalance value (cents) ────────────────
        # flow_value[j] = economic cost of NOT serving site j during this shift.
        #
        # Δt_h (per site) = max(0, max_driver_hours − hours_to_critical)
        #    = projected hours in crisis if site j is skipped today.
        #    Sites already past their critical point (htc ≤ 0): use full shift.
        #    Sites outside the planning window (htc ≥ max_driver_hours): Δt = 0.
        #
        # Producer j: overflow_kg = prod_rate × Δt_h
        #             flow_value  = overflow_kg × KG_TO_MWH × flaring_cost_eur_mwh
        # Consumer j: shortage_kg = cons_rate × Δt_h
        #             flow_value  = tiered stockout penalty (see _flow_value_eur)
        #
        # Urgency is correctly reflected: 1h-critical ≠ 8h-critical.
        # Sites beyond the planning window have flow_value = 0 (treated as optional).
        #
        # Used in TWO places:
        #   a) Arc cost discount: cost(i→j) = routing_cost − flow_value_cents[j]
        #      → high-urgency nodes attract the solver over pure distance
        #   b) Disjunction penalty: penalty(j) = max(handling_floor, flow_value_cents[j])

        _htc_map = hours_to_critical_map or {}
        _max_Dt_h = planning_horizon_h   # planning horizon for penalty Δt (not the driver shift budget)
        _node_flow_cents: Dict[int, int] = {}       # node_index → flow_value_cents

        for _fv_sid in demand_sites:
            if _fv_sid not in self.site_index:
                continue
            _fv_ni   = self.site_index[_fv_sid]
            _fv_site = self.sites.get(_fv_sid)
            if not _fv_site:
                continue
            _site_htc = _htc_map.get(_fv_sid, 0.0)
            _Dt_h     = self._compute_Dt(_site_htc, _max_Dt_h)
            _fv_eur   = self._flow_value_eur(_fv_site, _Dt_h)
            _fv_cents = int(_fv_eur * self.COST_SCALE)
            _node_flow_cents[_fv_ni] = _fv_cents

        # ── [FlowDebug] per-site flow value breakdown ─────────────────────────
        print("[FlowDebug] site_id | type | htc_h | rate_kg_h | usable_kg | flow_cents")
        for _dbg_sid in demand_sites:
            if _dbg_sid not in self.site_index:
                continue
            _dbg_ni   = self.site_index[_dbg_sid]
            _dbg_site = self.sites.get(_dbg_sid)
            if not _dbg_site:
                continue
            _dbg_htc  = _htc_map.get(_dbg_sid, 0.0)
            _dbg_Dt   = self._compute_Dt(_dbg_htc, _max_Dt_h)
            _dbg_floor = self.config.usable_floor_bar
            _dbg_usable = sum(
                get_normalized_kg(effective_pressure_bar(bay.pressure_bar), _dbg_floor)
                for bay in _dbg_site.bays
            )
            if _dbg_site.is_producer:
                _dbg_rate = (
                    _dbg_site.production.effective_kg_per_h
                    if _dbg_site.production and _dbg_site.production.effective_kg_per_h
                    else 0.0
                )
            else:
                _dbg_rate = _dbg_site.consumption_rate_kg_hour or 0.0
            _dbg_flow = _node_flow_cents.get(_dbg_ni, 0)
            print(
                f"[FlowDebug]  {_dbg_sid:<28}"
                f"  type={_dbg_site.site_type.value:<12}"
                f"  htc={_dbg_htc:.2f}h"
                f"  Dt={_dbg_Dt:.2f}h"
                f"  rate={_dbg_rate:.1f}kg/h"
                f"  usable_kg={_dbg_usable:.1f}"
                f"  flow={_dbg_flow}c ({_dbg_flow // self.COST_SCALE}EUR)"
            )

        _fv_nonzero = {ni: v for ni, v in _node_flow_cents.items() if v > 0}

        # ── Stability: cap flow_value at k × routing_cost_reference ─────────
        # routing_cost_reference = cost of the shortest valid arc between any
        # truck start and any demand node.  k=10 allows a high-urgency site to
        # attract the solver 10× more than a typical route costs — enough to
        # guarantee the site is prioritised while preventing ALL arc costs from
        # collapsing to 0 when a single site has an extreme flow value.
        _fv_ref_cost_c = 0
        for _fv_ref_sid in demand_sites:
            if _fv_ref_sid not in self.site_index:
                continue
            for _fv_ref_t in trucks:
                _fv_ref_d = self._lookup_distance(
                    self._resolve_truck_start(_fv_ref_t), _fv_ref_sid
                )
                if 0 < _fv_ref_d < self._MISSING_DIST_KM:
                    _c = int(_fv_ref_d * _cost_per_km * _contingency * _cost_scale)
                    if _c > 0 and (_fv_ref_cost_c == 0 or _c < _fv_ref_cost_c):
                        _fv_ref_cost_c = _c
        if _fv_ref_cost_c == 0:
            # No valid arcs found; fall back to cost of min_billed_km route
            _fv_ref_cost_c = int(_min_billed_km * _cost_per_km * _contingency * _cost_scale)
        _fv_max_cap = 10 * _fv_ref_cost_c
        _capped_count = 0
        for _ni_cap in list(_node_flow_cents.keys()):
            if _node_flow_cents[_ni_cap] > _fv_max_cap:
                _node_flow_cents[_ni_cap] = _fv_max_cap
                _capped_count += 1
        if _capped_count:
            print(
                f"[FlowValue] Stability cap: {_capped_count} node(s) capped at"
                f" {_fv_max_cap // self.COST_SCALE}EUR"
                f" (10× ref={_fv_ref_cost_c // self.COST_SCALE}EUR)"
            )
        _fv_nonzero = {ni: v for ni, v in _node_flow_cents.items() if v > 0}

        print(
            f"[FlowValue] {len(_node_flow_cents)} demand nodes computed"
            f"; {len(_fv_nonzero)} with non-zero flow value"
            + (
                f" | max={max(_fv_nonzero.values())//self.COST_SCALE}EUR"
                f" min={min(_fv_nonzero.values())//self.COST_SCALE}EUR"
                if _fv_nonzero else ""
            )
            + f" | shift={_max_Dt_h:.1f}h (per-site Δt=min(htc,shift))"
        )
        for _fv_ni_log, _fv_c_log in sorted(_fv_nonzero.items(), key=lambda x: -x[1])[:10]:
            _fv_sid_log  = self.site_ids[_fv_ni_log]
            _fv_site_log = self.sites.get(_fv_sid_log)
            _fv_name_log = _fv_site_log.name if _fv_site_log else _fv_sid_log
            _fv_kind_log = "producer" if _fv_ni_log in _producer_node_indices else "consumer"
            _fv_htc_log  = _htc_map.get(_fv_sid_log, 0.0)
            _fv_Dt_log   = _max_Dt_h if _fv_htc_log <= 0 else min(_fv_htc_log, _max_Dt_h)
            print(
                f"  {_fv_name_log:<22} [{_fv_kind_log}]"
                f"  htc={_fv_htc_log:.1f}h  Δt={_fv_Dt_log:.1f}h"
                f"  flow_value={_fv_c_log // self.COST_SCALE}EUR"
            )

        # ── Pre-compute marginal routing cost per demand node ─────────────────
        # marginal_cost(j) = min over all trucks of one-way distance_cost(start→j).
        # Marginal value condition: serve j only if flow_value(j) > marginal_cost(j).
        # Sites where routing cost exceeds economic gain are treated as optional (penalty=0).
        _marginal_cost_cents: Dict[int, int] = {}
        for _mc_sid in demand_sites:
            if _mc_sid not in self.site_index:
                continue
            _mc_ni = self.site_index[_mc_sid]
            _mc_min = min(
                int(self._lookup_distance(self._resolve_truck_start(t), _mc_sid)
                    * _cost_per_km * _cost_scale)
                for t in trucks
            )
            _marginal_cost_cents[_mc_ni] = _mc_min

        # ── [ArcDecision] sample: routing_cost, flow_value, final_arc_cost ──────
        # Logged pre-solve for the top-10 demand nodes by flow value so operators
        # can verify the arc discount logic without flooding the log.
        # min_billed_km applies at route-level reporting, NOT per arc.
        _arc_sample = sorted(_fv_nonzero.keys(), key=lambda ni: -_fv_nonzero[ni])[:10]
        for _ad_ni in _arc_sample:
            _ad_sid  = self.site_ids[_ad_ni]
            _ad_site = self.sites.get(_ad_sid)
            _ad_name = _ad_site.name if _ad_site else _ad_sid
            _ad_fv_c = _node_flow_cents.get(_ad_ni, 0)
            _ad_min_dist = min(
                self._lookup_distance(self._resolve_truck_start(t), _ad_sid)
                for t in trucks
            )
            _ad_routing_c = int(_ad_min_dist * _cost_per_km * _contingency * _cost_scale)
            _ad_final_c   = max(1, _ad_routing_c - _ad_fv_c)  # floored at 1 (no zero-cost)
            print(
                f"[ArcDecision] → {_ad_name:<22}"
                f"  dist={_ad_min_dist:.1f}km"
                f"  routing_cost={_ad_routing_c // _cost_scale}EUR"
                f"  flow_value={_ad_fv_c // _cost_scale}EUR"
                f"  final_arc_cost={_ad_final_c // _cost_scale}EUR"
            )

        # ── Arc cost: routing_cost(i→j) × cost_per_km × contingency ─────────
        # Formula: cost_cents = dist_km × cost_per_km × contingency × COST_SCALE
        # min_billed_km is a billing minimum for the whole route, not per arc —
        # it is applied at post-solve cost reporting, not inside the solver callback.
        # The flow-value discount steers the solver toward high-urgency sites.
        # Floored at 1 cent for real non-zero arcs to prevent zero-cost degeneracy.
        _missing_dist_m  = int(self._MISSING_DIST_KM * 1000)  # sentinel metres value
        _very_large_cost = self._VERY_LARGE_COST_CENTS

        # ── Effective initial loads ───────────────────────────────────────────────
        # Day 1: initial_load = 0 for all trucks (empty start, no pre-grants).
        # Day 2+: initial_load = containers physically carried over from previous day
        #         (set by _update_truck_states_for_next_day — real physical state).
        #
        # NO virtual containers are granted here.  Loading happens ONLY when the
        # solver explicitly routes a truck to a producer node (+1 via the capacity
        # dimension callback).  A truck starting empty MUST visit a producer first.
        _effective_initial_loads: Dict[int, int] = {
            _v: getattr(_t, 'initial_load', 0)
            for _v, _t in enumerate(trucks)
        }
        # Guard: reject any caller that sneaks in a negative initial load.
        for _v_assert, _il_assert in _effective_initial_loads.items():
            if _il_assert < 0:
                raise ValueError(
                    f"INVALID MODEL: truck index {_v_assert} has negative initial_load={_il_assert}"
                )
        _day1_trucks_with_load = [
            trucks[_v].id for _v, _il in _effective_initial_loads.items() if _il > 0
        ]
        if current_day == 1 and _day1_trucks_with_load:
            logger.warning(
                "[InitialLoad] Day 1 trucks with non-zero initial_load=%s"
                " — verify these are intentional (fleet config override)",
                _day1_trucks_with_load,
            )
        print(
            f"[InitialLoad] day={current_day}"
            f" loads={[_effective_initial_loads[_v] for _v in range(len(trucks))]}"
            f" (0 = start empty, >0 = carry-over from previous day)"
        )

        # ── [ProducerFirst] pre-compute start-node state ─────────────────────────
        # PATH_CHEAPEST_ARC and PARALLEL_CHEAPEST_INSERTION both select arcs
        # greedily by cost, ignoring dimension feasibility.  When trucks start
        # empty (initial_load=0) and consumer arcs are cheaper than producer arcs,
        # the heuristic picks a consumer first — which immediately violates the
        # capacity dimension (cumul 0-1=-1 < 0).  If ALL arcs from start are
        # similarly blocked, the strategy fails to build an initial solution and
        # OR-Tools returns NULL even though a trivial empty route is always valid.
        #
        # Fix: arc callback returns VERY_LARGE_COST for any non-producer arc
        # that originates at a vehicle start node when the corresponding truck has
        # zero initial load.  This hard-steers the first insertion toward a
        # producer, matching what the capacity dimension requires.
        #
        # Per-vehicle initial load: keyed by the PHYSICAL node index of the start
        # (not the routing index) so a single lookup in the callback is O(1).
        _start_node_initial_load: Dict[int, int] = {}
        for _vi, _truck in enumerate(trucks):
            _sn = starts[_vi]
            _il = _effective_initial_loads[_vi]   # use effective load (may be pre-loaded)
            # If two trucks share the same start node, use the MINIMUM load —
            # conservative: only unlock consumer arcs if ALL trucks at this
            # depot already have containers aboard.
            if _sn not in _start_node_initial_load or _il < _start_node_initial_load[_sn]:
                _start_node_initial_load[_sn] = _il

        _any_producer_reachable_from_start = any(
            dist_ext[_sn][_pni] < _missing_dist_m
            for _sn in _start_node_initial_load
            for _pni in _producer_node_indices
            if _pni < len(dist_ext[_sn])
        )
        _producer_first_active = (
            any(il == 0 for il in _start_node_initial_load.values())
            and _any_producer_reachable_from_start
        )
        print(
            f"[ProducerFirst] producer_first_gate={'ON' if _producer_first_active else 'OFF'}"
            f"  (trucks_empty={sum(1 for il in _start_node_initial_load.values() if il==0)}"
            f"/{len(_start_node_initial_load)}"
            f"  producer_reachable={_any_producer_reachable_from_start})"
        )

        # ── [ProducerFirstHard] Hard domain constraint: first arc → producer ──
        # When any truck starts empty, restrict NextVar(start) to only producer
        # routing indices (+ the vehicle's own end for the empty-route case).
        # This is a hard CP-domain constraint — not cost-based — so the solver
        # cannot choose start→consumer as the first insertion regardless of
        # arc cost, eliminating the construction failure at its root.
        if _producer_first_active:
            _prod_routing_indices = [
                _site_routing_index(self.site_ids[pni])
                for pni in _producer_node_indices
                if _site_routing_index(self.site_ids[pni]) >= 0
            ]
            # Hard domain constraint only on Layer 1 (strict).
            # Layer 2+ removes this to avoid construction failure under relaxed rules.
            if _prod_routing_indices and _solve_layer == 1:
                for _v_pf in range(num_vehicles):
                    if _effective_initial_loads[_v_pf] == 0:
                        _start_ri = routing.Start(_v_pf)
                        _allowed = _prod_routing_indices + [routing.End(_v_pf)]
                        routing.NextVar(_start_ri).SetValues(_allowed)
                        print(
                            f"[ProducerFirstHard] vehicle={_v_pf}"
                            f" NextVar(start={_start_ri}) restricted to"
                            f" {len(_prod_routing_indices)} producer(s) + end"
                        )
            elif _solve_layer >= 2:
                print(f"[Layer{_solve_layer}] ProducerFirstHard constraint DISABLED (relaxed layer)")

        # ── [DiagnosticMode] pure-distance callback flag ──────────────────────
        # Set _PURE_DISTANCE_MODE = True to strip ALL custom arc logic and run
        # with raw distance costs only.  Use this to isolate whether the cost
        # callback is causing NO_FEASIBLE_ROUTES:
        #   solution found  → problem is inside the full callback (penalties/discounts)
        #   still no solution → problem is in model structure (nodes / matrix / dims)
        # Flip back to False once the root cause is identified.
        _PURE_DISTANCE_MODE = False

        if _PURE_DISTANCE_MODE:
            print("[Test] running with pure distance cost only — all callback logic disabled")
            logger.warning("[Test] _PURE_DISTANCE_MODE active: flow_value, bonuses, penalties all suppressed")

            def monetary_arc_callback(from_index, to_index):
                fn = manager.IndexToNode(from_index)
                tn = manager.IndexToNode(to_index)
                dist_m = dist_ext[fn][tn]
                if dist_m >= _missing_dist_m:
                    return _very_large_cost
                return dist_m   # raw metres — no scaling, no penalties, no discounts

        else:
            def monetary_arc_callback(from_index, to_index):
                fn = manager.IndexToNode(from_index)
                tn = manager.IndexToNode(to_index)
                dist_m  = dist_ext[fn][tn]
                # Missing matrix entry: arc is infeasible — return prohibitive cost.
                if dist_m >= _missing_dist_m:
                    return _very_large_cost
                dist_km = dist_m / 1000.0
                # Pure transport cost: dist_km × cost_per_km × contingency.
                cost_cents = int(dist_km * _cost_per_km * _contingency * _cost_scale)
                # Handling fee: charged once per service stop (arriving at demand node),
                # not at the origin depot (truck is already positioned there).
                if tn in _service_node_set and fn not in _start_physical_nodes:
                    cost_cents += _handling_cents
                # Floor at 1 cent for real non-trivial arcs.
                if tn < _num_sites and dist_km > 0 and cost_cents == 0:
                    cost_cents = 1
                return cost_cents

        transit_cb = routing.RegisterTransitCallback(monetary_arc_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)
        # Vehicle activation cost: fixed at 100000 scaled units per used vehicle.
        # High enough to discourage spurious truck use while not blocking genuine demand.
        # In optimize_days mode: set to 0 so the solver never prefers leaving a site
        # unserved over activating an additional truck.  The disjunction penalty for
        # any unserved site must dominate the activation cost; zeroing it guarantees
        # that even a 1-cent penalty is enough to justify using another truck.
        _FIXED_VEHICLE_COST = int(self.VEHICLE_ACTIVATION_COST_EUR * self.COST_SCALE)
        if _vehicle_fixed_cost is None:
            _vehicle_fixed_cost = 0 if optimize_days_mode else _FIXED_VEHICLE_COST
        _vehicle_fixed_cost_eur = _vehicle_fixed_cost / _cost_scale
        print(
            f"[VehicleCost] activation_cost={_vehicle_fixed_cost_eur:.0f}EUR/vehicle"
            f"  ({num_vehicles} vehicle(s) available)"
            + (" [optimize_days: activation=0]" if optimize_days_mode else "")
        )
        for _v_act in range(num_vehicles):
            routing.SetFixedCostOfVehicle(_vehicle_fixed_cost, _v_act)

        # ── Force exact days: all trucks must be used ─────────────────────────
        if force_exact_days:
            _active_vehicles = []
            for _v_force in range(num_vehicles):
                routing.SetVehicleUsedWhenEmpty(True, _v_force)
                routing.SetFixedCostOfVehicle(100000, _v_force)
                _active_vehicles.append(trucks[_v_force].id)
            print(f"[ForceExact] mode=force — {num_vehicles} vehicle(s) forced active: {_active_vehicles}")
        elif min_active_vehicles is not None and min_active_vehicles > 1:
            _bounded_min_active = max(1, min(int(min_active_vehicles), num_vehicles))
            solver = routing.solver()
            active_vars = [routing.ActiveVehicleVar(v) for v in range(num_vehicles)]
            solver.Add(solver.Sum(active_vars) >= _bounded_min_active)
            print(
                f"[Planner] enforcing min_active_vehicles={_bounded_min_active}"
                f" out of {num_vehicles}"
            )

        # ── Time dimension ────────────────────────────────────────────────────
        service_time_minutes = int(self.config.swap_time_hours * 60)
        TRANSFER_SERVICE_MINUTES = 10  # brief relay stop at transfer hubs
        def time_callback(from_index, to_index):
            fn = manager.IndexToNode(from_index)
            tn = manager.IndexToNode(to_index)
            travel = time_ext[fn][tn]
            # Service time only at nodes with actual container operations.
            # Pass-through nodes (non-demand consumers, fill sites) get 0 service time
            # to prevent artificial inflation of route duration.
            if tn != fn and tn != dummy_end:
                if tn in _arc_transfer_nodes:
                    travel += TRANSFER_SERVICE_MINUTES
                elif tn in _producer_node_indices:
                    # Producers always have a pickup operation
                    travel += service_time_minutes
                elif (tn in _consumer_node_indices
                      and tn < _num_sites
                      and _site_ids[tn] in demand_sites_set):
                    # Demand consumers have a swap operation
                    travel += service_time_minutes
                # else: pass-through node — no container moved, no service time
            return travel

        time_cb = routing.RegisterTransitCallback(time_callback)

        max_time_minutes = int(self.config.max_driver_hours * 60)
        if _solve_layer >= 2:
            max_time_minutes = int(max_time_minutes * 1.25)
            print(f"[Layer{_solve_layer}] relaxed time budget: {max_time_minutes}min (+25% over strict)")

        # ── Multi-day model verification log ──────────────────────────────────
        # Rolling-horizon: each call to solve() is ONE day of the multi-day plan.
        # Every vehicle gets an INDEPENDENT time budget (max_driver_hours) that
        # resets at the start of each call — trucks never carry time debt across days.
        # Vehicle-day mapping: vehicle_id × horizon_day (assigned by caller).
        print(
            f"[VRPSolve] day={current_day}"
            f" vehicles={num_vehicles}"
            f" time_budget={self.config.max_driver_hours:.1f}h ({max_time_minutes}min) per vehicle (independent)"
            f" demand={len(demand_sites)}"
            f" fill_optional={len(fill_sites or [])}"
            f" transfer_optional={len(transfer_sites or [])}"
        )
        for v_idx, truck in enumerate(trucks):
            vehicle_day_id = v_idx * 10 + current_day  # conceptual ID for this vehicle-day
            start_site = self._resolve_truck_start(truck)
            end_mode = "open"
            if truck.force_end is not None and truck.force_end.day_index == current_day:
                end_mode = f"forced→{truck.force_end.site_id}"
            print(
                f"[VehicleDay] id={vehicle_day_id}"
                f" truck={truck.id}"
                f" day={current_day}"
                f" start={start_site}"
                f" end={end_mode}"
                f" time_budget={max_time_minutes}min"
            )

        logger.info(
            "VRP solve: day=%d, %d vehicle(s), open-route model, "
            "time_limit=%.1fh (%d min) per vehicle (independent daily budget), "
            "%d demand site(s)",
            current_day, num_vehicles, self.config.max_driver_hours, max_time_minutes,
            len(demand_sites),
        )

        # ── [VRPNodes] diagnostic ─────────────────────────────────────────────
        n_cons = sum(
            1 for sid in demand_sites
            if sid in self.sites and self.sites[sid].is_consumer
        )
        n_prod = len(demand_sites) - n_cons
        print(
            f"[VRPNodes] total={len(demand_sites)}"
            f" consumers={n_cons} producers={n_prod}"
            f" trucks={num_vehicles}"
        )
        for sid in demand_sites:
            site = self.sites.get(sid)
            if not site:
                print(f"[VRPNodes]   {sid} -> WARNING: not in site_index, will be skipped")
                continue
            risk = (risk_map or {}).get(sid, "normal")
            print(
                f"[VRPNodes]   {site.name:<22}"
                f" type={site.site_type.value:<10}"
                f" risk={risk}"
            )

        # ── [MatrixCoverage] distance matrix completeness check ───────────────
        # Counts (i,j) pairs where dist == 0 (self), MISSING sentinel, or
        # effectively VERY_LARGE_COST so operators can spot data-loading gaps.
        # Also lists every site that is unreachable from ANY truck start depot.
        _mc_total = 0
        _mc_missing = 0          # sentinel value (9999 km)
        _mc_zero_offdiag = 0     # off-diagonal zero (suspect — same coordinates?)
        _mc_unreachable: List[str] = []   # site_ids unreachable from all starts

        _mc_start_ids = list({self._resolve_truck_start(t) for t in trucks})

        for _mc_from in self.site_ids:
            for _mc_to in self.site_ids:
                if _mc_from == _mc_to:
                    continue
                _mc_total += 1
                _mc_d = self._lookup_distance(_mc_from, _mc_to)
                if _mc_d >= self._MISSING_DIST_KM:
                    _mc_missing += 1
                elif _mc_d == 0.0:
                    _mc_zero_offdiag += 1

        # Per-site: unreachable if ALL start depots have a missing/sentinel entry
        for _mc_sid in self.site_ids:
            _mc_site = self.sites.get(_mc_sid)
            if not _mc_site:
                continue
            _all_missing = all(
                self._lookup_distance(_mc_start, _mc_sid) >= self._MISSING_DIST_KM
                for _mc_start in _mc_start_ids
            )
            if _all_missing:
                _mc_unreachable.append(_mc_sid)

        _mc_pct = 100.0 * _mc_missing / max(_mc_total, 1)
        print(
            f"[MatrixCoverage] pairs={_mc_total}"
            f"  missing={_mc_missing} ({_mc_pct:.1f}%)"
            f"  zero_offdiag={_mc_zero_offdiag}"
            f"  unreachable_from_depot={len(_mc_unreachable)}"
        )
        if _mc_unreachable:
            for _mc_ur in _mc_unreachable:
                _mc_ur_site = self.sites.get(_mc_ur)
                _mc_ur_name = _mc_ur_site.name if _mc_ur_site else _mc_ur
                _mc_ur_kind = ("producer" if _mc_ur_site and _mc_ur_site.is_producer
                               else "consumer" if _mc_ur_site and _mc_ur_site.is_consumer
                               else "unknown")
                print(f"  [MatrixCoverage] UNREACHABLE  {_mc_ur_name:<22} [{_mc_ur_kind}]  id={_mc_ur}")

        # ── [VRPConstraint] feasibility pre-check ─────────────────────────────
        # Report nodes that are time-infeasible from ALL trucks (certain drops).
        for sid in demand_sites:
            site = self.sites.get(sid)
            if not site:
                continue
            travel_times = []
            for truck in trucks:
                start_id = self._resolve_truck_start(truck)
                t = self._scaled_travel_minutes(
                    start_id,
                    sid,
                    traffic_time_multiplier=traffic_time_multiplier,
                    effective_speed_kmph=_effective_speed_kmph,
                )
                travel_times.append(t)
            min_travel = min(travel_times) if travel_times else 999_999
            time_ok = (min_travel + service_time_minutes) <= max_time_minutes
            cap_ok = any(truck.capacity >= 1 for truck in trucks)
            if not time_ok or not cap_ok:
                print(
                    f"[VRPConstraint] Site={site.name:<22}"
                    f" distance_to_site={min_travel}min"
                    f" service_time={service_time_minutes}min"
                    f" driver_time={max_time_minutes}min"
                    f" capacity={'OK' if cap_ok else 'FAIL'}"
                    f" -> {'TIME_INFEASIBLE' if not time_ok else 'CAPACITY_FAIL'}"
                )

        # [RelaxedModel] Time dimension: soft cap, not hard.
        # Hard limit is set to 10× the nominal shift so it is never the binding
        # constraint.  A soft upper bound at max_time_minutes penalises overtime
        # without forbidding it — this guarantees GLS can always insert a node
        # (even a distant one) as long as it lowers total penalty.
        _time_hard_limit = max_time_minutes * 10   # effective no hard cap
        routing.AddDimension(
            time_cb,
            0,                    # no waiting-time slack
            _time_hard_limit,     # very large hard cap — time is soft
            False,                # NOT fixed globally — pinned per-vehicle below
            "Time",
        )
        _time_dim = routing.GetDimensionOrDie("Time")
        # Soft upper: penalise overtime at a fixed 500 c/min (~5 EUR/min).
        # No forced utilisation lower bound — truck usage emerges purely from
        # the economic objective.  An idle truck costs nothing extra; it simply
        # won't be used unless visiting a site reduces total cost.
        _overtime_coeff = 500  # 500 c/min = 5 EUR/min overtime
        for v in range(num_vehicles):
            _time_dim.SetCumulVarSoftUpperBound(
                routing.End(v), max_time_minutes, _overtime_coeff
            )

        # ── Time-dependent flaring penalty on arrival ─────────────────────────
        # For each producer in the model: if the truck arrives AFTER htc (the
        # point at which the site starts flaring), the objective accumulates
        #   penalty_per_minute × max(0, arrival_minutes − htc_minutes)
        # This fires even when the node IS visited — it penalises late service,
        # not just skipping.  SetCumulVarSoftUpperBound on the Time dimension
        # is the correct OR-Tools primitive for this: it adds a linear penalty
        # for every minute the cumul exceeds the upper bound.
        #
        # Rate: derived from site's production rate and flaring cost (EUR/MWh).
        # Minimum floor: 3000 cents/min (30 EUR/min = 1800 EUR/h) so that
        # 1 h of flaring >> cost of activating an extra truck.
        _MIN_FLARING_PENALTY_CPM = 3000  # cents per minute floor
        _max_delay_min = max(1, int(self.config.max_driver_hours * 60))
        print(f"[DEBUG] _max_delay_min={_max_delay_min}")
        for _fp_sid in list(demand_sites) + list(guaranteed_hub_sites or []):
            if _fp_sid not in self.site_index:
                continue
            _fp_site = self.sites.get(_fp_sid)
            if not (_fp_site and _fp_site.is_producer):
                continue
            if _fp_site.name == self.MALMI_SITE_NAME:
                continue
            _fp_htc_h = (_htc_map or {}).get(_fp_sid, 999.0)
            if _fp_htc_h >= self.config.max_driver_hours:
                continue  # won't flare within the shift window
            _fp_htc_min = int(_fp_htc_h * 60)
            # Compute site flaring rate in EUR/h
            _fp_prod_mwh_h = None
            if _fp_site.production:
                _fp_prod_mwh_h = _fp_site.production.effective_mwh_per_h
                if _fp_prod_mwh_h is None and _fp_site.production.effective_kg_per_h:
                    _fp_prod_mwh_h = _fp_site.production.effective_kg_per_h * self._KG_TO_MWH
            _fp_flare_rate = _fp_site.flaring_cost_eur_mwh or 50.0
            _fp_eur_per_h = (
                (_fp_prod_mwh_h * _fp_flare_rate * 2.0) if (_fp_prod_mwh_h and _fp_prod_mwh_h > 0) else 0.0
            )
            _fp_cpm_raw = max(
                _MIN_FLARING_PENALTY_CPM,
                int(_fp_eur_per_h / 60.0 * _cost_scale),
            )
            _fp_cpm = self._capped_cpm(_fp_cpm_raw, _max_delay_min)
            _fp_node = _site_routing_index(_fp_sid)
            if _fp_node < 0:
                logger.info("[FlaringTimeSkip] %s has no service routing index", _fp_sid)
                continue
            _time_dim.SetCumulVarSoftUpperBound(_fp_node, _fp_htc_min, _fp_cpm)
            print(
                f"[FlaringTime] {_fp_site.name:<22}"
                f" htc={_fp_htc_h:.2f}h ({_fp_htc_min}min)"
                f" rate={_fp_eur_per_h:.0f}EUR/h"
                f" penalty={_fp_cpm}c/min ({_fp_cpm // _cost_scale}EUR/min)"
            )

        # ── Time-dependent stockout penalty on arrival (consumers) ───────────
        # For each consumer in the model: if the truck arrives AFTER htc (the
        # point at which the site runs out of gas), the objective accumulates
        #   penalty_per_minute × max(0, arrival_minutes − htc_minutes)
        #
        # Takkula (CNG station): 1 000 000 EUR/day = 694 EUR/min.
        # Other consumers:
        #   shortage_hours ≤ 5h → 1 000 EUR/h = 16.67 EUR/min (early tier)
        #   shortage_hours >  5h → 5 000 EUR/h = 83.33 EUR/min (late tier)
        # We pick the tier based on htc at solve time (conservative: use late
        # tier when htc ≤ STOCKOUT_BREAK_HOURS, early tier otherwise).
        _raw_takkula_cpm      = int(1_000_000 / 24.0 / 60.0 * _cost_scale)  # ~694 EUR/min
        _TAKKULA_STOCKOUT_CPM = self._capped_cpm(_raw_takkula_cpm, _max_delay_min)
        _STOCKOUT_EARLY_CPM   = int(self.STOCKOUT_RATE_EARLY_EUR_H / 60.0 * _cost_scale)
        _STOCKOUT_LATE_CPM    = int(self.STOCKOUT_RATE_LATE_EUR_H  / 60.0 * _cost_scale)
        print(
            f"[PenaltyCap] Takkula CPM: raw={_raw_takkula_cpm // _cost_scale}EUR/min"
            f" → capped={_TAKKULA_STOCKOUT_CPM // _cost_scale}EUR/min"
            f" (cap={self.PENALTY_CAP_EUR:.0f}EUR / {_max_delay_min}min)"
        )

        for _sp_sid in demand_sites:
            if _sp_sid not in self.site_index:
                continue
            _sp_site = self.sites.get(_sp_sid)
            if not (_sp_site and _sp_site.is_consumer):
                continue
            _sp_htc_h = (_htc_map or {}).get(_sp_sid, 999.0)
            if _sp_htc_h >= self.config.max_driver_hours:
                continue  # won't deplete within the shift window
            _sp_htc_min = int(_sp_htc_h * 60)
            if _sp_site.name == self.TAKKULA_SITE_NAME:
                _sp_cpm = _TAKKULA_STOCKOUT_CPM
            elif _sp_htc_h <= self.STOCKOUT_BREAK_HOURS:
                _sp_cpm = _STOCKOUT_LATE_CPM    # already near/past crisis → late rate
            else:
                _sp_cpm = _STOCKOUT_EARLY_CPM
            _sp_node = _site_routing_index(_sp_sid)
            if _sp_node < 0:
                logger.info("[StockoutTimeSkip] %s has no service routing index", _sp_sid)
                continue
            _time_dim.SetCumulVarSoftUpperBound(_sp_node, _sp_htc_min, _sp_cpm)
            print(
                f"[StockoutTime] {_sp_site.name:<22}"
                f" htc={_sp_htc_h:.2f}h ({_sp_htc_min}min)"
                f" penalty={_sp_cpm}c/min ({_sp_cpm // _cost_scale}EUR/min)"
            )

        # ── Handling fee constants (used in arc callback) ────────────────────
        _handling_cents = int(self.config.handling_fee_eur * _cost_scale)

        # Set of service nodes: demand sites (producers + consumers in demand_sites).
        # Handling is charged for each arc ARRIVING at a service node.
        _service_node_set = frozenset(
            self.site_index[sid]
            for sid in demand_sites
            if sid in self.site_index
        )
        # Physical start nodes: arcs FROM these nodes are excluded from handling
        # to avoid charging for the depot departure (the start site is already
        # "visited" before the route begins — no swap service occurs there).
        _start_physical_nodes = frozenset(starts)

        # ── Capacity dimension (real container flow) ──────────────────────────
        # Tracks FULL containers on the truck:
        #   Producer stop: +1 (truck picks up a full container)
        #   Consumer stop: -1 (truck delivers a full container)
        #   Other nodes  :  0
        # Physical constraints:
        #   0 ≤ cumul ≤ truck.capacity at all times
        #   start cumul = initial_load (full containers already aboard from previous day)
        # A truck with 0 fulls cannot serve a consumer (cumul would go below 0).
        # A truck at full capacity cannot pick up more fulls from producers.
        # This correctly enforces producer-first ordering: trucks must load full
        # containers at a producer before delivering to any consumer.
        def container_flow_callback(from_index, to_index):
            tn = manager.IndexToNode(to_index)
            if tn >= _num_sites:
                return 0  # dummy end node
            # Producers always give +1 regardless of whether they are demand sites.
            # This is essential: trucks start empty and must be able to load full
            # containers at any producer (including non-urgent ones not in demand_sites)
            # before visiting consumers.  The capacity constraint (cumul ≥ 0) then
            # enforces that trucks visit a producer BEFORE every consumer.
            if tn in _producer_node_indices:
                return 1   # pick up full from producer (demand or non-demand)
            sid = _site_ids[tn]
            if sid not in demand_sites_set:
                return 0  # non-demand consumer: pass-through (no container change)
            if tn in _consumer_node_indices:
                return -1  # deliver full to consumer (demand sites only)
            return 0

        container_flow_cb = routing.RegisterTransitCallback(container_flow_callback)
        # ── [FlowRelax] Offset-based capacity dimension ───────────────────────────
        # OR-Tools dimension cumul is always ≥ 0 (hard lower bound).  To allow
        # temporary negative real load (consumer-before-producer during route
        # construction), we shift the entire scale up by _FLOW_OFFSET:
        #
        #   cumul  = real_load + _FLOW_OFFSET
        #   real_load = 0  →  cumul = _FLOW_OFFSET   (neutral / start point)
        #   real_load < 0  →  cumul < _FLOW_OFFSET   (consumer-first, allowed)
        #   real_load = -_FLOW_OFFSET → cumul = 0    (hard floor: never exceeded)
        #
        # container_flow_callback is unchanged (+1 producer / -1 consumer).
        # Start cumul is pinned EXACTLY to _FLOW_OFFSET + initial_load via SetMin=SetMax.
        # When initial_load=0: cumul=FLOW_OFFSET (real_load=0, truck starts empty).
        # When initial_load>0: cumul=FLOW_OFFSET+n (real carry-over from previous day).
        #
        # End-of-route soft bounds penalise finishing with negative real load
        # (more deliveries than pickups) and overloading, without hard-blocking.
        _FLOW_OFFSET      = self.MAX_FLEET_CAPACITY  # fixed constant: fleet capacity is bounded by MAX_FLEET_CAPACITY
        _cap_soft_penalty = int(self.config.handling_fee_eur * _cost_scale * 10)

        # Balance penalty: penalise trucks ending with real_load != 0.
        # Physical imbalance = |end_cumul − FLOW_OFFSET|.
        # Applied as soft bounds on the Capacity dimension end cumul (no separate dimension).
        #
        # Balance penalty: cost of a dedicated rebalancing trip per stranded container.
        # Formula (operator-defined): 3 × avg_dist_km × cost_per_km × contingency
        #                            + vehicle_activation + min_billed_km × cost_per_km
        # avg_dist_km is computed from the real distance matrix so the penalty scales
        # with the actual geography of this deployment.
        _all_dists = [
            self._distance_matrix[i][j]
            for i in range(self.num_sites)
            for j in range(self.num_sites)
            if i != j and 0 < self._distance_matrix[i][j] < self._MISSING_DIST_KM
        ]
        _avg_dist_km = (sum(_all_dists) / len(_all_dists)) if _all_dists else 150.0
        _balance_per_container_eur = (
            3.0 * _avg_dist_km * self.config.cost_per_km_eur * self.config.contingency_multiplier
            + self.VEHICLE_ACTIVATION_COST_EUR
            + self.config.min_billed_km * self.config.cost_per_km_eur
        )
        _base_balance = int(_balance_per_container_eur * self.COST_SCALE)
        # Balance multiplier:
        #   final day (or single-day)   → strong global-balance push so the full
        #                                 horizon ends with trucks empty whenever possible
        #   intermediate multi-day day  → 1× — allow carry-over between days freely
        if _is_final_day or not optimize_days_mode:
            _balance_multiplier = 12
        else:
            _balance_multiplier = 1
        if any(
            t.force_end is not None and t.force_end.day_index == current_day
            for t in trucks
        ):
            _balance_multiplier += 4
        _BALANCE_PENALTY_STRONG = _balance_multiplier * _base_balance

        routing.AddDimensionWithVehicleCapacity(
            container_flow_cb,
            0,                                                     # no slack
            [_FLOW_OFFSET + truck.capacity for truck in trucks],   # max cumul: offset + capacity
            False,   # start value NOT fixed to zero — pinned per-vehicle below
            "Capacity",
        )
        cap_dim = routing.GetDimensionOrDie("Capacity")
        for v_idx, truck in enumerate(trucks):
            start_var = cap_dim.CumulVar(routing.Start(v_idx))
            _il = _effective_initial_loads[v_idx]
            # Pin start cumul to FLOW_OFFSET + initial_load so the solver
            # starts with the correct number of containers aboard.
            # Trucks starting at a producer with initial_load > 0 are pre-loaded
            # (containers physically picked up before route departure).
            # Trucks starting elsewhere begin empty (initial_load=0, cumul=FLOW_OFFSET).
            start_var.SetMin(_FLOW_OFFSET + _il)
            start_var.SetMax(_FLOW_OFFSET + _il)

            # Preload service time: loading containers at the start site consumes
            # real shift time before departure.  Account for this by advancing the
            # time cumul at the start node so the remaining shift budget is correct.
            # Cost: initial_load × swap_time_minutes (same as a regular pickup stop).
            # Since fix_start_cumul_to_zero=False, we pin each vehicle's start time
            # explicitly: 0 for empty trucks, preload_time_min for preloaded trucks.
            # Assertion: start_load must equal _il exactly (no virtual containers).
            _pinned_min = start_var.Min()
            _pinned_max = start_var.Max()
            _expected_cumul = _FLOW_OFFSET + _il
            if _pinned_min != _expected_cumul or _pinned_max != _expected_cumul:
                raise RuntimeError(
                    f"INVALID MODEL: truck {truck.id} start cumul"
                    f" [{_pinned_min}, {_pinned_max}] != expected {_expected_cumul}"
                    f" (initial_load={_il}, FLOW_OFFSET={_FLOW_OFFSET})"
                )

            _preload_time_min = _il * service_time_minutes if _il > 0 else 0
            _time_dim.CumulVar(routing.Start(v_idx)).SetMin(_preload_time_min)
            _time_dim.CumulVar(routing.Start(v_idx)).SetMax(_preload_time_min)
            if _il > 0:
                print(
                    f"[Preload] truck={truck.id}"
                    f" initial_load={_il} containers (carry-over from previous day)"
                    f" preload_service={_preload_time_min}min"
                )
            else:
                print(f"[StartLoad] truck={truck.id} initial_load=0 (starts empty — must visit producer first)")

            # Balance: penalise ending with any containers (real_load != 0).
            # imbalance = |end_cumul − FLOW_OFFSET|; target cumul = FLOW_OFFSET.
            # Soft bounds are on BOTH sides so the solver is pushed toward exactly
            # FLOW_OFFSET regardless of whether trucks finish over- or under-loaded.
            cap_dim.SetCumulVarSoftLowerBound(
                routing.End(v_idx),
                _FLOW_OFFSET,
                _BALANCE_PENALTY_STRONG,
            )
            cap_dim.SetCumulVarSoftUpperBound(
                routing.End(v_idx),
                _FLOW_OFFSET,
                _BALANCE_PENALTY_STRONG,
            )
        # ── Hard per-consumer lower bound: real_load ≥ 0 at every consumer visit ──
        # Only active on Layer 1 (strict).  Layer 2+ removes this hard lower bound
        # to allow the solver to construct routes even when trucks start empty — the
        # swap assignment layer handles physical feasibility after solve.
        if _solve_layer == 1:
            for _tn_hard in _consumer_node_indices:
                _node_idx_hard = _site_routing_index(self.site_ids[_tn_hard])
                if _node_idx_hard < 0:
                    continue
                cap_dim.CumulVar(_node_idx_hard).SetMin(_FLOW_OFFSET)
        else:
            print(f"[Layer{_solve_layer}] consumer hard capacity floor DISABLED (relaxed layer)")

        print(
            f"[FlowRelax] allowing temporary negative load during search"
            f" — flow offset={_FLOW_OFFSET}"
            f"  cumul range per vehicle: [{_FLOW_OFFSET}, {_FLOW_OFFSET + trucks[0].capacity}]"
            f"  (real_load range: [{-_FLOW_OFFSET}, {trucks[0].capacity}])"
            f"  consumer_hard_floor=FLOW_OFFSET (real_load≥0 enforced at consumers)"
        )

        # ── [CapacitySafety] pre-solve info log ──────────────────────────────────
        # Hard pin removed — start range is now [0, capacity] per vehicle.
        # Log consumer demand vs effective loads for visibility only.
        _consumer_demand_sids = [
            sid for sid in demand_sites
            if self.sites.get(sid) and self.sites[sid].is_consumer
        ]
        if _consumer_demand_sids:
            _all_zero_fulls = all(_effective_initial_loads[vi] == 0 for vi in range(len(trucks)))
            if _all_zero_fulls:
                print(
                    f"[CapacitySafety] {len(_consumer_demand_sids)} consumer(s) in demand;"
                    f" effective_initial_loads all 0 — solver will choose start load freely"
                    f" (range [0, capacity] per vehicle)"
                )

        logger.info(
            "[BALANCE] penalty=%dEUR/container  (%dx base=%dEUR  horizon=%dh)"
            "  optimize_days=%s"
            "  (enforced via Capacity-dimension end soft-bounds; no separate dimension)",
            _BALANCE_PENALTY_STRONG // self.COST_SCALE,
            _balance_multiplier,
            _base_balance // self.COST_SCALE,
            int(current_day * 24),
            optimize_days_mode,
        )

        # ── Disjunctions: real economic penalty (no artificial tier floors) ────
        # penalty(j) = max(handling_floor, flow_value_cents[j])
        #
        # Flow value uses the full 24h planning horizon (current_day × 24h), NOT the
        # 9h shift budget.  This matters for sites with htc between 9h and 24h (e.g.
        # Tampere htc=11h, Salo htc=15h): within a 9h shift they won't stock out, so
        # the old handling-floor penalty (40 EUR) was << routing cost (~1200 EUR) and
        # the solver rationally skipped them.  With a 24h horizon these sites receive
        # their true stockout penalty (tens of thousands EUR) and are reliably served.
        #
        # Flaring limit enforcement (hard daily cap):
        #   If a producer would flare > MAX_FLARING_PER_DAY_H (≈ 0.286 h, from the
        #   2 h/week regulatory cap) the penalty is additionally scaled by
        #   FLARING_LIMIT_PENALTY_FACTOR.
        #
        # Cumulative flaring escalation (rolling-horizon):
        #   When cumulative_flaring_hours across prior days exceeds MAX_FLARING_PER_DAY_H,
        #   producer penalties escalate quadratically to prevent further flaring.
        _flaring_penalty_factor = 1.0
        if cumulative_flaring_hours > self.MAX_FLARING_PER_DAY_H:
            _flaring_penalty_factor = min(
                3.0,
                (cumulative_flaring_hours / self.MAX_FLARING_PER_DAY_H) ** 2,
            )
            print(
                f"[FlaringCumul] cumulative={cumulative_flaring_hours:.3f}h"
                f" > limit={self.MAX_FLARING_PER_DAY_H:.3f}h/day"
                f" — producer penalties ×{_flaring_penalty_factor:.2f} (quadratic escalation)"
            )

        _planning_horizon_h = current_day * 24.0
        _truck_start_ids = [self._resolve_truck_start(t) for t in trucks]
        # Record disjunction penalties for [ObjectiveBreakdown] post-solve assertion.
        # ── Disjunction penalty: priority_score = 1 / max(htc, 1) ─────────────
        # Every site gets a penalty proportional to its urgency, regardless of
        # whether consumption/production rates are configured.  This prevents
        # sites with missing rate data from being dropped for free (penalty=0).
        #
        # Formula:  penalty = BASE_EUR × (1 / max(htc, 1)) × COST_SCALE
        #   BASE_EUR = 25 000 EUR  (max penalty when htc ≤ 1 h)
        #   htc=1  → 25 000 EUR   htc=5  → 5 000 EUR
        #   htc=10 → 2 500 EUR    htc=20 → 1 250 EUR
        #
        # No Takkula special case — Takkula's real htc drives its penalty like
        # any other site.  Sites with lower htc naturally rank higher.
        # ── Real economic penalty: cost of deferring service by the demand horizon ──
        # deferral_h = hours this site would be in crisis if not served within the
        # planning window (72h demand horizon or solver horizon, whichever is larger).
        # Takkula: 1 000 000 EUR flat for any stockout event (operator policy).
        # Producers: overflow_kg × kg_to_mwh × flaring_cost → real EUR loss.
        # Consumers: tiered stockout rate (1 000 EUR/h early, 5 000 EUR/h late).
        _penalty_horizon_h = max(_planning_horizon_h, 96.0)
        _disjunction_penalties: Dict[str, int] = {}
        for site_id in demand_sites:
            if site_id not in self.site_index:
                continue
            site_obj = self.sites.get(site_id)
            node_index = _site_routing_index(site_id)
            _is_producer = site_obj.is_producer if site_obj else False
            _htc         = _htc_map.get(site_id, 0.0)
            _deferral_h  = max(0.0, _penalty_horizon_h - _htc)

            # ── Compute base penalty from real economic cost ───────────────────
            if site_obj and site_obj.name == self.TAKKULA_SITE_NAME:
                # 1 000 000 EUR flat — any stockout event is unacceptable
                _penalty_eur = 999_000.0 if _deferral_h > 0 else 0.0
            elif _is_producer:
                _prod_rate   = (
                    site_obj.production.effective_kg_per_h
                    if site_obj and site_obj.production and site_obj.production.effective_kg_per_h
                    else 0.0
                )
                _flaring_rate = (site_obj.flaring_cost_eur_mwh or 50.0) if site_obj else 50.0
                _overflow_kg  = _prod_rate * _deferral_h
                _penalty_eur  = _overflow_kg * self._KG_TO_MWH * _flaring_rate
            else:
                # Consumer: tiered stockout over deferral window
                if _deferral_h <= 0:
                    _penalty_eur = 0.0
                elif _deferral_h <= self.STOCKOUT_BREAK_HOURS:
                    _penalty_eur = _deferral_h * self.STOCKOUT_RATE_EARLY_EUR_H
                else:
                    _penalty_eur = (
                        self.STOCKOUT_BREAK_HOURS * self.STOCKOUT_RATE_EARLY_EUR_H
                        + (_deferral_h - self.STOCKOUT_BREAK_HOURS) * self.STOCKOUT_RATE_LATE_EUR_H
                    )

            # Floor: at least handling_fee so no site is ever free to skip
            _penalty_eur = max(_penalty_eur, self.config.handling_fee_eur)
            penalty = min(int(_penalty_eur * self.COST_SCALE), self._MAX_SOFT_PENALTY_CENTS)

            # Producers: escalate ×3 if flaring would exceed daily regulatory limit
            if _is_producer:
                _flaring_today_h = max(0.0, self.config.max_driver_hours - _htc)
                if _flaring_today_h > self.MAX_FLARING_PER_DAY_H:
                    penalty = min(self._MAX_SOFT_PENALTY_CENTS, penalty * 3)
                    print(
                        f"[FlaringLimit] {site_obj.name if site_obj else site_id}:"
                        f" flaring_today={_flaring_today_h:.2f}h"
                        f" → escalated ×3  penalty={penalty // self.COST_SCALE}EUR"
                    )
                    _safe_add_disjunction(node_index, penalty, f"demand:{site_id}")
                    _disjunction_penalties[site_id] = penalty
                    continue
                if _flaring_penalty_factor > 1.0:
                    penalty = int(min(self._MAX_SOFT_PENALTY_CENTS, penalty * _flaring_penalty_factor))

            # Risk re-opt multiplier (high-risk reopt pass)
            if risk_penalty_multiplier > 1.0 and penalty > 0:
                penalty = int(min(self._MAX_SOFT_PENALTY_CENTS, penalty * risk_penalty_multiplier))

            _risk_level = (risk_map or {}).get(site_id, "")
            if _risk_level == "critical" and penalty > 0:
                penalty = int(min(self._MAX_SOFT_PENALTY_CENTS, penalty * 8))
            elif _risk_level == "warning" and penalty > 0:
                penalty = int(min(self._MAX_SOFT_PENALTY_CENTS, penalty * 3))

            print(
                f"[Penalty] {(site_obj.name if site_obj else site_id):<22}"
                f"  htc={_htc:.1f}h  deferral={_deferral_h:.1f}h"
                f"  penalty={penalty // self.COST_SCALE}EUR"
            )
            _disjunction_penalties[site_id] = penalty
            _safe_add_disjunction(node_index, penalty, f"demand:{site_id}")

        # ── Optional fill sites (soft preference — visited when time slack exists) ─
        # penalty=10 000c (100 EUR) gives fill sites strong preference over idle time.
        # At 200c/min time-utilisation penalty, visiting a 20-min fill stop saves
        # 4 000c (40 EUR) in time penalty, and the 10 000c drop penalty adds further
        # incentive.  Together they make fill-site inclusion the dominant strategy.
        _demand_set = set(demand_sites or [])
        for site_id in (fill_sites or []):
            if site_id in _demand_set or site_id not in self.site_index:
                continue
            node_index = _site_routing_index(site_id)
            _safe_add_disjunction(
                node_index,
                int(self.config.fill_site_penalty_eur * _cost_scale),
                f"fill:{site_id}",
            )

        # ── Overflow sites (beyond fleet capacity budget — urgency-based penalty) ─
        # These are sites that ranked below the max-serviceable cutoff in the
        # pre-VRP decision phase.  They still have real urgency but the fleet
        # cannot guarantee visiting all of them, so their skip penalty is capped
        # at 2× handling_fee to ensure the solver always has a feasible exit.
        # Penalty is still urgency-ordered: a less-critical overflow site costs
        # less to skip than a nearly-critical one.
        _overflow_set = set(overflow_sites or [])
        _overflow_cap_c = int(2 * self.config.handling_fee_eur * _cost_scale)  # 80 EUR default
        for site_id in (overflow_sites or []):
            if site_id in _demand_set or site_id not in self.site_index:
                continue
            _ov_site = self.sites.get(site_id)
            _ov_htc  = (_htc_map or {}).get(site_id, 0.0)
            _ov_base = self._compute_site_penalty(_ov_site, _ov_htc)
            _ov_penalty = min(_ov_base, _overflow_cap_c)
            node_index = _site_routing_index(site_id)
            _safe_add_disjunction(node_index, _ov_penalty, f"overflow:{site_id}")
        if _overflow_set:
            print(
                f"[Overflow] {len(_overflow_set)} site(s) beyond fleet capacity budget"
                f" added as optional (penalty ≤ {_overflow_cap_c // _cost_scale}EUR)."
            )

        # ── Transfer hubs: incentive proportional to individual swappable containers ──
        # A container is "swappable" when its pressure is below the empty threshold
        # (each container is evaluated individually — a site at 50% average could have
        # one full container and one empty, which is very different from two half-full).
        # Penalty = n_swappable × dedicated_trip_cost:
        #   dedicated_trip_cost = handling_fee + avg_dist_to_hub × cost_per_km × contingency
        # This represents the real cost of making a separate trip later to do this swap.
        _empty_bar = self.config.usable_floor_bar * 2  # ~40 bar: effectively empty
        for site_id in (transfer_sites or []):
            if site_id in _demand_set or site_id not in self.site_index:
                continue
            _hub_site = self.sites.get(site_id)
            if _hub_site is None:
                continue
            # Count individual containers below the empty threshold
            _n_swappable = sum(
                1 for bay in _hub_site.bays
                if bay.pressure_bar < _empty_bar
            )
            if _n_swappable == 0:
                # No swappable containers — still add as optional with zero penalty
                node_index = _site_routing_index(site_id)
                _safe_add_disjunction(node_index, 0, f"transfer:{site_id}")
                continue
            # Compute distance from nearest truck start to this hub
            _hub_dist_km = min(
                (
                    self._lookup_distance(self._resolve_truck_start(t), site_id)
                    for t in trucks
                    if self._resolve_truck_start(t)
                ),
                default=_avg_dist_km,
            )
            if _hub_dist_km >= self._MISSING_DIST_KM:
                _hub_dist_km = _avg_dist_km
            _trip_cost_eur = (
                self.config.handling_fee_eur
                + _hub_dist_km * self.config.cost_per_km_eur * self.config.contingency_multiplier
            )
            _hub_penalty_eur = _n_swappable * _trip_cost_eur
            _hub_penalty_c   = int(_hub_penalty_eur * self.COST_SCALE)
            node_index = _site_routing_index(site_id)
            _safe_add_disjunction(node_index, _hub_penalty_c, f"transfer:{site_id}")
            print(
                f"[TransferHub] {_hub_site.name:<22}"
                f"  swappable={_n_swappable}  dist={_hub_dist_km:.0f}km"
                f"  penalty={_hub_penalty_eur:.0f}EUR"
            )

        # ── Producer loading hubs: ALL producers MUST be in the routing model ───
        #
        # Root cause of NO_FEASIBLE_ROUTES: if demand selection produces a
        # consumer-only active_demand list, and no producers appear in any of
        # demand_sites / fill_sites / overflow_sites / transfer_sites, trucks
        # start empty (initial_load=0) with zero accessible loading points.
        # The capacity dimension (cumul ≥ 0) then hard-blocks EVERY consumer
        # visit → solver cannot construct any route → returns NULL.
        #
        # Fix: iterate self.sites directly (authoritative, never stale) and add
        # every producer that is not already in the model as a penalty=0 disjunction.
        # guaranteed_hub_sites (from the caller) are added first so the solver
        # is aware of explicitly-requested hubs even before the auto-scan.
        _already_in_model = _demand_set | set(fill_sites or []) | set(transfer_sites or []) | _overflow_set
        _hub_added: List[str] = []   # site_ids added as loading hubs this call

        def _hub_penalty_cents(hub_site) -> int:
            """Disjunction penalty for a producer loading hub not in demand_sites.

            Non-demand producers are optional loading points: penalty=0 means
            the solver visits them only when economically useful.
            Exception: Malmi gets its imbalance penalty so it is not free.
            """
            if hub_site and hub_site.name == self.MALMI_SITE_NAME:
                _floor = self.config.usable_floor_bar
                _supply = sum(
                    get_normalized_kg(effective_pressure_bar(b.pressure_bar), _floor)
                    for b in hub_site.bays
                    if b.pressure_bar >= 200
                )
                _demand = sum(
                    pressure_to_kg(250) - pressure_to_kg(b.pressure_bar)
                    for b in hub_site.bays
                    if b.pressure_bar < 50
                )
                return int(abs(_supply - _demand) * self.MALMI_IMBALANCE_K_EUR_PER_KG * _cost_scale)
            return 0

        # Pass 1: guaranteed hubs from caller (explicit producer list)
        for _gh_sid in (guaranteed_hub_sites or []):
            if _gh_sid in _already_in_model or _gh_sid not in self.site_index:
                continue
            _gh_site = self.sites.get(_gh_sid)
            if not _gh_site or not _gh_site.is_producer:
                continue
            # PhantomDepots: producer start sites now have valid service routing indices;
            # no longer skip them here.
            _gh_node = _site_routing_index(_gh_sid)
            _safe_add_disjunction(_gh_node, _hub_penalty_cents(_gh_site), f"guaranteed_hub:{_gh_sid}")
            _already_in_model.add(_gh_sid)
            _hub_added.append(_gh_sid)

        # Pass 2: auto-scan self.sites for any remaining producers not yet in model
        for _lh_sid, _lh_site in self.sites.items():
            if _lh_sid in _already_in_model:
                continue
            if not _lh_site.is_producer:
                continue
            # PhantomDepots: producer start sites now have valid service routing indices;
            # no longer skip them here.
            if _lh_sid not in self.site_index:
                # Site exists but has no matrix entry — cannot route to it; skip.
                print(f"[LoadingHubs] WARN: producer {_lh_sid!r} missing from"
                      " site_index — no matrix entry, cannot add to model")
                continue
            _lh_node = _site_routing_index(_lh_sid)
            _safe_add_disjunction(_lh_node, _hub_penalty_cents(_lh_site), f"loading_hub:{_lh_sid}")
            _already_in_model.add(_lh_sid)
            _hub_added.append(_lh_sid)

        # Pass 3: every remaining physical site must still be OPTIONAL.
        # Otherwise OR-Tools treats it as mandatory even if it is outside the
        # active solve set, which can silently poison ALL_UNPERFORMED fallback.
        _baseline_optional_added: List[str] = []
        _start_physical_nodes = set(starts)
        for _sid, _physical_idx in self.site_index.items():
            if _sid in _already_in_model:
                continue
            if _physical_idx in _start_physical_nodes:
                continue
            _node = _site_routing_index(_sid)
            if _safe_add_disjunction(_node, 0, f"baseline_optional:{_sid}"):
                _already_in_model.add(_sid)
                _baseline_optional_added.append(_sid)

        # ── [ModelCheck] hard guard: model MUST contain at least one producer ──
        # Count ALL producers now in the routing model (from every source).
        _model_producer_sids: List[str] = []
        for _mp_sid in _already_in_model:
            _mp_site = self.sites.get(_mp_sid)
            if _mp_site and _mp_site.is_producer:
                _model_producer_sids.append(_mp_sid)

        _n_consumer_demand = sum(
            1 for sid in demand_sites
            if self.sites.get(sid) and self.sites[sid].is_consumer
        )
        print(
            f"[ModelCheck] producers_in_model={len(_model_producer_sids)}"
            f"  hubs_added={len(_hub_added)}"
            f"  consumer_demand={_n_consumer_demand}"
        )
        if _hub_added:
            _hub_names = [
                self.sites[s].name if s in self.sites else s
                for s in _hub_added
            ]
            print(f"[LoadingHubs] Added {len(_hub_added)} producer(s) as optional"
                  f" loading nodes (penalty=0): {_hub_names}")
        if _baseline_optional_added:
            print(
                f"[BaselineOptional] Added {len(_baseline_optional_added)} non-model"
                " site(s) as zero-penalty optional nodes"
            )

        if len(_model_producer_sids) == 0 and _n_consumer_demand > 0:
            _all_trucks_empty = all(_effective_initial_loads[vi] == 0 for vi in range(len(trucks)))
            _msg = (
                f"[ModelCheck] CRITICAL: 0 producers in routing model"
                f" but {_n_consumer_demand} consumer(s) in demand."
                + (" All trucks start empty — every consumer visit is"
                   " capacity-infeasible." if _all_trucks_empty else "")
                + f" Producer site_ids in self.sites:"
                f" {[sid for sid, s in self.sites.items() if s.is_producer]}"
            )
            print(_msg)
            raise ValueError(_msg)

        # ── [PenaltyCheck] — sanity: verify penalty vs estimated route cost ────
        # With the unified economic model, penalty ≈ real flow value so ratio
        # may be < 5 for safe sites (Δt=0) — that's correct, not a misconfiguration.
        # Only flag sites where penalty == handling_floor despite being in crisis.
        _handling_floor_c = int(self.config.handling_fee_eur * _cost_scale)
        for _pc_site_id in demand_sites:
            if _pc_site_id not in self.site_index:
                continue
            _pc_site_obj = self.sites.get(_pc_site_id)
            _pc_htc = (_htc_map or {}).get(_pc_site_id, 0.0)
            _pc_penalty = self._compute_site_penalty(_pc_site_obj, _pc_htc)
            _pc_travel_times = []
            for _pc_truck in trucks:
                _pc_start = self._resolve_truck_start(_pc_truck)
                _pc_t = self._scaled_travel_minutes(
                    _pc_start,
                    _pc_site_id,
                    traffic_time_multiplier=traffic_time_multiplier,
                    effective_speed_kmph=_effective_speed_kmph,
                )
                _pc_travel_times.append(_pc_t)
            _pc_min_travel = min(_pc_travel_times) if _pc_travel_times else 0
            _pc_travel_cost_cents = int(
                max(_pc_min_travel / 60.0 * self.config.avg_speed_kmph, _min_billed_km)
                * _cost_per_km * _contingency * _cost_scale
            )
            _pc_site = self.sites.get(_pc_site_id)
            _pc_name = _pc_site.name if _pc_site else _pc_site_id
            _pc_ratio = _pc_penalty / max(_pc_travel_cost_cents, 1)
            _pc_flag = ""
            if _pc_htc < self.config.critical_hours_threshold and _pc_penalty <= _handling_floor_c:
                _pc_flag = " WARN:crisis_but_floor!"
            print(
                f"[PenaltyCheck] {_pc_name:<22} htc={_pc_htc:.1f}h"
                f"  penalty={_pc_penalty // _cost_scale}EUR"
                f"  route_est={_pc_travel_cost_cents // _cost_scale}EUR"
                f"  ratio={_pc_ratio:.1f}x{_pc_flag}"
            )

        # ── [DegeneracyCheck] arc cost distribution sanity check ─────────────
        # Sample inter-site arcs for the demand nodes and count how many would
        # have final_arc_cost ≈ 0 (flow_value ≥ routing_cost).  A large share
        # of near-zero arcs can cause the solver to pick routes arbitrarily.
        _deg_total = 0
        _deg_zero  = 0
        for _deg_sid in demand_sites[:20]:  # sample up to 20 nodes
            if _deg_sid not in self.site_index:
                continue
            _deg_ni = self.site_index[_deg_sid]
            _deg_fv = _node_flow_cents.get(_deg_ni, 0)
            for _deg_t in trucks:
                _deg_d = self._lookup_distance(self._resolve_truck_start(_deg_t), _deg_sid)
                if _deg_d <= 0 or _deg_d >= self._MISSING_DIST_KM:
                    continue
                _deg_arc_c = int(_deg_d * _cost_per_km * _contingency * _cost_scale)
                _deg_total += 1
                if _deg_arc_c <= _deg_fv:
                    _deg_zero += 1
        if _deg_total > 0:
            _deg_ratio = _deg_zero / _deg_total
            if _deg_ratio >= 0.5:
                print(
                    f"[DegeneracyWarning] {_deg_zero}/{_deg_total} sampled arcs have"
                    f" final_arc_cost ≈ 0 ({_deg_ratio:.0%}) — flow_value cap may be too high;"
                    f" solver may pick routes arbitrarily"
                )

        # ── [FirstArcTrace] post-solve infeasibility diagnosis ───────────────────
        # Fired only when solution is None (fast failure ≈ 0s indicates hard
        # constraint blocking even the trivial empty route).
        # Sections:
        #   1. Per-truck × per-model-node: first constraint that blocks each arc
        #   2. PRODUCER SUMMARY: dedicated per-producer reachability table — the
        #      most common root cause of NO_FEASIBLE_ROUTES is producers being
        #      in the model but unreachable due to missing distance entries
        def _print_first_arc_trace():
            # ── Build model node catalogue (reflects _already_in_model) ─────────
            # Sources in priority order so the most informative label wins.
            _model_nodes: Dict[int, tuple] = {}   # node_idx → (site_id, label, kind)
            for _mn_sid in demand_sites:
                if _mn_sid in self.site_index:
                    _mn_ni = self.site_index[_mn_sid]
                    _mn_is_p = _mn_ni in _producer_node_indices
                    _model_nodes[_mn_ni] = (_mn_sid, "demand", "producer" if _mn_is_p else "consumer")
            for _mn_sid in (fill_sites or []):
                if _mn_sid in self.site_index and self.site_index[_mn_sid] not in _model_nodes:
                    _mn_ni = self.site_index[_mn_sid]
                    _model_nodes[_mn_ni] = (_mn_sid, "fill",
                                             "producer" if _mn_ni in _producer_node_indices else "consumer")
            for _mn_sid in (overflow_sites or []):
                if _mn_sid in self.site_index and self.site_index[_mn_sid] not in _model_nodes:
                    _mn_ni = self.site_index[_mn_sid]
                    _model_nodes[_mn_ni] = (_mn_sid, "overflow",
                                             "producer" if _mn_ni in _producer_node_indices else "consumer")
            for _mn_sid in (transfer_sites or []):
                if _mn_sid in self.site_index and self.site_index[_mn_sid] not in _model_nodes:
                    _mn_ni = self.site_index[_mn_sid]
                    _model_nodes[_mn_ni] = (_mn_sid, "transfer", "producer")
            # Loading hubs and guaranteed hubs: all producers in _already_in_model
            # not captured by the above
            for _mn_sid in _already_in_model:
                if _mn_sid not in self.site_index:
                    continue
                _mn_ni = self.site_index[_mn_sid]
                if _mn_ni in _model_nodes:
                    continue
                _mn_site = self.sites.get(_mn_sid)
                if _mn_site and _mn_site.is_producer:
                    _model_nodes[_mn_ni] = (_mn_sid, "hub", "producer")

            _model_producers = {ni: (sid, lbl) for ni, (sid, lbl, kind) in _model_nodes.items()
                                 if kind == "producer"}
            _model_consumers = {ni: (sid, lbl) for ni, (sid, lbl, kind) in _model_nodes.items()
                                 if kind == "consumer"}

            print(f"\n{'='*72}")
            print(f"[FirstArcTrace] NO_FEASIBLE_ROUTES  day={current_day}"
                  f"  model_nodes={len(_model_nodes)}"
                  f"  producers={len(_model_producers)}"
                  f"  consumers={len(_model_consumers)}")

            # ── Section 1: START NODE OVERLAP WARNING ────────────────────────────
            # When a vehicle start node == a demand node, OR-Tools auto-satisfies
            # the disjunction and container_flow_callback does NOT fire at start.
            # Producer at start → truck does NOT get +1 automatically.
            for _vi, _t in enumerate(trucks):
                _sn = starts[_vi]
                if _sn in _model_nodes:
                    _sid_ov, _lbl_ov, _kind_ov = _model_nodes[_sn]
                    _site_ov = self.sites.get(_sid_ov)
                    _name_ov = _site_ov.name if _site_ov else _sid_ov
                    print(f"  ⚠ START=MODEL overlap  truck={_t.id}"
                          f"  node={_sn} ({_name_ov}) [{_lbl_ov}/{_kind_ov}]"
                          f"  — flow callback skipped at start;"
                          f" initial_load={getattr(_t,'initial_load',0)} must cover this")

            # ── Helper: single-arc feasibility verdict ───────────────────────────
            def _arc_verdict(sn: int, ni: int, init_cap: int, cap_max: int,
                              sid: str) -> str:
                _dm = dist_ext[sn][ni]
                if _dm >= _missing_dist_m:
                    return "BLOCKED: no distance matrix entry"
                _tm = time_ext[sn][ni]
                _sm = (TRANSFER_SERVICE_MINUTES if ni in _arc_transfer_nodes
                       else service_time_minutes)
                _tot = _tm + _sm
                if _tot > max_time_minutes:
                    return f"BLOCKED: time ({_tot}min > {max_time_minutes}min)"
                # Capacity (container flow)
                if ni in _producer_node_indices:
                    _cd = 1
                elif sid not in demand_sites_set:
                    _cd = 0
                elif ni in _consumer_node_indices:
                    _cd = -1
                else:
                    _cd = 0
                _ca = init_cap + _cd
                if not (0 <= _ca <= cap_max):
                    return f"BLOCKED: capacity ({init_cap}{_cd:+d}={_ca} ∉ [0,{cap_max}])"
                return f"OK  {_dm//1000:.0f}km  {_tot}min  cap:{init_cap}{_cd:+d}→{_ca}"

            # ── Section 2: FIRST MOVE ANALYSIS (all nodes) ──────────────────────
            print(f"\n  === FIRST MOVE ANALYSIS ===")
            for _vi, _truck in enumerate(trucks):
                _sn      = starts[_vi]
                _ic      = _effective_initial_loads[_vi]   # effective (may be pre-loaded)
                _cm      = _truck.capacity
                _sid_s   = self._resolve_truck_start(_truck)
                print(f"\n  [Truck {_truck.id}]  start={_sid_s} (node {_sn})"
                      f"  initial_load={_ic}/{_cm}")
                _reachable = 0
                for _ni, (_sid, _lbl, _kind) in sorted(
                        _model_nodes.items(), key=lambda x: (x[1][2], x[1][1])):
                    if _ni == _sn:
                        continue
                    _site = self.sites.get(_sid)
                    _name = _site.name if _site else _sid
                    _v = _arc_verdict(_sn, _ni, _ic, _cm, _sid)
                    if _v.startswith("OK"):
                        _reachable += 1
                    print(f"    [{_kind:<8} {_lbl:<8}] {_name:<22}  {_v}")
                if _reachable == 0:
                    print(f"  *** {_truck.id}: ZERO reachable first moves ***")

            # ── Section 3: PRODUCER SUMMARY ──────────────────────────────────────
            # One line per producer showing distance from every truck start.
            # Makes it instantly obvious whether producers are unreachable due to
            # missing matrix entries vs time vs constraint.
            print(f"\n  === PRODUCER SUMMARY ===")
            if not _model_producers:
                print("  *** NO PRODUCERS IN MODEL — root cause of infeasibility ***")
            else:
                _hdr_trucks = "  ".join(f"{t.id}(load={getattr(t,'initial_load',0)})"
                                        for t in trucks)
                print(f"  {'Producer':<22} {'Label':<8}  {_hdr_trucks}")
                for _pni, (_psid, _plbl) in sorted(_model_producers.items()):
                    _psite = self.sites.get(_psid)
                    _pname = _psite.name if _psite else _psid
                    _cols = []
                    for _vi, _truck in enumerate(trucks):
                        _sn = starts[_vi]
                        _ic = _effective_initial_loads[_vi]   # effective load
                        _cm = _truck.capacity
                        _v = _arc_verdict(_sn, _pni, _ic, _cm, _psid)
                        # Compact form for summary
                        if "BLOCKED: no distance" in _v:
                            _cols.append("MISSING_DIST")
                        elif "BLOCKED: time" in _v:
                            _cols.append("TIME_FAIL")
                        elif "BLOCKED: capacity" in _v:
                            _cols.append("CAP_FAIL")
                        elif "BLOCKED: balance" in _v:
                            _cols.append("BAL_FAIL")
                        else:
                            _dm = dist_ext[_sn][_pni]
                            _cols.append(f"OK({_dm//1000:.0f}km)")
                    _all_blocked = all(c != c.startswith("OK") for c in _cols)
                    _row_flag = "  ← ALL BLOCKED" if all("OK" not in c for c in _cols) else ""
                    print(f"  {_pname:<22} {_plbl:<8}  {'  '.join(_cols)}{_row_flag}")

            # ── Section 4: ROOT CAUSE SUMMARY ────────────────────────────────────
            print(f"\n  === ROOT CAUSE SUMMARY ===")
            _all_trucks_empty = all(_effective_initial_loads[vi] == 0 for vi in range(len(trucks)))
            _any_prod_reachable = any(
                "OK" in _arc_verdict(starts[vi], pni,
                                     _effective_initial_loads[vi],
                                     trucks[vi].capacity, psid)
                for vi in range(len(trucks))
                for pni, (psid, _) in _model_producers.items()
            ) if _model_producers else False

            if not _model_producers:
                print("  CAUSE: Zero producers in model — trucks cannot load full containers")
            elif not _any_prod_reachable:
                print("  CAUSE: Producers in model but NONE reachable from any truck start")
                print("         Most likely: missing distance matrix entries for all producers")
                print(f"         Producer site_ids: {list(_model_producers.values())}")
            elif _all_trucks_empty and _model_consumers:
                print("  CAUSE: Trucks start empty — solver must visit producer before consumer")
                print("         Check: are producer arcs being selected by PATH_CHEAPEST_ARC?")
                print("         Possible issue: producer arc costs > consumer penalty → solver")
                print("         pays consumer penalty rather than route through producer first")
            else:
                print("  CAUSE: Unknown — producers reachable and trucks not all empty.")
                print("         Check time dimension and balance soft-bound penalties.")
            print(f"{'='*72}\n")

        # ── [ModelStructure] routing model integrity check ────────────────────────
        # Verifies the model OR-Tools received is internally consistent before
        # committing to a solve.  Catches misconfigured nodes, isolated vehicles,
        # and degenerate distance matrices early — before the solver wastes time.
        print(f"\n{'='*60}")
        print("=== MODEL STRUCTURE CHECK ===")
        print(f"  num_nodes (routing.Size()): {routing.Size()}")
        print(f"  num_vehicles:               {num_vehicles}")
        print(f"  num_sites (physical):       {_num_sites}")
        print(f"  dummy_end node:             {dummy_end}")

        # 1. Vehicle start / end sanity
        _ms_bad_vehicles: List[int] = []
        for _ms_v in range(num_vehicles):
            _ms_start = routing.Start(_ms_v)
            _ms_end   = routing.End(_ms_v)
            _ms_snode = manager.IndexToNode(_ms_start)
            _ms_enode = manager.IndexToNode(_ms_end)
            _ms_sid   = self.site_ids[_ms_snode] if _ms_snode < _num_sites else f"dummy({_ms_snode})"
            _ms_eid   = self.site_ids[_ms_enode] if _ms_enode < _num_sites else f"dummy({_ms_enode})"
            _ms_ok    = _ms_start != -1 and _ms_end != -1
            print(
                f"  Vehicle {_ms_v}: start_idx={_ms_start}→{_ms_sid}"
                f"  end_idx={_ms_end}→{_ms_eid}"
                f"  {'OK' if _ms_ok else 'BAD'}"
            )
            if not _ms_ok:
                _ms_bad_vehicles.append(_ms_v)
        if _ms_bad_vehicles:
            print(f"  [ERROR] {len(_ms_bad_vehicles)} vehicle(s) have invalid start/end indices")

        # 2. Distance matrix: count sentinel and zero off-diagonal arcs
        _ms_total_arcs  = 0
        _ms_sentinel    = 0
        _ms_zero_offdiag = 0
        _ms_valid        = 0
        for _ms_i in range(num_nodes):
            for _ms_j in range(num_nodes):
                if _ms_i == _ms_j:
                    continue
                _ms_total_arcs += 1
                _ms_d = dist_ext[_ms_i][_ms_j]
                if _ms_d >= _missing_dist_m:
                    _ms_sentinel += 1
                elif _ms_d == 0:
                    _ms_zero_offdiag += 1
                else:
                    _ms_valid += 1
        _ms_valid_pct  = 100.0 * _ms_valid    / max(_ms_total_arcs, 1)
        _ms_sentin_pct = 100.0 * _ms_sentinel / max(_ms_total_arcs, 1)
        print(
            f"  Arc stats: total={_ms_total_arcs}"
            f"  valid={_ms_valid} ({_ms_valid_pct:.1f}%)"
            f"  sentinel={_ms_sentinel} ({_ms_sentin_pct:.1f}%)"
            f"  zero_offdiag={_ms_zero_offdiag}"
        )

        # 3. Assert at least one valid arc exists
        if _ms_valid == 0:
            print("  [ERROR] NO VALID ARCS IN MODEL — solver cannot route anything")
        else:
            print(f"  [OK] valid arc exists ({_ms_valid} arc(s))")

        # 4. Reachability from each vehicle start
        _ms_any_isolated = False
        for _ms_v in range(num_vehicles):
            _ms_snode   = manager.IndexToNode(routing.Start(_ms_v))
            _ms_reach   = sum(
                1 for _ms_j in range(num_nodes)
                if _ms_j != _ms_snode and dist_ext[_ms_snode][_ms_j] < _missing_dist_m
            )
            _ms_reach_flag = "" if _ms_reach > 0 else "  ← ISOLATED"
            print(f"  Vehicle {_ms_v} reachable nodes from start: {_ms_reach}{_ms_reach_flag}")
            if _ms_reach == 0:
                _ms_any_isolated = True
        if _ms_any_isolated:
            print("  [ERROR] at least one vehicle has ZERO reachable nodes — model is infeasible")
        else:
            print("  [OK] all vehicles can reach at least one node")

        # 5. Dimension bounds per vehicle start
        print("  --- Dimension bounds at vehicle start ---")
        for _ms_v in range(num_vehicles):
            _ms_start_idx  = routing.Start(_ms_v)
            _ms_cap_var    = cap_dim.CumulVar(_ms_start_idx)
            _ms_time_var   = _time_dim.CumulVar(_ms_start_idx)
            _ms_cap_pinned = _ms_cap_var.Min() == _ms_cap_var.Max()
            _ms_cap_flag   = " ← PINNED (hard)" if _ms_cap_pinned else " ← flexible (OK)"
            # Decode real_load from offset-shifted cumul
            _ms_real_min   = _ms_cap_var.Min() - _FLOW_OFFSET
            _ms_real_max   = _ms_cap_var.Max() - _FLOW_OFFSET
            print(
                f"  Vehicle {_ms_v}:"
                f"  capacity(cumul)=[{_ms_cap_var.Min()}, {_ms_cap_var.Max()}]{_ms_cap_flag}"
                f"  real_load=[{_ms_real_min}, {_ms_real_max}]"
                f"  time=[{_ms_time_var.Min()}, {_ms_time_var.Max()}]"
            )
        print(f"{'='*60}\n")

        # ── [DisjunctionCheck] every non-start node must have a disjunction ─────
        # OR-Tools treats any node (0.._num_sites-1) that was NOT passed to
        # AddDisjunction as MANDATORY — it MUST be visited by some vehicle.
        # ALL_UNPERFORMED (the fallback strategy) can only drop optional nodes;
        # a mandatory node it cannot serve causes an instant NULL even in fallback.
        #
        # Expected: every physical node that is not a vehicle start is in
        # _already_in_model (= received AddDisjunction in one of the loops above).
        # Any node outside this set is mandatory and will block ALL_UNPERFORMED.
        _model_node_indices_set = {
            self.site_index[s] for s in _already_in_model if s in self.site_index
        }
        _start_physical_nodes = set(starts)
        _missing_disj: List[str] = []
        for _dv_ni in range(_num_sites):
            if _dv_ni in _start_physical_nodes:
                continue   # vehicle start nodes are auto-satisfied by OR-Tools
            if _dv_ni not in _model_node_indices_set:
                _dv_sid  = self.site_ids[_dv_ni]
                _dv_site = self.sites.get(_dv_sid)
                _dv_name = _dv_site.name if _dv_site else _dv_sid
                print(
                    f"[ERROR] node without disjunction: {_dv_name!r}"
                    f" (id={_dv_sid}, node={_dv_ni})"
                    f" — mandatory node will block ALL_UNPERFORMED"
                )
                _missing_disj.append(_dv_sid)
        if _missing_disj:
            logger.warning(
                "[DisjunctionCheck] %d mandatory node(s) lack a disjunction: %s"
                " — adding penalty=0 disjunctions to prevent ALL_UNPERFORMED failure",
                len(_missing_disj), _missing_disj,
            )
            for _dv_sid in _missing_disj:
                _dv_node = _site_routing_index(_dv_sid)
                _safe_add_disjunction(_dv_node, 0, f"fallback:{_dv_sid}")
                _model_node_indices_set.add(self.site_index[_dv_sid])
        else:
            print(
                f"[DisjunctionCheck] OK — all {_num_sites} non-start nodes"
                f" have disjunctions ({len(_model_node_indices_set)} in model)"
            )

        # ── [FeasibilityCheck] pre-solve reachability guard ──────────────────────
        # Verify that at least one model node is reachable (non-sentinel distance)
        # from at least one truck start node.  If this fails the solver will
        # instantly return NULL — catching it here produces a clearer message and
        # avoids wasting the search budget.
        # (_model_node_indices_set already built above, including any late additions)
        _feasible_first_move = any(
            dist_ext[starts[_vi]][_mn] < _missing_dist_m
            for _vi in range(num_vehicles)
            for _mn in _model_node_indices_set
            if _mn != starts[_vi]
        )
        if not _feasible_first_move:
            logger.warning(
                "[FeasibilityCheck] No reachable node from any truck start"
                " (%d truck(s), %d model node(s)) — all arcs are sentinel",
                num_vehicles, len(_model_node_indices_set),
            )
            print("[INFEASIBILITY DETECTED]")
            self._debug_infeasibility(
                demand_sites=demand_sites,
                trucks=trucks,
                htc_map=_htc_map,
                dist_ext=dist_ext,
                starts=starts,
                num_vehicles=num_vehicles,
            )
            raise InfeasibleRoutingError(
                f"No reachable model node from any truck start"
                f" ({num_vehicles} truck(s), {len(_model_node_indices_set)} model node(s))"
                f" — all distance-matrix arcs are sentinel values"
            )
        print(
            f"[FeasibilityCheck] OK — at least one reachable model node from truck starts"
            f" ({len(_model_node_indices_set)} model node(s))"
        )

        # ── Search parameters ─────────────────────────────────────────────────
        # Hard per-solve cap: each individual solve (including re-opt) must
        # complete within MAX_SECONDS_PER_SOLVE seconds regardless of the caller's
        # max_search_seconds value.  This prevents a 4-day horizon + re-opt from
        # running 4 × 2 × max_search_seconds, which would far exceed any frontend
        # timeout.  Callers should pass max_search_seconds ≤ 15 for interactive use.
        MAX_SECONDS_PER_SOLVE = 15
        _solve_seconds = min(max_search_seconds, MAX_SECONDS_PER_SOLVE)

        # ── Deterministic seeding ─────────────────────────────────────────────
        # OR-Tools 9.15 has no top-level random_seed on RoutingSearchParameters.
        # The correct mechanism for this version is routing.solver().ReSeed(n),
        # which seeds the CP solver's internal RNG used by GLS.
        _seed = getattr(self, "_sensitivity_random_seed", 42)
        routing.solver().ReSeed(_seed)

        # ── Single solve — PARALLEL_CHEAPEST_INSERTION + GLS ─────────────────
        # The solver runs ONCE per scenario call.  All retry / fallback logic
        # belongs exclusively to the ScenarioEvaluator (decision layer).
        # If no solution is found, InfeasibleRoutingError is raised so the
        # evaluator can mark this scenario invalid and select an alternative.
        #
        # Every demand node has a real-cost AddDisjunction, so PCI can always
        # produce the "drop everything" solution — genuine infeasibility is rare
        # and means the model has no feasible arc for any vehicle.
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        params.time_limit.seconds = _solve_seconds
        _layer_labels = {1: "strict_solver", 2: "relaxed_solver", 3: "distance_fallback"}
        print(
            f"[VRPSolve] layer={_solve_layer}({_layer_labels.get(_solve_layer, '?')})"
            f"  strategy=PATH_CHEAPEST_ARC"
            f"  producer_first_gate={'ON' if (_producer_first_active and _solve_layer == 1) else 'OFF'}"
        )

        import time as _time
        _solve_start = _time.monotonic()
        print(
            f"[RelaxedModel] running with soft constraints"
            f"  time=soft(10×hard+soft@1×)"
            f"  capacity=soft(2×hard+soft@1×)"
            f"  balance=capacity-end-bounds({_balance_multiplier}x)"
            f"  producer_first=disabled"
        )
        if force_exact_days:
            for v in range(num_vehicles):
                routing.SetFixedCostOfVehicle(100000, v)

        # ── #1/#5 Initial solution hint: hot-start + geographic clustering ──────
        # Priority: (1) stored previous solution (hot-start), (2) geographic
        # clusters using truck home positions as centroids.
        # OR-Tools ignores infeasible hints and falls back to its own construction.
        def _try_set_routing_hint() -> None:
            try:
                hint_routes: Optional[List[List[str]]] = None

                # (1) Hot-start from previous solution
                if self._last_hint_routes and len(self._last_hint_routes) == num_vehicles:
                    # Validate: keep only sites that are still in the model
                    _active = set(demand_sites or []) | set(fill_sites or [])
                    _filtered = [
                        [s for s in route if s in self.site_index and s in _active]
                        for route in self._last_hint_routes
                    ]
                    if any(_filtered):
                        hint_routes = _filtered
                        print(f"[Hint] Using hot-start from previous solution: {[len(r) for r in _filtered]} sites/truck")

                # (2) Geographic clustering — only if no hot-start available
                if hint_routes is None:
                    _coords: Dict[str, tuple] = {}
                    for _sid, _site in self.sites.items():
                        lat = getattr(_site, 'latitude', None)
                        lon = getattr(_site, 'longitude', None)
                        if lat is not None and lon is not None:
                            _coords[_sid] = (lat, lon)

                    _home_coords: Dict[int, tuple] = {}  # v_idx → (lat, lon)
                    for _vi, _t in enumerate(trucks):
                        home_id = _t.home_site_id
                        if home_id in _coords:
                            _home_coords[_vi] = _coords[home_id]
                        elif self._resolve_truck_start(_t) in _coords:
                            _home_coords[_vi] = _coords[self._resolve_truck_start(_t)]

                    if len(_home_coords) >= 2:
                        _demand_with_coords = [
                            s for s in (demand_sites or [])
                            if s in self.site_index and s in _coords
                        ]
                        if _demand_with_coords:
                            _clusters: Dict[int, List[str]] = {vi: [] for vi in range(num_vehicles)}
                            for _s in _demand_with_coords:
                                _slat, _slon = _coords[_s]
                                _best_vi = min(
                                    _home_coords.keys(),
                                    key=lambda vi: self._haversine_km(
                                        _home_coords[vi][0], _home_coords[vi][1], _slat, _slon
                                    )
                                )
                                _clusters[_best_vi].append(_s)
                            hint_routes = [_clusters.get(vi, []) for vi in range(num_vehicles)]
                            print(f"[Hint] Geographic cluster hint: {[len(r) for r in hint_routes]} sites/truck")

                if not hint_routes:
                    return

                # Convert site IDs → OR-Tools node indices
                _seen_hint_nodes: set[int] = set()
                _node_routes = []
                for route in hint_routes:
                    _route_nodes: List[int] = []
                    for site_id in route:
                        if site_id not in self.site_index:
                            continue
                        _node_idx = _site_routing_index(site_id)
                        if _node_idx < 0 or _node_idx in _seen_hint_nodes:
                            continue
                        _seen_hint_nodes.add(_node_idx)
                        _route_nodes.append(_node_idx)
                    _node_routes.append(_route_nodes)
                assignment = routing.ReadAssignmentFromRoutes(_node_routes, True)
                if assignment:
                    routing.SetFirstSolutionHint(assignment)
                    print("[Hint] First-solution hint accepted by OR-Tools")
                else:
                    print("[Hint] OR-Tools rejected hint (infeasible) — using default construction")
            except Exception as _he:
                print(f"[Hint] Failed to set hint: {_he}")

        _try_set_routing_hint()

        # ── [SEED_CHECK] count feasible start→producer→consumer paths ───────────
        # If count > 0 but solver returns None, this is a construction failure —
        # not genuine infeasibility — and the greedy fallback should be used.
        _demand_consumer_nodes = {
            self.site_index[s] for s in demand_sites
            if s in self.site_index and self.sites.get(s) and self.sites[s].is_consumer
        }
        _feasible_seed_count = 0
        for _vi_sc in range(num_vehicles):
            _sn_sc = starts[_vi_sc]
            for _pni_sc in _producer_node_indices:
                _d_sp = dist_ext[_sn_sc][_pni_sc]
                if _d_sp >= _missing_dist_m:
                    continue
                _t_sp = time_ext[_sn_sc][_pni_sc] + service_time_minutes
                for _cni_sc in _demand_consumer_nodes:
                    if dist_ext[_pni_sc][_cni_sc] >= _missing_dist_m:
                        continue
                    if _t_sp + time_ext[_pni_sc][_cni_sc] + service_time_minutes <= max_time_minutes:
                        _feasible_seed_count += 1
        print(f"[SEED_CHECK] feasible_single_routes={_feasible_seed_count}")
        if _feasible_seed_count > 0:
            print("[SEED_CHECK] feasible construction paths exist — if routes=0 after solve: CONSTRUCTION_FAILURE_NOT_INFEASIBLE")

        print(
            f"[VRPSolve] START day={current_day} reopt={_reopt_attempt}"
            f" budget={_solve_seconds}s vehicles={num_vehicles} demand={len(demand_sites)}"
        )
        solution = routing.SolveWithParameters(params)
        _solve_elapsed = _time.monotonic() - _solve_start
        print(
            f"[VRPSolve] END day={current_day} reopt={_reopt_attempt}"
            f" layer={_solve_layer} elapsed={_solve_elapsed:.2f}s solution={'found' if solution else 'NONE'}"
        )

        if not solution:
            _print_first_arc_trace()
            print(f"[Layer{_solve_layer}] solver returned no solution — {_layer_labels.get(_solve_layer, '?')}")
            self._debug_infeasibility(
                demand_sites=demand_sites,
                trucks=trucks,
                htc_map=_htc_map,
                dist_ext=dist_ext,
                starts=starts,
                num_vehicles=num_vehicles,
            )

            # ── Layer escalation: strict → relaxed, then greedy ──────────────
            if _solve_layer < 2:
                _next_layer = _solve_layer + 1
                _next_label = _layer_labels.get(_next_layer, "?")
                print(f"[Layer{_next_layer}] escalating → {_next_label}")
                logger.warning(
                    "[Layer%d] escalating from layer %d to %d (%s) day=%d",
                    _next_layer, _solve_layer, _next_layer, _next_label, current_day,
                )
                return self.solve(
                    trucks=trucks,
                    demand_sites=demand_sites,
                    max_search_seconds=max_search_seconds,
                    traffic_time_multiplier=traffic_time_multiplier,
                    risk_map=risk_map,
                    risk_score_map=risk_score_map,
                    urgency_factor_m=urgency_factor_m,
                    hours_to_critical_map=hours_to_critical_map,
                    current_day=current_day,
                    fill_sites=fill_sites,
                    transfer_sites=transfer_sites,
                    _vehicle_fixed_cost=_vehicle_fixed_cost,
                    _reopt_attempt=_reopt_attempt,
                    _container_penalty=_container_penalty,
                    _is_final_day=_is_final_day,
                    cumulative_flaring_hours=cumulative_flaring_hours,
                    risk_penalty_multiplier=risk_penalty_multiplier,
                    overflow_sites=overflow_sites,
                    planning_horizon_h=planning_horizon_h,
                    guaranteed_hub_sites=guaranteed_hub_sites,
                    optimize_days_mode=optimize_days_mode,
                    force_exact_days=force_exact_days,
                    _solve_layer=_next_layer,
                )

            # ── Layer 4: per-truck greedy fallback (unconditional) ────────────
            # Runs when all 3 OR-Tools layers fail.  No seed-count guard —
            # greedy always attempts to build at least one route per truck.
            print("[Layer4] greedy_fallback — all OR-Tools layers failed; building manual routes per truck")
            logger.warning(
                "[Layer4] GREEDY_FALLBACK day=%d — all OR-Tools layers failed;"
                " building manual routes (greedy per-truck)",
                current_day,
            )
            print("[WARNING] FALLBACK_GREEDY_USED — building manual routes")
            _svc_h = self.config.swap_time_hours
            _svc_min = service_time_minutes
            _demand_cons_list = [
                s for s in demand_sites
                if s in self.site_index and self.sites.get(s) and self.sites[s].is_consumer
            ]

            def _greedy_risk_key(sid):
                # Sort by hours_to_critical ascending — most urgent first.
                # Matches the priority_score = 1/max(htc,1) ordering used in
                # the OR-Tools disjunction penalties.
                return (hours_to_critical_map or {}).get(sid, float('inf'))

            _demand_cons_list.sort(key=_greedy_risk_key)
            _greedy_routes: List[Route] = []

            for _vi, _truck in enumerate(trucks):
                _cur_sid = self._resolve_truck_start(_truck)
                _cur_ni = self.site_index.get(_cur_sid)
                if _cur_ni is None:
                    continue
                _cur_time_min = 0.0
                _cur_dist_km = 0.0
                _cur_load = getattr(_truck, 'initial_load', 0)
                _truck_stops: list = []
                _seq = 0
                _served_cons: set = set()

                # While time remains: reload at nearest producer, then serve consumers.
                # Repeats so the truck can make multiple producer→consumer runs per day.
                while True:
                    # Find nearest reachable producer
                    _best_prod_sid = None
                    _best_prod_dist = float('inf')
                    for _psid, _psite in self.sites.items():
                        if not _psite.is_producer or _psid not in self.site_index:
                            continue
                        _pni = self.site_index[_psid]
                        _d = dist_ext[_cur_ni][_pni]
                        if _d < _missing_dist_m and _d < _best_prod_dist:
                            _best_prod_dist = _d
                            _best_prod_sid = _psid
                    if not _best_prod_sid:
                        break
                    _pni2 = self.site_index[_best_prod_sid]
                    _pd_km = _best_prod_dist / 1000.0
                    _pt_min = time_ext[_cur_ni][_pni2] + _svc_min
                    if _cur_time_min + _pt_min > max_time_minutes:
                        break
                    _cur_time_min += _pt_min
                    _cur_dist_km += _pd_km
                    _cur_load = _truck.capacity
                    _psite2 = self.sites[_best_prod_sid]
                    _truck_stops.append(RouteStop(
                        sequence=_seq,
                        site_id=_best_prod_sid,
                        site_name=_psite2.name,
                        arrival_time_hours=(_cur_time_min - _svc_min) / 60.0,
                        distance_from_previous_km=_pd_km,
                        cumulative_distance_km=_cur_dist_km,
                        service_time_hours=_svc_h,
                        truck_load_after=_cur_load,
                        load_full_after=_cur_load,
                    ))
                    _seq += 1
                    _cur_sid = _best_prod_sid
                    _cur_ni = _pni2

                    # Serve consumers sorted by hours_to_critical
                    _served_this_run = False
                    for _csid in _demand_cons_list:
                        if _cur_load <= 0:
                            break
                        if _csid in _served_cons or _csid not in self.site_index:
                            continue
                        _cni2 = self.site_index[_csid]
                        _d2 = dist_ext[_cur_ni][_cni2]
                        if _d2 >= _missing_dist_m:
                            continue
                        _d2_km = _d2 / 1000.0
                        _t2_min = time_ext[_cur_ni][_cni2] + _svc_min
                        if _cur_time_min + _t2_min > max_time_minutes:
                            continue
                        _cur_time_min += _t2_min
                        _cur_dist_km += _d2_km
                        _cur_load -= 1
                        _served_cons.add(_csid)
                        _served_this_run = True
                        _csite2 = self.sites.get(_csid)
                        _truck_stops.append(RouteStop(
                            sequence=_seq,
                            site_id=_csid,
                            site_name=_csite2.name if _csite2 else _csid,
                            arrival_time_hours=(_cur_time_min - _svc_min) / 60.0,
                            distance_from_previous_km=_d2_km,
                            cumulative_distance_km=_cur_dist_km,
                            service_time_hours=_svc_h,
                            truck_load_after=_cur_load,
                            load_full_after=_cur_load,
                        ))
                        _seq += 1
                        _cur_sid = _csid
                        _cur_ni = _cni2

                    # If no consumers could be served this pass, stop — no benefit reloading
                    if not _served_this_run:
                        break

                if _truck_stops:
                    _truck_stops[-1].load_full_after = _cur_load
                    _greedy_routes.append(Route(
                        truck_id=_truck.id,
                        day_index=current_day,
                        stops=_truck_stops,
                        start_site_id=self._resolve_truck_start(_truck),
                        end_site_id=_cur_sid,
                        start_label="FALLBACK_GREEDY",
                    ))
                    logger.warning(
                        "[Layer4] GREEDY_FALLBACK day=%d truck=%s stops=%d end=%s load=%d",
                        current_day, _truck.id, len(_truck_stops), _cur_sid, _cur_load,
                    )
                    print(f"[Layer4] truck={_truck.id} stops={len(_truck_stops)} end={_cur_sid} load={_cur_load}")

            if _greedy_routes:
                print(f"[Layer4] greedy_fallback returning {len(_greedy_routes)} route(s)")
                return _greedy_routes

            # ── Layer 5: survival plan — 1 truck to 1 most critical site ─────
            # Last resort before giving up.  Route a single truck to the site
            # with the lowest hours_to_critical.  Uses haversine if road dist
            # is missing.  Ignores capacity and optimality completely.
            print("[Layer5] SURVIVAL_PLAN — greedy produced nothing; routing 1 truck to most critical site")
            logger.warning("[Layer5] SURVIVAL_PLAN day=%d", current_day)
            _sv_demand_sorted = sorted(
                [s for s in demand_sites if s in self.site_index],
                key=lambda s: (hours_to_critical_map or {}).get(s, float('inf')),
            )
            _sv_routes: List[Route] = []
            for _sv_truck in trucks:
                _sv_start = self._resolve_truck_start(_sv_truck)
                _sv_ni = self.site_index.get(_sv_start)
                if _sv_ni is None:
                    continue
                for _sv_sid in _sv_demand_sorted:
                    _sv_cni = self.site_index[_sv_sid]
                    _sv_d = dist_ext[_sv_ni][_sv_cni]
                    if _sv_d >= _missing_dist_m:
                        # road dist missing — try haversine directly
                        _sv_fs = self.sites.get(_sv_start)
                        _sv_ts = self.sites.get(_sv_sid)
                        if (_sv_fs and _sv_ts
                                and getattr(_sv_fs, 'latitude', None) is not None
                                and getattr(_sv_ts, 'latitude', None) is not None):
                            _sv_d = int(
                                self._haversine_km(
                                    _sv_fs.latitude, _sv_fs.longitude,
                                    _sv_ts.latitude, _sv_ts.longitude,
                                ) * self._HAVERSINE_ROAD_FACTOR * 1000
                            )
                        else:
                            continue
                    _sv_d_km = _sv_d / 1000.0
                    _sv_t_min = _sv_d_km / max(1.0, _effective_speed_kmph) * 60 + service_time_minutes
                    _sv_site = self.sites.get(_sv_sid)
                    _sv_routes.append(Route(
                        truck_id=_sv_truck.id,
                        day_index=current_day,
                        stops=[RouteStop(
                            sequence=0,
                            site_id=_sv_sid,
                            site_name=_sv_site.name if _sv_site else _sv_sid,
                            arrival_time_hours=(_sv_t_min - service_time_minutes) / 60.0,
                            distance_from_previous_km=_sv_d_km,
                            cumulative_distance_km=_sv_d_km,
                            service_time_hours=self.config.swap_time_hours,
                            truck_load_after=0,
                            load_full_after=0,
                        )],
                        start_site_id=_sv_start,
                        end_site_id=_sv_sid,
                        start_label="SURVIVAL_PLAN",
                    ))
                    print(f"[Layer5] SURVIVAL_PLAN truck={_sv_truck.id} → {_sv_sid}")
                    logger.warning("[Layer5] SURVIVAL_PLAN truck=%s → site=%s", _sv_truck.id, _sv_sid)
                    break
                if _sv_routes:
                    break

            if _sv_routes:
                return _sv_routes

            # Truly no movement possible (no trucks, no reachable sites, no coords).
            logger.warning(
                "VRP: no movement possible at all (day=%d, vehicles=%d, demand=%d)"
                " — returning empty route list",
                current_day, num_vehicles, len(demand_sites),
            )
            print(f"[VRP] no movement possible day={current_day} — returning []")
            return []

        # Log which solver layer produced this solution.
        print(f"[SolverLayer] solution found at layer={_solve_layer} ({_layer_labels.get(_solve_layer, '?')})")
        logger.info("[SolverLayer] solution found at layer=%d (%s) day=%d", _solve_layer, _layer_labels.get(_solve_layer, "?"), current_day)

        # Log solver's internal objective value (cents) for validation.
        # This must match recommendation.total_cost_eur × COST_SCALE within the
        # rounding introduced by integer arithmetic in the callbacks.
        _obj = solution.ObjectiveValue()
        logger.info(
            "VRP solver objective: %d cents (%.2f EUR)",
            _obj, _obj / self.COST_SCALE,
        )

        # Log served / dropped breakdown
        dropped = self._get_dropped_sites(routing, manager, solution, demand_sites)
        served_count = len(demand_sites) - len(dropped)
        logger.info(
            "VRP result: %d/%d demand site(s) served, %d dropped  |  "
            "dropped: %s",
            served_count, len(demand_sites), len(dropped),
            dropped if dropped else "—",
        )
        if dropped:
            print(f"[DroppedNodes] {len(dropped)} site(s) not served (penalty paid):")
            for _dsid in dropped:
                _dsite = self.sites.get(_dsid)
                _dname = _dsite.name if _dsite else _dsid
                _drisk = (risk_map or {}).get(_dsid, "unknown")
                _dhtc  = (hours_to_critical_map or {}).get(_dsid)
                _dhtc_str = f"  htc={_dhtc:.1f}h" if _dhtc is not None else ""
                print(f"  - {_dname} ({_dsid})  risk={_drisk}{_dhtc_str}")

        # ── [SolutionValidity] — flag critical drops with idle trucks ─────────
        # If any critical/warning site was dropped (penalty paid) while there are
        # vehicles with unused time, the solution is operationally invalid.
        # This indicates a solver configuration issue (penalty too low, or a
        # genuine time infeasibility that needs operator attention).
        if dropped and not _reopt_attempt:
            _high_risk_dropped = [
                sid for sid in dropped
                if (risk_map or {}).get(sid) in ("critical", "warning")
            ]
            if _high_risk_dropped:
                _total_end_time_check = sum(
                    solution.Value(_time_dim.CumulVar(routing.End(_dv)))
                    for _dv in range(num_vehicles)
                )
                _available_time_check = num_vehicles * max_time_minutes
                _idle_time_check = _available_time_check - _total_end_time_check
                if _idle_time_check > max_time_minutes * 0.2:
                    # ≥ 20% of a truck-shift is idle yet critical sites dropped
                    print(
                        f"[SolutionValidity] INVALID: {len(_high_risk_dropped)} high-risk site(s)"
                        f" dropped despite {_idle_time_check}min ({_idle_time_check/60:.1f}h)"
                        f" idle truck time — solution has no operational value for:"
                        f" {[self.sites[s].name if s in self.sites else s for s in _high_risk_dropped]}"
                    )
                    logger.warning(
                        "[SolutionValidity] %d high-risk site(s) dropped with %.1fh idle"
                        " truck time — triggering reopt with higher fixed cost reduction",
                        len(_high_risk_dropped), _idle_time_check / 60,
                    )
        # ── Post-solve summary ────────────────────────────────────────────────
        print(f"[STEP] entering _extract_routes  day={current_day}")
        routes_extracted = self._extract_routes(
            manager, routing, solution, trucks, demand_sites_set,
            traffic_time_multiplier, dummy_end, current_day,
        )
        print(f"[STEP] exiting  _extract_routes  day={current_day} routes={len(routes_extracted)}")

        # ── [LoadTrace] per-stop capacity dimension trace (CRITICAL DEBUG) ────
        # Shows the number of full containers on each truck at every stop.
        # Validates: starts with 0 → producer visit → cumul +1 → consumer visit → cumul -1
        # A cumul that reaches -1 indicates a capacity constraint violation (should not happen).
        _lt_cap_dim = routing.GetDimensionOrDie("Capacity")
        for _lt_v, _lt_truck in enumerate(trucks):
            _lt_idx = routing.Start(_lt_v)
            _lt_steps: list = []
            while not routing.IsEnd(_lt_idx):
                _lt_node = manager.IndexToNode(_lt_idx)
                _lt_cumul = solution.Value(_lt_cap_dim.CumulVar(_lt_idx))
                if _lt_node < self.num_sites:
                    _lt_sid   = self.site_ids[_lt_node]
                    _lt_site  = self.sites.get(_lt_sid)
                    _lt_name  = _lt_site.name if _lt_site else _lt_sid
                    if _lt_node in _producer_node_indices:
                        _lt_kind = "P"
                    elif _lt_node in _consumer_node_indices:
                        _lt_kind = "C"
                    else:
                        _lt_kind = "-"
                    _lt_real = _lt_cumul - _FLOW_OFFSET
                    if _lt_real < 0:
                        print(f"[WARN] negative real_load={_lt_real} at {_lt_name} — FlowRelax violation in solution")
                    _lt_steps.append(f"{_lt_name}({_lt_kind})[real_load={_lt_real}]")
                _lt_idx = solution.Value(routing.NextVar(_lt_idx))
            if _lt_steps:
                print(f"[LoadTrace] truck={_lt_truck.id}: {' → '.join(_lt_steps)}")

        # ── [CapacitySafety] post-solve route validation ──────────────────────
        # Walk each route and simulate the full-container load to catch any impossible
        # operations that slipped past the dimension (should not happen, but verifies).
        # _csv_load = number of FULL containers on truck at each stop.
        for _csv_route in routes_extracted:
            _csv_truck = next((t for t in trucks if t.id == _csv_route.truck_id), None)
            _csv_cap   = _csv_truck.capacity if _csv_truck else 999
            _csv_vi    = next((vi for vi, t in enumerate(trucks) if t.id == _csv_route.truck_id), None)
            # Start from 0, not _effective_initial_loads: the recommendation service's
            # _assign_swap_operations begins with an empty truck and loads at the first
            # producer stop.  Using initial_load here causes false-positive VIOLATION
            # for trucks that start at a producer (loads at start + loads at stop = overflow).
            _csv_load  = 0
            for _csv_stop in _csv_route.stops:
                _csv_site = self.sites.get(_csv_stop.site_id)
                if not _csv_site:
                    continue
                if _csv_site.is_producer:
                    if _csv_load >= _csv_cap:
                        print(
                            f"[CapacitySafety] POST-SOLVE VIOLATION: truck {_csv_route.truck_id}"
                            f" attempts producer pickup {_csv_site.name} at full capacity={_csv_cap}"
                        )
                    _csv_load = min(_csv_cap, _csv_load + 1)
                elif _csv_site.is_consumer and _csv_stop.site_id in demand_sites_set:
                    # Only decrement for demand consumer sites — fill sites (optional
                    # routing waypoints) are pass-through in the VRP model (callback=0)
                    # and _assign_swap_operations skips them as non-demand.
                    if _csv_load <= 0:
                        print(
                            f"[CapacitySafety] POST-SOLVE VIOLATION: truck {_csv_route.truck_id}"
                            f" attempts consumer delivery {_csv_site.name} with 0 full containers"
                        )
                    _csv_load = max(0, _csv_load - 1)

        vehicles_used = len(routes_extracted)
        sites_served_ids = {
            stop.site_id
            for route in routes_extracted
            for stop in route.stops
            if stop.site_id in demand_sites_set
        }
        sites_unserved = [sid for sid in demand_sites if sid not in sites_served_ids]

        # ── [ObjectiveBreakdown] economic objective validation ─────────────────
        # Compute total monetary cost independently from extracted routes and
        # compare to the solver's reported objective.
        # Formula: transport + handling + fixed_costs + dropped_penalties ≈ solver_obj
        # Discrepancy > 1 EUR indicates the solver is not optimizing monetary costs
        # (e.g., _PURE_DISTANCE_MODE was True, or arc discounts dominate).
        # [ObjectiveBreakdown] uses the same monetary_arc_callback the solver uses,
        # so transport + handling arc costs are computed identically.
        _obd_arc_c   = 0   # arc costs from callback (transport + handling combined)
        _obd_fixed_c = 0
        for _obd_v in range(num_vehicles):
            if not routing.IsVehicleUsed(solution, _obd_v):
                continue
            _obd_fixed_c += _vehicle_fixed_cost
            _obd_idx = routing.Start(_obd_v)
            while not routing.IsEnd(_obd_idx):
                _obd_next = solution.Value(routing.NextVar(_obd_idx))
                _obd_arc_c += monetary_arc_callback(_obd_idx, _obd_next)
                _obd_idx = _obd_next
        _obd_dropped_c = sum(
            _disjunction_penalties.get(sid, 0)
            for sid in demand_sites
            if sid not in sites_served_ids
        )
        _obd_computed = _obd_arc_c + _obd_fixed_c + _obd_dropped_c
        _obd_diff     = abs(_obd_computed - _obj)
        _obd_passed   = _obd_diff <= 100  # 1 EUR = 100 cents tolerance
        print(
            f"[ObjectiveBreakdown]"
            f"  arc_cost(transport+handling)={_obd_arc_c // _cost_scale}EUR"
            f"  fixed={_obd_fixed_c // _cost_scale}EUR"
            f"  dropped_penalties={_obd_dropped_c // _cost_scale}EUR"
            f"  computed={_obd_computed // _cost_scale}EUR"
            f"  solver_obj={_obj // _cost_scale}EUR"
            f"  diff={_obd_diff // _cost_scale}EUR"
            f"  {'PASS' if _obd_passed else 'WARN'}"
        )
        if not _obd_passed:
            logger.warning(
                "[ObjectiveBreakdown] |computed - solver_obj| = %d cents (%.2f EUR) > 1 EUR tolerance"
                " — arc tie-breaker discounts or soft penalties account for %.2f EUR difference",
                _obd_diff, _obd_diff / _cost_scale, (_obj - _obd_computed) / _cost_scale,
            )

        # ── [VRPDecision] comprehensive post-solve diagnostics ───────────────
        # Compute per-vehicle end times from the Time dimension to derive
        # actual drive and service times used in the solution.
        _dec_total_end_time = 0
        _dec_vehicle_days_used = 0
        for _dv in range(num_vehicles):
            _dv_end = solution.Value(_time_dim.CumulVar(routing.End(_dv)))
            if _dv_end > 0:
                _dec_vehicle_days_used += 1
                _dec_total_end_time += _dv_end
        _dec_service_time = len(sites_served_ids) * service_time_minutes
        _dec_drive_time = max(0, _dec_total_end_time - _dec_service_time)
        _dec_available = num_vehicles * max_time_minutes

        print(
            f"[VRPDecision] day={current_day}"
            f" sites_served={len(sites_served_ids)}/{len(demand_sites)}"
            f" sites_unserved={len(sites_unserved)}"
            f" vehicles_used={vehicles_used}/{num_vehicles}"
            f" vehicle_days_used={_dec_vehicle_days_used}"
            f" total_drive_time={_dec_drive_time}min"
            f" total_service_time={_dec_service_time}min"
            f" objective={solution.ObjectiveValue()}c"
        )
        if sites_unserved:
            for site_id in sites_unserved:
                site = self.sites.get(site_id)
                site_name = site.name if site else site_id
                risk = (risk_map or {}).get(site_id, "normal")
                travel_times = []
                for truck in trucks:
                    start_id = self._resolve_truck_start(truck)
                    t = self._scaled_travel_minutes(
                        start_id,
                        site_id,
                        traffic_time_multiplier=traffic_time_multiplier,
                        effective_speed_kmph=_effective_speed_kmph,
                    )
                    travel_times.append(t)
                min_travel = min(travel_times) if travel_times else 999_999
                time_ok = (min_travel + service_time_minutes) <= max_time_minutes
                reason = "TIME_INFEASIBLE" if not time_ok else "PENALTY_PAID"
                print(
                    f"[Unserved] {site_name:<22} risk={risk:<8}"
                    f" min_travel={min_travel}min budget={max_time_minutes}min"
                    f" -> {reason}"
                )

        # ── [DroppedNode] per-dropped-node log with reason codes ──────────────
        _dropped_list = self._get_dropped_sites(routing, manager, solution, demand_sites)
        for _dn_sid in _dropped_list:
            _dn_site  = self.sites.get(_dn_sid)
            _dn_name  = _dn_site.name if _dn_site else _dn_sid
            _dn_risk  = (risk_map or {}).get(_dn_sid, "normal")
            _dn_htc   = (_htc_map or {}).get(_dn_sid, 0.0)
            _dn_pen   = _disjunction_penalties.get(_dn_sid, 0)
            # Reason: time-infeasible or economically optional (penalty paid)
            _dn_times = [
                self._scaled_travel_minutes(
                    self._resolve_truck_start(t),
                    _dn_sid,
                    traffic_time_multiplier=traffic_time_multiplier,
                    effective_speed_kmph=_effective_speed_kmph,
                )
                for t in trucks
            ]
            _dn_min_t  = min(_dn_times) if _dn_times else 999_999
            _dn_reason = (
                "TIME_INFEASIBLE"      if (_dn_min_t + service_time_minutes) > max_time_minutes
                else "OPTIONAL_ZERO"   if _dn_pen == 0
                else "PENALTY_PAID"
            )
            print(
                f"[DroppedNode] {_dn_name:<22}"
                f"  risk={_dn_risk:<8}"
                f"  htc={_dn_htc:.1f}h"
                f"  penalty={_dn_pen // _cost_scale}EUR"
                f"  reason={_dn_reason}"
            )

        # ── [VRPSanity] structural sanity checks ─────────────────────────────
        _sanity_excess_slack = _dec_available > 0 and _dec_drive_time < _dec_available * 0.3
        _sanity_vehicle_overuse = vehicles_used > 0 and vehicles_used > len(sites_served_ids)
        _sanity_unused_cap = bool(sites_unserved) and _dec_available > _dec_total_end_time

        # ── Flaring violation: producer dropped despite exceeding daily flaring limit ──
        _sanity_flaring_violation = False
        _flaring_violators: list = []
        for _fv_check_sid in sites_unserved:
            _fv_check_site = self.sites.get(_fv_check_sid)
            if not (_fv_check_site and _fv_check_site.is_producer):
                continue
            _fv_check_htc = (_htc_map or {}).get(_fv_check_sid, 999.0)
            _fv_flaring_h = max(0.0, self.config.max_driver_hours - _fv_check_htc)
            if _fv_flaring_h > self.MAX_FLARING_PER_DAY_H:
                _sanity_flaring_violation = True
                _flaring_violators.append((_fv_check_sid, _fv_flaring_h))
                print(
                    f"[VRPSanity] FLARING_VIOLATION: producer {_fv_check_site.name}"
                    f" dropped but flaring_h={_fv_flaring_h:.3f}h"
                    f" > limit={self.MAX_FLARING_PER_DAY_H:.3f}h — MUST be served"
                )

        # ── Minimum fleet usage: demand > 2 sites but only 1 truck used ──────
        _sanity_underfleet = (
            num_vehicles > 1
            and vehicles_used <= 1
            and len(demand_sites) > 2
        )
        if _sanity_underfleet:
            print(
                f"[VRPSanity] UNDERFLEET: {vehicles_used} truck(s) used"
                f" for {len(demand_sites)} demand sites"
                f" ({num_vehicles} available)"
            )

        if _sanity_excess_slack:
            print(
                f"[VRPSanity] EXCESS_SLACK: drive_time={_dec_drive_time}min"
                f" << available={_dec_available}min"
                f" ({100 * _dec_drive_time // _dec_available}% utilised)"
            )
        if _sanity_vehicle_overuse:
            print(
                f"[VRPSanity] VEHICLE_OVERUSE: vehicles_used={vehicles_used}"
                f" > sites_served={len(sites_served_ids)}"
            )
        if _sanity_unused_cap:
            _unused = _dec_available - _dec_total_end_time
            print(
                f"[VRPSanity] UNUSED_CAPACITY: {len(sites_unserved)} site(s) unserved"
                f" but {_unused}min ({_unused / 60:.1f}h) vehicle time unused"
            )

        # ── [ContainerFlow] container balance diagnostics ─────────────────────
        # Compute per-vehicle consumer/producer visit counts from the solution.
        # Producer visit → truck delivers empty, picks up full (balance −1).
        # Consumer visit → truck picks up empty (balance +1).
        # System is balanced when total_consumers == total_producers across all routes.
        _cf_total_consumers = 0
        _cf_total_producers = 0
        _cf_imbalance = False
        for _cf_route in routes_extracted:
            _cf_consumers = sum(
                1 for _s in _cf_route.stops
                if self.sites.get(_s.site_id) and self.sites[_s.site_id].is_consumer
            )
            _cf_producers = sum(
                1 for _s in _cf_route.stops
                if self.sites.get(_s.site_id) and self.sites[_s.site_id].is_producer
            )
            _cf_total_consumers += _cf_consumers
            _cf_total_producers += _cf_producers
            _cf_end_balance = _cf_consumers - _cf_producers  # positive = more empties on truck
            print(
                f"[ContainerFlow] truck={_cf_route.truck_id}"
                f" consumer_stops={_cf_consumers}"
                f" producer_stops={_cf_producers}"
                f" vehicle_end_balance={_cf_end_balance:+d}"
                f" {'BALANCED' if _cf_end_balance == 0 else 'IMBALANCED'}"
            )
        _cf_system_imbalance = abs(_cf_total_consumers - _cf_total_producers)
        print(
            f"[ContainerFlow] system"
            f" total_empty_pickups={_cf_total_consumers}"
            f" total_full_pickups={_cf_total_producers}"
            f" imbalance={_cf_system_imbalance}"
        )
        _sanity_container_imbalance = _cf_system_imbalance > 0
        if _sanity_container_imbalance:
            print(
                f"[VRPSanity] CONTAINER_IMBALANCE: {_cf_system_imbalance} container(s)"
                f" unmatched (consumers={_cf_total_consumers} producers={_cf_total_producers})"
            )

        # ── [ValidationBaseline] Step 1: concise solve quality summary ───────────
        # Compares solver objective to billed cost estimate to catch cost model drift.
        # Metrics: solver_obj_eur, real_cost_eur (billing estimate), ratio, imbalance,
        # zero_cost_arcs (arc cost ≤ 1¢ in solution), total_km, sites_served.
        _vb_solver_eur  = _obj / _cost_scale
        _vb_real_eur    = _obd_computed / _cost_scale
        _vb_ratio       = _vb_solver_eur / max(_vb_real_eur, 1.0)
        _vb_end_imbal   = _cf_system_imbalance
        _vb_total_km    = sum(r.total_distance_km for r in routes_extracted)
        _vb_sites_srvd  = len(sites_served_ids)

        # Count zero-cost arcs in the current solution (cost ≤ 1 cent)
        _vb_zero_arcs = 0
        for _vb_v in range(num_vehicles):
            _vb_idx = routing.Start(_vb_v)
            while not routing.IsEnd(_vb_idx):
                _vb_next = solution.Value(routing.NextVar(_vb_idx))
                _vb_fn   = manager.IndexToNode(_vb_idx)
                _vb_tn   = manager.IndexToNode(_vb_next)
                if _vb_fn < _num_sites and _vb_tn < _num_sites:
                    _vb_arc_c = monetary_arc_callback(_vb_idx, _vb_next)
                    if _vb_arc_c <= 1:
                        _vb_zero_arcs += 1
                _vb_idx = _vb_next

        print(
            f"[ValidationBaseline]"
            f"  solver_obj={_vb_solver_eur:.2f}EUR"
            f"  real_cost={_vb_real_eur:.2f}EUR"
            f"  ratio={_vb_ratio:.3f}"
            f"  end_imbalance={_vb_end_imbal}"
            f"  zero_cost_arcs={_vb_zero_arcs}"
            f"  total_km={_vb_total_km:.1f}"
            f"  sites_served={_vb_sites_srvd}"
        )
        if not (0.80 <= _vb_ratio <= 1.20):
            logger.warning(
                "[ValidationBaseline] ratio=%.3f outside [0.80, 1.20]"
                " — solver objective diverges from billing estimate by %.0f%%",
                _vb_ratio, abs(1.0 - _vb_ratio) * 100,
            )
        if _vb_zero_arcs > 0:
            logger.warning(
                "[ValidationBaseline] %d zero-cost arc(s) in solution"
                " — tie-breaker discount too aggressive; consider reducing flow_value cap",
                _vb_zero_arcs,
            )
        if (_is_final_day or not optimize_days_mode) and _vb_end_imbal > 0:
            logger.warning(
                "[ValidationBaseline] end_imbalance=%d on final day"
                " — containers unmatched; check balance penalty calibration",
                _vb_end_imbal,
            )


        # ── Short-route check: tiny day fragment with very low utilisation ──────
        # A vehicle driving only 7 km in a 9-hour shift indicates the solver
        # activated it trivially.  Flag and include in re-opt trigger.
        _sanity_short_route = False
        for _srv in range(num_vehicles):
            _srv_end = solution.Value(_time_dim.CumulVar(routing.End(_srv)))
            if _srv_end == 0 or _srv_end >= int(max_time_minutes * 0.20):
                continue
            _srv_idx = routing.Start(_srv)
            _srv_dist_m = 0
            while not routing.IsEnd(_srv_idx):
                _srv_next = solution.Value(routing.NextVar(_srv_idx))
                _srv_fn = manager.IndexToNode(_srv_idx)
                _srv_tn = manager.IndexToNode(_srv_next)
                _srv_dist_m += dist_ext[_srv_fn][_srv_tn]
                _srv_idx = _srv_next
            _srv_dist_km = _srv_dist_m / 1000.0
            if _srv_dist_km < 40.0:
                print(
                    f"[VRPSanity] SHORT_ROUTE: truck_idx={_srv}"
                    f" dist={_srv_dist_km:.1f}km"
                    f" shift_used={100 * _srv_end // max_time_minutes}%"
                    f" — route flagged as underutilised"
                )
                _sanity_short_route = True

        # ── Critical sites dropped despite available trucks ───────────────────
        # If a critical/warning site was dropped (penalty paid) and any truck has
        # unused time headroom, the initial solution failed to serve high-risk demand.
        # This is the most important reopt trigger: a critical site left unserved
        # while trucks are idle is always wrong, regardless of other sanity flags.
        _sanity_critical_dropped = bool(
            sites_unserved
            and any((risk_map or {}).get(sid) in ("critical", "warning") for sid in sites_unserved)
            and _dec_available > _dec_total_end_time * 1.1   # ≥ 10% truck time unused
        )
        if _sanity_critical_dropped:
            print(
                f"[VRPSanity] CRITICAL_DROPPED: {sum(1 for s in sites_unserved if (risk_map or {}).get(s) in ('critical','warning'))}"
                f" high-risk site(s) unserved with {(_dec_available - _dec_total_end_time)}min"
                f" ({(_dec_available - _dec_total_end_time)/60:.1f}h) idle truck time"
            )

        # ── Auto re-optimization when solution quality is poor ────────────────
        # Fixed fleet: no vehicle fixed cost manipulation in reopt.
        # The reopt uses the same zero fixed cost; only container penalty and fill
        # sites are adjusted to improve balance and coverage.
        if not _reopt_attempt and (
            _sanity_excess_slack or _sanity_vehicle_overuse
            or _sanity_unused_cap or _sanity_short_route
            or _sanity_container_imbalance or _sanity_critical_dropped
            or _sanity_underfleet
        ):
            _reopt_fill = [
                sid for sid in self.sites
                if sid not in demand_sites_set and sid in self.site_index
            ]
            _reopt_fill_sites = fill_sites if fill_sites else _reopt_fill
            _reopt_reason = ", ".join(filter(None, [
                "EXCESS_SLACK" if _sanity_excess_slack else "",
                "VEHICLE_OVERUSE" if _sanity_vehicle_overuse else "",
                "UNUSED_CAPACITY" if _sanity_unused_cap else "",
                "SHORT_ROUTE" if _sanity_short_route else "",
                "CONTAINER_IMBALANCE" if _sanity_container_imbalance else "",
                "CRITICAL_DROPPED" if _sanity_critical_dropped else "",
                "UNDERFLEET" if _sanity_underfleet else "",
            ]))
            _reopt_container_penalty = _container_penalty
            if _sanity_container_imbalance and _sanity_critical_dropped:
                _reopt_container_penalty = _container_penalty * 6
            elif _sanity_container_imbalance:
                _reopt_container_penalty = _container_penalty * 4
            elif _sanity_critical_dropped:
                _reopt_container_penalty = _container_penalty * 2

            _reopt_risk_penalty_multiplier = risk_penalty_multiplier
            if _sanity_critical_dropped:
                _reopt_risk_penalty_multiplier = max(_reopt_risk_penalty_multiplier, 3.0)
            elif _sanity_unused_cap or _sanity_excess_slack or _sanity_underfleet:
                _reopt_risk_penalty_multiplier = max(_reopt_risk_penalty_multiplier, 1.5)
            print(
                f"[ReOpt] attempt=1 reason={_reopt_reason}"
                f" → re-solving fixed_cost=0 (fixed fleet)"
                f" container_penalty={_reopt_container_penalty}c"
                f" risk_penalty_multiplier={_reopt_risk_penalty_multiplier:.1f}"
                f" fill_sites={len(_reopt_fill_sites)}"
            )
            _reopt_seconds = max(5, max_search_seconds // 2)
            return self.solve(
                trucks=trucks,
                demand_sites=demand_sites,
                max_search_seconds=_reopt_seconds,
                traffic_time_multiplier=traffic_time_multiplier,
                risk_map=risk_map,
                risk_score_map=risk_score_map,
                urgency_factor_m=urgency_factor_m,
                hours_to_critical_map=hours_to_critical_map,
                current_day=current_day,
                fill_sites=_reopt_fill_sites,
                transfer_sites=transfer_sites,
                _vehicle_fixed_cost=0,
                _reopt_attempt=True,
                _container_penalty=_reopt_container_penalty,
                risk_penalty_multiplier=_reopt_risk_penalty_multiplier,
                optimize_days_mode=optimize_days_mode,
                force_exact_days=force_exact_days,
            )

        # ── Route efficiency gate ─────────────────────────────────────────────
        # Discard routes where actual transport + handling cost exceeds the
        # real economic penalty of not serving those stops.
        # Uses _node_flow_cents (flow-value basis) consistent with the solver objective.
        # For zero-flow sites (htc > shift), fallback = _compute_site_penalty
        # which returns at most handling_floor — those routes are nearly always
        # dropped as uneconomical, which is correct: no urgency = optional service.
        _routes_before_efficiency_gate = list(routes_extracted)
        _eff_routes = []
        for _er in routes_extracted:
            _er_demand_stops = [s for s in _er.stops if s.site_id in demand_sites_set]
            if not _er_demand_stops:
                _eff_routes.append(_er)
                continue
            _er_transport_c = sum(
                int(
                    max(self._lookup_distance(_er.stops[i].site_id, _er.stops[i + 1].site_id),
                        _min_billed_km if self._lookup_distance(
                            _er.stops[i].site_id, _er.stops[i + 1].site_id) > 0 else 0.0)
                    * _cost_per_km * _contingency * _cost_scale
                )
                for i in range(len(_er.stops) - 1)
            )
            _er_handling_c = len(_er_demand_stops) * int(self.config.handling_fee_eur * _cost_scale)
            _er_cost_c = _er_transport_c + _er_handling_c
            _er_value_c = 0
            for _er_s in _er_demand_stops:
                # Use full disjunction penalty for gate: _node_flow_cents is capped
                # (stability cap) and too small to justify typical route costs.
                # _compute_site_penalty returns the amplified solver penalty which
                # correctly reflects urgency (critical → ~25000EUR >> route cost).
                _er_site = self.sites.get(_er_s.site_id)
                _er_htc  = _htc_map.get(_er_s.site_id, 0.0)
                _er_value_c += self._compute_site_penalty(_er_site, _er_htc)
            if _er_cost_c <= _er_value_c:
                _eff_routes.append(_er)
            else:
                print(
                    f"[EfficiencyGate] Dropped route truck={_er.truck_id}"
                    f" cost={_er_cost_c // _cost_scale}EUR"
                    f" > flow_value={_er_value_c // _cost_scale}EUR"
                    f" ({len(_er_demand_stops)} demand stop(s)) — uneconomical"
                )
                logger.info(
                    "[EfficiencyGate] Dropped route %s: cost=%d EUR > flow_value=%d EUR",
                    _er.truck_id, _er_cost_c // _cost_scale, _er_value_c // _cost_scale,
                )
        _demand_routes_before = [
            _route for _route in _routes_before_efficiency_gate
            if any(_stop.site_id in demand_sites_set for _stop in _route.stops)
        ]
        _demand_routes_after = [
            _route for _route in _eff_routes
            if any(_stop.site_id in demand_sites_set for _stop in _route.stops)
        ]
        if _demand_routes_before and not _demand_routes_after:
            print(
                "[EfficiencyGate] All demand-serving routes were filtered out after a valid solve; "
                "keeping original routes to avoid false NO_FEASIBLE_ROUTES."
            )
            logger.warning(
                "[EfficiencyGate] Filter would remove all demand-serving routes; "
                "restoring pre-gate solution."
            )
            routes_extracted = _routes_before_efficiency_gate
        else:
            routes_extracted = _eff_routes

        _routes_pre_gates = list(routes_extracted)

        # ── Trivial gate: drop routes with total_time < 1h AND 0 moves ─────────
        # Also drop routes with distance < 20 km and no real operations.
        _nontrivial_routes = []
        _zero_op_routes = [
            r for r in routes_extracted
            if r.total_containers_delivered == 0
        ]
        if _zero_op_routes:
            print(f"[ForceExact] routes with 0 operations: {[r.truck_id for r in _zero_op_routes]}")
        for _tr in routes_extracted:
            _tr_time = _tr.total_time_hours
            _tr_moves = _tr.total_containers_delivered
            _tr_dist = _tr.total_distance_km if hasattr(_tr, 'total_distance_km') else 0.0
            _trivial_time = _tr_time < 1.0 and _tr_moves == 0
            _trivial_dist = _tr_dist < 20.0 and _tr_moves == 0
            if _trivial_time or _trivial_dist:
                print(
                    f"[TrivialGate] Dropped route truck={_tr.truck_id}"
                    f" total_time={_tr_time:.2f}h dist={_tr_dist:.1f}km container_moves={_tr_moves}"
                )
            else:
                _nontrivial_routes.append(_tr)
        routes_extracted = _nontrivial_routes

        # ── Purpose gate: keep routes with ≥1 container move ─────────────────
        _purpose_routes = []
        for _pr in routes_extracted:
            if _pr.total_containers_delivered >= 1:
                _purpose_routes.append(_pr)
            else:
                print(
                    f"[PurposeGate] Dropped route truck={_pr.truck_id}"
                    f" moves={_pr.total_containers_delivered}"
                )
        routes_extracted = _purpose_routes

        # ── Safety fallback: if all routes removed, restore pre-gate solution ─
        if _routes_pre_gates and not routes_extracted:
            print("[GateFallback] All routes removed by post-solve gates; restoring original solution.")
            routes_extracted = _routes_pre_gates

        # ── Hot-start: save solution as hint for next call ────────────────────
        # Extract per-truck stop sequences (site IDs only) for use as the
        # initial assignment hint on the next call to solve().
        # Only saved when OR-Tools found the solution (not greedy/survival fallbacks).
        try:
            _hint_save: List[List[str]] = []
            for _hs_v in range(num_vehicles):
                _hs_route_sites: List[str] = []
                _hs_idx = routing.Start(_hs_v)
                while not routing.IsEnd(_hs_idx):
                    _hs_node = manager.IndexToNode(_hs_idx)
                    if _hs_node < self.num_sites:
                        _hs_route_sites.append(self.site_ids[_hs_node])
                    _hs_idx = solution.Value(routing.NextVar(_hs_idx))
                _hint_save.append(_hs_route_sites)
            self._last_hint_routes = _hint_save
        except Exception as _hs_err:
            print(f"[HotStart] Failed to save hint routes: {_hs_err}")

        return routes_extracted

    # ── Infeasibility analyser ────────────────────────────────────────────────

    def _debug_infeasibility(
        self,
        demand_sites: List[str],
        trucks: list,
        htc_map: Dict[str, float],
        dist_ext: List[List[int]],
        starts: List[int],
        num_vehicles: int,
    ) -> None:
        """Print a structured root-cause analysis when OR-Tools finds no solution.

        Each numbered check independently identifies one class of infeasibility.
        Checks are intentionally redundant — multiple root causes may co-exist.
        """
        _missing_dist_m = int(self._MISSING_DIST_KM * 1000)
        _max_time_min   = self.config.max_driver_hours * 60.0

        print("\n========== INFEASIBILITY ANALYSIS ==========\n")

        # ── Check 1: Disjunction coverage ─────────────────────────────────────
        print("[Check 1] Disjunction coverage")
        missing_disj: List[str] = []
        for sid in demand_sites:
            if sid not in self.site_index:
                missing_disj.append(f"{sid}(not in index)")
                continue
            node_idx = self.site_index[sid]
            if node_idx not in self._nodes_with_disjunction:
                missing_disj.append(sid)
        if missing_disj:
            print(f"  FAIL Missing disjunctions: {missing_disj}")
            print(
                "  Root cause: nodes without disjunctions are MANDATORY —"
                " if they cannot be served the solver has no valid assignment."
            )
        else:
            print(f"  OK   All {len(demand_sites)} demand nodes have disjunctions")

        # ── Check 2: Penalty scale ─────────────────────────────────────────────
        print("\n[Check 2] Penalty sanity (disjunction skip cost)")
        _HIGH_PENALTY_EUR = 100_000
        _handling_floor   = self.config.handling_fee_eur
        _any_high_penalty = False
        for sid in demand_sites:
            site_obj = self.sites.get(sid)
            if site_obj is None:
                continue
            _htc     = htc_map.get(sid, 0.0)
            _horizon = self.config.max_driver_hours
            _Dt      = self._compute_Dt(_htc, _horizon)
            _fv_eur  = self._flow_value_eur(site_obj, _Dt)
            penalty_eur = max(_handling_floor, _fv_eur)
            if penalty_eur > _HIGH_PENALTY_EUR:
                _any_high_penalty = True
                print(
                    f"  WARN {sid} ({site_obj.name}): penalty={penalty_eur:.0f} EUR"
                    f" — effectively mandatory (solver must serve or pay >{_HIGH_PENALTY_EUR} EUR)"
                )
        if not _any_high_penalty:
            print(f"  OK   All penalties <= {_HIGH_PENALTY_EUR} EUR")

        # ── Check 3: Capacity feasibility ─────────────────────────────────────
        print("\n[Check 3] Capacity feasibility")
        total_capacity = 0
        for t in trucks:
            cap   = getattr(t, "capacity", 0)
            load  = getattr(t, "initial_load", 0)
            free  = max(0, cap - load)
            total_capacity += free
            print(
                f"  Truck {getattr(t, 'id', '?')}:"
                f" capacity={cap}  initial_load={load}  free_slots={free}"
            )
        print(f"  Total free capacity: {total_capacity} container slot(s)")
        if total_capacity == 0:
            print(
                "  FAIL Total truck capacity = 0"
                " — no containers can be moved regardless of routing."
            )
        elif total_capacity < len(demand_sites):
            print(
                f"  WARN Free slots ({total_capacity}) < demand sites ({len(demand_sites)})"
                f" — solver must drop at least {len(demand_sites) - total_capacity} site(s)."
                f" High-penalty sites may prevent a valid assignment."
            )
        else:
            print("  OK   Sufficient capacity for all demand sites")

        # ── Check 4: Start state ───────────────────────────────────────────────
        print("\n[Check 4] Start state")
        all_empty = all(getattr(t, "initial_load", 0) == 0 for t in trucks)
        if all_empty:
            has_producer_demand = any(
                self.sites.get(sid) and self.sites[sid].is_producer
                for sid in demand_sites
            )
            print(
                "  WARN All trucks start EMPTY"
                " — they must pick up containers from a producer before"
                " serving any consumer."
            )
            if not has_producer_demand:
                print(
                    "  FAIL No producer in demand_sites AND all trucks empty"
                    " — consumers cannot be served; the routing model has no"
                    " feasible pickup→delivery arc."
                )
        else:
            preloaded = [
                getattr(t, "id", "?")
                for t in trucks if getattr(t, "initial_load", 0) > 0
            ]
            print(f"  OK   Pre-loaded trucks: {preloaded}")

        # ── Check 5: Connectivity (distance matrix) ───────────────────────────
        print("\n[Check 5] Connectivity (distance matrix)")
        unreachable: List[str] = []
        for sid in demand_sites:
            if sid not in self.distance_matrix:
                unreachable.append(f"{sid}(not in matrix)")
                continue
            reachable = any(
                self.distance_matrix.get(sid, {}).get(other, _missing_dist_m) < _missing_dist_m
                or self.distance_matrix.get(other, {}).get(sid, _missing_dist_m) < _missing_dist_m
                for other in self.site_ids if other != sid
            )
            if not reachable:
                unreachable.append(sid)
        if unreachable:
            print(f"  FAIL Isolated nodes (all arcs sentinel): {unreachable}")
        else:
            print(f"  OK   All {len(demand_sites)} demand nodes are reachable")

        # ── Check 6: Time feasibility (per-site approximate) ──────────────────
        print("\n[Check 6] Time feasibility (approx one-way travel from nearest truck)")
        _service_min = self.config.swap_time_hours * 60.0
        for sid in demand_sites:
            if sid not in self.distance_matrix:
                continue
            # Minimum one-way distance from any truck start to this site
            min_dist_km = float("inf")
            for vi, start_idx in enumerate(starts[:num_vehicles]):
                start_sid = self.site_ids[start_idx] if start_idx < len(self.site_ids) else None
                if start_sid and start_sid in self.distance_matrix:
                    d = self.distance_matrix[start_sid].get(sid, self._MISSING_DIST_KM)
                    if d < min_dist_km:
                        min_dist_km = d
            if min_dist_km >= self._MISSING_DIST_KM:
                print(f"  FAIL {sid}: no truck can reach it (all arcs sentinel)")
                continue
            travel_min = (min_dist_km / self.config.avg_speed_kmph) * 60.0
            total_min  = travel_min + _service_min
            if total_min > _max_time_min:
                print(
                    f"  FAIL {sid}: min travel={travel_min:.1f}min + service={_service_min:.1f}min"
                    f" = {total_min:.1f}min exceeds shift={_max_time_min:.0f}min"
                )
            else:
                print(
                    f"  OK   {sid}: min travel={travel_min:.1f}min + service={_service_min:.1f}min"
                    f" = {total_min:.1f}min  (shift={_max_time_min:.0f}min)"
                )

        # ── Check 7: Critical demand pressure ─────────────────────────────────
        print("\n[Check 7] Critical demand (hours-to-critical)")
        _crit_h = self.config.critical_hours_threshold
        _warn_h = self.config.warning_hours_threshold
        for sid in demand_sites:
            htc = htc_map.get(sid)
            site_obj = self.sites.get(sid)
            name = site_obj.name if site_obj else sid
            if htc is None:
                print(f"  ??   {name} ({sid}): no htc entry")
            elif htc < _crit_h:
                print(f"  CRIT {name} ({sid}): htc={htc:.1f}h — MUST be served immediately")
            elif htc < _warn_h:
                print(f"  WARN {name} ({sid}): htc={htc:.1f}h — urgent")
            else:
                print(f"  OK   {name} ({sid}): htc={htc:.1f}h")

        # ── Summary ───────────────────────────────────────────────────────────
        print("\n========== SUMMARY ==========")
        print(
            "Common root causes:\n"
            "  1. Missing disjunction      → node is a hard constraint; if unreachable, no solution\n"
            "  2. Penalty too high         → node effectively mandatory; same effect as (1)\n"
            "  3. Zero truck capacity      → no containers can move\n"
            "  4. All trucks empty + no producer demand → consumers unreachable\n"
            "  5. Disconnected graph       → isolated node cannot be served or skipped\n"
            "  6. Time infeasible          → site too far to reach within driver shift\n"
            "  7. Critical site + no time  → mandatory service but physically impossible\n"
        )
        print("=================================\n")

    # ── Helper: detect dropped nodes ─────────────────────────────────────────

    def _get_dropped_sites(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        solution: pywrapcp.Assignment,
        demand_sites: List[str],
    ) -> List[str]:
        """Return site IDs whose disjunction penalty was paid (i.e., dropped)."""
        dropped = []
        for site_id in demand_sites:
            if site_id not in self.site_index:
                continue
            node = self.site_index[site_id]
            index = -1
            for routing_idx in range(routing.Size()):
                if manager.IndexToNode(routing_idx) != node:
                    continue
                if routing.IsStart(routing_idx) or routing.IsEnd(routing_idx):
                    continue
                index = routing_idx
                break
            if index < 0:
                logger.warning(
                    "[DroppedSiteSkip] %s has invalid routing index %s"
                    " (commonly happens when the site is used as a forced-end node)",
                    site_id,
                    index,
                )
                continue
            if routing.IsStart(index) or routing.IsEnd(index):
                continue
            if solution.Value(routing.NextVar(index)) == index:
                dropped.append(site_id)
        return dropped

    # ── Route extraction ──────────────────────────────────────────────────────

    def _extract_routes(
        self,
        manager: pywrapcp.RoutingIndexManager,
        routing: pywrapcp.RoutingModel,
        solution: pywrapcp.Assignment,
        trucks: List[Truck],
        demand_sites_set: set,
        traffic_time_multiplier: float,
        dummy_end: int,
        current_day: int,
    ) -> List[Route]:
        """Extract Route objects from the OR-Tools solution.

        Open-route aware: the virtual dummy-end node is skipped.  The last entry
        in route.stops is therefore the truck's actual end location, which
        _update_truck_states_for_next_day() uses as the Day N+1 start.
        """
        routes = []
        time_dim = routing.GetDimensionOrDie("Time")

        for vehicle_id, truck in enumerate(trucks):
            route_distance = 0.0
            route_stops: List[RouteStop] = []
            sequence = 0
            prev_site_id: Optional[str] = None
            # Simulate truck full-container count for capacity-aware service time.
            # Use truck.initial_load directly — no artificial preload.
            sim_full = truck.initial_load

            index = routing.Start(vehicle_id)

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node < 0 or node >= self.num_sites:
                    logger.warning(
                        "Truck %s: skipping synthetic node %s during route extraction",
                        truck.id,
                        node,
                    )
                    index = solution.Value(routing.NextVar(index))
                    continue
                site_id = self.site_ids[node]
                site = self.sites[site_id]

                time_var = time_dim.CumulVar(index)
                arrival_time_hours = solution.Min(time_var) / 60.0

                dist_from_prev = (
                    self._get_distance_km(prev_site_id, site_id) if prev_site_id else 0.0
                )
                route_distance += dist_from_prev

                # Capacity-aware operation check:
                #   pickup   = 1 if producer  AND truck has room   (sim_full < capacity)
                #   delivery = 1 if dem.consumer AND truck has fulls (sim_full > 0)
                # No forced operations — if the truck can't physically act, service_time = 0.
                if sequence > 0 and site.is_producer:
                    _pickup_count   = 1 if sim_full < truck.capacity else 0
                    _delivery_count = 0
                elif sequence > 0 and site.is_consumer and site_id in demand_sites_set:
                    _delivery_count = 1 if sim_full > 0 else 0
                    _pickup_count   = 0
                else:
                    _pickup_count   = 0
                    _delivery_count = 0

                _has_operation = (_pickup_count > 0 or _delivery_count > 0)

                # Build SwapOperation now that actual counts are known.
                # containers_dropped / containers_picked use placeholder IDs; the
                # lists must be non-empty for total_containers_delivered to be > 0.
                swap_op = None
                if _delivery_count > 0 or _pickup_count > 0:
                    swap_op = SwapOperation(
                        site_id=site_id,
                        containers_dropped=[
                            f"ctr_{site_id}_{sequence}_{i}" for i in range(_delivery_count)
                        ],
                        containers_picked=[
                            f"ctr_{site_id}_{sequence}_{i}" for i in range(_pickup_count)
                        ],
                    )
                elif site_id in demand_sites_set and site.is_consumer:
                    # Truck visited consumer but couldn't deliver (empty truck).
                    # Still attach an empty SwapOperation so the stop is marked.
                    swap_op = SwapOperation(
                        site_id=site_id,
                        containers_dropped=[],
                        containers_picked=[],
                    )

                # Advance simulated load so subsequent stops see the correct state.
                sim_full = min(sim_full + _pickup_count, truck.capacity)
                sim_full = max(sim_full - _delivery_count, 0)

                route_stops.append(RouteStop(
                    sequence=sequence,
                    site_id=site_id,
                    site_name=site.name,
                    arrival_time_hours=arrival_time_hours,
                    distance_from_previous_km=dist_from_prev,
                    cumulative_distance_km=route_distance,
                    service_time_hours=(
                        self.config.swap_time_hours if _has_operation else 0
                    ),
                    swap_operation=swap_op,
                    truck_load_after=0,
                ))

                prev_site_id = site_id
                sequence += 1
                index = solution.Value(routing.NextVar(index))

            # ── Open-route end handling ───────────────────────────────────────
            # `index` is now the end node. If it's a real site (forced end), add it
            # as the last stop. If it's dummy_end (open route), skip it.
            forced_end_site_id = None
            if (
                truck.force_end is not None
                and truck.force_end.day_index == current_day
                and truck.force_end.site_id in self.sites
            ):
                forced_end_site_id = truck.force_end.site_id

            end_site_id = forced_end_site_id
            if end_site_id is None:
                end_node = manager.IndexToNode(routing.End(vehicle_id))
                if 0 <= end_node < self.num_sites:
                    end_site_id = self.site_ids[end_node]

            if end_site_id is not None and prev_site_id != end_site_id:
                # Forced end or explicit real end: add the site as the final stop.
                end_site = self.sites[end_site_id]
                time_var = time_dim.CumulVar(routing.End(vehicle_id))
                arrival_time_hours = solution.Min(time_var) / 60.0
                dist_from_prev = (
                    self._get_distance_km(prev_site_id, end_site_id) if prev_site_id else 0.0
                )
                route_distance += dist_from_prev
                route_stops.append(RouteStop(
                    sequence=sequence,
                    site_id=end_site_id,
                    site_name=end_site.name,
                    arrival_time_hours=arrival_time_hours,
                    distance_from_previous_km=dist_from_prev,
                    cumulative_distance_km=route_distance,
                    service_time_hours=0,
                    swap_operation=None,
                    truck_load_after=0,
                ))

            if route_stops:
                actual_end_site = route_stops[-1].site_id
                logger.debug(
                    "Truck %s: route ends at '%s' "
                    "(next day will start here for continuity)",
                    truck.id, actual_end_site,
                )

            # ── Filter out positioning-only routes and zero-demand routes ─────
            # A useful route has ≥ 1 stop AFTER the start depot AND at least one
            # of those stops is a demand site (actual service, not pure repositioning).
            # NOTE: sequence==0 is the start depot — explicitly excluded so a truck
            # that merely starts at a demand site but visits no others is not counted.
            has_demand_stop = any(
                s.site_id in demand_sites_set and s.sequence > 0
                for s in route_stops
            )
            has_force_end_positioning = (
                len(route_stops) > 1
                and truck.force_end is not None
                and truck.force_end.day_index == current_day
                and route_stops[-1].site_id == truck.force_end.site_id
                and route_stops[0].site_id != route_stops[-1].site_id
            )
            if len(route_stops) > 1 and has_demand_stop:
                routes.append(Route(truck_id=truck.id, stops=route_stops))
            elif has_force_end_positioning:
                routes.append(Route(truck_id=truck.id, stops=route_stops))
                logger.debug(
                    "Truck %s: keeping positioning route to forced end '%s'",
                    truck.id,
                    truck.force_end.site_id,
                )
            elif len(route_stops) > 1:
                # Positioning-only (start moved but no demand visited) — skip.
                logger.debug(
                    "Truck %s: route has %d stops but no demand — filtered as positioning-only",
                    truck.id, len(route_stops),
                )

        return [r for r in routes if r.num_stops > 0]

    # ── Utility ───────────────────────────────────────────────────────────────

    def _get_distance_km(self, from_id: str, to_id: str) -> float:
        if from_id == to_id:
            return 0.0
        return self._lookup_distance(from_id, to_id)

    def calculate_route_cost(self, route: Route, cost_per_km: float = None) -> float:
        if cost_per_km is None:
            cost_per_km = self.config.cost_per_km_eur
        effective_km = max(route.total_distance_km, self.config.min_billed_km)
        transport_cost = effective_km * cost_per_km
        handling_cost  = route.num_stops * self.config.handling_fee_eur
        return (transport_cost + handling_cost) * self.config.contingency_multiplier


# ── Module-level convenience wrapper ─────────────────────────────────────────

def solve_vrp(
    sites: Dict[str, Site],
    trucks: List[Truck],
    distance_matrix: Dict[str, Dict[str, float]],
    config: OperationalConfig,
    demand_sites: List[str] = None,
    max_search_seconds: int = 30,
    time_matrix_minutes: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[Route]:
    """Convenience wrapper around VRPSolver.solve()."""
    solver = VRPSolver(sites, distance_matrix, config, time_matrix_minutes)
    return solver.solve(trucks, demand_sites, max_search_seconds)
