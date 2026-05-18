// Site types
export type SiteType = 'production' | 'traffic' | 'industry';
export type RiskLevel = 'critical' | 'warning' | 'normal' | 'safe';

// Production rate mode
export type ProductionRateMode = 'KG_PER_H' | 'MWH_PER_H' | 'UNKNOWN';

// Bay interface
export interface Bay {
  bay_id: string;
  pressure_bar: number;
  kg: number;         // raw kg (diagnostic only — includes gas below usable floor)
  mwh: number;        // raw MWh (diagnostic only)
  kg_usable: number;  // usable kg above site's usable_floor_bar (primary)
  mwh_usable: number; // usable MWh (primary)
  serial_number?: string | null;
}

// Production rates interface
export interface ProductionRates {
  mode: ProductionRateMode;
  kg_per_h: number | null;
  mwh_per_h: number | null;
  effective_kg_per_h: number | null;
  effective_mwh_per_h: number | null;
}

// Site interface
export interface Site {
  id: string;
  name: string;
  site_type: SiteType;
  latitude: number;
  longitude: number;

  // Bay-based inventory
  bays_fixed: number;
  bays: Bay[];
  total_kg: number;              // raw total kg (diagnostic only)
  total_mwh: number;             // raw total MWh (diagnostic only)
  total_kg_usable: number;       // usable kg above usable_floor_bar (primary)
  total_mwh_usable: number;      // usable MWh (primary)
  usable_floor_bar: number;      // floor from config — echoed so frontend never hardcodes it
  is_full: boolean;
  min_pressure: number;
  max_pressure: number;
  avg_pressure: number;
  utilization_percentage: number;     // avg-pressure-based (informational)
  utilization_usable_pct: number;     // usable-kg-based (primary metric)

  // Consumption (consumers only)
  consumption_rate_kg_hour: number;

  // Production (producers only)
  production: ProductionRates | null;
  flaring_cost_eur_mwh: number;
  flaring_loss_eur_per_h: number | null;

  // Risk assessment
  risk_level: RiskLevel;
  hours_to_critical: number;
  risk_score: number;
  risk_explanation: string;
}

// Patch request types
export interface BayUpdate {
  bay_id: string;
  pressure_bar: number;
  serial_number?: string | null;
}

// Container move (from recommendation)
export interface ContainerMove {
  from_site_id: string;
  from_site_name: string;
  to_site_id: string;
  to_site_name: string;
  bay_id: string;
  bay_serial_number?: string | null;
  reason?: string | null;
  truck_id: string;
  day_index: number;
}

export interface ProductionRatesUpdate {
  mode: ProductionRateMode;
  kg_per_h?: number | null;
  mwh_per_h?: number | null;
}

export interface SitePatchRequest {
  bays?: BayUpdate[];
  production?: ProductionRatesUpdate;
}

// Container types
export type ContainerStatus = 'at_site' | 'in_transit';

export interface Container {
  id: string;
  location_site_id: string;
  location_bay: string;
  pressure_bar: number;
  mwh: number;
  kg: number;
  max_pressure_bar: number;
  status: ContainerStatus;
  fill_percentage: number;
  is_full: boolean;
  is_empty: boolean;
}

// Truck types
export type Carrier = 'SPEED' | 'SYRIAPALO';

export interface Truck {
  id: string;
  carrier: Carrier;
  capacity: number;
  home_site_id: string;
  current_location_site_id: string;
  current_load: string[];
  cost_per_km: number;
  available_capacity: number;
  start_resolved?: boolean;
}

// Route types
export interface SwapOperation {
  site_id: string;
  containers_dropped: string[];
  containers_picked: string[];
  truck_load_delta: number;
  site_inventory_delta: number;
}

export interface RouteStop {
  sequence: number;
  site_id: string;
  site_name: string;
  arrival_time_hours: number;
  distance_from_previous_km: number;
  cumulative_distance_km: number;
  service_time_hours: number;
  swap_operation: SwapOperation | null;
  truck_load_after: number;
  load_full_after?: number;
  load_empty_after?: number;
}

export interface RouteLeg {
  from_stop_index: number;
  to_stop_index: number;
  from_site_id: string;
  from_site_name: string;
  to_site_id: string;
  to_site_name: string;
  distance_km: number;
  drive_time_hours?: number;
  geometry?: [number, number][] | null;
}

export interface Route {
  id: string;
  truck_id: string;
  day_index: number;
  stops: RouteStop[];
  legs: RouteLeg[];
  start_site_id?: string;
  start_label?: string;
  end_site_id?: string;
  geometry?: [number, number][] | null;
  total_distance_km: number;
  legs_sum_km: number;
  total_time_hours: number;
  num_stops: number;
}

// Recommendation types
export type RecommendationStatus = 'computing' | 'ready' | 'approved' | 'rejected' | 'executed' | 'infeasible';
export type ObjectiveFunction = 'time' | 'flaring' | 'balanced';
export type ObjectiveKey = 'time' | 'flaring' | 'balanced';
export const ALL_OBJECTIVES: ObjectiveKey[] = ['balanced', 'time', 'flaring'];

export interface EnergyMovedBayDetail {
  bay_id: string;
  pressure_bar: number | null;
  mwh: number;
}

export interface EnergyMovedLeg {
  from: string;
  to: string;
  to_name?: string;
  site_type?: string;
  is_consumer: boolean;
  bays_dropped: number;
  bays_picked: number;
  dropped_mwh: number;
  counted_toward_total: boolean;
  distance_km?: number;
  bay_details?: EnergyMovedBayDetail[];
}

export interface EnergyMovedRouteEntry {
  route_id: string;
  truck_id: string;
  moved_mwh: number;
  legs: EnergyMovedLeg[];
}

export interface EnergyMovedDebug {
  definition: string;
  totals: {
    gross_dropped_mwh?: number;
    net_delivered_mwh?: number;
  };
  per_route: EnergyMovedRouteEntry[];
  assumptions: string[];
}

export interface Recommendation {
  id: string;
  created_at: string;
  status: RecommendationStatus;
  objective_function: string;
  horizon_days: number;
  routes: Route[];
  total_distance_km: number;
  transport_cost_eur: number;
  handling_cost_eur: number;
  total_cost_eur: number;
  total_mwh_moved: number;
  eur_per_mwh: number | null;
  energy_moved_debug?: EnergyMovedDebug | null;
  sites_served: number;
  critical_sites_addressed: number;
  risk_reduction_score: number;
  explanation: string;
  warnings: string[];
  feasibility_level?: string;
  solution_feedback?: Array<{
    type: 'info' | 'warning' | 'error';
    code: string;
    truck_id?: string;
    message: string;
  }>;
  reason_code?: string;
  reason_message?: string;
  unreturned_containers: number;
  container_moves?: ContainerMove[];
  fill_sites_count?: number;
  transfer_hubs_count?: number;
  fill_sites_visited?: number;
  transfer_hubs_visited?: number;
  solution_risk_score?: number | null;
  flaring_exposure_hours?: number | null;
  end_of_horizon_imbalance?: number;
  infeasibility_diagnostics?: InfeasibilityDiagnostics | null;
}

// Structured diagnostics for infeasible recommendations
export interface InfeasibilityDiagConstraints {
  max_driver_hours: number;
  max_driver_min: number;
  service_time_min: number;
  avg_speed_kmph: number;
}

export interface InfeasibilityDiagTruck {
  truck_id: string;
  start_site_id: string;
  start_site_name: string;
  config_initial_load: number;
  actual_current_load: number;
  capacity: number;
  availability_days: number;
}

export interface InfeasibilityDiagTruckRow {
  truck_id: string;
  dist_km: number | null;
  drive_min: number | null;
  fits_direct: boolean | null;
}

export interface InfeasibilityDiagDemandSite {
  site_id: string;
  site_name: string;
  site_type: string;
  risk_level: string;
  hours_to_critical: number;
  bays_fixed: number;
  swappable_bays: number;
  trucks: InfeasibilityDiagTruckRow[];
}

export interface InfeasibilityDiagProducer {
  site_id: string;
  site_name: string;
  full_bays: number;
  near_full_bays: number;
  trucks: InfeasibilityDiagTruckRow[];
}

export interface InfeasibilityDiagFlowRoute {
  demand_site: string;
  route: string;
  total_drive_min: number | null;
  service_min: number;
  total_min: number | null;
  budget_min: number;
  feasible: boolean | null;
  note?: string;
}

export interface InfeasibilityDiagFlowEntry {
  truck_id: string;
  note?: string;
  nearest_producer?: string;
  drive_to_producer_min?: number;
  routes?: InfeasibilityDiagFlowRoute[];
}

export interface InfeasibilityDiagnostics {
  constraints: InfeasibilityDiagConstraints;
  trucks: InfeasibilityDiagTruck[];
  demand_sites: InfeasibilityDiagDemandSite[];
  producer_hubs: InfeasibilityDiagProducer[];
  flow_analysis: InfeasibilityDiagFlowEntry[];
  fallback_warnings: string[];
}

// Distance query types
export interface DistanceQueryResponse {
  from_site_id: string;
  to_site_id: string;
  distance_km: number;
  drive_time_hours: number | null;
}

// Fleet configuration types
export type StartMode = 'site' | 'custom' | 'in_transit' | 'random';
export type PointKind = 'site' | 'custom';

// Point - either a site or a custom point (NO coordinates)
export interface Point {
  kind: PointKind;
  site_id?: string;      // when kind='site'
  custom_id?: string;    // when kind='custom'
  label?: string;        // optional label for custom
}

// Truck start configuration
export interface TruckStart {
  kind: StartMode;
  // For site start
  site_id?: string;
  // For custom start
  custom_id?: string;
  label?: string;
  // For in_transit start
  from_point?: Point;
  to_point?: Point;
  distance_from_from_km?: number;
  total_edge_distance_km?: number;
}

// Manual distance entry for custom points
export interface ManualDistanceEntry {
  from_key: string;  // 'site:{siteId}' or 'custom:{customId}'
  to_key: string;
  distance_km: number;
}

// Custom point with coordinates for automatic routing
export interface CustomPoint {
  id: string;                                   // Stable ID e.g., "C1", "C2"
  label: string;                                // Editable label e.g., "Custom 1"
  latitude?: number;                            // WGS-84 latitude
  longitude?: number;                           // WGS-84 longitude
  routingStatus?: 'pending' | 'ok' | 'failed' | 'outside_coverage';
  distancesToSites: Record<string, number>;     // Legacy: siteId -> km (manual)
}

// Maximum number of custom points allowed
export const MAX_CUSTOM_POINTS = 7;

export interface FleetConfigTruck {
  truck_id: string;
  // Per-truck availability (working days) - replaces global horizon
  availability_days: number;
  // Initial containers already on truck (0–3)
  initial_load: number;
  // Start configuration
  start_mode: StartMode;
  start?: TruckStart;
  // Force end constraint (optional)
  force_end_enabled: boolean;
  force_end_day?: number;
  force_end_point?: Point;
}

export interface FleetConfig {
  trucks: FleetConfigTruck[];
}

// API Response types
export interface SystemState {
  sites_count: number;
  containers_count: number;
  trucks_count: number;
  mode: string;
  data_loaded_at: string | null;
  last_recommendation_at: string | null;
}

export interface SiteListResponse {
  sites: Site[];
  total: number;
  critical_count: number;
  warning_count: number;
}

export interface OptimalDaysTrialResult {
  days: number;
  score: number;
  feasible: boolean;
  total_cost_eur: number;
  containers_delivered: number;
}

export interface OptimalDaysResult {
  days_used: number;
  tested_days: OptimalDaysTrialResult[];
  explanation: {
    summary: string;
    details: string[];
  };
}

export interface RecommendationResponse {
  recommendation: Recommendation;
  computation_time_seconds: number;
  optimal_days_result?: OptimalDaysResult;
}

export interface MultiRecommendationResponse {
  recommendations: Record<ObjectiveKey, RecommendationResponse>;
  generated_at: string;
  computation_time_seconds: number;
}

// App state types
export type RecommendationState = 'idle' | 'computing' | 'ready' | 'approved' | 'infeasible';
export type TrafficMode = 'normal' | 'heavy';

// Route visibility layers - distinct types for clarity
export type RouteLayerType =
  | 'realRoutes'
  | 'activeRoutes'
  | 'recommendedRoutes';
export type SiteLayerType = 'producers' | 'consumers';
export type LayerType = SiteLayerType | RouteLayerType;

// Route classification for visual distinction
export type RouteCategory = 'real' | 'recommended' | 'simulation';

export interface CategorizedRoute extends Route {
  category: RouteCategory;
}
