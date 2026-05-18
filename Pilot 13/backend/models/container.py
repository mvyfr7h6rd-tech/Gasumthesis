"""Container data model for biogas containers with continuous fill tracking."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, computed_field


class ContainerStatus(str, Enum):
    """Current status of a container."""
    AT_SITE = "at_site"
    IN_TRANSIT = "in_transit"


class Container(BaseModel):
    """
    Represents a biogas container with continuous fill level tracking.

    Containers hold compressed biogas and are transported between
    production plants and consumption stations. Fill level is tracked
    via pressure (bar), energy content (MWh), and mass (kg).
    """
    id: str = Field(..., description="Unique container identifier (e.g., CNG0006A)")
    location_site_id: str = Field(
        default="", description="ID of site where container is located"
    )
    location_bay: str = Field(
        default="", description="Bay number at the site (e.g., Bay1, Bay2)"
    )
    pressure_bar: float = Field(
        default=0.0, ge=0, le=300, description="Current pressure in bar"
    )
    mwh: float = Field(
        default=0.0, ge=0, description="Energy content in MWh"
    )
    kg: float = Field(
        default=0.0, ge=0, description="Mass content in kg"
    )
    max_pressure_bar: float = Field(
        default=250.0, ge=0, description="Maximum pressure capacity in bar"
    )
    status: ContainerStatus = Field(
        default=ContainerStatus.AT_SITE, description="Current status"
    )

    @computed_field
    @property
    def fill_percentage(self) -> float:
        """Calculate fill level as percentage of max pressure."""
        if self.max_pressure_bar == 0:
            return 0.0
        return (self.pressure_bar / self.max_pressure_bar) * 100

    @computed_field
    @property
    def is_full(self) -> bool:
        """Consider container full if fill percentage >= 80%."""
        return self.fill_percentage >= 80.0

    @computed_field
    @property
    def is_empty(self) -> bool:
        """Consider container empty if fill percentage <= 20%."""
        return self.fill_percentage <= 20.0

    @computed_field
    @property
    def is_partial(self) -> bool:
        """Container has partial fill (between empty and full thresholds)."""
        return not self.is_full and not self.is_empty

    @computed_field
    @property
    def fill_category(self) -> str:
        """Get human-readable fill category."""
        if self.is_full:
            return "full"
        elif self.is_empty:
            return "empty"
        else:
            return "partial"

    def set_in_transit(self) -> None:
        """Mark container as in transit."""
        self.status = ContainerStatus.IN_TRANSIT
        self.location_site_id = ""
        self.location_bay = ""

    def set_at_site(self, site_id: str, bay: str = "") -> None:
        """Mark container as arrived at a site."""
        self.status = ContainerStatus.AT_SITE
        self.location_site_id = site_id
        self.location_bay = bay

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "CNG0006A",
                "location_site_id": "oulu_rusko",
                "location_bay": "Bay5",
                "pressure_bar": 200,
                "mwh": 20,
                "kg": 1315.79,
                "max_pressure_bar": 250,
                "status": "at_site",
            }
        }
    }
