import React, { useState } from 'react';
import type { Route } from '../../types';
import { getTruckRouteStyle } from '../../utils/truckColors';
import type { RouteWarning } from '../../utils/routeValidation';
import { getRouteDisplayTimeHours, getRouteStopDisplayTimes } from '../../utils/routeMetrics';

interface RouteAccordionItemProps {
  route: Route;
  index: number;
}

const RouteAccordionItem: React.FC<RouteAccordionItemProps> = ({ route, index: _index }) => {
  void _index; // Used for key in parent, not directly here
  const [expanded, setExpanded] = useState(false);
  const displayTimeHours = getRouteDisplayTimeHours(route);
  const stopDisplayTimes = getRouteStopDisplayTimes(route);

  // Format time nicely
  const formatTime = (hours: number): string => {
    if (hours >= 1) {
      return `${hours.toFixed(1)}h`;
    }
    return `${Math.round(hours * 60)}min`;
  };

  // Check distance consistency
  const legsSum = route.legs?.reduce((sum, leg) => sum + leg.distance_km, 0) || 0;
  const distanceMismatch = Math.abs(route.total_distance_km - legsSum) > 0.01;

  // Check if any stop has swap data
  const hasAnySwaps = route.stops.some(
    (s) =>
      s.swap_operation &&
      (s.swap_operation.containers_dropped.length > 0 || s.swap_operation.containers_picked.length > 0)
  );

  const truckStyle = getTruckRouteStyle(route.truck_id);

  return (
    <div
      className="rounded-lg overflow-hidden"
      style={{ backgroundColor: '#334155', borderLeft: `4px solid ${truckStyle.color}` }}
    >
      {/* Header - always visible */}
      <div
        className="flex items-center justify-between p-3 cursor-pointer hover:bg-slate-600/50"
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
          <div className="flex items-center gap-2">
            <span
              className="inline-block w-3 h-3 rounded-sm flex-shrink-0"
              style={{ backgroundColor: truckStyle.color }}
            />
            <span className="font-medium">{route.truck_id}</span>
            <span className="text-slate-400">-</span>
            <span className="text-slate-300">Day {route.day_index}</span>
          </div>
        </div>
        <div className="text-right">
          <span className="font-bold">{route.total_distance_km.toFixed(0)} km</span>
          <span className="text-slate-400 text-sm ml-2">
            ({formatTime(displayTimeHours)})
          </span>
        </div>
      </div>

      {/* Expanded content - legs breakdown */}
      {expanded && (
        <div className="border-t border-slate-600 p-3 space-y-3">
          {/* Route summary */}
          <div className="text-xs text-slate-400">
            {route.stops.map((s, i) =>
              i === 0 && route.start_label ? route.start_label : s.site_name
            ).join(' → ')}
          </div>

          {/* Legs table */}
          {route.legs && route.legs.length > 0 ? (
            <div className="space-y-1">
              <div className="text-xs font-medium text-slate-400 mb-2">Legs Breakdown</div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-slate-500">
                    <th className="text-left py-1">From</th>
                    <th className="text-left py-1">To</th>
                    <th className="text-right py-1">Distance</th>
                    <th className="text-right py-1">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {route.legs.map((leg, idx) => (
                    <tr key={idx} className="border-t border-slate-600/50">
                      <td className="py-1.5 text-slate-300 truncate max-w-[100px]">
                        {leg.from_site_name || leg.from_site_id}
                      </td>
                      <td className="py-1.5 text-slate-300 truncate max-w-[100px]">
                        {leg.to_site_name || leg.to_site_id}
                      </td>
                      <td className="py-1.5 text-right font-mono">
                        {leg.distance_km.toFixed(1)} km
                      </td>
                      <td className="py-1.5 text-right text-slate-400">
                        {leg.drive_time_hours ? formatTime(leg.drive_time_hours) : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-slate-500 font-medium">
                    <td colSpan={2} className="py-2">Total</td>
                    <td className="py-2 text-right font-mono">
                      {route.total_distance_km.toFixed(1)} km
                    </td>
                    <td className="py-2 text-right text-slate-400">
                      {formatTime(displayTimeHours)}
                    </td>
                  </tr>
                </tfoot>
              </table>

              {/* Audit line */}
              <div className="flex justify-between text-xs pt-2 border-t border-slate-600/50">
                <span className="text-slate-500">Sum of legs</span>
                <span className={distanceMismatch ? 'text-red-400' : 'text-slate-400'}>
                  {legsSum.toFixed(1)} km
                  {distanceMismatch && ' (mismatch!)'}
                </span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-500">No leg data available</p>
          )}

          {/* Operations Timeline (swap-by-swap) */}
          <div className="mt-3 pt-3 border-t border-slate-600">
            <div className="text-xs font-medium text-slate-400 mb-2">Operations Timeline</div>
            {hasAnySwaps ? (
              <div className="space-y-0">
                {route.stops.map((stop, idx) => {
                  const op = stop.swap_operation;
                  const dropped = op?.containers_dropped?.length ?? 0;
                  const picked = op?.containers_picked?.length ?? 0;
                  const hasOps = dropped > 0 || picked > 0;
                  const serviceMin = Math.round(stop.service_time_hours * 60);
                  const isStart = stop.sequence === 0;
                  const prev = idx > 0 ? route.stops[idx - 1] : null;
                  const fullBefore = prev ? (prev.load_full_after ?? 0) : (stop.load_full_after ?? 0);
                  const emptyBefore = prev ? (prev.load_empty_after ?? 0) : (stop.load_empty_after ?? 0);
                  const fullAfter = stop.load_full_after ?? 0;
                  const emptyAfter = stop.load_empty_after ?? 0;

                  return (
                    <div key={idx} className="flex gap-2">
                      {/* Timeline line */}
                      <div className="flex flex-col items-center w-5 flex-shrink-0">
                        <div
                          className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${
                            isStart
                              ? 'bg-blue-600 text-white'
                              : hasOps
                              ? 'bg-amber-600 text-white'
                              : 'bg-slate-600 text-slate-400'
                          }`}
                        >
                          {stop.sequence}
                        </div>
                        {idx < route.stops.length - 1 && (
                          <div className="w-px flex-1 bg-slate-600 my-0.5" />
                        )}
                      </div>
                      {/* Stop content */}
                      <div className="flex-1 pb-2 min-w-0">
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="text-sm text-slate-200 truncate">
                            {isStart
                            ? (route.day_index > 1
                                ? (route.start_label || 'In transit from previous day')
                                : 'Start location')
                            : (stop.site_name || stop.site_id)}
                          </span>
                          {/* Hide 0 km | 0 min for the start depot — it carries no meaning */}
                          {!isStart && (
                            <span className="text-xs text-slate-500 whitespace-nowrap">
                              {stop.cumulative_distance_km.toFixed(0)} km | {formatTime(stopDisplayTimes[idx] ?? stop.arrival_time_hours)}
                            </span>
                          )}
                        </div>
                        {isStart && !hasOps ? (
                          <div className="text-xs text-slate-500 mt-0.5">
                            {route.start_label || stop.site_name || stop.site_id}
                            {fullAfter > 0 && (
                              <span className="ml-2 text-blue-400">
                                {route.day_index > 1
                                  ? `Carry-over from previous day: ${fullAfter} full`
                                  : `Initial load at start: ${fullAfter} full`}
                              </span>
                            )}
                            <div className="mt-0.5 text-slate-400">
                              Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                            </div>
                          </div>
                        ) : hasOps ? (
                          <div className="text-xs mt-0.5 space-x-2">
                            {dropped > 0 && (
                              <span className="text-green-400">Deliver: {dropped}</span>
                            )}
                            {picked > 0 && (
                              <span className="text-blue-400">Pickup: {picked}</span>
                            )}
                            <span className="text-slate-500">Service: {serviceMin}m</span>
                            <span className="text-slate-400">
                              Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                            </span>
                          </div>
                        ) : (
                          <div className="text-xs text-slate-500 mt-0.5">
                            Transit
                            <span className="ml-1">
                              Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-slate-500">No swap details available for this route.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

interface RouteAccordionProps {
  routes: Route[];
}

export const RouteAccordion: React.FC<RouteAccordionProps> = ({ routes }) => {
  if (!routes || routes.length === 0) {
    return <p className="text-sm text-slate-500">No routes generated</p>;
  }

  // Sort routes by truck and day
  const sortedRoutes = [...routes].sort((a, b) => {
    const truckCompare = a.truck_id.localeCompare(b.truck_id);
    if (truckCompare !== 0) return truckCompare;
    return a.day_index - b.day_index;
  });

  return (
    <div className="space-y-2">
      {sortedRoutes.map((route, idx) => (
        <RouteAccordionItem key={route.id} route={route} index={idx} />
      ))}
    </div>
  );
};

export default RouteAccordion;

// ─── Master-detail exports (used when panel is wide enough) ───

/** Compact clickable row for the route list */
export const RouteListItem: React.FC<{
  route: Route;
  selected: boolean;
  visible: boolean;
  warnings: RouteWarning[];
  onClick: () => void;
  onToggleVisibility: (e: React.MouseEvent) => void;
}> = ({ route, selected, visible, warnings, onClick, onToggleVisibility }) => {
  const truckStyle = getTruckRouteStyle(route.truck_id);
  const hasWarnings = warnings.length > 0;
  const stopsCount = route.stops.filter(
    (s) =>
      s.swap_operation &&
      (s.swap_operation.containers_dropped.length > 0 || s.swap_operation.containers_picked.length > 0)
  ).length;

  const tooltipText = warnings.map((w) => w.message).join('\n');

  return (
    <div
      className={`flex items-center gap-2 p-2 rounded cursor-pointer transition-colors border ${
        hasWarnings
          ? selected
            ? 'bg-amber-900/30 border-amber-500/70'
            : 'bg-amber-900/15 border-amber-700/40 hover:bg-amber-900/25'
          : selected
          ? 'border-opacity-60'
          : 'bg-slate-700/50 hover:bg-slate-600/50 border-transparent'
      }`}
      style={
        !hasWarnings && selected
          ? { backgroundColor: `${truckStyle.color}22`, borderColor: `${truckStyle.color}99` }
          : {}
      }
      onClick={onClick}
      title={hasWarnings ? tooltipText : undefined}
    >
      {/* Checkbox for visibility */}
      <input
        type="checkbox"
        checked={visible}
        onChange={() => {}}
        onClick={onToggleVisibility}
        className="flex-shrink-0 w-3.5 h-3.5 rounded cursor-pointer accent-blue-500"
        title={visible ? 'Hide on map' : 'Show on map'}
      />
      {/* Color dot */}
      <span
        className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
        style={{ backgroundColor: truckStyle.color, opacity: visible ? 1 : 0.4 }}
      />
      <span className={`text-sm font-medium flex-shrink-0 ${!visible ? 'text-slate-500' : ''}`}>
        {route.truck_id}
      </span>
      <span className={`text-xs flex-shrink-0 ${!visible ? 'text-slate-600' : 'text-slate-400'}`}>
        D{route.day_index}
      </span>
      <span className="flex-1" />
      {/* Warning badge */}
      {hasWarnings && (
        <span
          className="text-amber-400 text-sm flex-shrink-0"
          title={tooltipText}
        >
          ⚠
        </span>
      )}
      <span className={`text-xs ${!visible ? 'text-slate-600' : 'text-slate-400'}`}>{stopsCount} stops</span>
      <span className={`text-sm font-mono ${!visible ? 'text-slate-600' : ''}`}>
        {route.total_distance_km.toFixed(0)} km
      </span>
    </div>
  );
};

/** Full route detail panel (legs table + operations timeline) */
export const RouteDetail: React.FC<{ route: Route }> = ({ route }) => {
  const [showLegsBreakdown, setShowLegsBreakdown] = useState(false);
  const displayTimeHours = getRouteDisplayTimeHours(route);
  const stopDisplayTimes = getRouteStopDisplayTimes(route);

  const formatTime = (hours: number): string => {
    if (hours >= 1) return `${hours.toFixed(1)}h`;
    return `${Math.round(hours * 60)}min`;
  };

  const legsSum = route.legs?.reduce((sum, leg) => sum + leg.distance_km, 0) || 0;
  const distanceMismatch = Math.abs(route.total_distance_km - legsSum) > 0.01;

  const hasAnySwaps = route.stops.some(
    (s) =>
      s.swap_operation &&
      (s.swap_operation.containers_dropped.length > 0 || s.swap_operation.containers_picked.length > 0)
  );

  const truckStyle = getTruckRouteStyle(route.truck_id);

  return (
    <div className="space-y-3">
      {/* Route header */}
      <div className="flex items-center gap-2 pb-2 border-b border-slate-600">
        <span
          className="inline-block w-3 h-3 rounded-sm flex-shrink-0"
          style={{ backgroundColor: truckStyle.color }}
        />
        <span className="font-medium">{route.truck_id}</span>
        <span className="text-slate-400">-</span>
        <span className="text-slate-300">Day {route.day_index}</span>
        <span className="flex-1" />
        <span className="font-bold">{route.total_distance_km.toFixed(0)} km</span>
        <span className="text-slate-400 text-sm">({formatTime(displayTimeHours)})</span>
      </div>

      {/* Route summary */}
      <div className="text-xs text-slate-400">
        {route.stops
          .map((s, i) => (i === 0 && route.start_label ? route.start_label : s.site_name))
          .join(' \u2192 ')}
      </div>

      {/* Operations Timeline */}
      <div className="mt-3 pt-3 border-t border-slate-600">
        <div className="text-xs font-medium text-slate-400 mb-2">Operations Timeline</div>
        {hasAnySwaps ? (
          <div className="space-y-0">
            {route.stops.map((stop, idx) => {
              const op = stop.swap_operation;
              const dropped = op?.containers_dropped?.length ?? 0;
              const picked = op?.containers_picked?.length ?? 0;
              const hasOps = dropped > 0 || picked > 0;
              const serviceMin = Math.round(stop.service_time_hours * 60);
              const isStart = stop.sequence === 0;
              const prev = idx > 0 ? route.stops[idx - 1] : null;
              const fullBefore = prev ? (prev.load_full_after ?? 0) : (stop.load_full_after ?? 0);
              const emptyBefore = prev ? (prev.load_empty_after ?? 0) : (stop.load_empty_after ?? 0);
              const fullAfter = stop.load_full_after ?? 0;
              const emptyAfter = stop.load_empty_after ?? 0;

              return (
                <div key={idx} className="flex gap-2">
                  <div className="flex flex-col items-center w-5 flex-shrink-0">
                    <div
                      className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${
                        isStart
                          ? 'bg-blue-600 text-white'
                          : hasOps
                          ? 'bg-amber-600 text-white'
                          : 'bg-slate-600 text-slate-400'
                      }`}
                    >
                      {stop.sequence}
                    </div>
                    {idx < route.stops.length - 1 && (
                      <div className="w-px flex-1 bg-slate-600 my-0.5" />
                    )}
                  </div>
                  <div className="flex-1 pb-2 min-w-0">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-sm text-slate-200 truncate">
                        {isStart
                          ? (route.day_index > 1
                              ? (route.start_label || 'In transit from previous day')
                              : 'Start location')
                          : (stop.site_name || stop.site_id)}
                      </span>
                      {/* Hide 0 km | 0 min for the start depot — it carries no meaning */}
                      {!isStart && (
                        <span className="text-xs text-slate-500 whitespace-nowrap">
                          {stop.cumulative_distance_km.toFixed(0)} km | {formatTime(stopDisplayTimes[idx] ?? stop.arrival_time_hours)}
                        </span>
                      )}
                    </div>
                    {isStart && !hasOps ? (
                      <div className="text-xs text-slate-500 mt-0.5">
                        {route.start_label || stop.site_name || stop.site_id}
                        {fullAfter > 0 && (
                          <span className="ml-2 text-blue-400">
                            {route.day_index > 1
                              ? `Carry-over from previous day: ${fullAfter} full`
                              : `Initial load at start: ${fullAfter} full`}
                          </span>
                        )}
                        <div className="mt-0.5 text-slate-400">
                          Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                        </div>
                      </div>
                    ) : hasOps ? (
                      <div className="text-xs mt-0.5 space-x-2">
                        {dropped > 0 && <span className="text-green-400">Deliver: {dropped}</span>}
                        {picked > 0 && <span className="text-blue-400">Pickup: {picked}</span>}
                        <span className="text-slate-500">Service: {serviceMin}m</span>
                        <span className="text-slate-400">
                          Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                        </span>
                      </div>
                    ) : (
                      <div className="text-xs text-slate-500 mt-0.5">
                        Transit
                        <span className="ml-1">
                          Load before: {fullBefore} full, {emptyBefore} empty · after: {fullAfter} full, {emptyAfter} empty
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="text-xs text-slate-500">No swap details available for this route.</p>
        )}
      </div>

      {/* Legs table (collapsible) */}
      <div className="mt-3 pt-3 border-t border-slate-600">
        <button
          onClick={() => setShowLegsBreakdown(!showLegsBreakdown)}
          className="w-full flex items-center justify-between mb-2 text-left"
        >
          <span className="text-xs font-medium text-slate-400">Legs Breakdown</span>
          <svg
            className={`w-3 h-3 text-slate-400 transition-transform ${showLegsBreakdown ? 'rotate-90' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </button>
        {!showLegsBreakdown ? (
          <p className="text-xs text-slate-500">Collapsed to reduce scrolling.</p>
        ) : route.legs && route.legs.length > 0 ? (
          <div className="space-y-1">
            <div className="overflow-x-auto">
              <table className="w-full text-sm min-w-[320px]">
                <thead>
                  <tr className="text-xs text-slate-500">
                    <th className="text-left py-1">From</th>
                    <th className="text-left py-1">To</th>
                    <th className="text-right py-1">Distance</th>
                    <th className="text-right py-1">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {route.legs.map((leg, idx) => (
                    <tr key={idx} className="border-t border-slate-600/50">
                      <td className="py-1.5 text-slate-300 truncate max-w-[120px]">
                        {leg.from_site_name || leg.from_site_id}
                      </td>
                      <td className="py-1.5 text-slate-300 truncate max-w-[120px]">
                        {leg.to_site_name || leg.to_site_id}
                      </td>
                      <td className="py-1.5 text-right font-mono">{leg.distance_km.toFixed(1)} km</td>
                      <td className="py-1.5 text-right text-slate-400">
                        {leg.drive_time_hours ? formatTime(leg.drive_time_hours) : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-slate-500 font-medium">
                    <td colSpan={2} className="py-2">Total</td>
                    <td className="py-2 text-right font-mono">{route.total_distance_km.toFixed(1)} km</td>
                    <td className="py-2 text-right text-slate-400">{formatTime(displayTimeHours)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
            <div className="flex justify-between text-xs pt-2 border-t border-slate-600/50">
              <span className="text-slate-500">Sum of legs</span>
              <span className={distanceMismatch ? 'text-red-400' : 'text-slate-400'}>
                {legsSum.toFixed(1)} km
                {distanceMismatch && ' (mismatch!)'}
              </span>
            </div>
          </div>
        ) : (
          <p className="text-sm text-slate-500">No leg data available</p>
        )}
      </div>
    </div>
  );
};
