"""FastAPI routes for the logistics API."""

import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import httpx

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Body
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..models import Site, Container, Truck, OperationalConfig, Bay, ProductionRates, ProductionRateMode, RecommendationStatus
from ..utils.conversions import pressure_to_kg, kg_to_mwh
from ..services.data_loader import DataLoader
from ..services.risk_calculator import RiskCalculator, RiskLevel
from ..services.recommendation import RecommendationService, ObjectiveFunction, RECOMMENDATIONS_FILE
from ..services.excel_export import build_recommendation_excel
from ..services.analytics import get_analytics_service
from .schemas import (
    UploadResponse,
    SystemState,
    SiteWithRisk,
    SiteListResponse,
    SiteDetailResponse,
    SitePatchRequest,
    SitesQueryRequest,
    ContainerListResponse,
    TruckListResponse,
    RecommendationRequest,
    RecommendationResponse,
    MultiRecommendationRequest,
    MultiRecommendationResponse,
    ApproveRequest,
    ApprovalResponse,
    HistoryResponse,
    ConfigResponse,
    DistanceMatrixResponse,
    DistanceQueryResponse,
    BaySchema,
    ProductionRatesSchema,
    ErrorCode,
    ValidationErrorResponse,
    StateSnapshot,
    SnapshotSite,
    SnapshotBay,
    SnapshotContainer,
    ScenarioRequest,
    ScenarioResponse,
    AdvanceTimeRequest,
    AdvanceTimeResponse,
    RestartSimulationResponse,
)
from ..services.scenario_generator import apply_scenario, SCENARIO_DEFINITIONS

router = APIRouter()
logger = logging.getLogger(__name__)

_SERIAL_RE = re.compile(r'^\d{3,4}$')


def _validate_bay_serials(sites: Dict) -> Optional[str]:
    """
    Cross-site serial number validation.

    Rules:
      1. Every non-None serial_number must match exactly 3 or 4 digits.
      2. No two bays (across all sites) may share the same serial_number.

    Returns None if valid, or an error message string if invalid.
    """
    seen: Dict[str, str] = {}  # serial → "site_id.bay_id" for duplicate reporting
    for site_id, site in sites.items():
        for bay in getattr(site, 'bays', []):
            sn = bay.serial_number
            if sn is None:
                continue
            if not _SERIAL_RE.match(sn):
                return (
                    f"Invalid bay configuration: serial numbers must be unique and 3–4 digits "
                    f"(site={site_id}, bay={bay.bay_id}, serial={sn!r})"
                )
            key = f"{site_id}.{bay.bay_id}"
            if sn in seen:
                return (
                    f"Invalid bay configuration: serial numbers must be unique and 3–4 digits "
                    f"(duplicate serial={sn!r}: {seen[sn]} and {key})"
                )
            seen[sn] = key
    return None


def _site_capacity_kg(site: Site) -> float:
    """Return nominal physical site capacity in kg at 250 bar per bay."""
    return float(site.bays_fixed * pressure_to_kg(250))


def _build_analytics_site_states(loader: DataLoader) -> List[Dict[str, Any]]:
    """Build a consistent current-state snapshot for analytics persistence."""
    risk_calc = RiskCalculator(loader.config)
    assessments = risk_calc.assess_all_sites(loader.sites)
    assessment_map = {a.site_id: a for a in assessments}

    site_states: List[Dict[str, Any]] = []
    for site in loader.sites.values():
        assessment = assessment_map.get(site.id)
        site_states.append(
            {
                "id": site.id,
                "name": site.name,
                "site_type": site.site_type.value if hasattr(site.site_type, "value") else str(site.site_type),
                "risk_level": assessment.risk_level.value if assessment and hasattr(assessment.risk_level, "value") else (
                    str(assessment.risk_level) if assessment else None
                ),
                "hours_to_critical": float(assessment.hours_to_critical) if assessment else None,
                "utilization_percentage": site.utilization_percentage,
                "inventory_kg": site.total_kg,
                "capacity_kg": _site_capacity_kg(site),
            }
        )
    return site_states


# Global data loader instance
_data_loader: Optional[DataLoader] = None
_data_loaded_at: Optional[datetime] = None
_last_recommendation_at: Optional[datetime] = None
_recommendation_service: Optional[RecommendationService] = None


def get_data_loader() -> DataLoader:
    """Get or create the data loader instance."""
    global _data_loader, _data_loaded_at
    if _data_loader is None:
        data_dir = Path(__file__).parent.parent / "data"
        _data_loader = DataLoader(data_dir=data_dir)
        # Try to load existing data
        if (data_dir / "sites.json").exists():
            _data_loader.load_from_json()
            _data_loaded_at = datetime.utcnow()
    return _data_loader


def get_recommendation_service() -> RecommendationService:
    """Get or create the recommendation service instance.

    If the service was previously created without a routing service (GraphHopper
    was unavailable at startup) but routing is now available, the routing service
    is injected and road-network matrices are rebuilt so road geometry is used.
    """
    global _recommendation_service
    loader = get_data_loader()

    from .routing_routes import get_routing_service
    routing_svc = get_routing_service()

    if _recommendation_service is None:
        _recommendation_service = RecommendationService(
            sites=loader.sites,
            containers=loader.containers,
            trucks=loader.trucks,
            distance_matrix=loader.distance_matrix,
            config=loader.config,
            routing_service=routing_svc,
        )
    elif _recommendation_service.routing_service is None and routing_svc is not None:
        # GraphHopper became available after initial startup — inject it now
        # and rebuild road-network distance/time matrices.
        logger.info("GraphHopper now available — upgrading recommendation service to road routing")
        _recommendation_service.routing_service = routing_svc
        _recommendation_service._use_road_routing = True
        try:
            road_dist, road_time = routing_svc.build_site_matrices(loader.sites)
            _recommendation_service.distance_matrix = road_dist
            _recommendation_service.time_matrix_minutes = road_time
            logger.info("Road-network matrices rebuilt successfully")
        except Exception as e:
            logger.warning("Failed to rebuild road matrices: %s", e)

    return _recommendation_service


def reset_recommendation_service():
    """Reset recommendation service after data changes."""
    global _recommendation_service
    _recommendation_service = None


def build_site_with_risk(site: Site, assessment, config: OperationalConfig) -> SiteWithRisk:
    """Build a SiteWithRisk response from a site and its risk assessment.

    All usable kg/MWh values are computed here from config.usable_floor_bar so that
    the frontend never needs to independently recompute them (single source of truth).
    """
    floor = config.usable_floor_bar
    kg_at_floor = pressure_to_kg(floor)
    max_usable_per_bay = pressure_to_kg(250) - kg_at_floor  # e.g. 2829 - 131.6 = 2697.4

    # Per-bay schema with both raw and usable values
    bay_schemas = []
    for bay in site.bays:
        kg_usable = max(0.0, bay.kg - kg_at_floor)
        mwh_usable = kg_to_mwh(kg_usable)
        bay_schemas.append(BaySchema(
            bay_id=bay.bay_id,
            pressure_bar=bay.pressure_bar,
            kg=bay.kg,
            mwh=bay.mwh,
            kg_usable=kg_usable,
            mwh_usable=mwh_usable,
            serial_number=bay.serial_number,
        ))

    # Site-level usable totals (primary metrics)
    total_kg_usable = sum(b.kg_usable for b in bay_schemas)
    total_mwh_usable = kg_to_mwh(total_kg_usable)
    max_usable_capacity = site.bays_fixed * max_usable_per_bay
    utilization_usable_pct = (
        (total_kg_usable / max_usable_capacity * 100) if max_usable_capacity > 0 else 0.0
    )

    # Build production rates schema if present
    production_schema = None
    if site.production:
        production_schema = ProductionRatesSchema(
            mode=site.production.mode.value,
            kg_per_h=site.production.kg_per_h,
            mwh_per_h=site.production.mwh_per_h,
            effective_kg_per_h=site.production.effective_kg_per_h,
            effective_mwh_per_h=site.production.effective_mwh_per_h,
        )

    return SiteWithRisk(
        id=site.id,
        name=site.name,
        site_type=site.site_type.value,
        latitude=site.latitude,
        longitude=site.longitude,
        bays_fixed=site.bays_fixed,
        bays=bay_schemas,
        total_kg=site.total_kg,
        total_mwh=site.total_mwh,
        total_kg_usable=total_kg_usable,
        total_mwh_usable=total_mwh_usable,
        usable_floor_bar=floor,
        is_full=site.is_full,
        min_pressure=site.min_pressure,
        max_pressure=site.max_pressure,
        avg_pressure=site.avg_pressure,
        utilization_percentage=site.utilization_percentage,
        utilization_usable_pct=utilization_usable_pct,
        consumption_rate_kg_hour=site.consumption_rate_kg_hour,
        production=production_schema,
        flaring_cost_eur_mwh=site.flaring_cost_eur_mwh,
        flaring_loss_eur_per_h=site.flaring_loss_eur_per_h,
        risk_level=assessment.risk_level.value,
        hours_to_critical=assessment.hours_to_critical,
        risk_score=assessment.risk_score,
        risk_explanation=assessment.explanation,
    )


# ============== System State ==============

@router.get("/state", response_model=SystemState)
async def get_system_state():
    """Get current system state overview."""
    loader = get_data_loader()
    return SystemState(
        sites_count=len(loader.sites),
        containers_count=len(loader.containers),
        trucks_count=len(loader.trucks),
        mode="real",
        data_loaded_at=_data_loaded_at,
        last_recommendation_at=_last_recommendation_at,
    )


@router.get("/snapshot", response_model=StateSnapshot)
async def get_state_snapshot():
    """Return compact mutable-state fingerprint (bay pressures + container locations)."""
    loader = get_data_loader()
    risk_calc = RiskCalculator(loader.config)

    snap_sites = []
    for site in loader.get_site_list():
        containers = loader.get_containers_at_site(site.id)
        assessment = risk_calc.assess_site(site, containers)
        snap_sites.append(SnapshotSite(
            id=site.id,
            total_kg=assessment.total_kg,
            utilization_percentage=assessment.utilization_percentage,
            bays=[SnapshotBay(bay_id=b.bay_id, pressure_bar=b.pressure_bar) for b in site.bays],
        ))

    snap_containers = [
        SnapshotContainer(
            id=c.id,
            location_site_id=c.location_site_id,
            pressure_bar=c.pressure_bar,
            status=c.status.value,
        )
        for c in loader.containers.values()
    ]

    return StateSnapshot(sites=snap_sites, containers=snap_containers)


# ============== Data Upload ==============

@router.post("/upload/sites", response_model=UploadResponse)
async def upload_sites(file: UploadFile = File(...)):
    """Upload sites data from JSON file."""
    global _data_loaded_at
    try:
        content = await file.read()
        data = json.loads(content)

        loader = get_data_loader()
        count = loader.update_from_json_upload(data, "sites")

        # Validate serial number uniqueness and format before persisting
        _serial_err = _validate_bay_serials(loader.sites)
        if _serial_err:
            raise HTTPException(status_code=422, detail=_serial_err)

        loader.save_to_json()
        reset_recommendation_service()

        _data_loaded_at = datetime.utcnow()

        return UploadResponse(
            success=True,
            message=f"Successfully uploaded {count} sites",
            items_updated=count,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload/containers", response_model=UploadResponse)
async def upload_containers(file: UploadFile = File(...)):
    """Upload containers data from JSON file."""
    global _data_loaded_at
    try:
        content = await file.read()
        data = json.loads(content)

        loader = get_data_loader()
        count = loader.update_from_json_upload(data, "containers")
        loader.save_to_json()
        reset_recommendation_service()

        _data_loaded_at = datetime.utcnow()

        return UploadResponse(
            success=True,
            message=f"Successfully uploaded {count} containers",
            items_updated=count,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload/trucks", response_model=UploadResponse)
async def upload_trucks(file: UploadFile = File(...)):
    """Upload trucks data from JSON file."""
    global _data_loaded_at
    try:
        content = await file.read()
        data = json.loads(content)

        loader = get_data_loader()
        count = loader.update_from_json_upload(data, "trucks")
        loader.save_to_json()
        reset_recommendation_service()

        _data_loaded_at = datetime.utcnow()

        return UploadResponse(
            success=True,
            message=f"Successfully uploaded {count} trucks",
            items_updated=count,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload/config", response_model=UploadResponse)
async def upload_config(file: UploadFile = File(...)):
    """Upload operational configuration from JSON file."""
    global _data_loaded_at
    try:
        content = await file.read()
        data = json.loads(content)

        loader = get_data_loader()
        count = loader.update_from_json_upload(data, "config")
        loader.save_to_json()
        reset_recommendation_service()

        _data_loaded_at = datetime.utcnow()

        return UploadResponse(
            success=True,
            message="Successfully uploaded configuration",
            items_updated=count,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Sites ==============

def _build_site_list_response(
    risk_filter: Optional[str],
    sort_by: str,
) -> SiteListResponse:
    """Build site list response off the event loop."""
    loader = get_data_loader()
    risk_calc = RiskCalculator(loader.config)

    container_list = list(loader.containers.values())
    assessments = risk_calc.assess_all_sites(loader.sites, container_list)

    if risk_filter:
        assessments = [a for a in assessments if a.risk_level.value == risk_filter]

    if sort_by == "name":
        assessments.sort(key=lambda a: a.site_name)
    elif sort_by == "hours_to_critical":
        assessments.sort(key=lambda a: a.hours_to_critical)

    sites_with_risk = []
    for a in assessments:
        site = loader.sites.get(a.site_id)
        if site:
            sites_with_risk.append(build_site_with_risk(site, a, loader.config))

    critical_count = sum(1 for a in assessments if a.risk_level == RiskLevel.CRITICAL)
    warning_count = sum(1 for a in assessments if a.risk_level == RiskLevel.WARNING)

    return SiteListResponse(
        sites=sites_with_risk,
        total=len(sites_with_risk),
        critical_count=critical_count,
        warning_count=warning_count,
    )


@router.get("/sites", response_model=SiteListResponse)
async def list_sites(
    risk_filter: Optional[str] = Query(
        None,
        description="Filter by risk level: critical, warning, normal"
    ),
    sort_by: str = Query(
        "risk_score",
        description="Sort by: risk_score, name, hours_to_critical"
    ),
):
    """List all sites with risk assessment."""
    return await run_in_threadpool(_build_site_list_response, risk_filter, sort_by)


def _build_site_list_with_overrides_response(request: SitesQueryRequest) -> SiteListResponse:
    """Build overridden site list response off the event loop."""
    loader = get_data_loader()

    sites_copy = {}
    for site_id, site in loader.sites.items():
        site_copy = site.model_copy(deep=True)

        if request.rate_overrides and site_id in request.rate_overrides.flaring_costs:
            site_copy.flaring_cost_eur_mwh = request.rate_overrides.flaring_costs[site_id]

        if request.rate_overrides and site_id in request.rate_overrides.consumption_rates:
            site_copy.consumption_rate_kg_hour = request.rate_overrides.consumption_rates[site_id]

        sites_copy[site_id] = site_copy

    risk_calc = RiskCalculator(loader.config)
    assessments = risk_calc.assess_all_sites(sites_copy)

    if request.risk_filter:
        assessments = [a for a in assessments if a.risk_level.value == request.risk_filter]

    if request.sort_by == "name":
        assessments.sort(key=lambda a: a.site_name)
    elif request.sort_by == "hours_to_critical":
        assessments.sort(key=lambda a: a.hours_to_critical)

    sites_with_risk = []
    for a in assessments:
        site = sites_copy.get(a.site_id)
        if site:
            sites_with_risk.append(build_site_with_risk(site, a, loader.config))

    critical_count = sum(1 for a in assessments if a.risk_level == RiskLevel.CRITICAL)
    warning_count = sum(1 for a in assessments if a.risk_level == RiskLevel.WARNING)

    return SiteListResponse(
        sites=sites_with_risk,
        total=len(sites_with_risk),
        critical_count=critical_count,
        warning_count=warning_count,
    )


@router.post("/sites/query", response_model=SiteListResponse)
async def query_sites_with_overrides(request: SitesQueryRequest):
    """Query sites with rate overrides applied for risk calculations.

    This endpoint allows applying temporary rate overrides (flaring costs,
    consumption rates) that affect risk calculations without modifying
    the underlying site data.
    """
    return await run_in_threadpool(_build_site_list_with_overrides_response, request)


@router.get("/sites/{site_id}", response_model=SiteDetailResponse)
async def get_site_detail(site_id: str):
    """Get detailed information for a specific site."""
    loader = get_data_loader()

    site = loader.sites.get(site_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Site not found: {site_id}")

    # Get containers at this site
    containers = loader.get_containers_at_site(site_id)

    # Calculate risk
    risk_calc = RiskCalculator(loader.config)
    assessment = risk_calc.assess_site(site, containers)

    return SiteDetailResponse(
        site=site,
        containers=containers,
        risk_level=assessment.risk_level.value,
        hours_to_critical=assessment.hours_to_critical,
        risk_score=assessment.risk_score,
        risk_explanation=assessment.explanation,
        recent_deliveries=[],  # Would come from historical data
    )


@router.patch("/sites/{site_id}", response_model=SiteWithRisk)
async def patch_site(site_id: str, request: SitePatchRequest):
    """Partial update of site bay pressures and production rates."""
    loader = get_data_loader()

    site = loader.sites.get(site_id)
    if not site:
        raise HTTPException(status_code=404, detail=f"Site not found: {site_id}")

    # Update bay pressures
    if request.bays is not None:
        for bay_update in request.bays:
            # Find the bay to update
            bay_found = False
            for bay in site.bays:
                if bay.bay_id == bay_update.bay_id:
                    if bay_update.pressure_bar < 0 or bay_update.pressure_bar > 250:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Pressure must be 0-250 bar, got {bay_update.pressure_bar}"
                        )
                    bay.pressure_bar = bay_update.pressure_bar
                    if bay_update.serial_number is not None:
                        bay.serial_number = bay_update.serial_number
                    bay_found = True
                    break
            if not bay_found:
                raise HTTPException(
                    status_code=400,
                    detail=f"Bay not found: {bay_update.bay_id}"
                )

    # Update production rates (producers only)
    if request.production is not None:
        if not site.is_producer:
            raise HTTPException(
                status_code=400,
                detail="Production rates can only be set for producer sites"
            )
        try:
            mode = ProductionRateMode(request.production.mode)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid production mode: {request.production.mode}. Must be KG_PER_H, MWH_PER_H, or UNKNOWN"
            )
        site.production = ProductionRates(
            mode=mode,
            kg_per_h=request.production.kg_per_h,
            mwh_per_h=request.production.mwh_per_h,
        )

    # Validate serial number uniqueness and format across all sites before persisting
    _serial_err = _validate_bay_serials(loader.sites)
    if _serial_err:
        raise HTTPException(status_code=422, detail=_serial_err)

    # Save to JSON for persistence
    loader.save_to_json()
    reset_recommendation_service()

    # Recalculate risk for response
    risk_calc = RiskCalculator(loader.config)
    containers = loader.get_containers_at_site(site_id)
    assessment = risk_calc.assess_site(site, containers)

    return build_site_with_risk(site, assessment, loader.config)


# ============== Containers ==============

@router.get("/containers", response_model=ContainerListResponse)
async def list_containers(
    site_id: Optional[str] = Query(None, description="Filter by site ID"),
    status: Optional[str] = Query(None, description="Filter by status: at_site, in_transit"),
):
    """List all containers."""
    loader = get_data_loader()
    containers = list(loader.containers.values())

    # Apply filters
    if site_id:
        containers = [c for c in containers if c.location_site_id == site_id]
    if status:
        containers = [c for c in containers if c.status.value == status]

    full_count = sum(1 for c in containers if c.is_full)
    empty_count = sum(1 for c in containers if c.is_empty)
    in_transit_count = sum(1 for c in containers if c.location_site_id == "")

    return ContainerListResponse(
        containers=containers,
        total=len(containers),
        full_count=full_count,
        empty_count=empty_count,
        in_transit_count=in_transit_count,
    )


# ============== Trucks ==============

def _build_truck_list_response() -> TruckListResponse:
    """Build truck list response off the event loop."""
    loader = get_data_loader()
    trucks = list(loader.trucks.values())

    available_count = sum(1 for t in trucks if t.is_empty)

    return TruckListResponse(
        trucks=trucks,
        total=len(trucks),
        available_count=available_count,
    )


@router.get("/trucks", response_model=TruckListResponse)
async def list_trucks():
    """List all trucks."""
    return await run_in_threadpool(_build_truck_list_response)


# ============== Optimal-days scoring helper ==============

def _score_recommendation(rec: Any, n_trucks: int, days: int) -> float:
    """Score a Recommendation for optimal-days selection. Lower is better.

    Priority order for planning quality:
    1. Avoid unmitigated high-risk demand (stockout/flaring risk exposure)
    2. Avoid end-of-horizon container imbalance
    3. Maximize service output (delivered containers, active service days)
    4. Minimize cost among comparable-risk plans

    This intentionally avoids over-favoring "shorter calendar horizon" plans.
    """
    if rec is None or rec.status == "infeasible":
        return float("inf")

    total_cost = float(rec.total_cost_eur or 0.0)
    _ai_summary = _extract_ai_coordinator_summary(rec)
    if _ai_summary is not None:
        critical_unserved = float(_ai_summary.get("critical_unserved", 0) or 0)
        critical_impact = min(float(_ai_summary.get("critical_unserved_impact_eur", 0.0) or 0.0), 1_000_000_000.0)
        active_truck_days = float(_ai_summary.get("active_truck_days", 0) or 0)
        short_active_days = float(_ai_summary.get("short_active_days", 0) or 0)
        underused_drive_hours = float(_ai_summary.get("underused_drive_hours", 0.0) or 0.0)
        future_unserved = float(_ai_summary.get("future_unserved", 0) or 0)
        future_impact = min(float(_ai_summary.get("future_unserved_impact_eur", 0.0) or 0.0), 1_000_000_000.0)
        idle_trucks = float(_ai_summary.get("idle_trucks", 0) or 0)
        imbalance = float(_ai_summary.get("end_imbalance", getattr(rec, "end_of_horizon_imbalance", 0) or 0) or 0)
        return (
            critical_unserved * 1e15
            + critical_impact * 1e5
            + active_truck_days * 1e9
            + short_active_days * 1e8
            + underused_drive_hours * 1e6
            + future_unserved * 1e5
            + future_impact * 1e1
            + imbalance * 1e7
            + idle_trucks * 1e4
            + total_cost
        )

    total_delivered = float(sum(r.total_containers_delivered for r in rec.routes))
    total_stops = float(sum(max(0, r.num_stops) for r in rec.routes))
    active_days = len({
        int(getattr(r, "day_index", 0) or 0)
        for r in rec.routes
        if (r.num_stops or 0) > 0
    })
    active_days = max(0, active_days)

    # If risk remains unmitigated, this should dominate selection.
    critical_unserved = _extract_unserved_critical_count(rec)
    _feedback = getattr(rec, "solution_feedback", None) or []
    _risk_unmitigated = (
        critical_unserved > 0
        or any(
            isinstance(item, dict) and item.get("code") == "RISK_UNMITIGATED"
            for item in _feedback
        )
    )
    risk_unmitigated_penalty = 350_000.0 if _risk_unmitigated else 0.0
    critical_unserved_penalty = critical_unserved * 60_000.0

    risk_score = float(getattr(rec, "solution_risk_score", 0.0) or 0.0)
    risk_score_penalty = risk_score * 1_200.0

    flaring_h = float(getattr(rec, "flaring_exposure_hours", 0.0) or 0.0)
    flaring_penalty = flaring_h * 200.0 + max(0.0, flaring_h - 10.0) * 3_000.0

    imbalance = float(getattr(rec, "end_of_horizon_imbalance", 0) or 0)
    imbalance_penalty = imbalance * 7_500.0

    # Prefer using contracted truck-days for real service (light reward, not dominant).
    active_day_reward = active_days * 120.0
    service_reward = total_delivered * 220.0 + total_stops * 18.0

    # Mild penalty for idle contracted capacity (kept small versus risk penalties).
    idle_truck_days = max(0, n_trucks * days - active_days)
    idle_penalty = idle_truck_days * 35.0

    # Daily-usage preference (operator policy):
    # reward plans that achieve meaningful operational work per active day.
    stops_per_active_day = total_stops / max(1.0, float(active_days))
    delivered_per_active_day = total_delivered / max(1.0, float(active_days))
    daily_usage_reward = stops_per_active_day * 50.0 + delivered_per_active_day * 140.0

    return (
        total_cost
        + risk_unmitigated_penalty
        + critical_unserved_penalty
        + risk_score_penalty
        + flaring_penalty
        + imbalance_penalty
        + idle_penalty
        - service_reward
        - active_day_reward
        - daily_usage_reward
    )


def _extract_unserved_critical_count(rec: Any) -> int:
    """Best-effort parser for unserved critical-site count from diagnostics."""
    if rec is None:
        return 0

    pool: List[str] = []
    for item in (getattr(rec, "solution_feedback", None) or []):
        if isinstance(item, dict):
            pool.append(f"{item.get('code', '')} {item.get('message', '')}".strip())
        else:
            code = getattr(item, "code", "")
            msg = getattr(item, "message", "")
            pool.append(f"{code} {msg}".strip())
    pool.extend(getattr(rec, "warnings", None) or [])

    for msg in pool:
        if not re.search(r"RISK_UNMITIGATED|high-risk demand|critical", str(msg), re.IGNORECASE):
            continue
        m = re.search(r"(\d+)\s+critical", str(msg), flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def _extract_ai_coordinator_summary(rec: Any) -> Optional[Dict[str, Any]]:
    if rec is None:
        return None
    for item in (getattr(rec, "solution_feedback", None) or []):
        if isinstance(item, dict) and item.get("code") == "AI_COORDINATOR_SUMMARY":
            return item
    return None


def _has_unmitigated_critical_risk(rec: Any) -> bool:
    """Return True when recommendation feedback reports unserved critical demand."""
    return _extract_unserved_critical_count(rec) > 0


def _has_end_load_not_zero(rec: Any) -> bool:
    """True when diagnostics indicate truck(s) ended with residual load."""
    if rec is None:
        return False
    for msg in (getattr(rec, "warnings", None) or []):
        txt = str(msg)
        if "END_LOAD_NOT_ZERO" in txt:
            return True
        if re.search(r"ends?\s+with\s+\d+\s+full", txt, re.IGNORECASE):
            return True
    return False


# ============== Recommendations ==============

@router.post("/recommend", response_model=RecommendationResponse)
async def generate_recommendation(request: RecommendationRequest):
    """Generate a route recommendation."""
    global _last_recommendation_at
    import traceback
    import logging

    logger = logging.getLogger(__name__)

    # Log sanitized request
    logger.info(f"[/recommend] Request: objective={request.objective}, "
                f"truck_ids={request.truck_ids}, site_ids={request.site_ids}, "
                f"max_search_seconds={request.max_search_seconds}, "
                f"traffic_mode={request.traffic_mode}")

    start_time = time.time()
    loader = get_data_loader()

    # Input validation - return 422 for invalid input
    validation_errors = []

    def validate_point(point, field_prefix: str, allow_custom: bool = True):
        """Validate a Point (site or custom)."""
        if point.kind == "site":
            if not point.site_id:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.site_id",
                    "message": "site_id required when kind is 'site'"
                })
            elif point.site_id not in loader.sites:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.site_id",
                    "message": f"Unknown site ID: {point.site_id}"
                })
        elif point.kind == "custom":
            if not allow_custom:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.kind",
                    "message": "Custom points not allowed here"
                })
            elif not point.custom_id:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.custom_id",
                    "message": "custom_id required when kind is 'custom'"
                })

    # Build custom points lookup
    custom_points_map: Dict[str, Dict[str, float]] = {}
    if request.custom_points:
        for cp in request.custom_points:
            custom_points_map[cp.id] = cp.distances_to_sites

    def get_distance(from_key: str, to_key: str) -> float:
        """Get distance between two points from matrix, custom points, or manual distances."""
        # Check manual distances first
        if request.manual_distances:
            for md in request.manual_distances:
                if (md.from_key == from_key and md.to_key == to_key) or \
                   (md.from_key == to_key and md.to_key == from_key):
                    return md.distance_km

        # Check matrix for site-to-site
        if from_key.startswith("site:") and to_key.startswith("site:"):
            from_id = from_key.replace("site:", "")
            to_id = to_key.replace("site:", "")
            if from_id in loader.distance_matrix:
                dist = loader.distance_matrix[from_id].get(to_id)
                if dist is not None:
                    return dist
            if to_id in loader.distance_matrix:
                dist = loader.distance_matrix[to_id].get(from_id)
                if dist is not None:
                    return dist

        # Check custom point distances (custom <-> site)
        if from_key.startswith("custom:") and to_key.startswith("site:"):
            custom_id = from_key.replace("custom:", "")
            site_id = to_key.replace("site:", "")
            if custom_id in custom_points_map:
                dist = custom_points_map[custom_id].get(site_id)
                if dist is not None:
                    return dist
        if from_key.startswith("site:") and to_key.startswith("custom:"):
            site_id = from_key.replace("site:", "")
            custom_id = to_key.replace("custom:", "")
            if custom_id in custom_points_map:
                dist = custom_points_map[custom_id].get(site_id)
                if dist is not None:
                    return dist

        return -1  # Not found

    # Validate custom points (max 7)
    MAX_CUSTOM_POINTS = 7
    if request.custom_points and len(request.custom_points) > MAX_CUSTOM_POINTS:
        validation_errors.append({
            "code": ErrorCode.INVALID_INPUT,
            "field": "custom_points",
            "message": f"Maximum {MAX_CUSTOM_POINTS} custom points allowed, got {len(request.custom_points)}"
        })

    # Validate custom points: must have either coordinates or manual distances
    if request.custom_points:
        for i, cp in enumerate(request.custom_points):
            has_coords = cp.latitude is not None and cp.longitude is not None
            has_manual = bool(cp.distances_to_sites)
            if not has_coords and not has_manual:
                validation_errors.append({
                    "code": ErrorCode.INVALID_INPUT,
                    "field": f"custom_points[{i}]",
                    "message": f"Custom point '{cp.id}' needs coordinates (latitude/longitude) or manual distances"
                })

    # Validate site_ids reference existing sites
    if request.site_ids:
        invalid_sites = [sid for sid in request.site_ids if sid not in loader.sites]
        if invalid_sites:
            validation_errors.append({
                "code": ErrorCode.INVALID_INPUT,
                "field": "site_ids",
                "message": f"Unknown site IDs: {invalid_sites}"
            })

    # Validate fleet configuration
    if request.fleet and request.fleet.trucks:
        for i, fc in enumerate(request.fleet.trucks):
            availability = fc.availability_days
            # In optimize mode horizon_days is the search ceiling; availability_days
            # is irrelevant for validation — override it so no downstream check can
            # reject a valid optimal_days request.
            if request.optimal_days and availability != request.horizon_days:
                logger.info(
                    "[VALIDATION] optimize mode → availability overridden: "
                    "truck=%s availability_days=%d → horizon_days=%d",
                    fc.truck_id, availability, request.horizon_days,
                )
                availability = request.horizon_days

            # Validate truck exists
            if fc.truck_id not in loader.trucks:
                validation_errors.append({
                    "code": ErrorCode.INVALID_TRUCK_CONFIG,
                    "field": f"fleet.trucks[{i}].truck_id",
                    "message": f"Unknown truck ID: {fc.truck_id}"
                })

            # Validate start configuration
            if fc.start:
                start = fc.start
                if start.kind == "site":
                    if not start.site_id:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start.site_id",
                            "message": "site_id required for site start"
                        })
                    elif start.site_id not in loader.sites:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_POINT,
                            "field": f"fleet.trucks[{i}].start.site_id",
                            "message": f"Unknown site ID: {start.site_id}"
                        })
                elif start.kind == "custom":
                    if not start.custom_id:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start.custom_id",
                            "message": "custom_id required for custom start"
                        })
                    elif start.custom_id not in custom_points_map:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_POINT,
                            "field": f"fleet.trucks[{i}].start.custom_id",
                            "message": f"Custom point '{start.custom_id}' not found in custom_points"
                        })
                    else:
                        # Check if custom point has coordinates (auto-routed) or manual distances
                        cp_obj = next((c for c in request.custom_points if c.id == start.custom_id), None)
                        has_coords = cp_obj and cp_obj.latitude is not None and cp_obj.longitude is not None
                        if not has_coords and not custom_points_map[start.custom_id]:
                            validation_errors.append({
                                "code": ErrorCode.MISSING_DISTANCE,
                                "field": f"fleet.trucks[{i}].start.custom_id",
                                "message": f"Custom point '{start.custom_id}' has no coordinates or distances defined"
                            })
                elif start.kind == "in_transit":
                    if not start.from_point or not start.to_point:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start",
                            "message": "from_point and to_point required for in_transit start"
                        })
                    else:
                        validate_point(start.from_point, f"fleet.trucks[{i}].start.from_point")
                        validate_point(start.to_point, f"fleet.trucks[{i}].start.to_point")

                        # Validate from != to
                        from_key = f"{start.from_point.kind}:{start.from_point.site_id or start.from_point.custom_id}"
                        to_key = f"{start.to_point.kind}:{start.to_point.site_id or start.to_point.custom_id}"
                        if from_key == to_key:
                            validation_errors.append({
                                "code": ErrorCode.INVALID_INTRANSIT_POSITION,
                                "field": f"fleet.trucks[{i}].start",
                                "message": "from_point and to_point must be different"
                            })

                        # Validate total_edge_distance_km
                        if start.total_edge_distance_km is None:
                            # Try to get from matrix or manual distances
                            dist = get_distance(from_key, to_key)
                            if dist < 0:
                                validation_errors.append({
                                    "code": ErrorCode.MISSING_DISTANCE,
                                    "field": f"fleet.trucks[{i}].start.total_edge_distance_km",
                                    "message": f"Distance not found for {from_key} -> {to_key}. Provide total_edge_distance_km or add to manual_distances."
                                })
                        else:
                            # Validate distance_from_from_km <= total_edge_distance_km
                            if start.distance_from_from_km is not None:
                                if start.distance_from_from_km > start.total_edge_distance_km:
                                    validation_errors.append({
                                        "code": ErrorCode.INVALID_INTRANSIT_POSITION,
                                        "field": f"fleet.trucks[{i}].start.distance_from_from_km",
                                        "message": f"distance_from_from_km ({start.distance_from_from_km}) exceeds total_edge_distance_km ({start.total_edge_distance_km})"
                                    })

            # NOTE: delayed_start fields are deprecated and ignored

            # Validate force end constraint.
            # In optimal-days mode, force_end is still respected; trial horizons
            # simply start from the latest required force_end day.
            if fc.force_end_enabled:
                if fc.force_end_day is None:
                    validation_errors.append({
                        "code": ErrorCode.MISSING_FORCE_END_CONFIG,
                        "field": f"fleet.trucks[{i}].force_end_day",
                        "message": "force_end_day required when force_end_enabled is true"
                    })
                elif not request.optimal_days and fc.force_end_day > availability:
                    validation_errors.append({
                        "code": ErrorCode.INVALID_TRUCK_CONFIG,
                        "field": f"fleet.trucks[{i}].force_end_day",
                        "message": f"force_end_day ({fc.force_end_day}) exceeds availability_days ({availability})"
                    })
                elif not request.optimal_days and fc.force_end_day > request.horizon_days:
                    validation_errors.append({
                        "code": ErrorCode.INVALID_TRUCK_CONFIG,
                        "field": f"fleet.trucks[{i}].force_end_day",
                        "message": f"force_end_day ({fc.force_end_day}) exceeds horizon_days ({request.horizon_days})"
                    })
                if not fc.force_end_point:
                    validation_errors.append({
                        "code": ErrorCode.MISSING_FORCE_END_CONFIG,
                        "field": f"fleet.trucks[{i}].force_end_point",
                        "message": "force_end_point required when force_end_enabled is true"
                    })
                else:
                    validate_point(fc.force_end_point, f"fleet.trucks[{i}].force_end_point")
                    # Validate custom force_end_point has distances
                    if fc.force_end_point.kind == "custom" and fc.force_end_point.custom_id:
                        if fc.force_end_point.custom_id not in custom_points_map:
                            validation_errors.append({
                                "code": ErrorCode.INVALID_POINT,
                                "field": f"fleet.trucks[{i}].force_end_point.custom_id",
                                "message": f"Custom point '{fc.force_end_point.custom_id}' not found in custom_points"
                            })
                        else:
                            fe_cp = next((c for c in request.custom_points if c.id == fc.force_end_point.custom_id), None)
                            fe_has_coords = fe_cp and fe_cp.latitude is not None and fe_cp.longitude is not None
                            if not fe_has_coords and not custom_points_map[fc.force_end_point.custom_id]:
                                validation_errors.append({
                                    "code": ErrorCode.MISSING_DISTANCE,
                                    "field": f"fleet.trucks[{i}].force_end_point.custom_id",
                                    "message": f"Custom point '{fc.force_end_point.custom_id}' has no coordinates or distances defined"
                                })

    # Return 422 if validation failed
    if validation_errors:
        raise HTTPException(
            status_code=422,
            detail={
                "code": ErrorCode.INVALID_INPUT,
                "message": "Request validation failed",
                "details": validation_errors
            }
        )

    try:
        # Parse objective
        try:
            objective = ObjectiveFunction(request.objective)
        except ValueError:
            objective = ObjectiveFunction.BALANCED

        # Parse traffic mode
        traffic_mode = request.traffic_mode if request.traffic_mode in ('normal', 'heavy') else 'normal'

        # Compute effective speed: custom overrides preset
        avg_speed_kmh = request.avg_speed_kmh if request.avg_speed_kmh is not None else (60.0 if traffic_mode == 'heavy' else 80.0)
        avg_speed_kmh = max(20.0, min(120.0, avg_speed_kmh))

        # Cap solver budget server-side: each solve call is capped at 15 s inside
        # VRPSolver, but also cap here so callers can't send arbitrarily large values.
        # With 4-day horizon + re-opt: 4 × (15 + 7) ≈ 88 s total — fits in 120 s frontend timeout.
        max_search_seconds = min(request.max_search_seconds, 15)

        # Convert fleet config to dict format if provided
        fleet_config = None
        if request.fleet and request.fleet.trucks:
            fleet_config = [fc.model_dump() for fc in request.fleet.trucks]

        # Convert rate overrides to dict format if provided
        rate_overrides = None
        if request.rate_overrides:
            rate_overrides = request.rate_overrides.model_dump()

        # Build custom points list for routing (only those with coordinates)
        custom_points_for_routing = None
        if request.custom_points:
            custom_points_for_routing = [
                cp.model_dump() for cp in request.custom_points
                if cp.latitude is not None and cp.longitude is not None
            ]

        service = get_recommendation_service()
        optimal_days_result = None

        if request.optimal_days:
            # ── Optimal-days: enumerate 1–4 days and pick best ───────────────
            _od_logger = logging.getLogger(__name__)
            _od_logger.info("[OptimalDays] Starting search across 1–4 days")
            _min_trial_days = 1

            n_trucks = len(fleet_config) if fleet_config else len(loader.trucks)
            # Budget per trial: 4 trials × 4 days × _trial_seconds must fit in frontend timeout.
            # With 2 trucks the VRP is harder — 3 s/solve: 4×4×3 = 48 s + overhead << 300 s.
            _trial_seconds = 3
            _od_results = []  # (days, rec, score, feasible, delivered, cost)

            for _days in range(_min_trial_days, 5):
                _test_fleet = (
                    [
                        {
                            **tc,
                            "availability_days": _days,
                            **(
                                {
                                    "force_end_day": min(
                                        int((tc or {}).get("force_end_day") or _days),
                                        _days,
                                    )
                                }
                                if (tc or {}).get("force_end_enabled")
                                else {}
                            ),
                        }
                        for tc in fleet_config
                    ]
                    if fleet_config else None
                )
                if _test_fleet:
                    _od_logger.info(
                        "[VALIDATION] optimize mode → availability overridden to %d day(s) for %d truck(s)",
                        _days, len(_test_fleet),
                    )
                _od_logger.debug("[FINAL SANITIZED FLEET_CONFIG] days=%d fleet=%s", _days, _test_fleet)
                try:
                    _trial_rec = service.generate_recommendation(
                        objective=objective,
                        truck_ids=request.truck_ids,
                        site_ids=request.site_ids,
                        max_search_seconds=_trial_seconds,
                        traffic_mode=traffic_mode,
                        avg_speed_kmh=avg_speed_kmh,
                        horizon_days=_days,
                        fleet_config=_test_fleet,
                        rate_overrides=rate_overrides,
                        debug_trace=False,
                        custom_points=custom_points_for_routing,
                        fill_remaining_time=request.fill_remaining_time,
                        allow_transfers=request.allow_transfers,
                        cost_per_km_override=request.cost_per_km_override,
                        max_driver_hours_override=request.max_driver_hours_override,
                        swap_time_min_override=request.swap_time_min_override,
                        max_containers_override=request.max_containers_override,
                        optimize_days_mode=True,
                        auto_restrict_horizon=False,
                        persist_history=False,
                        ai_api_key=request.ai_api_key,
                    )
                    _score = _score_recommendation(_trial_rec, n_trucks, _days)
                except Exception as _exc:
                    _od_logger.warning(f"[OptimalDays] tested_days={_days} exception: {_exc}")
                    _trial_rec = None
                    _score = float("inf")

                _strict_feasible = (
                    _trial_rec is not None
                    and getattr(_trial_rec, "status", "infeasible") != "infeasible"
                    and not _has_unmitigated_critical_risk(_trial_rec)
                )
                _end_load_not_zero = _has_end_load_not_zero(_trial_rec)
                _feasible = (
                    _trial_rec is not None
                    and getattr(_trial_rec, "status", "infeasible") != "infeasible"
                )
                _delivered = sum(r.total_containers_delivered for r in _trial_rec.routes) if _trial_rec else 0
                _cost = _trial_rec.total_cost_eur if _trial_rec else 0.0

                _od_logger.info(
                    f"[OptimalDays] tested_days={_days} score={_score:.1f} "
                    f"cost={_cost:.0f} feasible={_feasible} strict_feasible={_strict_feasible}"
                    f" end_load_not_zero={_end_load_not_zero}"
                )
                _od_results.append((
                    _days,
                    _trial_rec,
                    _score,
                    _strict_feasible,
                    _feasible,
                    _end_load_not_zero,
                    _delivered,
                    _cost,
                ))

            # Selection policy:
            # 1) strict feasible (no unmitigated critical risk),
            # 2) otherwise any feasible,
            # 3) otherwise best available trial by score.
            _strict_clean_list = [
                (d, r, s, dlv, c)
                for d, r, s, strict_f, _f, end_load_nz, dlv, c in _od_results
                if strict_f and not end_load_nz
            ]
            _strict_list = [
                (d, r, s, dlv, c)
                for d, r, s, strict_f, _f, _end_load_nz, dlv, c in _od_results if strict_f
            ]
            _feasible_clean_list = [
                (d, r, s, dlv, c)
                for d, r, s, _strict_f, f, end_load_nz, dlv, c in _od_results
                if f and not end_load_nz
            ]
            _feasible_list = [
                (d, r, s, dlv, c)
                for d, r, s, _strict_f, f, _end_load_nz, dlv, c in _od_results if f
            ]
            _any_scored = [
                (d, r, s, dlv, c)
                for d, r, s, _strict_f, _f, _end_load_nz, dlv, c in _od_results
                if r is not None and s != float("inf")
            ]

            # Selection policy with END_LOAD_NOT_ZERO preference:
            # 1) strict feasible + clean end-load
            # 2) strict feasible (fallback may include end-load residual)
            # 3) feasible + clean end-load
            # 4) any feasible
            # 5) least-bad scored trial
            if _strict_clean_list:
                _best_days, _best_rec, _best_score, _best_delivered, _best_cost = min(
                    # Lower score is better; for identical scores, prefer more
                    # available working days (operator preference for utilization).
                    _strict_clean_list, key=lambda x: (x[2], -x[0])
                )
            elif _strict_list:
                _od_logger.warning(
                    "[OptimalDays] Strict-feasible clean-endload trial not found; "
                    "falling back to strict-feasible with residual end-load warning."
                )
                _best_days, _best_rec, _best_score, _best_delivered, _best_cost = min(
                    _strict_list, key=lambda x: (x[2], -x[0])
                )
            elif _feasible_clean_list:
                _od_logger.warning(
                    "[OptimalDays] No strict-feasible trial found (critical risk remains); "
                    "selecting best feasible clean-endload fallback by score."
                )
                _best_days, _best_rec, _best_score, _best_delivered, _best_cost = min(
                    _feasible_clean_list, key=lambda x: (x[2], -x[0])
                )
            elif _feasible_list:
                _od_logger.warning(
                    "[OptimalDays] No strict-feasible clean trial found (critical risk remains); "
                    "selecting best feasible fallback by score."
                )
                _best_days, _best_rec, _best_score, _best_delivered, _best_cost = min(
                    _feasible_list, key=lambda x: (x[2], -x[0])
                )
            elif _any_scored:
                _od_logger.warning(
                    "[OptimalDays] No feasible trial found; selecting least-bad scored trial."
                )
                _best_days, _best_rec, _best_score, _best_delivered, _best_cost = min(
                    _any_scored, key=lambda x: (x[2], -x[0])
                )
            else:
                # All infeasible — use the 1-day trial result as the canonical fallback.
                # If that trial also produced None (e.g. InfeasibleRoutingError → NO_ACTION),
                # generate a fresh 1-day recommendation so the response is never None.
                _od_logger.warning("[OptimalDays] All trials infeasible — falling back to 1 day")
                _best_days = 1
                _best_rec = next((r for d, r, _, __, ___, ____, _____, ______ in _od_results if d == 1), None)
                if _best_rec is None:
                    _od_logger.warning("[OptimalDays] 1-day trial was None — re-running single solve")
                    try:
                        _best_rec = service.generate_recommendation(
                            objective=objective,
                            truck_ids=request.truck_ids,
                            site_ids=request.site_ids,
                            max_search_seconds=_trial_seconds,
                            traffic_mode=traffic_mode,
                            avg_speed_kmh=avg_speed_kmh,
                            horizon_days=1,
                            fleet_config=fleet_config,
                            rate_overrides=rate_overrides,
                            debug_trace=False,
                            custom_points=custom_points_for_routing,
                            fill_remaining_time=request.fill_remaining_time,
                            allow_transfers=request.allow_transfers,
                            cost_per_km_override=request.cost_per_km_override,
                            max_driver_hours_override=request.max_driver_hours_override,
                            swap_time_min_override=request.swap_time_min_override,
                            max_containers_override=request.max_containers_override,
                            optimize_days_mode=True,
                            auto_restrict_horizon=False,
                            persist_history=False,
                            ai_api_key=request.ai_api_key,
                        )
                    except Exception as _fb_exc:
                        _od_logger.error("[OptimalDays] fallback solve also failed: %s", _fb_exc)
                        _best_rec = None
                # Last resort: return an empty infeasible recommendation rather than
                # None so the caller never receives a 422 from this path.
                if _best_rec is None:
                    from ..models import RecommendationStatus as _RecStatus
                    _best_rec = service._create_empty_recommendation(
                        "No feasible routes found across all horizon trials (1–4 days)."
                    )
                    _best_rec.status = _RecStatus.INFEASIBLE
                    _od_logger.warning(
                        "[OptimalDays] all trials returned None — returning empty infeasible recommendation"
                    )
                _best_delivered = sum(r.total_containers_delivered for r in (_best_rec.routes if _best_rec else []))
                _best_cost = _best_rec.total_cost_eur if _best_rec else 0.0

            _od_logger.info(f"[OptimalDays] selected={_best_days}")

            # Build explanation
            _drive_h = sum(r.total_time_hours for r in (_best_rec.routes if _best_rec else []))
            _days_label = f"{_best_days} day{'s' if _best_days > 1 else ''}"
            _active_days_raw = sorted({
                int(getattr(r, "day_index", 0) or 0)
                for r in (_best_rec.routes if _best_rec else [])
                if getattr(r, "day_index", None) is not None
            })
            # Normalize for display: some pipelines use 0-based day_index.
            _active_days = [d if d >= 1 else d + 1 for d in _active_days_raw]
            _active_days = sorted(set(d for d in _active_days if d >= 1))
            _has_force_end_constraints = any(
                bool((tc or {}).get("force_end_enabled"))
                for tc in (fleet_config or [])
            )
            _details = [
                f"Total driving time: {_drive_h:.1f}h",
                f"Containers delivered: {_best_delivered}",
                f"Total cost: \u20ac{_best_cost:.0f}",
            ]
            if _active_days:
                _active_label = ", ".join(f"D{d}" for d in _active_days)
                _details.append(f"Operational routes appear on: {_active_label}.")
                if _best_days > len(_active_days):
                    if _has_force_end_constraints:
                        _details.append(
                            "Note: extra calendar days come from force-end / positioning constraints, "
                            "not necessarily additional service work."
                        )
                    else:
                        _details.append(
                            "Note: workload is concentrated in fewer active days than the selected horizon."
                        )
            from .schemas import OptimalDaysResult, OptimalDaysTrialResult
            optimal_days_result = OptimalDaysResult(
                days_used=_best_days,
                tested_days=[
                    OptimalDaysTrialResult(
                        days=d,
                        score=round(s, 1) if s != float("inf") else -1.0,
                        feasible=f,
                        total_cost_eur=round(c, 2),
                        containers_delivered=dlv,
                    )
                    for d, _, s, _strict_f, f, _end_load_nz, dlv, c in _od_results
                ],
                explanation={
                    "summary": f"Best feasible horizon: {_days_label}",
                    "details": _details,
                },
            )
            recommendation = _best_rec
            # Trials ran with persist_history=False to avoid history pollution.
            # Persist the winning recommendation now so approve/reject can find it by ID.
            if recommendation is not None:
                service._history.append(recommendation)
                service._save_history()

        else:
            # ── Standard single-run ──────────────────────────────────────────
            recommendation = service.generate_recommendation(
                objective=objective,
                truck_ids=request.truck_ids,
                site_ids=request.site_ids,
                max_search_seconds=max_search_seconds,
                traffic_mode=traffic_mode,
                avg_speed_kmh=avg_speed_kmh,
                horizon_days=request.horizon_days,
                fleet_config=fleet_config,
                rate_overrides=rate_overrides,
                debug_trace=request.debug_trace,
                custom_points=custom_points_for_routing,
                fill_remaining_time=request.fill_remaining_time,
                allow_transfers=request.allow_transfers,
                cost_per_km_override=request.cost_per_km_override,
                max_driver_hours_override=request.max_driver_hours_override,
                swap_time_min_override=request.swap_time_min_override,
                max_containers_override=request.max_containers_override,
                optimize_days_mode=False,
                force_exact_days=request.force_exact_days,
                ai_api_key=request.ai_api_key,
            )

        computation_time = time.time() - start_time
        _last_recommendation_at = datetime.utcnow()

        if recommendation is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "NO_RECOMMENDATION",
                    "message": "Solver returned no recommendation (all scenarios infeasible). Check server logs for [INFEASIBILITY ANALYSIS].",
                    "type": "RoutingError",
                }
            )

        _has_force_end_constraints = any(
            bool((tc or {}).get("force_end_enabled"))
            for tc in (fleet_config or [])
        )
        if (
            recommendation is not None
            and _has_force_end_constraints
            and float(getattr(recommendation, "end_of_horizon_imbalance", 0) or 0) > 0
        ):
            imbalance = int(getattr(recommendation, "end_of_horizon_imbalance", 0) or 0)
            recommendation = service._create_infeasible_recommendation(
                reason_code="FORCE_END_IMBALANCE",
                reason_message=(
                    f"Plan violates force-end closure: {imbalance} container(s) still remain on trucks "
                    "at the end of the horizon. With force_end active, trucks must finish globally balanced."
                ),
                objective=objective,
                horizon_days=request.horizon_days,
            )

        logger.info(f"[/recommend] Success: {len(recommendation.routes)} routes, "
                    f"computation_time={computation_time:.2f}s")

        # Auto-save to analytics DB (non-blocking — errors don't fail the request)
        try:
            _loader = get_data_loader()
            _cfg = _loader.config
            _site_states = _build_analytics_site_states(_loader)
            get_analytics_service().save_recommendation(
                rec=recommendation,
                computation_time=computation_time,
                config_snapshot={
                    "cost_per_km_eur": _cfg.cost_per_km_eur,
                    "handling_fee_eur": _cfg.handling_fee_eur,
                    "contingency_multiplier": _cfg.contingency_multiplier,
                },
                site_states=_site_states,
            )
        except Exception as _ae:
            logger.warning(f"[analytics] Failed to save recommendation: {_ae}")

        return RecommendationResponse(
            recommendation=recommendation,
            computation_time_seconds=computation_time,
            trace=service._last_trace if request.debug_trace else None,
            optimal_days_result=optimal_days_result,
        )

    except HTTPException:
        raise  # Re-raise validation errors as-is

    except ValueError as e:
        # Routing or symmetry errors — return actionable 422
        logger.warning(f"[/recommend] ValueError: {str(e)}")
        raise HTTPException(
            status_code=422,
            detail={
                "code": ErrorCode.INVALID_INPUT,
                "message": str(e),
                "type": "RoutingError",
            }
        )

    except Exception as e:
        _tb = traceback.format_exc()
        logger.error(f"[/recommend] Error: {str(e)}")
        logger.error(f"[/recommend] Traceback:\n{_tb}")
        print("\n[CRASH] Exception in /recommend:")
        traceback.print_exc()
        print("[CRASH] End traceback\n")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "RecommendationError",
                "message": str(e),
                "type": type(e).__name__,
                "traceback": _tb,
            }
        )


@router.post("/recommend_multi", response_model=MultiRecommendationResponse)
async def generate_multi_recommendation(request: MultiRecommendationRequest):
    """Generate route recommendations for all 4 objectives in one call.

    Returns a bundle keyed by objective (cost, time, flaring, balanced),
    each containing the same payload shape as the single /recommend endpoint.
    If any objective fails, the entire request fails (no partial results).
    """
    global _last_recommendation_at
    import traceback
    import logging

    logger = logging.getLogger(__name__)

    logger.info(f"[/recommend_multi] Request: site_ids={request.site_ids}, "
                f"max_search_seconds={request.max_search_seconds}, "
                f"traffic_mode={request.traffic_mode}")
    # Dump full request body for debugging 422s
    try:
        import json as _json
        logger.info(f"[/recommend_multi] Full body: {_json.dumps(request.model_dump(exclude_none=True), indent=2, default=str)}")
    except Exception as dump_err:
        logger.warning(f"[/recommend_multi] Could not dump request: {dump_err}")

    start_time = time.time()
    loader = get_data_loader()

    # --- Input validation (same logic as /recommend minus objective) ---
    validation_errors = []

    def validate_point(point, field_prefix: str, allow_custom: bool = True):
        if point.kind == "site":
            if not point.site_id:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.site_id",
                    "message": "site_id required when kind is 'site'"
                })
            elif point.site_id not in loader.sites:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.site_id",
                    "message": f"Unknown site ID: {point.site_id}"
                })
        elif point.kind == "custom":
            if not allow_custom:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.kind",
                    "message": "Custom points not allowed here"
                })
            elif not point.custom_id:
                validation_errors.append({
                    "code": ErrorCode.INVALID_POINT,
                    "field": f"{field_prefix}.custom_id",
                    "message": "custom_id required when kind is 'custom'"
                })

    custom_points_map: Dict[str, Dict[str, float]] = {}
    if request.custom_points:
        for cp in request.custom_points:
            custom_points_map[cp.id] = cp.distances_to_sites

    def get_distance(from_key: str, to_key: str) -> float:
        if request.manual_distances:
            for md in request.manual_distances:
                if (md.from_key == from_key and md.to_key == to_key) or \
                   (md.from_key == to_key and md.to_key == from_key):
                    return md.distance_km
        if from_key.startswith("site:") and to_key.startswith("site:"):
            from_id = from_key.replace("site:", "")
            to_id = to_key.replace("site:", "")
            if from_id in loader.distance_matrix:
                dist = loader.distance_matrix[from_id].get(to_id)
                if dist is not None:
                    return dist
            if to_id in loader.distance_matrix:
                dist = loader.distance_matrix[to_id].get(from_id)
                if dist is not None:
                    return dist
        if from_key.startswith("custom:") and to_key.startswith("site:"):
            custom_id = from_key.replace("custom:", "")
            site_id = to_key.replace("site:", "")
            if custom_id in custom_points_map:
                dist = custom_points_map[custom_id].get(site_id)
                if dist is not None:
                    return dist
        if from_key.startswith("site:") and to_key.startswith("custom:"):
            site_id = from_key.replace("site:", "")
            custom_id = to_key.replace("custom:", "")
            if custom_id in custom_points_map:
                dist = custom_points_map[custom_id].get(site_id)
                if dist is not None:
                    return dist
        return -1

    MAX_CUSTOM_POINTS = 7
    if request.custom_points and len(request.custom_points) > MAX_CUSTOM_POINTS:
        validation_errors.append({
            "code": ErrorCode.INVALID_INPUT,
            "field": "custom_points",
            "message": f"Maximum {MAX_CUSTOM_POINTS} custom points allowed, got {len(request.custom_points)}"
        })

    if request.custom_points:
        for i, cp in enumerate(request.custom_points):
            has_coords = cp.latitude is not None and cp.longitude is not None
            has_manual = bool(cp.distances_to_sites)
            if not has_coords and not has_manual:
                validation_errors.append({
                    "code": ErrorCode.INVALID_INPUT,
                    "field": f"custom_points[{i}]",
                    "message": f"Custom point '{cp.id}' needs coordinates (latitude/longitude) or manual distances"
                })

    if request.site_ids:
        invalid_sites = [sid for sid in request.site_ids if sid not in loader.sites]
        if invalid_sites:
            validation_errors.append({
                "code": ErrorCode.INVALID_INPUT,
                "field": "site_ids",
                "message": f"Unknown site IDs: {invalid_sites}"
            })

    if request.fleet and request.fleet.trucks:
        for i, fc in enumerate(request.fleet.trucks):
            availability = fc.availability_days
            if fc.truck_id not in loader.trucks:
                validation_errors.append({
                    "code": ErrorCode.INVALID_TRUCK_CONFIG,
                    "field": f"fleet.trucks[{i}].truck_id",
                    "message": f"Unknown truck ID: {fc.truck_id}"
                })
            if fc.start:
                start = fc.start
                if start.kind == "site":
                    if not start.site_id:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start.site_id",
                            "message": "site_id required for site start"
                        })
                    elif start.site_id not in loader.sites:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_POINT,
                            "field": f"fleet.trucks[{i}].start.site_id",
                            "message": f"Unknown site ID: {start.site_id}"
                        })
                elif start.kind == "custom":
                    if not start.custom_id:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start.custom_id",
                            "message": "custom_id required for custom start"
                        })
                    elif start.custom_id not in custom_points_map:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_POINT,
                            "field": f"fleet.trucks[{i}].start.custom_id",
                            "message": f"Custom point '{start.custom_id}' not found in custom_points"
                        })
                    else:
                        cp_obj = next((c for c in request.custom_points if c.id == start.custom_id), None)
                        has_coords = cp_obj and cp_obj.latitude is not None and cp_obj.longitude is not None
                        if not has_coords and not custom_points_map[start.custom_id]:
                            validation_errors.append({
                                "code": ErrorCode.MISSING_DISTANCE,
                                "field": f"fleet.trucks[{i}].start.custom_id",
                                "message": f"Custom point '{start.custom_id}' has no coordinates or distances defined"
                            })
                elif start.kind == "in_transit":
                    if not start.from_point or not start.to_point:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_START_CONFIG,
                            "field": f"fleet.trucks[{i}].start",
                            "message": "from_point and to_point required for in_transit start"
                        })
                    else:
                        validate_point(start.from_point, f"fleet.trucks[{i}].start.from_point")
                        validate_point(start.to_point, f"fleet.trucks[{i}].start.to_point")
                        from_key = f"{start.from_point.kind}:{start.from_point.site_id or start.from_point.custom_id}"
                        to_key = f"{start.to_point.kind}:{start.to_point.site_id or start.to_point.custom_id}"
                        if from_key == to_key:
                            validation_errors.append({
                                "code": ErrorCode.INVALID_INTRANSIT_POSITION,
                                "field": f"fleet.trucks[{i}].start",
                                "message": "from_point and to_point must be different"
                            })
                        if start.total_edge_distance_km is None:
                            dist = get_distance(from_key, to_key)
                            if dist < 0:
                                validation_errors.append({
                                    "code": ErrorCode.MISSING_DISTANCE,
                                    "field": f"fleet.trucks[{i}].start.total_edge_distance_km",
                                    "message": f"Distance not found for {from_key} -> {to_key}. Provide total_edge_distance_km or add to manual_distances."
                                })
                        else:
                            if start.distance_from_from_km is not None:
                                if start.distance_from_from_km > start.total_edge_distance_km:
                                    validation_errors.append({
                                        "code": ErrorCode.INVALID_INTRANSIT_POSITION,
                                        "field": f"fleet.trucks[{i}].start.distance_from_from_km",
                                        "message": f"distance_from_from_km ({start.distance_from_from_km}) exceeds total_edge_distance_km ({start.total_edge_distance_km})"
                                    })

            if fc.force_end_enabled:
                if not request.optimal_days:
                    if fc.force_end_day is None:
                        validation_errors.append({
                            "code": ErrorCode.MISSING_FORCE_END_CONFIG,
                            "field": f"fleet.trucks[{i}].force_end_day",
                            "message": "force_end_day required when force_end_enabled is true"
                        })
                    elif fc.force_end_day > availability:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_TRUCK_CONFIG,
                            "field": f"fleet.trucks[{i}].force_end_day",
                            "message": f"force_end_day ({fc.force_end_day}) exceeds availability_days ({availability})"
                        })
                    elif fc.force_end_day > request.horizon_days:
                        validation_errors.append({
                            "code": ErrorCode.INVALID_TRUCK_CONFIG,
                            "field": f"fleet.trucks[{i}].force_end_day",
                            "message": f"force_end_day ({fc.force_end_day}) exceeds horizon_days ({request.horizon_days})"
                        })
                if not fc.force_end_point:
                    validation_errors.append({
                        "code": ErrorCode.MISSING_FORCE_END_CONFIG,
                        "field": f"fleet.trucks[{i}].force_end_point",
                        "message": "force_end_point required when force_end_enabled is true"
                    })
                else:
                    validate_point(fc.force_end_point, f"fleet.trucks[{i}].force_end_point")
                    if fc.force_end_point.kind == "custom" and fc.force_end_point.custom_id:
                        if fc.force_end_point.custom_id not in custom_points_map:
                            validation_errors.append({
                                "code": ErrorCode.INVALID_POINT,
                                "field": f"fleet.trucks[{i}].force_end_point.custom_id",
                                "message": f"Custom point '{fc.force_end_point.custom_id}' not found in custom_points"
                            })
                        else:
                            fe_cp = next((c for c in request.custom_points if c.id == fc.force_end_point.custom_id), None)
                            fe_has_coords = fe_cp and fe_cp.latitude is not None and fe_cp.longitude is not None
                            if not fe_has_coords and not custom_points_map[fc.force_end_point.custom_id]:
                                validation_errors.append({
                                    "code": ErrorCode.MISSING_DISTANCE,
                                    "field": f"fleet.trucks[{i}].force_end_point.custom_id",
                                    "message": f"Custom point '{fc.force_end_point.custom_id}' has no coordinates or distances defined"
                                })

    if validation_errors:
        logger.error(f"[/recommend_multi] Custom validation failed: {validation_errors}")
        raise HTTPException(
            status_code=422,
            detail={
                "code": ErrorCode.INVALID_INPUT,
                "message": "Request validation failed",
                "details": validation_errors
            }
        )

    # --- Generate all objectives ---
    try:
        traffic_mode = request.traffic_mode if request.traffic_mode in ('normal', 'heavy') else 'normal'

        # Compute effective speed: custom overrides preset
        avg_speed_kmh = request.avg_speed_kmh if request.avg_speed_kmh is not None else (60.0 if traffic_mode == 'heavy' else 80.0)
        avg_speed_kmh = max(20.0, min(120.0, avg_speed_kmh))

        fleet_config = None
        if request.fleet and request.fleet.trucks:
            fleet_config = [fc.model_dump() for fc in request.fleet.trucks]

        rate_overrides = None
        if request.rate_overrides:
            rate_overrides = request.rate_overrides.model_dump()

        custom_points_for_routing = None
        if request.custom_points:
            custom_points_for_routing = [
                cp.model_dump() for cp in request.custom_points
                if cp.latitude is not None and cp.longitude is not None
            ]

        service = get_recommendation_service()
        all_recs = service.generate_all_objectives(
            truck_ids=request.truck_ids,
            site_ids=request.site_ids,
            max_search_seconds=request.max_search_seconds,
            traffic_mode=traffic_mode,
            avg_speed_kmh=avg_speed_kmh,
            horizon_days=request.horizon_days,
            fleet_config=fleet_config,
            rate_overrides=rate_overrides,
            debug_trace=request.debug_trace,
            custom_points=custom_points_for_routing,
        )

        computation_time = time.time() - start_time
        _last_recommendation_at = datetime.utcnow()

        # Build response bundle
        recommendations_bundle = {}
        for obj_key, rec in all_recs.items():
            recommendations_bundle[obj_key] = RecommendationResponse(
                recommendation=rec,
                computation_time_seconds=computation_time,
                trace=service._last_trace if request.debug_trace else None,
            )

        logger.info(f"[/recommend_multi] Success: {len(all_recs)} objectives, "
                    f"computation_time={computation_time:.2f}s")

        return MultiRecommendationResponse(
            recommendations=recommendations_bundle,
            computation_time_seconds=computation_time,
        )

    except HTTPException:
        raise

    except ValueError as e:
        logger.warning(f"[/recommend_multi] ValueError: {str(e)}")
        raise HTTPException(
            status_code=422,
            detail={
                "code": ErrorCode.INVALID_INPUT,
                "message": str(e),
                "type": "RoutingError",
            }
        )

    except Exception as e:
        logger.error(f"[/recommend_multi] Error: {str(e)}")
        logger.error(f"[/recommend_multi] Traceback:\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "RecommendationError",
                "message": str(e),
                "type": type(e).__name__
            }
        )


@router.post("/approve/{recommendation_id}", response_model=ApprovalResponse)
async def approve_recommendation(recommendation_id: str, body: ApproveRequest = ApproveRequest()):
    """Approve a recommendation, apply it to canonical state, and optionally replan."""
    service = get_recommendation_service()
    loader = get_data_loader()

    # Find the recommendation
    rec = None
    for r in service._history:
        if r.id == recommendation_id:
            rec = r
            break

    if not rec:
        raise HTTPException(
            status_code=404,
            detail=f"Recommendation not found: {recommendation_id}"
        )

    if rec.status == RecommendationStatus.EXECUTED:
        raise HTTPException(status_code=409, detail="Recommendation already applied")

    # Apply swap operations to canonical site bay pressures.
    # Pass save_to_json as callback so sites are persisted BEFORE history
    # (history write can trigger uvicorn --reload, losing in-memory bay mutations).
    service.apply_recommendation(rec, save_sites_fn=loader.save_to_json)
    try:
        get_analytics_service().record_operator_action(
            recommendation_id,
            action=rec.status.value,
            action_at=datetime.utcnow(),
            source="approve_endpoint",
        )
    except Exception as _ae:
        logger.warning(f"[analytics] Failed to record operator action for approval: {_ae}")

    if body.next_steps:
        next_horizon_days = body.horizon_days or rec.horizon_days

        # Prefer fleet config supplied by the frontend (current UI state) over the
        # stale config stored in the original recommendation.  Fallback to the stored
        # config so that callers that don't send a fleet still get the original constraints.
        raw_fleet_source = (
            [fc.model_dump() for fc in body.fleet.trucks]
            if body.fleet and body.fleet.trucks
            else rec.fleet_config
        )

        next_fleet_config = None
        if raw_fleet_source:
            next_fleet_config = []
            for fc in raw_fleet_source:
                updated_fc = dict(fc)
                updated_fc["availability_days"] = next_horizon_days
                # Clamp force_end_day to the new horizon so the solver never
                # gets an impossible constraint (force_end on day N > horizon).
                if updated_fc.get("force_end_enabled") and updated_fc.get("force_end_day") is not None:
                    updated_fc["force_end_day"] = min(
                        int(updated_fc["force_end_day"]), next_horizon_days
                    )
                next_fleet_config.append(updated_fc)

        new_rec = service.generate_recommendation(
            objective=ObjectiveFunction(rec.objective_function),
            horizon_days=next_horizon_days,
            fleet_config=next_fleet_config,
            max_search_seconds=10,
        )
        return ApprovalResponse(
            success=True,
            recommendation_id=recommendation_id,
            new_status=rec.status.value,
            message="Plan applied and new recommendation generated",
            new_recommendation=new_rec,
        )

    return ApprovalResponse(
        success=True,
        recommendation_id=recommendation_id,
        new_status=rec.status.value,
        message="Plan applied",
    )


@router.post("/reject/{recommendation_id}", response_model=ApprovalResponse)
async def reject_recommendation(recommendation_id: str):
    """Reject a recommendation."""
    service = get_recommendation_service()
    recommendation = service.reject_recommendation(recommendation_id)

    if not recommendation:
        # Unknown ID — treat as a no-op so the frontend state machine can move on
        return JSONResponse(status_code=200, content={"status": "ignored", "recommendation_id": recommendation_id})

    try:
        get_analytics_service().record_operator_action(
            recommendation_id,
            action=recommendation.status.value,
            action_at=datetime.utcnow(),
            source="reject_endpoint",
        )
    except Exception as _ae:
        logger.warning(f"[analytics] Failed to record operator action for rejection: {_ae}")

    return ApprovalResponse(
        success=True,
        recommendation_id=recommendation_id,
        new_status=recommendation.status.value,
        message="Recommendation rejected",
    )


@router.get("/history", response_model=HistoryResponse)
async def get_recommendation_history(
    limit: int = Query(10, ge=1, le=100, description="Max items to return"),
):
    """Get recommendation history."""
    service = get_recommendation_service()
    history = service.get_history()

    # Return most recent first
    history = list(reversed(history))[:limit]

    return HistoryResponse(
        recommendations=history,
        total=len(history),
    )


# ============== Export & Analytics ==============

@router.get("/export/{recommendation_id}")
async def export_recommendation_excel(recommendation_id: str):
    """Export a recommendation as a multi-sheet Excel file (.xlsx)."""
    from ..models import Recommendation as _RecModel

    service = get_recommendation_service()
    rec = None

    # Search in-memory history first
    for r in service.get_history():
        if r.id == recommendation_id:
            rec = r
            break

    # Fallback: read directly from JSON file (handles post-reset state loss)
    if rec is None and RECOMMENDATIONS_FILE.exists():
        try:
            with open(RECOMMENDATIONS_FILE, "r") as _f:
                _data = json.load(_f)
            for _rd in _data.get("recommendations", []):
                if _rd.get("id") == recommendation_id:
                    rec = _RecModel.model_validate(_rd)
                    break
        except Exception as _fe:
            logger.warning(f"[export] JSON fallback failed: {_fe}")

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation '{recommendation_id}' not found")

    # Retrieve computation time from analytics DB if available
    computation_time: Optional[float] = None
    try:
        stored = get_analytics_service().get_recommendation(recommendation_id)
        if stored:
            computation_time = stored.get("computation_time_seconds")
    except Exception:
        pass

    xlsx_bytes = await run_in_threadpool(
        build_recommendation_excel, rec, computation_time
    )

    filename = f"GASUM_Plan_{recommendation_id}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.post("/export/{recommendation_id}")
async def save_recommendation_to_db(recommendation_id: str):
    """
    Save full recommendation detail to the analytics database.
    Called by the Export button in the UI — no file is downloaded.
    Stores route stops, container moves (with serial numbers), site snapshots with truck assignments.
    """
    from ..models import Recommendation as _RecModel

    service = get_recommendation_service()
    rec = None

    for r in service.get_history():
        if r.id == recommendation_id:
            rec = r
            break

    if rec is None and RECOMMENDATIONS_FILE.exists():
        try:
            with open(RECOMMENDATIONS_FILE, "r") as _f:
                _data = json.load(_f)
            for _rd in _data.get("recommendations", []):
                if _rd.get("id") == recommendation_id:
                    rec = _RecModel.model_validate(_rd)
                    break
        except Exception as _fe:
            logger.warning(f"[export-save] JSON fallback failed: {_fe}")

    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recommendation '{recommendation_id}' not found")

    loader = get_data_loader()
    site_states = _build_analytics_site_states(loader)

    # Build bay_id → serial_number lookup: {site_id: {bay_id: serial}}
    bay_serials: Dict[str, Dict[str, str]] = {}
    for site in loader.sites.values():
        bay_serials[site.id] = {
            bay.bay_id: bay.serial_number
            for bay in site.bays
            if bay.serial_number
        }

    try:
        await run_in_threadpool(
            get_analytics_service().save_recommendation_detail,
            rec,
            site_states,
            bay_serials,
        )
    except Exception as _e:
        logger.error(f"[export-save] detail save failed: {_e}")
        raise HTTPException(status_code=500, detail=f"Failed to save detail: {_e}")

    return {
        "ok": True,
        "recommendation_id": recommendation_id,
        "route_stops_saved": sum(len(r.stops) for r in rec.routes),
        "container_moves_saved": sum(
            len((s.swap_operation.containers_dropped or []) + (s.swap_operation.containers_picked or []))
            for r in rec.routes for s in r.stops if s.swap_operation
        ),
        "sites_snapshot_saved": len(site_states),
    }


@router.get("/analytics/export")
async def export_analytics_excel():
    """Export full analytics history (all recommendations + manual ops) as Excel."""
    xlsx_bytes = await run_in_threadpool(
        get_analytics_service().export_analytics_excel
    )
    filename = f"GASUM_Analytics_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@router.post("/analytics/manual-operation")
async def add_manual_operation(body: Dict[str, Any] = Body(...)):
    """
    Save a manual real-life operation to the analytics database.

    Body fields:
      description   (required) — what happened
      operation_date            — ISO date string, defaults to today
      action_taken              — what you did
      outcome                   — result
      sites_involved            — list of site IDs
      cost_eur                  — float, optional
      distance_km               — float, optional
      notes                     — free text
    """
    if not body.get("description"):
        raise HTTPException(status_code=422, detail="'description' is required")

    row_id = get_analytics_service().add_manual_operation(
        description=body["description"],
        operation_date=body.get("operation_date"),
        action_taken=body.get("action_taken"),
        outcome=body.get("outcome"),
        sites_involved=body.get("sites_involved"),
        cost_eur=body.get("cost_eur"),
        distance_km=body.get("distance_km"),
        notes=body.get("notes"),
    )
    return {"status": "saved", "id": row_id}


@router.get("/analytics/manual-operations")
async def list_manual_operations():
    """List all manual operations."""
    ops = get_analytics_service().list_manual_operations()
    return {"operations": ops, "total": len(ops)}


# ============== Configuration ==============

@router.get("/config", response_model=ConfigResponse)
async def get_config():
    """Get current operational configuration."""
    loader = get_data_loader()
    return ConfigResponse(config=loader.config)


@router.get("/distances", response_model=DistanceMatrixResponse)
async def get_distance_matrix():
    """Get the distance matrix between sites."""
    loader = get_data_loader()
    return DistanceMatrixResponse(
        matrix=loader.distance_matrix,
        site_count=len(loader.sites),
    )


@router.get("/distance", response_model=DistanceQueryResponse)
async def get_distance(
    from_site: str = Query(..., alias="from", description="Origin site ID"),
    to_site: str = Query(..., alias="to", description="Destination site ID"),
):
    """Get distance and drive time between two sites.

    This endpoint is useful for the distance inspector UI panel.
    """
    loader = get_data_loader()

    # Validate site IDs
    if from_site not in loader.sites:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_INPUT", "message": f"Unknown origin site: {from_site}"}
        )
    if to_site not in loader.sites:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_INPUT", "message": f"Unknown destination site: {to_site}"}
        )

    # Same site = 0 distance
    if from_site == to_site:
        return DistanceQueryResponse(
            from_site_id=from_site,
            to_site_id=to_site,
            distance_km=0.0,
            drive_time_hours=0.0,
        )

    # Try road routing first (real road distance + duration)
    from .routing_routes import get_routing_service
    routing = get_routing_service()
    if routing:
        from_s = loader.sites[from_site]
        to_s = loader.sites[to_site]
        result = routing.route(from_s.latitude, from_s.longitude, to_s.latitude, to_s.longitude)
        return DistanceQueryResponse(
            from_site_id=from_site,
            to_site_id=to_site,
            distance_km=round(result.distance_km, 3),
            drive_time_hours=round(result.duration_min / 60.0, 4),
        )

    # Fallback: static distance matrix
    distance_km = 0.0
    if from_site in loader.distance_matrix:
        distance_km = loader.distance_matrix[from_site].get(to_site, 0.0)
    elif to_site in loader.distance_matrix:
        distance_km = loader.distance_matrix[to_site].get(from_site, 0.0)

    # Calculate drive time using config
    drive_time_hours = None
    if distance_km > 0 and loader.config.avg_speed_kmph > 0:
        drive_time_hours = distance_km / loader.config.avg_speed_kmph

    return DistanceQueryResponse(
        from_site_id=from_site,
        to_site_id=to_site,
        distance_km=distance_km,
        drive_time_hours=drive_time_hours,
    )


# ============== Scenario Generator ==============

@router.post("/generate_scenario", response_model=ScenarioResponse)
async def generate_scenario(request: ScenarioRequest):
    """Apply a test scenario to site bay pressures and (for capacity_crisis) truck capacities.

    Bay pressures are derived from hours_to_critical logic — NOT random values.
    The canonical site state is overwritten and persisted.
    Time evolution is NOT triggered.
    Returns the updated site list and a tier-map for debugging.
    """
    if request.scenario_type not in SCENARIO_DEFINITIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown scenario type '{request.scenario_type}'. "
                f"Valid types: {list(SCENARIO_DEFINITIONS.keys())}"
            ),
        )

    loader = get_data_loader()
    # Freeze baseline before first simulation mutation so operator can restart later.
    loader.ensure_simulation_baseline()

    # Apply scenario — mutates loader.sites and loader.trucks in-place.
    # No time evolution, no recommendation side effects.
    applied_tiers = apply_scenario(
        sites=loader.sites,
        trucks=loader.trucks,
        config=loader.config,
        scenario_type=request.scenario_type,
        seed=request.seed,
    )

    # Persist updated site/container state only. Scenario-side truck changes
    # (for example capacity_crisis) must remain temporary and in-memory.
    loader.save_to_json(include_trucks=False)

    # Invalidate the cached recommendation service so next generate() sees
    # the new bay pressures (risk assessment depends on current state).
    reset_recommendation_service()

    return ScenarioResponse(
        success=True,
        scenario_type=request.scenario_type,
        description=SCENARIO_DEFINITIONS[request.scenario_type],
        sites_updated=len(applied_tiers),
        applied_tiers=applied_tiers,
    )


@router.post("/advance_time", response_model=AdvanceTimeResponse)
async def advance_time(request: AdvanceTimeRequest):
    """Move canonical site state by signed hours (advance or rewind)."""
    loader = get_data_loader()
    service = get_recommendation_service()
    # Freeze baseline before first simulation mutation so operator can restart later.
    loader.ensure_simulation_baseline()

    # Apply deterministic time evolution to canonical mutable state.
    print(f"[CLOCK] delta_hours={request.hours}")
    service._apply_time_evolution(delta_time_hours=request.hours)

    # Persist updated bay pressures.
    loader.save_to_json()

    # Return fresh risk summary for UI confirmation.
    assessments = service.risk_calculator.assess_all_sites(loader.sites)
    critical_count = sum(1 for a in assessments if a.risk_level == RiskLevel.CRITICAL)
    warning_count = sum(1 for a in assessments if a.risk_level == RiskLevel.WARNING)

    _action = "advanced" if request.hours > 0 else "rewound"
    return AdvanceTimeResponse(
        success=True,
        advanced_hours=request.hours,
        message=f"System state {_action} by {abs(request.hours):.1f}h",
        total_sites=len(loader.sites),
        critical_count=critical_count,
        warning_count=warning_count,
    )


@router.post("/simulation/restart", response_model=RestartSimulationResponse)
async def restart_simulation():
    """Restore canonical state to the baseline captured before simulation mutations."""
    loader = get_data_loader()
    restored = loader.restore_simulation_baseline()

    if restored:
        # Ensure recommendation service uses the restored state.
        reset_recommendation_service()
        service = get_recommendation_service()
    else:
        # Fallback: no baseline yet — treat current state as baseline and keep state unchanged.
        loader.ensure_simulation_baseline()
        service = get_recommendation_service()

    assessments = service.risk_calculator.assess_all_sites(loader.sites)
    critical_count = sum(1 for a in assessments if a.risk_level == RiskLevel.CRITICAL)
    warning_count = sum(1 for a in assessments if a.risk_level == RiskLevel.WARNING)

    return RestartSimulationResponse(
        success=True,
        restored=restored,
        message="Simulation state restored to baseline" if restored else "No baseline found; current state saved as baseline",
        total_sites=len(loader.sites),
        critical_count=critical_count,
        warning_count=warning_count,
    )


# ============== AI Chatbot ==============

import os
from pydantic import BaseModel as _BaseModel

class ChatMessage(_BaseModel):
    role: str
    content: str

class ChatRequest(_BaseModel):
    messages: list[ChatMessage]
    system: str
    api_key: str | None = None

class ChatResponse(_BaseModel):
    content: str

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Proxy chat messages to DeepSeek API.

    Uses the request API key when provided, otherwise falls back to the
    server-side DEEPSEEK_API_KEY environment variable.
    """
    api_key = (request.api_key or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="DeepSeek API key missing. Add one in the chat UI or configure DEEPSEEK_API_KEY on the server.",
        )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "max_tokens": 1024,
                    "messages": [
                        {"role": "system", "content": request.system},
                        *[{"role": m.role, "content": m.content} for m in request.messages],
                    ],
                },
            )

        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Invalid DeepSeek API key")
        if response.status_code == 429:
            raise HTTPException(status_code=429, detail="DeepSeek rate limit reached — try again shortly")
        if response.status_code >= 400:
            detail = "DeepSeek API error"
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message") or payload.get("detail") or detail
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=detail)

        payload = response.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        return ChatResponse(content=content or "")
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="DeepSeek API timed out")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"DeepSeek API connection error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepSeek API error: {str(e)}")
