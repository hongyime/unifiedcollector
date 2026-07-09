import { NavLink } from "react-router-dom";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../services/api";
import {
  LayoutDashboard,
  Database,
  Activity,
  AlertTriangle,
  Settings,
  Target,
  Calendar,
  PlayCircle,
  Network,
  Users,
  Link2,
  ImageIcon,
  Bike,
  MessageCircle,
  QrCode,
  KeyRound,
} from "lucide-react";

// Per-platform sections: each social/messaging/other platform gets its own entry
// -> /platform/<name> (a generic overview: what's been collected + recent activity).
// Specialized pages (strava feed, telegram onboard, wa link) are linked from there.
const groups = [
  {
    label: "Overview",
    items: [
      { to: "/", label: "Dashboard", icon: LayoutDashboard },
      { to: "/collectors", label: "Collectors", icon: Database },
      { to: "/browse", label: "All media", icon: ImageIcon },
    ],
  },
  {
    label: "Social",
    items: [
      { to: "/platform/instagram", label: "Instagram", icon: ImageIcon },
      { to: "/tiktok/feed", label: "TikTok", icon: ImageIcon },
      { to: "/platform/lemon8", label: "Lemon8", icon: ImageIcon },
      { to: "/platform/threads", label: "Threads", icon: ImageIcon },
      { to: "/platform/x", label: "X", icon: ImageIcon },
      { to: "/platform/facebook", label: "Facebook", icon: ImageIcon },
      { to: "/platform/youtube", label: "YouTube", icon: ImageIcon },
    ],
  },
  {
    label: "Messaging",
    items: [
      { to: "/telegram/chats", label: "Telegram", icon: MessageCircle },
      { to: "/platform/whatsapp", label: "WhatsApp", icon: MessageCircle },
      { to: "/platform/beeper", label: "Beeper", icon: MessageCircle },
    ],
  },
  {
    label: "Other",
    items: [
      { to: "/platform/strava", label: "Strava", icon: Bike },
      { to: "/platform/github", label: "GitHub", icon: Database },
      { to: "/platform/search", label: "Search", icon: Activity },
      { to: "/platform/website", label: "Website", icon: Network },
    ],
  },
  {
    label: "People & links",
    items: [
      { to: "/social/users", label: "Social Users", icon: Users },
      { to: "/whatsapp/links", label: "Links", icon: Link2 },
      { to: "/graph", label: "Graph", icon: Network },
    ],
  },
  {
    label: "Manage",
    items: [
      { to: "/accounts", label: "Accounts", icon: KeyRound },
      { to: "/whatsapp/link", label: "Link Device", icon: QrCode },
      { to: "/targets", label: "Targets", icon: Target },
      { to: "/schedules", label: "Schedules", icon: Calendar },
      { to: "/runs", label: "Runs", icon: PlayCircle },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/health", label: "Health", icon: Activity },
      { to: "/dlq", label: "Dead Letters", icon: AlertTriangle },
      { to: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

// Branded collector mark (matches favicon): multi-platform signals funnelled in.
function LogoMark() {
  return (
    <svg viewBox="0 0 48 48" className="w-7 h-7 shrink-0" aria-hidden="true">
      <rect width="48" height="48" rx="11" fill="#12141c" />
      <circle cx="13" cy="12" r="2.6" fill="#47bfff" />
      <circle cx="24" cy="9.5" r="2.6" fill="#9b5cff" />
      <circle cx="35" cy="12" r="2.6" fill="#47bfff" />
      <path d="M8.5 17 L39.5 17 L27 32 L27 40.5 a1.8 1.8 0 0 1-2.6 1.6 L21 40.2 L21 32 Z" fill="#863bff" />
      <circle cx="24" cy="45.4" r="2.2" fill="#47bfff" />
    </svg>
  );
}

// At-a-glance collection health so you don't have to open a page to know the
// firehose is alive. Reuses the same /collectors query as the dashboard.
function StatusPill() {
  // Real liveness from data freshness + source_health (not service_cursors.status,
  // which flips 'idle' between cycles and is 'never' for realtime feeds — that made
  // healthy collectors read as down, e.g. a flickering "9/12").
  const { data } = useQuery({
    queryKey: ["collectors-live"],
    queryFn: api.collectorsLive,
    refetchInterval: 15_000,
  });
  const total = data?.total ?? 0;
  const live = data?.live ?? 0;
  const dot =
    total === 0 ? "bg-text-muted" : live === total ? "bg-emerald-500" : live > 0 ? "bg-amber-500" : "bg-rose-500";
  const notLive = (data?.sources ?? []).filter((s) => s.status !== "live");
  const title = notLive.length
    ? "Not live: " + notLive.map((s) => `${s.source} (${s.status})`).join(", ")
    : "All collectors live";
  return (
    <div className="flex items-center gap-1.5 mt-2" title={title}>
      <span className={clsx("w-2 h-2 rounded-full", dot, live > 0 && "animate-pulse")} />
      <span className="text-[10px] text-text-secondary tabular-nums">
        {total === 0 ? "loading…" : `${live}/${total} collectors live`}
      </span>
    </div>
  );
}

export function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 bottom-0 w-52 bg-surface border-r border-border flex flex-col">
      <div className="p-4 border-b border-border">
        <div className="flex items-center gap-2.5">
          <LogoMark />
          <div className="min-w-0">
            <h1 className="text-sm font-semibold tracking-wide text-text-primary leading-tight">UnifiedCollector</h1>
            <p className="text-[10px] text-text-muted">Collection control</p>
          </div>
        </div>
        <StatusPill />
      </div>
      <nav className="flex-1 p-3 overflow-y-auto">
        {groups.map((group) => (
          <div key={group.label} className="mb-3">
            <p className="px-3 py-1 text-[10px] font-medium uppercase tracking-wider text-text-muted">
              {group.label}
            </p>
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) =>
                    clsx(
                      "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
                      isActive
                        ? "bg-white text-black font-medium"
                        : "text-text-secondary hover:bg-white/10 hover:text-text-primary",
                    )
                  }
                >
                  <item.icon className="w-4 h-4" />
                  {item.label}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
      <div className="p-3 border-t border-border text-[10px] text-text-muted">v0.2.0</div>
    </aside>
  );
}
