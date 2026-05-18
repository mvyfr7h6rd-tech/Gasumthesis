import React, { useEffect, useMemo, useState } from 'react';
import { useAppStore } from '../../stores/appStore';
import type { FleetConfigTruck, Point, TruckStart, StartMode, CustomPoint } from '../../types';
import { QuickAddDistance } from './QuickAddDistance';
import { CustomPointsPanel } from './CustomPointsPanel';

/** Pure helper: clamp a force-end day to the truck's working-day range. */
export function clampForceEndDay(day: number, workingDays: number): number {
  return Math.min(Math.max(1, day), Math.max(1, workingDays));
}

// Dev-mode regression guard — runs once when the module loads.
if (import.meta.env.DEV) {
  console.assert(clampForceEndDay(3, 2) === 2, 'clampForceEndDay: must clamp 3 → 2 when workingDays=2');
  console.assert(clampForceEndDay(1, 3) === 1, 'clampForceEndDay: must not clamp 1 within workingDays=3');
  console.assert(clampForceEndDay(4, 4) === 4, 'clampForceEndDay: 4 within 4 unchanged');
  // Day N must be valid: force_end_day=N with workingDays=N must stay N (not clamp to N-1).
  console.assert(clampForceEndDay(3, 3) === 3, 'clampForceEndDay: day N must be valid when workingDays=N');
}

interface PointSelectorProps {
  value: Point | undefined;
  onChange: (point: Point) => void;
  sites: { id: string; name: string }[];
  customPoints: CustomPoint[];
  onAddCustom: () => string | null;
  label: string;
}

const PointSelector: React.FC<PointSelectorProps> = ({
  value,
  onChange,
  sites,
  customPoints,
  onAddCustom,
  label,
}) => {
  const kind = value?.kind || 'site';

  return (
    <div className="space-y-2">
      <label className="block text-xs text-slate-500">{label}</label>
      <div className="flex gap-2 mb-1">
        <button
          onClick={() => onChange({ kind: 'site', site_id: sites[0]?.id })}
          className={`px-2 py-1 text-xs rounded ${
            kind === 'site' ? 'bg-blue-600 text-white' : 'bg-slate-700 text-slate-300'
          }`}
        >
          Site
        </button>
        <button
          onClick={() => {
            const customId = customPoints[0]?.id || onAddCustom();
            if (customId) {
              onChange({ kind: 'custom', custom_id: customId, label: customPoints[0]?.label });
            }
          }}
          className={`px-2 py-1 text-xs rounded ${
            kind === 'custom' ? 'bg-amber-600 text-white' : 'bg-slate-700 text-slate-300'
          }`}
        >
          Custom
        </button>
      </div>
      {kind === 'site' ? (
        <select
          value={value?.site_id || ''}
          onChange={(e) => onChange({ kind: 'site', site_id: e.target.value })}
          className="w-full p-1.5 bg-slate-700 border border-slate-600 rounded text-sm"
        >
          <option value="">Select site...</option>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
      ) : (
        <select
          value={value?.custom_id || ''}
          onChange={(e) => {
            const cp = customPoints.find((p) => p.id === e.target.value);
            onChange({ kind: 'custom', custom_id: e.target.value, label: cp?.label });
          }}
          className="w-full p-1.5 bg-slate-700 border border-slate-600 rounded text-sm"
        >
          <option value="">Select custom point...</option>
          {customPoints.map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      )}
    </div>
  );
};

interface InlineQuickAddProps {
  customId: string;
  customPoints: CustomPoint[];
  sites: { id: string; name: string }[];
  setCustomPointDistance: (customId: string, siteId: string, km: number) => void;
  removeCustomPointDistance: (customId: string, siteId: string) => void;
  suggestedSiteId?: string;
}

const InlineQuickAdd: React.FC<InlineQuickAddProps> = ({
  customId,
  customPoints,
  sites,
  setCustomPointDistance,
  removeCustomPointDistance,
  suggestedSiteId,
}) => {
  const cp = customPoints.find((p) => p.id === customId);
  if (!cp) return null;

  // If custom point has coordinates, it's auto-routed — no manual distances needed
  if (cp.latitude != null && cp.longitude != null) {
    return (
      <div className="text-xs text-slate-500 bg-slate-700/50 rounded p-2">
        {cp.label} — auto-routed via coordinates ({cp.latitude.toFixed(4)}, {cp.longitude.toFixed(4)})
      </div>
    );
  }

  return (
    <QuickAddDistance
      customId={customId}
      customLabel={cp.label}
      sites={sites}
      existingDistances={cp.distancesToSites}
      onAddDistance={(siteId, km) => setCustomPointDistance(customId, siteId, km)}
      onRemoveDistance={(siteId) => removeCustomPointDistance(customId, siteId)}
      onEditDistance={(siteId, km) => setCustomPointDistance(customId, siteId, km)}
      suggestedSiteId={suggestedSiteId}
    />
  );
};

interface TruckConfigCardProps {
  config: FleetConfigTruck;
  sites: { id: string; name: string }[];
  customPoints: CustomPoint[];
  onUpdate: (updates: Partial<FleetConfigTruck>) => void;
  onRemove: () => void;
  onAddCustom: () => string | null;
  getDistance: (fromKey: string, toKey: string) => number | null;
  setCustomPointDistance: (customId: string, siteId: string, km: number) => void;
  removeCustomPointDistance: (customId: string, siteId: string) => void;
  optimalDaysMode: 'force' | 'optimize';
}

const TruckConfigCard: React.FC<TruckConfigCardProps> = ({
  config,
  sites,
  customPoints,
  onUpdate,
  onRemove,
  onAddCustom,
  getDistance,
  setCustomPointDistance,
  removeCustomPointDistance,
  optimalDaysMode,
}) => {
  const [expanded, setExpanded] = useState(false);
  const modeCardClass = optimalDaysMode === 'force'
    ? 'bg-slate-800 border-[3px] border-blue-700/85'
    : 'bg-slate-800 border-[3px] border-cyan-500/80';
  const modeHeaderHoverClass = optimalDaysMode === 'force'
    ? 'hover:bg-blue-950/20'
    : 'hover:bg-cyan-950/20';
  const dayBadgeClass = optimalDaysMode === 'force'
    ? 'text-blue-200 bg-blue-950/50'
    : 'text-cyan-200 bg-cyan-950/40';

  const workingDays = config.availability_days;

  // In optimize mode always expose the full 1–4 range so the user is not
  // artificially limited before the solver has decided how many days to use.
  const forceEndDayRange = optimalDaysMode === 'optimize' ? 4 : workingDays;
  const dayOptions = useMemo(
    () => Array.from({ length: forceEndDayRange }, (_, i) => i + 1),
    [forceEndDayRange]
  );

  // When availability_days changes (force mode only), clamp force_end_day.
  // Skipped in optimize mode — the full range is always available.
  useEffect(() => {
    if (optimalDaysMode === 'optimize') return;
    if (!config.force_end_enabled) return;
    const d = config.force_end_day;
    if (d !== undefined && d > workingDays) {
      onUpdate({ force_end_day: workingDays });
    }
  }, [workingDays, optimalDaysMode]); // re-run when either changes

  const handleStartModeChange = (mode: StartMode) => {
    let start: TruckStart;
    if (mode === 'site') {
      start = { kind: 'site', site_id: sites[0]?.id };
    } else if (mode === 'custom') {
      const customId = customPoints[0]?.id || onAddCustom();
      start = { kind: 'custom', custom_id: customId || undefined, label: customPoints[0]?.label };
    } else if (mode === 'random') {
      const randomSite = sites[Math.floor(Math.random() * sites.length)];
      start = { kind: 'site', site_id: randomSite?.id };
    } else {
      start = {
        kind: 'in_transit',
        from_point: { kind: 'site', site_id: sites[0]?.id },
        to_point: { kind: 'site', site_id: sites[1]?.id || sites[0]?.id },
        distance_from_from_km: 0,
      };
    }
    onUpdate({ start_mode: mode, start });
  };

  // Get edge distance for in_transit - with auto-fill logic
  const getEdgeDistance = (): number | null => {
    if (config.start_mode !== 'in_transit' || !config.start?.from_point || !config.start?.to_point) {
      return null;
    }
    // If manually set, use that
    if (config.start.total_edge_distance_km !== undefined) {
      return config.start.total_edge_distance_km;
    }
    // Try auto-fill from matrix or custom distances
    const fromKey = `${config.start.from_point.kind}:${config.start.from_point.site_id || config.start.from_point.custom_id}`;
    const toKey = `${config.start.to_point.kind}:${config.start.to_point.site_id || config.start.to_point.custom_id}`;
    return getDistance(fromKey, toKey);
  };

  const edgeDistance = getEdgeDistance();
  const needsManualEdgeDistance = config.start_mode === 'in_transit' && edgeDistance === null;

  // Determine if we need to show quick add for in-transit custom points
  const inTransitFromCustomId = config.start_mode === 'in_transit' &&
    config.start?.from_point?.kind === 'custom' ? config.start.from_point.custom_id : null;
  const inTransitToCustomId = config.start_mode === 'in_transit' &&
    config.start?.to_point?.kind === 'custom' ? config.start.to_point.custom_id : null;

  // For in-transit custom->site, suggest the site as the distance target
  const inTransitToSiteId = config.start_mode === 'in_transit' &&
    config.start?.to_point?.kind === 'site' ? config.start.to_point.site_id : undefined;
  const inTransitFromSiteId = config.start_mode === 'in_transit' &&
    config.start?.from_point?.kind === 'site' ? config.start.from_point.site_id : undefined;

  // Check if custom point needs distance for force end
  const forceEndCustomId = config.force_end_enabled &&
    config.force_end_point?.kind === 'custom' ? config.force_end_point.custom_id : null;

  return (
    <div className={`rounded-lg border overflow-hidden ${modeCardClass}`}>
      {/* Header */}
      <div
        className={`flex items-center justify-between p-2 cursor-pointer ${modeHeaderHoverClass}`}
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <svg
            className={`w-4 h-4 transition-transform ${expanded ? 'rotate-90' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          <span className="font-medium">{config.truck_id}</span>
          <span className={`text-xs px-2 py-0.5 rounded ${dayBadgeClass}`}>
            {optimalDaysMode === 'optimize' ? 'Auto' : `${config.availability_days}d`}
          </span>
          {config.start_mode === 'custom' && (
            <span className="text-xs text-amber-400 bg-amber-900/30 px-2 py-0.5 rounded">
              Custom
            </span>
          )}
          {config.start_mode === 'in_transit' && (
            <span className="text-xs text-purple-400 bg-purple-900/30 px-2 py-0.5 rounded">
              In Transit
            </span>
          )}
          {needsManualEdgeDistance && (
            <span className="text-xs text-red-400 bg-red-900/30 px-2 py-0.5 rounded">
              Missing distance
            </span>
          )}
        </div>
        {/* Container load selector */}
        <div className="flex items-center mr-1" onClick={(e) => e.stopPropagation()}>
          {/* Clip container — hides whitespace, keeps 3:2 truck image compact */}
          <div className="relative overflow-hidden" style={{ width: 113, height: 69 }}>
            <img
              src="/truck-concept.png"
              alt="truck"
              draggable={false}
              style={{ width: 113, height: 69, display: 'block', userSelect: 'none' }}
            />
            {/* Slots overlaid on cargo box (left 17–65%, top 18–57% of image) */}
            <div
              style={{
                position: 'absolute',
                top: '18%',
                left: '17%',
                width: '48%',
                height: '39%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 3,
              }}
            >
              {[0, 1, 2].map((i) => {
                const filled = i < (config.initial_load ?? 0);
                return (
                  <button
                    key={i}
                    title={filled ? 'Click to remove' : 'Click to add'}
                    onClick={() => {
                      const current = config.initial_load ?? 0;
                      onUpdate({ initial_load: i < current ? i : i + 1 });
                    }}
                    style={{
                      width: 13,
                      height: 13,
                      border: `2px solid ${filled ? '#93c5fd' : 'rgba(255,255,255,0.35)'}`,
                      borderRadius: 2,
                      background: filled ? '#3b82f6' : 'rgba(255,255,255,0.08)',
                      cursor: 'pointer',
                      flexShrink: 0,
                      transition: 'background 0.15s, border-color 0.15s',
                    }}
                  />
                );
              })}
            </div>
          </div>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="text-slate-500 hover:text-red-400 p-1"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* Expanded content */}
      {expanded && (
        <div className="p-3 pt-0 space-y-4 border-t border-slate-700">
          {/* Availability (Working Days) */}
          <div>
            <label className="block text-xs text-slate-400 mb-1">Availability (Working Days)</label>
            <p className="text-xs text-slate-500 mb-2">Number of days this truck can operate within the plan.</p>
            {optimalDaysMode === 'optimize' ? (
              <div className="px-3 py-1.5 bg-slate-700/50 border border-slate-600 rounded text-xs text-slate-400">
                Days auto-selected (1–4)
              </div>
            ) : (
              <div className="flex gap-2">
                {[1, 2, 3, 4].map((days) => (
                  <button
                    key={days}
                    onClick={() => onUpdate({ availability_days: days })}
                    className={`px-3 py-1 rounded text-sm ${
                      config.availability_days === days
                        ? 'bg-blue-600 text-white'
                        : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                    }`}
                  >
                    {days}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Start Location */}
          <div>
            <label className="block text-xs text-slate-400 mb-1">Start Location</label>
            <div className="flex gap-2 mb-2">
              <button
                onClick={() => handleStartModeChange('site')}
                className={`px-3 py-1 rounded text-sm ${
                  config.start_mode === 'site'
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                Site
              </button>
              <button
                onClick={() => handleStartModeChange('custom')}
                className={`px-3 py-1 rounded text-sm ${
                  config.start_mode === 'custom'
                    ? 'bg-amber-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                Custom
              </button>
              <button
                onClick={() => handleStartModeChange('in_transit')}
                className={`px-3 py-1 rounded text-sm ${
                  config.start_mode === 'in_transit'
                    ? 'bg-purple-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                In Transit
              </button>
              <button
                onClick={() => handleStartModeChange('random')}
                className={`px-3 py-1 rounded text-sm ${
                  config.start_mode === 'random'
                    ? 'bg-green-600 text-white'
                    : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                }`}
              >
                Random
              </button>
            </div>

            {config.start_mode === 'random' && (
              <div className="flex items-center gap-2 p-2 bg-slate-700/50 rounded">
                <span className="text-xs text-slate-400">Resolved to:</span>
                <span className="text-xs text-green-400 font-medium">
                  {sites.find((s) => s.id === config.start?.site_id)?.name || 'Unknown site'}
                </span>
                <button
                  onClick={() => handleStartModeChange('random')}
                  className="ml-auto text-xs text-slate-500 hover:text-slate-300"
                  title="Re-randomize"
                >
                  ↺
                </button>
              </div>
            )}

            {config.start_mode === 'site' && (
              <select
                value={config.start?.site_id || ''}
                onChange={(e) =>
                  onUpdate({ start: { kind: 'site', site_id: e.target.value } })
                }
                className="w-full p-2 bg-slate-700 border border-slate-600 rounded text-sm"
              >
                <option value="">Select site...</option>
                {sites.map((site) => (
                  <option key={site.id} value={site.id}>{site.name}</option>
                ))}
              </select>
            )}

            {config.start_mode === 'custom' && (
              <div className="space-y-2">
                <select
                  value={config.start?.custom_id || ''}
                  onChange={(e) => {
                    const cp = customPoints.find((p) => p.id === e.target.value);
                    onUpdate({ start: { kind: 'custom', custom_id: e.target.value, label: cp?.label } });
                  }}
                  className="w-full p-2 bg-slate-700 border border-slate-600 rounded text-sm"
                >
                  <option value="">Select custom point...</option>
                  {customPoints.map((p) => (
                    <option key={p.id} value={p.id}>{p.label}</option>
                  ))}
                </select>
                <button
                  onClick={() => {
                    const newId = onAddCustom();
                    if (newId) {
                      onUpdate({ start: { kind: 'custom', custom_id: newId } });
                    }
                  }}
                  className="text-xs text-blue-400 hover:text-blue-300"
                >
                  + Add new custom point
                </button>

                {/* Inline Quick Add Distance for custom start */}
                {config.start?.custom_id && (
                  <InlineQuickAdd
                    customId={config.start.custom_id}
                    customPoints={customPoints}
                    sites={sites}
                    setCustomPointDistance={setCustomPointDistance}
                    removeCustomPointDistance={removeCustomPointDistance}
                  />
                )}
              </div>
            )}

            {config.start_mode === 'in_transit' && (
              <div className="space-y-3 p-2 bg-slate-700/50 rounded">
                <div className="grid grid-cols-2 gap-2">
                  <PointSelector
                    label="From"
                    value={config.start?.from_point}
                    onChange={(point) =>
                      onUpdate({
                        start: { ...config.start!, kind: 'in_transit', from_point: point },
                      })
                    }
                    sites={sites}
                    customPoints={customPoints}
                    onAddCustom={onAddCustom}
                  />
                  <PointSelector
                    label="To"
                    value={config.start?.to_point}
                    onChange={(point) =>
                      onUpdate({
                        start: { ...config.start!, kind: 'in_transit', to_point: point },
                      })
                    }
                    sites={sites}
                    customPoints={customPoints}
                    onAddCustom={onAddCustom}
                  />
                </div>

                {/* Inline Quick Add for custom From point in in-transit */}
                {inTransitFromCustomId && (
                  <InlineQuickAdd
                    customId={inTransitFromCustomId}
                    customPoints={customPoints}
                    sites={sites}
                    setCustomPointDistance={setCustomPointDistance}
                    removeCustomPointDistance={removeCustomPointDistance}
                    suggestedSiteId={inTransitToSiteId}
                  />
                )}

                {/* Inline Quick Add for custom To point in in-transit */}
                {inTransitToCustomId && inTransitToCustomId !== inTransitFromCustomId && (
                  <InlineQuickAdd
                    customId={inTransitToCustomId}
                    customPoints={customPoints}
                    sites={sites}
                    setCustomPointDistance={setCustomPointDistance}
                    removeCustomPointDistance={removeCustomPointDistance}
                    suggestedSiteId={inTransitFromSiteId}
                  />
                )}

                <div>
                  <label className="block text-xs text-slate-500 mb-1">
                    Distance traveled from [{config.start?.from_point?.kind === 'site'
                      ? sites.find(s => s.id === config.start?.from_point?.site_id)?.name || 'From'
                      : customPoints.find(c => c.id === config.start?.from_point?.custom_id)?.label || 'From'}] (km)
                    {edgeDistance !== null && (
                      <span className="text-blue-400"> / {edgeDistance.toFixed(1)} total</span>
                    )}
                  </label>
                  <input
                    type="number"
                    min="0"
                    max={edgeDistance ?? undefined}
                    step="0.1"
                    value={config.start?.distance_from_from_km || 0}
                    onChange={(e) =>
                      onUpdate({
                        start: {
                          ...config.start!,
                          kind: 'in_transit',
                          distance_from_from_km: parseFloat(e.target.value) || 0,
                        },
                      })
                    }
                    className="w-full p-1.5 bg-slate-700 border border-slate-600 rounded text-sm"
                  />
                </div>

                {needsManualEdgeDistance && (
                  <div>
                    <label className="block text-xs text-amber-400 mb-1">
                      Total edge distance (km) - Required
                    </label>
                    <p className="text-xs text-slate-500 mb-1">
                      {inTransitFromCustomId && inTransitToCustomId
                        ? 'Custom-to-Custom requires manual distance entry.'
                        : 'Define distances above or enter total edge distance manually.'}
                    </p>
                    <input
                      type="number"
                      min="0"
                      step="0.1"
                      value={config.start?.total_edge_distance_km || ''}
                      onChange={(e) =>
                        onUpdate({
                          start: {
                            ...config.start!,
                            kind: 'in_transit',
                            total_edge_distance_km: parseFloat(e.target.value) || undefined,
                          },
                        })
                      }
                      className="w-full p-1.5 bg-slate-700 border border-amber-500 rounded text-sm"
                      placeholder="Enter total edge distance"
                    />
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Force End Location (Optional) */}
          <div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={config.force_end_enabled}
                onChange={(e) => {
                  const enabled = e.target.checked;
                  // When enabling, default force_end_day to the last working day (day N)
                  // so the displayed value is always persisted in state.
                  onUpdate({
                    force_end_enabled: enabled,
                    ...(enabled && !config.force_end_day ? { force_end_day: forceEndDayRange } : {}),
                  });
                }}
                className="rounded bg-slate-700 border-slate-600"
              />
              <span className="text-sm">Force End Location (Optional)</span>
            </label>
            {config.force_end_enabled && (
              <div className="mt-2 ml-6 space-y-2">
                {optimalDaysMode === 'optimize' ? (
                  <p className="text-xs text-slate-500">
                    End day determined automatically by the optimizer.
                  </p>
                ) : (
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">Force End On Day</label>
                    <select
                      value={clampForceEndDay(config.force_end_day ?? forceEndDayRange, forceEndDayRange)}
                      onChange={(e) => onUpdate({ force_end_day: parseInt(e.target.value) })}
                      disabled={forceEndDayRange === 0}
                      className="w-full p-2 bg-slate-700 border border-slate-600 rounded text-sm disabled:opacity-50"
                    >
                      {dayOptions.map((day) => (
                        <option key={day} value={day}>Day {day}</option>
                      ))}
                    </select>
                  </div>
                )}
                <PointSelector
                  label="Force End At"
                  value={config.force_end_point}
                  onChange={(point) => onUpdate({ force_end_point: point })}
                  sites={sites}
                  customPoints={customPoints}
                  onAddCustom={onAddCustom}
                />

                {/* Inline Quick Add for force end custom point */}
                {forceEndCustomId && (
                  <InlineQuickAdd
                    customId={forceEndCustomId}
                    customPoints={customPoints}
                    sites={sites}
                    setCustomPointDistance={setCustomPointDistance}
                    removeCustomPointDistance={removeCustomPointDistance}
                  />
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export const TrucksPanel: React.FC = () => {
  const {
    sites,
    fleetConfig,
    setFleetConfig,
    availableTrucks,
    loadTrucks,
    loadDistanceMatrix,
    updateTruckConfig,
    customPoints,
    addCustomPoint,
    getDistance,
    setCustomPointDistance,
    removeCustomPointDistance,
    optimalDaysMode,
    setOptimalDaysMode,
  } = useAppStore();

  useEffect(() => {
    loadTrucks();
    loadDistanceMatrix();
  }, [loadTrucks, loadDistanceMatrix]);

  const siteOptions = sites.map((s) => ({ id: s.id, name: s.name }));

  const handleAddTruck = (truckId: string) => {
    const truck = availableTrucks.find((t) => t.id === truckId);
    if (!truck) return;

    const newConfig: FleetConfigTruck = {
      truck_id: truckId,
      availability_days: 1,
      initial_load: 0,
      start_mode: 'site',
      start: { kind: 'site', site_id: truck.home_site_id },
      force_end_enabled: false,
    };

    setFleetConfig([...fleetConfig, newConfig]);
  };

  const handleRemoveTruck = (truckId: string) => {
    setFleetConfig(fleetConfig.filter((tc) => tc.truck_id !== truckId));
  };

  const trucksInFleet = new Set(fleetConfig.map((tc) => tc.truck_id));
  const availableToAdd = availableTrucks.filter((t) => !trucksInFleet.has(t.id));

  const maxAvailability = fleetConfig.length > 0
    ? Math.max(...fleetConfig.map((tc) => tc.availability_days))
    : 1;
  const isForceMode = optimalDaysMode === 'force';
  const inactiveToggleClass = 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/70';
  const hintClass = isForceMode ? 'text-blue-300/80' : 'text-cyan-300/80';
  const addTruckClass = isForceMode
    ? 'bg-slate-800 border-[3px] border-blue-700/85 focus:border-blue-400'
    : 'bg-slate-800 border-[3px] border-cyan-500/80 focus:border-cyan-300';

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-400">Fleet Configuration</h3>
        <span className="text-xs text-slate-500">
          {fleetConfig.length} truck{fleetConfig.length !== 1 ? 's' : ''} |{' '}
          {optimalDaysMode === 'optimize' ? 'auto days (1–4)' : `${maxAvailability} day${maxAvailability !== 1 ? 's' : ''} max`}
        </span>
      </div>

      {/* Days mode toggle */}
      <div className="flex gap-1 p-1 bg-slate-900 rounded-lg border border-slate-700">
        <button
          onClick={() => setOptimalDaysMode('force')}
          className={`flex-1 px-2 py-1.5 rounded text-xs font-medium transition-all ${
            optimalDaysMode === 'force'
              ? 'bg-blue-800/95 text-blue-50 ring-1 ring-blue-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.14)]'
              : inactiveToggleClass
          }`}
        >
          Force exact days
        </button>
        <button
          onClick={() => setOptimalDaysMode('optimize')}
          className={`flex-1 px-2 py-1.5 rounded text-xs font-medium transition-all ${
            optimalDaysMode === 'optimize'
              ? 'bg-cyan-600/90 text-cyan-50 ring-1 ring-cyan-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.14)]'
              : inactiveToggleClass
          }`}
        >
          Optimize days
        </button>
      </div>
      <p className={`text-xs ${hintClass}`}>
        {optimalDaysMode === 'optimize'
          ? 'System will choose the optimal number of working days (1-4).'
          : 'Working days are fixed manually for this planning run.'}
      </p>

      {/* Custom Points Panel */}
      <div className={isForceMode ? 'rounded-lg border-[3px] border-blue-700/85 overflow-hidden' : 'rounded-lg border-[3px] border-cyan-500/80 overflow-hidden'}>
        <CustomPointsPanel />
      </div>

      {/* Truck list */}
      <div className="space-y-2">
        {fleetConfig.map((tc) => (
          <TruckConfigCard
            key={tc.truck_id}
            config={tc}
            sites={siteOptions}
            customPoints={customPoints}
            onUpdate={(updates) => updateTruckConfig(tc.truck_id, updates)}
            onRemove={() => handleRemoveTruck(tc.truck_id)}
            onAddCustom={addCustomPoint}
            getDistance={getDistance}
            setCustomPointDistance={setCustomPointDistance}
            removeCustomPointDistance={removeCustomPointDistance}
            optimalDaysMode={optimalDaysMode}
          />
        ))}
      </div>

      {/* Add truck dropdown */}
      {availableToAdd.length > 0 && (
        <select
          className={`w-full p-2 border rounded text-sm ${addTruckClass}`}
          defaultValue=""
          onChange={(e) => {
            if (e.target.value) {
              handleAddTruck(e.target.value);
              e.target.value = '';
            }
          }}
        >
          <option value="">Add truck...</option>
          {availableToAdd.map((truck) => (
            <option key={truck.id} value={truck.id}>{truck.name}</option>
          ))}
        </select>
      )}

      {fleetConfig.length === 0 && (
        <p className={`text-sm text-center py-4 ${isForceMode ? 'text-blue-300/70' : 'text-cyan-300/70'}`}>
          No trucks configured. Add trucks above to plan routes.
        </p>
      )}
    </div>
  );
};

export default TrucksPanel;
