import { useQuery } from "@tanstack/react-query";
import { api } from "../services/api";

export function useMedia(source?: string, limit = 50) {
  return useQuery({
    queryKey: ["media", source, limit],
    queryFn: () => api.media(source, limit),
    refetchInterval: 30_000,
  });
}
