import React, { useState } from 'react';
import { useAppStore } from '../../stores/appStore';
import { getSiteStatus, STATUS_BG_CLASSES, STATUS_TEXT_CLASSES } from '../../utils/siteStatus';

type FilterOption = 'all' | 'critical' | 'warning' | 'producers' | 'consumers';

export const SitePriority: React.FC = () => {
  const { sites, selectedSiteId, selectSite, sitesLoading } = useAppStore();
  const [filter, setFilter] = useState<FilterOption>('all');

  // Filter sites
  const filteredSites = sites.filter((site) => {
    switch (filter) {
      case 'critical':
        return site.risk_level === 'critical';
      case 'warning':
        return site.risk_level === 'warning';
      case 'producers':
        return site.site_type === 'production';
      case 'consumers':
        return site.site_type === 'traffic' || site.site_type === 'industry';
      default:
        return true;
    }
  });

  // Sort by risk score (highest first)
  const sortedSites = [...filteredSites].sort((a, b) => b.risk_score - a.risk_score);

  const getRiskDotColor = (site: typeof sites[0]) => STATUS_BG_CLASSES[getSiteStatus(site)];
  const getBarColor = (site: typeof sites[0]) => STATUS_BG_CLASSES[getSiteStatus(site)];

  // Get bar fill percentage based on site type
  // For producers: higher pressure = more full = closer to critical (flaring)
  // For consumers: higher pressure = more inventory = healthier
  const getBarPercentage = (site: typeof sites[0]) => {
    const isConsumer = site.site_type === 'traffic' || site.site_type === 'industry';

    if (isConsumer) {
      // For consumers, show how much inventory they have (higher = better)
      // Use utilization as a health indicator
      return Math.min(100, site.utilization_percentage);
    } else {
      // For producers, show capacity usage (higher = closer to flaring)
      return Math.min(100, site.utilization_percentage);
    }
  };

  const formatHours = (hours: number) => {
    if (hours >= 99999) return 'N/A';
    if (hours < 1) return `${Math.round(hours * 60)}m`;
    if (hours < 24) return `${hours.toFixed(1)}h`;
    return `${Math.round(hours / 24)}d`;
  };

  const filters: { key: FilterOption; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'critical', label: 'Critical' },
    { key: 'warning', label: 'Warning' },
    { key: 'producers', label: 'Producers' },
    { key: 'consumers', label: 'Consumers' },
  ];

  const getFilterButtonClass = (key: FilterOption, active: boolean) => {
    if (!active) return 'bg-slate-700 text-slate-300 hover:bg-slate-600';
    switch (key) {
      case 'critical':
        return 'bg-red-600 text-white ring-1 ring-red-300';
      case 'warning':
        return 'bg-amber-600 text-white ring-1 ring-amber-300';
      case 'producers':
        return 'bg-emerald-700 text-white ring-1 ring-emerald-300';
      case 'consumers':
        return 'bg-orange-600 text-white ring-1 ring-orange-300';
      default:
        return 'bg-blue-600 text-white ring-1 ring-blue-300';
    }
  };

  return (
    <div className="h-full flex flex-col bg-slate-900 text-white">
      {/* Header */}
      <div className="p-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold mb-3">Site Priority</h2>

        {/* Filter buttons */}
        <div className="flex flex-wrap gap-2">
          {filters.map((opt) => (
            <button
              key={opt.key}
              onClick={() => setFilter(opt.key)}
              className={`px-3 py-1 rounded text-sm font-medium transition-colors ${getFilterButtonClass(opt.key, filter === opt.key)}`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Site list */}
      <div className="flex-1 overflow-y-auto">
        {sitesLoading ? (
          <div className="flex items-center justify-center h-32">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
          </div>
        ) : sortedSites.length === 0 ? (
          <div className="p-4 text-center text-slate-400">
            No sites match the filter
          </div>
        ) : (
          <ul className="divide-y divide-slate-700">
            {sortedSites.map((site) => (
              <li
                key={site.id}
                onClick={() => selectSite(site.id)}
                className={`px-3 py-3.5 cursor-pointer transition-colors border-l-2 ${
                  selectedSiteId === site.id
                    ? `${getBarColor(site)} bg-slate-700/70`
                    : 'border-transparent hover:bg-slate-800/85'
                }`}
              >
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-semibold">{site.name}</span>
                  <span
                    className={`w-3 h-3 rounded-full ${getRiskDotColor(site)}`}
                  ></span>
                </div>

                <div className="flex items-center justify-between text-xs text-slate-400">
                  <span>
                    {site.total_mwh.toFixed(1)} MWh ({site.bays.length} bays)
                  </span>
                  <span className={`font-medium ${STATUS_TEXT_CLASSES[getSiteStatus(site)]}`}>
                    {formatHours(site.hours_to_critical)}
                  </span>
                </div>

                {/* Progress bar - color matches risk level, no stripes */}
                <div className="mt-2 h-1.5 bg-slate-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full transition-all ${getBarColor(site)}`}
                    style={{ width: `${getBarPercentage(site)}%` }}
                  ></div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Footer stats */}
      <div className="p-3 border-t border-slate-700 text-xs text-slate-400">
        Showing {sortedSites.length} of {sites.length} sites
      </div>

    </div>
  );
};

export default SitePriority;
