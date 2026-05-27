import { createElement } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "./components/layout/AppShell";
import { AuthContext, useAuthProvider } from "./hooks/useAuth";
import { DashboardPage } from "./features/collectors/DashboardPage";
import { CollectorsPage } from "./features/collectors/CollectorsPage";
import { CollectorDetailPage } from "./features/collectors/CollectorDetailPage";
import { MediaPage } from "./features/collectors/MediaPage";
import { DLQPage } from "./features/collectors/DLQPage";
import { HealthPage } from "./features/health/HealthPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { TargetsPage } from "./features/targets/TargetsPage";
import { SchedulesPage } from "./features/schedules/SchedulesPage";
import { RunsPage } from "./features/runs/RunsPage";
import { GraphPage } from "./features/graph/GraphPage";
import { MediaBrowserPage } from "./features/media/MediaBrowserPage";
import { UsersPage } from "./features/whatsapp/UsersPage";
import { LinksPage } from "./features/whatsapp/LinksPage";
import { StravaFeedPage } from "./features/strava/StravaFeedPage";
import { LoginPage } from "./features/auth/LoginPage";
import { TelegramAccountsPage } from "./features/telegram/TelegramAccountsPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: false },
  },
});

const router = createBrowserRouter([
  {
    path: "/login",
    element: <LoginPage />,
  },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "collectors", element: <CollectorsPage /> },
      { path: "collectors/:source", element: <CollectorDetailPage /> },
      { path: "targets", element: <TargetsPage /> },
      { path: "schedules", element: <SchedulesPage /> },
      { path: "runs", element: <RunsPage /> },
      { path: "media", element: <MediaPage /> },
      { path: "browse", element: <MediaBrowserPage /> },
      { path: "graph", element: <GraphPage /> },
      { path: "whatsapp/users", element: <UsersPage /> },
      { path: "whatsapp/links", element: <LinksPage /> },
      { path: "strava/feed", element: <StravaFeedPage /> },
      { path: "telegram/accounts", element: <TelegramAccountsPage /> },
      { path: "dlq", element: <DLQPage /> },
      { path: "health", element: <HealthPage /> },
      { path: "settings", element: <SettingsPage /> },
    ],
  },
]);

function AuthProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuthProvider();
  return createElement(AuthContext.Provider, { value: auth }, children);
}

export function App() {
  return (
    <AuthProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </AuthProvider>
  );
}
