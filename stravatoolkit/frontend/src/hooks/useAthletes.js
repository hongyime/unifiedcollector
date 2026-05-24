import { useCallback, useState } from "react";
import { fetchJson } from "../lib/api";

export default function useAthletes() {
  const [athletes, setAthletes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadAthletes = useCallback(async (date, month) => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (date) {
        params.set("date", date);
      }
      if (month) {
        params.set("month", month);
      }
      const search = params.toString() ? `?${params.toString()}` : "";
      const payload = await fetchJson(`/api/v1/athletes${search}`);
      setAthletes(payload.athletes ?? []);
    } catch (loadError) {
      setError(loadError.message);
      setAthletes([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { athletes, loading, error, loadAthletes };
}
