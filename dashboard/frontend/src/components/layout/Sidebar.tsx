import { NavLink } from "react-router-dom";
import { clsx } from "clsx";
import {
  LayoutDashboard,
  Database,
  FolderOpen,
  Activity,
  AlertTriangle,
  Settings,
  Target,
  Calendar,
  PlayCircle,
  Network,
  ScanFace,
  Users,
  Link2,
  ImageIcon,
} from "lucide-react";

const groups = [
  {
    label: "Operations",
    items: [
      { to: "/", label: "Dashboard", icon: LayoutDashboard },
      { to: "/collectors", label: "Collectors", icon: Database },
      { to: "/targets", label: "Targets", icon: Target },
      { to: "/schedules", label: "Schedules", icon: Calendar },
      { to: "/runs", label: "Runs", icon: PlayCircle },
    ],
  },
  {
    label: "Data",
    items: [
      { to: "/media", label: "Media", icon: FolderOpen },
      { to: "/browse", label: "Browser", icon: ImageIcon },
      { to: "/graph", label: "Graph", icon: Network },
    ],
  },
  {
    label: "WhatsApp",
    items: [
      { to: "/whatsapp/faces", label: "Faces", icon: ScanFace },
      { to: "/whatsapp/users", label: "Users", icon: Users },
      { to: "/whatsapp/links", label: "Links", icon: Link2 },
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

export function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 bottom-0 w-52 bg-surface border-r border-border flex flex-col">
      <div className="p-4 border-b border-border">
        <h1 className="text-sm font-semibold tracking-wide text-text-primary">UnifiedCollector</h1>
        <p className="text-[10px] text-text-muted mt-0.5">Dashboard</p>
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
