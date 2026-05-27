import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/api";

export default function NetworkTab() {
  const containerRef = useRef(null);
  const cyRef = useRef(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [edgeCount, setEdgeCount] = useState(0);

  useEffect(() => {
    loadNetwork();
    return () => { if (cyRef.current) { cyRef.current.destroy(); cyRef.current = null; } };
  }, []);

  async function loadNetwork() {
    setLoading(true);
    setError(null);
    try {
      const [athleteData, coData] = await Promise.all([
        fetchJson("/api/v1/athletes"),
        fetchJson("/api/v1/analysis/proximity?min_count=2"),
      ]);

      const athletes = athleteData?.athletes || athleteData || [];
      const pairs = Array.isArray(coData) ? coData : [];
      setEdgeCount(pairs.length);

      if (!containerRef.current) return;

      const cytoscape = (await import("cytoscape")).default;
      if (cyRef.current) { cyRef.current.destroy(); }

      const athleteMap = new Map(athletes.map((a) => [a.athlete_id, a.name]));
      const nodeIds = new Set();
      pairs.forEach((p) => { nodeIds.add(p.athlete_id_a); nodeIds.add(p.athlete_id_b); });

      const nodes = Array.from(nodeIds).map((id) => ({
        data: { id: String(id), label: athleteMap.get(id) || `#${id}` }
      }));
      const edges = pairs.map((p, i) => ({
        data: {
          id: `e${i}`,
          source: String(p.athlete_id_a),
          target: String(p.athlete_id_b),
          weight: p.co_occurrence_count
        }
      }));

      cyRef.current = cytoscape({
        container: containerRef.current,
        elements: { nodes, edges },
        style: [
          {
            selector: "node",
            style: {
              "background-color": "#e94560",
              "label": "data(label)",
              "color": "#ffffff",
              "font-size": "10px",
              "text-valign": "bottom",
              "text-margin-y": "4px",
              "width": "20px",
              "height": "20px"
            }
          },
          {
            selector: "edge",
            style: {
              "line-color": "#2a6a9a",
              "width": ["mapData", "weight", 1, 20, 1, 6],
              "opacity": 0.7
            }
          }
        ],
        layout: { name: "cose", idealEdgeLength: 100, nodeOverlap: 20, animate: false }
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="tabContent tabContentMap">
      {loading && <div className="tabOverlay">Building network...</div>}
      {error && <div className="tabOverlay tabError">Network error: {error}</div>}
      {!loading && !error && edgeCount === 0 && (
        <div className="tabOverlay tabEmpty">
          No co-occurrence data yet. Run Analysis → Co-occurrence to compute it.
        </div>
      )}
      <div ref={containerRef} className="mapFill" />
    </div>
  );
}
