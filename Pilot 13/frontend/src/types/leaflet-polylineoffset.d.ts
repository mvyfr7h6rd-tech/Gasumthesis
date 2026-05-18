// Type augmentation for leaflet-polylineoffset plugin
// Adds `offset` option to L.PolylineOptions
declare module 'leaflet' {
  interface PolylineOptions {
    /** Lateral offset in pixels — positive = right of direction, negative = left */
    offset?: number;
  }
}

declare module 'leaflet-polylineoffset' {
  const _: unknown;
  export default _;
}
