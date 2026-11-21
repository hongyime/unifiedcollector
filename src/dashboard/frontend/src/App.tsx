import { createElement, lazy, Suspense, type ComponentType } from "react";
import { createBrowserRouter, RouterProvider } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AppShell } from "./components/layout/AppShell";
import { AuthContext, useAuthProvider } from "./hooks/useAuth";
import { DashboardPage } from "./features/collectors/DashboardPage";
import { CollectorsPage } from "./features/collectors/CollectorsPage";
import { CollectorDetailPage } from "./features/collectors/CollectorDetailPage";

const CoveragePage = lazy(() => import("./features/collectors/CoveragePage").then(({ CoveragePage }) => ({ default: CoveragePage })));
const MediaPage = lazy(() => import("./features/collectors/MediaPage").then(({ MediaPage }) => ({ default: MediaPage })));
const DLQPage = lazy(() => import("./features/collectors/DLQPage").then(({ DLQPage }) => ({ default: DLQPage })));
const HealthPage = lazy(() => import("./features/health/HealthPage").then(({ HealthPage }) => ({ default: HealthPage })));
const SettingsPage = lazy(() => import("./features/settings/SettingsPage").then(({ SettingsPage }) => ({ default: SettingsPage })));
const TargetsPage = lazy(() => import("./features/targets/TargetsPage").then(({ TargetsPage }) => ({ default: TargetsPage })));
const SchedulesPage = lazy(() => import("./features/schedules/SchedulesPage").then(({ SchedulesPage }) => ({ default: SchedulesPage })));
const RunsPage = lazy(() => import("./features/runs/RunsPage").then(({ RunsPage }) => ({ default: RunsPage })));
const GraphPage = lazy(() => import("./features/graph/GraphPage").then(({ GraphPage }) => ({ default: GraphPage })));
const ReconPage = lazy(() => import("./features/recon/ReconPage").then(({ ReconPage }) => ({ default: ReconPage })));
const MediaBrowserPage = lazy(() => import("./features/media/MediaBrowserPage").then(({ MediaBrowserPage }) => ({ default: MediaBrowserPage })));
const StoriesPage = lazy(() => import("./features/stories/StoriesPage").then(({ StoriesPage }) => ({ default: StoriesPage })));
const UsersPage = lazy(() => import("./features/whatsapp/UsersPage").then(({ UsersPage }) => ({ default: UsersPage })));
const SocialUsersPage = lazy(() => import("./features/social/SocialUsersPage").then(({ SocialUsersPage }) => ({ default: SocialUsersPage })));
const AccountsPage = lazy(() => import("./features/accounts/AccountsPage").then(({ AccountsPage }) => ({ default: AccountsPage })));
const PlatformPage = lazy(() => import("./features/platform/PlatformPage").then(({ PlatformPage }) => ({ default: PlatformPage })));
const LinksPage = lazy(() => import("./features/whatsapp/LinksPage").then(({ LinksPage }) => ({ default: LinksPage })));
const WhatsAppLinkPage = lazy(() => import("./features/whatsapp/WhatsAppLinkPage").then(({ WhatsAppLinkPage }) => ({ default: WhatsAppLinkPage })));
const WhatsAppChatsPage = lazy(() => import("./features/whatsapp/WhatsAppChatsPage").then(({ WhatsAppChatsPage }) => ({ default: WhatsAppChatsPage })));
const StravaFeedPage = lazy(() => import("./features/strava/StravaFeedPage").then(({ StravaFeedPage }) => ({ default: StravaFeedPage })));
const TiktokFeedPage = lazy(() => import("./features/tiktok/TiktokFeedPage").then(({ TiktokFeedPage }) => ({ default: TiktokFeedPage })));
const ThreadsFeedPage = lazy(() => import("./features/threads/ThreadsFeedPage").then(({ ThreadsFeedPage }) => ({ default: ThreadsFeedPage })));
const YoutubeFeedPage = lazy(() => import("./features/youtube/YoutubeFeedPage").then(({ YoutubeFeedPage }) => ({ default: YoutubeFeedPage })));
const GithubFeedPage = lazy(() => import("./features/github/GithubFeedPage").then(({ GithubFeedPage }) => ({ default: GithubFeedPage })));
const Lemon8FeedPage = lazy(() => import("./features/lemon8/Lemon8FeedPage").then(({ Lemon8FeedPage }) => ({ default: Lemon8FeedPage })));
const BeeperChatsPage = lazy(() => import("./features/beeper/BeeperChatsPage").then(({ BeeperChatsPage }) => ({ default: BeeperChatsPage })));
const LoginPage = lazy(() => import("./features/auth/LoginPage").then(({ LoginPage }) => ({ default: LoginPage })));
const TelegramAccountsPage = lazy(() => import("./features/telegram/TelegramAccountsPage").then(({ TelegramAccountsPage }) => ({ default: TelegramAccountsPage })));
const TelegramStatsPage = lazy(() => import("./features/telegram/TelegramStatsPage").then(({ TelegramStatsPage }) => ({ default: TelegramStatsPage })));
const TelegramChatsPage = lazy(() => import("./features/telegram/TelegramChatsPage").then(({ TelegramChatsPage }) => ({ default: TelegramChatsPage })));

function deferred(Page: ComponentType<any>, props: Record<string, unknown> = {}) {
  return <Suspense fallback={<div className="p-6 text-text-muted">Loading…</div>}><Page {...props} /></Suspense>;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: false },
  },
});

const router = createBrowserRouter([
  {
    path: "/login",
    element: deferred(LoginPage),
  },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: "collectors", element: <CollectorsPage /> },
      { path: "collectors/:source", element: <CollectorDetailPage /> },
      { path: "coverage", element: deferred(CoveragePage) },
      { path: "recon", element: deferred(ReconPage) },
      { path: "targets", element: deferred(TargetsPage) },
      { path: "schedules", element: deferred(SchedulesPage) },
      { path: "runs", element: deferred(RunsPage) },
      { path: "media", element: deferred(MediaPage) },
      { path: "browse", element: deferred(MediaBrowserPage) },
      { path: "stories", element: deferred(StoriesPage) },
      { path: "graph", element: deferred(GraphPage) },
      { path: "accounts", element: deferred(AccountsPage) },
      { path: "platform/:name", element: deferred(PlatformPage) },
      { path: "social/users", element: deferred(SocialUsersPage) },
      { path: "whatsapp/users", element: deferred(UsersPage) },
      { path: "whatsapp/links", element: deferred(LinksPage) },
      { path: "whatsapp/link", element: deferred(WhatsAppLinkPage) },
      { path: "whatsapp/chats", element: deferred(WhatsAppChatsPage) },
      { path: "strava/feed", element: deferred(StravaFeedPage) },
      { path: "tiktok/feed", element: deferred(TiktokFeedPage) },
      { path: "threads/feed", element: deferred(ThreadsFeedPage) },
      { path: "youtube/feed", element: deferred(YoutubeFeedPage) },
      { path: "github/feed", element: deferred(GithubFeedPage) },
      { path: "lemon8/feed", element: deferred(Lemon8FeedPage) },
      { path: "telegram/accounts", element: deferred(TelegramAccountsPage) },
      { path: "telegram/stats", element: deferred(TelegramStatsPage) },
      { path: "telegram/chats", element: deferred(TelegramChatsPage) },
      { path: "beeper/chats", element: deferred(BeeperChatsPage) },
      {
        path: "discord/chats",
        element: deferred(BeeperChatsPage, {
          network: "Discord",
          title: "Discord",
          subtitle: "Discord chats captured through Beeper",
        }),
      },
      { path: "dlq", element: deferred(DLQPage) },
      { path: "health", element: deferred(HealthPage) },
      { path: "settings", element: deferred(SettingsPage) },
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
