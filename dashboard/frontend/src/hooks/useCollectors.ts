import { useQuery } from "@tanstack/react-query";
import { api } from "../services/api";

export function useCollectors() {
  return useQuery({
    queryKey: ["collectors"],
    queryFn: api.collectors,
    refetchInterval: 10_000,
  });
}

export function useMediaStats() {
  return useQuery({
    queryKey: ["media-stats"],
    queryFn: api.mediaStats,
    refetchInterval: 30_000,
  });
}
