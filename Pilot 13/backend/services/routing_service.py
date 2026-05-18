"""Road-network routing service using GraphHopper (offline OSM)."""

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


def decode_polyline(encoded: str, precision: int = 5) -> list[list[float]]:
    """Decode a Google/GraphHopper encoded polyline string into [[lat, lon], ...]."""
    inv = 10 ** precision
    decoded = []
    lat = lon = 0
    i = 0
    while i < len(encoded):
        for coord in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            if result & 1:
                val = ~(result >> 1)
            else:
                val = result >> 1
            if coord == 0:
                lat += val
            else:
                lon += val
        decoded.append([lat / inv, lon / inv])
    return decoded


@dataclass
class RouteResult:
    """Result of a single route computation."""

    distance_km: float
    duration_min: float
    geometry: Optional[str] = None

    @property
    def decoded_geometry(self) -> list[list[float]]:
        """Decode encoded polyline to [[lat, lon], ...]. Empty list if no geometry."""
        if not self.geometry:
            return []
        try:
            return decode_polyline(self.geometry)
        except Exception:
            return []


@dataclass
class RoutingConfig:
    """Configuration for the routing service."""

    graphhopper_url: str = "http://localhost:8989"
    profile: str = "car"
    cache_db_path: str = str(
        Path(__file__).parent.parent / "data" / "route_cache.sqlite"
    )
    penalty_distance_km: float = 500.0
    penalty_duration_min: float = 600.0
    request_timeout_seconds: float = 30.0
    coordinate_precision: int = 5  # decimal places for cache key rounding
    # OSRM public API used as geometry fallback when GraphHopper is unavailable.
    # Set OSRM_URL="" to disable OSRM fallback entirely.
    osrm_base_url: str = "https://router.project-osrm.org"
    osrm_timeout_seconds: float = 8.0

    @staticmethod
    def from_env() -> "RoutingConfig":
        default_cache = str(
            Path(__file__).parent.parent / "data" / "route_cache.sqlite"
        )
        return RoutingConfig(
            graphhopper_url=os.getenv("GRAPHHOPPER_URL", "http://localhost:8989"),
            profile=os.getenv("ROUTER_PROFILE", "car"),
            cache_db_path=os.getenv("ROUTE_CACHE_DB", default_cache),
            penalty_distance_km=float(
                os.getenv("ROUTE_PENALTY_DISTANCE_KM", "500")
            ),
            penalty_duration_min=float(
                os.getenv("ROUTE_PENALTY_DURATION_MIN", "600")
            ),
            osrm_base_url=os.getenv("OSRM_URL", "https://router.project-osrm.org"),
        )


class RoutingService:
    """
    Road-network routing via a local GraphHopper instance.

    All results are cached in SQLite. On routing failure,
    returns configurable penalty values so the solver can still run.
    """

    def __init__(self, config: RoutingConfig = None):
        self.config = config or RoutingConfig.from_env()
        self._client = httpx.Client(
            base_url=self.config.graphhopper_url,
            timeout=self.config.request_timeout_seconds,
        )
        self._db_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_cache()

        # Check GraphHopper availability at startup for early visibility.
        # The routing methods always try GraphHopper first regardless of this flag,
        # but logging it here surfaces connectivity issues immediately.
        self._graphhopper_available: bool = self.health_check()
        if self._graphhopper_available:
            logger.info("GraphHopper is available at %s", self.config.graphhopper_url)
        else:
            osrm_status = "enabled" if self.config.osrm_base_url else "disabled"
            logger.warning(
                "GraphHopper unavailable at %s — OSRM geometry fallback %s (%s)",
                self.config.graphhopper_url,
                osrm_status,
                self.config.osrm_base_url or "no fallback URL configured",
            )

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _init_cache(self) -> None:
        """Create SQLite cache table if it does not exist."""
        self._conn = sqlite3.connect(
            self.config.cache_db_path, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS route_cache (
                from_lat     REAL NOT NULL,
                from_lon     REAL NOT NULL,
                to_lat       REAL NOT NULL,
                to_lon       REAL NOT NULL,
                profile      TEXT NOT NULL DEFAULT 'car',
                distance_km  REAL NOT NULL,
                duration_min REAL NOT NULL,
                geometry     TEXT,
                cached_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (from_lat, from_lon, to_lat, to_lon, profile)
            )
            """
        )
        self._conn.commit()

    def _round(self, val: float) -> float:
        return round(val, self.config.coordinate_precision)

    def _cache_get(
        self, from_lat: float, from_lon: float, to_lat: float, to_lon: float
    ) -> Optional[RouteResult]:
        cur = self._conn.execute(
            "SELECT distance_km, duration_min, geometry FROM route_cache "
            "WHERE from_lat=? AND from_lon=? AND to_lat=? AND to_lon=? AND profile=?",
            (
                self._round(from_lat),
                self._round(from_lon),
                self._round(to_lat),
                self._round(to_lon),
                self.config.profile,
            ),
        )
        row = cur.fetchone()
        if row:
            return RouteResult(
                distance_km=row[0], duration_min=row[1], geometry=row[2]
            )
        return None

    def _cache_put(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        result: RouteResult,
    ) -> None:
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO route_cache "
                "(from_lat, from_lon, to_lat, to_lon, profile, distance_km, duration_min, geometry) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._round(from_lat),
                    self._round(from_lon),
                    self._round(to_lat),
                    self._round(to_lon),
                    self.config.profile,
                    result.distance_km,
                    result.duration_min,
                    result.geometry,
                ),
            )
            self._conn.commit()

    def _cache_get_many(
        self, pairs: List[Tuple[float, float, float, float]]
    ) -> Dict[Tuple[float, float, float, float], RouteResult]:
        """Batch-read from cache. Returns dict keyed by rounded coordinate tuple."""
        results: Dict[Tuple[float, float, float, float], RouteResult] = {}
        for from_lat, from_lon, to_lat, to_lon in pairs:
            hit = self._cache_get(from_lat, from_lon, to_lat, to_lon)
            if hit:
                key = (
                    self._round(from_lat),
                    self._round(from_lon),
                    self._round(to_lat),
                    self._round(to_lon),
                )
                results[key] = hit
        return results

    # ------------------------------------------------------------------
    # GraphHopper HTTP
    # ------------------------------------------------------------------

    def route(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
    ) -> RouteResult:
        """
        Get road route between two coordinates.

        1. Check SQLite cache (coords rounded to configured precision)
        2. On miss, call GraphHopper /route API
        3. On failure, return penalty values and log error
        """
        # Same point
        if self._round(from_lat) == self._round(to_lat) and self._round(
            from_lon
        ) == self._round(to_lon):
            return RouteResult(distance_km=0.0, duration_min=0.0)

        # Cache lookup
        cached = self._cache_get(from_lat, from_lon, to_lat, to_lon)
        if cached:
            return cached

        # Call GraphHopper
        try:
            resp = self._client.get(
                "/route",
                params={
                    "point": [
                        f"{from_lat},{from_lon}",
                        f"{to_lat},{to_lon}",
                    ],
                    "profile": self.config.profile,
                    "calc_points": "true",
                    "points_encoded": "true",
                },
            )
            resp.raise_for_status()
            data = resp.json()

            path = data["paths"][0]
            result = RouteResult(
                distance_km=path["distance"] / 1000.0,
                duration_min=path["time"] / 60000.0,
                geometry=path.get("points"),
            )
            self._cache_put(from_lat, from_lon, to_lat, to_lon, result)
            return result

        except Exception as e:
            logger.warning(
                "GraphHopper routing failed (%s,%s)->(%s,%s): %s — trying OSRM fallback",
                from_lat, from_lon, to_lat, to_lon, e,
            )
            # Try OSRM public API before returning penalty values.
            # Cache the result so the next request is instant.
            osrm = self._route_osrm(from_lat, from_lon, to_lat, to_lon)
            if osrm is not None:
                self._cache_put(from_lat, from_lon, to_lat, to_lon, osrm)
                return osrm
            logger.error(
                "All routing failed (%s,%s)->(%s,%s) — returning penalty values",
                from_lat, from_lon, to_lat, to_lon,
            )
            return RouteResult(
                distance_km=self.config.penalty_distance_km,
                duration_min=self.config.penalty_duration_min,
            )

    def route_multi_stop(
        self,
        waypoints: List[Tuple[float, float]],
    ) -> list[list[float]]:
        """
        Get road-following geometry for an ordered sequence of waypoints.

        Calls GraphHopper /route with all waypoints in order and returns
        the decoded polyline as [[lat, lon], ...].

        Returns empty list on failure.
        """
        if len(waypoints) < 2:
            return []

        try:
            resp = self._client.get(
                "/route",
                params={
                    "point": [f"{lat},{lon}" for lat, lon in waypoints],
                    "profile": self.config.profile,
                    "calc_points": "true",
                    "points_encoded": "true",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            encoded = data["paths"][0].get("points")
            if not encoded:
                return []
            pts = decode_polyline(encoded)
            logger.debug(
                "Multi-stop route: %d waypoints → %d geometry points",
                len(waypoints), len(pts),
            )
            return pts
        except Exception as e:
            logger.warning(
                "GraphHopper multi-stop failed (%d waypoints): %s — trying OSRM fallback",
                len(waypoints), e,
            )
            pts = self._route_multi_stop_osrm(waypoints)
            if pts:
                logger.info(
                    "OSRM multi-stop fallback succeeded: %d waypoints → %d points",
                    len(waypoints), len(pts),
                )
            return pts

    def matrix(
        self,
        points: List[Tuple[str, float, float]],
    ) -> Dict[str, Dict[str, RouteResult]]:
        """
        Compute N x N route matrix for a list of named points.

        Each entry is (id, lat, lon).  Returns nested dict:
            results[from_id][to_id] = RouteResult

        Uses GraphHopper /matrix for uncached pairs, caches individually.
        """
        n = len(points)
        results: Dict[str, Dict[str, RouteResult]] = {
            p[0]: {} for p in points
        }

        # Identify which pairs are already cached
        uncached_pairs: List[Tuple[int, int]] = []  # (i, j) indices
        for i in range(n):
            for j in range(n):
                from_id, from_lat, from_lon = points[i]
                to_id, to_lat, to_lon = points[j]
                if i == j:
                    results[from_id][to_id] = RouteResult(0.0, 0.0)
                    continue
                cached = self._cache_get(from_lat, from_lon, to_lat, to_lon)
                if cached:
                    results[from_id][to_id] = cached
                else:
                    uncached_pairs.append((i, j))

        if not uncached_pairs:
            return results

        # Call GraphHopper /matrix for uncached pairs
        try:
            point_params = [f"{p[1]},{p[2]}" for p in points]
            resp = self._client.post(
                "/matrix",
                json={
                    "points": [[p[2], p[1]] for p in points],  # GH matrix expects [lon, lat]
                    "profile": self.config.profile,
                    "out_arrays": ["distances", "times"],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

            distances = data["distances"]  # meters
            times = data["times"]  # milliseconds

            for i_idx, j_idx in uncached_pairs:
                from_id, from_lat, from_lon = points[i_idx]
                to_id, to_lat, to_lon = points[j_idx]
                dist_km = distances[i_idx][j_idx] / 1000.0
                dur_min = times[i_idx][j_idx] / 60000.0
                r = RouteResult(distance_km=dist_km, duration_min=dur_min)
                results[from_id][to_id] = r
                self._cache_put(from_lat, from_lon, to_lat, to_lon, r)

        except Exception as e:
            logger.warning(
                "Matrix API unavailable (%s), falling back to individual /route calls for %d pairs",
                e, len(uncached_pairs),
            )
            # Fall back to individual route() calls
            for i_idx, j_idx in uncached_pairs:
                from_id, from_lat, from_lon = points[i_idx]
                to_id, to_lat, to_lon = points[j_idx]
                if to_id not in results[from_id]:
                    results[from_id][to_id] = self.route(
                        from_lat, from_lon, to_lat, to_lon
                    )

        return results

    def build_site_matrices(
        self,
        sites: Dict,
        extra_points: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
        """
        Build distance and time matrices for all sites (+ optional extra points).

        Returns:
            (distance_matrix, time_matrix) where each is
            Dict[from_id, Dict[to_id, float]]
            distance in km, time in minutes.
        """
        # Build point list: (id, lat, lon)
        point_list: List[Tuple[str, float, float]] = []
        for site_id, site in sites.items():
            point_list.append((site_id, site.latitude, site.longitude))
        if extra_points:
            for pid, (lat, lon) in extra_points.items():
                point_list.append((pid, lat, lon))

        logger.info(
            "Building road-based matrices for %d points (%d sites + %d extra)",
            len(point_list),
            len(sites),
            len(extra_points) if extra_points else 0,
        )

        # Compute full matrix via GraphHopper
        route_matrix = self.matrix(point_list)

        # Convert to separate distance and time dicts
        distance_matrix: Dict[str, Dict[str, float]] = {}
        time_matrix: Dict[str, Dict[str, float]] = {}

        for from_id, row in route_matrix.items():
            distance_matrix[from_id] = {}
            time_matrix[from_id] = {}
            for to_id, result in row.items():
                distance_matrix[from_id][to_id] = round(result.distance_km, 3)
                time_matrix[from_id][to_id] = round(result.duration_min, 2)

        return distance_matrix, time_matrix

    # ------------------------------------------------------------------
    # OSRM fallback geometry
    # ------------------------------------------------------------------
    # Used when GraphHopper is offline or returns an error.  Queries the
    # OSRM public HTTP API (or a self-hosted instance via OSRM_URL env var).
    # Results are cached in SQLite identically to GraphHopper results so a
    # recovered GraphHopper won't re-request already-cached pairs.
    #
    # OSRM coordinate order is lon,lat (opposite of GraphHopper).
    # Geometry is a Google-encoded polyline (precision 5) — same as GraphHopper.

    def _route_osrm(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
    ) -> Optional[RouteResult]:
        """Query OSRM for a single leg. Returns None on failure."""
        if not self.config.osrm_base_url:
            return None
        try:
            url = (
                f"{self.config.osrm_base_url}/route/v1/driving/"
                f"{from_lon},{from_lat};{to_lon},{to_lat}"
            )
            resp = httpx.get(
                url,
                params={"overview": "full", "geometries": "polyline"},
                timeout=self.config.osrm_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                return None
            r = data["routes"][0]
            result = RouteResult(
                distance_km=r["distance"] / 1000.0,
                duration_min=r["duration"] / 60.0,
                geometry=r.get("geometry"),
            )
            logger.debug(
                "OSRM fallback: (%s,%s)->(%s,%s) → %.1f km",
                from_lat, from_lon, to_lat, to_lon, result.distance_km,
            )
            return result
        except Exception as e:
            logger.debug(
                "OSRM fallback failed (%s,%s)->(%s,%s): %s",
                from_lat, from_lon, to_lat, to_lon, e,
            )
            return None

    def _route_multi_stop_osrm(
        self,
        waypoints: List[Tuple[float, float]],
    ) -> list[list[float]]:
        """Query OSRM for a multi-stop route geometry. Returns [] on failure."""
        if not self.config.osrm_base_url or len(waypoints) < 2:
            return []
        try:
            # OSRM coordinate order: lon,lat
            coords = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
            url = f"{self.config.osrm_base_url}/route/v1/driving/{coords}"
            resp = httpx.get(
                url,
                params={"overview": "full", "geometries": "polyline"},
                timeout=self.config.osrm_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                return []
            encoded = data["routes"][0].get("geometry")
            if not encoded:
                return []
            pts = decode_polyline(encoded)
            logger.debug(
                "OSRM multi-stop fallback: %d waypoints → %d geometry points",
                len(waypoints), len(pts),
            )
            return pts
        except Exception as e:
            logger.debug(
                "OSRM multi-stop fallback failed (%d waypoints): %s",
                len(waypoints), e,
            )
            return []

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Check if GraphHopper is reachable and ready."""
        try:
            resp = self._client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        """Clean up resources."""
        if self._conn:
            self._conn.close()
        self._client.close()
