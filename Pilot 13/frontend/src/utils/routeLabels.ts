/**
 * Utilities for computing route segment label positions on the Leaflet map.
 *
 * Label positions are computed by arc distance (not average lat/lng) so they
 * sit at the true visual midpoint of each polyline segment.
 */

/** Haversine distance in km between two [lat, lng] points. */
function haversineKm(a: [number, number], b: [number, number]): number {
  const R = 6371;
  const dLat = ((b[0] - a[0]) * Math.PI) / 180;
  const dLon = ((b[1] - a[1]) * Math.PI) / 180;
  const sinLat = Math.sin(dLat / 2);
  const sinLon = Math.sin(dLon / 2);
  const c =
    sinLat * sinLat +
    Math.cos((a[0] * Math.PI) / 180) *
      Math.cos((b[0] * Math.PI) / 180) *
      sinLon * sinLon;
  return R * 2 * Math.atan2(Math.sqrt(c), Math.sqrt(1 - c));
}

/**
 * Find the geographic midpoint of a polyline by arc distance (not average lat/lng).
 *
 * Builds a cumulative distance array, finds the halfway distance, then
 * interpolates linearly within the matching sub-segment.
 *
 * @param points  Array of [lat, lng] with at least 2 entries.
 * @returns       [lat, lng] of the distance-based midpoint.
 */
export function midpointByDistance(points: [number, number][]): [number, number] {
  if (points.length < 2) return points[0];

  // Fast path: straight 2-point segment
  if (points.length === 2) {
    return [
      (points[0][0] + points[1][0]) / 2,
      (points[0][1] + points[1][1]) / 2,
    ];
  }

  // Cumulative distance array
  const cumDist: number[] = [0];
  for (let i = 1; i < points.length; i++) {
    cumDist.push(cumDist[i - 1] + haversineKm(points[i - 1], points[i]));
  }

  const half = cumDist[cumDist.length - 1] / 2;

  for (let i = 1; i < cumDist.length; i++) {
    if (cumDist[i] >= half) {
      const segLen = cumDist[i] - cumDist[i - 1];
      const t = segLen > 0 ? (half - cumDist[i - 1]) / segLen : 0;
      return [
        points[i - 1][0] + t * (points[i][0] - points[i - 1][0]),
        points[i - 1][1] + t * (points[i][1] - points[i - 1][1]),
      ];
    }
  }

  // Fallback — should not reach here
  return points[Math.floor(points.length / 2)];
}

/** Data for a single rendered label. */
export interface SegmentLabelData {
  midpoint: [number, number];
  label: string;
  color: string;
  kind: 'start' | 'finish' | 'segment';
}

/**
 * Compute all label positions for one route (pure function, memoize-friendly).
 *
 * Output order: [START badge, segment badges..., FINISH badge].
 *
 * Labels use hierarchical day.stop numbering:
 *   Day 1 → S, 1.1, 1.2, …, F
 *   Day 2 → S, 2.1, 2.2, …, F
 */
export function computeRouteLabelData(
  route: {
    day_index: number;
    stops: Array<{ site_id: string }>;
    legs: Array<{
      from_site_id: string;
      to_site_id: string;
      geometry?: [number, number][] | null;
    }>;
  },
  siteMap: Record<string, { latitude: number; longitude: number }>,
  color: string,
): SegmentLabelData[] {
  const results: SegmentLabelData[] = [];
  const { day_index, stops, legs } = route;

  if (stops.length === 0) return results;

  // ── START badge at first stop ─────────────────────────────────────────────
  const firstSite = siteMap[stops[0].site_id];
  if (firstSite) {
    results.push({
      midpoint: [firstSite.latitude, firstSite.longitude],
      label: 'S',
      color,
      kind: 'start',
    });
  }

  // ── Per-segment (leg) badges ──────────────────────────────────────────────
  const segCount = legs.length > 0 ? legs.length : Math.max(0, stops.length - 1);

  for (let k = 0; k < segCount; k++) {
    let segPoints: [number, number][] | null = null;

    if (legs[k]) {
      const leg = legs[k];
      if (leg.geometry && leg.geometry.length >= 2) {
        segPoints = leg.geometry;
      } else {
        const from = siteMap[leg.from_site_id];
        const to = siteMap[leg.to_site_id];
        if (from && to) {
          segPoints = [
            [from.latitude, from.longitude],
            [to.latitude, to.longitude],
          ];
        }
      }
    } else if (stops[k] && stops[k + 1]) {
      const from = siteMap[stops[k].site_id];
      const to = siteMap[stops[k + 1].site_id];
      if (from && to) {
        segPoints = [
          [from.latitude, from.longitude],
          [to.latitude, to.longitude],
        ];
      }
    }

    if (!segPoints || segPoints.length < 2) continue;

    results.push({
      midpoint: midpointByDistance(segPoints),
      label: `${day_index}.${k + 1}`,
      color,
      kind: 'segment',
    });
  }

  // ── FINISH badge at last stop ─────────────────────────────────────────────
  if (stops.length > 1) {
    const lastSite = siteMap[stops[stops.length - 1].site_id];
    if (lastSite) {
      results.push({
        midpoint: [lastSite.latitude, lastSite.longitude],
        label: 'F',
        color,
        kind: 'finish',
      });
    }
  }

  return results;
}
