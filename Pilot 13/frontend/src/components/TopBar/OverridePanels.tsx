import React, { useEffect, useState, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useAppStore } from '../../stores/appStore';
import type { Site } from '../../types';

// localStorage keys
const FLARING_OVERRIDES_KEY = 'gasum_flaring_cost_overrides';
const CONSUMPTION_OVERRIDES_KEY = 'gasum_consumption_rate_overrides';

export type FlaringOverrides = Record<string, number>; // site_id -> flaring_cost_eur_mwh
export type ConsumptionOverrides = Record<string, number>; // site_id -> consumption_rate_kg_hour

// Helper to load from localStorage
function loadOverrides<T extends Record<string, unknown>>(key: string): T {
  try {
    const stored = localStorage.getItem(key);
    return stored ? JSON.parse(stored) : ({} as T);
  } catch {
    return {} as T;
  }
}

// Helper to save to localStorage
function saveOverrides<T>(key: string, data: T): void {
  localStorage.setItem(key, JSON.stringify(data));
}

// Portal wrapper component for dropdown panels
interface PortalDropdownProps {
  isOpen: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement>;
  children: React.ReactNode;
  align?: 'left' | 'right';
}

const PortalDropdown: React.FC<PortalDropdownProps> = ({
  isOpen,
  onClose,
  anchorRef,
  children,
  align = 'left',
}) => {
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const panelRef = useRef<HTMLDivElement>(null);

  // Calculate position based on anchor element
  useEffect(() => {
    if (isOpen && anchorRef.current) {
      const rect = anchorRef.current.getBoundingClientRect();
      const panelWidth = 320; // w-80 = 20rem = 320px

      let left = rect.left;
      if (align === 'right') {
        left = rect.right - panelWidth;
      }

      // Ensure panel doesn't go off-screen
      if (left + panelWidth > window.innerWidth) {
        left = window.innerWidth - panelWidth - 8;
      }
      if (left < 8) {
        left = 8;
      }

      setPosition({
        top: rect.bottom + 4,
        left,
      });
    }
  }, [isOpen, anchorRef, align]);

  // Handle click outside to close
  useEffect(() => {
    if (!isOpen) return;

    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node;

      // Don't close if clicking on the anchor button
      if (anchorRef.current?.contains(target)) {
        return;
      }

      // Don't close if clicking inside the panel
      if (panelRef.current?.contains(target)) {
        return;
      }

      onClose();
    };

    // Use capture phase to catch events before they reach the map
    document.addEventListener('mousedown', handleClickOutside, true);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside, true);
    };
  }, [isOpen, onClose, anchorRef]);

  // Handle escape key
  useEffect(() => {
    if (!isOpen) return;

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      ref={panelRef}
      className="fixed bg-slate-800 border border-slate-600 rounded-lg shadow-2xl w-80 max-h-96 overflow-hidden"
      style={{
        top: position.top,
        left: position.left,
        zIndex: 9999, // Well above Leaflet's z-index (~1000)
        pointerEvents: 'auto',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {children}
    </div>,
    document.body
  );
};

interface FlaringCostPanelProps {
  isOpen: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLButtonElement>;
}

export const FlaringCostPanel: React.FC<FlaringCostPanelProps> = ({ isOpen, onClose, anchorRef }) => {
  const { sites } = useAppStore();
  const [overrides, setOverrides] = useState<FlaringOverrides>({});
  const [editValues, setEditValues] = useState<Record<string, string>>({});

  // Load overrides from localStorage on mount
  useEffect(() => {
    const stored = loadOverrides<FlaringOverrides>(FLARING_OVERRIDES_KEY);
    setOverrides(stored);
    // Notify store about loaded overrides
    useAppStore.getState().setFlaringOverrides(stored);
  }, []);

  // Filter to producer sites only
  const producerSites = sites.filter(s => s.site_type === 'production');

  const getEffectiveValue = (site: Site): number => {
    return overrides[site.id] ?? site.flaring_cost_eur_mwh;
  };

  const handleEditStart = (siteId: string, value: number) => {
    setEditValues(prev => ({ ...prev, [siteId]: value.toString() }));
  };

  const handleEditChange = (siteId: string, value: string) => {
    setEditValues(prev => ({ ...prev, [siteId]: value }));
  };

  const handleEditSave = (siteId: string) => {
    const value = parseFloat(editValues[siteId]);
    if (!isNaN(value) && value >= 0) {
      const newOverrides = { ...overrides, [siteId]: value };
      setOverrides(newOverrides);
      saveOverrides(FLARING_OVERRIDES_KEY, newOverrides);
      useAppStore.getState().setFlaringOverrides(newOverrides);
    }
    setEditValues(prev => {
      const { [siteId]: _, ...rest } = prev;
      return rest;
    });
  };

  const handleEditCancel = (siteId: string) => {
    setEditValues(prev => {
      const { [siteId]: _, ...rest } = prev;
      return rest;
    });
  };

  const handleReset = (siteId: string) => {
    const { [siteId]: _, ...rest } = overrides;
    setOverrides(rest);
    saveOverrides(FLARING_OVERRIDES_KEY, rest);
    useAppStore.getState().setFlaringOverrides(rest);
  };

  const handleResetAll = () => {
    setOverrides({});
    saveOverrides(FLARING_OVERRIDES_KEY, {});
    useAppStore.getState().setFlaringOverrides({});
  };

  return (
    <PortalDropdown isOpen={isOpen} onClose={onClose} anchorRef={anchorRef} align="left">
      <div className="flex items-center justify-between p-3 border-b border-slate-700">
        <h3 className="font-medium text-white">Producers: Flaring Cost</h3>
        <div className="flex items-center gap-2">
          {Object.keys(overrides).length > 0 && (
            <button
              onClick={handleResetAll}
              className="text-xs text-slate-400 hover:text-white px-2 py-1 rounded bg-slate-700"
            >
              Reset All
            </button>
          )}
          <button onClick={onClose} className="text-slate-400 hover:text-white">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>
      <div className="overflow-y-auto max-h-72">
        {producerSites.length === 0 ? (
          <p className="p-3 text-slate-400 text-sm">No producer sites found</p>
        ) : (
          <ul className="divide-y divide-slate-700">
            {producerSites.map(site => {
              const isEditing = (siteId: string) => editValues[siteId] !== undefined;
              const hasOverride = overrides[site.id] !== undefined;
              const effectiveValue = getEffectiveValue(site);

              return (
                <li key={site.id} className="p-3 hover:bg-slate-700/50">
                  <div className="flex items-center justify-between">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-white truncate">{site.name}</p>
                      <p className="text-xs text-slate-400">
                        Default: {site.flaring_cost_eur_mwh.toFixed(2)} EUR/MWh
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {isEditing(site.id) ? (
                        <>
                          <input
                            type="number"
                            value={editValues[site.id]}
                            onChange={e => handleEditChange(site.id, e.target.value)}
                            className="w-20 px-2 py-1 text-sm bg-slate-700 border border-slate-500 rounded text-white"
                            min="0"
                            step="0.01"
                            autoFocus
                            onKeyDown={e => {
                              if (e.key === 'Enter') handleEditSave(site.id);
                              if (e.key === 'Escape') handleEditCancel(site.id);
                            }}
                          />
                          <button
                            onClick={() => handleEditSave(site.id)}
                            className="text-green-400 hover:text-green-300"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          </button>
                          <button
                            onClick={() => handleEditCancel(site.id)}
                            className="text-slate-400 hover:text-white"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                          </button>
                        </>
                      ) : (
                        <>
                          <span className={`text-sm ${hasOverride ? 'text-amber-400 font-medium' : 'text-white'}`}>
                            {effectiveValue.toFixed(2)}
                          </span>
                          <button
                            onClick={() => handleEditStart(site.id, effectiveValue)}
                            className="text-slate-400 hover:text-white"
                            title="Edit"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                            </svg>
                          </button>
                          {hasOverride && (
                            <button
                              onClick={() => handleReset(site.id)}
                              className="text-slate-400 hover:text-red-400"
                              title="Reset to default"
                            >
                              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                              </svg>
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </PortalDropdown>
  );
};

interface ConsumptionRatePanelProps {
  isOpen: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLButtonElement>;
}

export const ConsumptionRatePanel: React.FC<ConsumptionRatePanelProps> = ({ isOpen, onClose, anchorRef }) => {
  const { sites } = useAppStore();
  const [overrides, setOverrides] = useState<ConsumptionOverrides>({});
  const [editValues, setEditValues] = useState<Record<string, string>>({});

  // Load overrides from localStorage on mount
  useEffect(() => {
    const stored = loadOverrides<ConsumptionOverrides>(CONSUMPTION_OVERRIDES_KEY);
    setOverrides(stored);
    // Notify store about loaded overrides
    useAppStore.getState().setConsumptionOverrides(stored);
  }, []);

  // Filter to consumer sites only (traffic + industry)
  const consumerSites = sites.filter(s => s.site_type === 'traffic' || s.site_type === 'industry');

  const getEffectiveValue = (site: Site): number => {
    return overrides[site.id] ?? site.consumption_rate_kg_hour;
  };

  const handleEditStart = (siteId: string, value: number) => {
    setEditValues(prev => ({ ...prev, [siteId]: value.toString() }));
  };

  const handleEditChange = (siteId: string, value: string) => {
    setEditValues(prev => ({ ...prev, [siteId]: value }));
  };

  const handleEditSave = (siteId: string) => {
    const value = parseFloat(editValues[siteId]);
    if (!isNaN(value) && value >= 0) {
      const newOverrides = { ...overrides, [siteId]: value };
      setOverrides(newOverrides);
      saveOverrides(CONSUMPTION_OVERRIDES_KEY, newOverrides);
      useAppStore.getState().setConsumptionOverrides(newOverrides);
    }
    setEditValues(prev => {
      const { [siteId]: _, ...rest } = prev;
      return rest;
    });
  };

  const handleEditCancel = (siteId: string) => {
    setEditValues(prev => {
      const { [siteId]: _, ...rest } = prev;
      return rest;
    });
  };

  const handleReset = (siteId: string) => {
    const { [siteId]: _, ...rest } = overrides;
    setOverrides(rest);
    saveOverrides(CONSUMPTION_OVERRIDES_KEY, rest);
    useAppStore.getState().setConsumptionOverrides(rest);
  };

  const handleResetAll = () => {
    setOverrides({});
    saveOverrides(CONSUMPTION_OVERRIDES_KEY, {});
    useAppStore.getState().setConsumptionOverrides({});
  };

  return (
    <PortalDropdown isOpen={isOpen} onClose={onClose} anchorRef={anchorRef} align="right">
      <div className="flex items-center justify-between p-3 border-b border-slate-700">
        <h3 className="font-medium text-white">Consumption Rate</h3>
        <div className="flex items-center gap-2">
          {Object.keys(overrides).length > 0 && (
            <button
              onClick={handleResetAll}
              className="text-xs text-slate-400 hover:text-white px-2 py-1 rounded bg-slate-700"
            >
              Reset All
            </button>
          )}
          <button onClick={onClose} className="text-slate-400 hover:text-white">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>
      <div className="overflow-y-auto max-h-72">
        {consumerSites.length === 0 ? (
          <p className="p-3 text-slate-400 text-sm">No consumer sites found</p>
        ) : (
          <ul className="divide-y divide-slate-700">
            {consumerSites.map(site => {
              const isEditing = (siteId: string) => editValues[siteId] !== undefined;
              const hasOverride = overrides[site.id] !== undefined;
              const effectiveValue = getEffectiveValue(site);

              return (
                <li key={site.id} className="p-3 hover:bg-slate-700/50">
                  <div className="flex items-center justify-between">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-white truncate">{site.name}</p>
                      <p className="text-xs text-slate-400">
                        Default: {site.consumption_rate_kg_hour.toFixed(2)} kg/h
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      {isEditing(site.id) ? (
                        <>
                          <input
                            type="number"
                            value={editValues[site.id]}
                            onChange={e => handleEditChange(site.id, e.target.value)}
                            className="w-20 px-2 py-1 text-sm bg-slate-700 border border-slate-500 rounded text-white"
                            min="0"
                            step="0.1"
                            autoFocus
                            onKeyDown={e => {
                              if (e.key === 'Enter') handleEditSave(site.id);
                              if (e.key === 'Escape') handleEditCancel(site.id);
                            }}
                          />
                          <button
                            onClick={() => handleEditSave(site.id)}
                            className="text-green-400 hover:text-green-300"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          </button>
                          <button
                            onClick={() => handleEditCancel(site.id)}
                            className="text-slate-400 hover:text-white"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                          </button>
                        </>
                      ) : (
                        <>
                          <span className={`text-sm ${hasOverride ? 'text-amber-400 font-medium' : 'text-white'}`}>
                            {effectiveValue.toFixed(2)}
                          </span>
                          <button
                            onClick={() => handleEditStart(site.id, effectiveValue)}
                            className="text-slate-400 hover:text-white"
                            title="Edit"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                            </svg>
                          </button>
                          {hasOverride && (
                            <button
                              onClick={() => handleReset(site.id)}
                              className="text-slate-400 hover:text-red-400"
                              title="Reset to default"
                            >
                              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                              </svg>
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </PortalDropdown>
  );
};

// Export the localStorage key helpers for external use
export { FLARING_OVERRIDES_KEY, CONSUMPTION_OVERRIDES_KEY, loadOverrides, saveOverrides };

// ─────────────────────────────────────────────────────────────────────────────
// Active Constraints Panel
// ─────────────────────────────────────────────────────────────────────────────

interface ActiveConstraintsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLElement>;
}

// Defaults matching backend config
const DEFAULTS = {
  maxContainers: 3,
  maxDriverHours: 9,
  swapTimeMin: 20,
  avgSpeedKmh: 80,
  costPerKm: 2.25,
};

export const ActiveConstraintsPanel: React.FC<ActiveConstraintsPanelProps> = ({
  isOpen,
  onClose,
  anchorRef,
}) => {
  const {
    constraintsMode,
    setConstraintsMode,
    constraintOverrides,
    setConstraintOverrides,
    customSpeedKmh,
    setCustomSpeedKmh,
    getEffectiveSpeedKmh,
  } = useAppStore();

  const isAdvanced = constraintsMode === 'advanced';

  // Local draft state — committed on blur/enter
  const [draft, setDraft] = useState<{
    avgSpeed: string;
    costPerKm: string;
    maxDriverHours: string;
    swapTimeMin: string;
    maxContainers: string;
  }>({
    avgSpeed: String(getEffectiveSpeedKmh()),
    costPerKm: String(constraintOverrides.costPerKm ?? DEFAULTS.costPerKm),
    maxDriverHours: String(constraintOverrides.maxDriverHours ?? DEFAULTS.maxDriverHours),
    swapTimeMin: String(constraintOverrides.swapTimeMin ?? DEFAULTS.swapTimeMin),
    maxContainers: String(constraintOverrides.maxContainers ?? DEFAULTS.maxContainers),
  });

  // Keep draft in sync when panel opens
  useEffect(() => {
    if (isOpen) {
      setDraft({
        avgSpeed: String(getEffectiveSpeedKmh()),
        costPerKm: String(constraintOverrides.costPerKm ?? DEFAULTS.costPerKm),
        maxDriverHours: String(constraintOverrides.maxDriverHours ?? DEFAULTS.maxDriverHours),
        swapTimeMin: String(constraintOverrides.swapTimeMin ?? DEFAULTS.swapTimeMin),
        maxContainers: String(constraintOverrides.maxContainers ?? DEFAULTS.maxContainers),
      });
    }
  }, [isOpen]);

  const commitSpeed = () => {
    const v = Number(draft.avgSpeed);
    if (v >= 20 && v <= 120) setCustomSpeedKmh(v);
    else setDraft((d) => ({ ...d, avgSpeed: String(getEffectiveSpeedKmh()) }));
  };

  const commitField = (field: keyof typeof constraintOverrides, raw: string, min: number, max: number, defaultVal: number) => {
    const v = Number(raw);
    if (!isNaN(v) && v >= min && v <= max) {
      const next = { ...constraintOverrides };
      if (v === defaultVal) {
        delete next[field];
      } else {
        (next as Record<string, number>)[field] = v;
      }
      setConstraintOverrides(next);
    }
  };

  const hasOverrides = customSpeedKmh !== null ||
    Object.values(constraintOverrides).some((v) => v !== undefined);

  const resetAll = () => {
    setCustomSpeedKmh(null);
    setConstraintOverrides({});
    setDraft({
      avgSpeed: String(DEFAULTS.avgSpeedKmh),
      costPerKm: String(DEFAULTS.costPerKm),
      maxDriverHours: String(DEFAULTS.maxDriverHours),
      swapTimeMin: String(DEFAULTS.swapTimeMin),
      maxContainers: String(DEFAULTS.maxContainers),
    });
  };

  const fieldClass = (isModified: boolean) =>
    `w-20 px-2 py-1 rounded text-sm text-right font-mono border ${
      isModified
        ? 'bg-blue-600/20 border-blue-500 text-white'
        : 'bg-slate-700 border-slate-600 text-slate-300'
    }`;

  return (
    <PortalDropdown isOpen={isOpen} onClose={onClose} anchorRef={anchorRef} align="right">
      <div className="p-4 space-y-4 w-72">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">Active Constraints</h3>
          <div className="flex items-center gap-2">
            {hasOverrides && (
              <button
                onClick={resetAll}
                className="text-xs text-slate-400 hover:text-white"
              >
                Reset
              </button>
            )}
            <button
              onClick={() => setConstraintsMode(isAdvanced ? 'basic' : 'advanced')}
              className={`text-xs px-2 py-0.5 rounded ${
                isAdvanced
                  ? 'bg-slate-600 text-white'
                  : 'bg-slate-700 text-slate-400 hover:text-white'
              }`}
            >
              {isAdvanced ? 'Advanced' : 'Basic'}
            </button>
          </div>
        </div>

        {/* Fields */}
        <div className="space-y-3">
          {/* Average speed */}
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-slate-300">Average speed</p>
              <p className="text-xs text-slate-500">km/h</p>
            </div>
            <input
              type="number"
              min={20}
              max={120}
              step={5}
              value={draft.avgSpeed}
              onChange={(e) => setDraft((d) => ({ ...d, avgSpeed: e.target.value }))}
              onBlur={commitSpeed}
              onKeyDown={(e) => { if (e.key === 'Enter') commitSpeed(); }}
              className={fieldClass(customSpeedKmh !== null)}
            />
          </div>

          {/* Cost per km */}
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-slate-300">Cost per km</p>
              <p className="text-xs text-slate-500">EUR/km</p>
            </div>
            <input
              type="number"
              min={0}
              max={20}
              step={0.05}
              value={draft.costPerKm}
              onChange={(e) => setDraft((d) => ({ ...d, costPerKm: e.target.value }))}
              onBlur={() => commitField('costPerKm', draft.costPerKm, 0, 20, DEFAULTS.costPerKm)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitField('costPerKm', draft.costPerKm, 0, 20, DEFAULTS.costPerKm);
              }}
              className={fieldClass(constraintOverrides.costPerKm !== undefined)}
            />
          </div>

          {/* Advanced fields */}
          {isAdvanced && (
            <>
              <div className="border-t border-slate-700 pt-3 space-y-3">
                {/* Truck capacity */}
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-slate-300">Truck capacity</p>
                    <p className="text-xs text-slate-500">max containers</p>
                  </div>
                  <input
                    type="number"
                    min={1}
                    max={5}
                    step={1}
                    value={draft.maxContainers}
                    onChange={(e) => setDraft((d) => ({ ...d, maxContainers: e.target.value }))}
                    onBlur={() => commitField('maxContainers', draft.maxContainers, 1, 5, DEFAULTS.maxContainers)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitField('maxContainers', draft.maxContainers, 1, 5, DEFAULTS.maxContainers);
                    }}
                    className={fieldClass(constraintOverrides.maxContainers !== undefined)}
                  />
                </div>

                {/* Driver time */}
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-slate-300">Driver time</p>
                    <p className="text-xs text-slate-500">max hours/day</p>
                  </div>
                  <input
                    type="number"
                    min={1}
                    max={24}
                    step={0.5}
                    value={draft.maxDriverHours}
                    onChange={(e) => setDraft((d) => ({ ...d, maxDriverHours: e.target.value }))}
                    onBlur={() => commitField('maxDriverHours', draft.maxDriverHours, 1, 24, DEFAULTS.maxDriverHours)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitField('maxDriverHours', draft.maxDriverHours, 1, 24, DEFAULTS.maxDriverHours);
                    }}
                    className={fieldClass(constraintOverrides.maxDriverHours !== undefined)}
                  />
                </div>

                {/* Service time */}
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-slate-300">Service time</p>
                    <p className="text-xs text-slate-500">min/stop</p>
                  </div>
                  <input
                    type="number"
                    min={1}
                    max={120}
                    step={5}
                    value={draft.swapTimeMin}
                    onChange={(e) => setDraft((d) => ({ ...d, swapTimeMin: e.target.value }))}
                    onBlur={() => commitField('swapTimeMin', draft.swapTimeMin, 1, 120, DEFAULTS.swapTimeMin)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitField('swapTimeMin', draft.swapTimeMin, 1, 120, DEFAULTS.swapTimeMin);
                    }}
                    className={fieldClass(constraintOverrides.swapTimeMin !== undefined)}
                  />
                </div>
              </div>
            </>
          )}
        </div>

        {hasOverrides && (
          <p className="text-xs text-blue-400">
            Modified constraints will apply on next plan generation.
          </p>
        )}
      </div>
    </PortalDropdown>
  );
};
