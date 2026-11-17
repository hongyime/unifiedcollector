import { describe, expect, it } from "vitest";

import { sourceMatrixCacheNotice } from "../sourceMatrixCache";

describe("sourceMatrixCacheNotice", () => {
  it("keeps fresh data quiet and identifies non-current matrix payloads", () => {
    expect(sourceMatrixCacheNotice({ status: "fresh" })).toBeNull();
    expect(sourceMatrixCacheNotice({ status: "rebuilt" })).toBeNull();
    expect(sourceMatrixCacheNotice({ status: "refreshing" })).toContain("cached");
    expect(sourceMatrixCacheNotice({ status: "stale" })).toContain("stale");
    expect(sourceMatrixCacheNotice({ status: "unavailable" })).toContain("unavailable");
  });
});
