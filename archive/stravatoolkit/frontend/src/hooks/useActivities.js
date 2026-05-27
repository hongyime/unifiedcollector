import { useCallback, useState } from "react";
import { fetchJson } from "../lib/api";

export default function useActivities() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadDate = useCallback(async (date) => {
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson(`/api/v1/activities?date=${date}`);
      setData(payload);
    } catch (loadError) {
      setError(loadError.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  return { data, loading, error, loadDate };
}
