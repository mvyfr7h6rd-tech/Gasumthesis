"""Scenario generator for testing the GASUM DSS.

Generates test scenarios by setting bay pressures derived from
hours_to_critical logic (NOT random pressure values).

Variability is controlled via an optional seed:
  - seed=None  → random.Random() with OS entropy (different each call)
  - seed=N     → random.Random(N) — fully reproducible

Randomisation affects only WHICH sites are assigned to each tier (shuffle).
Pressure is always computed from physics: usable_kg = rate × hours → pressure.

All mutations are applied in-place to the passed sites dict and trucks dict.
No time evolution is triggered. Isolated from solver/recommendation logic.
"""

import logging
import random
from typing import Dict, List, Optional, Tuple

from ..models import Site, Truck
from ..models.config import OperationalConfig
from ..utils.conversions import pressure_to_kg, kg_to_pressure

logger = logging.getLogger(__name__)

# Maximum kg a single bay can hold (at 250 bar)
_MAX_KG_PER_BAY: float = pressure_to_kg(250)  # 2829.0 kg

# Human-readable descriptions for each scenario type
SCENARIO_DEFINITIONS: Dict[str, str] = {
    "balanced": "10% critical (6–12 h), 20% warning (24–36 h), 70% normal (48–72 h). "
                "Applied to both consumers and producers.",
    "high_demand": "40% of consumers set to critical (4–12 h). Producers left at normal (48–72 h). "
                   "Models a day with high consumer demand.",
    "high_production": "40% of producers set near overflow (4–12 h to full). Consumers at normal. "
                       "Models peak gas production with insufficient pickup.",
    "capacity_crisis": "Balanced site pressures (same as 'balanced') but all truck "
                       "capacities temporarily reduced to 1 container.",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pick_hours(tier: str, rng: random.Random) -> float:
    """Pick hours within a tier's range using the provided RNG.

    With a seeded RNG this is reproducible; with an unseeded RNG it varies
    each call — but pressure is always derived physically from the result.
    """
    ranges: Dict[str, Tuple[float, float]] = {
        "critical_consumer": (4.0, 12.0),
        "critical_standard": (6.0, 12.0),
        "warning": (24.0, 36.0),
        "normal": (48.0, 72.0),
        "near_overflow": (4.0, 12.0),
    }
    lo, hi = ranges.get(tier, (48.0, 72.0))
    return rng.uniform(lo, hi)


def _allocate_usable_sequential(
    usable_total_kg: float,
    max_usable_per_bay: float,
    num_bays: int,
    fill_from_start: bool,
) -> List[float]:
    """Split total usable kg across bays using a sequential (one-at-a-time) model.

    fill_from_start=True:
      Bay1 fills first, then Bay2, ... (producer behavior).
    fill_from_start=False:
      Last bay retains gas first (consumer behavior after sequential drain).
    """
    usable_total_kg = max(0.0, usable_total_kg)
    per_bay = [0.0] * num_bays
    order = range(num_bays) if fill_from_start else range(num_bays - 1, -1, -1)

    remaining = usable_total_kg
    for idx in order:
        if remaining <= 0.0:
            break
        assigned = min(max_usable_per_bay, remaining)
        per_bay[idx] = assigned
        remaining -= assigned

    return per_bay


def _consumer_pressures(hours: float, site: Site, floor_bar: int) -> List[int]:
    """Per-bay pressures so a consumer has exactly `hours` hours_to_critical.

    Derivation:
        hours_to_critical = usable_kg / consumption_rate
        usable_kg         = consumption_rate * hours
        total_kg_per_bay  = usable_kg / num_bays + kg_at_floor
        pressure_bar      = kg_to_pressure(total_kg_per_bay)
    """
    rate = site.consumption_rate_kg_hour
    if rate <= 0 or not site.bays:
        return [floor_bar for _ in site.bays]  # no rate defined — leave at floor

    num_bays = len(site.bays)
    usable_total_kg = rate * hours
    kg_at_floor = pressure_to_kg(floor_bar)
    max_kg_per_bay = _MAX_KG_PER_BAY
    max_usable_per_bay = max_kg_per_bay - kg_at_floor

    # Consumers drain one bay at a time: older bays tend to be emptier while
    # later bays remain fuller.
    usable_by_bay = _allocate_usable_sequential(
        usable_total_kg=min(usable_total_kg, max_usable_per_bay * num_bays),
        max_usable_per_bay=max_usable_per_bay,
        num_bays=num_bays,
        fill_from_start=False,
    )

    out: List[int] = []
    for usable_kg in usable_by_bay:
        total_kg = kg_at_floor + usable_kg
        bar = round(kg_to_pressure(total_kg))
        out.append(max(floor_bar, min(250, bar)))
    return out


def _producer_pressures(hours: float, site: Site, floor_bar: int) -> List[int]:
    """Per-bay pressures so a producer has exactly `hours` hours_to_critical (until overflow).

    Derivation:
        remaining_capacity_kg  = production_rate * hours          (total for site)
        max_usable_total       = num_bays * (MAX_KG - kg_at_floor)
        usable_total           = max_usable_total - remaining_capacity_kg
        usable_per_bay         = usable_total / num_bays
        total_kg_per_bay       = usable_per_bay + kg_at_floor
        pressure_bar           = kg_to_pressure(total_kg_per_bay)
    """
    if site.production is None or not site.bays:
        return [200 for _ in site.bays]  # default mid-range if no production data

    rate = site.production.effective_kg_per_h
    if rate is None or rate <= 0:
        return [200 for _ in site.bays]

    num_bays = len(site.bays)
    kg_at_floor = pressure_to_kg(floor_bar)
    max_usable_per_bay = _MAX_KG_PER_BAY - kg_at_floor
    max_usable_total = num_bays * max_usable_per_bay

    remaining_capacity_kg = rate * hours
    usable_total = max(0.0, max_usable_total - remaining_capacity_kg)

    # Producers fill one bay at a time: earlier bays become fuller first.
    usable_by_bay = _allocate_usable_sequential(
        usable_total_kg=usable_total,
        max_usable_per_bay=max_usable_per_bay,
        num_bays=num_bays,
        fill_from_start=True,
    )

    out: List[int] = []
    for usable_kg in usable_by_bay:
        total_kg = kg_at_floor + usable_kg
        bar = round(kg_to_pressure(total_kg))
        out.append(max(floor_bar, min(250, bar)))
    return out


def _set_site_pressures(site: Site, pressures_bar: List[int]) -> None:
    """Set site bay pressures from an ordered pressure list."""
    for bay, pressure in zip(site.bays, pressures_bar):
        bay.pressure_bar = pressure


# ── Main entry point ──────────────────────────────────────────────────────────

def apply_scenario(
    sites: Dict[str, Site],
    trucks: Dict[str, Truck],
    config: OperationalConfig,
    scenario_type: str,
    seed: Optional[int] = None,
) -> Dict[str, str]:
    """Apply a test scenario by mutating bay pressures (and optionally truck capacities).

    Args:
        sites: The canonical site dict (mutated in-place).
        trucks: The canonical truck dict (mutated in-place for capacity_crisis only).
        config: Operational config for floor_bar.
        scenario_type: One of the keys in SCENARIO_DEFINITIONS.
        seed: Optional RNG seed.
              If provided, the scenario is fully reproducible (same seed → same result).
              If None, OS entropy is used — a different set of sites gets each tier
              every call while pressure values remain physically consistent.

    Returns:
        Dict mapping site_id → tier label for logging / debug response.

    Raises:
        ValueError: If scenario_type is not recognised.
    """
    if scenario_type not in SCENARIO_DEFINITIONS:
        valid = list(SCENARIO_DEFINITIONS.keys())
        raise ValueError(f"Unknown scenario type '{scenario_type}'. Valid types: {valid}")

    rng = random.Random(seed)  # seeded → reproducible; seed=None → OS entropy
    floor = config.usable_floor_bar
    applied: Dict[str, str] = {}

    # Sort by site_id first for a stable base, then shuffle with the RNG.
    # Shuffling determines WHICH sites fall into each tier; pressure is still
    # derived from physics (rate × hours → kg → bar).
    sorted_ids = sorted(sites.keys())
    consumers = [sid for sid in sorted_ids if sites[sid].is_consumer]
    producers = [sid for sid in sorted_ids if sites[sid].is_producer]
    rng.shuffle(consumers)
    rng.shuffle(producers)

    logger.info(
        "[scenario] Applying '%s' (seed=%s): %d consumers, %d producers",
        scenario_type, seed, len(consumers), len(producers),
    )

    # ── balanced / capacity_crisis share the same site-pressure logic ──────────
    if scenario_type in ("balanced", "capacity_crisis"):
        # Combine shuffled consumers + producers into one list for tier assignment
        all_shuffled = consumers + producers
        n = len(all_shuffled)
        n_critical = max(1, round(n * 0.10))
        n_warning = max(1, round(n * 0.20))

        for i, sid in enumerate(all_shuffled):
            site = sites[sid]
            if i < n_critical:
                tier_key = "critical_standard"
                tier_label = "critical"
            elif i < n_critical + n_warning:
                tier_key = "warning"
                tier_label = "warning"
            else:
                tier_key = "normal"
                tier_label = "normal"

            hours = _pick_hours(tier_key, rng)
            if site.is_consumer:
                pressures = _consumer_pressures(hours, site, floor)
            else:
                pressures = _producer_pressures(hours, site, floor)

            _set_site_pressures(site, pressures)
            applied[sid] = tier_label

        if scenario_type == "capacity_crisis":
            for truck in trucks.values():
                truck.capacity = 1
            logger.info("[scenario] capacity_crisis: all truck capacities set to 1")

    # ── high_demand ────────────────────────────────────────────────────────────
    elif scenario_type == "high_demand":
        n_crit = max(1, round(len(consumers) * 0.40))
        for i, sid in enumerate(consumers):
            site = sites[sid]
            tier_key = "critical_consumer" if i < n_crit else "normal"
            tier_label = "critical" if i < n_crit else "normal"
            hours = _pick_hours(tier_key, rng)
            _set_site_pressures(site, _consumer_pressures(hours, site, floor))
            applied[sid] = tier_label

        for sid in producers:
            site = sites[sid]
            hours = _pick_hours("normal", rng)
            _set_site_pressures(site, _producer_pressures(hours, site, floor))
            applied[sid] = "normal"

    # ── high_production ────────────────────────────────────────────────────────
    elif scenario_type == "high_production":
        n_crit = max(1, round(len(producers) * 0.40))
        for i, sid in enumerate(producers):
            site = sites[sid]
            tier_key = "near_overflow" if i < n_crit else "normal"
            tier_label = "near_overflow" if i < n_crit else "normal"
            hours = _pick_hours(tier_key, rng)
            _set_site_pressures(site, _producer_pressures(hours, site, floor))
            applied[sid] = tier_label

        for sid in consumers:
            site = sites[sid]
            hours = _pick_hours("normal", rng)
            _set_site_pressures(site, _consumer_pressures(hours, site, floor))
            applied[sid] = "normal"

    logger.info(
        "[scenario] Applied '%s': %d sites mutated",
        scenario_type, len(applied),
    )
    return applied
