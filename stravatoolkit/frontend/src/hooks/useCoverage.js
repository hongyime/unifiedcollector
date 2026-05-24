import { useCallback, useState } from "react";
import { fetchJson } from "../lib/api";

export default function useCoverage() {
  const [coverage, setCoverage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadCoverage = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await fetchJson("/api/v1/backfill/coverage");
      setCoverage(payload);
    } catch (loadError) {
      setError(loadError.message);
      setCoverage(null);
    } finally {
      setLoading(false);
    }
  }, []);

  return { coverage, loading, error, loadCoverage };
}
