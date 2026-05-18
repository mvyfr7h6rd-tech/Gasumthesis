"""Truck/vehicle data model for container transport."""

from enum import Enum
from typing import List, Optional, Union, Literal
from pydantic import BaseModel, Field, computed_field, model_validator


class Carrier(str, Enum):
    """Transport carrier company."""
    SPEED = "SPEED"
    SYRIAPALO = "SYRIAPALO"


# Cost per km by carrier
CARRIER_COSTS = {
    Carrier.SPEED: 2.25,
    Carrier.SYRIAPALO: 1.95,
}


class SiteLocation(BaseModel):
    """Start location specified by site ID."""
    kind: Literal["site"] = "site"
    site_id: str = Field(..., description="Site ID where truck starts")


class CoordinateLocation(BaseModel):
    """Start location specified by coordinates."""
    kind: Literal["coord"] = "coord"
    lat: float = Field(..., ge=-90, le=90, description="Latitude")
    lon: float = Field(..., ge=-180, le=180, description="Longitude")


class ForceEnd(BaseModel):
    """Constraint to force truck to end at specific location on specific day."""
    day_index: int = Field(..., ge=1, description="Day index (1-based) when truck must end at site")
    site_id: str = Field(..., description="Site ID where truck must end")


class Truck(BaseModel):
    """
    Represents a truck that transports biogas containers.

    Each truck has:
    - A home depot where it starts/ends routes (legacy) or flexible start location
    - A capacity (1-3 containers typically)
    - A carrier (determines cost per km)
    - Current location and load state
    - Optional force end constraint for multi-day planning
    """
    id: str = Field(..., description="Unique truck identifier")
    carrier: Carrier = Field(
        default=Carrier.SPEED, description="Transport carrier company"
    )
    capacity: int = Field(
        default=2, ge=1, le=5, description="Maximum container capacity"
    )
    home_site_id: str = Field(
        ..., description="Depot site ID where truck starts/ends routes (legacy, used if start not specified)"
    )

    # Flexible start location (overrides home_site_id if specified)
    start: Optional[Union[SiteLocation, CoordinateLocation]] = Field(
        default=None,
        description="Flexible start location: site ID or coordinates. If None, uses home_site_id."
    )

    # Force end constraint for multi-day planning
    force_end: Optional[ForceEnd] = Field(
        default=None,
        description="Constraint to force truck to end at specific site on specific day"
    )

    current_location_site_id: str = Field(
        default="", description="Current location site ID"
    )
    current_load: List[str] = Field(
        default_factory=list, description="List of container IDs currently on truck"
    )
    initial_load: int = Field(
        default=0, ge=0, le=3,
        description="Number of containers pre-loaded at start of planning horizon (set from fleet config)"
    )

    # Set to True when the stored start.site_id was not found in the routing matrix
    # and was automatically corrected to home_site_id on load.
    start_resolved: bool = Field(
        default=False,
        description="True if the truck's stored start location was invalid and fell back to home_site_id"
    )

    @computed_field
    @property
    def cost_per_km(self) -> float:
        """Get cost per km based on carrier."""
        return CARRIER_COSTS.get(self.carrier, 2.25)

    @computed_field
    @property
    def effective_start_site_id(self) -> Optional[str]:
        """Get effective start site ID (from start or home_site_id)."""
        if self.start is None:
            return self.home_site_id
        if isinstance(self.start, SiteLocation):
            return self.start.site_id
        return None  # Coordinate-based start has no site ID

    @computed_field
    @property
    def has_coordinate_start(self) -> bool:
        """Check if truck has coordinate-based start location."""
        return self.start is not None and isinstance(self.start, CoordinateLocation)

    @computed_field
    @property
    def current_load_count(self) -> int:
        """Number of containers currently loaded."""
        return len(self.current_load)

    @computed_field
    @property
    def available_capacity(self) -> int:
        """Number of additional containers that can be loaded."""
        return self.capacity - len(self.current_load)

    @computed_field
    @property
    def is_full(self) -> bool:
        """Check if truck is at full capacity."""
        return len(self.current_load) >= self.capacity

    @computed_field
    @property
    def is_empty(self) -> bool:
        """Check if truck has no containers."""
        return len(self.current_load) == 0

    def can_load(self, count: int = 1) -> bool:
        """Check if truck can load additional containers."""
        return len(self.current_load) + count <= self.capacity

    def can_unload(self, count: int = 1) -> bool:
        """Check if truck has containers to unload."""
        return len(self.current_load) >= count

    def load_container(self, container_id: str) -> bool:
        """Load a container onto the truck. Returns False if at capacity."""
        if self.is_full:
            return False
        self.current_load.append(container_id)
        return True

    def unload_container(self, container_id: str) -> bool:
        """Unload a container from the truck. Returns False if not found."""
        if container_id not in self.current_load:
            return False
        self.current_load.remove(container_id)
        return True

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "TRUCK001",
                "carrier": "SPEED",
                "capacity": 2,
                "home_site_id": "helsinki_malmi",
                "current_location_site_id": "helsinki_malmi",
                "current_load": [],
            }
        }
    }
