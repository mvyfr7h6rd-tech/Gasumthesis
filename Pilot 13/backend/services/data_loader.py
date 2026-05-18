"""Data loading service for Excel and JSON data."""

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any
import pandas as pd

from ..models import (
    Site, SiteType,
    Container, ContainerStatus,
    Truck, Carrier,
    OperationalConfig,
)


# Approximate coordinates for Finnish sites
SITE_COORDINATES = {
    "Helsinki Malmi": (60.2550, 25.0100),
    "Malmi": (60.2550, 25.0100),
    "Huittinen Vampula": (61.1750, 22.7000),
    "Vampula": (61.1750, 22.7000),
    "Aanekoski": (62.6000, 25.7300),
    "Oulu Rusko": (65.0100, 25.4700),
    "Oulu Limingantulli": (65.0000, 25.4500),
    "Pori": (61.4850, 21.7970),
    "Tampere": (61.4978, 23.7610),
    "Raisio": (60.4860, 22.1690),
    "Salo": (60.3830, 23.1330),
    "Espoo": (60.2195, 24.8143),
    "Espoo L": (60.2195, 24.8143),
    "Espoo Lommila": (60.2195, 24.8143),
    "Lansisalmi": (60.2630, 25.0200),
    "Jarvenpaa": (60.4720, 25.0890),
    "Hameenlinna": (60.9959, 24.4643),
    "Porttipuisto": (60.2900, 25.0000),
    "Portipuisto": (60.2900, 25.0000),
    "Takkula": (61.1800, 22.7100),
    "Vehmaa": (60.6880, 21.6690),
}

# Site name normalization mapping
SITE_NAME_MAP = {
    "Helsinki Malmi": "helsinki_malmi",
    "Malmi": "helsinki_malmi",
    "Huittinen Vampula": "huittinen_vampula",
    "Huittenen Vampula": "huittinen_vampula",
    "Vampula": "huittinen_vampula",
    "Aanekoski": "aanekoski",
    "Oulu Rusko": "oulu_rusko",
    "Oulu Limingantulli": "oulu_limingantulli",
    "Pori": "pori",
    "Pori Tiilimäki": "pori",
    "Tampere": "tampere",
    "Tampere Lahdesjärvi": "tampere",
    "Raisio": "raisio",
    "Raisio Kuninkoja": "raisio",
    "Salo": "salo",
    "Salo Piihovi": "salo",
    "Espoo": "espoo_lommila",
    "Espoo L": "espoo_lommila",
    "Espoo Lommila": "espoo_lommila",
    "Lansisalmi": "vantaa_lansisalmi",
    "Vantaa Länsisalmi": "vantaa_lansisalmi",
    "Jarvenpaa": "jarvenpaa",
    "Järvenpää": "jarvenpaa",
    "Hameenlinna": "hameenlinna",
    "Hämeenlinna Moreeni": "hameenlinna",
    "Porttipuisto": "vantaa_porttipuisto",
    "Portipuisto": "vantaa_porttipuisto",
    "Vantaa Porttipuisto": "vantaa_porttipuisto",
    "Takkula": "huittinen_takkula",
    "Huittinen Takkula": "huittinen_takkula",
    "Vehmaa": "vehmaa",
    "Biolan Eura": "biolan_eura",
    "Metsä Fibre Äänekoski": "aanekoski",
}


def normalize_site_name(name: str) -> str:
    """Normalize a site name to a consistent ID format."""
    if not name or pd.isna(name):
        return ""
    name = str(name).strip()
    return SITE_NAME_MAP.get(name, name.lower().replace(" ", "_").replace("-", "_"))


def get_coordinates(name: str) -> tuple:
    """Get coordinates for a site name."""
    for key, coords in SITE_COORDINATES.items():
        if key.lower() in name.lower() or name.lower() in key.lower():
            return coords
    # Default to Helsinki area if not found
    return (60.17, 24.94)


class DataLoader:
    """Service for loading and managing logistics data."""

    def __init__(self, data_dir: Path = None):
        """Initialize with data directory."""
        self.data_dir = data_dir or Path(__file__).parent.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._sites: Dict[str, Site] = {}
        self._containers: Dict[str, Container] = {}
        self._trucks: Dict[str, Truck] = {}
        self._distance_matrix: Dict[str, Dict[str, float]] = {}
        self._config: OperationalConfig = OperationalConfig()

    @property
    def sites(self) -> Dict[str, Site]:
        return self._sites

    @property
    def containers(self) -> Dict[str, Container]:
        return self._containers

    @property
    def trucks(self) -> Dict[str, Truck]:
        return self._trucks

    @property
    def distance_matrix(self) -> Dict[str, Dict[str, float]]:
        return self._distance_matrix

    @property
    def config(self) -> OperationalConfig:
        return self._config

    def load_from_excel(self, excel_path: Path) -> None:
        """Load all data from Excel file."""
        xlsx = pd.ExcelFile(excel_path)

        # Load from first sheet (Ark1)
        df1 = pd.read_excel(xlsx, sheet_name='Ark1', header=None)
        self._parse_sites_from_sheet1(df1)
        self._parse_containers_from_sheet1(df1)

        # Load from second sheet (Ark1 (2)) - distance matrix
        df2 = pd.read_excel(xlsx, sheet_name='Ark1 (2)', header=None)
        self._parse_distance_matrix(df2)
        self._parse_cost_params(df2)

        # Create default trucks
        self._create_default_trucks()

        # Update site inventories from containers
        self._update_site_inventories()

    def _parse_sites_from_sheet1(self, df: pd.DataFrame) -> None:
        """Parse site information from first Excel sheet."""
        # Traffic/Industry sites are in columns 4-7 (rows 4-17)
        # Production sites are in columns 11-16 (rows 4-8)

        # Parse Traffic sites
        for i in range(4, 18):  # Rows 4-17 in 0-indexed
            if i >= len(df):
                break
            row = df.iloc[i]

            site_type_raw = row[4] if pd.notna(row[4]) else ""
            name = row[5] if pd.notna(row[5]) else ""

            if not name or name == "xx" or pd.isna(name):
                continue

            site_type = SiteType.TRAFFIC
            if str(site_type_raw).lower() == "industry":
                site_type = SiteType.INDUSTRY

            site_id = normalize_site_name(name)
            coords = get_coordinates(name)

            site = Site(
                id=site_id,
                name=name,
                site_type=site_type,
                location=str(row[6]) if pd.notna(row[6]) else "",
                latitude=coords[0],
                longitude=coords[1],
                num_bays=int(row[7]) if pd.notna(row[7]) else 2,
                consumption_low_kg_hour=float(row[2]) if pd.notna(row[2]) else 0,
                consumption_high_kg_hour=float(row[3]) if pd.notna(row[3]) else 0,
                production_low_kg_hour=0,
                production_high_kg_hour=0,
                flaring_cost_eur_mwh=0,
                current_inventory=0,
            )
            self._sites[site_id] = site

        # Parse Production sites
        for i in range(4, 9):  # Rows 4-8 in 0-indexed
            if i >= len(df):
                break
            row = df.iloc[i]

            site_type_raw = row[11] if pd.notna(row[11]) else ""
            name = row[12] if pd.notna(row[12]) else ""

            if not name or name == "xx" or str(site_type_raw).lower() != "production":
                continue

            site_id = normalize_site_name(name)
            coords = get_coordinates(name)

            site = Site(
                id=site_id,
                name=name,
                site_type=SiteType.PRODUCTION,
                location=str(row[13]) if pd.notna(row[13]) else "",
                latitude=coords[0],
                longitude=coords[1],
                num_bays=int(row[14]) if pd.notna(row[14]) else 3,
                consumption_low_kg_hour=0,
                consumption_high_kg_hour=0,
                production_low_kg_hour=float(row[9]) if pd.notna(row[9]) else 0,
                production_high_kg_hour=float(row[10]) if pd.notna(row[10]) else 0,
                flaring_cost_eur_mwh=float(row[16]) if pd.notna(row[16]) else 0,
                current_inventory=0,
            )
            self._sites[site_id] = site

    def _parse_containers_from_sheet1(self, df: pd.DataFrame) -> None:
        """Parse container information from first Excel sheet."""
        # Containers are in columns 4-8 starting from row 40

        for i in range(40, len(df)):
            row = df.iloc[i]

            container_id = row[4] if pd.notna(row[4]) else ""
            if not container_id or pd.isna(container_id):
                continue

            location_raw = str(row[5]) if pd.notna(row[5]) else ""

            # Parse location and bay
            site_id = ""
            bay = ""
            if " - " in location_raw:
                parts = location_raw.split(" - ")
                site_name = parts[0].strip()
                bay = parts[1].strip() if len(parts) > 1 else ""
                site_id = normalize_site_name(site_name)
            elif location_raw.lower() == "in transit":
                site_id = ""

            pressure = float(row[6]) if pd.notna(row[6]) else 0
            mwh = float(row[7]) if pd.notna(row[7]) else 0
            kg = float(row[8]) if pd.notna(row[8]) else 0

            status = ContainerStatus.IN_TRANSIT if not site_id else ContainerStatus.AT_SITE

            container = Container(
                id=str(container_id),
                location_site_id=site_id,
                location_bay=bay,
                pressure_bar=pressure,
                mwh=mwh,
                kg=kg,
                max_pressure_bar=250,
                status=status,
            )
            self._containers[str(container_id)] = container

    def _parse_distance_matrix(self, df: pd.DataFrame) -> None:
        """Parse distance matrix from second Excel sheet."""
        # Headers are in row 9 (0-indexed), data starts at row 10

        # Get site names from header row
        header_row = df.iloc[9]
        site_names = []
        for i in range(2, 18):  # Columns 2-17
            name = header_row[i] if pd.notna(header_row[i]) else ""
            if name:
                site_names.append((i, str(name)))

        # Parse distances (rows 10-25)
        for i in range(10, 26):
            if i >= len(df):
                break
            row = df.iloc[i]

            from_name = row[1] if pd.notna(row[1]) else ""
            if not from_name:
                continue

            from_id = normalize_site_name(from_name)
            if from_id not in self._distance_matrix:
                self._distance_matrix[from_id] = {}

            for col_idx, to_name in site_names:
                to_id = normalize_site_name(to_name)
                distance = row[col_idx] if pd.notna(row[col_idx]) else 0

                if from_id == to_id:
                    self._distance_matrix[from_id][to_id] = 0
                else:
                    self._distance_matrix[from_id][to_id] = float(distance) if distance else 0

    def _parse_cost_params(self, df: pd.DataFrame) -> None:
        """Parse cost parameters from second Excel sheet."""
        # SPEED cost is in row 4, SYRIAPALO in row 5, handling fee in row 6

        for i in range(4, 8):
            if i >= len(df):
                break
            row = df.iloc[i]
            param_name = str(row[1]) if pd.notna(row[1]) else ""
            value = row[2] if pd.notna(row[2]) else 0

            if "SPEED" in param_name.upper():
                self._config.cost_per_km_eur = float(value)
            elif "HANDLING" in param_name.upper():
                self._config.handling_fee_eur = float(value)

    def _create_default_trucks(self) -> None:
        """Create default trucks for testing."""
        production_sites = [s for s in self._sites.values() if s.is_producer]

        # Create 2 trucks at each production site
        truck_id = 1
        for site in production_sites[:3]:  # Limit to 3 production sites
            for capacity in [2, 3]:
                truck = Truck(
                    id=f"TRUCK{truck_id:03d}",
                    carrier=Carrier.SPEED,
                    capacity=capacity,
                    home_site_id=site.id,
                    current_location_site_id=site.id,
                    current_load=[],
                )
                self._trucks[truck.id] = truck
                truck_id += 1

    def _update_site_inventories(self) -> None:
        """Update site inventory counts based on container locations."""
        # Reset all inventories
        for site in self._sites.values():
            site.current_inventory = 0

        # Count containers at each site
        for container in self._containers.values():
            if container.location_site_id and container.location_site_id in self._sites:
                self._sites[container.location_site_id].current_inventory += 1

    def get_distance(self, from_site_id: str, to_site_id: str) -> float:
        """Get distance between two sites in km."""
        if from_site_id == to_site_id:
            return 0.0

        if from_site_id in self._distance_matrix:
            return self._distance_matrix[from_site_id].get(to_site_id, 0.0)

        # Try reverse lookup
        if to_site_id in self._distance_matrix:
            return self._distance_matrix[to_site_id].get(from_site_id, 0.0)

        return 0.0

    def save_to_json(self, *, include_trucks: bool = True) -> None:
        """Save mutable state to JSON files.

        Args:
            include_trucks: When False, truck state stays in memory only.
                This is useful for temporary scenario generation where site
                pressures should persist but truck capacities should not
                contaminate the canonical dataset.
        """
        # Save sites
        sites_data = {site_id: site.model_dump() for site_id, site in self._sites.items()}
        with open(self.data_dir / "sites.json", "w") as f:
            json.dump(sites_data, f, indent=2, default=str)

        # Save containers
        containers_data = {c_id: c.model_dump() for c_id, c in self._containers.items()}
        with open(self.data_dir / "containers.json", "w") as f:
            json.dump(containers_data, f, indent=2, default=str)

        if include_trucks:
            trucks_data = {t_id: t.model_dump() for t_id, t in self._trucks.items()}
            with open(self.data_dir / "trucks.json", "w") as f:
                json.dump(trucks_data, f, indent=2, default=str)

        # Save distance matrix
        with open(self.data_dir / "distances.json", "w") as f:
            json.dump(self._distance_matrix, f, indent=2)

        # Save config
        with open(self.data_dir / "config.json", "w") as f:
            json.dump(self._config.model_dump(), f, indent=2)

    def _simulation_baseline_dir(self) -> Path:
        """Directory where simulation baseline snapshots are stored."""
        return self.data_dir / "_simulation_baseline"

    def ensure_simulation_baseline(self) -> bool:
        """Create simulation baseline snapshot if it does not already exist.

        Returns:
            True if a new baseline was created, False if it already existed.
        """
        baseline_dir = self._simulation_baseline_dir()
        baseline_dir.mkdir(parents=True, exist_ok=True)

        source_files = ["sites.json", "containers.json", "trucks.json", "config.json", "distances.json"]
        target_files = [baseline_dir / name for name in source_files]
        if all(path.exists() for path in target_files):
            return False

        for name in source_files:
            src = self.data_dir / name
            dst = baseline_dir / name
            if src.exists():
                shutil.copy2(src, dst)
        return True

    def restore_simulation_baseline(self) -> bool:
        """Restore canonical JSON files from simulation baseline snapshot.

        Returns:
            True if baseline existed and restore was performed, False otherwise.
        """
        baseline_dir = self._simulation_baseline_dir()
        if not baseline_dir.exists():
            return False

        files = ["sites.json", "containers.json", "trucks.json", "config.json", "distances.json"]
        if not all((baseline_dir / name).exists() for name in files):
            return False

        for name in files:
            shutil.copy2(baseline_dir / name, self.data_dir / name)

        # Refresh in-memory models from restored JSON.
        self.load_from_json()
        return True

    def load_from_json(self) -> None:
        """Load all data from JSON files."""
        # Load sites
        sites_path = self.data_dir / "sites.json"
        if sites_path.exists():
            with open(sites_path) as f:
                sites_data = json.load(f)
                self._sites = {k: Site(**v) for k, v in sites_data.items()}

        # Load containers
        containers_path = self.data_dir / "containers.json"
        if containers_path.exists():
            with open(containers_path) as f:
                containers_data = json.load(f)
                self._containers = {k: Container(**v) for k, v in containers_data.items()}

        # Load trucks
        trucks_path = self.data_dir / "trucks.json"
        if trucks_path.exists():
            with open(trucks_path) as f:
                trucks_data = json.load(f)
                self._trucks = {k: Truck(**v) for k, v in trucks_data.items()}

        # Load distance matrix
        distances_path = self.data_dir / "distances.json"
        if distances_path.exists():
            with open(distances_path) as f:
                self._distance_matrix = json.load(f)

        # Load config
        config_path = self.data_dir / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                config_data = json.load(f)
                self._config = OperationalConfig(**config_data)

        # Enforce operational pressure floor on persisted site bays.
        # Historical snapshots may contain 0-bar placeholders from older logic.
        floor_bar = self._config.usable_floor_bar
        for site in self._sites.values():
            for bay in site.bays:
                if bay.pressure_bar < floor_bar:
                    bay.pressure_bar = floor_bar

        # Validate truck start locations against the distance matrix.
        # If a truck's stored start site_id is not a known routing node, fall back
        # to home_site_id and mark start_resolved=True so the UI can warn the operator.
        if self._trucks and self._distance_matrix:
            from ..models.truck import SiteLocation
            import logging as _logging
            _dl_logger = _logging.getLogger(__name__)
            for truck_id, truck in self._trucks.items():
                start_id = truck.effective_start_site_id
                if start_id and start_id not in self._distance_matrix:
                    _dl_logger.warning(
                        "Truck %s: start site_id=%r not in distance matrix; "
                        "falling back to home_site_id=%r",
                        truck_id, start_id, truck.home_site_id,
                    )
                    truck.start = SiteLocation(site_id=truck.home_site_id)
                    truck.start_resolved = True

    def update_from_json_upload(self, data: Dict[str, Any], data_type: str) -> int:
        """
        Update data from uploaded JSON.

        Returns number of items updated.
        """
        count = 0

        if data_type == "sites":
            for site_id, site_data in data.items():
                self._sites[site_id] = Site(**site_data)
                count += 1

        elif data_type == "containers":
            for container_id, container_data in data.items():
                self._containers[container_id] = Container(**container_data)
                count += 1
            self._update_site_inventories()

        elif data_type == "trucks":
            for truck_id, truck_data in data.items():
                self._trucks[truck_id] = Truck(**truck_data)
                count += 1

        elif data_type == "config":
            self._config = OperationalConfig(**data)
            count = 1

        return count

    def get_site_list(self) -> List[Site]:
        """Get list of all sites."""
        return list(self._sites.values())

    def get_container_list(self) -> List[Container]:
        """Get list of all containers."""
        return list(self._containers.values())

    def get_truck_list(self) -> List[Truck]:
        """Get list of all trucks."""
        return list(self._trucks.values())

    def get_containers_at_site(self, site_id: str) -> List[Container]:
        """Get all containers at a specific site."""
        return [c for c in self._containers.values() if c.location_site_id == site_id]

    def get_full_containers_at_site(self, site_id: str) -> List[Container]:
        """Get full containers at a specific site."""
        return [c for c in self.get_containers_at_site(site_id) if c.is_full]

    def get_empty_containers_at_site(self, site_id: str) -> List[Container]:
        """Get empty containers at a specific site."""
        return [c for c in self.get_containers_at_site(site_id) if c.is_empty]
