import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";
import { useFilters } from "../context/FilterContext";
import { fetchJson } from "../lib/api";

const STADIA_KEY = import.meta.env.VITE_STADIA_MAPS_API_KEY || "";
const FALLBACK_CENTER = [103.8198, 1.3521];

export default function HeatmapTab() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const { filters } = useFilters();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const style = {
      version: 8,
      sources: {
        stadia: {
          type: "raster",
          tiles: [`https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png${STADIA_KEY ? "?api_key=" + STADIA_KEY : ""}`],
          tileSize: 256,
          attribution: "© Stadia Maps © OpenMapTiles © OpenStreetMap"
        }
      },
      layers: [{ id: "bg", type: "raster", source: "stadia" }]
    };
    mapRef.current = new maplibregl.Map({ container: containerRef.current, style, center: FALLBACK_CENTER, zoom: 11 });
    mapRef.current.addControl(new maplibregl.NavigationControl(), "top-right");
    mapRef.current.on("load", () => loadHeatmap());
    return () => { if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; } };
  }, []);

  useEffect(() => {
    if (mapRef.current?.loaded()) loadHeatmap();
  }, [filters]);

  async function loadHeatmap() {
    const map = mapRef.current;
    if (!map) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (filters.sportType) params.set("sport_type", filters.sportType);
      if (filters.dateFrom) params.set("date_from", filters.dateFrom);
      if (filters.dateTo) params.set("date_to", filters.dateTo);
      const data = await fetchJson(`/api/v1/heatmap?${params}`);
      if (!Array.isArray(data) || !data.length) return;

      const features = data.map((h) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [h.lon, h.lat] },
        properties: { count: h.count }
      }));
      const maxCount = Math.max(...data.map((h) => h.count));

      if (map.getSource("heatmap-src")) {
        map.getSource("heatmap-src").setData({ type: "FeatureCollection", features });
      } else {
        map.addSource("heatmap-src", { type: "geojson", data: { type: "FeatureCollection", features } });
        map.addLayer({
          id: "heatmap-layer",
          type: "heatmap",
          source: "heatmap-src",
          paint: {
            "heatmap-weight": ["interpolate", ["linear"], ["get", "count"], 0, 0, maxCount, 1],
            "heatmap-intensity": 1.5,
            "heatmap-radius": 20,
            "heatmap-color": [
              "interpolate", ["linear"], ["heatmap-density"],
              0, "rgba(0,0,255,0)",
              0.2, "#4040ff",
              0.5, "#e94560",
              0.8, "#ff8c00",
              1, "#ffffff"
            ],
            "heatmap-opacity": 0.8
          }
        });
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="tabContent tabContentMap">
      {loading && <div className="tabOverlay">Loading heatmap...</div>}
      {error && <div className="tabOverlay tabError">{error}</div>}
      <div ref={containerRef} className="mapFill" />
    </div>
  );
}
