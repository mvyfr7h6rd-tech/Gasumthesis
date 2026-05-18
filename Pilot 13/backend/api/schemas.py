"""API request and response schemas."""

import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, field_validator

from ..models import Site, Container, Truck, OperationalConfig, Recommendation


class OptimalDaysTrialResult(BaseModel):
    """Result for a single days-count trial in optimal-days search."""
    days: int
    score: float
    feasible: bool
    total_cost_eur: float
    containers_delivered: int


class OptimalDaysResult(BaseModel):
    """Outcome of the optimal-days enumeration (returned when optimal_days=true)."""
    days_used: int
    tested_days: List[OptimalDaysTrialResult]
    explanation: Dict[str, Any]


class UploadResponse(BaseModel):
    """Response for data upload endpoints."""
    success: bool
    message: str
    items_updated: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SystemState(BaseModel):
    """Complete system state response."""
    sites_count: int
    containers_count: int
    trucks_count: int
    mode: str = "real"
    data_loaded_at: Optional[datetime] = None
    last_recommendation_at: Optional[datetime] = None


class BaySchema(BaseModel):
    """Bay data for API responses."""
    bay_id: str
    pressure_bar: int
    kg: float          # raw total kg at this pressure (diagnostic only)
    mwh: float         # raw total MWh (diagnostic only)
    kg_usable: float   # usable kg above config.usable_floor_bar (primary)
    mwh_usable: float  # usable MWh above config.usable_floor_bar (primary)
    serial_number: Optional[str] = None


class ProductionRatesSchema(BaseModel):
    """Production rates for API responses."""
    mode: str
    kg_per_h: Optional[float] = None
    mwh_per_h: Optional[float] = None
    effective_kg_per_h: Optional[float] = None
    effective_mwh_per_h: Optional[float] = None


class SiteWithRisk(BaseModel):
    """Site data with risk assessment."""
    id: str
    name: str
    site_type: str
    latitude: float
    longitude: float
    bays_fixed: int
    bays: List[BaySchema]
    # Raw totals (diagnostic only — includes gas below usable floor)
    total_kg: float
    total_mwh: float
    # Usable totals (primary — gas strictly above usable_floor_bar)
    total_kg_usable: float
    total_mwh_usable: float
    # Floor value echoed so frontend never needs to hardcode it
    usable_floor_bar: int
    is_full: bool
    min_pressure: int
    max_pressure: int
    avg_pressure: float
    # Pressure-based utilization (kept for info; NOT the primary metric)
    utilization_percentage: float
    # Usable-based utilization (primary metric)
    utilization_usable_pct: float
    consumption_rate_kg_hour: float
    production: Optional[ProductionRatesSchema] = None
    flaring_cost_eur_mwh: float
    flaring_loss_eur_per_h: Optional[float] = None
    risk_level: str
    hours_to_critical: float
    risk_score: float
    risk_explanation: str


class SiteListResponse(BaseModel):
    """Response for site list endpoint."""
    sites: List[SiteWithRisk]
    total: int
    critical_count: int
    warning_count: int


class SnapshotBay(BaseModel):
    """Compact bay state for snapshot."""
    bay_id: str
    pressure_bar: int


class SnapshotSite(BaseModel):
    """Compact site state for snapshot."""
    id: str
    total_kg: float
    utilization_percentage: float
    bays: List[SnapshotBay]


class SnapshotContainer(BaseModel):
    """Compact container state for snapshot."""
    id: str
    location_site_id: str
    pressure_bar: float
    status: str


class StateSnapshot(BaseModel):
    """Full mutable-state fingerprint for diffing."""
    sites: List[SnapshotSite]
    containers: List[SnapshotContainer]


class SiteDetailResponse(BaseModel):
    """Detailed site information."""
    site: Site
    containers: List[Container]
    risk_level: str
    hours_to_critical: float
    risk_score: float
    risk_explanation: str
    recent_deliveries: List[Dict[str, Any]] = []


class ContainerListResponse(BaseModel):
    """Response for container list endpoint."""
    containers: List[Container]
    total: int
    full_count: int
    empty_count: int
    in_transit_count: int


class TruckListResponse(BaseModel):
    """Response for truck list endpoint."""
    trucks: List[Truck]
    total: int
    available_count: int


class BayUpdateSchema(BaseModel):
    """Request schema for updating a bay's pressure and optional serial number."""
    bay_id: str
    pressure_bar: int = Field(..., ge=0, le=250)
    serial_number: Optional[str] = Field(
        default=None,
        description="3- or 4-digit physical container serial number"
    )

    @field_validator('serial_number')
    @classmethod
    def validate_serial_number_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r'^\d{3,4}$', v):
            raise ValueError(
                'Invalid bay configuration: serial numbers must be unique and 3–4 digits'
            )
        return v


class ProductionRatesUpdateSchema(BaseModel):
    """Request schema for updating production rates."""
    mode: str = Field(..., description="KG_PER_H, MWH_PER_H, or UNKNOWN")
    kg_per_h: Optional[float] = Field(None, ge=0)
    mwh_per_h: Optional[float] = Field(None, ge=0)


class SitePatchRequest(BaseModel):
    """Request for partial site update."""
    bays: Optional[List[BayUpdateSchema]] = Field(None, description="Bay pressure updates")
    production: Optional[ProductionRatesUpdateSchema] = Field(
        None, description="Production rate updates (producers only)"
    )


class ValidationErrorDetail(BaseModel):
    """Detail for a validation error."""
    code: str = Field(..., description="Error code")
    message: str = Field(..., description="Human-readable message")
    details: Optional[Dict[str, Any]] = Field(default=None, description="Additional details")


class ValidationErrorResponse(BaseModel):
    """Response for validation errors (422)."""
    code: str = Field(..., description="Top-level error code")
    message: str = Field(..., description="Human-readable message")
    details: List[ValidationErrorDetail] = Field(default_factory=list)


# Error codes
class ErrorCode:
    INVALID_TRUCK_CONFIG = "INVALID_TRUCK_CONFIG"
    MISSING_DISTANCE = "MISSING_DISTANCE"
    INVALID_INTRANSIT_POSITION = "INVALID_INTRANSIT_POSITION"
    INFEASIBLE_FORCE_END = "INFEASIBLE_FORCE_END"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_POINT = "INVALID_POINT"
    MISSING_START_CONFIG = "MISSING_START_CONFIG"
    MISSING_FORCE_END_CONFIG = "MISSING_FORCE_END_CONFIG"
    INVALID_BAY_CONFIG = "INVALID_BAY_CONFIG"


class RecommendationRequest(BaseModel):
    """Request for generating recommendations."""
    objective: str = Field(
        default="balanced",
        description="Optimization objective: time, flaring, or balanced"
    )
    site_ids: Optional[List[str]] = Field(
        default=None,
        description="Specific sites to serve (default: auto from risk)"
    )
    max_search_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Maximum time for optimization"
    )
    traffic_mode: str = Field(
        default="normal",
        description="Traffic mode: normal or heavy (affects travel time)"
    )
    avg_speed_kmh: Optional[float] = Field(
        default=None,
        ge=20,
        le=120,
        description="Custom average speed in km/h. Overrides traffic_mode preset when provided."
    )

    # Fleet configuration (primary way to specify trucks)
    fleet: Optional[FleetConfig] = Field(
        default=None,
        description="Fleet configuration with per-truck availability and constraints."
    )

    # Manual distances for custom points or missing matrix entries
    manual_distances: Optional[List[ManualDistanceEntry]] = Field(
        default=None,
        description="Manual distance entries for custom points or missing matrix entries."
    )

    # Custom points with their distance definitions
    custom_points: Optional[List[CustomPoint]] = Field(
        default=None,
        description="Custom point definitions with distances to sites (max 7)."
    )

    # Rate overrides for risk calculations
    rate_overrides: Optional[RateOverrides] = Field(
        default=None,
        description="Rate overrides for flaring costs and consumption rates."
    )

    # Frontend override for transport cost rate
    cost_per_km_override: Optional[float] = Field(
        default=None, ge=0,
        description="Override transport cost per km in EUR. If not provided, uses system config value (2.25 EUR/km)."
    )

    # Constraint overrides (Advanced mode)
    max_driver_hours_override: Optional[float] = Field(
        default=None, ge=1, le=24,
        description="Override max driver shift hours. Default: 9h."
    )
    swap_time_min_override: Optional[float] = Field(
        default=None, ge=1, le=120,
        description="Override service time per stop in minutes. Default: 20 min."
    )
    max_containers_override: Optional[int] = Field(
        default=None, ge=1, le=5,
        description="Override truck container capacity. Default: 3."
    )

    # DEPRECATED fields for backwards compatibility
    truck_ids: Optional[List[str]] = Field(
        default=None,
        description="DEPRECATED: Use fleet.trucks instead."
    )
    horizon_days: int = Field(
        default=1,
        ge=1,
        le=4,
        description="DEPRECATED: Use per-truck availability_days in fleet config."
    )

    # Routing enhancements
    fill_remaining_time: bool = Field(
        default=True,
        description=(
            "When true, expand the candidate site pool with lower-priority sites so "
            "trucks with spare shift time can serve more stops instead of returning idle."
        )
    )
    allow_transfers: bool = Field(
        default=False,
        description=(
            "When true, production sites with spare capacity are added as optional "
            "transfer nodes (15-min stop) where trucks can exchange containers."
        )
    )

    # Force exact days: trucks must be used every day
    force_exact_days: bool = Field(
        default=False,
        description=(
            "If true, all trucks are forced active every day of the horizon. "
            "Used when optimalDaysMode='force' to prevent idle vehicles."
        )
    )

    # Optimal days enumeration
    optimal_days: bool = Field(
        default=False,
        description=(
            "If true, enumerate horizon_days 1–4 and return the plan with the best score. "
            "Ignores per-truck availability_days — all trucks are tested at the same day count. "
            "Adds optimal_days_result to the response with per-trial scores and an explanation."
        )
    )

    ai_api_key: Optional[str] = Field(
        default=None,
        description="Optional DeepSeek API key used by the planner-style AI coordinator during recommendation generation."
    )

    # Debug
    debug_trace: bool = Field(
        default=False,
        description="If true, include full calculation trace in response"
    )

    @field_validator('objective')
    @classmethod
    def validate_objective(cls, v: str) -> str:
        """Validate objective is one of the allowed values."""
        allowed = {"time", "flaring", "balanced"}
        if v not in allowed:
            raise ValueError(f"objective must be one of {allowed}, got '{v}'")
        return v

    @field_validator('traffic_mode')
    @classmethod
    def validate_traffic_mode(cls, v: str) -> str:
        """Validate traffic_mode is one of the allowed values."""
        allowed = {"normal", "heavy"}
        if v not in allowed:
            raise ValueError(f"traffic_mode must be one of {allowed}, got '{v}'")
        return v


class RecommendationResponse(BaseModel):
    """Response containing recommendation."""
    recommendation: Recommendation
    computation_time_seconds: float
    trace: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Calculation trace (only when debug_trace=true)"
    )
    optimal_days_result: Optional[OptimalDaysResult] = Field(
        default=None,
        description="Present when optimal_days=true; contains per-trial scores and explanation."
    )


class MultiRecommendationRequest(BaseModel):
    """Request for generating recommendations across all objectives.

    Same inputs as RecommendationRequest but objective is ignored
    (all 4 objectives are computed).
    """
    site_ids: Optional[List[str]] = Field(
        default=None,
        description="Specific sites to serve (default: auto from risk)"
    )
    max_search_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Maximum time for optimization per objective"
    )
    traffic_mode: str = Field(
        default="normal",
        description="Traffic mode: normal or heavy (affects travel time)"
    )
    avg_speed_kmh: Optional[float] = Field(
        default=None,
        ge=20,
        le=120,
        description="Custom average speed in km/h. Overrides traffic_mode preset when provided."
    )
    fleet: Optional[FleetConfig] = Field(
        default=None,
        description="Fleet configuration with per-truck availability and constraints."
    )
    manual_distances: Optional[List[ManualDistanceEntry]] = Field(
        default=None,
        description="Manual distance entries for custom points or missing matrix entries."
    )
    custom_points: Optional[List[CustomPoint]] = Field(
        default=None,
        description="Custom point definitions with distances to sites (max 7)."
    )
    rate_overrides: Optional[RateOverrides] = Field(
        default=None,
        description="Rate overrides for flaring costs and consumption rates."
    )
    truck_ids: Optional[List[str]] = Field(
        default=None,
        description="DEPRECATED: Use fleet.trucks instead."
    )
    horizon_days: int = Field(
        default=1,
        ge=1,
        le=4,
        description="DEPRECATED: Use per-truck availability_days in fleet config."
    )
    debug_trace: bool = Field(
        default=False,
        description="If true, include full calculation trace in response"
    )

    @field_validator('traffic_mode')
    @classmethod
    def validate_traffic_mode(cls, v: str) -> str:
        allowed = {"normal", "heavy"}
        if v not in allowed:
            raise ValueError(f"traffic_mode must be one of {allowed}, got '{v}'")
        return v


class MultiRecommendationResponse(BaseModel):
    """Response containing recommendations for all active objectives."""
    recommendations: Dict[str, RecommendationResponse] = Field(
        ...,
        description="Recommendations keyed by objective: time, flaring, balanced"
    )
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    computation_time_seconds: float = Field(
        ...,
        description="Total computation time for all objectives"
    )


class ApproveRequest(BaseModel):
    """Request body for plan approval."""
    next_steps: bool = Field(default=False, description="If true, generate a new recommendation after applying")
    horizon_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=4,
        description="Planning horizon in days for the generated next-steps recommendation",
    )
    fleet: Optional["FleetConfig"] = Field(
        default=None,
        description="Fleet config for the next-steps recommendation. "
                    "If provided, overrides the fleet config stored in the original recommendation.",
    )


class ApprovalResponse(BaseModel):
    """Response for plan approval."""
    success: bool
    recommendation_id: str
    new_status: str
    message: str
    new_recommendation: Optional[Recommendation] = Field(
        default=None, description="New recommendation if next_steps was true"
    )


class HistoryResponse(BaseModel):
    """Response for recommendation history."""
    recommendations: List[Recommendation]
    total: int


class ConfigResponse(BaseModel):
    """Response for configuration endpoint."""
    config: OperationalConfig


class DistanceMatrixResponse(BaseModel):
    """Response for distance matrix endpoint."""
    matrix: Dict[str, Dict[str, float]]
    site_count: int


class DistanceQueryResponse(BaseModel):
    """Response for distance query between two sites."""
    from_site_id: str
    to_site_id: str
    distance_km: float
    drive_time_hours: Optional[float] = None


class Point(BaseModel):
    """A point reference - either a site or a custom point (no coordinates)."""
    kind: str = Field(..., description="Point kind: 'site' or 'custom'")
    site_id: Optional[str] = Field(default=None, description="Site ID (when kind='site')")
    custom_id: Optional[str] = Field(default=None, description="Custom point ID (when kind='custom')")
    label: Optional[str] = Field(default=None, description="Custom point label (optional)")

    @field_validator('kind')
    @classmethod
    def validate_kind(cls, v: str) -> str:
        allowed = {"site", "custom"}
        if v not in allowed:
            raise ValueError(f"kind must be one of {allowed}, got '{v}'")
        return v


class InTransitStart(BaseModel):
    """Configuration for a truck starting in transit between two points."""
    from_point: Point = Field(..., description="Origin point")
    to_point: Point = Field(..., description="Destination point")
    distance_from_from_km: float = Field(
        ..., ge=0, description="Distance already traveled from origin (km)"
    )
    total_edge_distance_km: float = Field(
        ..., ge=0, description="Total edge distance (km)"
    )


class TruckStart(BaseModel):
    """Start configuration for a truck."""
    kind: str = Field(..., description="Start kind: 'site', 'custom', or 'in_transit'")
    # For site start
    site_id: Optional[str] = Field(default=None, description="Site ID (when kind='site')")
    # For custom start
    custom_id: Optional[str] = Field(default=None, description="Custom point ID (when kind='custom')")
    label: Optional[str] = Field(default=None, description="Custom point label")
    # For in_transit start
    from_point: Optional[Point] = Field(default=None, description="Origin point (when kind='in_transit')")
    to_point: Optional[Point] = Field(default=None, description="Destination point (when kind='in_transit')")
    distance_from_from_km: Optional[float] = Field(default=None, ge=0, description="Distance from origin (km)")
    total_edge_distance_km: Optional[float] = Field(default=None, ge=0, description="Total edge distance (km)")

    @field_validator('kind')
    @classmethod
    def validate_kind(cls, v: str) -> str:
        allowed = {"site", "custom", "in_transit"}
        if v not in allowed:
            raise ValueError(f"kind must be one of {allowed}, got '{v}'")
        return v


class ManualDistanceEntry(BaseModel):
    """Manual distance entry for custom points or missing matrix entries."""
    from_key: str = Field(..., description="From key: 'site:{siteId}' or 'custom:{customId}'")
    to_key: str = Field(..., description="To key: 'site:{siteId}' or 'custom:{customId}'")
    distance_km: float = Field(..., gt=0, description="Distance in km (must be positive)")


class CustomPoint(BaseModel):
    """Custom point definition with coordinates for automatic routing."""
    id: str = Field(..., description="Unique ID e.g., 'C1', 'C2'")
    label: str = Field(..., description="Display label e.g., 'Custom 1'")
    latitude: Optional[float] = Field(default=None, ge=-90, le=90, description="WGS-84 latitude")
    longitude: Optional[float] = Field(default=None, ge=-180, le=180, description="WGS-84 longitude")
    distances_to_sites: Dict[str, float] = Field(
        default_factory=dict,
        description="Legacy: manual siteId -> distance_km (used when no coordinates)"
    )


class FleetConfigTruck(BaseModel):
    """Truck configuration for recommendation request with per-truck settings."""
    truck_id: str = Field(..., description="Truck ID")

    # Per-truck availability (working days) - replaces global horizon
    availability_days: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Number of days this truck can operate (1-4)"
    )

    # Start configuration
    start_mode: str = Field(
        default="site",
        description="Start mode: 'site', 'custom', or 'in_transit'"
    )
    start: Optional[TruckStart] = Field(
        default=None,
        description="Start configuration"
    )

    # DEPRECATED: delayed_start fields (ignored, kept for backward compatibility)
    delayed_start_enabled: bool = Field(
        default=False, description="DEPRECATED - ignored. Kept for backward compatibility."
    )
    available_from_day: int = Field(
        default=1, ge=1, description="DEPRECATED - ignored. Kept for backward compatibility."
    )

    # Initial containers already loaded on the truck (from previous day, etc.)
    initial_load: int = Field(
        default=0,
        ge=0,
        le=3,
        description="Number of containers already on the truck at start of planning (0-3)"
    )

    # Force end constraint (optional)
    force_end_enabled: bool = Field(
        default=False, description="If true, truck must end at specific point on specific day"
    )
    force_end_day: Optional[int] = Field(
        default=None, ge=1, description="Day index to force end"
    )
    force_end_point: Optional[Point] = Field(
        default=None, description="Point where truck must end"
    )

    @field_validator('start_mode')
    @classmethod
    def validate_start_mode(cls, v: str) -> str:
        allowed = {"site", "custom", "in_transit"}
        if v not in allowed:
            raise ValueError(f"start_mode must be one of {allowed}, got '{v}'")
        return v


class FleetConfig(BaseModel):
    """Fleet configuration for recommendation request."""
    trucks: List[FleetConfigTruck] = Field(
        default_factory=list, description="List of truck configurations"
    )


# Resolve forward reference in ApproveRequest.fleet
ApproveRequest.model_rebuild()


class RateOverrides(BaseModel):
    """Rate overrides for risk calculations."""
    flaring_costs: Dict[str, float] = Field(
        default_factory=dict,
        description="Flaring cost overrides: site_id -> flaring_cost_eur_mwh"
    )
    consumption_rates: Dict[str, float] = Field(
        default_factory=dict,
        description="Consumption rate overrides: site_id -> consumption_rate_kg_hour"
    )


class SitesQueryRequest(BaseModel):
    """Request for querying sites with rate overrides."""
    risk_filter: Optional[str] = Field(
        default=None,
        description="Filter by risk level: critical, warning, normal"
    )
    sort_by: str = Field(
        default="risk_score",
        description="Sort by: risk_score, name, hours_to_critical"
    )
    rate_overrides: Optional[RateOverrides] = Field(
        default=None,
        description="Rate overrides for risk calculations"
    )


# ── Scenario Generator ────────────────────────────────────────────────────────

class ScenarioRequest(BaseModel):
    """Request body for POST /generate_scenario."""
    scenario_type: str = Field(
        ...,
        description=(
            "Scenario type: 'balanced', 'high_demand', 'high_production', "
            "'capacity_crisis'"
        ),
    )
    seed: Optional[int] = Field(
        default=None,
        description=(
            "RNG seed for reproducible scenarios. "
            "If provided, the same seed always produces the same site-tier assignment. "
            "If omitted, OS entropy is used — different sites each call."
        ),
    )


class ScenarioResponse(BaseModel):
    """Response from POST /generate_scenario."""
    success: bool
    scenario_type: str
    description: str
    sites_updated: int
    applied_tiers: Dict[str, str]  # site_id → tier label (for debug / logging)


class AdvanceTimeRequest(BaseModel):
    """Request body for POST /advance_time."""
    hours: float = Field(
        ...,
        ge=-168,
        le=168,
        description="Signed hours to move simulation clock. Positive=advance, negative=rewind (range -168..168, excluding 0)."
    )

    @field_validator('hours')
    @classmethod
    def validate_non_zero_hours(cls, v: float) -> float:
        if abs(v) < 1e-9:
            raise ValueError("hours must be non-zero")
        return v


class AdvanceTimeResponse(BaseModel):
    """Response from POST /advance_time."""
    success: bool
    advanced_hours: float
    message: str
    total_sites: int
    critical_count: int
    warning_count: int


class RestartSimulationResponse(BaseModel):
    """Response from POST /simulation/restart."""
    success: bool
    message: str
    restored: bool
    total_sites: int
    critical_count: int
    warning_count: int
