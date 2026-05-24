import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";
import { useFilters } from "../context/FilterContext";
import { fetchJson } from "../lib/api";

const STADIA_KEY = import.meta.env.VITE_STADIA_MAPS_API_KEY || "";
const FALLBACK_CENTER = [103.8198, 1.3521];

export default function RoutesTab() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const { filters } = useFilters();
  const [clusters, setClusters] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const style = {
      version: 8,
      sources: {
        stadia: {
          type: "raster",
          tiles: [`https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png${STADIA_KEY ? "?api_key=" + STADIA_KEY : ""}`],
          tileSize: 256,
          attribution: "© Stadia Maps"
        }
      },
      layers: [{ id: "bg", type: "raster", source: "stadia" }]
    };
    mapRef.current = new maplibregl.Map({ container: containerRef.current, style, center: FALLBACK_CENTER, zoom: 11 });
    mapRef.current.addControl(new maplibregl.NavigationControl(), "top-right");
    mapRef.current.on("load", () => loadClusters());
    return () => { if (mapRef.current) { mapRef.current.remove(); mapRef.current = null; } };
  }, []);

  useEffect(() => {
    if (mapRef.current?.loaded()) loadClusters();
  }, [filters]);

  async function loadClusters() {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.sportType) params.set("sport_type", filters.sportType);
      const data = await fetchJson(`/api/v1/routes/clusters?${params}`);
      setClusters(data || []);
      plotClusters(data || []);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  function plotClusters(data) {
    const map = mapRef.current;
    if (!map) return;
    const features = data.map((c) => ({
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: [
          [c.centroid_start_lon, c.centroid_start_lat],
          [c.centroid_end_lon, c.centroid_end_lat]
        ]
      },
      properties: { count: c.activity_count, cluster_id: c.cluster_id }
    }));

    if (map.getSource("clusters-src")) {
      map.getSource("clusters-src").setData({ type: "FeatureCollection", features });
    } else {
      map.addSource("clusters-src", { type: "geojson", data: { type: "FeatureCollection", features } });
      map.addLayer({
        id: "clusters-layer",
        type: "line",
        source: "clusters-src",
        paint: {
          "line-color": "#e94560",
          "line-width": ["interpolate", ["linear"], ["get", "count"], 1, 1, 100, 6],
          "line-opacity": 0.8
        }
      });
    }
  }

  return (
    <div className="tabContent tabContentMap">
      {loading && <div className="tabOverlay">Loading routes...</div>}
      <div className="routesSidebar">
        <h3>Route Clusters ({clusters.length})</h3>
        <div className="clusterList">
          {clusters.slice(0, 20).map((c) => (
            <div key={c.cluster_id} className="clusterItem">
              <span className="clusterCount">{c.activity_count}</span>
              <span className="clusterType">{c.sport_type}</span>
              <span className="clusterAth">{c.athlete_count} athletes</span>
            </div>
          ))}
        </div>
      </div>
      <div ref={containerRef} className="mapFill" />
    </div>
  );
}
