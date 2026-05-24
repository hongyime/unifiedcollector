import { useCallback, useState } from "react";
import { fetchJson } from "../lib/api";

export default function useAthleteRoutes() {
  const [routes, setRoutes] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadAthleteRoutes = useCallback(async (athleteId) => {
    if (!athleteId) {
      setRoutes(null);
      setError("");
      return;
    }

    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson(`/api/v1/athletes/${athleteId}/routes`);
      setRoutes(payload);
    } catch (loadError) {
      setError(loadError.message);
      setRoutes(null);
    } finally {
      setLoading(false);
    }
  }, []);

  return { routes, loading, error, loadAthleteRoutes };
}
