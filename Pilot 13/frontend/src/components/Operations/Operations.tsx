import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useAppStore } from '../../stores/appStore';
import type { ObjectiveFunction, ContainerMove, Recommendation, Route, Site, FleetConfigTruck } from '../../types';
import { validateAllRoutes } from '../../utils/routeValidation';
import { getRouteDisplayTimeHours } from '../../utils/routeMetrics';
import { TrucksPanel } from './TrucksPanel';
import { RouteListItem, RouteDetail } from './RouteAccordion';
import { DaySummary } from './DaySummary';
import { ChatbotDrawer } from './ChatbotDrawer';
import { exportRecommendationExcel } from '../../api/client';

const DEV_TRACE_ENABLED = import.meta.env.DEV;

// ─────────────────────────────────────────────────────────────────────────────
// Diagnostic code → human-readable translation layer
// ─────────────────────────────────────────────────────────────────────────────
const DIAGNOSTIC_TRANSLATIONS: Record<string, { title: string; detail: string }> = {
  END_LOAD_NOT_ZERO: {
    title: 'Truck finished the day carrying containers.',
    detail: 'These will be available as starting load on the next shift.',
  },
  TEMP_EMPTY_PICKUP: {
    title: 'Truck collected empty containers before delivering full ones.',
    detail: 'These will be balanced at the next producer stop.',
  },
  NO_SWAP_STOP: {
    title: 'Route includes a stop where no container swap occurred.',
    detail: 'This stop may be a transit or relay point.',
  },
  BAY_IMBALANCE: {
    title: 'Container imbalance detected.',
    detail: 'More containers were picked up from plants than returned.',
  },
  CONSTRAINT_RELAXED: {
    title: 'Some routing constraints were adjusted to find a solution.',
    detail: 'Review the plan carefully before approving.',
  },
  OVERTIME: {
    title: 'Truck shift exceeds the standard 9-hour limit.',
    detail: 'Consider redistributing stops to another truck.',
  },
  DEADHEAD: {
    title: 'Truck drove without cargo on part of the route.',
    detail: 'This may indicate an inefficient start or end location.',
  },
};

/** Translate a raw diagnostic message or code into operational language. */
function translateMessage(code: string | undefined, rawMessage: string): string {
  const msg = rawMessage ?? '';
  if (/\bRISK_UNMITIGATED\b/i.test(msg) || /Plan does not mitigate all high-risk demand/i.test(msg)) {
    return 'Some critical sites are still unserved in this plan.';
  }
  if (/\bPARTIAL_ACT\b/i.test(msg) || /Partial service selected/i.test(msg)) {
    return 'The solver intentionally skipped lower-priority stops to protect critical operations.';
  }
  if (/Configured horizon is .*current workload fits in about/i.test(msg)) {
    return 'The plan has spare day capacity under current constraints.';
  }
  if (/below min quality/i.test(msg)) {
    return 'Short/low-impact route kept to maintain feasibility.';
  }
  if (code && DIAGNOSTIC_TRANSLATIONS[code]) {
    return DIAGNOSTIC_TRANSLATIONS[code].title;
  }
  for (const [key, val] of Object.entries(DIAGNOSTIC_TRANSLATIONS)) {
    if (msg.includes(key)) return val.title;
  }
  // Strip bracketed codes like [END_LOAD_NOT_ZERO] from raw messages
  return msg.replace(/\[[\w_]+\]\s*/g, '').trim();
}

/** Return a secondary detail line for a known code, if available. */
function getTranslationDetail(code: string | undefined, rawMessage: string): string | null {
  const msg = rawMessage ?? '';
  if (/\bRISK_UNMITIGATED\b/i.test(msg) || /Plan does not mitigate all high-risk demand/i.test(msg)) {
    return 'Open “Site Priority” and prioritize these red sites first, or add truck-day capacity.';
  }
  if (/\bPARTIAL_ACT\b/i.test(msg) || /Partial service selected/i.test(msg)) {
    return 'This is a cost/risk trade-off. If you want fuller coverage, increase available truck-days or relax force-end constraints.';
  }
  if (/Configured horizon is .*current workload fits in about/i.test(msg)) {
    return 'Not an error: it means the plan can be completed with fewer active working days.';
  }
  if (/below min quality/i.test(msg)) {
    return 'Kept as fallback because removing it would leave demand uncovered.';
  }
  if (code && DIAGNOSTIC_TRANSLATIONS[code]) return DIAGNOSTIC_TRANSLATIONS[code].detail;
  for (const [key, val] of Object.entries(DIAGNOSTIC_TRANSLATIONS)) {
    if (msg.includes(key)) return val.detail;
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────────────
// Warning categorization (used for grouping raw warnings)
// ─────────────────────────────────────────────────────────────────────────────
type WarningCategory = 'time' | 'load' | 'container' | 'operational' | 'other';

const CATEGORY_LABELS: Record<WarningCategory, string> = {
  time: 'Shift & timing',
  load: 'Load & capacity',
  container: 'Container state',
  operational: 'Route efficiency',
  other: 'Other observations',
};

const CATEGORY_ORDER: WarningCategory[] = ['time', 'load', 'container', 'operational', 'other'];


function dedupeWarnings(items: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of items) {
    const key = raw.replace(/\s+/g, ' ').trim().toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(raw);
  }
  return out;
}

function extractUnservedCriticalCount(rec: Recommendation): number {
  const feedback = rec.solution_feedback ?? [];
  const warnings = rec.warnings ?? [];
  const pool = [
    ...feedback.map((f) => `${f.code ?? ''} ${f.message ?? ''}`.trim()),
    ...warnings,
  ];
  for (const msg of pool) {
    if (!/RISK_UNMITIGATED|high-risk demand|critical site/i.test(msg)) continue;
    const m = msg.match(/(\d+)\s+critical/i);
    if (m) return Number(m[1]) || 0;
  }
  return 0;
}

function categorizeWarning(w: string): WarningCategory {
  const lc = w.toLowerCase();
  if (/\b(time|hour|hours|driver|shift|overtime|9h|duration)\b/.test(lc)) return 'time';
  if (/\b(load|capaci|pickup|pick up|dropoff|drop off|balance|containers on truck)\b/.test(lc)) return 'load';
  if (/\b(empty|full|pressure|bay|serial|container state)\b/.test(lc)) return 'container';
  if (/\b(deadhead|unused|idle|inefficien|no swap|no action|return)\b/.test(lc)) return 'operational';
  return 'other';
}

/** Scan warning text for a known site ID or name and return the site ID if found. */
function extractSiteId(text: string, sites: Site[]): string | null {
  for (const site of sites) {
    if (text.includes(site.id) || text.includes(site.name)) return site.id;
  }
  return null;
}

/** Compute issue counts from a recommendation. */
function getIssueCounts(rec: Recommendation) {
  const feedback = rec.solution_feedback ?? [];
  const critical = feedback.filter((f) => f.type === 'error').length;
  const info = feedback.filter((f) => f.type === 'info').length;
  const uniqueWarnings = dedupeWarnings(rec.warnings ?? []);
  const warnings =
    uniqueWarnings.length + feedback.filter((f) => f.type === 'warning').length;
  return { critical, warnings, info };
}

/** Derive Plan Health indicators from diagnostics — pure function, no hooks. */
function computePlanHealth(rec: Recommendation): { ok: boolean; label: string }[] {
  const feedback = rec.solution_feedback ?? [];
  const warnings = rec.warnings ?? [];
  const { critical } = getIssueCounts(rec);
  const unservedCritical = extractUnservedCriticalCount(rec);

  const allMessages = [
    ...feedback.map((f) => f.code ?? f.message),
    ...warnings,
  ];
  const hasCode = (code: string) =>
    feedback.some((f) => f.code === code) || allMessages.some((m) => m.includes(code));

  const indicators: { ok: boolean; label: string }[] = [];

  indicators.push({
    ok: critical === 0 && unservedCritical === 0,
    label:
      critical === 0 && unservedCritical === 0
        ? 'Demand satisfied'
        : 'Demand could not be fully satisfied',
  });

  if (hasCode('BAY_IMBALANCE')) {
    indicators.push({ ok: false, label: 'Container imbalance detected' });
  }
  if (hasCode('END_LOAD_NOT_ZERO')) {
    indicators.push({ ok: false, label: 'Truck ended the day carrying containers' });
  }
  if (hasCode('TEMP_EMPTY_PICKUP')) {
    indicators.push({ ok: false, label: 'Empty containers collected before delivering full ones' });
  }

  const hubsUsed = rec.transfer_hubs_visited ?? 0;
  if ((rec.transfer_hubs_count ?? 0) > 0) {
    indicators.push({
      ok: hubsUsed > 0,
      label: hubsUsed > 0 ? 'Transfer hub used in this plan' : 'No transfer hub used',
    });
  }

  if (rec.feasibility_level && rec.feasibility_level !== 'STRICT') {
    indicators.push({ ok: false, label: 'Fallback constraints applied' });
  }

  return indicators;
}

function formatSiteList(names: string[]): string {
  if (names.length === 0) return '';
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`;
}

/** Generate one sentence per truck describing what it does in the plan. */
function generatePlanNarrative(routes: Route[]): string[] {
  const byTruck = new Map<string, { delivery: Set<string>; pickup: Set<string>; days: Set<number> }>();

  for (const route of routes) {
    if (!byTruck.has(route.truck_id)) {
      byTruck.set(route.truck_id, { delivery: new Set(), pickup: new Set(), days: new Set() });
    }
    const entry = byTruck.get(route.truck_id)!;
    entry.days.add(route.day_index);

    for (const stop of route.stops) {
      if (!stop.swap_operation) continue;
      const dropped = stop.swap_operation.containers_dropped.length;
      const picked = stop.swap_operation.containers_picked.length;
      if (dropped > 0) entry.delivery.add(stop.site_name);
      if (picked > 0) entry.pickup.add(stop.site_name);
    }
  }

  const sentences: string[] = [];
  const sortedTrucks = [...byTruck.keys()].sort();

  for (const truckId of sortedTrucks) {
    const { delivery, pickup, days } = byTruck.get(truckId)!;
    const dayLabel = days.size === 1
      ? `Day ${[...days][0] + 1}`
      : `${days.size} days`;
    const deliveries = [...delivery];
    const pickups = [...pickup];

    if (deliveries.length > 0 && pickups.length > 0) {
      sentences.push(
        `${truckId} collects containers from ${formatSiteList(pickups)} and delivers to ${formatSiteList(deliveries)} (${dayLabel}).`
      );
    } else if (deliveries.length > 0) {
      sentences.push(`${truckId} delivers containers to ${formatSiteList(deliveries)} (${dayLabel}).`);
    } else if (pickups.length > 0) {
      sentences.push(`${truckId} collects empty containers from ${formatSiteList(pickups)} (${dayLabel}).`);
    } else {
      const allSites = routes
        .filter((r) => r.truck_id === truckId)
        .flatMap((r) => r.stops.map((s) => s.site_name))
        .filter((v, i, arr) => arr.indexOf(v) === i);
      if (allSites.length > 1) {
        sentences.push(`${truckId} operates through ${formatSiteList(allSites)} (${dayLabel}).`);
      }
    }
  }

  return sentences;
}

/** Build "Why this plan" rationale sentences from recommendation metadata. */
function generateWhyThisPlan(rec: Recommendation): string[] {
  const reasons: string[] = [];

  if (rec.objective_function === 'flaring') {
    reasons.push('Sites with active gas flaring were prioritised to reduce environmental impact.');
  } else if (rec.objective_function === 'time') {
    reasons.push('Routes were optimised for shortest total driving time.');
  } else {
    reasons.push('Cost, time, and demand risk were balanced across all routes.');
  }

  const critical = rec.critical_sites_addressed ?? 0;
  if (critical > 0) {
    reasons.push(
      `${critical} critical ${critical === 1 ? 'site' : 'sites'} with less than 24 h of inventory ${critical === 1 ? 'was' : 'were'} prioritised.`
    );
  }

  if ((rec.transfer_hubs_visited ?? 0) > 0) {
    reasons.push('Transfer hubs extended route coverage without increasing fleet size.');
  }

  if ((rec.fill_sites_visited ?? 0) > 0) {
    reasons.push('Additional lower-priority sites were served using spare shift time.');
  }

  if ((rec.unreturned_containers ?? 0) > 0) {
    reasons.push('Some containers remain on trucks at end of day and will be accounted for in the next cycle.');
  }

  return reasons;
}

interface TraceContext {
  sites?: Site[];
  fleetConfig?: FleetConfigTruck[];
  constraintOverrides?: {
    maxContainers?: number;
  };
}

function buildDeveloperTrace(rec: Recommendation, ctx: TraceContext = {}): string {
  const lines: string[] = [];
  const sortedRoutes = [...(rec.routes ?? [])].sort((a, b) => {
    if (a.day_index !== b.day_index) return a.day_index - b.day_index;
    return a.truck_id.localeCompare(b.truck_id);
  });
  lines.push(
    `Plan ${rec.id} | objective=${rec.objective_function} | horizon=${rec.horizon_days}d | routes=${sortedRoutes.length} | cost=${Math.round(rec.total_cost_eur)} EUR`
  );
  lines.push(`status=${rec.status} | feasibility=${rec.feasibility_level ?? 'STRICT'} | risk=${rec.solution_risk_score ?? 'n/a'}`);

  // Reason code / message (most useful for infeasible plans)
  if (rec.reason_code || rec.reason_message) {
    lines.push(`reason=${rec.reason_code ?? '—'} | ${rec.reason_message ?? ''}`);
  }
  lines.push('');

  // Fleet configuration summary
  if (ctx.fleetConfig && ctx.fleetConfig.length > 0) {
    lines.push('=== Fleet Config ===');
    for (const tc of ctx.fleetConfig) {
      const start = tc.start;
      let startLabel = '?';
      if (start) {
        if (start.kind === 'site' && start.site_id) {
          const site = ctx.sites?.find((s) => s.id === start.site_id);
          startLabel = site ? site.name : start.site_id;
        } else if (start.kind === 'custom') {
          startLabel = start.label ?? start.custom_id ?? 'custom';
        } else if (start.kind === 'in_transit') {
          const fromId = start.from_point?.site_id;
          const toId = start.to_point?.site_id;
          const fromSite = ctx.sites?.find((s) => s.id === fromId);
          const toSite = ctx.sites?.find((s) => s.id === toId);
          const fromName = fromSite?.name ?? fromId ?? '?';
          const toName = toSite?.name ?? toId ?? '?';
          startLabel = `in_transit(${fromName}->${toName})`;
        }
      }
      const forceEnd = tc.force_end_enabled
        ? ` | force_end=day${tc.force_end_day ?? '?'}`
        : '';
      const capLabel = ctx.constraintOverrides?.maxContainers ?? 3;
      lines.push(
        `  ${tc.truck_id} | days=${tc.availability_days} | cap=${capLabel} | init_load=${tc.initial_load} | start=${startLabel}${forceEnd}`
      );
    }
    lines.push('');
  }

  // Demand sites sorted by urgency (most critical first)
  if (ctx.sites && ctx.sites.length > 0) {
    const riskOrder: Record<string, number> = { critical: 0, warning: 1, normal: 2, safe: 3 };
    const urgentSites = ctx.sites
      .filter((s) => s.risk_level === 'critical' || s.risk_level === 'warning')
      .sort((a, b) => {
        const ro = (riskOrder[a.risk_level] ?? 99) - (riskOrder[b.risk_level] ?? 99);
        if (ro !== 0) return ro;
        return (a.hours_to_critical ?? 9999) - (b.hours_to_critical ?? 9999);
      });
    if (urgentSites.length > 0) {
      lines.push('=== Demand Sites (urgent) ===');
      for (const s of urgentSites) {
        const h = s.hours_to_critical != null ? `${s.hours_to_critical.toFixed(1)}h` : '—';
        const baysUsable = s.bays?.filter((b) => b.kg_usable > 0).length ?? 0;
        const baysTotal = s.bays_fixed ?? s.bays?.length ?? 0;
        lines.push(
          `  ${s.name} (${s.id}) | ${s.risk_level.toUpperCase()} | htc=${h} | util=${s.utilization_usable_pct?.toFixed(0) ?? '?'}% | bays=${baysUsable}/${baysTotal} usable | type=${s.site_type}`
        );
      }
      lines.push('');
    } else {
      lines.push('=== Demand Sites ===');
      lines.push('  (none flagged critical or warning)');
      lines.push('');
    }
  }

  for (const route of sortedRoutes) {
    lines.push(`=== Day ${route.day_index} | ${route.truck_id} | ${route.total_distance_km.toFixed(1)} km | ${getRouteDisplayTimeHours(route).toFixed(1)} h ===`);
    for (const stop of route.stops) {
      const op = stop.swap_operation;
      const dropped = op?.containers_dropped?.length ?? 0;
      const picked = op?.containers_picked?.length ?? 0;
      const opText =
        dropped || picked
          ? `drop=${dropped} pick=${picked}`
          : 'transit';
      lines.push(
        `S${stop.sequence} ${stop.site_name || stop.site_id} | ${opText} | F=${stop.load_full_after ?? 0} E=${stop.load_empty_after ?? 0} | dist=${stop.cumulative_distance_km.toFixed(1)} km`
      );
      if (op && (dropped > 0 || picked > 0)) {
        if (op.containers_dropped.length > 0) lines.push(`  dropped_ids: ${op.containers_dropped.join(', ')}`);
        if (op.containers_picked.length > 0) lines.push(`  picked_ids:  ${op.containers_picked.join(', ')}`);
      }
    }
    lines.push('');
  }

  if (rec.container_moves && rec.container_moves.length > 0) {
    lines.push('=== Container Moves ===');
    for (const mv of rec.container_moves) {
      lines.push(
        `D${mv.day_index} ${mv.truck_id} ${mv.bay_id} (${mv.bay_serial_number ?? '—'}) ${mv.from_site_name} -> ${mv.to_site_name} [${mv.reason ?? '—'}]`
      );
    }
    lines.push('');
  }

  // Infeasibility diagnostics (only present when status=infeasible)
  const diag = rec.infeasibility_diagnostics;
  if (diag) {
    const c = diag.constraints;
    lines.push('=== Infeasibility Diagnostics ===');
    lines.push(`  Shift budget: ${c.max_driver_hours}h (${c.max_driver_min} min) | service/stop: ${c.service_time_min} min | speed: ${c.avg_speed_kmph} km/h`);
    lines.push('');

    // Trucks
    lines.push('  -- Trucks --');
    for (const t of diag.trucks) {
      const loadNote = t.config_initial_load !== t.actual_current_load
        ? ` ← actual=${t.actual_current_load} (truck has containers from prev plan!)`
        : '';
      lines.push(`  ${t.truck_id} | start=${t.start_site_name} | init_load(cfg)=${t.config_initial_load}/${t.capacity}${loadNote} | days=${t.availability_days}`);
    }
    lines.push('');

    // Producer hubs
    lines.push('  -- Producer Hubs (available full containers) --');
    if (diag.producer_hubs.length === 0) {
      lines.push('  WARNING: No producers with full/near-full containers found in network!');
    } else {
      for (const p of diag.producer_hubs) {
        const truckRows = p.trucks.map((r) =>
          `${r.truck_id}: ${r.dist_km != null ? r.dist_km + ' km' : '?'} / ${r.drive_min != null ? r.drive_min + ' min' : '?'}`
        ).join(', ');
        lines.push(`  ${p.site_name} | full=${p.full_bays} near_full=${p.near_full_bays} | dist [${truckRows}]`);
      }
    }
    lines.push('');

    // Demand sites with reachability
    lines.push('  -- Demand Sites (direct reachability from truck start) --');
    for (const ds of diag.demand_sites) {
      const truckRows = ds.trucks.map((r) => {
        const fits = r.fits_direct === true ? '✓' : r.fits_direct === false ? '✗' : '?';
        return `${r.truck_id}: ${r.dist_km != null ? r.dist_km + ' km' : '?'} / ${r.drive_min != null ? r.drive_min + ' min' : '?'} ${fits}`;
      }).join(', ');
      const htcStr = ds.hours_to_critical != null ? `${ds.hours_to_critical}h` : '?';
      lines.push(`  ${ds.site_name} | ${ds.risk_level.toUpperCase()} htc=${htcStr} | swappable_bays=${ds.swappable_bays}/${ds.bays_fixed} | [${truckRows}]`);
    }
    lines.push('');

    // Flow analysis (full round-trip estimates for empty trucks)
    lines.push('  -- Flow Analysis (start→producer→consumer→start round-trip) --');
    for (const fa of diag.flow_analysis) {
      if (fa.note) {
        lines.push(`  ${fa.truck_id}: ${fa.note}`);
        continue;
      }
      lines.push(`  ${fa.truck_id} | nearest producer: ${fa.nearest_producer} (${fa.drive_to_producer_min} min)`);
      for (const r of fa.routes ?? []) {
        const feasMark = r.feasible === true ? '✓ OK' : r.feasible === false ? '✗ OVER BUDGET' : '?';
        const totalStr = r.total_min != null ? `${r.total_min} min` : '? min';
        lines.push(`    ${r.route} | drive=${r.total_drive_min ?? '?'} + svc=${r.service_min} = ${totalStr} / budget=${r.budget_min} min → ${feasMark}`);
        if (r.note) lines.push(`      note: ${r.note}`);
      }
    }
    lines.push('');
  }

  const warnings = dedupeWarnings(rec.warnings ?? []);
  lines.push('=== Warnings ===');
  if (warnings.length > 0) {
    for (const w of warnings) lines.push(`- ${w}`);
  } else {
    lines.push('  (none)');
  }
  lines.push('');

  const feedback = rec.solution_feedback ?? [];
  lines.push('=== Solution Feedback ===');
  if (feedback.length > 0) {
    for (const f of feedback) {
      lines.push(`- [${f.type}] ${f.code}${f.truck_id ? ` (${f.truck_id})` : ''}: ${f.message}`);
    }
  } else {
    lines.push('  (none)');
  }

  return lines.join('\n');
}

// ─────────────────────────────────────────────────────────────────────────────
// Warning item — highlights referenced site on map when hovered
// ─────────────────────────────────────────────────────────────────────────────
const WarningItem: React.FC<{
  text: string;
  sites: Site[];
  setHoveredSiteId: (id: string | null) => void;
}> = ({ text, sites, setHoveredSiteId }) => {
  const siteId = useMemo(() => extractSiteId(text, sites), [text, sites]);
  const translated = translateMessage(undefined, text);
  const detail = getTranslationDetail(undefined, text);
  return (
    <div
      className={`text-xs px-2 py-1.5 bg-amber-900/20 border-l-2 border-amber-500 rounded-r transition-colors ${siteId ? 'cursor-default hover:bg-amber-900/35' : ''}`}
      onMouseEnter={() => siteId && setHoveredSiteId(siteId)}
      onMouseLeave={() => setHoveredSiteId(null)}
    >
      <span className="text-amber-200/80 block leading-snug">{translated}</span>
      {detail && <span className="block mt-0.5 text-[10px] text-amber-300/50 leading-snug">{detail}</span>}
      {siteId && (
        <span className="block mt-0.5 text-[9px] text-amber-500/60 italic">↑ highlight on map</span>
      )}
    </div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// Collapsible warning group (accordion section)
// ─────────────────────────────────────────────────────────────────────────────
const WarningGroup: React.FC<{
  label: string;
  warnings: string[];
  sites: Site[];
  setHoveredSiteId: (id: string | null) => void;
}> = ({ label, warnings, sites, setHoveredSiteId }) => {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-slate-700 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-3 py-2 bg-slate-800 hover:bg-slate-700/60 text-left"
      >
        <span className="text-xs font-medium text-slate-300">
          {label}
          <span className="ml-1.5 text-slate-500">({warnings.length})</span>
        </span>
        <svg
          className={`w-3 h-3 text-slate-400 transition-transform flex-shrink-0 ${open ? 'rotate-90' : ''}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </button>
      {open && (
        <div className="p-2 space-y-1 bg-slate-800/40">
          {warnings.map((w, i) => (
            <WarningItem key={i} text={w} sites={sites} setHoveredSiteId={setHoveredSiteId} />
          ))}
        </div>
      )}
    </div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────
// Diagnostics drawer (opens via portal from right edge)
// ─────────────────────────────────────────────────────────────────────────────
const DiagnosticsDrawer: React.FC<{
  recommendation: Recommendation;
  sites: Site[];
  onClose: () => void;
  setHoveredSiteId: (id: string | null) => void;
}> = ({ recommendation, sites, onClose, setHoveredSiteId }) => {
  const feedback = recommendation.solution_feedback ?? [];
  const warnings = useMemo(
    () => dedupeWarnings(recommendation.warnings ?? []),
    [recommendation.warnings],
  );

  // Group solution_feedback by truck_id for hierarchical display
  const feedbackByTruck = useMemo(() => {
    const map = new Map<string, typeof feedback>();
    for (const item of feedback) {
      const key = item.truck_id ?? '__other__';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(item);
    }
    return map;
  }, [feedback]);

  // Group raw warnings by category
  const grouped = useMemo(() => {
    const g: Partial<Record<WarningCategory, string[]>> = {};
    for (const w of warnings) {
      const cat = categorizeWarning(w);
      if (!g[cat]) g[cat] = [];
      g[cat]!.push(w);
    }
    return g;
  }, [warnings]);

  const activeCategories = CATEGORY_ORDER.filter((c) => (grouped[c]?.length ?? 0) > 0);
  const { critical, warnings: warnCount, info } = getIssueCounts(recommendation);
  const total = critical + warnCount + info;

  const truckKeys = [...feedbackByTruck.keys()].filter((k) => k !== '__other__').sort();
  const otherItems = feedbackByTruck.get('__other__') ?? [];

  const planHealth = useMemo(() => computePlanHealth(recommendation), [recommendation]);

  return createPortal(
    <div className="fixed inset-0" style={{ zIndex: 9998 }}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="absolute top-0 right-0 h-full w-[440px] max-w-[92vw] bg-slate-800 border-l border-slate-600 shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-slate-700">
          <h3 className="text-sm font-semibold text-white">Diagnostics</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none px-1">&times;</button>
        </div>

        {/* Severity summary */}
        <div className="flex-shrink-0 px-4 py-2.5 border-b border-slate-700 flex flex-wrap items-center gap-3">
          {critical > 0 && (
            <span className="flex items-center gap-1.5 text-xs font-medium text-red-400">
              Critical issues ({critical})
            </span>
          )}
          {warnCount > 0 && (
            <span className="flex items-center gap-1.5 text-xs font-medium text-amber-400">
              Operational warnings ({warnCount})
            </span>
          )}
          {info > 0 && (
            <span className="flex items-center gap-1.5 text-xs font-medium text-blue-400">
              Planning insights ({info})
            </span>
          )}
          {total === 0 && <span className="text-xs text-slate-400">No issues detected</span>}
        </div>

        {/* Plan Health summary */}
        {planHealth.length > 0 && (
          <div className="flex-shrink-0 px-4 py-3 border-b border-slate-700 space-y-2">
            <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">Plan Health</p>
            <div className="space-y-1">
              {planHealth.map((item, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <span className={`mt-0.5 flex-shrink-0 font-medium ${item.ok ? 'text-green-400' : 'text-amber-400'}`}>
                    {item.ok ? '✓' : '⚠'}
                  </span>
                  <span className={item.ok ? 'text-slate-300' : 'text-amber-200/80'}>{item.label}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-5">

          {/* Per-truck groups */}
          {truckKeys.map((truckId) => {
            const items = feedbackByTruck.get(truckId)!;
            return (
              <div key={truckId}>
                <p className="text-xs font-semibold text-slate-300 mb-2 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 17a2 2 0 11-4 0 2 2 0 014 0zM19 17a2 2 0 11-4 0 2 2 0 014 0z" />
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16V6a1 1 0 00-1-1H4a1 1 0 00-1 1v10l2 2h10l2-2z" />
                  </svg>
                  {truckId}
                </p>
                <div className="space-y-1.5 pl-1">
                  {items.map((item, i) => {
                    const siteId = extractSiteId(item.message, sites);
                    const translated = translateMessage(item.code, item.message);
                    const detail = getTranslationDetail(item.code, item.message);
                    const isError = item.type === 'error';
                    const isInfo = item.type === 'info';
                    const borderColor = isError ? 'border-red-500' : isInfo ? 'border-blue-500' : 'border-amber-500';
                    const bgColor = isError ? 'bg-red-900/20' : isInfo ? 'bg-blue-900/20' : 'bg-amber-900/20';
                    const textColor = isError ? 'text-red-200/90' : isInfo ? 'text-blue-200/90' : 'text-amber-200/90';
                    const detailColor = isError ? 'text-red-300/60' : isInfo ? 'text-blue-300/60' : 'text-amber-300/60';
                    return (
                      <div
                        key={i}
                        className={`text-xs px-2.5 py-2 border-l-2 rounded-r transition-colors ${borderColor} ${bgColor} ${siteId ? 'cursor-default hover:brightness-110' : ''}`}
                        onMouseEnter={() => siteId && setHoveredSiteId(siteId)}
                        onMouseLeave={() => setHoveredSiteId(null)}
                      >
                        <span className={`block leading-snug ${textColor}`}>{translated}</span>
                        {detail && <span className={`block mt-0.5 text-[10px] leading-snug ${detailColor}`}>{detail}</span>}
                        {siteId && <span className="block mt-0.5 text-[9px] text-slate-500 italic">↑ highlight on map</span>}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}

          {/* Ungrouped feedback items (no truck_id) */}
          {otherItems.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-slate-400 mb-2">Other observations</p>
              <div className="space-y-1.5">
                {otherItems.map((item, i) => {
                  const siteId = extractSiteId(item.message, sites);
                  const translated = translateMessage(item.code, item.message);
                  const detail = getTranslationDetail(item.code, item.message);
                  const isError = item.type === 'error';
                  const isInfo = item.type === 'info';
                  const borderColor = isError ? 'border-red-500' : isInfo ? 'border-blue-500' : 'border-amber-500';
                  const bgColor = isError ? 'bg-red-900/20' : isInfo ? 'bg-blue-900/20' : 'bg-amber-900/20';
                  const textColor = isError ? 'text-red-200/90' : isInfo ? 'text-blue-200/90' : 'text-amber-200/90';
                  const detailColor = isError ? 'text-red-300/60' : isInfo ? 'text-blue-300/60' : 'text-amber-300/60';
                  return (
                    <div
                      key={i}
                      className={`text-xs px-2.5 py-2 border-l-2 rounded-r transition-colors ${borderColor} ${bgColor} ${siteId ? 'cursor-default hover:brightness-110' : ''}`}
                      onMouseEnter={() => siteId && setHoveredSiteId(siteId)}
                      onMouseLeave={() => setHoveredSiteId(null)}
                    >
                      <span className={`block leading-snug ${textColor}`}>{translated}</span>
                      {detail && <span className={`block mt-0.5 text-[10px] leading-snug ${detailColor}`}>{detail}</span>}
                      {siteId && <span className="block mt-0.5 text-[9px] text-slate-500 italic">↑ highlight on map</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Grouped raw warnings (categorised) */}
          {activeCategories.length > 0 && (
            <div className="space-y-2">
              {(truckKeys.length > 0 || otherItems.length > 0) && (
                <div className="border-t border-slate-700/60 pt-3">
                  <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-2">Additional warnings</p>
                </div>
              )}
              {activeCategories.map((cat) => (
                <WarningGroup
                  key={cat}
                  label={CATEGORY_LABELS[cat]}
                  warnings={grouped[cat]!}
                  sites={sites}
                  setHoveredSiteId={setHoveredSiteId}
                />
              ))}
            </div>
          )}

          {total === 0 && (
            <p className="text-sm text-slate-400 text-center py-8">No diagnostics to show.</p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
};

// Container Moves Section
const ContainerMovesSection: React.FC<{ moves: ContainerMove[] }> = ({ moves }) => {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="border border-slate-700 rounded-lg overflow-hidden">
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center justify-between px-2.5 py-1.5 bg-slate-800 hover:bg-slate-750 text-left"
      >
        <span className="text-xs font-medium text-slate-300">
          Container Moves ({moves.length})
        </span>
        <svg
          className={`w-3 h-3 text-slate-400 transition-transform ${collapsed ? '' : 'rotate-90'}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </button>
      {!collapsed && (
        <div className="max-h-56 overflow-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-700 bg-slate-850">
                <th className="px-2 py-1 text-left text-slate-400 font-medium">Day</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">Truck</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">Bay</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">S/N</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">From</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">To</th>
                <th className="px-2 py-1 text-left text-slate-400 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {moves.map((m, i) => (
                <tr key={i} className="border-b border-slate-700/50 hover:bg-slate-800/40">
                  <td className="px-2 py-1 text-slate-400">{m.day_index}</td>
                  <td className="px-2 py-1 text-slate-300">{m.truck_id}</td>
                  <td className="px-2 py-1 font-mono text-slate-300">{m.bay_id}</td>
                  <td className="px-2 py-1 font-mono">
                    {m.bay_serial_number
                      ? <span className="text-emerald-400">{m.bay_serial_number}</span>
                      : <span className="text-slate-600">—</span>
                    }
                  </td>
                  <td className="px-2 py-1 text-slate-300">{m.from_site_name}</td>
                  <td className="px-2 py-1 text-slate-300">{m.to_site_name}</td>
                  <td className="px-2 py-1 text-slate-500 capitalize">{m.reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export const Operations: React.FC = () => {
  const {
    objectiveFunction,
    setObjectiveFunction,
    recommendationState,
    recommendation,
    computationTime,
    generateRecommendation,
    cancelRecommendation,
    approveRecommendation,
    rejectRecommendation,
    clearRecommendation,
    error,
    clearError,
    activeLayers,
    customSpeedKmh,
    getEffectiveSpeedKmh,
    constraintOverrides,
    validateFleetConfig,
    validationErrors,
    sites,
    setHoveredSiteId,
    visibleRouteIds,
    toggleRouteVisibility,
    focusedRouteId,
    setFocusedRouteId,
    solveProgress,
    optimalDaysResult,
    optimalDaysMode,
    fleetConfig,
    timeAdvanceLoading,
    simulationRestartLoading,
    advanceSimulationTime,
    restartSimulationState,
  } = useAppStore();

  const [showApproveModal, setShowApproveModal] = useState(false);
  const [showEnergyDrawer, setShowEnergyDrawer] = useState(false);
  const [showCostDrawer, setShowCostDrawer] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [showChatbot, setShowChatbot] = useState(false);
  const [isApplying, setIsApplying] = useState(false);
  const [exportState, setExportState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [showPlanExplanation, setShowPlanExplanation] = useState(false);
  const [resultPanelOpen, setResultPanelOpen] = useState(true);
  const [showDeveloperTrace, setShowDeveloperTrace] = useState(false);
  const [showInfeasibleTrace, setShowInfeasibleTrace] = useState(false);
  const [nextStepsDays, setNextStepsDays] = useState(1);
  const [clockHoursInput, setClockHoursInput] = useState('2');

  const objectives: { key: ObjectiveFunction; label: string; desc: string }[] = [
    { key: 'balanced', label: 'Balanced', desc: 'Balance cost, time and flaring reduction' },
    { key: 'time', label: 'Least Time', desc: 'Optimize for shortest total route time' },
    { key: 'flaring', label: 'Less Flaring', desc: 'Prioritize sites to reduce gas burning' },
  ];

  const isComputing = recommendationState === 'computing';
  const hasRecommendation = recommendation && recommendationState === 'ready';
  const isApproved = recommendationState === 'approved';
  const isInfeasible = recommendationState === 'infeasible';
  const displayedSolveProgress = Math.min(99, Math.max(7, Math.round(solveProgress)));
  const solverStage = useMemo(() => {
    if (displayedSolveProgress < 28) {
      return {
        label: 'Preparing network model',
        detail: 'Loading constraints, truck state and site priorities.',
      };
    }
    if (displayedSolveProgress < 58) {
      return {
        label: 'Exploring route candidates',
        detail: 'Comparing feasible truck-day combinations and service order.',
      };
    }
    if (displayedSolveProgress < 84) {
      return {
        label: 'Refining best plan',
        detail: 'Improving route quality, balance and timing trade-offs.',
      };
    }
    return {
      label: 'Final validation',
      detail: 'Checking route consistency and assembling the final recommendation.',
    };
  }, [displayedSolveProgress]);
  const getClockHoursValue = () => {
    const parsed = Number(clockHoursInput);
    if (!Number.isFinite(parsed)) return 2;
    return Math.min(168, Math.max(0.5, parsed));
  };

  const handleAdvanceHours = async () => {
    await advanceSimulationTime(getClockHoursValue());
  };

  const handleRewindHours = async () => {
    await advanceSimulationTime(-getClockHoursValue());
  };

  const handleRestartSimulation = async () => {
    await restartSimulationState();
  };

  const planCompressionHint = useMemo(() => {
    if (!recommendation) return null;
    // Do not show "compressible" hints when force-end constraints are active.
    // Those constraints can intentionally require extra calendar days even when
    // raw driving time looks short.
    if (fleetConfig.some((tc) => tc.force_end_enabled)) return null;
    const fleetSize = Math.max(fleetConfig.length, 1);
    const totalRouteTime = recommendation.routes.reduce((sum, route) => sum + getRouteDisplayTimeHours(route), 0);
    const minDaysRequired = Math.max(
      1,
      Math.ceil(totalRouteTime / (fleetSize * 9.0)),
    );
    if (recommendation.horizon_days <= minDaysRequired) return null;
    return {
      minDaysRequired,
      forced: optimalDaysMode === 'force',
    };
  }, [fleetConfig.length, optimalDaysMode, recommendation]);

  const closeModal = useCallback(() => setShowApproveModal(false), []);

  const handleApprove = async (nextSteps: boolean) => {
    closeModal();
    setIsApplying(true);
    try {
      await approveRecommendation(nextSteps, nextSteps ? nextStepsDays : undefined);
    } finally {
      setIsApplying(false);
    }
  };

  useEffect(() => {
    if (!showApproveModal) return;
    setNextStepsDays(recommendation?.horizon_days ?? 1);
  }, [showApproveModal, recommendation?.horizon_days]);

  // Close modal/drawer on ESC key
  useEffect(() => {
    if (!showApproveModal && !showEnergyDrawer && !showCostDrawer && !showDiagnostics && !showChatbot) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (showChatbot) setShowChatbot(false);
        if (showDiagnostics) setShowDiagnostics(false);
        if (showCostDrawer) setShowCostDrawer(false);
        if (showEnergyDrawer) setShowEnergyDrawer(false);
        if (showApproveModal) closeModal();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showApproveModal, showEnergyDrawer, showCostDrawer, showDiagnostics, showChatbot, closeModal]);

  const showRecommended = activeLayers.has('recommendedRoutes');
  const visibleRoutes = recommendation && showRecommended ? recommendation.routes : [];

  // Sort routes for the list
  const sortedRoutes = [...visibleRoutes].sort((a, b) => {
    const truckCompare = a.truck_id.localeCompare(b.truck_id);
    if (truckCompare !== 0) return truckCompare;
    return a.day_index - b.day_index;
  });

  const selectedRoute = sortedRoutes.find((r) => r.id === focusedRouteId) || null;
  const developerTraceText = useMemo(
    () => (recommendation ? buildDeveloperTrace(recommendation, { sites, fleetConfig }) : ''),
    [recommendation, sites, fleetConfig],
  );

  // Route sanity checks — run once per recommendation
  const routeWarnings = useMemo(
    () => validateAllRoutes(recommendation?.routes ?? []),
    [recommendation],
  );

  // ─── Pre-recommendation view (objective selection, trucks, constraints, generate button) ───
  const renderPreRecommendation = () => (
    <div className="flex-1 p-4 space-y-6 overflow-y-auto">
      {/* Error display */}
      {error && (
        <div className="p-3 bg-red-900/50 border border-red-600 rounded-lg">
          <div className="flex justify-between items-start">
            <p className="text-sm text-red-200">{error}</p>
            <button onClick={clearError} className="text-red-400 hover:text-red-200">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}

      {/* Objective Function */}
      <div>
        <h3 className="text-sm font-medium text-slate-400 mb-3">Objective Function</h3>
        <div className="space-y-2">
          {objectives.map((obj) => (
            <label
              key={obj.key}
              className={`flex items-center p-3 rounded-lg cursor-pointer transition-colors ${
                objectiveFunction === obj.key
                  ? 'bg-blue-600/20 border border-blue-500'
                  : 'bg-slate-800 border border-transparent hover:border-slate-600'
              }`}
            >
              <input
                type="radio"
                name="objective"
                value={obj.key}
                checked={objectiveFunction === obj.key}
                onChange={() => setObjectiveFunction(obj.key)}
                className="sr-only"
              />
              <div
                className={`w-4 h-4 rounded-full border-2 mr-3 flex items-center justify-center ${
                  objectiveFunction === obj.key
                    ? 'border-blue-500 bg-blue-500'
                    : 'border-slate-500'
                }`}
              >
                {objectiveFunction === obj.key && (
                  <div className="w-2 h-2 rounded-full bg-white"></div>
                )}
              </div>
              <div className="flex-1">
                <p className="font-medium">{obj.label}</p>
                <p className="text-xs text-slate-400">{obj.desc}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      {/* Trucks Configuration */}
      <TrucksPanel />

      {/* Constraints Display */}
      <div>
        <h3 className="text-sm font-medium text-slate-400 mb-3">Active Constraints</h3>
        <div className="space-y-2 text-sm">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${constraintOverrides.maxContainers !== undefined ? 'bg-blue-500' : 'bg-green-500'}`}></span>
            <span>Truck capacity: max {constraintOverrides.maxContainers ?? 3} containers</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${constraintOverrides.maxDriverHours !== undefined ? 'bg-blue-500' : 'bg-green-500'}`}></span>
            <span>Driver time: max {constraintOverrides.maxDriverHours ?? 9} hours</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${constraintOverrides.swapTimeMin !== undefined ? 'bg-blue-500' : 'bg-green-500'}`}></span>
            <span>Service time: {constraintOverrides.swapTimeMin ?? 20} min per stop</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${customSpeedKmh !== null ? 'bg-blue-500' : 'bg-green-500'}`}></span>
            <span>Avg speed: {getEffectiveSpeedKmh()} km/h</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${constraintOverrides.costPerKm !== undefined ? 'bg-blue-500' : 'bg-green-500'}`}></span>
            <span>Cost per km: {constraintOverrides.costPerKm ?? 2.25} EUR/km</span>
          </div>
        </div>
      </div>

      {/* Validation Banner */}
      {validationErrors.length > 0 && (
        <div className="p-3 bg-amber-900/50 border border-amber-600 rounded-lg">
          <div className="flex items-start gap-2">
            <svg className="w-5 h-5 text-amber-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
            <div>
              <p className="text-sm font-medium text-amber-200">Missing information</p>
              <ul className="mt-1 text-xs text-amber-300 space-y-0.5">
                {validationErrors.slice(0, 5).map((err, i) => (
                  <li key={i}>{err.message}</li>
                ))}
                {validationErrors.length > 5 && (
                  <li>...and {validationErrors.length - 5} more</li>
                )}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Generate / Stop Button */}
      {isComputing ? (
        <div className="flex gap-2">
          <div className="flex-1 py-3 rounded-lg bg-slate-700 text-slate-300 text-sm font-medium text-center cursor-not-allowed">
            {solverStage.label}
          </div>
          <button
            onClick={cancelRecommendation}
            className="px-4 py-3 rounded-lg font-medium transition-colors bg-red-700 hover:bg-red-600 text-white text-sm"
            title="Cancel route generation"
          >
            Stop
          </button>
        </div>
      ) : (
        <button
          onClick={() => {
            const errors = validateFleetConfig();
            if (errors.length === 0) {
              generateRecommendation();
            }
          }}
          disabled={validationErrors.length > 0}
          className={`w-full py-3 rounded-lg font-medium transition-colors ${
            validationErrors.length > 0
              ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
              : 'bg-blue-600 hover:bg-blue-700 text-white'
          }`}
        >
          {validationErrors.length > 0 ? 'Fix Configuration Errors' : 'Generate Recommendations'}
        </button>
      )}

      {/* Quick simulation clock controls */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-slate-400">Advance Simulation Clock</h3>
          <button
            onClick={handleRestartSimulation}
            disabled={isComputing || simulationRestartLoading}
            className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
              isComputing || simulationRestartLoading
                ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                : 'bg-red-700/80 hover:bg-red-700 text-red-100'
            }`}
          >
            {simulationRestartLoading ? 'Restarting…' : 'Restart'}
          </button>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={0.5}
            max={168}
            step={0.5}
            value={clockHoursInput}
            onChange={(e) => setClockHoursInput(e.target.value)}
            className="w-24 px-2 py-2 rounded bg-slate-800 border border-slate-600 text-slate-100 text-sm"
            aria-label="Simulation hours"
          />
          <button
            onClick={handleRewindHours}
            disabled={isComputing || timeAdvanceLoading}
            className={`px-3 py-2 rounded text-sm font-medium transition-colors ${
              isComputing || timeAdvanceLoading
                ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                : 'bg-slate-700 hover:bg-slate-600 text-slate-100'
            }`}
          >
            Rewind hours
          </button>
          <button
            onClick={handleAdvanceHours}
            disabled={isComputing || timeAdvanceLoading}
            className={`px-3 py-2 rounded text-sm font-medium transition-colors ${
              isComputing || timeAdvanceLoading
                ? 'bg-slate-700 text-slate-400 cursor-not-allowed'
                : 'bg-slate-700 hover:bg-slate-600 text-slate-100'
            }`}
          >
            Advance hours
          </button>
        </div>
      </div>

      {/* Solver loading indicator */}
      {isComputing && (
        <div className="rounded-2xl border border-cyan-500/30 bg-slate-800/90 p-4 shadow-[0_18px_50px_rgba(8,145,178,0.12)]">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <div className="h-2.5 w-2.5 rounded-full bg-cyan-400 shadow-[0_0_16px_rgba(34,211,238,0.8)] solver-pulse" />
                <p className="text-sm font-semibold text-slate-100">Optimising routes</p>
                <span className="rounded-full border border-cyan-400/25 bg-cyan-500/10 px-2 py-0.5 text-[11px] font-medium text-cyan-200">
                  {displayedSolveProgress}%
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-300">{solverStage.label}</p>
              <p className="mt-1 text-xs text-slate-400">{solverStage.detail}</p>
            </div>
            <div className="relative h-14 w-14 flex-shrink-0">
              <svg className="h-14 w-14 -rotate-90" viewBox="0 0 48 48" fill="none">
                <circle cx="24" cy="24" r="20" stroke="#1e293b" strokeWidth="4" />
                <circle
                  cx="24"
                  cy="24"
                  r="20"
                  stroke="#22d3ee"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeDasharray={125.6}
                  strokeDashoffset={125.6 - (125.6 * displayedSolveProgress) / 100}
                />
              </svg>
              <span className="absolute inset-0 flex items-center justify-center text-[11px] font-semibold text-slate-200">
                {displayedSolveProgress}%
              </span>
            </div>
          </div>

          <div className="mt-4">
            <div className="h-3 overflow-hidden rounded-full bg-slate-900/80 ring-1 ring-inset ring-cyan-500/15">
              <div
                className="solver-progress-fill h-full rounded-full"
                style={{ width: `${displayedSolveProgress}%` }}
              />
            </div>
            <div className="mt-2 flex items-center justify-between text-[11px] text-slate-500">
              <span>Model setup</span>
              <span>Search</span>
              <span>Validation</span>
            </div>
          </div>

          <div className="mt-3 flex items-center justify-between gap-3 text-[11px]">
            <p className="text-slate-500">
              Search progress is estimated. Final checks can pause briefly before completion.
            </p>
            <p className="whitespace-nowrap text-cyan-300">OR-Tools VRP solver running</p>
          </div>
        </div>
      )}

      {/* Infeasible Result */}
      {isInfeasible && recommendation && (
        <div className="p-4 bg-red-900/40 border border-red-600 rounded-lg space-y-2">
          <h3 className="font-medium flex items-center gap-2 text-red-300">
            <svg className="w-5 h-5 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            No Feasible Plan
          </h3>
          {recommendation.reason_code && (
            <p className="text-xs font-mono text-red-400">{recommendation.reason_code}</p>
          )}
          <p className="text-sm text-red-200">{recommendation.reason_message || recommendation.explanation}</p>
          {computationTime !== null && (
            <p className="text-xs text-slate-400">Computed in {computationTime.toFixed(1)}s</p>
          )}
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            <button
              onClick={clearRecommendation}
              className="px-3 py-1.5 text-sm bg-slate-700 hover:bg-slate-600 rounded transition-colors"
            >
              Dismiss
            </button>
            {DEV_TRACE_ENABLED && (
              <button
                onClick={() => setShowInfeasibleTrace(!showInfeasibleTrace)}
                className="px-3 py-1.5 text-sm bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded transition-colors flex items-center gap-1.5"
              >
                <svg className="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                </svg>
                <span className="text-slate-300">Developer Trace</span>
              </button>
            )}
          </div>
          {showInfeasibleTrace && DEV_TRACE_ENABLED && (
            <div className="mt-2 space-y-1.5">
              <div className="flex justify-end">
                <button
                  onClick={() => navigator.clipboard.writeText(developerTraceText)}
                  className="px-2 py-1 text-[11px] rounded bg-slate-700 hover:bg-slate-600 text-slate-200"
                >
                  Copy trace
                </button>
              </div>
              <pre className="max-h-72 overflow-auto whitespace-pre text-[11px] leading-snug text-slate-300 bg-slate-950/60 border border-slate-600 rounded p-2">
                {developerTraceText}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );

  // ─── Recommendation results: master-detail layout ───
  const renderRecommendation = () => {
    const rec = recommendation!;
    const { critical, warnings: warnCount, info } = getIssueCounts(rec);
    const totalIssues = critical + warnCount + info;
    const isFallback = rec.feasibility_level && rec.feasibility_level !== 'STRICT';

    // Minimized floating button — shown when panel is collapsed
    if (!resultPanelOpen) {
      return (
        <div className="flex-1 relative">
          <button
            onClick={() => setResultPanelOpen(true)}
            className="absolute bottom-4 right-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-green-700 hover:bg-green-600 text-white text-sm font-medium shadow-lg transition-colors z-10"
            title="Reopen plan panel"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Plan Ready
          </button>
        </div>
      );
    }

    return (
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* ── 1. Plan Status ── */}
        <div className="flex-shrink-0 px-3 pt-3 pb-2 border-b border-slate-700 space-y-2">
          {/* Status header */}
          <h3 className="font-medium flex items-center gap-2 text-sm">
            <svg className={`w-4 h-4 ${isFallback ? 'text-amber-400' : 'text-green-500'}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={isFallback
                ? 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z'
                : 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z'} />
            </svg>
            {isFallback ? 'Fallback plan generated' : 'Recommendation Ready'}
            {/* Collapse button */}
            <button
              onClick={() => setResultPanelOpen(false)}
              className="ml-auto p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
              title="Minimise plan panel"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
          </h3>

          {/* Feasibility detail */}
          {isFallback && (
            <p className="text-xs text-amber-200/70">
              Some constraints were relaxed to produce a feasible plan.
            </p>
          )}

          {/* Issue summary + diagnostics button */}
          {totalIssues > 0 && (
            <div className="flex items-center justify-between">
              <div className="flex flex-wrap items-center gap-2.5">
                {critical > 0 && (
                  <span className="flex items-center gap-1 text-xs font-medium text-red-400">
                    Critical issues ({critical})
                  </span>
                )}
                {warnCount > 0 && (
                  <span className="flex items-center gap-1 text-xs font-medium text-amber-400">
                    Operational warnings ({warnCount})
                  </span>
                )}
                {info > 0 && (
                  <span className="flex items-center gap-1 text-xs font-medium text-blue-400">
                    Planning insights ({info})
                  </span>
                )}
              </div>
              <button
                onClick={() => setShowDiagnostics(true)}
                className="text-xs text-blue-400 hover:text-blue-300 underline underline-offset-2 flex-shrink-0 ml-2"
              >
                View details
              </button>
            </div>
          )}
        </div>

        {/* ── 2. Metrics ── */}
        <div className="flex-shrink-0 px-3 py-1.5 border-b border-slate-700 space-y-1.5">
          <div className="grid grid-cols-3 gap-1">
            <div className="p-1 bg-slate-700 rounded">
              <p className="text-[10px] text-slate-400">Routes</p>
              <p className="text-sm font-bold">{visibleRoutes.length}</p>
            </div>
            <div className="p-1 bg-slate-700 rounded">
              <p className="text-[10px] text-slate-400">Distance</p>
              <p className="text-sm font-bold">{rec.total_distance_km.toFixed(0)} km</p>
            </div>
            <div
              className="p-1 bg-slate-700 rounded cursor-pointer hover:bg-slate-600 transition-colors"
              onClick={() => { setShowCostDrawer(true); setShowEnergyDrawer(false); }}
              title="Click to see breakdown"
            >
              <p className="text-[10px] text-slate-400">Cost</p>
              <p className="text-sm font-bold text-green-400">{rec.total_cost_eur.toFixed(0)} EUR</p>
            </div>
            <div className="p-1 bg-slate-700 rounded">
              <p className="text-[10px] text-slate-400">Sites</p>
              <p className="text-sm font-bold">{rec.sites_served}</p>
            </div>
            <div
              className="p-1 bg-slate-700 rounded cursor-pointer hover:bg-slate-600 transition-colors"
              onClick={() => { setShowEnergyDrawer(true); setShowCostDrawer(false); }}
              title="Click to see breakdown"
            >
              <p className="text-[10px] text-slate-400">Energy</p>
              <p className="text-sm font-bold">
                {rec.total_mwh_moved > 0 ? `${rec.total_mwh_moved.toFixed(1)} MWh` : 'N/A'}
              </p>
            </div>
            <div className="p-1 bg-slate-700 rounded">
              <p className="text-[10px] text-slate-400">EUR/MWh</p>
              <p className="text-sm font-bold text-blue-400">
                {rec.eur_per_mwh !== null ? rec.eur_per_mwh.toFixed(2) : 'N/A'}
              </p>
            </div>
          </div>
          {computationTime && (
            <p className="text-[10px] text-slate-500">Computed in {computationTime.toFixed(2)}s</p>
          )}
        </div>

        {/* ── 3. Risk & Operational Indicators ── */}
        <div className="flex-shrink-0 px-3 py-2 border-b border-slate-700 space-y-2">
          <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide">Operational Risk</p>

          {/* Flaring exposure — always shown, regulatory cap = 2h/week */}
          {(() => {
            const WEEKLY_FLARING_CAP_H = 2.0;
            const hrs = rec.flaring_exposure_hours ?? 0;
            const pct = Math.min(100, (hrs / WEEKLY_FLARING_CAP_H) * 100);
            const isOver = hrs >= WEEKLY_FLARING_CAP_H;
            const isWarn = hrs > 0 && hrs < WEEKLY_FLARING_CAP_H;
            const barColor = isOver ? 'bg-red-500' : isWarn ? 'bg-amber-500' : 'bg-green-600';
            const labelColor = isOver ? 'text-red-400' : isWarn ? 'text-amber-400' : 'text-green-400';
            return (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] text-slate-400">Flaring exposure (plan)</span>
                  <span className={`text-[10px] font-bold ${labelColor}`}>
                    {hrs.toFixed(2)}h / {WEEKLY_FLARING_CAP_H}h weekly cap
                    {isOver && ' ⚠ OVER LIMIT'}
                  </span>
                </div>
                <div className="w-full h-1.5 bg-slate-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all duration-500 ${barColor}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                {isOver && (
                  <p className="text-[10px] text-red-400 mt-0.5">Regulatory limit exceeded — serve all producers</p>
                )}
              </div>
            );
          })()}

          {/* Risk score + container imbalance */}
          <div className="flex flex-wrap gap-2">
            {rec.solution_risk_score != null && (() => {
              const score = rec.solution_risk_score!;
              const label = score <= 3 ? 'LOW' : score <= 4 ? 'OK' : 'HIGH';
              const color = score <= 3 ? 'text-green-400' : score <= 4 ? 'text-yellow-400' : 'text-red-400';
              const bg = score <= 3 ? 'bg-green-900/40' : score <= 4 ? 'bg-yellow-900/40' : 'bg-red-900/40';
              return (
                <span className={`text-xs px-2 py-0.5 rounded font-medium ${color} ${bg}`} title="Solution risk score 0–10. Target: 3–4.">
                  Risk {score.toFixed(1)}/10 — {label}
                </span>
              );
            })()}
            {(rec.end_of_horizon_imbalance ?? 0) > 0 && (
              <span className="text-xs px-2 py-0.5 rounded font-medium text-amber-400 bg-amber-900/30" title="Containers still on trucks after final day. Target: 0.">
                {rec.end_of_horizon_imbalance} container{rec.end_of_horizon_imbalance !== 1 ? 's' : ''} unbalanced
              </span>
            )}
          </div>
          <p className="text-[10px] text-slate-500">Malmi: pipeline-connected — no flaring risk</p>
        </div>

        {/* ── Timing scenario sensitivity analysis ── */}
        {(() => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const scenarioEntry: any = (rec.solution_feedback ?? []).find(
            (f: any) => f.code === 'SCENARIO_SELECTION' && Array.isArray((f as any).all_scenarios) && (f as any).all_scenarios.length > 1,
          );
          if (!scenarioEntry) return null;
          const all: Array<{ name: string; wait_hours: number; stockout_cost_eur: number; flaring_cost_eur: number; routing_cost_eur: number; total_cost_eur: number; valid: boolean }> = scenarioEntry.all_scenarios;
          const selected: string = scenarioEntry.scenario;
          const labels: Record<string, string> = { ACT_NOW: 'Act Now', WAIT_12H: 'Wait 12h', WAIT_24H: 'Wait 24h' };
          const minTotal = Math.min(...all.filter(s => s.valid).map(s => s.total_cost_eur));
          return (
            <div className="mx-3 mb-2 p-2.5 bg-slate-800/60 border border-slate-700 rounded-lg">
              <p className="text-[10px] font-semibold text-slate-300 uppercase tracking-wide mb-2">Timing Scenarios</p>
              <div className="flex gap-1.5">
                {all.map(s => {
                  const isSelected = s.name === selected;
                  const isCheapest = s.valid && s.total_cost_eur === minTotal;
                  return (
                    <div
                      key={s.name}
                      className={`flex-1 rounded px-1.5 py-1 text-[10px] border ${
                        isSelected
                          ? 'bg-blue-900/40 border-blue-500 text-blue-200'
                          : s.valid
                            ? 'bg-slate-700/40 border-slate-600 text-slate-400'
                            : 'bg-slate-800/30 border-slate-700 text-slate-600'
                      }`}
                    >
                      <div className="font-semibold flex items-center gap-1">
                        {labels[s.name] ?? s.name}
                        {isSelected && <span className="text-blue-400">✓</span>}
                        {isCheapest && !isSelected && <span className="text-emerald-400" title="Cheapest valid option">★</span>}
                      </div>
                      {s.valid ? (
                        <>
                          <div className="mt-0.5">€{Math.round(s.total_cost_eur).toLocaleString()}</div>
                          {s.stockout_cost_eur > 0 && (
                            <div className="text-red-400">+€{Math.round(s.stockout_cost_eur).toLocaleString()} stockout</div>
                          )}
                          {s.flaring_cost_eur > 0 && (
                            <div className="text-amber-400">+€{Math.round(s.flaring_cost_eur).toLocaleString()} flaring</div>
                          )}
                        </>
                      ) : (
                        <div className="mt-0.5 italic">Infeasible</div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })()}

        {/* ── Optimal days explanation (when optimize mode was used) ── */}
        {optimalDaysMode === 'optimize' && optimalDaysResult && (
          <div className="mx-3 mb-2 p-2.5 bg-emerald-900/20 border border-emerald-700/40 rounded-lg">
            <div className="flex items-center gap-1.5 mb-1.5">
              <span className="text-xs font-semibold text-emerald-300">
                {optimalDaysResult.explanation.summary.replace(/^[^A-Za-z0-9]+/, '').trim()}
              </span>
            </div>
            <ul className="space-y-0.5">
              {optimalDaysResult.explanation.details.map((d, i) => (
                <li key={i} className="text-xs text-slate-400">{d}</li>
              ))}
            </ul>
            <div className="flex gap-2 mt-2">
              {optimalDaysResult.tested_days.map((t) => (
                <div
                  key={t.days}
                  className={`flex flex-col items-center px-2 py-1 rounded text-xs ${
                    t.days === optimalDaysResult.days_used
                      ? 'bg-emerald-700/40 text-emerald-200 ring-1 ring-emerald-500'
                      : t.feasible
                        ? 'bg-slate-700/50 text-slate-400'
                        : 'bg-slate-800/50 text-slate-600'
                  }`}
                >
                  <span className="font-medium">{t.days}d</span>
                  <span>{t.feasible ? `€${Math.round(t.total_cost_eur)}` : '—'}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {optimalDaysMode === 'force' && planCompressionHint && (
          <div className="mx-3 mb-2 p-2.5 bg-blue-900/20 border border-blue-700/40 rounded-lg">
            <div className="flex items-center gap-1.5 mb-1.5">
              <span className="text-blue-400 text-sm">i</span>
              <span className="text-xs font-semibold text-blue-300">
                Forced-day plan uses {recommendation?.horizon_days ?? 0} days, but current workload fits in about {planCompressionHint.minDaysRequired} working {planCompressionHint.minDaysRequired === 1 ? 'day' : 'days'}.
              </span>
            </div>
            <p className="text-xs text-slate-400">
              This plan is valid under the current constraint, but it is not the most compact use of the fleet. Use `Optimize days` if you want the system to collapse light work into fewer days.
            </p>
          </div>
        )}

        {/* ── 4. Routes — master-detail ── */}
        <div className="flex-1 flex overflow-hidden min-h-0">
          {/* Left: day summary + route list */}
          <div className="w-2/5 flex-shrink-0 border-r border-slate-700 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-y-auto p-2 space-y-2">
              {visibleRoutes.length > 0 && <DaySummary routes={visibleRoutes} />}
              <div className="space-y-1">
                <p className="text-xs font-medium text-slate-400">Routes:</p>
                {sortedRoutes.map((route) => (
                  <RouteListItem
                    key={route.id}
                    route={route}
                    selected={route.id === focusedRouteId}
                    visible={visibleRouteIds.has(route.id)}
                    warnings={routeWarnings.get(route.id) ?? []}
                    onClick={() => setFocusedRouteId(route.id)}
                    onToggleVisibility={(e) => { e.stopPropagation(); toggleRouteVisibility(route.id); }}
                  />
                ))}
              </div>

	              {/* Collapsible plan explanation */}
              <div className="border border-slate-700 rounded-lg overflow-hidden">
                <button
                  onClick={() => setShowPlanExplanation(!showPlanExplanation)}
                  className="w-full flex items-center justify-between px-3 py-2 bg-slate-800 hover:bg-slate-700/60 text-left"
                >
                  <span className="text-xs font-medium text-slate-300">Explain this plan</span>
                  <svg
                    className={`w-3 h-3 text-slate-400 transition-transform flex-shrink-0 ${showPlanExplanation ? 'rotate-90' : ''}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                </button>
                {showPlanExplanation && (
                  <div className="p-3 bg-slate-800/40 space-y-3 text-xs text-slate-300">
                    {/* Planning Insights */}
                    {(() => {
                      const MAX_DRIVER_HOURS = 9.0;
                      const fleetSize = Math.max(fleetConfig.length, 1);
                      const totalDriveTime = rec.routes.reduce((s, r) => s + getRouteDisplayTimeHours(r), 0);
                      const minDaysRequired = Math.max(
                        1,
                        Math.ceil(totalDriveTime / (fleetSize * MAX_DRIVER_HOURS)),
                      );
                      if (minDaysRequired < rec.horizon_days && rec.horizon_days > 1) {
                        return (
                          <p className="text-blue-300/80">
                            ✓ Demand can be satisfied in{' '}
                            <span className="font-medium">{minDaysRequired} working {minDaysRequired === 1 ? 'day' : 'days'}</span>
                            {' '}({rec.horizon_days} available) — unused vehicle capacity remains.
                          </p>
                        );
                      }
                      return null;
                    })()}

                    {/* Plan Explanation */}
                    {generatePlanNarrative(visibleRoutes).map((s, i) => (
                      <p key={i} className="leading-snug">{s}</p>
                    ))}

                    {/* Why This Plan */}
                    {generateWhyThisPlan(rec).map((r, i) => (
                      <p key={i} className="text-slate-400 leading-snug">{r}</p>
                    ))}

                    {/* Routing insights */}
                    {(rec.fill_sites_count ?? 0) > 0 && (() => {
                      const visited = rec.fill_sites_visited ?? 0;
                      const total = rec.fill_sites_count ?? 0;
                      return visited > 0 ? (
                        <p className="text-slate-400">{visited} of {total} top-up {total === 1 ? 'site' : 'sites'} used to extend routes.</p>
                      ) : null;
                    })()}
                    {(rec.transfer_hubs_visited ?? 0) > 0 && (
                      <p className="text-slate-400">
                        {rec.transfer_hubs_visited === 1
                          ? 'Route includes 1 transfer hub stop.'
                          : `Route includes ${rec.transfer_hubs_visited} transfer hub stops.`}
                      </p>
                    )}

                    {/* Fallback improvement hints */}
                    {rec.feasibility_level && rec.feasibility_level !== 'STRICT' && (
                      <div className="pt-1 border-t border-slate-700/50 space-y-0.5 text-amber-300/70">
                        <p className="font-medium text-amber-300/90">This plan would likely improve if:</p>
                        <p>• an additional working day were available</p>
                        <p>• intermediate container swaps were allowed</p>
                        <p>• truck capacity were increased</p>
                      </div>
                    )}

                    {/* Backend explanation */}
                    {rec.explanation && (
                      <p className="text-slate-500 leading-snug border-t border-slate-700/50 pt-2">{rec.explanation}</p>
                    )}
                  </div>
                )}
	              </div>

                {DEV_TRACE_ENABLED && (
                  <div className="border border-slate-700 rounded-lg overflow-hidden">
                    <button
                      onClick={() => setShowDeveloperTrace(!showDeveloperTrace)}
                      className="w-full flex items-center justify-between px-3 py-2 bg-slate-800 hover:bg-slate-700/60 text-left"
                    >
                      <span className="text-xs font-medium text-slate-300">Developer Trace</span>
                      <svg
                        className={`w-3 h-3 text-slate-400 transition-transform flex-shrink-0 ${showDeveloperTrace ? 'rotate-90' : ''}`}
                        fill="none" stroke="currentColor" viewBox="0 0 24 24"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                    </button>
                    {showDeveloperTrace && (
                      <div className="p-2.5 bg-slate-900/50 space-y-2">
                        <div className="flex justify-end">
                          <button
                            onClick={() => navigator.clipboard.writeText(developerTraceText)}
                            className="px-2 py-1 text-[11px] rounded bg-slate-700 hover:bg-slate-600 text-slate-200"
                          >
                            Copy trace
                          </button>
                        </div>
                        <pre className="max-h-72 overflow-auto whitespace-pre text-[11px] leading-snug text-slate-300 bg-slate-950/60 border border-slate-700 rounded p-2">
                          {developerTraceText}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
	            </div>
	          </div>

          {/* Right: route detail + container moves */}
          <div className="flex-1 overflow-y-auto p-2.5 min-w-0 space-y-3">
            {selectedRoute && (routeWarnings.get(selectedRoute.id) ?? []).length > 0 && (
              <div className="p-2.5 bg-amber-900/25 border border-amber-600/50 rounded-lg space-y-1.5">
                <p className="text-xs font-semibold text-amber-400">Route observations</p>
                {(routeWarnings.get(selectedRoute.id) ?? []).map((w, i) => {
                  const translated = translateMessage(undefined, w.message);
                  const detail = getTranslationDetail(undefined, w.message);
                  return (
                    <div key={i} className="pl-4">
                      <p className="text-xs text-amber-300/80">{translated}</p>
                      {detail && <p className="text-[10px] text-amber-300/50 mt-0.5">{detail}</p>}
                    </div>
                  );
                })}
              </div>
            )}
            {rec.container_moves && rec.container_moves.length > 0 && (
              <ContainerMovesSection moves={rec.container_moves} />
            )}
            {selectedRoute ? (
              <RouteDetail route={selectedRoute} />
            ) : (
              <div className="flex items-center justify-center h-32 text-sm text-slate-500">
                Select a route to view details
              </div>
            )}
          </div>
        </div>

        {/* ── Action buttons ── */}
        <div className="flex-shrink-0 p-3 border-t border-slate-700">
          {hasRecommendation && !isApplying && (
            <div className="flex gap-2">
              <button
                onClick={() => setShowApproveModal(true)}
                className="flex-1 py-1.5 bg-green-600 hover:bg-green-700 rounded-lg font-medium transition-colors text-sm"
              >
                Approve Plan
              </button>
              <button
                onClick={() => setShowChatbot(true)}
                title="Open DeepSeek plan assistant"
                className="px-3 py-1.5 bg-blue-700 hover:bg-blue-600 rounded-lg font-medium transition-colors text-sm flex items-center gap-1.5"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
                DeepSeek
              </button>
              <button
                disabled={exportState === 'loading'}
                onClick={async () => {
                  if (!recommendation) return;
                  setExportState('loading');
                  try {
                    await exportRecommendationExcel(recommendation.id);
                    setExportState('done');
                    setTimeout(() => setExportState('idle'), 3000);
                  } catch {
                    setExportState('error');
                    setTimeout(() => setExportState('idle'), 3000);
                  }
                }}
                title="Save full plan detail to analytics database"
                className={`px-3 py-1.5 rounded-lg font-medium transition-colors text-sm flex items-center gap-1.5 ${
                  exportState === 'done'  ? 'bg-green-600 text-white' :
                  exportState === 'error' ? 'bg-red-700 text-white' :
                  exportState === 'loading' ? 'bg-emerald-800 text-slate-300 cursor-wait' :
                  'bg-emerald-700 hover:bg-emerald-600 text-white'
                }`}
              >
                {exportState === 'loading' ? (
                  <>
                    <svg className="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                    </svg>
                    Saving…
                  </>
                ) : exportState === 'done' ? (
                  <>
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                    Saved to DB ✓
                  </>
                ) : exportState === 'error' ? (
                  'Save failed'
                ) : (
                  <>
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M12 5l7 7-7 7" />
                    </svg>
                    Export
                  </>
                )}
              </button>
              <button
                onClick={rejectRecommendation}
                className="flex-1 py-1.5 bg-slate-700 hover:bg-slate-600 rounded-lg font-medium transition-colors text-sm"
              >
                Reject
              </button>
            </div>
          )}

          {isApplying && (
            <div className="flex items-center justify-center py-2 text-slate-400 text-sm">
              <svg className="animate-spin h-4 w-4 mr-2" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Applying plan...
            </div>
          )}

          {isApproved && (
            <div className="p-2 bg-green-900/30 border border-green-600 rounded-lg">
              <div className="flex items-center gap-2 text-green-400 text-sm">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span className="font-medium">Plan Applied</span>
              </div>
              <p className="text-xs text-green-200 mt-1">Site data updated</p>
              <button
                onClick={clearRecommendation}
                className="mt-2 text-xs text-slate-400 hover:text-white"
              >
                Generate new recommendation
              </button>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <>
    <div className="h-full flex flex-col bg-slate-900 text-white overflow-hidden">
      {/* Header */}
      <div className="flex-shrink-0 p-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold">Operations</h2>
      </div>

      {/* Show pre-recommendation view OR recommendation results */}
      {(hasRecommendation || isApproved) && recommendation
        ? renderRecommendation()
        : renderPreRecommendation()
      }
    </div>

    {/* AI Chatbot drawer – rendered via portal */}
    {showChatbot && recommendation && (
      <ChatbotDrawer
        recommendation={recommendation}
        sites={sites}
        onClose={() => setShowChatbot(false)}
      />
    )}

    {/* Diagnostics drawer – rendered via portal */}
    {showDiagnostics && recommendation && (
      <DiagnosticsDrawer
        recommendation={recommendation}
        sites={sites}
        onClose={() => setShowDiagnostics(false)}
        setHoveredSiteId={setHoveredSiteId}
      />
    )}

    {/* Energy Moved breakdown drawer – rendered via portal */}
    {showEnergyDrawer && recommendation?.energy_moved_debug && createPortal(
      <div
        className="fixed inset-0"
        style={{ zIndex: 9998 }}
      >
        <div
          className="absolute inset-0 bg-black/40"
          onClick={() => setShowEnergyDrawer(false)}
        />
        <div className="absolute top-0 left-0 h-full w-[440px] max-w-[92vw] bg-slate-800 border-r border-slate-600 shadow-2xl overflow-y-auto">
          <div className="p-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-white">Energy Moved Breakdown</h3>
              <button
                onClick={() => setShowEnergyDrawer(false)}
                className="text-slate-400 hover:text-white text-xl leading-none px-2"
              >
                &times;
              </button>
            </div>

            {/* Definition */}
            <div className="mb-4 p-3 bg-slate-700/50 rounded text-sm text-slate-300">
              <p className="font-medium text-slate-200 mb-1">What this number means</p>
              <p>{recommendation.energy_moved_debug.definition}</p>
            </div>

            {/* Totals */}
            <div className="mb-4 grid grid-cols-2 gap-2">
              <div className="p-3 bg-slate-700 rounded">
                <p className="text-xs text-slate-400">Net Delivered</p>
                <p className="text-lg font-bold text-white">
                  {recommendation.energy_moved_debug.totals.net_delivered_mwh?.toFixed(3) ?? 'N/A'} MWh
                </p>
              </div>
              <div className="p-3 bg-slate-700 rounded">
                <p className="text-xs text-slate-400">Gross Dropped</p>
                <p className="text-lg font-bold text-slate-300">
                  {recommendation.energy_moved_debug.totals.gross_dropped_mwh?.toFixed(3) ?? 'N/A'} MWh
                </p>
              </div>
            </div>

            {/* Per-route breakdown */}
            <div className="mb-4">
              <p className="text-sm font-medium text-slate-200 mb-2">Per-Route Breakdown</p>
              {recommendation.energy_moved_debug.per_route.map((route) => (
                <div key={route.route_id} className="mb-3 border border-slate-600 rounded overflow-hidden">
                  <div className="p-2 bg-slate-700 flex justify-between items-center">
                    <span className="text-sm font-medium text-slate-200">
                      {route.truck_id}
                    </span>
                    <span className="text-sm text-blue-400 font-mono">
                      {route.moved_mwh.toFixed(3)} MWh
                    </span>
                  </div>
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-slate-400 border-b border-slate-700">
                        <th className="text-left p-1.5 pl-2">Stop</th>
                        <th className="text-right p-1.5">Drop/Pick</th>
                        <th className="text-right p-1.5 pr-2">MWh</th>
                      </tr>
                    </thead>
                    <tbody>
                      {route.legs.map((leg, i) => (
                        <tr
                          key={i}
                          className={`border-b border-slate-700/50 ${leg.counted_toward_total ? 'text-white' : 'text-slate-500'}`}
                        >
                          <td className="p-1.5 pl-2">
                            <span>{leg.to_name || leg.to}</span>
                            {leg.is_consumer && (
                              <span className="ml-1 text-[10px] text-green-400 font-medium">consumer</span>
                            )}
                            {!leg.is_consumer && leg.site_type && (
                              <span className="ml-1 text-[10px] text-slate-500">{leg.site_type}</span>
                            )}
                          </td>
                          <td className="text-right p-1.5 font-mono">
                            {leg.bays_dropped > 0 && <span className="text-green-400">+{leg.bays_dropped}</span>}
                            {leg.bays_dropped > 0 && leg.bays_picked > 0 && '/'}
                            {leg.bays_picked > 0 && <span className="text-orange-400">-{leg.bays_picked}</span>}
                          </td>
                          <td className="text-right p-1.5 pr-2 font-mono">
                            {leg.dropped_mwh > 0 ? leg.dropped_mwh.toFixed(3) : '-'}
                            {!leg.counted_toward_total && leg.dropped_mwh > 0 && (
                              <span className="text-[9px] text-slate-500 ml-0.5">*</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
              <p className="text-[10px] text-slate-500 mt-1">* not counted (non-consumer site)</p>
            </div>

            {/* Assumptions */}
            {recommendation.energy_moved_debug.assumptions.length > 0 && (
              <div className="mb-2">
                <p className="text-sm font-medium text-slate-200 mb-1">Assumptions</p>
                <ul className="text-xs text-slate-400 list-disc pl-4 space-y-0.5">
                  {recommendation.energy_moved_debug.assumptions.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      </div>,
      document.body,
    )}

    {/* Cost breakdown drawer – rendered via portal */}
    {showCostDrawer && recommendation && createPortal(
      <div className="fixed inset-0" style={{ zIndex: 9998 }}>
        <div className="absolute inset-0 bg-black/40" onClick={() => setShowCostDrawer(false)} />
        <div className="absolute top-0 left-0 h-full w-[440px] max-w-[92vw] bg-slate-800 border-r border-slate-600 shadow-2xl overflow-y-auto">
          <div className="p-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-white">Cost Breakdown</h3>
              <button
                onClick={() => setShowCostDrawer(false)}
                className="text-slate-400 hover:text-white text-xl leading-none px-2"
              >
                &times;
              </button>
            </div>

            <div className="mb-4 grid grid-cols-2 gap-2">
              <div className="p-3 bg-slate-700 rounded">
                <p className="text-xs text-slate-400">Total Cost</p>
                <p className="text-lg font-bold text-emerald-300">{recommendation.total_cost_eur.toFixed(2)} EUR</p>
              </div>
              <div className="p-3 bg-slate-700 rounded">
                <p className="text-xs text-slate-400">Total Distance</p>
                <p className="text-lg font-bold text-slate-100">{recommendation.total_distance_km.toFixed(1)} km</p>
              </div>
            </div>

            <div className="border border-slate-600 rounded overflow-hidden mb-4">
              <table className="w-full text-sm">
                <tbody>
                  <tr className="border-b border-slate-700/60">
                    <td className="px-3 py-2 text-slate-300">Transport (distance-based)</td>
                    <td className="px-3 py-2 text-right font-mono text-slate-100">{recommendation.transport_cost_eur.toFixed(2)} EUR</td>
                  </tr>
                  <tr className="border-b border-slate-700/60">
                    <td className="px-3 py-2 text-slate-300">Handling / service fees</td>
                    <td className="px-3 py-2 text-right font-mono text-slate-100">{recommendation.handling_cost_eur.toFixed(2)} EUR</td>
                  </tr>
                  <tr className="border-b border-slate-700/60">
                    <td className="px-3 py-2 text-slate-300">Other adjustments</td>
                    <td className="px-3 py-2 text-right font-mono text-slate-300">
                      {(recommendation.total_cost_eur - recommendation.transport_cost_eur - recommendation.handling_cost_eur).toFixed(2)} EUR
                    </td>
                  </tr>
                  <tr>
                    <td className="px-3 py-2 text-slate-100 font-semibold">Total</td>
                    <td className="px-3 py-2 text-right font-mono text-emerald-300 font-semibold">{recommendation.total_cost_eur.toFixed(2)} EUR</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="p-3 bg-slate-700/40 rounded text-xs text-slate-400">
              Cost per km in current run: {(recommendation.total_distance_km > 0
                ? (recommendation.transport_cost_eur / recommendation.total_distance_km)
                : 0).toFixed(2)} EUR/km
            </div>
          </div>
        </div>
      </div>,
      document.body,
    )}

    {/* Approve confirmation modal – rendered via portal at document.body */}
    {showApproveModal && createPortal(
      <div
        className="fixed inset-0 flex items-center justify-center"
        style={{ zIndex: 9999 }}
      >
        {/* Backdrop */}
        <div
          className="absolute inset-0 bg-black/60"
          onClick={closeModal}
        />
        {/* Dialog */}
        <div className="relative bg-slate-800 border border-slate-600 rounded-xl p-6 shadow-2xl w-96">
          <h3 className="text-lg font-semibold text-white mb-4 text-center">
            Plan Next Steps
          </h3>
          <div className="mb-4">
            <label className="block text-sm text-slate-300 mb-2 text-center">
              Days to plan
            </label>
            <div className="grid grid-cols-4 gap-2">
              {[1, 2, 3, 4].map((days) => (
                <button
                  key={days}
                  onClick={() => setNextStepsDays(days)}
                  className={`py-2 rounded-lg border text-sm font-medium transition-colors ${
                    nextStepsDays === days
                      ? 'bg-blue-600 border-blue-500 text-white'
                      : 'bg-slate-700 border-slate-600 text-slate-200 hover:bg-slate-600'
                  }`}
                >
                  {days}d
                </button>
              ))}
            </div>
            <p className="text-xs text-slate-400 mt-2 text-center">
              {nextStepsDays * 24}h planning horizon
            </p>
          </div>
          <div className="flex gap-3 mb-3">
            <button
              onClick={() => handleApprove(true)}
              className="flex-1 py-2.5 bg-green-600 hover:bg-green-700 rounded-lg font-medium transition-colors text-white"
            >
              Apply + Plan
            </button>
            <button
              onClick={() => handleApprove(false)}
              className="flex-1 py-2.5 bg-slate-700 hover:bg-slate-600 rounded-lg font-medium transition-colors text-white"
            >
              Apply Only
            </button>
          </div>
          <button
            onClick={closeModal}
            className="w-full text-sm text-slate-400 hover:text-slate-200 py-1"
          >
            Cancel
          </button>
        </div>
      </div>,
      document.body,
    )}
</>
  );
};

export default Operations;
