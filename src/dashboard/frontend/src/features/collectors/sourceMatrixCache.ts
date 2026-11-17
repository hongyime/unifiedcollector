import type { SourceCollectionMatrix } from "../../services/types";

export function sourceMatrixCacheNotice(cache: SourceCollectionMatrix["cache"]) {
  switch (cache?.status) {
    case "refreshing":
      return "Showing cached source matrix while it refreshes";
    case "stale":
      return "Showing stale source matrix data";
    case "unavailable":
      return "Source matrix is unavailable; showing a known-source fallback";
    default:
      return null;
  }
}
