import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import type { Site } from '../../types';

interface NetworkStatusPanelProps {
  sites: Site[];
  onClose: () => void;
}

const RISK_COLORS: Record<string, string> = {
  critical: 'text-red-400 bg-red-900/30 border-red-700',
  warning:  'text-amber-400 bg-amber-900/30 border-amber-700',
  normal:   'text-slate-400 bg-slate-800/40 border-slate-700',
  safe:     'text-emerald-400 bg-emerald-900/20 border-emerald-800',
};

const RISK_DOT: Record<string, string> = {
  critical: 'bg-red-500',
  warning:  'bg-amber-400',
  normal:   'bg-slate-400',
  safe:     'bg-emerald-400',
};

function formatRuntime(hours: number | null | undefined): string {
  if (hours == null || hours <= 0) return '—';
  if (hours >= 99999) return '∞';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 24) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

function bayStatus(pressure: number): { label: string; color: string } {
  if (pressure >= 200) return { label: 'Full',    color: 'text-emerald-400' };
  if (pressure > 20)   return { label: 'Partial', color: 'text-amber-400'  };
  return                      { label: 'Empty',   color: 'text-red-400'    };
}

function buildNetworkStatusText(sites: Site[]): string {
  const sorted = [...sites].sort((a, b) => {
    const order: Record<string, number> = { critical: 0, warning: 1, normal: 2, safe: 3 };
    return (order[a.risk_level] ?? 2) - (order[b.risk_level] ?? 2);
  });
  const lines: string[] = [`Network Status — ${sites.length} sites\n`];
  for (const site of sorted) {
    const htc = formatRuntime(site.hours_to_critical);
    const rate = site.site_type === 'production'
      ? (site.production?.effective_kg_per_h ?? site.production?.kg_per_h ?? null)
      : (site.consumption_rate_kg_hour ?? null);
    const rateStr = rate != null ? `  rate=${rate.toFixed(0)}kg/h` : '';
    lines.push(`${site.name} [${site.site_type}] risk=${site.risk_level} runtime=${htc}${rateStr}`);
    for (const bay of (site.bays ?? [])) {
      const label = bay.serial_number ?? bay.bay_id ?? '';
      const kg = (bay.kg_usable ?? (bay as any).kg ?? 0);
      const mwh = (bay.mwh_usable ?? (bay as any).mwh ?? 0);
      lines.push(`  ${label}  ${bay.pressure_bar?.toFixed(0) ?? '—'}bar  ${kg.toFixed(0)}kg  ${mwh.toFixed(2)}MWh`);
    }
    lines.push(`  Total: ${(site.total_kg_usable ?? 0).toFixed(0)}kg / ${(site.total_mwh_usable ?? 0).toFixed(2)}MWh | ${(site.utilization_usable_pct ?? site.utilization_percentage ?? 0).toFixed(0)}%`);
    lines.push('');
  }
  return lines.join('\n');
}

export const NetworkStatusPanel: React.FC<NetworkStatusPanelProps> = ({ sites, onClose }) => {
  const [search, setSearch] = useState('');

  const sorted = [...sites]
    .sort((a, b) => {
      const order = { critical: 0, warning: 1, normal: 2, safe: 3 };
      return (order[a.risk_level] ?? 2) - (order[b.risk_level] ?? 2);
    })
    .filter((s) => !search || s.name.toLowerCase().includes(search.toLowerCase()));

  return createPortal(
    <div className="fixed inset-0" style={{ zIndex: 9997 }}>
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="absolute top-0 right-0 h-full w-[480px] max-w-[96vw] bg-slate-900 border-l border-slate-700 shadow-2xl flex flex-col">

        {/* Header */}
        <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-slate-700 bg-slate-800">
          <div className="flex items-center gap-2">
            <svg className="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16" />
            </svg>
            <h3 className="text-sm font-semibold text-white">Network Status</h3>
            <span className="text-[10px] text-slate-400 bg-slate-700 px-1.5 py-0.5 rounded">
              {sites.length} sites · {sites.reduce((n, s) => n + (s.bays?.length ?? 0), 0)} bays
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => navigator.clipboard.writeText(buildNetworkStatusText(sites))}
              className="px-2 py-1 text-[11px] rounded bg-slate-700 hover:bg-slate-600 text-slate-200"
              title="Copy network status to clipboard"
            >
              Copy Network Status
            </button>
            <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none px-1">&times;</button>
          </div>
        </div>

        {/* Search */}
        <div className="flex-shrink-0 px-3 py-2 border-b border-slate-700/60">
          <input
            type="text"
            placeholder="Filter sites…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full px-3 py-1.5 bg-slate-800 border border-slate-700 rounded text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* Site list */}
        <div className="flex-1 overflow-y-auto">
          {sorted.map((site) => {
            const htc = formatRuntime(site.hours_to_critical);
            const riskStyle = RISK_COLORS[site.risk_level] ?? RISK_COLORS.normal;
            const dot = RISK_DOT[site.risk_level] ?? RISK_DOT.normal;
            const isConsumer = site.site_type !== 'production';
            const isProducer = site.site_type === 'production';
            const consRate = site.consumption_rate_kg_hour ?? 0;
            const prodRate = site.production?.effective_kg_per_h ?? site.production?.kg_per_h ?? null;

            return (
              <div key={site.id} className={`border-l-2 ${riskStyle} mx-3 my-2 rounded-r-lg overflow-hidden`}>
                {/* Site header */}
                <div className="flex items-center justify-between px-3 py-2 bg-slate-800/60">
                  <div className="flex items-center gap-2">
                    <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${dot}`} />
                    <span className="text-sm font-semibold text-white">{site.name}</span>
                    <span className="text-[10px] text-slate-500 uppercase tracking-wide">
                      {site.site_type}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 text-right">
                    <div>
                      <div className="text-[10px] text-slate-500">Runtime</div>
                      <div className={`text-xs font-bold ${
                        site.risk_level === 'critical' ? 'text-red-400' :
                        site.risk_level === 'warning'  ? 'text-amber-400' : 'text-slate-300'
                      }`}>{htc}</div>
                    </div>
                    {isConsumer && consRate > 0 && (
                      <div>
                        <div className="text-[10px] text-slate-500">Consumption</div>
                        <div className="text-xs text-slate-300">{consRate.toFixed(0)} kg/h</div>
                      </div>
                    )}
                    {isProducer && prodRate != null && (
                      <div>
                        <div className="text-[10px] text-slate-500">Production</div>
                        <div className="text-xs text-slate-300">{prodRate.toFixed(0)} kg/h</div>
                      </div>
                    )}
                  </div>
                </div>

                {/* Bay rows */}
                <div className="divide-y divide-slate-700/40">
                  {(site.bays ?? []).map((bay, idx) => {
                    const label = bay.serial_number ?? bay.bay_id ?? `Bay ${idx + 1}`;
                    const status = bayStatus(bay.pressure_bar);
                    const kgUsable = bay.kg_usable ?? bay.kg ?? 0;
                    const mwhUsable = bay.mwh_usable ?? bay.mwh ?? 0;

                    // Per-bay runtime estimate (usable kg / consumption rate)
                    let bayRuntime: string | null = null;
                    if (isConsumer && consRate > 0 && kgUsable >= 0) {
                      bayRuntime = formatRuntime(kgUsable / consRate);
                    }

                    return (
                      <div key={bay.bay_id} className="flex items-center justify-between px-3 py-1.5 bg-slate-900/40 text-xs">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="text-slate-500 font-mono text-[10px] w-4 text-center">{idx + 1}</span>
                          <span className="text-slate-300 font-mono truncate">{label}</span>
                          <span className={`text-[10px] ${status.color}`}>{status.label}</span>
                        </div>
                        <div className="flex items-center gap-3 flex-shrink-0 text-right">
                          <div>
                            <span className="text-slate-400">{bay.pressure_bar?.toFixed(0) ?? '—'}</span>
                            <span className="text-slate-600 ml-0.5">bar</span>
                          </div>
                          <div>
                            <span className="text-white font-medium">{kgUsable.toFixed(0)}</span>
                            <span className="text-slate-600 ml-0.5">kg</span>
                          </div>
                          <div>
                            <span className="text-slate-400">{mwhUsable.toFixed(2)}</span>
                            <span className="text-slate-600 ml-0.5">MWh</span>
                          </div>
                          {bayRuntime != null && (
                            <div className="w-14 text-right">
                              <span className="text-[10px] text-slate-500">~{bayRuntime}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Site totals footer */}
                <div className="flex items-center justify-between px-3 py-1.5 bg-slate-800/30 border-t border-slate-700/30 text-[10px] text-slate-500">
                  <span>{site.bays?.length ?? 0} bays</span>
                  <span>
                    Total usable: <span className="text-slate-300">{(site.total_kg_usable ?? 0).toFixed(0)} kg</span>
                    {' / '}
                    <span className="text-slate-300">{(site.total_mwh_usable ?? 0).toFixed(2)} MWh</span>
                    {' · '}
                    <span className={site.utilization_usable_pct != null && site.utilization_usable_pct < 20 ? 'text-red-400' : 'text-slate-300'}>
                      {(site.utilization_usable_pct ?? site.utilization_percentage ?? 0).toFixed(0)}%
                    </span>
                  </span>
                </div>
              </div>
            );
          })}

          {sorted.length === 0 && (
            <div className="flex items-center justify-center h-32 text-slate-500 text-sm">
              No sites match "{search}"
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
};
