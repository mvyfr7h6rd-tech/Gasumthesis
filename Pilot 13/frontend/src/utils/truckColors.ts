/**
 * Deterministic per-truck route color for map polylines and UI elements.
 * Weight and opacity are controlled by active/dim state in the map component.
 */

export interface TruckRouteStyle {
  color: string;
}

const TRUCK_STYLES: Record<string, TruckRouteStyle> = {
  TRUCK001: { color: '#3B82F6' }, // blue
  TRUCK002: { color: '#06B6D4' }, // cyan
  TRUCK003: { color: '#F97316' }, // orange
};

// Fallback palette for unknown truck IDs
const FALLBACK_COLORS = ['#EC4899', '#A855F7', '#14B8A6', '#EAB308'];

function hashString(s: string): number {
  let hash = 0;
  for (let i = 0; i < s.length; i++) {
    hash = ((hash << 5) - hash + s.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function getTruckRouteStyle(truckId: string): TruckRouteStyle {
  if (TRUCK_STYLES[truckId]) return TRUCK_STYLES[truckId];
  return { color: FALLBACK_COLORS[hashString(truckId) % FALLBACK_COLORS.length] };
}
