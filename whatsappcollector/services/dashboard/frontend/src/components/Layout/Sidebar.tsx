import { NavLink } from 'react-router-dom'
import {
  Activity,
  Database,
  Image,
  Users,
  Link,
  Send,
  Settings,
  Cpu,
} from 'lucide-react'
import { useHealthWS } from '../../hooks/useHealthWS'

interface NavItem {
  to: string
  icon: React.ReactNode
  label: string
}

const NAV_ITEMS: NavItem[] = [
  { to: '/health', icon: <Activity size={16} />, label: 'System Health' },
  { to: '/collector', icon: <Database size={16} />, label: 'Collector' },
  { to: '/media', icon: <Image size={16} />, label: 'Media Archival' },
  { to: '/faces', icon: <Cpu size={16} />, label: 'Face Recognition' },
  { to: '/users', icon: <Users size={16} />, label: 'User Intelligence' },
  { to: '/links', icon: <Link size={16} />, label: 'Link Discovery' },
  { to: '/bulk', icon: <Send size={16} />, label: 'Bulk Sender' },
  { to: '/config', icon: <Settings size={16} />, label: 'Live Config' },
]

export default function Sidebar() {
  const { connected, services } = useHealthWS()

  const upCount = services.filter((s) => s.status === 'up').length
  const downCount = services.filter((s) => s.status === 'down').length

  return (
    <aside className="w-56 flex-shrink-0 h-screen flex flex-col bg-bg-surface border-r border-border">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-border">
        <div className="flex items-center gap-3">
          <span className="font-mono text-xl font-semibold text-white tracking-tight">
            WAC
          </span>
          <span className="text-text-muted text-xs font-mono">dashboard</span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              [
                'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors duration-100',
                isActive
                  ? 'bg-white text-black font-medium'
                  : 'text-text-secondary hover:text-white hover:bg-accent-10',
              ].join(' ')
            }
          >
            {item.icon}
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>

      {/* Bottom status */}
      <div className="px-4 py-4 border-t border-border space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-text-muted text-xs font-mono">WS</span>
          <div className="flex items-center gap-1.5">
            <div
              className={[
                'w-1.5 h-1.5 rounded-full',
                connected ? 'bg-status-up animate-pulse-slow' : 'bg-status-down',
              ].join(' ')}
            />
            <span className={['text-xs font-mono', connected ? 'text-status-up' : 'text-status-down'].join(' ')}>
              {connected ? 'live' : 'off'}
            </span>
          </div>
        </div>
        {services.length > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-text-muted text-xs">services</span>
            <div className="flex items-center gap-2">
              <span className="text-status-up text-xs font-mono">{upCount}↑</span>
              {downCount > 0 && (
                <span className="text-status-down text-xs font-mono">{downCount}↓</span>
              )}
            </div>
          </div>
        )}
        <div className="text-text-muted text-xs font-mono">v1.0.0</div>
      </div>
    </aside>
  )
}
