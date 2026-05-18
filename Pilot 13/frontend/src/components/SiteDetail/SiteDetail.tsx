import React, { useState, useEffect, useMemo } from 'react';
import { useAppStore } from '../../stores/appStore';
import * as api from '../../api/client';
import { pressureToKg, kgToPressure, kgToMwh, mwhToKg, formatKg, formatMwh } from '../../utils/conversions';
import { getSiteStatus, STATUS_BADGE_BG_CLASSES, STATUS_TEXT_CLASSES } from '../../utils/siteStatus';
import type { Bay, Site, ProductionRateMode, SitePatchRequest } from '../../types';

interface CollapsibleSectionProps {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

const CollapsibleSection: React.FC<CollapsibleSectionProps> = ({
  title,
  defaultOpen = true,
  children,
}) => {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="border border-slate-700 rounded-lg overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full px-3 py-2 bg-slate-800 flex items-center justify-between text-sm font-medium text-slate-300 hover:bg-slate-750"
      >
        <span>{title}</span>
        <svg
          className={`w-4 h-4 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {isOpen && <div className="p-3 bg-slate-900">{children}</div>}
    </div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// DEV-only Calculation Trace Panel
// Rendered only when import.meta.env.DEV is true (npm run dev).
// Shows every intermediate step so calculations can be verified visually.
// ─────────────────────────────────────────────────────────────────────────────

const BASE_PRESSURE_BAR = 20; // "empty" baseline used by the backend risk model

interface CalcTraceProps {
  site: Site;
  editBays: Bay[];
  effectiveConsumptionRate: number | null;
}

const CalcTrace: React.FC<CalcTraceProps> = ({ site, editBays, effectiveConsumptionRate }) => {
  const isProducer = site.site_type === 'production';
  const kgAtBase = pressureToKg(BASE_PRESSURE_BAR); // ~131.6 kg — backend "empty" floor

  const bayTrace = editBays.map((bay) => {
    const kg = pressureToKg(bay.pressure_bar);
    const mwh = kgToMwh(kg);
    // usable = kg above the 20-bar base (matches backend get_normalized_kg)
    const usableKg = bay.pressure_bar <= BASE_PRESSURE_BAR ? 0 : kg - kgAtBase;
    return { ...bay, kg, mwh, usableKg };
  });

  const totalKg = bayTrace.reduce((s, b) => s + b.kg, 0);
  const totalMwh = kgToMwh(totalKg);
  const totalUsableKg = bayTrace.reduce((s, b) => s + b.usableKg, 0);
  const totalUsableMwh = kgToMwh(totalUsableKg);
  const avgPressure = bayTrace.length > 0
    ? bayTrace.reduce((s, b) => s + b.pressure_bar, 0) / bayTrace.length
    : 0;
  const minPressure = bayTrace.length > 0 ? Math.min(...bayTrace.map((b) => b.pressure_bar)) : 0;
  const maxPressure = bayTrace.length > 0 ? Math.max(...bayTrace.map((b) => b.pressure_bar)) : 0;

  // Consumer: hours_to_critical = usable_kg / consumption_rate
  const consumerHours =
    !isProducer && effectiveConsumptionRate && effectiveConsumptionRate > 0
      ? totalUsableKg / effectiveConsumptionRate
      : null;

  // Producer: remaining_capacity_kg = bays_fixed × (maxKgPerBay - kgAtBase) - usable_kg
  const maxUsablePerBay = pressureToKg(250) - kgAtBase; // 2829 - 131.6 = 2697.4
  const maxUsableCapacity = site.bays_fixed * maxUsablePerBay;
  const remainingKg = maxUsableCapacity - totalUsableKg;
  const productionRateKgH = site.production?.effective_kg_per_h ?? null;
  const producerHours =
    isProducer && productionRateKgH && productionRateKgH > 0
      ? remainingKg / productionRateKgH
      : null;

  const row = 'flex justify-between text-[11px] py-0.5 border-b border-slate-800';
  const label = 'text-slate-500';
  const value = 'font-mono text-slate-300';
  const formula = 'text-slate-600 text-[10px] italic ml-1';

  return (
    <div className="space-y-4 text-[11px]">
      {/* ── Per-Bay ── */}
      <div>
        <div className="text-xs font-medium text-slate-400 mb-1">Per-bay trace</div>
        <div className="bg-slate-800/50 rounded p-2 space-y-2">
          {bayTrace.map((bay) => (
            <div key={bay.bay_id} className="space-y-0.5">
              <div className="font-medium text-slate-300">{bay.bay_id}</div>
              <div className={row}>
                <span className={label}>pressure_bar</span>
                <span className={value}>{bay.pressure_bar} bar</span>
              </div>
              <div className={row}>
                <span className={label}>kg</span>
                <span>
                  <span className={value}>{bay.kg.toFixed(2)} kg</span>
                  <span className={formula}>
                    {bay.pressure_bar <= 200
                      ? `(${bay.pressure_bar}/200)×1316`
                      : `1316+((${bay.pressure_bar}-200)/50)×1513`}
                  </span>
                </span>
              </div>
              <div className={row}>
                <span className={label}>mwh</span>
                <span>
                  <span className={value}>{bay.mwh.toFixed(4)} MWh</span>
                  <span className={formula}>(kg/1000)×15.2</span>
                </span>
              </div>
              <div className={row}>
                <span className={label}>usable_kg (above {BASE_PRESSURE_BAR} bar)</span>
                <span>
                  <span className={value}>{bay.usableKg.toFixed(2)} kg</span>
                  <span className={formula}>
                    {bay.pressure_bar <= BASE_PRESSURE_BAR ? '0 (at/below base)' : `${bay.kg.toFixed(2)}-${kgAtBase.toFixed(2)}`}
                  </span>
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Site Totals ── */}
      <div>
        <div className="text-xs font-medium text-slate-400 mb-1">Site totals</div>
        <div className="bg-slate-800/50 rounded p-2 space-y-0.5">
          <div className={row}><span className={label}>total_kg</span><span className={value}>{totalKg.toFixed(2)} kg</span></div>
          <div className={row}><span className={label}>total_mwh</span><span><span className={value}>{totalMwh.toFixed(4)} MWh</span><span className={formula}>(total_kg/1000)×15.2</span></span></div>
          <div className={row}><span className={label}>total_usable_kg</span><span><span className={value}>{totalUsableKg.toFixed(2)} kg</span><span className={formula}>Σ bay.usable_kg</span></span></div>
          <div className={row}><span className={label}>total_usable_mwh</span><span className={value}>{totalUsableMwh.toFixed(4)} MWh</span></div>
          <div className={row}><span className={label}>avg_pressure</span><span><span className={value}>{avgPressure.toFixed(1)} bar</span><span className={formula}>Σpressure/{bayTrace.length}</span></span></div>
          <div className={row}><span className={label}>min_pressure</span><span className={value}>{minPressure} bar</span></div>
          <div className={row}><span className={label}>max_pressure</span><span className={value}>{maxPressure} bar</span></div>
          <div className={row}><span className={label}>utilization_pct</span><span><span className={value}>{((avgPressure / 250) * 100).toFixed(1)} %</span><span className={formula}>(avg/250)×100</span></span></div>
        </div>
      </div>

      {/* ── Risk Path ── */}
      <div>
        <div className="text-xs font-medium text-slate-400 mb-1">
          Risk path: {isProducer ? 'producer (flaring)' : 'consumer (stockout)'}
        </div>
        <div className="bg-slate-800/50 rounded p-2 space-y-0.5">
          {!isProducer && (
            <>
              <div className={row}><span className={label}>usable_kg (input)</span><span className={value}>{totalUsableKg.toFixed(2)} kg</span></div>
              <div className={row}><span className={label}>consumption_rate</span><span className={value}>{effectiveConsumptionRate?.toFixed(1) ?? '—'} kg/h</span></div>
              <div className={row}><span className={label}>consumption_rate (MWh)</span><span><span className={value}>{effectiveConsumptionRate ? ((effectiveConsumptionRate / 1000) * 15.2).toFixed(3) : '—'} MWh/h</span><span className={formula}>(rate/1000)×15.2</span></span></div>
              <div className={row + ' font-semibold'}>
                <span className={label}>hours_to_critical</span>
                <span>
                  <span className="text-amber-300">{consumerHours?.toFixed(1) ?? '∞'} h</span>
                  {consumerHours !== null && <span className={formula}>usable_kg/rate</span>}
                </span>
              </div>
              {consumerHours !== null && (
                <div className={row}>
                  <span className={label}>→ days</span>
                  <span className={value}>{(consumerHours / 24).toFixed(1)} d</span>
                </div>
              )}
            </>
          )}
          {isProducer && (
            <>
              <div className={row}><span className={label}>bays_fixed</span><span className={value}>{site.bays_fixed}</span></div>
              <div className={row}><span className={label}>max_usable_per_bay</span><span><span className={value}>{maxUsablePerBay.toFixed(2)} kg</span><span className={formula}>kg(250)-kg(20)</span></span></div>
              <div className={row}><span className={label}>max_usable_capacity</span><span><span className={value}>{maxUsableCapacity.toFixed(2)} kg</span><span className={formula}>bays_fixed×max_usable_per_bay</span></span></div>
              <div className={row}><span className={label}>current_usable_kg</span><span className={value}>{totalUsableKg.toFixed(2)} kg</span></div>
              <div className={row}><span className={label}>remaining_kg</span><span><span className={value}>{remainingKg.toFixed(2)} kg</span><span className={formula}>max-current</span></span></div>
              <div className={row}><span className={label}>production_rate</span><span className={value}>{productionRateKgH?.toFixed(1) ?? '—'} kg/h</span></div>
              <div className={row + ' font-semibold'}>
                <span className={label}>hours_to_critical</span>
                <span>
                  <span className="text-amber-300">{producerHours?.toFixed(1) ?? '∞'} h</span>
                  {producerHours !== null && <span className={formula}>remaining_kg/rate</span>}
                </span>
              </div>
            </>
          )}
          <div className={row}>
            <span className={label}>thresholds</span>
            <span className={value}>critical &lt; 24 h · warning &lt; 48 h</span>
          </div>
        </div>
      </div>

      <div className="text-[10px] text-slate-600">
        Pressure model: 0–200 bar linear (0→1316 kg); 200–250 bar linear (1316→2829 kg).
        Energy: MWh = (kg/1000)×15.2. Base pressure: {BASE_PRESSURE_BAR} bar = "empty".
        Only visible in DEV mode.
      </div>
    </div>
  );
};

export const SiteDetail: React.FC = () => {
  const {
    sites,
    selectedSiteId,
    selectSite,
    loadSites,
    getEffectiveFlaringCost,
    getEffectiveConsumptionRate,
    flaringOverrides,
    consumptionOverrides,
  } = useAppStore();

  const selectedSite = sites.find((s) => s.id === selectedSiteId);

  // Edit state for bays
  const [editBays, setEditBays] = useState<Bay[]>([]);
  const [bayKgDrafts, setBayKgDrafts] = useState<Record<string, string>>({});

  // Edit state for production rates (producers only)
  const [editProduction, setEditProduction] = useState<{
    mode: ProductionRateMode;
    kg_per_h: string;
    mwh_per_h: string;
  }>({
    mode: 'UNKNOWN',
    kg_per_h: '',
    mwh_per_h: '',
  });

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);

  // Reset edit values when selected site changes
  useEffect(() => {
    if (selectedSite) {
      // Initialize bays with current values
      setEditBays(
        selectedSite.bays.map((bay) => ({
          bay_id: bay.bay_id,
          pressure_bar: bay.pressure_bar,
          kg: bay.kg,
          mwh: bay.mwh,
          kg_usable: bay.kg_usable,
          mwh_usable: bay.mwh_usable,
          serial_number: bay.serial_number ?? null,
        }))
      );
      setBayKgDrafts(
        Object.fromEntries(
          selectedSite.bays.map((bay) => [bay.bay_id, bay.kg.toFixed(1)])
        )
      );

      // Initialize production rates
      if (selectedSite.production) {
        setEditProduction({
          mode: selectedSite.production.mode,
          kg_per_h: selectedSite.production.kg_per_h?.toString() || '',
          mwh_per_h: selectedSite.production.mwh_per_h?.toString() || '',
        });
      } else {
        setEditProduction({
          mode: 'UNKNOWN',
          kg_per_h: '',
          mwh_per_h: '',
        });
      }

      setHasChanges(false);
      setError(null);
    }
  }, [selectedSite?.id]);

  // Usable floor from backend — never hardcoded in the frontend
  const usableFloor = selectedSite?.usable_floor_bar ?? 20;
  const kgAtFloor = pressureToKg(usableFloor);
  const maxUsablePerBay = pressureToKg(250) - kgAtFloor; // e.g. 2829 - 131.6 = 2697.4

  // Calculate local totals from edit state.
  // Usable values are primary; raw totals kept only for the dev trace.
  const localTotals = useMemo(() => {
    const pressures = editBays.map((b) => b.pressure_bar);
    const minPressure = pressures.length > 0 ? Math.min(...pressures) : 0;
    const maxPressure = pressures.length > 0 ? Math.max(...pressures) : 0;
    const avgPressure = pressures.length > 0 ? pressures.reduce((a, b) => a + b, 0) / pressures.length : 0;

    // Raw (diagnostic only)
    const totalKgRaw = editBays.reduce((sum, bay) => sum + pressureToKg(bay.pressure_bar), 0);
    const totalMwhRaw = kgToMwh(totalKgRaw);

    // Usable (primary) — matches backend get_normalized_kg(p, floor)
    const totalKgUsable = editBays.reduce((sum, bay) => {
      const kg = pressureToKg(bay.pressure_bar);
      return sum + Math.max(0, kg - kgAtFloor);
    }, 0);
    const totalMwhUsable = kgToMwh(totalKgUsable);

    // Usable utilization: total_kg_usable / (bays_fixed × max_usable_per_bay)
    const maxUsableCapacity = (editBays.length > 0 ? editBays.length : 1) * maxUsablePerBay;
    const utilizationUsablePct = maxUsableCapacity > 0
      ? Math.min(100, (totalKgUsable / maxUsableCapacity) * 100)
      : 0;

    return { totalKgRaw, totalMwhRaw, totalKgUsable, totalMwhUsable, utilizationUsablePct, minPressure, maxPressure, avgPressure };
  }, [editBays, kgAtFloor, maxUsablePerBay]);

  // Calculate effective production rates
  const effectiveProduction = useMemo(() => {
    if (editProduction.mode === 'KG_PER_H') {
      const kg = parseFloat(editProduction.kg_per_h);
      if (!isNaN(kg)) {
        return {
          kg_per_h: kg,
          mwh_per_h: (kg / 1000) * 15.2,
        };
      }
    } else if (editProduction.mode === 'MWH_PER_H') {
      const mwh = parseFloat(editProduction.mwh_per_h);
      if (!isNaN(mwh)) {
        return {
          kg_per_h: mwhToKg(mwh),
          mwh_per_h: mwh,
        };
      }
    }
    return null;
  }, [editProduction]);

  // Check if producer is "full" (min pressure >= 250)
  const isProducerFull = useMemo(() => {
    if (!selectedSite || selectedSite.site_type !== 'production') return false;
    return localTotals.minPressure >= 250;
  }, [selectedSite, localTotals]);

  // Effective flaring cost (respects overrides)
  const effectiveFlaringCost = useMemo(() => {
    if (!selectedSiteId) return null;
    return getEffectiveFlaringCost(selectedSiteId);
  }, [selectedSiteId, flaringOverrides, getEffectiveFlaringCost]);

  // Effective consumption rate (respects overrides)
  const effectiveConsumptionRate = useMemo(() => {
    if (!selectedSiteId) return null;
    return getEffectiveConsumptionRate(selectedSiteId);
  }, [selectedSiteId, consumptionOverrides, getEffectiveConsumptionRate]);

  // Check if value is overridden
  const isFlaringOverridden = selectedSiteId ? flaringOverrides[selectedSiteId] !== undefined : false;
  const isConsumptionOverridden = selectedSiteId ? consumptionOverrides[selectedSiteId] !== undefined : false;

  // Calculate flaring loss (uses effective flaring cost from overrides)
  const flaringLoss = useMemo(() => {
    if (!selectedSite || !isProducerFull || !effectiveProduction || effectiveFlaringCost === null) return null;
    return effectiveProduction.mwh_per_h * effectiveFlaringCost;
  }, [selectedSite, isProducerFull, effectiveProduction, effectiveFlaringCost]);

  const handleBayPressureChange = (bayId: string, value: string) => {
    const pressure = parseInt(value) || 0;
    const clampedPressure = Math.max(0, Math.min(250, pressure));

    const kgRaw = pressureToKg(clampedPressure);
    const kgUsable = Math.max(0, kgRaw - kgAtFloor);
    setEditBays((prev) =>
      prev.map((bay) =>
        bay.bay_id === bayId
          ? {
              ...bay,
              pressure_bar: clampedPressure,
              kg: kgRaw,
              mwh: kgToMwh(kgRaw),
              kg_usable: kgUsable,
              mwh_usable: kgToMwh(kgUsable),
            }
          : bay
      )
    );
    setBayKgDrafts((prev) => ({
      ...prev,
      [bayId]: kgRaw.toFixed(1),
    }));
    setHasChanges(true);
  };

  const handleBayKgChange = (bayId: string, value: string) => {
    const normalizedValue = value.replace(',', '.');
    setBayKgDrafts((prev) => ({
      ...prev,
      [bayId]: value,
    }));

    const kg = parseFloat(normalizedValue);
    if (Number.isNaN(kg)) {
      return;
    }

    const clampedKg = Math.max(0, Math.min(2829, kg));
    const pressure = Math.round(kgToPressure(clampedKg));
    const kgRaw = pressureToKg(pressure);
    const kgUsable = Math.max(0, kgRaw - kgAtFloor);

    setEditBays((prev) =>
      prev.map((bay) =>
        bay.bay_id === bayId
          ? {
              ...bay,
              pressure_bar: pressure,
              kg: kgRaw,
              mwh: kgToMwh(kgRaw),
              kg_usable: kgUsable,
              mwh_usable: kgToMwh(kgUsable),
            }
          : bay
      )
    );
    setHasChanges(true);
  };

  const handleBayKgBlur = (bayId: string) => {
    const currentValue = bayKgDrafts[bayId];
    const kg = parseFloat((currentValue ?? '').replace(',', '.'));

    if (Number.isNaN(kg)) {
      const currentBay = editBays.find((bay) => bay.bay_id === bayId);
      setBayKgDrafts((prev) => ({
        ...prev,
        [bayId]: currentBay ? currentBay.kg.toFixed(1) : '0.0',
      }));
      return;
    }

    const clampedKg = Math.max(0, Math.min(2829, kg));
    const pressure = Math.round(kgToPressure(clampedKg));
    const kgRaw = pressureToKg(pressure);

    setBayKgDrafts((prev) => ({
      ...prev,
      [bayId]: kgRaw.toFixed(1),
    }));
  };

  const handleBaySerialChange = (bayId: string, value: string) => {
    // Allow only digits, max 4
    const digits = value.replace(/\D/g, '').slice(0, 4);
    setEditBays((prev) =>
      prev.map((bay) =>
        bay.bay_id === bayId
          ? { ...bay, serial_number: digits || null }
          : bay
      )
    );
    setHasChanges(true);
  };

  const handleProductionModeChange = (newMode: ProductionRateMode) => {
    setEditProduction((prev) => ({ ...prev, mode: newMode }));
    setHasChanges(true);
  };

  const handleProductionValueChange = (field: 'kg_per_h' | 'mwh_per_h', value: string) => {
    setEditProduction((prev) => ({ ...prev, [field]: value }));
    setHasChanges(true);
  };

  const handleSave = async () => {
    if (!selectedSite) return;

    setError(null);
    setSaving(true);

    try {
      // Build the patch request
      const patchRequest: SitePatchRequest = {
        bays: editBays.map((bay) => ({
          bay_id: bay.bay_id,
          pressure_bar: bay.pressure_bar,
          serial_number: bay.serial_number ?? null,
        })),
      };

      // Add production rates if this is a producer
      if (selectedSite.site_type === 'production') {
        patchRequest.production = {
          mode: editProduction.mode,
          kg_per_h: editProduction.mode === 'KG_PER_H' ? parseFloat(editProduction.kg_per_h) || null : null,
          mwh_per_h: editProduction.mode === 'MWH_PER_H' ? parseFloat(editProduction.mwh_per_h) || null : null,
        };
      }

      await api.patchSite(selectedSite.id, patchRequest);
      await loadSites();
      setHasChanges(false);
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (err as Error)?.message ||
        'Failed to save changes';
      setError(message);
      console.error('Save error:', err);
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    if (selectedSite) {
      setEditBays(
        selectedSite.bays.map((bay) => ({
          bay_id: bay.bay_id,
          pressure_bar: bay.pressure_bar,
          kg: bay.kg,
          mwh: bay.mwh,
          kg_usable: bay.kg_usable,
          mwh_usable: bay.mwh_usable,
          serial_number: bay.serial_number ?? null,
        }))
      );
      setBayKgDrafts(
        Object.fromEntries(
          selectedSite.bays.map((bay) => [bay.bay_id, bay.kg.toFixed(1)])
        )
      );
      if (selectedSite.production) {
        setEditProduction({
          mode: selectedSite.production.mode,
          kg_per_h: selectedSite.production.kg_per_h?.toString() || '',
          mwh_per_h: selectedSite.production.mwh_per_h?.toString() || '',
        });
      }
      setHasChanges(false);
      setError(null);
    }
  };

  if (!selectedSite) {
    return (
      <div className="h-full flex items-center justify-center bg-slate-900 text-slate-400 p-4">
        <div className="text-center">
          <svg
            className="w-12 h-12 mx-auto mb-3 text-slate-600"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"
            />
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"
            />
          </svg>
          <p>Select a site to view details</p>
        </div>
      </div>
    );
  }

  const getRiskBadgeColor = (site: typeof selectedSite) => STATUS_BADGE_BG_CLASSES[getSiteStatus(site!)];

  const formatHours = (hours: number) => {
    if (hours >= 99999) return 'Not applicable';
    if (hours < 1) return `${Math.round(hours * 60)} minutes`;
    if (hours < 24) return `${hours.toFixed(1)} hours`;
    return `${Math.round(hours / 24)} days`;
  };

  return (
    <div className="h-full bg-slate-900 text-white overflow-y-auto">
      {/* Header */}
      <div className="p-4 border-b border-slate-700">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xl font-bold">{selectedSite.name}</h2>
          <button
            onClick={() => selectSite(null)}
            className="text-slate-400 hover:text-white"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-slate-400 capitalize">{selectedSite.site_type}</span>
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${getRiskBadgeColor(selectedSite)}`}>
            {selectedSite.risk_level}
          </span>
        </div>
      </div>

      {/* Risk explanation */}
      <div className="p-4 border-b border-slate-700">
        <p className="text-sm text-slate-300">{selectedSite.risk_explanation}</p>
        <div className="mt-2 flex items-center gap-4 text-xs text-slate-400">
          <span>Time to critical: <span className={`font-medium ${STATUS_TEXT_CLASSES[getSiteStatus(selectedSite)]}`}>{formatHours(selectedSite.hours_to_critical)}</span></span>
          <span>Risk score: <span className="font-medium">{selectedSite.risk_score.toFixed(0)}/100</span></span>
        </div>
      </div>

      {/* Error display */}
      {error && (
        <div className="mx-4 mt-4 p-3 bg-red-900/50 border border-red-600 rounded text-sm text-red-200">
          {error}
        </div>
      )}

      {/* Edit Panel Content */}
      <div className="p-4 space-y-4">
        {/* Section A: Static Info */}
        <CollapsibleSection title="Site Information">
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-slate-400">Site Name</span>
              <span className="font-medium">{selectedSite.name}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Type</span>
              <span className="font-medium capitalize">{selectedSite.site_type}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Total Bays (fixed)</span>
              <span className="font-medium">{selectedSite.bays_fixed}</span>
            </div>
            {selectedSite.site_type === 'production' && effectiveFlaringCost !== null && (
              <div className="flex justify-between">
                <span className="text-slate-400">Flaring Cost</span>
                <span className={`font-medium ${isFlaringOverridden ? 'text-amber-400' : 'text-red-400'}`}>
                  {effectiveFlaringCost} EUR/MWh
                  {isFlaringOverridden && <span className="ml-1 text-xs">(override)</span>}
                </span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-slate-400">Location</span>
              <span className="font-medium text-xs">
                {selectedSite.latitude.toFixed(4)}, {selectedSite.longitude.toFixed(4)}
              </span>
            </div>
          </div>
        </CollapsibleSection>

        {/* Section B: Bay Editor */}
        <CollapsibleSection title="Bay Pressures">
          <div className="space-y-3">
            {/* Bay list */}
            <div className="space-y-2">
              {editBays.map((bay) => (
                <div key={bay.bay_id} className="flex items-center gap-2 text-sm">
                  <span className="w-12 text-slate-400">{bay.bay_id}</span>
                  <div className="flex items-center gap-1">
                    <input
                      type="number"
                      min="0"
                      max="250"
                      value={bay.pressure_bar}
                      onChange={(e) => handleBayPressureChange(bay.bay_id, e.target.value)}
                      className="w-20 px-2 py-1 bg-slate-800 border border-slate-600 rounded text-sm focus:outline-none focus:border-blue-500"
                    />
                    <span className="text-slate-500">bar</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <input
                      type="number"
                      min="0"
                      max="2829"
                      step="0.1"
                      value={bayKgDrafts[bay.bay_id] ?? bay.kg.toFixed(1)}
                      onChange={(e) => handleBayKgChange(bay.bay_id, e.target.value)}
                      onBlur={() => handleBayKgBlur(bay.bay_id)}
                      className="w-24 px-2 py-1 bg-slate-800 border border-slate-600 rounded text-sm focus:outline-none focus:border-blue-500"
                    />
                    <span className="text-slate-500">kg</span>
                  </div>
                  <input
                    type="text"
                    inputMode="numeric"
                    placeholder="S/N"
                    maxLength={4}
                    value={bay.serial_number ?? ''}
                    onChange={(e) => handleBaySerialChange(bay.bay_id, e.target.value)}
                    className={`w-14 px-2 py-1 rounded text-sm text-center font-mono focus:outline-none ${
                      bay.serial_number && bay.serial_number.length === 4
                        ? 'bg-slate-800 border border-emerald-600 text-emerald-300'
                        : 'bg-slate-800 border border-slate-600 text-slate-400'
                    }`}
                  />
                  <div className="w-24 text-right text-slate-400">
                    {formatKg(bay.kg_usable)} kg
                  </div>
                  <div className="w-20 text-right text-slate-400">
                    {formatMwh(bay.mwh_usable)} MWh
                  </div>
                </div>
              ))}
            </div>

            {/* Divider */}
            <div className="border-t border-slate-700 pt-2">
              {/* Usable totals (primary) */}
              <div className="flex items-center gap-2 text-sm font-medium">
                <span className="w-12 text-slate-300">Usable</span>
                <div className="flex-1" />
                <div className="w-24 text-right">{formatKg(localTotals.totalKgUsable)} kg</div>
                <div className="w-20 text-right">{formatMwh(localTotals.totalMwhUsable)} MWh</div>
              </div>

              {/* Raw totals (diagnostic) */}
              <div className="flex items-center gap-2 mt-0.5">
                <span className="w-12 text-[10px] text-slate-600">Raw</span>
                <div className="flex-1" />
                <div className="w-24 text-right text-[11px] text-slate-600">{formatKg(localTotals.totalKgRaw)} kg</div>
                <div className="w-20 text-right text-[11px] text-slate-600">{formatMwh(localTotals.totalMwhRaw)} MWh</div>
              </div>
              <div className="text-[10px] text-slate-600 text-right mt-0.5">
                incl. &lt;{usableFloor} bar (diagnostic)
              </div>

              {/* Pressure stats */}
              <div className="mt-2 text-xs text-slate-500 flex gap-4">
                <span>Min: {localTotals.minPressure} bar</span>
                <span>Avg: {localTotals.avgPressure.toFixed(0)} bar</span>
                <span>Max: {localTotals.maxPressure} bar</span>
              </div>

              {/* Usable utilization bar */}
              <div className="mt-2">
                <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full transition-all ${
                      localTotals.utilizationUsablePct >= 90
                        ? 'bg-red-500'
                        : localTotals.utilizationUsablePct >= 70
                        ? 'bg-yellow-500'
                        : 'bg-green-500'
                    }`}
                    style={{ width: `${localTotals.utilizationUsablePct}%` }}
                  />
                </div>
                <div className="mt-1 text-xs text-slate-500 text-right">
                  {localTotals.utilizationUsablePct.toFixed(0)}% usable capacity (≥{usableFloor} bar)
                </div>
              </div>
            </div>
          </div>
        </CollapsibleSection>

        {/* Section C: Production Rate (producers only) */}
        {selectedSite.site_type === 'production' && (
          <CollapsibleSection title="Production Rate">
            <div className="space-y-3">
              {/* Mode selector */}
              <div>
                <label className="block text-xs text-slate-400 mb-2">Input Mode</label>
                <div className="flex gap-2">
                  {(['KG_PER_H', 'MWH_PER_H', 'UNKNOWN'] as ProductionRateMode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => handleProductionModeChange(m)}
                      className={`px-3 py-1 text-xs rounded ${
                        editProduction.mode === m
                          ? 'bg-blue-600 text-white'
                          : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                      }`}
                    >
                      {m === 'KG_PER_H' ? 'kg/h' : m === 'MWH_PER_H' ? 'MWh/h' : 'Unknown'}
                    </button>
                  ))}
                </div>
              </div>

              {/* Rate inputs */}
              {editProduction.mode !== 'UNKNOWN' ? (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">kg/h</label>
                    <input
                      type="number"
                      min="0"
                      step="0.1"
                      value={
                        editProduction.mode === 'KG_PER_H'
                          ? editProduction.kg_per_h
                          : effectiveProduction?.kg_per_h?.toFixed(1) || ''
                      }
                      onChange={(e) => handleProductionValueChange('kg_per_h', e.target.value)}
                      disabled={editProduction.mode !== 'KG_PER_H'}
                      className={`w-full px-2 py-1 bg-slate-800 border border-slate-600 rounded text-sm focus:outline-none focus:border-blue-500 ${
                        editProduction.mode !== 'KG_PER_H' ? 'opacity-50 cursor-not-allowed' : ''
                      }`}
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">MWh/h</label>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={
                        editProduction.mode === 'MWH_PER_H'
                          ? editProduction.mwh_per_h
                          : effectiveProduction?.mwh_per_h?.toFixed(2) || ''
                      }
                      onChange={(e) => handleProductionValueChange('mwh_per_h', e.target.value)}
                      disabled={editProduction.mode !== 'MWH_PER_H'}
                      className={`w-full px-2 py-1 bg-slate-800 border border-slate-600 rounded text-sm focus:outline-none focus:border-blue-500 ${
                        editProduction.mode !== 'MWH_PER_H' ? 'opacity-50 cursor-not-allowed' : ''
                      }`}
                    />
                  </div>
                </div>
              ) : (
                <div className="p-3 bg-slate-800 rounded text-sm text-slate-400">
                  Rate not provided; flaring loss cannot be computed.
                </div>
              )}

              {/* Flaring warning */}
              {isProducerFull && (
                <div className="p-3 bg-red-900/30 border border-red-700 rounded">
                  <div className="flex items-center gap-2 text-red-400 font-medium text-sm">
                    <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                      <path
                        fillRule="evenodd"
                        d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
                        clipRule="evenodd"
                      />
                    </svg>
                    <span>Site is at capacity - gas is being flared!</span>
                  </div>
                  <div className="mt-2 text-sm text-slate-300">
                    <div>
                      Flaring cost: {effectiveFlaringCost} EUR/MWh
                      {isFlaringOverridden && <span className="ml-1 text-amber-400 text-xs">(override)</span>}
                    </div>
                    {effectiveProduction && flaringLoss !== null && (
                      <>
                        <div>Flaring rate: {effectiveProduction.mwh_per_h.toFixed(2)} MWh/h</div>
                        <div className="font-medium text-red-400">
                          Loss: {flaringLoss.toFixed(2)} EUR/h
                        </div>
                      </>
                    )}
                    {!effectiveProduction && (
                      <div className="text-slate-500 italic">
                        Set production rate to calculate hourly loss
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </CollapsibleSection>
        )}

        {/* Consumer rate display */}
        {selectedSite.site_type !== 'production' && effectiveConsumptionRate !== null && effectiveConsumptionRate > 0 && (
          <CollapsibleSection title="Consumption Rate">
            <div className="text-sm">
              <div className="flex justify-between">
                <span className="text-slate-400">Rate</span>
                <span className={`font-medium ${isConsumptionOverridden ? 'text-amber-400' : ''}`}>
                  {effectiveConsumptionRate.toFixed(0)} kg/h
                  {isConsumptionOverridden && <span className="ml-1 text-xs">(override)</span>}
                </span>
              </div>
              <div className="flex justify-between mt-1">
                <span className="text-slate-400">Equivalent</span>
                <span className="font-medium">
                  {((effectiveConsumptionRate / 1000) * 15.2).toFixed(2)} MWh/h
                </span>
              </div>
            </div>
          </CollapsibleSection>
        )}

        {/* DEV-only: Calculation Trace */}
        {import.meta.env.DEV && (
          <CollapsibleSection title="⚙ Calculation Trace (dev)" defaultOpen={false}>
            <CalcTrace
              site={selectedSite}
              editBays={editBays}
              effectiveConsumptionRate={effectiveConsumptionRate}
            />
          </CollapsibleSection>
        )}

        {/* Save/Cancel buttons */}
        {hasChanges && (
          <div className="flex gap-2 pt-2">
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex-1 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-700 rounded text-sm font-medium"
            >
              {saving ? 'Saving...' : 'Save Changes'}
            </button>
            <button
              onClick={handleCancel}
              className="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded text-sm"
            >
              Cancel
            </button>
          </div>
        )}

      </div>
    </div>
  );
};

export default SiteDetail;
