"""Conversion utilities for pressure, mass, and energy calculations.

Anchors:
- 200 bar → 1316 kg → 20 MWh
- 250 bar → 2829 kg → 43 MWh

Energy formula: MWh = (kg / 1000) * 15.2

Pressure Normalization:
- Base pressure of 20 bar is considered "empty" (normalized to 0)
- Formula: p_norm = max(0, p_raw - 20)
- This applies to both producers and consumers for consistent risk/demand scoring
"""

# Base pressure constant: 20 bar = 0 in normalized model
BASE_PRESSURE_BAR = 20


def effective_pressure_bar(raw_bar: int) -> int:
    """Return operational pressure, treating readings below the usable floor as zero.

    Physical pressure may be non-zero below 20 bar (BASE_PRESSURE_BAR), but is
    operationally treated as zero — gas at that level is inaccessible to consumers
    and logistics.  All risk, demand, and time-evolution calculations must use this
    function rather than raw pressure_bar directly.

    Args:
        raw_bar: Raw pressure reading in bar (0-250)

    Returns:
        0 if raw_bar < BASE_PRESSURE_BAR (20), else raw_bar

    Examples:
        >>> effective_pressure_bar(0)
        0
        >>> effective_pressure_bar(15)
        0
        >>> effective_pressure_bar(20)
        20
        >>> effective_pressure_bar(100)
        100
    """
    return 0 if raw_bar < BASE_PRESSURE_BAR else raw_bar


def normalize_pressure(pressure_raw: int) -> int:
    """Normalize pressure by subtracting base pressure (20 bar = 0).

    The minimum operational pressure of 20 bar is considered "empty" in the
    logistics model. This function converts raw pressure readings to the
    normalized scale where:
    - 20 bar raw → 0 normalized
    - 100 bar raw → 80 normalized
    - 0 bar raw → 0 normalized (clamped)

    Args:
        pressure_raw: Raw pressure reading in bar (0-250)

    Returns:
        Normalized pressure (0-230 range)

    Examples:
        >>> normalize_pressure(20)
        0
        >>> normalize_pressure(100)
        80
        >>> normalize_pressure(0)
        0
        >>> normalize_pressure(250)
        230
    """
    return max(0, pressure_raw - BASE_PRESSURE_BAR)


def denormalize_pressure(pressure_norm: int) -> int:
    """Convert normalized pressure back to raw pressure.

    Inverse of normalize_pressure.

    Args:
        pressure_norm: Normalized pressure (0-230)

    Returns:
        Raw pressure in bar (20-250 range)
    """
    return pressure_norm + BASE_PRESSURE_BAR


def pressure_to_kg(pressure_bar: int) -> float:
    """Convert pressure (bar) to mass (kg) using piecewise linear formula.

    - 0..200 bar: linear from (0, 0 kg) to (200, 1316 kg)
    - 200..250 bar: linear from (200, 1316 kg) to (250, 2829 kg)

    Args:
        pressure_bar: Pressure in bar (0-250)

    Returns:
        Mass in kg
    """
    if pressure_bar <= 0:
        return 0.0
    if pressure_bar <= 200:
        return (pressure_bar / 200) * 1316
    # 200-250 range
    return 1316 + ((pressure_bar - 200) / 50) * (2829 - 1316)


def kg_to_pressure(kg: float) -> float:
    """Inverse of pressure_to_kg — convert mass (kg) back to pressure (bar).

    Piecewise linear inverse of the two-segment pressure_to_kg formula:
    - 0..1316 kg  → 0..200 bar  (lower segment)
    - 1316..2829 kg → 200..250 bar (upper segment)

    Args:
        kg: Mass in kg (0-2829)

    Returns:
        Pressure in bar as a float (0.0–250.0), clamped at both ends.
    """
    if kg <= 0.0:
        return 0.0
    if kg <= 1316.0:
        return (kg / 1316.0) * 200.0
    if kg <= 2829.0:
        return 200.0 + ((kg - 1316.0) / (2829.0 - 1316.0)) * 50.0
    return 250.0


def kg_to_mwh(kg: float) -> float:
    """Convert mass (kg) to energy (MWh).

    Formula: MWh = (kg / 1000) * 15.2

    Args:
        kg: Mass in kilograms

    Returns:
        Energy in MWh
    """
    return (kg / 1000) * 15.2


def mwh_to_kg(mwh: float) -> float:
    """Convert energy (MWh) to mass (kg).

    Formula: kg = (MWh / 15.2) * 1000

    Args:
        mwh: Energy in MWh

    Returns:
        Mass in kg
    """
    return (mwh / 15.2) * 1000


def normalized_pressure_to_kg(pressure_norm: int) -> float:
    """Convert normalized pressure to kg.

    Uses the normalized pressure (where 20 bar = 0) and converts
    to the raw scale before computing kg.

    Args:
        pressure_norm: Normalized pressure (0-230)

    Returns:
        Mass in kg (using raw pressure conversion)
    """
    pressure_raw = denormalize_pressure(pressure_norm)
    return pressure_to_kg(pressure_raw)


def get_normalized_kg(pressure_raw: int, floor_bar: int = BASE_PRESSURE_BAR) -> float:
    """Get usable kg from raw pressure (accounting for operational floor pressure).

    Returns the kg of gas that is actually usable/available, i.e., the kg above
    the minimum operational pressure (floor_bar).  Gas at or below the floor is
    inaccessible and counts as zero.

    For consumers: this is the usable supply before "empty".
    For producers: use remaining capacity calculation instead.

    Args:
        pressure_raw: Raw pressure reading in bar (0-250)
        floor_bar: Minimum operational pressure (default: BASE_PRESSURE_BAR = 20).
                   Reads from config.usable_floor_bar at call sites.

    Returns:
        Usable kg (kg at raw pressure minus kg at floor_bar). 0 if at/below floor.
    """
    if pressure_raw <= floor_bar:
        return 0.0
    return pressure_to_kg(pressure_raw) - pressure_to_kg(floor_bar)


import math

def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two points using Haversine formula.

    Args:
        lat1, lon1: Latitude and longitude of first point in degrees
        lat2, lon2: Latitude and longitude of second point in degrees

    Returns:
        Distance in kilometers
    """
    # Earth radius in km
    R = 6371.0

    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    # Haversine formula
    a = math.sin(delta_lat / 2) ** 2 + \
        math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c
