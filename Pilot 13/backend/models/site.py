"""Site data model for production plants and consumer stations."""

import re
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, computed_field, field_validator

from ..utils.conversions import pressure_to_kg, kg_to_mwh, effective_pressure_bar


class SiteType(str, Enum):
    """Type of site in the biogas network."""
    PRODUCTION = "production"
    TRAFFIC = "traffic"
    INDUSTRY = "industry"


class ProductionRateMode(str, Enum):
    """Mode for production rate specification."""
    KG_PER_H = "KG_PER_H"
    MWH_PER_H = "MWH_PER_H"
    UNKNOWN = "UNKNOWN"


class Bay(BaseModel):
    """Represents a single bay at a site with its pressure reading."""
    bay_id: str = Field(..., description="Bay identifier (e.g., 'Bay1', 'Bay2')")
    pressure_bar: int = Field(default=0, ge=0, le=250, description="Current pressure in bar (0-250)")
    serial_number: Optional[str] = Field(
        default=None,
        description="4-digit physical container serial number"
    )

    @field_validator('serial_number')
    @classmethod
    def validate_serial_number(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r'^\d{3,4}$', v):
            raise ValueError('serial_number must be exactly 3 or 4 digits')
        return v

    @computed_field
    @property
    def kg(self) -> float:
        """Convert pressure to kg using piecewise linear formula.

        Uses effective_pressure_bar so that gas below the operational floor
        (20 bar) is treated as zero — consistent with risk and demand calculations.
        Physical pressure may be non-zero below 20 bar, but is operationally zero.
        """
        return pressure_to_kg(effective_pressure_bar(self.pressure_bar))

    @computed_field
    @property
    def mwh(self) -> float:
        """Convert kg to MWh."""
        return kg_to_mwh(self.kg)


class ProductionRates(BaseModel):
    """Production rates for producer sites."""
    mode: ProductionRateMode = Field(default=ProductionRateMode.UNKNOWN)
    kg_per_h: Optional[float] = Field(default=None, ge=0)
    mwh_per_h: Optional[float] = Field(default=None, ge=0)

    @computed_field
    @property
    def effective_kg_per_h(self) -> Optional[float]:
        """Get kg/h, calculating from MWh if needed."""
        if self.mode == ProductionRateMode.KG_PER_H and self.kg_per_h is not None:
            return self.kg_per_h
        elif self.mode == ProductionRateMode.MWH_PER_H and self.mwh_per_h is not None:
            return (self.mwh_per_h / 15.2) * 1000
        return None

    @computed_field
    @property
    def effective_mwh_per_h(self) -> Optional[float]:
        """Get MWh/h, calculating from kg if needed."""
        if self.mode == ProductionRateMode.MWH_PER_H and self.mwh_per_h is not None:
            return self.mwh_per_h
        elif self.mode == ProductionRateMode.KG_PER_H and self.kg_per_h is not None:
            return (self.kg_per_h / 1000) * 15.2
        return None


class Site(BaseModel):
    """
    Represents a location in the biogas logistics network.

    Sites can be:
    - Production plants: Generate biogas, fill containers
    - Traffic stations: Consume biogas for vehicle refueling
    - Industry stations: Consume biogas for industrial use
    """
    id: str = Field(..., description="Unique site identifier")
    name: str = Field(..., description="Human-readable site name")
    site_type: SiteType = Field(..., description="Type of site")
    location: str = Field(default="", description="Address or location description")
    latitude: float = Field(..., ge=-90, le=90, description="Latitude coordinate")
    longitude: float = Field(..., ge=-180, le=180, description="Longitude coordinate")

    # Fixed bay count (immutable)
    bays_fixed: int = Field(..., ge=0, description="Fixed number of bays (immutable)")

    # Bay pressure readings (pressure is editable)
    bays: List[Bay] = Field(default_factory=list, description="Array of bay pressure readings")

    # Consumption rate (for traffic/industry sites)
    consumption_rate_kg_hour: float = Field(
        default=0.0, ge=0, description="Consumption rate in kg/hour"
    )

    # Production rates (for production sites only)
    production: Optional[ProductionRates] = Field(
        default=None, description="Production rates (producers only)"
    )

    # Flaring cost (only production sites typically have this)
    flaring_cost_eur_mwh: float = Field(
        default=0.0, ge=0, description="Cost of flaring excess gas in EUR/MWh"
    )

    # Swap designation: only sites explicitly marked as swap points allow container swaps
    swap_allowed: bool = Field(
        default=True,
        description="Whether container swaps are permitted at this site. "
                    "Set to False to prevent illegal swaps at non-designated locations."
    )

    @computed_field
    @property
    def total_kg(self) -> float:
        """Sum of kg across all bays."""
        return sum(bay.kg for bay in self.bays)

    @computed_field
    @property
    def total_mwh(self) -> float:
        """Sum of MWh across all bays."""
        return sum(bay.mwh for bay in self.bays)

    @computed_field
    @property
    def is_producer(self) -> bool:
        """Check if site produces biogas."""
        return self.site_type == SiteType.PRODUCTION

    @computed_field
    @property
    def is_consumer(self) -> bool:
        """Check if site consumes biogas."""
        return self.site_type in (SiteType.TRAFFIC, SiteType.INDUSTRY)

    @computed_field
    @property
    def is_full(self) -> bool:
        """Producer is full when min(bay.pressure_bar) >= 250."""
        if not self.is_producer or not self.bays:
            return False
        return min(bay.pressure_bar for bay in self.bays) >= 250

    @computed_field
    @property
    def min_pressure(self) -> int:
        """Minimum pressure across all bays."""
        if not self.bays:
            return 0
        return min(bay.pressure_bar for bay in self.bays)

    @computed_field
    @property
    def max_pressure(self) -> int:
        """Maximum pressure across all bays."""
        if not self.bays:
            return 0
        return max(bay.pressure_bar for bay in self.bays)

    @computed_field
    @property
    def avg_pressure(self) -> float:
        """Average pressure across all bays."""
        if not self.bays:
            return 0.0
        return sum(bay.pressure_bar for bay in self.bays) / len(self.bays)

    @computed_field
    @property
    def utilization_percentage(self) -> float:
        """Current capacity utilization as percentage based on average pressure."""
        if not self.bays:
            return 0.0
        return (self.avg_pressure / 250) * 100

    @computed_field
    @property
    def flaring_loss_eur_per_h(self) -> Optional[float]:
        """Calculate flaring loss if producer is full with known production."""
        if not self.is_producer or not self.is_full:
            return None
        if self.production and self.production.effective_mwh_per_h is not None:
            return self.production.effective_mwh_per_h * self.flaring_cost_eur_mwh
        return None

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "tampere",
                "name": "Tampere",
                "site_type": "traffic",
                "location": "Gasum Truck Pirkkala, Teollisuustie 2, 33960 Pirkkala, Finland",
                "latitude": 61.4978,
                "longitude": 23.7610,
                "bays_fixed": 2,
                "bays": [
                    {"bay_id": "Bay1", "pressure_bar": 200},
                    {"bay_id": "Bay2", "pressure_bar": 180}
                ],
                "consumption_rate_kg_hour": 92.5,
            }
        }
    }
