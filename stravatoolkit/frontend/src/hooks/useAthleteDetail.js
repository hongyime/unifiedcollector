import { useCallback, useState } from "react";
import { fetchJson } from "../lib/api";

export default function useAthleteDetail() {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadAthleteDetail = useCallback(async (athleteId, month) => {
    if (!athleteId) {
      setDetail(null);
      setError("");
      return;
    }

    setLoading(true);
    setError("");
    try {
      const search = month ? `?month=${month}` : "";
      const payload = await fetchJson(`/api/v1/athletes/${athleteId}${search}`);
      setDetail(payload);
    } catch (loadError) {
      setError(loadError.message);
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, []);

  return { detail, loading, error, loadAthleteDetail };
}
