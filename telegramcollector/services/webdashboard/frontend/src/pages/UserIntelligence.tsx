import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import { useApiQuery } from '../hooks/useApi'

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

interface User { id: number; username: string | null; first_name: string | null; last_name: string | null; is_bot: boolean; is_premium: boolean; first_seen: string; last_seen: string }
interface UserStats { total_users: number; bots: number; premium: number; sightings_24h: number; error: string | null }

const USER_COLS: Column<User>[] = [
  { key: 'id', header: 'User ID', mono: true },
  { key: 'username', header: 'Username', mono: true, render: v => v ? <span className="text-white">@{String(v)}</span> : <span className="text-text-muted">—</span> },
  { key: 'first_name', header: 'First Name' },
  { key: 'last_name', header: 'Last Name' },
  { key: 'is_bot', header: 'Bot', render: v => v ? <span className="text-status-running text-xs font-mono">BOT</span> : <span className="text-text-muted text-xs">—</span> },
  { key: 'is_premium', header: 'Premium', render: v => v ? <span className="text-status-pending text-xs font-mono">⭐</span> : <span className="text-text-muted text-xs">—</span> },
  { key: 'last_seen', header: 'Last Seen', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

export default function UserIntelligence() {
  const qc = useQueryClient()
  const statsQ = useApiQuery<UserStats>(['users', 'stats'], '/api/users/stats', { refetchInterval: 30_000 })
  const usersQ = useApiQuery<{ users: User[]; error: string | null }>(['users', 'list'], '/api/users/list', { refetchInterval: 30_000 })

  const stats = statsQ.data

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="User Intelligence"
        subtitle="Profile tracking and sighting history"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['users'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Total Users" value={stats?.total_users ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Bots" value={stats?.bots ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Premium" value={stats?.premium ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Sightings (24h)" value={stats?.sightings_24h ?? '—'} mono /></div>

        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Users — Recent Activity</h2>
          </div>
          {usersQ.isLoading ? <LoadingSpinner className="py-12" label="Loading users…" /> : (
            <DataTable columns={USER_COLS as unknown as Column<Record<string, unknown>>[]} data={(usersQ.data?.users ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No users tracked yet" maxHeight="560px" />
          )}
        </div>
      </div>
    </div>
  )
}
