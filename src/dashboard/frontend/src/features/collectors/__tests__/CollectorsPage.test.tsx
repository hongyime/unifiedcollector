import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { expect, it, vi } from "vitest";
import { CollectorsPage } from "../CollectorsPage";

vi.mock("../../../hooks/useCollectors", () => ({
  useCollectors: () => ({
    data: [{ service: "strava", status: "running", last_processed_id: null, last_processed_at: null }],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

it("links collector names to their detail route", () => {
  render(<MemoryRouter><CollectorsPage /></MemoryRouter>);

  expect(screen.getByRole("link", { name: "strava" })).toHaveAttribute("href", "/collectors/strava");
});
