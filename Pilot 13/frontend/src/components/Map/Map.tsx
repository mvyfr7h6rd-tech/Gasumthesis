import React, { useEffect, useMemo, useState } from 'react';
import { MapContainer, TileLayer, Marker, Popup, useMap, useMapEvents } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet-polylineoffset';
import { useAppStore } from '../../stores/appStore';
import type { Site, Route, CustomPoint } from '../../types';
import { getTruckRouteStyle } from '../../utils/truckColors';
import { computeRouteLabelData } from '../../utils/routeLabels';
import { STATUS_HEX_COLORS, STATUS_TEXT_DARK_CLASSES, getSiteStatus } from '../../utils/siteStatus';

// Fix for default marker icons in Leaflet with Vite
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png';
import markerIcon from 'leaflet/dist/images/marker-icon.png';
import markerShadow from 'leaflet/dist/images/marker-shadow.png';

// @ts-ignore
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: markerIcon2x,
  iconUrl: markerIcon,
  shadowUrl: markerShadow,
});

// Custom marker icons - circles for consumers, triangles for producers
const createCircleIcon = (color: string) => {
  return L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      width: 24px;
      height: 24px;
      background-color: ${color};
      border: 3px solid white;
      border-radius: 50%;
      box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    "></div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    popupAnchor: [0, -12],
  });
};

const createTriangleIcon = (color: string) => {
  return L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      width: 0;
      height: 0;
      border-left: 14px solid transparent;
      border-right: 14px solid transparent;
      border-bottom: 24px solid ${color};
      filter: drop-shadow(0 2px 2px rgba(0,0,0,0.3));
      position: relative;
    "><div style="
      position: absolute;
      width: 0;
      height: 0;
      border-left: 10px solid transparent;
      border-right: 10px solid transparent;
      border-bottom: 17px solid white;
      top: 4px;
      left: -10px;
    "></div><div style="
      position: absolute;
      width: 0;
      height: 0;
      border-left: 8px solid transparent;
      border-right: 8px solid transparent;
      border-bottom: 14px solid ${color};
      top: 6px;
      left: -8px;
    "></div></div>`,
    iconSize: [28, 24],
    iconAnchor: [14, 24],
    popupAnchor: [0, -24],
  });
};

const icons = {
  producer: {
    normal:   createTriangleIcon(STATUS_HEX_COLORS.normal),
    warning:  createTriangleIcon(STATUS_HEX_COLORS.warning),
    critical: createTriangleIcon(STATUS_HEX_COLORS.critical),
    safe:     createTriangleIcon(STATUS_HEX_COLORS.safe),
  },
  consumer: {
    normal:   createCircleIcon(STATUS_HEX_COLORS.normal),
    warning:  createCircleIcon(STATUS_HEX_COLORS.warning),
    critical: createCircleIcon(STATUS_HEX_COLORS.critical),
    safe:     createCircleIcon(STATUS_HEX_COLORS.safe),
  },
};

// Highlighted (hovered from diagnostics) icon variants — pulsing white ring
const createHighlightedCircleIcon = (color: string) =>
  L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      width: 30px; height: 30px;
      background-color: ${color};
      border: 3px solid white;
      border-radius: 50%;
      box-shadow: 0 0 0 4px white, 0 0 12px 6px ${color};
      animation: site-pulse 1s ease-in-out infinite;
    "></div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -15],
  });

const createHighlightedTriangleIcon = (color: string) =>
  L.divIcon({
    className: 'custom-marker',
    html: `<div style="
      filter: drop-shadow(0 0 8px ${color}) drop-shadow(0 0 3px white);
      animation: site-pulse 1s ease-in-out infinite;
    "><div style="
      width: 0; height: 0;
      border-left: 16px solid transparent;
      border-right: 16px solid transparent;
      border-bottom: 28px solid ${color};
      position: relative;
    "></div></div>`,
    iconSize: [32, 28],
    iconAnchor: [16, 28],
    popupAnchor: [0, -28],
  });

// Component to handle map centering
const MapController: React.FC<{ selectedSiteId: string | null; sites: Site[] }> = ({
  selectedSiteId,
  sites,
}) => {
  const map = useMap();

  useEffect(() => {
    if (selectedSiteId) {
      const site = sites.find((s) => s.id === selectedSiteId);
      if (site) {
        map.flyTo([site.latitude, site.longitude], 10, { duration: 0.5 });
      }
    }
  }, [selectedSiteId, sites, map]);

  return null;
};

// Listens for double-clicks on the map and fires onDoubleClick with lat/lng
const MapDoubleClickHandler: React.FC<{ onDoubleClick: (lat: number, lng: number) => void }> = ({
  onDoubleClick,
}) => {
  useMapEvents({
    dblclick(e) {
      onDoubleClick(e.latlng.lat, e.latlng.lng);
    },
  });
  return null;
};

// Helper: build lat/lon positions for a route.
//
// Priority:
//   1. route.geometry — full decoded road polyline (GraphHopper or OSRM, multi-stop)
//   2. leg.geometry   — stitch per-leg road polylines into one path
//   3. straight lines between stop coordinates (last resort)
function buildRoutePositions(
  route: Route,
  siteMap: Record<string, { latitude: number; longitude: number }>,
): [number, number][] {
  // Tier 1: full route geometry
  if (Array.isArray(route.geometry) && route.geometry.length > 2) {
    return route.geometry.map(([a, b]) => [a, b] as [number, number]);
  }

  // Tier 2: assemble from per-leg geometries
  if (route.legs && route.legs.some((l) => Array.isArray(l.geometry) && l.geometry.length >= 2)) {
    const pts: [number, number][] = [];
    for (const leg of route.legs) {
      if (Array.isArray(leg.geometry) && leg.geometry.length >= 2) {
        // Skip the first point of subsequent legs — it duplicates the previous leg's last point
        const startIdx = pts.length > 0 ? 1 : 0;
        for (let i = startIdx; i < leg.geometry.length; i++) {
          pts.push([leg.geometry[i][0], leg.geometry[i][1]]);
        }
      } else {
        // Straight-line gap for this specific leg when its geometry is missing
        const from = siteMap[leg.from_site_id];
        const to = siteMap[leg.to_site_id];
        if (from && pts.length === 0) pts.push([from.latitude, from.longitude]);
        if (to) pts.push([to.latitude, to.longitude]);
      }
    }
    if (pts.length >= 2) return pts;
  }

  // Tier 3: straight lines between stops
  return route.stops
    .map((stop) => {
      const site = siteMap[stop.site_id];
      return site ? ([site.latitude, site.longitude] as [number, number]) : null;
    })
    .filter((pos): pos is [number, number] => pos !== null);
}

// ─────────────────────────────────────────────────────────────────────────────
// All route polylines rendered imperatively so leaflet-polylineoffset works.
// Routes sharing road segments are offset laterally so they appear as parallel
// lines rather than stacked on top of each other.
// Offset formula: (truckIndex - (numTrucks - 1) / 2) × OFFSET_PX
//   → 1 truck: 0   2 trucks: -3, +3   3 trucks: -6, 0, +6
// ─────────────────────────────────────────────────────────────────────────────
const OFFSET_PX = 6;

const RoutePolylines: React.FC<{
  routes: Route[];
  sites: Site[];
  customPoints: CustomPoint[];
  visibleRouteIds: Set<string>;
  focusedRouteId: string | null;
}> = ({ routes, sites, customPoints, visibleRouteIds, focusedRouteId }) => {
  const map = useMap();

  const siteMap = useMemo(() => {
    const m: Record<string, { latitude: number; longitude: number }> = {};
    for (const s of sites) m[s.id] = s;
    for (const cp of customPoints) {
      if (cp.latitude != null && cp.longitude != null) {
        m[cp.id] = { latitude: cp.latitude!, longitude: cp.longitude! };
      }
    }
    return m;
  }, [sites, customPoints]);

  // Only render routes that are toggled visible
  const visibleRoutes = useMemo(
    () => routes.filter((r) => visibleRouteIds.has(r.id)),
    [routes, visibleRouteIds],
  );

  // Stable sorted unique truck IDs → consistent offset across re-renders
  const routeKeys = useMemo(
    () => [...new Set(routes.map((r) => `${r.truck_id}::${r.day ?? 1}`))].sort(),
    [routes],
  );

  const hasFocus = focusedRouteId !== null && visibleRoutes.some((r) => r.id === focusedRouteId);

  // Auto-zoom to active route bounds
  useEffect(() => {
    if (!focusedRouteId) return;
    const active = routes.find((r) => r.id === focusedRouteId);
    if (!active) return;
    const positions = buildRoutePositions(active, siteMap);
    if (positions.length < 2) return;
    const bounds = L.latLngBounds(positions.map(([lat, lng]) => L.latLng(lat, lng)));
    map.fitBounds(bounds, { padding: [48, 48], maxZoom: 12, animate: true });
  }, [focusedRouteId, routes, siteMap, map]);

  // Draw polylines
  useEffect(() => {
    const group = L.layerGroup().addTo(map);
    const numRouteKeys = routeKeys.length;

    for (const route of visibleRoutes) {
      const positions = buildRoutePositions(route, siteMap);
      if (positions.length < 2) continue;

      const routeKey = `${route.truck_id}::${route.day ?? 1}`;
      const routeIdx = routeKeys.indexOf(routeKey);
      const offset = (routeIdx - (numRouteKeys - 1) / 2) * OFFSET_PX;
      const { color } = getTruckRouteStyle(route.truck_id);

      const isFocused = route.id === focusedRouteId;
      const isDimmed = hasFocus && !isFocused;

      const hasRoadGeometry =
        (Array.isArray(route.geometry) && route.geometry.length > 2) ||
        (route.legs?.some((l) => Array.isArray(l.geometry) && l.geometry.length >= 2) ?? false);
      const dashArray = hasRoadGeometry ? undefined : '8 8';

      L.polyline(positions, {
        color,
        weight: isFocused ? 6 : 3,
        opacity: isDimmed ? 0.25 : 1,
        offset,
        lineCap: 'round',
        lineJoin: 'round',
        dashArray,
      } as L.PolylineOptions).addTo(group);
    }

    return () => {
      group.clearLayers();
      map.removeLayer(group);
    };
  }, [visibleRoutes, siteMap, routeKeys, focusedRouteId, hasFocus, map]);

  return null;
};

// ─────────────────────────────────────────────────────────────────────────────
// Route segment order labels (imperative Leaflet markers via useMap)
// One LayerGroup per render; cleared on routes change or unmount.
// ─────────────────────────────────────────────────────────────────────────────

const RouteSegmentLabels: React.FC<{ routes: Route[]; sites: Site[]; customPoints: CustomPoint[] }> = ({
  routes,
  sites,
  customPoints,
}) => {
  const map = useMap();

  // Build a stable siteId → coords lookup
  const siteMap = useMemo(() => {
    const m: Record<string, { latitude: number; longitude: number }> = {};
    for (const s of sites) m[s.id] = s;
    for (const cp of customPoints) {
      if (cp.latitude != null && cp.longitude != null) {
        m[cp.id] = { latitude: cp.latitude!, longitude: cp.longitude! };
      }
    }
    return m;
  }, [sites, customPoints]);

  // Compute all label data (memoised — only recomputed when routes or sites change)
  const allLabels = useMemo(() => {
    return routes.flatMap((route) => {
      const color = getTruckRouteStyle(route.truck_id).color;
      return computeRouteLabelData(route, siteMap, color);
    });
  }, [routes, siteMap]);

  // Imperatively create/destroy Leaflet markers so they don't block map interaction
  useEffect(() => {
    const group = L.layerGroup().addTo(map);

    for (const lbl of allLabels) {
      let html: string;
      let iconSize: [number, number];
      let iconAnchor: [number, number];

      if (lbl.kind === 'start') {
        html = `<div class="start-badge" style="background:#16a34a">${lbl.label}</div>`;
        iconSize = [26, 18];
        iconAnchor = [13, 9];
      } else if (lbl.kind === 'finish') {
        html = `<div class="finish-badge">${lbl.label}</div>`;
        iconSize = [26, 18];
        iconAnchor = [13, 9];
      } else {
        html = `<div class="seg-badge" style="background:${lbl.color}">${lbl.label}</div>`;
        // Wider pill for "D.N" labels (e.g. "1.1")
        iconSize = [32, 22];
        iconAnchor = [16, 11];
      }

      const icon = L.divIcon({
        className: 'route-seg-label',
        html,
        iconSize,
        iconAnchor,
      });

      L.marker(lbl.midpoint as L.LatLngExpression, {
        icon,
        interactive: false,
        keyboard: false,
      }).addTo(group);
    }

    return () => {
      group.clearLayers();
      map.removeLayer(group);
    };
  }, [allLabels, map]);

  return null;
};

const customPointIcon = L.divIcon({
  className: 'custom-marker',
  html: `<div style="
    width: 22px;
    height: 22px;
    background-color: #f59e0b;
    border: 2px solid white;
    border-radius: 4px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: bold;
    color: white;
    line-height: 1;
  ">CP</div>`,
  iconSize: [22, 22],
  iconAnchor: [11, 11],
  popupAnchor: [0, -11],
});

export const Map: React.FC = () => {
  const {
    sites,
    selectedSiteId,
    hoveredSiteId,
    selectSite,
    activeLayers,
    recommendation,
    customPoints,
    addCustomPoint,
    setCustomPointCoordinates,
    visibleRouteIds,
    focusedRouteId,
  } = useAppStore();

  // Pending double-click position for the "Create Custom Point?" modal
  const [pendingPoint, setPendingPoint] = useState<{ lat: number; lng: number } | null>(null);

  const handleMapDblClick = (lat: number, lng: number) => {
    setPendingPoint({ lat, lng });
  };

  const handleCreateCustomPoint = () => {
    if (!pendingPoint) return;
    const id = addCustomPoint();
    if (id) {
      setCustomPointCoordinates(id, pendingPoint.lat, pendingPoint.lng);
    }
    setPendingPoint(null);
  };

  // Finland center
  const center: [number, number] = [62.0, 25.0];
  const zoom = 6;

  // Filter sites based on active layers
  const visibleSites = sites.filter((site) => {
    if (site.site_type === 'production' && !activeLayers.has('producers')) {
      return false;
    }
    if ((site.site_type === 'traffic' || site.site_type === 'industry') && !activeLayers.has('consumers')) {
      return false;
    }
    return true;
  });

  // Get icon for site — highlighted when referenced by a hovered diagnostic warning
  const getIcon = (site: Site) => {
    if (site.id === hoveredSiteId) {
      const color = STATUS_HEX_COLORS[site.risk_level] ?? STATUS_HEX_COLORS.normal;
      return site.site_type === 'production'
        ? createHighlightedTriangleIcon(color)
        : createHighlightedCircleIcon(color);
    }
    const type = site.site_type === 'production' ? 'producer' : 'consumer';
    return icons[type][site.risk_level] || icons[type].normal;
  };

  return (
    <div className="h-full w-full relative">
      <MapContainer
        center={center}
        zoom={zoom}
        className="h-full w-full"
        style={{ background: '#1e293b' }}
        doubleClickZoom={false}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        <MapController selectedSiteId={selectedSiteId} sites={sites} />
        <MapDoubleClickHandler onDoubleClick={handleMapDblClick} />

        {/* Route polylines - show when recommendedRoutes layer is active */}
        {activeLayers.has('recommendedRoutes') && recommendation?.routes && (
          <RoutePolylines
            routes={recommendation.routes}
            sites={sites}
            customPoints={customPoints}
            visibleRouteIds={visibleRouteIds}
            focusedRouteId={focusedRouteId}
          />
        )}

        {/* Segment order labels (1, 2, 3 … + START badge per route) */}
        {activeLayers.has('recommendedRoutes') && recommendation?.routes && (
          <RouteSegmentLabels
            routes={recommendation.routes.filter((r) => visibleRouteIds.has(r.id))}
            sites={sites}
            customPoints={customPoints}
          />
        )}

        {/* Site markers */}
        {visibleSites.map((site) => (
          <Marker
            key={site.id}
            position={[site.latitude, site.longitude]}
            icon={getIcon(site)}
            eventHandlers={{
              click: () => selectSite(site.id),
            }}
          >
            <Popup>
              <div className="min-w-[200px]">
                <h3 className="font-bold text-lg">{site.name}</h3>
                <p className="text-sm text-gray-600 capitalize">{site.site_type}</p>
                <div className="mt-2 space-y-1 text-sm">
                  <p>
                    <span className="font-medium">Inventory:</span>{' '}
                    {site.total_mwh.toFixed(1)} MWh ({site.bays.length} bays)
                  </p>
                  <p>
                    <span className="font-medium">Utilization:</span>{' '}
                    {site.utilization_percentage.toFixed(0)}%
                  </p>
                  <p>
                    <span className="font-medium">Risk:</span>{' '}
                    <span className={`font-medium ${STATUS_TEXT_DARK_CLASSES[getSiteStatus(site)]}`}>
                      {site.risk_level}
                    </span>
                  </p>
                  <p>
                    <span className="font-medium">Time to critical:</span>{' '}
                    {site.hours_to_critical < 1000
                      ? `${site.hours_to_critical.toFixed(1)}h`
                      : 'N/A'}
                  </p>
                </div>
              </div>
            </Popup>
          </Marker>
        ))}

        {/* Custom point markers (coord-based only) */}
        {customPoints
          .filter((cp) => cp.latitude != null && cp.longitude != null)
          .map((cp) => (
            <Marker
              key={`cp-${cp.id}`}
              position={[cp.latitude!, cp.longitude!]}
              icon={customPointIcon}
            >
              <Popup>
                <div className="min-w-[140px]">
                  <h3 className="font-bold">{cp.label}</h3>
                  <p className="text-xs text-gray-500">Custom Point</p>
                  <p className="text-xs">{cp.latitude!.toFixed(5)}, {cp.longitude!.toFixed(5)}</p>
                </div>
              </Popup>
            </Marker>
          ))}
      </MapContainer>

      {/* Double-click "Create Custom Point?" modal */}
      {pendingPoint && (
        <div className="absolute inset-0 flex items-center justify-center z-[1000] pointer-events-none">
          <div className="bg-slate-800 border border-slate-600 rounded-lg shadow-xl p-5 w-64 pointer-events-auto">
            <h3 className="text-sm font-semibold text-white mb-3">Create Custom Point?</h3>
            <div className="text-xs text-slate-400 space-y-1 mb-4 font-mono">
              <div>Lat: <span className="text-slate-200">{pendingPoint.lat.toFixed(5)}</span></div>
              <div>Lng: <span className="text-slate-200">{pendingPoint.lng.toFixed(5)}</span></div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setPendingPoint(null)}
                className="flex-1 px-3 py-1.5 text-xs rounded bg-slate-700 text-slate-300 hover:bg-slate-600"
              >
                Cancel
              </button>
              <button
                onClick={handleCreateCustomPoint}
                className="flex-1 px-3 py-1.5 text-xs rounded bg-amber-600 text-white hover:bg-amber-500 font-medium"
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Map;
