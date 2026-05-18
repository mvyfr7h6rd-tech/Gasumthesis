"""API routes for road-network routing."""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services.routing_service import RoutingService, RoutingConfig

logger = logging.getLogger(__name__)

routing_router = APIRouter(prefix="/routing", tags=["routing"])

# Singleton routing service (lazy init)
_routing_service: Optional[RoutingService] = None


def get_routing_service() -> Optional[RoutingService]:
    """Get or create routing service. Returns None if GraphHopper unavailable."""
    global _routing_service
    if _routing_service is None:
        try:
            config = RoutingConfig.from_env()
            service = RoutingService(config)
            if service.health_check():
                _routing_service = service
                logger.info("GraphHopper routing service initialized at %s", config.graphhopper_url)
            else:
                logger.warning("GraphHopper not reachable at %s", config.graphhopper_url)
        except Exception as e:
            logger.warning("Could not initialize routing service: %s", e)
    return _routing_service


# --- Response schemas ---

class RouteQueryResponse(BaseModel):
    distance_km: float
    duration_min: float
    geometry: Optional[str] = None


class MatrixDestination(BaseModel):
    id: str
    lat: float
    lon: float


class MatrixOrigin(BaseModel):
    lat: float
    lon: float


class MatrixRequestBody(BaseModel):
    """Request body for POST /routing/matrix."""
    origin: MatrixOrigin = Field(..., alias="from")
    to: List[MatrixDestination]
    profile: Optional[str] = "car"

    model_config = {"populate_by_name": True}


class MatrixResultEntry(BaseModel):
    to_id: str
    distance_km: float
    duration_min: float


class MatrixResponse(BaseModel):
    origin: MatrixOrigin = Field(..., alias="from")
    results: List[MatrixResultEntry]

    model_config = {"populate_by_name": True}


class RoutingHealthResponse(BaseModel):
    status: str
    engine: str


# --- Endpoints ---

@routing_router.get("/health", response_model=RoutingHealthResponse)
async def routing_health():
    """Check if the routing engine is available."""
    svc = get_routing_service()
    if svc and svc.health_check():
        return RoutingHealthResponse(status="ok", engine="graphhopper")
    return RoutingHealthResponse(status="unavailable", engine="graphhopper")


@routing_router.get("/route", response_model=RouteQueryResponse)
async def get_road_route(
    from_coords: str = Query(..., alias="from", description="Origin as lat,lon"),
    to_coords: str = Query(..., alias="to", description="Destination as lat,lon"),
):
    """
    Get a single road route between two coordinates.

    Useful for debugging and inspection.
    """
    svc = get_routing_service()
    if not svc:
        raise HTTPException(503, detail="Routing service not available. Is GraphHopper running?")

    try:
        from_lat, from_lon = (float(x.strip()) for x in from_coords.split(","))
        to_lat, to_lon = (float(x.strip()) for x in to_coords.split(","))
    except (ValueError, TypeError):
        raise HTTPException(400, detail="Invalid coordinate format. Use: lat,lon")

    result = svc.route(from_lat, from_lon, to_lat, to_lon)
    return RouteQueryResponse(
        distance_km=round(result.distance_km, 3),
        duration_min=round(result.duration_min, 2),
        geometry=result.geometry,
    )


@routing_router.post("/matrix", response_model=MatrixResponse)
async def compute_road_matrix(body: MatrixRequestBody):
    """
    Compute road distances from one origin to multiple destinations.

    Used for custom-point distance precomputation and solver warm-up.
    """
    svc = get_routing_service()
    if not svc:
        raise HTTPException(503, detail="Routing service not available. Is GraphHopper running?")

    results: List[MatrixResultEntry] = []
    for dest in body.to:
        r = svc.route(body.origin.lat, body.origin.lon, dest.lat, dest.lon)
        results.append(MatrixResultEntry(
            to_id=dest.id,
            distance_km=round(r.distance_km, 3),
            duration_min=round(r.duration_min, 2),
        ))

    return MatrixResponse(**{"from": body.origin, "results": results})
