import type { Route } from '../types';
import { getRouteDisplayTimeHours, getRouteDriveTimeHours } from './routeMetrics';

export type WarningSeverity = 'warn';

export interface RouteWarning {
  code: 'DISTANCE_MISMATCH' | 'SPEED_TOO_HIGH' | 'SPEED_TOO_LOW' | 'SHORT_ROUTE' | 'REPEATED_NODE' | 'TRUCK_UNDERUTILIZED';
  message: string;
  severity: WarningSeverity;
}

const MAX_DRIVER_HOURS = 9.0;

/**
 * Run sanity checks on a single route. Returns any issues found.
 */
export function validateRoute(route: Route): RouteWarning[] {
  const warnings: RouteWarning[] = [];
  const { total_distance_km, stops, legs } = route;
  const totalTimeHours = getRouteDisplayTimeHours(route);

  // ── A. Distance mismatch ─────────────────────────────────────────────────
  const legDistanceSum =
    route.legs_sum_km ??
    legs.reduce((sum, leg) => sum + leg.distance_km, 0);

  if (total_distance_km > 0 && legDistanceSum > 0) {
    const diff = Math.abs(total_distance_km - legDistanceSum);
    if (diff / total_distance_km > 0.05) {
      warnings.push({
        code: 'DISTANCE_MISMATCH',
        message: `Leg sum (${legDistanceSum.toFixed(1)} km) differs from route total (${total_distance_km.toFixed(1)} km) by ${((diff / total_distance_km) * 100).toFixed(0)}%`,
        severity: 'warn',
      });
    }
  }

  // ── B. Unrealistic speed ─────────────────────────────────────────────────
  // Drive time = total time minus service time at swap stops
  const driveTimeHours = Math.max(getRouteDriveTimeHours(route), 0.01);
  const estimatedSpeed = total_distance_km / driveTimeHours;

  if (total_distance_km > 1 && driveTimeHours > 0) {
    if (estimatedSpeed > 110) {
      warnings.push({
        code: 'SPEED_TOO_HIGH',
        message: `Estimated drive speed ${estimatedSpeed.toFixed(0)} km/h is unrealistically high (>110 km/h)`,
        severity: 'warn',
      });
    } else if (estimatedSpeed < 15 && stops.length > 1) {
      warnings.push({
        code: 'SPEED_TOO_LOW',
        message: `Estimated drive speed ${estimatedSpeed.toFixed(0)} km/h is suspiciously low (<15 km/h)`,
        severity: 'warn',
      });
    }
  }

  // ── C. Suspicious short route ────────────────────────────────────────────
  const swapStops = stops.filter(
    (s) =>
      s.swap_operation &&
      ((s.swap_operation.containers_dropped?.length ?? 0) > 0 ||
        (s.swap_operation.containers_picked?.length ?? 0) > 0),
  ).length;

  if (total_distance_km < 5 && swapStops > 1) {
    warnings.push({
      code: 'SHORT_ROUTE',
      message: `Route distance (${total_distance_km.toFixed(1)} km) is unusually small for ${swapStops} swap stops`,
      severity: 'warn',
    });
  }

  // ── D. Repeated node ─────────────────────────────────────────────────────
  // Exclude the first stop (depot/start) from duplicate check since open-route
  // models may share it as a start/end node legitimately.
  const midStops = stops.slice(1);
  const seen = new Set<string>();
  for (const stop of midStops) {
    if (seen.has(stop.site_id)) {
      warnings.push({
        code: 'REPEATED_NODE',
        message: `Site "${stop.site_name}" appears more than once in this route`,
        severity: 'warn',
      });
      break; // one warning per route is enough
    }
    seen.add(stop.site_id);
  }

  // ── E. Severely underutilized truck ─────────────────────────────────────
  const utilization = totalTimeHours / MAX_DRIVER_HOURS;
  if (utilization < 0.25 && total_distance_km > 0) {
    warnings.push({
      code: 'TRUCK_UNDERUTILIZED',
      message: `Truck only uses ${(utilization * 100).toFixed(0)}% of available shift time (${totalTimeHours.toFixed(1)}h / ${MAX_DRIVER_HOURS}h) — solution may be inefficient`,
      severity: 'warn',
    });
  }

  return warnings;
}

/** Validate all routes and return a map of routeId → warnings */
export function validateAllRoutes(routes: Route[]): Map<string, RouteWarning[]> {
  const result = new Map<string, RouteWarning[]>();
  for (const route of routes) {
    const warnings = validateRoute(route);
    if (warnings.length > 0) result.set(route.id, warnings);
  }
  return result;
}
