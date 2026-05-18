"""Risk calculation service for site criticality assessment."""

from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum

from ..models import Site, Container, OperationalConfig
from ..utils.conversions import pressure_to_kg, get_normalized_kg, BASE_PRESSURE_BAR, effective_pressure_bar


class RiskLevel(str, Enum):
    """Risk level categories."""
    CRITICAL = "critical"   # < critical_hours_threshold (10h)
    WARNING = "warning"     # < warning_hours_threshold (24h)
    NORMAL = "normal"       # < safe_hours_threshold (48h)
    SAFE = "safe"           # ≥ safe_hours_threshold (48h)


@dataclass
class SiteRiskAssessment:
    """Risk assessment for a single site."""
    site_id: str
    site_name: str
    site_type: str
    risk_level: RiskLevel
    hours_to_critical: float
    total_kg: float
    usable_kg: float  # kg above usable_floor_bar (accessible gas)
    total_mwh: float
    bays_fixed: int
    utilization_percentage: float
    consumption_rate_kg_hour: float
    production_rate_kg_hour: float
    flaring_cost_eur_mwh: float
    flaring_loss_eur_per_h: Optional[float]
    risk_score: float  # 0-100, higher = more urgent
    explanation: str


class RiskCalculator:
    """
    Service for calculating site risk levels and time-to-critical.

    For consumer sites (traffic/industry):
    - Risk is stockout (running out of gas)
    - Time to critical = total_kg / consumption rate

    For producer sites:
    - Risk is flaring (having to flare excess gas)
    - Time to critical = remaining_capacity_kg / production rate
    - Producer is "full" when min(bay.pressure_bar) >= 250
    """

    # Max kg per bay at 250 bar (calculated from pressure_to_kg(250))
    MAX_KG_PER_BAY = 2829.0

    def __init__(self, config: OperationalConfig):
        self.config = config

    # ── Forecast helpers ──────────────────────────────────────────────────────
    # Project the current stock / remaining capacity forward by `horizon_hours`
    # of autonomous flow (consumption or production) to compute a forecast HTC.
    #
    # Why forecast instead of current state?
    #   The planning horizon for a dispatch decision is typically 24 h.  A site
    #   with HTC = 30 h looks NORMAL today, but in 24 h it will have only 6 h of
    #   supply left — squarely CRITICAL.  Forecasting surfaces that urgency now,
    #   when dispatch can still prevent the stockout.
    #
    # Formula (both cases reduce to the same form):
    #   forecast_htc = max(0,  current_stock − rate × horizon)  /  rate
    #                = max(0,  current_htc − horizon_hours)
    #
    # `horizon_hours` is wired to config.warning_hours_threshold (24 h) so
    # operators who change the warning window automatically get a matching
    # forecast horizon without touching this code.

    @staticmethod
    def _forecast_consumer_htc(
        usable_kg: float,
        consumption_rate_kg_h: float,
        horizon_hours: float,
    ) -> float:
        """Return consumer HTC projected forward by horizon_hours of consumption.

        Equivalent to max(0, current_htc - horizon_hours) but expressed in
        stock-projection form for clarity.

        Args:
            usable_kg: Current usable gas above the operational floor (kg).
            consumption_rate_kg_h: Site consumption rate (kg/h).  Must be > 0.
            horizon_hours: Planning horizon to project over (h).

        Returns:
            Forecast HTC in hours (≥ 0).  0 means site will have no usable gas
            left within `horizon_hours` and is already effectively critical.
        """
        future_kg = max(0.0, usable_kg - consumption_rate_kg_h * horizon_hours)
        return future_kg / consumption_rate_kg_h

    @staticmethod
    def _forecast_producer_htc(
        remaining_capacity_kg: float,
        production_rate_kg_h: float,
        horizon_hours: float,
    ) -> float:
        """Return producer HTC projected forward by horizon_hours of production.

        A producer that currently has 30 h until it must flare will have only
        6 h of headroom in 24 h — this surfaces that urgency immediately.

        Args:
            remaining_capacity_kg: Current free capacity above the usable floor (kg).
            production_rate_kg_h: Site production rate (kg/h).  Must be > 0.
            horizon_hours: Planning horizon to project over (h).

        Returns:
            Forecast HTC in hours (≥ 0).
        """
        future_remaining = max(0.0, remaining_capacity_kg - production_rate_kg_h * horizon_hours)
        return future_remaining / production_rate_kg_h

    def assess_site(
        self,
        site: Site,
        containers: List[Container] = None,
        horizon_hours: Optional[float] = None,
    ) -> SiteRiskAssessment:
        """
        Calculate risk assessment for a single site.

        Args:
            site: The site to assess
            containers: Optional list of containers (ignored in new model, kept for compatibility)
            horizon_hours: Planning window for forecast HTC (h).
                None → defaults to config.warning_hours_threshold (24 h).
                Pass horizon_days * 24 from the recommendation layer for a
                horizon-aware assessment that surfaces urgency over the full
                planning window rather than just the next 24 h.

        Returns:
            SiteRiskAssessment with risk level and time to critical

        Note:
            Uses normalized pressure model where 20 bar = 0 (empty).
            For consumers: usable_kg = kg_above_20_bar
            For producers: remaining capacity calculated relative to effective range
        """
        # Default to current-state HTC (no forecast) unless caller explicitly
        # requests a forecast horizon.
        _horizon = horizon_hours if horizon_hours is not None else 0.0

        # Calculate usable kg (above config.usable_floor_bar, default 20 bar).
        # Gas at or below the floor is inaccessible and contributes 0 usable kg.
        # effective_pressure_bar enforces the floor semantics before normalization.
        floor = self.config.usable_floor_bar
        usable_kg = sum(get_normalized_kg(effective_pressure_bar(bay.pressure_bar), floor) for bay in site.bays)

        # Calculate time to critical based on site type
        if site.is_consumer:
            hours_to_critical, explanation = self._assess_consumer_risk(site, usable_kg, _horizon)
        else:
            hours_to_critical, explanation = self._assess_producer_risk(site, usable_kg, _horizon)

        # Determine risk level
        risk_level = self._get_risk_level(hours_to_critical)

        # Calculate risk score (0-100, higher = more urgent)
        risk_score = self._calculate_risk_score(hours_to_critical, site)

        # Get production rate for producers
        production_rate = 0.0
        if site.is_producer and site.production and site.production.effective_kg_per_h:
            production_rate = site.production.effective_kg_per_h

        return SiteRiskAssessment(
            site_id=site.id,
            site_name=site.name,
            site_type=site.site_type.value,
            risk_level=risk_level,
            hours_to_critical=hours_to_critical,
            total_kg=site.total_kg,
            usable_kg=usable_kg,
            total_mwh=site.total_mwh,
            bays_fixed=site.bays_fixed,
            utilization_percentage=site.utilization_percentage,
            consumption_rate_kg_hour=site.consumption_rate_kg_hour,
            production_rate_kg_hour=production_rate,
            flaring_cost_eur_mwh=site.flaring_cost_eur_mwh,
            flaring_loss_eur_per_h=site.flaring_loss_eur_per_h,
            risk_score=risk_score,
            explanation=explanation,
        )

    def _assess_consumer_risk(
        self,
        site: Site,
        usable_kg: float,
        horizon_hours: float,
    ) -> tuple[float, str]:
        """
        Assess stockout risk for a consumer site using forecast HTC.

        Args:
            site: Consumer site to assess
            usable_kg: Usable gas in kg (above 20 bar base pressure)
            horizon_hours: Planning window to project forward (h).  Supplied by
                assess_site() — callers control the horizon, not this method.

        Returns: (hours_to_critical, explanation)

        Note:
            Uses normalized pressure model: 20 bar is considered "empty".
            HTC is projected forward by horizon_hours:
              forecast_htc = max(0, current_htc - horizon_hours)
            A site with 30 h of supply and a 24 h horizon will have only 6 h
            of headroom left — surfaces as WARNING now rather than tomorrow.
        """
        consumption_rate = site.consumption_rate_kg_hour

        if consumption_rate <= 0:
            return 99999.0, "No consumption rate defined - cannot estimate risk"

        if usable_kg <= 0:
            return 0.0, f"STOCKOUT: No usable gas at {site.name} (at or below 20 bar base)"

        hours = self._forecast_consumer_htc(
            usable_kg,
            consumption_rate,
            horizon_hours,
        )

        # Convert usable_kg to approximate MWh for display
        usable_mwh = (usable_kg / 1000) * 15.2

        if hours < self.config.critical_hours_threshold:
            explanation = (
                f"CRITICAL: {site.name} will reach minimum pressure in {hours:.1f} hours. "
                f"Usable: {usable_kg:.0f} kg ({usable_mwh:.1f} MWh above 20 bar base), "
                f"Consumption: {consumption_rate:.0f} kg/h"
            )
        elif hours < self.config.warning_hours_threshold:
            explanation = (
                f"WARNING: {site.name} has {hours:.1f} hours of usable supply. "
                f"Usable: {usable_kg:.0f} kg ({usable_mwh:.1f} MWh above 20 bar base), "
                f"Consumption: {consumption_rate:.0f} kg/h"
            )
        else:
            explanation = (
                f"{site.name} has {hours:.1f} hours of usable supply. "
                f"Usable: {usable_mwh:.1f} MWh across {site.bays_fixed} bays (above 20 bar base)"
            )

        return hours, explanation

    def _assess_producer_risk(
        self,
        site: Site,
        usable_kg: float,
        horizon_hours: float,
    ) -> tuple[float, str]:
        """
        Assess flaring risk for a producer site using forecast HTC.

        Args:
            site: Producer site to assess
            usable_kg: Current usable gas in kg (above 20 bar base pressure)
            horizon_hours: Planning window to project forward (h).  Supplied by
                assess_site() — callers control the horizon, not this method.

        Returns: (hours_to_critical, explanation)

        Note:
            Uses normalized pressure model: effective capacity is 20-250 bar range.
            Maximum usable kg per bay = kg(250) - kg(20) = 2829 - 131.6 = 2697.4 kg
            HTC is projected forward by horizon_hours:
              forecast_htc = max(0, current_htc - horizon_hours)
        """
        # Get production rate from ProductionRates object
        if site.production is None or site.production.effective_kg_per_h is None:
            return 99999.0, "No production rate defined - cannot estimate risk"

        production_rate = site.production.effective_kg_per_h

        # Check if already full (min pressure >= 250)
        if site.is_full:
            flaring_msg = ""
            if site.flaring_loss_eur_per_h is not None:
                flaring_msg = f" Flaring loss: {site.flaring_loss_eur_per_h:.2f} EUR/h"
            return 0.0, (
                f"FLARING: {site.name} is at full pressure!{flaring_msg}"
            )

        # Calculate remaining usable capacity in kg
        # Max usable per bay = kg(250) - kg(floor), e.g. 2829 - 131.6 = 2697.4 at floor=20
        kg_at_base = pressure_to_kg(self.config.usable_floor_bar)
        max_usable_kg_per_bay = self.MAX_KG_PER_BAY - kg_at_base
        max_usable_capacity_kg = site.bays_fixed * max_usable_kg_per_bay
        remaining_kg = max_usable_capacity_kg - usable_kg
        hours = self._forecast_producer_htc(
            remaining_kg,
            production_rate,
            horizon_hours,
        )

        # Convert to MWh for display
        remaining_mwh = (remaining_kg / 1000) * 15.2

        if hours < self.config.critical_hours_threshold:
            explanation = (
                f"CRITICAL: {site.name} will need flaring in {hours:.1f} hours. "
                f"Remaining capacity: {remaining_kg:.0f} kg ({remaining_mwh:.1f} MWh), "
                f"Production: {production_rate:.0f} kg/h. "
                f"Flaring cost: {site.flaring_cost_eur_mwh} EUR/MWh"
            )
        elif hours < self.config.warning_hours_threshold:
            explanation = (
                f"WARNING: {site.name} has {hours:.1f} hours until flaring. "
                f"Remaining capacity: {remaining_kg:.0f} kg ({remaining_mwh:.1f} MWh), "
                f"Production: {production_rate:.0f} kg/h"
            )
        else:
            explanation = (
                f"{site.name} has {hours:.1f} hours of capacity remaining. "
                f"Remaining: {remaining_mwh:.1f} MWh, Avg pressure: {site.avg_pressure:.0f} bar"
            )

        return hours, explanation

    def _get_risk_level(self, hours_to_critical: float) -> RiskLevel:
        """Determine risk level from hours to critical.

        CRITICAL: < critical_hours_threshold (10h)
        WARNING:  < warning_hours_threshold (24h)
        NORMAL:   < safe_hours_threshold (48h)
        SAFE:     ≥ safe_hours_threshold (48h)
        """
        if hours_to_critical <= self.config.critical_hours_threshold:
            return RiskLevel.CRITICAL
        elif hours_to_critical <= self.config.warning_hours_threshold:
            return RiskLevel.WARNING
        elif hours_to_critical <= self.config.safe_hours_threshold:
            return RiskLevel.NORMAL
        else:
            return RiskLevel.SAFE

    def _calculate_risk_score(self, hours_to_critical: float, site: Site) -> float:
        """
        Calculate risk score from 0-100.

        Higher score = more urgent attention needed.
        """
        if hours_to_critical <= 0:
            return 100.0

        # Handle very large values (no defined rate)
        if hours_to_critical >= 99999 or hours_to_critical >= self.config.safe_hours_threshold:
            return 0.0

        # Base score from time (inverse relationship).
        # Uses safe_hours_threshold (48h) as the upper bound so the gradient is
        # smooth across the full CRITICAL→WARNING→NORMAL range:
        #   0h  → 80 pts,  24h → 40 pts,  48h → 0 pts
        # Previously used warning_hours_threshold (24h), which cliffed all
        # NORMAL-zone sites (24–48h) to 0 — making them indistinguishable.
        time_score = max(0, 80 - (hours_to_critical / self.config.safe_hours_threshold * 80))

        # Bonus for flaring cost (producers only)
        flaring_bonus = 0
        if site.is_producer and site.flaring_cost_eur_mwh > 0:
            # Normalize flaring cost (assume 150 EUR/MWh is max)
            flaring_bonus = min(20, (site.flaring_cost_eur_mwh / 150) * 20)

        return min(100, time_score + flaring_bonus)

    def assess_all_sites(
        self,
        sites: Dict[str, Site],
        containers: List[Container] = None,
        horizon_hours: Optional[float] = None,
    ) -> List[SiteRiskAssessment]:
        """
        Assess risk for all sites.

        Args:
            sites: Dictionary of site_id -> Site
            containers: Optional list of all containers (ignored in new model)
            horizon_hours: Planning window for forecast HTC (h).
                None → defaults to config.warning_hours_threshold (24 h).
                Pass horizon_days * 24 from the recommendation layer so the
                VRP-feeding htc_map reflects the full planning horizon rather
                than a fixed 24 h window.  All other callers (API display,
                diagnostics, "tomorrow" snapshots) should leave this as None.

        Returns:
            List of SiteRiskAssessment sorted by risk score (highest first)
        """
        assessments = []
        for site in sites.values():
            assessment = self.assess_site(site, horizon_hours=horizon_hours)
            assessments.append(assessment)

        # Sort by risk score descending
        assessments.sort(key=lambda a: a.risk_score, reverse=True)
        return assessments

    def get_critical_sites(
        self,
        sites: Dict[str, Site],
        containers: List[Container] = None,
    ) -> List[SiteRiskAssessment]:
        """Get only sites with CRITICAL risk level."""
        all_assessments = self.assess_all_sites(sites, containers)
        return [a for a in all_assessments if a.risk_level == RiskLevel.CRITICAL]

    def get_sites_needing_service(
        self,
        sites: Dict[str, Site],
        containers: List[Container] = None,
        max_hours: float = None,
    ) -> List[str]:
        """
        Get list of site IDs that need service within time window.

        Args:
            sites: Dictionary of sites
            containers: Optional container list (ignored in new model)
            max_hours: Maximum hours to critical (default: warning threshold)

        Returns:
            List of site IDs needing service, ordered by urgency
        """
        if max_hours is None:
            max_hours = self.config.warning_hours_threshold

        all_assessments = self.assess_all_sites(sites, containers)
        needing_service = [
            a.site_id for a in all_assessments
            if a.hours_to_critical <= max_hours
        ]
        return needing_service

    def calculate_risk_reduction(
        self,
        site: Site,
        pressure_delta: int = 0,
    ) -> float:
        """
        Calculate risk score change from a pressure change.

        Args:
            site: The site to assess
            pressure_delta: Change in average pressure across bays

        Returns positive value if risk is reduced, negative if increased.
        """
        # Assess current risk
        current_assessment = self.assess_site(site)
        current_score = current_assessment.risk_score

        # Create hypothetical site with updated pressure
        hypothetical_site = site.model_copy(deep=True)
        for bay in hypothetical_site.bays:
            new_pressure = bay.pressure_bar + pressure_delta
            bay.pressure_bar = max(0, min(250, new_pressure))

        # Assess new risk
        new_assessment = self.assess_site(hypothetical_site)
        new_score = new_assessment.risk_score

        # Return reduction (positive = good)
        return current_score - new_score
