"""Route, swap operation, and recommendation models."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, computed_field
import uuid


class SwapOperation(BaseModel):
    """
    Represents a container swap operation at a site.

    At production sites: typically pick up full containers
    At consumer sites: drop full containers, pick up empty ones
    """
    site_id: str = Field(..., description="Site where swap occurs")
    containers_dropped: List[str] = Field(
        default_factory=list, description="Container IDs being dropped at site"
    )
    containers_picked: List[str] = Field(
        default_factory=list, description="Container IDs being picked up from site"
    )

    @computed_field
    @property
    def truck_load_delta(self) -> int:
        """Change in truck load: positive = picking up more than dropping."""
        return len(self.containers_picked) - len(self.containers_dropped)

    @computed_field
    @property
    def site_inventory_delta(self) -> int:
        """Change in site inventory: positive = receiving more than giving."""
        return len(self.containers_dropped) - len(self.containers_picked)

    @computed_field
    @property
    def is_pickup_only(self) -> bool:
        """Check if this is a pickup-only operation."""
        return len(self.containers_dropped) == 0 and len(self.containers_picked) > 0

    @computed_field
    @property
    def is_dropoff_only(self) -> bool:
        """Check if this is a dropoff-only operation."""
        return len(self.containers_dropped) > 0 and len(self.containers_picked) == 0

    @computed_field
    @property
    def is_swap(self) -> bool:
        """Check if this is a true swap (both drop and pick)."""
        return len(self.containers_dropped) > 0 and len(self.containers_picked) > 0


class RouteLeg(BaseModel):
    """
    Represents a single leg (segment) between two stops in a route.

    Each leg has a from/to stop and distance information.
    """
    from_stop_index: int = Field(..., ge=0, description="Index of origin stop")
    to_stop_index: int = Field(..., ge=0, description="Index of destination stop")
    from_site_id: str = Field(..., description="Origin site ID")
    from_site_name: str = Field(default="", description="Origin site name")
    to_site_id: str = Field(..., description="Destination site ID")
    to_site_name: str = Field(default="", description="Destination site name")
    distance_km: float = Field(..., ge=0, description="Distance of this leg in km")
    drive_time_hours: Optional[float] = Field(
        default=None, ge=0, description="Drive time for this leg in hours"
    )
    geometry: Optional[List[List[float]]] = Field(
        default=None,
        description="Road-following polyline as [[lat, lon], ...] decoded from GraphHopper"
    )


class RouteStop(BaseModel):
    """
    Represents a single stop in a route.

    Each stop includes the site visited, the swap operation performed,
    and timing/distance information.
    """
    sequence: int = Field(..., ge=0, description="Stop sequence number (0 = start)")
    site_id: str = Field(..., description="Site ID for this stop")
    site_name: str = Field(default="", description="Human-readable site name")
    arrival_time_hours: float = Field(
        default=0.0, ge=0, description="Cumulative time from route start in hours"
    )
    distance_from_previous_km: float = Field(
        default=0.0, ge=0, description="Distance from previous stop in km"
    )
    cumulative_distance_km: float = Field(
        default=0.0, ge=0, description="Total distance from route start in km"
    )
    service_time_hours: float = Field(
        default=0.333, ge=0, description="Time spent at stop in hours (default 20 min)"
    )
    swap_operation: Optional[SwapOperation] = Field(
        default=None, description="Swap operation performed at this stop"
    )
    truck_load_after: int = Field(
        default=0, ge=0, description="Number of containers on truck after this stop"
    )
    load_full_after: int = Field(
        default=0, ge=0, description="Full containers on truck after this stop"
    )
    load_empty_after: int = Field(
        default=0, ge=0, description="Empty/returnable containers on truck after this stop"
    )

    @computed_field
    @property
    def departure_time_hours(self) -> float:
        """Time when truck departs this stop."""
        return self.arrival_time_hours + self.service_time_hours


class Route(BaseModel):
    """
    Represents a complete route for a single truck for a single day.

    A route is a sequence of stops. In multi-day planning:
    - day_index indicates which day (1-based)
    - start_location may differ from end_location
    - Trucks continue from previous day's end location
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4())[:8],
        description="Unique route identifier"
    )
    truck_id: str = Field(..., description="Truck assigned to this route")
    day_index: int = Field(
        default=1, ge=1, description="Day index in multi-day plan (1-based)"
    )
    stops: List[RouteStop] = Field(
        default_factory=list, description="Ordered list of stops"
    )
    legs: List[RouteLeg] = Field(
        default_factory=list,
        description="Legs (segments) between stops with distances"
    )
    start_site_id: Optional[str] = Field(
        default=None, description="Site ID where route starts (if site-based)"
    )
    start_label: Optional[str] = Field(
        default=None,
        description="Human-readable start location label (e.g., site name, 'Custom Point: X', 'In Transit')"
    )
    end_site_id: Optional[str] = Field(
        default=None, description="Site ID where route ends (may differ from start)"
    )
    geometry: Optional[List[List[float]]] = Field(
        default=None,
        description="Road-following polyline as [[lat, lon], ...] from GraphHopper"
    )

    @computed_field
    @property
    def total_distance_km(self) -> float:
        """Total route distance in km (from legs if available, else from stops)."""
        if self.legs:
            return sum(leg.distance_km for leg in self.legs)
        if not self.stops:
            return 0.0
        return self.stops[-1].cumulative_distance_km

    @computed_field
    @property
    def legs_sum_km(self) -> float:
        """Sum of all leg distances for audit/validation."""
        return sum(leg.distance_km for leg in self.legs)

    @computed_field
    @property
    def total_time_hours(self) -> float:
        """Total route time including service time at all stops."""
        if not self.stops:
            return 0.0
        last_stop = self.stops[-1]
        return last_stop.arrival_time_hours + last_stop.service_time_hours

    @computed_field
    @property
    def num_stops(self) -> int:
        """Number of stops excluding start depot."""
        return max(0, len(self.stops) - 1)

    @computed_field
    @property
    def total_containers_delivered(self) -> int:
        """Total containers dropped at consumer sites."""
        total = 0
        for stop in self.stops:
            if stop.swap_operation:
                total += len(stop.swap_operation.containers_dropped)
        return total

    @computed_field
    @property
    def total_containers_picked(self) -> int:
        """Total containers picked up from sites."""
        total = 0
        for stop in self.stops:
            if stop.swap_operation:
                total += len(stop.swap_operation.containers_picked)
        return total

    def build_legs_from_stops(self, avg_speed_kmph: float = 80.0) -> None:
        """Build legs array from stops data for consistency."""
        self.legs = []
        for i in range(1, len(self.stops)):
            prev_stop = self.stops[i - 1]
            curr_stop = self.stops[i]
            dist = curr_stop.distance_from_previous_km
            drive_time = dist / avg_speed_kmph if avg_speed_kmph > 0 else None

            leg = RouteLeg(
                from_stop_index=i - 1,
                to_stop_index=i,
                from_site_id=prev_stop.site_id,
                from_site_name=prev_stop.site_name,
                to_site_id=curr_stop.site_id,
                to_site_name=curr_stop.site_name,
                distance_km=dist,
                drive_time_hours=drive_time,
            )
            self.legs.append(leg)


class ContainerMove(BaseModel):
    """
    Describes a single container/bay movement from one site to another.
    Derived from route swap operations; enriched with serial numbers when available.
    """
    from_site_id: str = Field(..., description="Origin site ID")
    from_site_name: str = Field(default="", description="Origin site name")
    to_site_id: str = Field(..., description="Destination site ID")
    to_site_name: str = Field(default="", description="Destination site name")
    bay_id: str = Field(..., description="Bay/container identifier being moved")
    bay_serial_number: Optional[str] = Field(
        default=None, description="4-digit serial number if assigned"
    )
    reason: Optional[str] = Field(
        default=None, description="Reason for move (e.g., 'empty return', 'full delivery')"
    )
    truck_id: str = Field(..., description="Truck performing this move")
    day_index: int = Field(default=1, ge=1, description="Day this move occurs")


class RecommendationStatus(str, Enum):
    """Status of a route recommendation."""
    COMPUTING = "computing"
    READY = "ready"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    INFEASIBLE = "infeasible"
    NO_ACTION_NEEDED = "no_action_needed"  # All sites within safe thresholds; no service required.


class Recommendation(BaseModel):
    """
    A recommendation for routes to execute.

    Contains one or more routes, cost estimates, and explanation.
    Supports multi-day planning with horizon_days.
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4())[:8],
        description="Unique recommendation identifier"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When recommendation was created"
    )
    status: RecommendationStatus = Field(
        default=RecommendationStatus.READY,
        description="Current status of recommendation"
    )
    objective_function: str = Field(
        default="cost", description="Objective used: cost, risk, or balanced"
    )
    horizon_days: int = Field(
        default=1, ge=1, le=4, description="Planning horizon in days"
    )
    routes: List[Route] = Field(
        default_factory=list, description="Recommended routes (may span multiple days)"
    )

    # Cost breakdown
    total_distance_km: float = Field(
        default=0.0, ge=0, description="Total distance across all routes"
    )
    transport_cost_eur: float = Field(
        default=0.0, ge=0, description="Total transport cost (distance * rate)"
    )
    handling_cost_eur: float = Field(
        default=0.0, ge=0, description="Total handling fees (stops * fee)"
    )
    total_cost_eur: float = Field(
        default=0.0, ge=0, description="Total cost including contingency"
    )

    # Energy metrics
    total_mwh_moved: float = Field(
        default=0.0, ge=0, description="Total MWh delivered to consumers"
    )
    eur_per_mwh: Optional[float] = Field(
        default=None, description="Cost efficiency: EUR per MWh delivered (null if no MWh moved)"
    )
    energy_moved_debug: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Breakdown/trace of energy_moved calculation for auditability"
    )

    # Risk metrics
    sites_served: int = Field(
        default=0, ge=0, description="Number of sites receiving service"
    )
    critical_sites_addressed: int = Field(
        default=0, ge=0, description="Number of critical sites addressed"
    )
    risk_reduction_score: float = Field(
        default=0.0, description="Estimated risk reduction (0-100)"
    )

    # Explanation for human operator
    explanation: str = Field(
        default="", description="Human-readable explanation of recommendation"
    )
    warnings: List[str] = Field(
        default_factory=list, description="Any warnings or concerns"
    )
    feasibility_level: str = Field(
        default="STRICT",
        description="Feasibility level: STRICT (all rules), L1 (end-load relaxed), L2 (pickup relaxed), L3 (deadhead)"
    )
    reason_code: Optional[str] = Field(
        default=None, description="Error code when status is infeasible"
    )
    reason_message: Optional[str] = Field(
        default=None, description="Error message when status is infeasible"
    )
    unreturned_containers: int = Field(
        default=0, ge=0,
        description="Containers picked from plants but not returned within this plan"
    )
    container_moves: List[ContainerMove] = Field(
        default_factory=list,
        description="All container movements derived from routes, enriched with serial numbers"
    )
    solution_feedback: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Informational messages about the solution, e.g. unused truck days. "
            "Each item: {type, code, truck_id?, message}"
        ),
    )

    # Solution risk score (0-10): composite exposure to stock-out and flaring risk.
    # Target acceptable range: 3–4. Computed post-solve from site assessments.
    solution_risk_score: Optional[float] = Field(
        default=None, ge=0, le=10,
        description="Solution risk score 0–10 based on stock-out and flaring exposure. "
                    "Target acceptable range: 3–4."
    )

    # Flaring and balance diagnostics for dashboard
    flaring_exposure_hours: Optional[float] = Field(
        default=None, ge=0,
        description="Total expected flaring hours across unserved producers in this plan. "
                    "Exceeding the configured soft limit triggers exponential penalty in the solver."
    )
    end_of_horizon_imbalance: int = Field(
        default=0, ge=0,
        description="Containers still on trucks at the end of the final planning day. "
                    "Target: 0 (fully balanced)."
    )

    # Stored so next_steps approval can replay the same fleet config
    fleet_config: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Fleet config (list of FleetConfigTruck dicts) used when generating this recommendation"
    )

    # Routing diagnostics — populated by generate_recommendation
    fill_sites_count: int = Field(
        default=0, ge=0,
        description="Number of optional fill sites added to the VRP (fill_remaining_time=True)"
    )
    transfer_hubs_count: int = Field(
        default=0, ge=0,
        description="Number of optional transfer hub nodes added to the VRP (allow_transfers=True)"
    )
    fill_sites_visited: int = Field(
        default=0, ge=0,
        description="Number of fill sites the solver actually visited in the final solution"
    )
    transfer_hubs_visited: int = Field(
        default=0, ge=0,
        description="Number of transfer hub sites the solver actually visited in the final solution"
    )

    # Rich diagnostics for infeasible plans — populated by _diagnose_infeasibility
    infeasibility_diagnostics: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Structured diagnostics when status=infeasible. Contains trucks, demand_sites, "
            "producer_hubs, constraints, and flow_analysis sub-objects."
        ),
    )

    @computed_field
    @property
    def total_time_hours(self) -> float:
        """Total time across all routes."""
        return sum(r.total_time_hours for r in self.routes)

    @computed_field
    @property
    def num_trucks_used(self) -> int:
        """Number of trucks in recommendation."""
        return len(self.routes)

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "rec_abc123",
                "status": "ready",
                "objective_function": "cost",
                "total_distance_km": 450.0,
                "transport_cost_eur": 1012.50,
                "handling_cost_eur": 160.0,
                "total_cost_eur": 1289.75,
                "sites_served": 4,
                "explanation": "Optimal route serving 4 critical sites...",
            }
        }
    }
