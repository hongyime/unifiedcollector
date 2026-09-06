import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Mock the API client so <Sidebar />'s liveness useQuery doesn't fire real
// network calls (`/api/collectors/live`) inside jsdom. The smoke test only
// cares that the shell chrome renders — the query resolves to a benign empty
// snapshot so no unhandled rejections leak into the test runner output.
vi.mock("../../../services/api", () => ({
  api: {
    collectorsLive: vi.fn().mockResolvedValue({ live: 0, total: 0, sources: [] }),
  },
}));

import { AppShell } from "../AppShell";

function renderShell() {
  // Fresh QueryClient per test — no shared cache across cases. `retry: false`
  // keeps any unmocked query from spamming retries during teardown.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<AppShell />}>
            <Route index element={<div data-testid="outlet-content">home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("renders the sidebar brand header", () => {
    renderShell();
    // Sidebar's <h1>UnifiedCollector</h1> — the shell's primary heading.
    expect(
      screen.getByRole("heading", { level: 1, name: /UnifiedCollector/i }),
    ).toBeInTheDocument();
  });

  it("mounts nested route content via <Outlet />", () => {
    renderShell();
    expect(screen.getByTestId("outlet-content")).toBeInTheDocument();
  });
});
