"""Operational configuration parameters."""

from pydantic import BaseModel, Field


class OperationalConfig(BaseModel):
    """
    Operational parameters for the logistics system.

    These values control constraints, costs, and calculations
    throughout the system.
    """
    # Driving parameters
    avg_speed_kmph: float = Field(
        default=80.0, gt=0, description="Average driving speed in km/h"
    )
    max_driver_hours: float = Field(
        default=9.0, gt=0, description="Maximum driver shift in hours"
    )

    # Service time
    swap_time_hours: float = Field(
        default=0.333, gt=0, description="Time per swap operation in hours (20 min default)"
    )

    # Costs
    cost_per_km_eur: float = Field(
        default=2.25, ge=0, description="Default transport cost per km in EUR"
    )
    handling_fee_eur: float = Field(
        default=40.0, ge=0, description="Fee per stop/swap in EUR"
    )
    contingency_multiplier: float = Field(
        default=1.10, ge=1.0, description="Multiplier for cost contingency (1.10 = +10%)"
    )

    # Conversion factors
    kg_to_mwh_factor: float = Field(
        default=0.0152, gt=0, description="Conversion factor from kg to MWh"
    )
    bar_to_kg_factor: float = Field(
        default=6.579, gt=0, description="Approximate conversion from bar to kg for 250bar max"
    )

    # Risk thresholds
    critical_hours_threshold: float = Field(
        default=10.0, gt=0, description="Hours until critical state triggers alert (< 10h)"
    )
    warning_hours_threshold: float = Field(
        default=24.0, gt=0, description="Hours until warning state triggers alert (< 24h)"
    )
    safe_hours_threshold: float = Field(
        default=48.0, gt=0, description="Hours above which site is considered safe (> 48h)"
    )

    # Minimum billed distance per route
    min_billed_km: float = Field(
        default=50.0, ge=0, description="Minimum billed distance per route in km. Routes shorter than this are charged as if this distance was driven."
    )

    # Fill level thresholds
    full_threshold_percent: float = Field(
        default=80.0, ge=0, le=100, description="Percentage above which container is 'full'"
    )
    empty_threshold_percent: float = Field(
        default=20.0, ge=0, le=100, description="Percentage below which container is 'empty'"
    )

    # Container balance constraints for plants
    min_containers_at_plant: int = Field(
        default=1, ge=0, description="Minimum containers that must remain at each production plant"
    )

    # Urgency-aware arc cost tuning
    urgency_factor_m: int = Field(
        default=0, ge=0,
        description=(
            "Legacy urgency discount applied to arc costs. "
            "When > 0: adjusted_cost(from→to) = monetary_cost_cents − risk_score[to] × urgency_factor_m. "
            "Default 0 disables the discount so arc costs are pure monetary transport cost, "
            "preserving metric structure for the distance matrix. "
            "Urgency priority is encoded via tiered disjunction penalties instead. "
            "Set > 0 only for sensitivity experiments comparing discount vs. no-discount behaviour."
        ),
    )

    # Usable gas floor — gas at or below this pressure is inaccessible to consumers/logistics
    usable_floor_bar: int = Field(
        default=20, ge=0, le=250,
        description="Minimum operational pressure (bar). Gas at or below this level is not usable. "
                    "All risk / time-to-critical calculations use kg strictly above this threshold."
    )

    # Fill-site disjunction penalty (soft preference over idle time)
    fill_site_penalty_eur: float = Field(
        default=400.0, ge=0,
        description="Penalty in EUR for not visiting a fill site when time allows. 400 EUR = 40 000 cents."
    )

    def calculate_travel_time_hours(self, distance_km: float) -> float:
        """Calculate travel time for a given distance."""
        if self.avg_speed_kmph <= 0:
            return 0.0
        return distance_km / self.avg_speed_kmph

    def calculate_transport_cost(self, distance_km: float, cost_per_km: float = None) -> float:
        """Calculate transport cost for a given distance.

        Applies minimum billed distance: if route < min_billed_km, charges min_billed_km.
        """
        rate = cost_per_km if cost_per_km is not None else self.cost_per_km_eur
        billed_km = max(distance_km, self.min_billed_km)
        return billed_km * rate

    def calculate_total_route_cost(
        self,
        distance_km: float,
        num_stops: int,
        cost_per_km: float = None,
        include_contingency: bool = True
    ) -> float:
        """Calculate total route cost including handling fees and contingency."""
        transport = self.calculate_transport_cost(distance_km, cost_per_km)
        handling = num_stops * self.handling_fee_eur
        total = transport + handling
        if include_contingency:
            total *= self.contingency_multiplier
        return total

    def kg_to_mwh(self, kg: float) -> float:
        """Convert kilograms to megawatt-hours."""
        return kg * self.kg_to_mwh_factor

    def mwh_to_kg(self, mwh: float) -> float:
        """Convert megawatt-hours to kilograms."""
        if self.kg_to_mwh_factor <= 0:
            return 0.0
        return mwh / self.kg_to_mwh_factor

    model_config = {
        "json_schema_extra": {
            "example": {
                "avg_speed_kmph": 80,
                "max_driver_hours": 9,
                "swap_time_hours": 0.333,
                "cost_per_km_eur": 2.25,
                "handling_fee_eur": 40,
                "contingency_multiplier": 1.10,
            }
        }
    }
