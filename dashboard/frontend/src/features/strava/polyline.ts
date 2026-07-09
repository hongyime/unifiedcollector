// Google encoded polyline decoder — ports the algorithm at
// https://developers.google.com/maps/documentation/utilities/polylinealgorithm
// so the dashboard can render Strava `summary_polyline` route thumbnails
// client-side without a mapping library.
//
// Contract:
//   decodePolyline("") === []
//   decodePolyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
//       -> [[38.5,-120.2],[40.7,-120.95],[43.252,-126.453]]
// Inputs that aren't 5-bit-chunk-clean fall through with a short prefix of
// valid points rather than throwing — the SVG renderer degrades to "no map"
// via the empty-array check.
export function decodePolyline(encoded: string | null | undefined): [number, number][] {
  if (!encoded) return [];
  const points: [number, number][] = [];
  let index = 0;
  let lat = 0;
  let lng = 0;
  const len = encoded.length;
  while (index < len) {
    // lat delta
    let result = 0;
    let shift = 0;
    let byte: number;
    do {
      if (index >= len) return points;
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    const dLat = (result & 1) ? ~(result >> 1) : (result >> 1);
    lat += dLat;
    // lng delta
    result = 0;
    shift = 0;
    do {
      if (index >= len) return points;
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    const dLng = (result & 1) ? ~(result >> 1) : (result >> 1);
    lng += dLng;
    points.push([lat / 1e5, lng / 1e5]);
  }
  return points;
}

// Scale lat/lng points into a fixed-size SVG viewBox path. Uses equirectangular
// projection with a cos(lat) longitude correction so a run in Singapore doesn't
// stretch horizontally. Returns null when the track is empty or degenerate.
export function polylineToSvgPath(
  points: [number, number][],
  width: number,
  height: number,
  padding = 3,
): string | null {
  if (points.length < 2) return null;
  let minLat = points[0][0];
  let maxLat = points[0][0];
  let minLng = points[0][1];
  let maxLng = points[0][1];
  for (const [la, ln] of points) {
    if (la < minLat) minLat = la;
    if (la > maxLat) maxLat = la;
    if (ln < minLng) minLng = ln;
    if (ln > maxLng) maxLng = ln;
  }
  const midLat = (minLat + maxLat) / 2;
  const cosLat = Math.cos((midLat * Math.PI) / 180) || 1;
  const spanLat = Math.max(maxLat - minLat, 1e-6);
  const spanLng = Math.max((maxLng - minLng) * cosLat, 1e-6);
  const scale = Math.min(
    (width - 2 * padding) / spanLng,
    (height - 2 * padding) / spanLat,
  );
  const projW = spanLng * scale;
  const projH = spanLat * scale;
  const offX = (width - projW) / 2;
  const offY = (height - projH) / 2;
  const parts: string[] = [];
  for (let i = 0; i < points.length; i++) {
    const [la, ln] = points[i];
    const x = offX + (ln - minLng) * cosLat * scale;
    // SVG y-axis grows downward — flip lat so north is at the top.
    const y = offY + (maxLat - la) * scale;
    parts.push(`${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`);
  }
  return parts.join(" ");
}
