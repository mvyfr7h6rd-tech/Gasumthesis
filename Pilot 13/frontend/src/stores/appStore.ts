import { create } from 'zustand';
import type {
  Site,
  Recommendation,
  RecommendationState,
  ObjectiveFunction,
  LayerType,
  RouteLayerType,
  TrafficMode,
  FleetConfigTruck,
  ManualDistanceEntry,
  CustomPoint,
  OptimalDaysResult,
} from '../types';
import { MAX_CUSTOM_POINTS } from '../types';
import * as api from '../api/client';

// Re-export CustomPoint for backwards compatibility
export type { CustomPoint } from '../types';

export const MAX_DAYS = 4;

// Validation error for missing info banner
export interface ValidationError {
  field: string;
  message: string;
}

// Override types
export type FlaringOverrides = Record<string, number>; // site_id -> flaring_cost_eur_mwh
export type ConsumptionOverrides = Record<string, number>; // site_id -> consumption_rate_kg_hour

interface AppState {
  // UI State
  selectedSiteId: string | null;
  hoveredSiteId: string | null;
  activeLayers: Set<LayerType>;
  visibleRouteIds: Set<string>;   // routes shown on map (eye toggle)
  focusedRouteId: string | null;  // route selected in detail panel
  objectiveFunction: ObjectiveFunction;
  recommendationState: RecommendationState;
  solveProgress: number; // 0–100, only meaningful while recommendationState === 'computing'
  trafficMode: TrafficMode;
  customSpeedKmh: number | null; // null = use preset
  containerSummaryOpen: boolean;
  fillRemainingTime: boolean;
  allowTransfers: boolean;
  // Constraint overrides (Active Constraints panel)
  constraintsMode: 'basic' | 'advanced';
  constraintOverrides: {
    costPerKm?: number;
    maxDriverHours?: number;
    swapTimeMin?: number;
    maxContainers?: number;
  };
  // Fleet configuration
  fleetConfig: FleetConfigTruck[];
  manualDistances: ManualDistanceEntry[];
  customPoints: CustomPoint[];
  // Rate overrides (persisted in localStorage)
  flaringOverrides: FlaringOverrides;
  consumptionOverrides: ConsumptionOverrides;
  // Validation
  validationErrors: ValidationError[];
  // Trucks available for selection
  availableTrucks: { id: string; name: string; home_site_id: string; start_resolved?: boolean }[];
  // Distance matrix cache
  distanceMatrix: Record<string, Record<string, number>> | null;
  // DEPRECATED
  horizonDays: number;

  // Optimal days
  optimalDaysMode: 'force' | 'optimize';
  optimalDaysResult: OptimalDaysResult | null;
  setOptimalDaysMode: (mode: 'force' | 'optimize') => void;

  // Data
  sites: Site[];
  sitesLoading: boolean;
  recommendation: Recommendation | null;
  computationTime: number | null;
  error: string | null;

  // Stats
  criticalCount: number;
  warningCount: number;

  // Computed getters
  getActiveSites: () => Site[];
  getActiveRecommendation: () => Recommendation | null;
  getActiveRecommendationState: () => RecommendationState;
  getVisibleRouteLayers: () => RouteLayerType[];
  // Effective value selectors (apply overrides)
  getEffectiveFlaringCost: (siteId: string) => number | null;
  getEffectiveConsumptionRate: (siteId: string) => number | null;

  // Actions
  setContainerSummaryOpen: (open: boolean) => void;
  setFillRemainingTime: (v: boolean) => void;
  setAllowTransfers: (v: boolean) => void;
  setConstraintsMode: (mode: 'basic' | 'advanced') => void;
  setConstraintOverrides: (overrides: AppState['constraintOverrides']) => void;
  selectSite: (siteId: string | null) => void;
  setHoveredSiteId: (id: string | null) => void;
  toggleLayer: (layer: LayerType) => void;
  toggleRouteVisibility: (routeId: string) => void;
  setFocusedRouteId: (id: string | null) => void;
  setObjectiveFunction: (obj: ObjectiveFunction) => void;
  setTrafficMode: (mode: TrafficMode) => void;
  setCustomSpeedKmh: (speed: number | null) => void;
  getEffectiveSpeedKmh: () => number;
  setHorizonDays: (days: number) => void;
  setFleetConfig: (config: FleetConfigTruck[]) => void;
  setManualDistances: (distances: ManualDistanceEntry[]) => void;
  setFlaringOverrides: (overrides: FlaringOverrides) => void;
  setConsumptionOverrides: (overrides: ConsumptionOverrides) => void;
  addManualDistance: (entry: ManualDistanceEntry) => void;
  removeManualDistance: (fromKey: string, toKey: string) => void;
  addCustomPoint: (label?: string) => string | null;  // returns custom_id or null if max reached
  removeCustomPoint: (customId: string) => { success: boolean; error?: string };
  renameCustomPoint: (customId: string, newLabel: string) => void;
  setCustomPointCoordinates: (customId: string, lat: number | undefined, lon: number | undefined) => void;
  setCustomPointRoutingStatus: (customId: string, status: CustomPoint['routingStatus']) => void;
  setCustomPointDistance: (customId: string, siteId: string, distanceKm: number) => void;
  removeCustomPointDistance: (customId: string, siteId: string) => void;
  getCustomPointDistanceToSite: (customId: string, siteId: string) => number | null;
  isCustomPointInUse: (customId: string) => { inUse: boolean; usedBy: string[] };
  loadTrucks: () => Promise<void>;
  loadDistanceMatrix: () => Promise<void>;
  updateTruckConfig: (truckId: string, updates: Partial<FleetConfigTruck>) => void;
  validateFleetConfig: () => ValidationError[];
  getDistance: (fromKey: string, toKey: string) => number | null;

  // Scenario Generator
  scenarioLoading: boolean;
  timeAdvanceLoading: boolean;
  simulationRestartLoading: boolean;
  generateScenario: (scenarioType: api.ScenarioType, seed?: number) => Promise<void>;
  advanceSimulationTime: (hours: number) => Promise<void>;
  restartSimulationState: () => Promise<void>;

  // Data Actions
  loadSites: (riskFilter?: string) => Promise<void>;
  generateRecommendation: () => Promise<void>;
  cancelRecommendation: () => void;
  approveRecommendation: (nextSteps?: boolean, horizonDays?: number) => Promise<void>;
  rejectRecommendation: () => Promise<void>;
  clearRecommendation: () => void;
  clearError: () => void;
}


// Module-level timer — lives outside the store so it survives across re-renders.
let _progressTimer: ReturnType<typeof setInterval> | null = null;
let _generateController: AbortController | null = null;

function _startSolveProgress(set: (s: Partial<AppState>) => void, get: () => AppState) {
  if (_progressTimer) clearInterval(_progressTimer);
  set({ solveProgress: 7 });
  _progressTimer = setInterval(() => {
    const current = get().solveProgress;
    let next = current;

    if (current < 38) {
      next = current + Math.random() * 4.5 + 2.5;
    } else if (current < 64) {
      next = current + Math.random() * 2.8 + 1.2;
    } else if (current < 78) {
      next = current + Math.random() * 1.6 + 0.7;
    } else if (current < 88) {
      next = current + Math.random() * 0.85 + 0.3;
    } else if (current < 92) {
      next = current + Math.random() * 0.45 + 0.12;
    }

    if (next !== current) {
      set({ solveProgress: Math.min(92, next) });
    }
  }, 450);
}

function _stopSolveProgress(set: (s: Partial<AppState>) => void) {
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
  set({ solveProgress: 100 });
}

export const useAppStore = create<AppState>((set, get) => ({
  // Initial UI State
  selectedSiteId: null,
  hoveredSiteId: null,
  activeLayers: new Set(['producers', 'consumers', 'recommendedRoutes', 'activeRoutes'] as LayerType[]),
  visibleRouteIds: new Set<string>(),
  focusedRouteId: null,
  objectiveFunction: 'balanced',
  recommendationState: 'idle',
  solveProgress: 0,
  trafficMode: 'normal',
  customSpeedKmh: (() => {
    const saved = localStorage.getItem('gasum-custom-speed');
    return saved ? Number(saved) : null;
  })(),
  horizonDays: 1,
  optimalDaysMode: 'force',
  optimalDaysResult: null,
  containerSummaryOpen: false,
  fillRemainingTime: false,
  allowTransfers: false,
  constraintsMode: 'basic',
  constraintOverrides: {},
  fleetConfig: [],
  manualDistances: [],
  customPoints: [],
  flaringOverrides: {},
  consumptionOverrides: {},
  validationErrors: [],
  availableTrucks: [],
  distanceMatrix: null,

  // Initial Data
  sites: [],
  sitesLoading: false,
  scenarioLoading: false,
  timeAdvanceLoading: false,
  simulationRestartLoading: false,
  recommendation: null,
  computationTime: null,
  error: null,
  criticalCount: 0,
  warningCount: 0,

  // Computed getters
  getActiveSites: () => get().sites,

  getActiveRecommendation: () => get().recommendation,

  getActiveRecommendationState: () => get().recommendationState,

  getVisibleRouteLayers: () => {
    const state = get();
    const layers: RouteLayerType[] = [];
    if (state.activeLayers.has('realRoutes')) layers.push('realRoutes');
    if (state.activeLayers.has('activeRoutes')) layers.push('activeRoutes');
    if (state.activeLayers.has('recommendedRoutes')) layers.push('recommendedRoutes');
    return layers;
  },

  // Effective value selectors (apply overrides on top of site defaults)
  getEffectiveFlaringCost: (siteId: string) => {
    const state = get();
    const site = state.sites.find((s) => s.id === siteId);
    if (!site || site.site_type !== 'production') return null;
    return state.flaringOverrides[siteId] ?? site.flaring_cost_eur_mwh;
  },

  getEffectiveConsumptionRate: (siteId: string) => {
    const state = get();
    const site = state.sites.find((s) => s.id === siteId);
    if (!site || site.site_type === 'production') return null;
    return state.consumptionOverrides[siteId] ?? site.consumption_rate_kg_hour;
  },

  // UI Actions
  setContainerSummaryOpen: (open) => set({ containerSummaryOpen: open }),
  setFillRemainingTime: (v) => set({ fillRemainingTime: v }),
  setAllowTransfers: (v) => set({ allowTransfers: v }),
  setConstraintsMode: (mode) => set({ constraintsMode: mode }),
  setConstraintOverrides: (overrides) => set({ constraintOverrides: overrides }),

  selectSite: (siteId) => set({ selectedSiteId: siteId }),
  setHoveredSiteId: (id) => set({ hoveredSiteId: id }),

  toggleLayer: (layer) =>
    set((state) => {
      const newLayers = new Set(state.activeLayers);
      if (newLayers.has(layer)) {
        newLayers.delete(layer);
      } else {
        newLayers.add(layer);
      }
      return { activeLayers: newLayers };
    }),

  toggleRouteVisibility: (routeId) =>
    set((state) => {
      const next = new Set(state.visibleRouteIds);
      if (next.has(routeId)) {
        next.delete(routeId);
      } else {
        next.add(routeId);
      }
      return { visibleRouteIds: next };
    }),

  setFocusedRouteId: (id) => set({ focusedRouteId: id }),

  setObjectiveFunction: (obj) => set({ objectiveFunction: obj }),

  setTrafficMode: (trafficMode) => set({ trafficMode, customSpeedKmh: null }),

  setCustomSpeedKmh: (speed) => {
    if (speed !== null) {
      localStorage.setItem('gasum-custom-speed', String(speed));
    } else {
      localStorage.removeItem('gasum-custom-speed');
    }
    set({ customSpeedKmh: speed });
  },

  getEffectiveSpeedKmh: () => {
    const state = get();
    if (state.customSpeedKmh !== null) return state.customSpeedKmh;
    return state.trafficMode === 'heavy' ? 60 : 80;
  },

  setHorizonDays: (horizonDays) => set({ horizonDays }),
  setOptimalDaysMode: (mode) =>
    set((state) => ({
      optimalDaysMode: mode,
      optimalDaysResult: mode === 'optimize' ? state.optimalDaysResult : null,
    })),

  setFleetConfig: (fleetConfig) => set({ fleetConfig, validationErrors: [] }),

  setManualDistances: (manualDistances) => set({ manualDistances }),

  setFlaringOverrides: (flaringOverrides) => {
    set({ flaringOverrides });
    // Trigger sites reload to recalculate risk with new overrides
    get().loadSites();
  },

  setConsumptionOverrides: (consumptionOverrides) => {
    set({ consumptionOverrides });
    // Trigger sites reload to recalculate risk with new overrides
    get().loadSites();
  },

  addManualDistance: (entry) => {
    set((state) => {
      // Remove existing entry for same pair if exists
      const filtered = state.manualDistances.filter(
        (d) => !(d.from_key === entry.from_key && d.to_key === entry.to_key)
      );
      return { manualDistances: [...filtered, entry] };
    });
  },

  removeManualDistance: (fromKey, toKey) => {
    set((state) => ({
      manualDistances: state.manualDistances.filter(
        (d) => !(d.from_key === fromKey && d.to_key === toKey)
      ),
    }));
  },

  addCustomPoint: (label) => {
    const state = get();
    // Check max limit
    if (state.customPoints.length >= MAX_CUSTOM_POINTS) {
      return null;
    }
    const id = `C${Date.now()}`;
    const pointLabel = label || `Custom ${state.customPoints.length + 1}`;
    set({
      customPoints: [...state.customPoints, { id, label: pointLabel, distancesToSites: {} }],
    });
    return id;
  },

  removeCustomPoint: (customId) => {
    const state = get();
    // Check if in use
    const usage = state.isCustomPointInUse(customId);
    if (usage.inUse) {
      return { success: false, error: `Cannot delete: ${customId} is in use by ${usage.usedBy.join(', ')}` };
    }
    set({
      customPoints: state.customPoints.filter((p) => p.id !== customId),
      // Also remove any manual distances involving this custom point
      manualDistances: state.manualDistances.filter(
        (d) => !d.from_key.includes(customId) && !d.to_key.includes(customId)
      ),
    });
    return { success: true };
  },

  renameCustomPoint: (customId, newLabel) => {
    set((state) => ({
      customPoints: state.customPoints.map((p) =>
        p.id === customId ? { ...p, label: newLabel } : p
      ),
    }));
  },

  setCustomPointCoordinates: (customId, lat, lon) => {
    set((state) => ({
      customPoints: state.customPoints.map((p) =>
        p.id === customId ? { ...p, latitude: lat, longitude: lon, routingStatus: 'pending' } : p
      ),
    }));
  },

  setCustomPointRoutingStatus: (customId, status) => {
    set((state) => ({
      customPoints: state.customPoints.map((p) =>
        p.id === customId ? { ...p, routingStatus: status } : p
      ),
    }));
  },

  setCustomPointDistance: (customId, siteId, distanceKm) => {
    set((state) => ({
      customPoints: state.customPoints.map((p) =>
        p.id === customId
          ? { ...p, distancesToSites: { ...p.distancesToSites, [siteId]: distanceKm } }
          : p
      ),
    }));
  },

  removeCustomPointDistance: (customId, siteId) => {
    set((state) => ({
      customPoints: state.customPoints.map((p) => {
        if (p.id !== customId) return p;
        const { [siteId]: _, ...rest } = p.distancesToSites;
        return { ...p, distancesToSites: rest };
      }),
    }));
  },

  getCustomPointDistanceToSite: (customId, siteId) => {
    const state = get();
    const cp = state.customPoints.find((p) => p.id === customId);
    if (!cp) return null;
    return cp.distancesToSites[siteId] ?? null;
  },

  isCustomPointInUse: (customId) => {
    const state = get();
    const usedBy: string[] = [];

    for (const tc of state.fleetConfig) {
      // Check start custom
      if (tc.start_mode === 'custom' && tc.start?.custom_id === customId) {
        usedBy.push(tc.truck_id);
        continue;
      }
      // Check in_transit endpoints
      if (tc.start_mode === 'in_transit') {
        if (tc.start?.from_point?.kind === 'custom' && tc.start.from_point.custom_id === customId) {
          usedBy.push(tc.truck_id);
          continue;
        }
        if (tc.start?.to_point?.kind === 'custom' && tc.start.to_point.custom_id === customId) {
          usedBy.push(tc.truck_id);
          continue;
        }
      }
      // Check force end
      if (tc.force_end_enabled && tc.force_end_point?.kind === 'custom' && tc.force_end_point.custom_id === customId) {
        usedBy.push(tc.truck_id);
      }
    }

    return { inUse: usedBy.length > 0, usedBy };
  },

  loadTrucks: async () => {
    try {
      const response = await api.getTrucks();
      const trucks = response.trucks.map((t) => ({
        id: t.id,
        name: t.id,
        home_site_id: t.home_site_id,
        start_resolved: t.start_resolved,
      }));
      set({ availableTrucks: trucks });

      // Initialize fleet config if empty
      const state = get();
      if (state.fleetConfig.length === 0 && trucks.length > 0) {
        const defaultFleet: FleetConfigTruck[] = trucks.map((t) => ({
          truck_id: t.id,
          availability_days: 1,
          initial_load: 0,
          start_mode: 'site' as const,
          start: { kind: 'site' as const, site_id: t.home_site_id },
          force_end_enabled: false,
        }));
        set({ fleetConfig: defaultFleet });
      }
    } catch (err) {
      console.error('Failed to load trucks:', err);
      set({ error: err instanceof Error ? err.message : 'Failed to load trucks' });
    }
  },

  loadDistanceMatrix: async () => {
    try {
      const response = await api.getDistanceMatrix();
      set({ distanceMatrix: response.matrix });
    } catch (err) {
      console.error('Failed to load distance matrix:', err);
      set({ error: err instanceof Error ? err.message : 'Failed to load distance matrix' });
    }
  },

  updateTruckConfig: (truckId, updates) => {
    set((state) => ({
      fleetConfig: state.fleetConfig.map((tc) =>
        tc.truck_id === truckId ? { ...tc, ...updates } : tc
      ),
      validationErrors: [], // Clear validation errors on update
    }));
  },

  getDistance: (fromKey, toKey) => {
    const state = get();

    // Check manual distances first (legacy support)
    const manual = state.manualDistances.find(
      (d) =>
        (d.from_key === fromKey && d.to_key === toKey) ||
        (d.from_key === toKey && d.to_key === fromKey)
    );
    if (manual) return manual.distance_km;

    // Check matrix for site-to-site
    if (fromKey.startsWith('site:') && toKey.startsWith('site:') && state.distanceMatrix) {
      const fromId = fromKey.replace('site:', '');
      const toId = toKey.replace('site:', '');
      if (state.distanceMatrix[fromId]?.[toId] !== undefined) {
        return state.distanceMatrix[fromId][toId];
      }
      if (state.distanceMatrix[toId]?.[fromId] !== undefined) {
        return state.distanceMatrix[toId][fromId];
      }
    }

    // Check custom point distances (custom <-> site)
    if (fromKey.startsWith('custom:') && toKey.startsWith('site:')) {
      const customId = fromKey.replace('custom:', '');
      const siteId = toKey.replace('site:', '');
      const cp = state.customPoints.find((p) => p.id === customId);
      if (cp && cp.distancesToSites[siteId] !== undefined) {
        return cp.distancesToSites[siteId];
      }
    }
    if (fromKey.startsWith('site:') && toKey.startsWith('custom:')) {
      const siteId = fromKey.replace('site:', '');
      const customId = toKey.replace('custom:', '');
      const cp = state.customPoints.find((p) => p.id === customId);
      if (cp && cp.distancesToSites[siteId] !== undefined) {
        return cp.distancesToSites[siteId];
      }
    }

    // Custom-to-custom requires manual entry (no auto-resolution)
    // Falls through to return null

    return null;
  },

  validateFleetConfig: () => {
    const state = get();
    const errors: ValidationError[] = [];

    for (const tc of state.fleetConfig) {
      const prefix = tc.truck_id;

      // Validate availability_days
      if (tc.availability_days < 1 || tc.availability_days > 4) {
        errors.push({
          field: `${prefix}.availability_days`,
          message: `${prefix}: Availability must be 1-4 days`,
        });
      }

      // Validate start configuration
      if (tc.start_mode === 'site') {
        if (!tc.start?.site_id) {
          errors.push({
            field: `${prefix}.start`,
            message: `${prefix}: Start site not selected`,
          });
        }
      } else if (tc.start_mode === 'custom') {
        if (!tc.start?.custom_id) {
          errors.push({
            field: `${prefix}.start`,
            message: `${prefix}: Custom start point not configured`,
          });
        }
      } else if (tc.start_mode === 'random') {
        if (!tc.start?.site_id) {
          errors.push({
            field: `${prefix}.start`,
            message: `${prefix}: Random start site not resolved`,
          });
        }
      } else if (tc.start_mode === 'in_transit') {
        if (!tc.start?.from_point || !tc.start?.to_point) {
          errors.push({
            field: `${prefix}.start`,
            message: `${prefix}: In-transit from/to points not configured`,
          });
        } else {
          // Validate from != to
          const fromKey = `${tc.start.from_point.kind}:${tc.start.from_point.site_id || tc.start.from_point.custom_id}`;
          const toKey = `${tc.start.to_point.kind}:${tc.start.to_point.site_id || tc.start.to_point.custom_id}`;
          if (fromKey === toKey) {
            errors.push({
              field: `${prefix}.start`,
              message: `${prefix}: From and To must be different`,
            });
          }

          // Validate total edge distance
          const edgeDist = tc.start.total_edge_distance_km ?? state.getDistance(fromKey, toKey);
          if (edgeDist === null || edgeDist === undefined) {
            errors.push({
              field: `${prefix}.start.total_edge_distance_km`,
              message: `${prefix}: Total edge distance required for in-transit`,
            });
          } else if (tc.start.distance_from_from_km !== undefined && tc.start.distance_from_from_km > edgeDist) {
            errors.push({
              field: `${prefix}.start.distance_from_from_km`,
              message: `${prefix}: Distance from origin exceeds total edge distance`,
            });
          }
        }
      }

      // Validate force end
      if (tc.force_end_enabled) {
        if (!tc.force_end_day) {
          errors.push({
            field: `${prefix}.force_end_day`,
            message: `${prefix}: Force end day not selected`,
          });
        } else if (state.optimalDaysMode !== 'optimize' && tc.force_end_day != null && tc.force_end_day > tc.availability_days) {
          errors.push({
            field: `${prefix}.force_end_day`,
            message: `${prefix}: Force end day exceeds availability`,
          });
        }
        if (!tc.force_end_point) {
          errors.push({
            field: `${prefix}.force_end_point`,
            message: `${prefix}: Force end location not selected`,
          });
        }
      }
    }

    // Helper to get custom point label
    const getCustomLabel = (customId: string): string => {
      const cp = state.customPoints.find((p) => p.id === customId);
      return cp?.label || customId;
    };

    // Helper to check if custom point has coordinates (auto-routed)
    const customHasCoords = (customId: string): boolean => {
      const cp = state.customPoints.find((p) => p.id === customId);
      return cp?.latitude != null && cp?.longitude != null;
    };

    // Helper to check if custom point has any distances defined
    const customHasAnyDistance = (customId: string): boolean => {
      const cp = state.customPoints.find((p) => p.id === customId);
      if (!cp) return false;
      return Object.keys(cp.distancesToSites).length > 0;
    };

    // Validate custom points used in fleet config
    for (const tc of state.fleetConfig) {
      const prefix = tc.truck_id;

      // Check custom start point has distances (skip if it has coordinates — auto-routed)
      if (tc.start_mode === 'custom' && tc.start?.custom_id) {
        if (!customHasCoords(tc.start.custom_id) && !customHasAnyDistance(tc.start.custom_id)) {
          const label = getCustomLabel(tc.start.custom_id);
          errors.push({
            field: `${prefix}.start.custom`,
            message: `${prefix}: ${label} has no distances defined`,
          });
        }
      }

      // Check in-transit custom points have relevant distances
      if (tc.start_mode === 'in_transit') {
        const fromPoint = tc.start?.from_point;
        const toPoint = tc.start?.to_point;

        // If from is custom and to is site, check custom has distance to that site
        // (skip if custom point has coordinates — will be auto-routed)
        if (fromPoint?.kind === 'custom' && fromPoint.custom_id && toPoint?.kind === 'site' && toPoint.site_id) {
          if (!customHasCoords(fromPoint.custom_id)) {
            const cp = state.customPoints.find((p) => p.id === fromPoint.custom_id);
            const hasDist = cp && cp.distancesToSites[toPoint.site_id] !== undefined;
            const hasManualEdge = tc.start?.total_edge_distance_km !== undefined;
            if (!hasDist && !hasManualEdge) {
              const fromLabel = getCustomLabel(fromPoint.custom_id);
              const toSite = state.sites.find((s) => s.id === toPoint.site_id);
              errors.push({
                field: `${prefix}.start.edge_distance`,
                message: `${prefix}: Missing distance ${fromLabel} ↔ ${toSite?.name || toPoint.site_id}`,
              });
            }
          }
        }

        // If to is custom and from is site, check custom has distance to that site
        if (toPoint?.kind === 'custom' && toPoint.custom_id && fromPoint?.kind === 'site' && fromPoint.site_id) {
          if (!customHasCoords(toPoint.custom_id)) {
            const cp = state.customPoints.find((p) => p.id === toPoint.custom_id);
            const hasDist = cp && cp.distancesToSites[fromPoint.site_id] !== undefined;
            const hasManualEdge = tc.start?.total_edge_distance_km !== undefined;
            if (!hasDist && !hasManualEdge) {
              const toLabel = getCustomLabel(toPoint.custom_id);
              const fromSite = state.sites.find((s) => s.id === fromPoint.site_id);
              errors.push({
                field: `${prefix}.start.edge_distance`,
                message: `${prefix}: Missing distance ${fromSite?.name || fromPoint.site_id} ↔ ${toLabel}`,
              });
            }
          }
        }

        // If both are custom, require manual total edge distance only if neither has coords
        if (fromPoint?.kind === 'custom' && toPoint?.kind === 'custom') {
          const fromHasCoords = fromPoint.custom_id ? customHasCoords(fromPoint.custom_id) : false;
          const toHasCoords = toPoint.custom_id ? customHasCoords(toPoint.custom_id) : false;
          if (!fromHasCoords || !toHasCoords) {
            const hasManualEdge = tc.start?.total_edge_distance_km !== undefined;
            if (!hasManualEdge) {
              const fromLabel = getCustomLabel(fromPoint.custom_id || '');
              const toLabel = getCustomLabel(toPoint.custom_id || '');
              errors.push({
                field: `${prefix}.start.total_edge_distance_km`,
                message: `${prefix}: ${fromLabel} ↔ ${toLabel} requires manual edge distance`,
              });
            }
          }
        }
      }

      // Check force end custom point has distances (skip if it has coordinates)
      if (tc.force_end_enabled && tc.force_end_point?.kind === 'custom' && tc.force_end_point.custom_id) {
        if (!customHasCoords(tc.force_end_point.custom_id) && !customHasAnyDistance(tc.force_end_point.custom_id)) {
          const label = getCustomLabel(tc.force_end_point.custom_id);
          errors.push({
            field: `${prefix}.force_end_point.custom`,
            message: `${prefix}: Force end ${label} has no distances defined`,
          });
        }
      }
    }

    set({ validationErrors: errors });
    return errors;
  },

  // Scenario Generator
  generateScenario: async (scenarioType: api.ScenarioType, seed?: number) => {
    set({ scenarioLoading: true, error: null });
    try {
      await api.generateScenario(scenarioType, seed);
      // Reload sites so the UI reflects the new bay pressures
      await get().loadSites();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to apply scenario' });
    } finally {
      set({ scenarioLoading: false });
    }
  },

  advanceSimulationTime: async (hours: number) => {
    set({ timeAdvanceLoading: true, error: null });
    try {
      await api.advanceSimulationTime(hours);
      // Any previous recommendation is now stale against the evolved system state.
      set({
        recommendation: null,
        recommendationState: 'idle',
        computationTime: null,
        visibleRouteIds: new Set<string>(),
        focusedRouteId: null,
      });
      await get().loadSites();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to advance simulation time' });
    } finally {
      set({ timeAdvanceLoading: false });
    }
  },

  restartSimulationState: async () => {
    set({ simulationRestartLoading: true, error: null });
    try {
      await api.restartSimulationState();
      set({
        recommendation: null,
        recommendationState: 'idle',
        computationTime: null,
        visibleRouteIds: new Set<string>(),
        focusedRouteId: null,
      });
      await get().loadSites();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to restart simulation state' });
    } finally {
      set({ simulationRestartLoading: false });
    }
  },

  // Data Actions
  loadSites: async (riskFilter?: string) => {
    if (get().sitesLoading) {
      console.log('[loadSites] Already in flight — skipped');
      return;
    }
    console.log('[loadSites] START');
    set({ sitesLoading: true, error: null });
    try {
      const { flaringOverrides, consumptionOverrides } = get();
      // Use the overrides-aware endpoint when overrides exist
      const response = await api.getSitesWithOverrides(
        riskFilter,
        Object.keys(flaringOverrides).length > 0 ? flaringOverrides : undefined,
        Object.keys(consumptionOverrides).length > 0 ? consumptionOverrides : undefined
      );
      console.log(`[loadSites] SUCCESS — ${response.sites?.length ?? 0} sites`);
      set({
        sites: response.sites,
        criticalCount: response.critical_count,
        warningCount: response.warning_count,
      });
    } catch (err) {
      console.error('[loadSites] ERROR:', err);
      set({ error: err instanceof Error ? err.message : 'Failed to load sites' });
    } finally {
      console.log('[loadSites] END');
      set({ sitesLoading: false });
    }
  },

  generateRecommendation: async () => {
    const state = get();
    const {
      objectiveFunction,
      trafficMode,
      fleetConfig,
      customPoints,
      flaringOverrides,
      consumptionOverrides,
      customSpeedKmh,
      constraintOverrides,
      optimalDaysMode,
      fillRemainingTime,
      allowTransfers,
    } = state;
    const effectiveSpeed = customSpeedKmh !== null ? customSpeedKmh : (trafficMode === 'heavy' ? 60 : 80);

    // horizon_days logic:
    // optimize → full range (MAX_DAYS); force → max truck availability; else → 1 day
    const effectiveHorizon =
      optimalDaysMode === 'optimize'
        ? MAX_DAYS
        : optimalDaysMode === 'force'
          ? fleetConfig.length > 0
            ? Math.max(...fleetConfig.map((tc) => tc.availability_days))
            : 1
          : 1;

    console.log('[DEBUG] horizon_days:', effectiveHorizon, '| mode:', optimalDaysMode);

    _generateController = new AbortController();
    set({ recommendationState: 'computing', error: null });
    _startSolveProgress(set, get);

    // Filter custom points that don't have coordinates yet
    const validCustomPoints = customPoints.filter(
      (cp) => cp.latitude !== undefined && cp.latitude !== null &&
               cp.longitude !== undefined && cp.longitude !== null
    );
    const invalidCustomIds = new Set(
      customPoints.filter(
        (cp) => cp.latitude === undefined || cp.latitude === null ||
                 cp.longitude === undefined || cp.longitude === null
      ).map((cp) => cp.id)
    );
    if (invalidCustomIds.size > 0) {
      console.warn('[appStore] Filtered custom points without coordinates:', [...invalidCustomIds]);
    }

    // Remap fleet trucks that reference filtered-out custom points back to their home site
    const truckHomeMap = Object.fromEntries(
      state.availableTrucks.map((t) => [t.id, t.home_site_id])
    );
    const remappedFleet = fleetConfig.map((tc) => {
      const homeSiteId = truckHomeMap[tc.truck_id];
      let updated = { ...tc };
      // Remap start
      if (
        updated.start_mode === 'custom' &&
        updated.start?.kind === 'custom' &&
        updated.start.custom_id &&
        invalidCustomIds.has(updated.start.custom_id)
      ) {
        console.warn(`[appStore] Truck ${tc.truck_id} start custom point ${updated.start.custom_id} invalid — falling back to home site ${homeSiteId}`);
        updated = {
          ...updated,
          start_mode: 'site',
          start: { kind: 'site', site_id: homeSiteId },
        };
      }
      // Remap force_end_point
      if (
        updated.force_end_enabled &&
        updated.force_end_point?.kind === 'custom' &&
        updated.force_end_point.custom_id &&
        invalidCustomIds.has(updated.force_end_point.custom_id)
      ) {
        console.warn(`[appStore] Truck ${tc.truck_id} force_end custom point ${updated.force_end_point.custom_id} invalid — disabling force end`);
        updated = {
          ...updated,
          force_end_enabled: false,
          force_end_point: undefined,
          force_end_day: undefined,
        };
      }
      return updated;
    });

    try {
      const response = await api.generateRecommendation(
        objectiveFunction,
        undefined,
        undefined,
        30,
        trafficMode,
        effectiveHorizon,
        remappedFleet.length > 0 ? remappedFleet : undefined,
        validCustomPoints.length > 0 ? validCustomPoints : undefined,
        Object.keys(flaringOverrides).length > 0 ? flaringOverrides : undefined,
        Object.keys(consumptionOverrides).length > 0 ? consumptionOverrides : undefined,
        effectiveSpeed,
        fillRemainingTime,
        allowTransfers,
        constraintOverrides.costPerKm,
        constraintOverrides.maxDriverHours,
        constraintOverrides.swapTimeMin,
        constraintOverrides.maxContainers,
        optimalDaysMode === 'optimize',
        optimalDaysMode === 'force',
        _generateController?.signal,
      );

      _stopSolveProgress(set);
      const rec = response.recommendation;
      const recStatus = rec?.status === 'infeasible' ? 'infeasible' : 'ready';

      set({
        recommendation: rec || null,
        computationTime: response.computation_time_seconds,
        optimalDaysResult: optimalDaysMode === 'optimize'
          ? (response.optimal_days_result ?? null)
          : null,
        recommendationState: recStatus,
        solveProgress: 0,
        visibleRouteIds: new Set((rec?.routes ?? []).map((r) => r.id)),
        focusedRouteId: rec?.routes?.[0]?.id ?? null,
      });
    } catch (err) {
      if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
      _generateController = null;
      // Cancelled by user — reset silently without showing an error
      if (err instanceof Error && (err.name === 'AbortError' || err.name === 'CanceledError')) {
        set({ recommendationState: 'idle', solveProgress: 0 });
        return;
      }
      // Solver returned no feasible routes — show as infeasible, not an error
      const axiosErr = err as { response?: { data?: { detail?: { code?: string; message?: string } } } };
      if (axiosErr.response?.data?.detail?.code === 'NO_RECOMMENDATION') {
        set({ recommendationState: 'infeasible', solveProgress: 0, error: null });
        return;
      }
      set({
        error: err instanceof Error ? err.message : 'Failed to generate recommendation',
        recommendationState: 'idle',
        solveProgress: 0,
      });
    }
  },

  cancelRecommendation: () => {
    if (_generateController) {
      _generateController.abort();
      _generateController = null;
    }
    if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
    set({ recommendationState: 'idle', solveProgress: 0 });
  },

  approveRecommendation: async (nextSteps = false, horizonDays?: number) => {
    const state = get();
    const { recommendation, fleetConfig } = state;
    if (!recommendation) return;

    // Pass the current UI fleet config for next-steps planning so start/force_end
    // constraints reflect what the user has configured, not the stale stored copy.
    const nextFleet = nextSteps && fleetConfig.length > 0 ? fleetConfig : undefined;

    try {
      const result = await api.approveRecommendation(recommendation.id, nextSteps, horizonDays, nextFleet);

      if (nextSteps && result.new_recommendation) {
        // Apply + replan: load fresh site data and show the new recommendation
        await get().loadSites();
        set({
          recommendationState: 'ready',
          recommendation: result.new_recommendation as unknown as typeof recommendation,
        });
      } else {
        // Apply only: refresh sites and mark as approved
        await get().loadSites();
        set({
          recommendationState: 'approved',
          recommendation: { ...recommendation, status: 'executed' },
        });
      }
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status: number } };
      if (axiosErr.response?.status === 404) {
        set({
          error: 'This recommendation is no longer available. Please generate a new one.',
          recommendation: null,
          recommendationState: 'idle',
        });
      } else if (axiosErr.response?.status === 409) {
        set({
          error: 'This recommendation has already been applied.',
        });
      } else {
        set({
          error: err instanceof Error ? err.message : 'Failed to approve recommendation',
        });
      }
    }
  },

  rejectRecommendation: async () => {
    const state = get();
    const { recommendation } = state;
    if (!recommendation) return;

    try {
      await api.rejectRecommendation(recommendation.id);
      set({
        recommendationState: 'idle',
        recommendation: null,
      });
    } catch (err: unknown) {
      // Check for 404 (recommendation not found)
      const axiosErr = err as { response?: { status: number } };
      if (axiosErr.response?.status === 404) {
        // Recommendation not found - just clear it
        set({
          recommendation: null,
          recommendationState: 'idle',
          error: 'This recommendation is no longer available.',
        });
      } else {
        set({
          error: err instanceof Error ? err.message : 'Failed to reject recommendation',
        });
      }
    }
  },

  clearRecommendation: () => {
    set({
      recommendation: null,
      recommendationState: 'idle',
      computationTime: null,
      visibleRouteIds: new Set<string>(),
      focusedRouteId: null,
    });
  },

  clearError: () => set({ error: null }),
}));
