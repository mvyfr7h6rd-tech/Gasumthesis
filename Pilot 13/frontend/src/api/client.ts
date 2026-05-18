import axios from 'axios';
import type {
  SystemState,
  SiteListResponse,
  Site,
  SitePatchRequest,
  Container,
  Truck,
  Recommendation,
  RecommendationResponse,
  MultiRecommendationResponse,
  ObjectiveFunction,
  DistanceQueryResponse,
  FleetConfigTruck,
  CustomPoint,
} from '../types';
import { getDeepSeekApiKey } from '../utils/deepseek';

function resolveApiBaseUrl(): string {
  if (import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL;
  }

  // Vite dev server can proxy /api, but packaged desktop builds need
  // a direct backend URL because there is no dev proxy in front of them.
  if (typeof window !== 'undefined') {
    const protocol = window.location.protocol;
    if (protocol === 'http:' || protocol === 'https:') {
      return '/api';
    }
  }

  return 'http://127.0.0.1:8000/api';
}

const API_BASE_URL = resolveApiBaseUrl();

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 15000, // 15 s — prevents hung requests from freezing the UI indefinitely
});

const RETRYABLE_STATUSES = new Set([500, 502, 503, 504]);

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableError(error: unknown): boolean {
  if (!axios.isAxiosError(error)) {
    return false;
  }
  const status = error.response?.status;
  if (status !== undefined) {
    return RETRYABLE_STATUSES.has(status);
  }
  return Boolean(error.code);
}

async function withRetry<T>(request: () => Promise<T>, attempts: number = 3): Promise<T> {
  let lastError: unknown;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await request();
    } catch (error) {
      lastError = error;
      if (attempt >= attempts || !isRetryableError(error)) {
        throw error;
      }
      await sleep(250 * attempt);
    }
  }

  throw lastError instanceof Error ? lastError : new Error('Request failed');
}

// System state
export async function getSystemState(): Promise<SystemState> {
  const response = await withRetry(() => api.get<SystemState>('/state'));
  return response.data;
}

// Sites
export async function getSites(riskFilter?: string): Promise<SiteListResponse> {
  const params = riskFilter ? { risk_filter: riskFilter } : {};
  const response = await withRetry(() => api.get<SiteListResponse>('/sites', { params }));
  return response.data;
}

// Sites with rate overrides (recalculates risk with overrides applied)
export async function getSitesWithOverrides(
  riskFilter?: string,
  flaringOverrides?: FlaringOverrides,
  consumptionOverrides?: ConsumptionOverrides
): Promise<SiteListResponse> {
  const hasOverrides =
    (flaringOverrides && Object.keys(flaringOverrides).length > 0) ||
    (consumptionOverrides && Object.keys(consumptionOverrides).length > 0);

  // If no overrides, use the simpler GET endpoint
  if (!hasOverrides) {
    return getSites(riskFilter);
  }

  const payload = {
    risk_filter: riskFilter || null,
    sort_by: 'risk_score',
    rate_overrides: {
      flaring_costs: flaringOverrides || {},
      consumption_rates: consumptionOverrides || {},
    },
  };

  const response = await withRetry(() => api.post<SiteListResponse>('/sites/query', payload));
  return response.data;
}

export async function getSiteDetail(siteId: string): Promise<{
  site: Site;
  containers: Container[];
  risk_level: string;
  hours_to_critical: number;
  risk_score: number;
  risk_explanation: string;
}> {
  const response = await withRetry(() => api.get(`/sites/${siteId}`));
  return response.data;
}

// PATCH site (for manual editing in real mode)
export async function patchSite(siteId: string, updates: SitePatchRequest): Promise<Site> {
  const response = await api.patch<Site>(`/sites/${siteId}`, updates);
  return response.data;
}

// Containers
export async function getContainers(siteId?: string): Promise<{
  containers: Container[];
  total: number;
  full_count: number;
  empty_count: number;
  in_transit_count: number;
}> {
  const params = siteId ? { site_id: siteId } : {};
  const response = await api.get('/containers', { params });
  return response.data;
}

// Trucks
export async function getTrucks(): Promise<{
  trucks: Truck[];
  total: number;
  available_count: number;
}> {
  const response = await withRetry(() => api.get('/trucks'));
  return response.data;
}

export async function getDistanceMatrix(): Promise<{
  matrix: Record<string, Record<string, number>>;
  site_count: number;
}> {
  const response = await withRetry(() => api.get('/distances'));
  return response.data;
}

// Rate override types
export type FlaringOverrides = Record<string, number>; // site_id -> flaring_cost_eur_mwh
export type ConsumptionOverrides = Record<string, number>; // site_id -> consumption_rate_kg_hour

// Scenario Generator
export type ScenarioType =
  | 'balanced'
  | 'high_demand'
  | 'high_production'
  | 'capacity_crisis';

export interface ScenarioResponse {
  success: boolean;
  scenario_type: string;
  description: string;
  sites_updated: number;
  applied_tiers: Record<string, string>;
}

export interface AdvanceTimeResponse {
  success: boolean;
  advanced_hours: number;
  message: string;
  total_sites: number;
  critical_count: number;
  warning_count: number;
}

export interface RestartSimulationResponse {
  success: boolean;
  message: string;
  restored: boolean;
  total_sites: number;
  critical_count: number;
  warning_count: number;
}

export async function generateScenario(scenarioType: ScenarioType, seed?: number): Promise<ScenarioResponse> {
  const response = await api.post<ScenarioResponse>('/generate_scenario', {
    scenario_type: scenarioType,
    ...(seed !== undefined ? { seed } : {}),
  });
  return response.data;
}

export async function advanceSimulationTime(hours: number): Promise<AdvanceTimeResponse> {
  const response = await api.post<AdvanceTimeResponse>('/advance_time', { hours });
  return response.data;
}

export async function restartSimulationState(): Promise<RestartSimulationResponse> {
  const response = await api.post<RestartSimulationResponse>('/simulation/restart');
  return response.data;
}

// Recommendations
export async function generateRecommendation(
  objective: ObjectiveFunction = 'balanced',
  truckIds?: string[],
  siteIds?: string[],
  maxSearchSeconds: number = 30,
  trafficMode: 'normal' | 'heavy' = 'normal',
  horizonDays: number = 1,
  fleet?: FleetConfigTruck[],
  customPoints?: CustomPoint[],
  flaringOverrides?: FlaringOverrides,
  consumptionOverrides?: ConsumptionOverrides,
  avgSpeedKmh?: number,
  fillRemainingTime?: boolean,
  allowTransfers?: boolean,
  costPerKmOverride?: number,
  maxDriverHoursOverride?: number,
  swapTimeMinOverride?: number,
  maxContainersOverride?: number,
  optimalDays?: boolean,
  forceExactDays?: boolean,
  signal?: AbortSignal,
): Promise<RecommendationResponse> {
  // Convert custom points to backend schema — drop any without coordinates to avoid 422
  const validCPs = customPoints?.filter(
    (cp) => cp.latitude !== undefined && cp.latitude !== null &&
             cp.longitude !== undefined && cp.longitude !== null
  ) ?? [];
  if (customPoints && validCPs.length < customPoints.length) {
    const dropped = customPoints.filter(
      (cp) => cp.latitude === undefined || cp.latitude === null ||
               cp.longitude === undefined || cp.longitude === null
    ).map((cp) => cp.id);
    console.warn('[API] Dropped custom points without coordinates:', dropped);
  }
  const customPointsPayload = validCPs.map((cp) => ({
    id: cp.id,
    label: cp.label,
    latitude: cp.latitude as number,
    longitude: cp.longitude as number,
    distances_to_sites: cp.distancesToSites,
  }));

  // Build rate overrides if present
  const rateOverrides =
    (flaringOverrides && Object.keys(flaringOverrides).length > 0) ||
    (consumptionOverrides && Object.keys(consumptionOverrides).length > 0)
      ? {
          flaring_costs: flaringOverrides || {},
          consumption_rates: consumptionOverrides || {},
        }
      : undefined;

  const normalizedFleet = fleet?.map((t) =>
    t.start_mode === 'random' ? { ...t, start_mode: 'site' as const } : t
  );

  const payload = {
    objective,
    truck_ids: truckIds,
    site_ids: siteIds,
    max_search_seconds: maxSearchSeconds,
    traffic_mode: trafficMode,
    avg_speed_kmh: avgSpeedKmh,
    horizon_days: horizonDays,
    fleet: normalizedFleet ? { trucks: normalizedFleet } : undefined,
    custom_points: customPointsPayload.length > 0 ? customPointsPayload : undefined,
    rate_overrides: rateOverrides,
    fill_remaining_time: fillRemainingTime ?? false,
    allow_transfers: allowTransfers ?? false,
    cost_per_km_override: costPerKmOverride,
    max_driver_hours_override: maxDriverHoursOverride,
    swap_time_min_override: swapTimeMinOverride,
    max_containers_override: maxContainersOverride,
    optimal_days: optimalDays ?? false,
    force_exact_days: forceExactDays ?? false,
    ai_api_key: getDeepSeekApiKey() || undefined,
  };

  // Log request payload for debugging
  console.log('[API] POST /recommend payload:', JSON.stringify(payload, null, 2));

  try {
    // Multi-day force/optimize modes can legitimately exceed the standard 120 s timeout
    // because they fan out into several day-level solves and internal repair passes.
    const requestTimeout = (optimalDays || forceExactDays) ? 300000 : 120000;
    const response = await api.post<RecommendationResponse>('/recommend', payload, { timeout: requestTimeout, signal });
    console.log('[API] POST /recommend success:', {
      routes: response.data.recommendation.routes.length,
      total_cost_eur: response.data.recommendation.total_cost_eur,
      total_mwh_moved: response.data.recommendation.total_mwh_moved,
      eur_per_mwh: response.data.recommendation.eur_per_mwh,
    });
    return response.data;
  } catch (error: unknown) {
    // Log error details
    if (error && typeof error === 'object' && 'response' in error) {
      const axiosError = error as { response?: { status: number; data: unknown } };
      console.error('[API] POST /recommend error:', {
        status: axiosError.response?.status,
        data: axiosError.response?.data,
      });
    }
    throw error;
  }
}

// Multi-objective recommendations (all 4 objectives in one call)
export async function generateMultiRecommendation(
  truckIds?: string[],
  siteIds?: string[],
  maxSearchSeconds: number = 30,
  trafficMode: 'normal' | 'heavy' = 'normal',
  horizonDays: number = 1,
  fleet?: FleetConfigTruck[],
  customPoints?: CustomPoint[],
  flaringOverrides?: FlaringOverrides,
  consumptionOverrides?: ConsumptionOverrides,
  avgSpeedKmh?: number
): Promise<MultiRecommendationResponse> {
  const validCPsMulti = customPoints?.filter(
    (cp) => cp.latitude !== undefined && cp.latitude !== null &&
             cp.longitude !== undefined && cp.longitude !== null
  ) ?? [];
  if (customPoints && validCPsMulti.length < customPoints.length) {
    const dropped = customPoints.filter(
      (cp) => cp.latitude === undefined || cp.latitude === null ||
               cp.longitude === undefined || cp.longitude === null
    ).map((cp) => cp.id);
    console.warn('[API] Dropped custom points without coordinates (multi):', dropped);
  }
  const customPointsPayload = validCPsMulti.map((cp) => ({
    id: cp.id,
    label: cp.label,
    latitude: cp.latitude as number,
    longitude: cp.longitude as number,
    distances_to_sites: cp.distancesToSites,
  }));

  const rateOverrides =
    (flaringOverrides && Object.keys(flaringOverrides).length > 0) ||
    (consumptionOverrides && Object.keys(consumptionOverrides).length > 0)
      ? {
          flaring_costs: flaringOverrides || {},
          consumption_rates: consumptionOverrides || {},
        }
      : undefined;

  const payload = {
    truck_ids: truckIds,
    site_ids: siteIds,
    max_search_seconds: maxSearchSeconds,
    traffic_mode: trafficMode,
    avg_speed_kmh: avgSpeedKmh,
    horizon_days: horizonDays,
    fleet: fleet ? { trucks: fleet } : undefined,
    custom_points: customPointsPayload.length > 0 ? customPointsPayload : undefined,
    rate_overrides: rateOverrides,
  };

  console.log('[API] POST /recommend_multi payload:', JSON.stringify(payload, null, 2));

  try {
    // 4 objectives × 120 s each = 480 s worst case; in practice 4 × 88 s ≈ 352 s
    const response = await api.post<MultiRecommendationResponse>('/recommend_multi', payload, { timeout: 480000 });
    console.log('[API] POST /recommend_multi success:', {
      objectives: Object.keys(response.data.recommendations),
      computation_time: response.data.computation_time_seconds,
    });
    return response.data;
  } catch (error: unknown) {
    if (error && typeof error === 'object' && 'response' in error) {
      const axiosError = error as { response?: { status: number; data: unknown } };
      console.error('[API] POST /recommend_multi error:', {
        status: axiosError.response?.status,
        data: JSON.stringify(axiosError.response?.data, null, 2),
      });
      console.error('[API] POST /recommend_multi sent payload:', JSON.stringify(payload, null, 2));
    }
    throw error;
  }
}

// Distance query
const distanceCache = new Map<string, DistanceQueryResponse>();

export async function getDistance(fromSiteId: string, toSiteId: string): Promise<DistanceQueryResponse> {
  const cacheKey = `${fromSiteId}|${toSiteId}`;

  // Check cache first
  if (distanceCache.has(cacheKey)) {
    return distanceCache.get(cacheKey)!;
  }

  const response = await api.get<DistanceQueryResponse>('/distance', {
    params: { from: fromSiteId, to: toSiteId },
  });

  // Cache the result
  distanceCache.set(cacheKey, response.data);

  return response.data;
}

export async function approveRecommendation(
  recommendationId: string,
  nextSteps: boolean = false,
  horizonDays?: number,
  fleet?: FleetConfigTruck[],
): Promise<{
  success: boolean;
  recommendation_id: string;
  new_status: string;
  message: string;
  new_recommendation?: Record<string, unknown>;
}> {
  const response = await api.post(`/approve/${recommendationId}`, {
    next_steps: nextSteps,
    horizon_days: nextSteps ? horizonDays : undefined,
    fleet: nextSteps && fleet && fleet.length > 0 ? { trucks: fleet } : undefined,
  }, { timeout: nextSteps ? 60000 : 30000 });
  return response.data;
}

export async function rejectRecommendation(recommendationId: string): Promise<{
  success: boolean;
  recommendation_id: string;
  new_status: string;
  message: string;
}> {
  const response = await api.post(`/reject/${recommendationId}`);
  return response.data;
}

export async function getRecommendationHistory(limit: number = 10): Promise<{
  recommendations: Recommendation[];
  total: number;
}> {
  const response = await api.get('/history', { params: { limit } });
  return response.data;
}

// Save recommendation full detail to analytics DB (route stops, container serials, site snapshots)
// No file download — data goes into database/analytics.db
export async function exportRecommendationExcel(recommendationId: string): Promise<{
  route_stops_saved: number;
  container_moves_saved: number;
  sites_snapshot_saved: number;
}> {
  const response = await api.post(`/export/${recommendationId}`);
  return response.data;
}

// Export full analytics history as Excel
export async function exportAnalyticsExcel(): Promise<void> {
  const today = new Date().toISOString().split('T')[0];
  const response = await api.get('/analytics/export', { responseType: 'blob' });
  const url = window.URL.createObjectURL(new Blob([response.data]));
  const a = document.createElement('a');
  a.href = url;
  a.download = `GASUM_Analytics_${today}.xlsx`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  window.URL.revokeObjectURL(url);
}

// Add a manual real-life operation to analytics DB
export async function addManualOperation(data: {
  description: string;
  operation_date?: string;
  action_taken?: string;
  outcome?: string;
  sites_involved?: string[];
  cost_eur?: number;
  distance_km?: number;
  notes?: string;
}): Promise<{ status: string; id: number }> {
  const response = await api.post('/analytics/manual-operation', data);
  return response.data;
}

// Configuration
export async function getConfig(): Promise<{
  config: {
    avg_speed_kmph: number;
    max_driver_hours: number;
    swap_time_hours: number;
    cost_per_km_eur: number;
    handling_fee_eur: number;
    contingency_multiplier: number;
  };
}> {
  const response = await api.get('/config');
  return response.data;
}

// File uploads
export async function uploadSites(file: File): Promise<{
  success: boolean;
  message: string;
  items_updated: number;
}> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await api.post('/upload/sites', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

export async function uploadContainers(file: File): Promise<{
  success: boolean;
  message: string;
  items_updated: number;
}> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await api.post('/upload/containers', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

export async function uploadConfig(file: File): Promise<{
  success: boolean;
  message: string;
  items_updated: number;
}> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await api.post('/upload/config', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

export default api;
