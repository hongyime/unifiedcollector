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
import { StoriesPage } from "./features/stories/StoriesPage";
import { UsersPage } from "./features/whatsapp/UsersPage";
import { SocialUsersPage } from "./features/social/SocialUsersPage";
import { AccountsPage } from "./features/accounts/AccountsPage";
import { PlatformPage } from "./features/platform/PlatformPage";
import { LinksPage } from "./features/whatsapp/LinksPage";
import { WhatsAppLinkPage } from "./features/whatsapp/WhatsAppLinkPage";
import { WhatsAppChatsPage } from "./features/whatsapp/WhatsAppChatsPage";
import { StravaFeedPage } from "./features/strava/StravaFeedPage";
import { InstagramDmPage } from "./features/instagram/InstagramDmPage";
import { TiktokDmPage } from "./features/tiktok/TiktokDmPage";
import { TiktokFeedPage } from "./features/tiktok/TiktokFeedPage";
import { ThreadsFeedPage } from "./features/threads/ThreadsFeedPage";
import { YoutubeFeedPage } from "./features/youtube/YoutubeFeedPage";
import { GithubFeedPage } from "./features/github/GithubFeedPage";
import { Lemon8FeedPage } from "./features/lemon8/Lemon8FeedPage";
import { BeeperChatsPage } from "./features/beeper/BeeperChatsPage";
import { LoginPage } from "./features/auth/LoginPage";
import { TelegramAccountsPage } from "./features/telegram/TelegramAccountsPage";
import { TelegramStatsPage } from "./features/telegram/TelegramStatsPage";
import { TelegramChatsPage } from "./features/telegram/TelegramChatsPage";

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
      { path: "stories", element: <StoriesPage /> },
      { path: "graph", element: <GraphPage /> },
      { path: "accounts", element: <AccountsPage /> },
      { path: "platform/:name", element: <PlatformPage /> },
      { path: "social/users", element: <SocialUsersPage /> },
      { path: "whatsapp/users", element: <UsersPage /> },
      { path: "whatsapp/links", element: <LinksPage /> },
      { path: "whatsapp/link", element: <WhatsAppLinkPage /> },
      { path: "whatsapp/chats", element: <WhatsAppChatsPage /> },
      { path: "strava/feed", element: <StravaFeedPage /> },
      { path: "instagram/dms", element: <InstagramDmPage /> },
      { path: "tiktok/dms", element: <TiktokDmPage /> },
      { path: "tiktok/feed", element: <TiktokFeedPage /> },
      { path: "threads/feed", element: <ThreadsFeedPage /> },
      { path: "youtube/feed", element: <YoutubeFeedPage /> },
      { path: "github/feed", element: <GithubFeedPage /> },
      { path: "lemon8/feed", element: <Lemon8FeedPage /> },
      { path: "telegram/accounts", element: <TelegramAccountsPage /> },
      { path: "telegram/stats", element: <TelegramStatsPage /> },
      { path: "telegram/chats", element: <TelegramChatsPage /> },
      { path: "beeper/chats", element: <BeeperChatsPage /> },
      {
        path: "discord/chats",
        element: (
          <BeeperChatsPage
            network="Discord"
            title="Discord"
            subtitle="Discord chats captured through Beeper"
          />
        ),
      },
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
