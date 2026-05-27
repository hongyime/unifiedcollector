import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import Button from '../components/UI/Button'
import { useApiQuery, apiFetch } from '../hooks/useApi'
import { Search, ChevronLeft, User } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

interface UserRecord {
  jid: string
  phone_number: string | null
  display_name: string | null
  push_name: string | null
  business_name: string | null
  is_business: boolean
  is_verified: boolean
  first_seen: string
  last_seen: string
}

interface HistoryEntry {
  id: number
  user_jid: string
  field_name: string
  old_value: string | null
  new_value: string | null
  changed_at: string
}

interface Membership {
  user_jid: string
  chat_jid: string
  first_seen: string
  last_seen: string
  message_count: number
}

interface UserHistoryResp {
  user: UserRecord
  profile_history: HistoryEntry[]
  memberships: Membership[]
  error: string | null
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export default function UserIntelligence() {
  const [query, setQuery] = useState('')
  const [selectedJid, setSelectedJid] = useState<string | null>(null)
  const qc = useQueryClient()

  const statsQ = useApiQuery<{
    total_users: number; changes_today: number; connections: number;
    tracked_memberships: number; top_chat: { chat_jid: string; member_count: number } | null;
    error: string | null
  }>(['users', 'stats'], '/api/users/stats', { refetchInterval: 30_000 })

  const searchQ = useApiQuery<{ users: UserRecord[]; error: string | null }>(
    ['users', 'search', query],
    `/api/users/search?q=${encodeURIComponent(query)}`,
    { refetchInterval: undefined }
  )

  const historyQ = useQuery<UserHistoryResp>({
    queryKey: ['users', 'history', selectedJid],
    queryFn: () => apiFetch<UserHistoryResp>(`/api/users/${encodeURIComponent(selectedJid!)}/history`),
    enabled: !!selectedJid,
  })

  const stats = statsQ.data
  const users = searchQ.data?.users ?? []
  const history = historyQ.data

  return (
    <div className="flex flex-col h-full">
      <Header
        title="User Intelligence"
        subtitle="User profile tracking and change history"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['users'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="flex-1 p-6 space-y-5 overflow-auto">
        {/* Metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <MetricCard label="Total Users" value={stats?.total_users ?? '—'} mono />
          <MetricCard label="Changes Today" value={stats?.changes_today ?? '—'} mono trend={stats?.changes_today ? 'up' : 'neutral'} />
          <MetricCard label="Connections" value={stats?.connections ?? '—'} mono />
          <MetricCard label="Tracked Memberships" value={stats?.tracked_memberships ?? '—'} mono />
          <MetricCard
            label="Top Chat"
            value={stats?.top_chat?.member_count ?? '—'}
            sublabel={stats?.top_chat?.chat_jid?.slice(0, 20) ?? undefined}
            mono
          />
        </div>

        {selectedJid ? (
          /* User detail view */
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <Button size="sm" variant="ghost" onClick={() => setSelectedJid(null)}>
                <ChevronLeft size={14} />
                Back
              </Button>
              <span className="font-mono text-xs text-text-secondary">{selectedJid}</span>
            </div>

            {historyQ.isLoading ? (
              <LoadingSpinner className="py-12" label="Loading user history…" />
            ) : history ? (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {/* User info */}
                <div className="card p-4 space-y-3">
                  <h3 className="text-sm font-medium text-white mb-2">Profile</h3>
                  {[
                    ['JID', history.user?.jid],
                    ['Phone', history.user?.phone_number],
                    ['Display Name', history.user?.display_name],
                    ['Push Name', history.user?.push_name],
                    ['Business', history.user?.is_business ? 'Yes' : 'No'],
                    ['First Seen', fmtTs(history.user?.first_seen ?? null)],
                    ['Last Seen', fmtTs(history.user?.last_seen ?? null)],
                  ].map(([label, value]) => (
                    <div key={label} className="flex items-start gap-3">
                      <span className="text-text-muted text-xs w-28 flex-shrink-0">{label}</span>
                      <span className="font-mono text-xs text-text-primary break-all">{value || '—'}</span>
                    </div>
                  ))}
                </div>

                {/* Change timeline */}
                <div className="card p-4">
                  <h3 className="text-sm font-medium text-white mb-3">
                    Profile Changes ({history.profile_history.length})
                  </h3>
                  {history.profile_history.length === 0 ? (
                    <p className="text-text-muted text-sm">No profile changes recorded</p>
                  ) : (
                    <div className="space-y-2 max-h-64 overflow-y-auto">
                      {history.profile_history.map((entry) => (
                        <div
                          key={entry.id}
                          className="flex flex-col gap-1 py-2 border-b border-border/50 last:border-0"
                        >
                          <div className="flex items-center justify-between">
                            <span className="font-mono text-xs text-white">{entry.field_name}</span>
                            <span className="font-mono text-xs text-text-muted">{fmtTs(entry.changed_at)}</span>
                          </div>
                          <div className="flex items-center gap-2 text-xs font-mono">
                            <span className="text-status-down truncate max-w-[120px]">{entry.old_value || '(empty)'}</span>
                            <span className="text-text-muted">→</span>
                            <span className="text-status-up truncate max-w-[120px]">{entry.new_value || '(empty)'}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Memberships */}
                <div className="card p-4 lg:col-span-2">
                  <h3 className="text-sm font-medium text-white mb-3">
                    Group Memberships ({history.memberships.length})
                  </h3>
                  {history.memberships.length === 0 ? (
                    <p className="text-text-muted text-sm">No group memberships tracked</p>
                  ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 max-h-64 overflow-y-auto">
                      {history.memberships.map((m) => (
                        <div key={m.chat_jid} className="bg-bg-elevated border border-border/50 rounded p-2.5">
                          <p className="font-mono text-xs text-white truncate">{m.chat_jid}</p>
                          <div className="flex items-center justify-between mt-1.5 text-xs text-text-muted">
                            <span>{m.message_count} msgs</span>
                            <span>{new Date(m.last_seen).toLocaleDateString()}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          /* Search view */
          <div className="space-y-4">
            {/* Search input */}
            <div className="relative max-w-md">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                type="text"
                placeholder="Search JID, name, phone number…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="w-full pl-9 pr-4 py-2 bg-bg-surface border border-border rounded-md text-sm text-white placeholder-text-muted focus:outline-none focus:border-border-strong"
              />
            </div>

            {searchQ.isLoading ? (
              <LoadingSpinner className="py-12" label="Searching…" />
            ) : (
              <div className="space-y-1">
                {users.length === 0 && (
                  <div className="card p-8 text-center">
                    <User size={28} className="mx-auto mb-2 text-text-muted" />
                    <p className="text-text-muted text-sm">
                      {query.length >= 2 ? 'No users match your search' : 'Enter at least 2 characters to search'}
                    </p>
                  </div>
                )}
                {users.map((user) => (
                  <button
                    key={user.jid}
                    onClick={() => setSelectedJid(user.jid)}
                    className="w-full card p-3 text-left hover:border-border-strong hover:bg-accent-5 transition-all flex items-center gap-4"
                  >
                    <div className="w-8 h-8 rounded-full bg-bg-elevated border border-border flex items-center justify-center flex-shrink-0">
                      <User size={14} className="text-text-muted" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-white text-sm font-medium truncate">
                          {user.display_name || user.push_name || user.jid}
                        </span>
                        {user.is_business && (
                          <span className="text-status-running text-xs font-mono">BIZ</span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 mt-0.5 text-xs text-text-muted font-mono">
                        <span className="truncate">{user.jid}</span>
                        {user.phone_number && <span>{user.phone_number}</span>}
                      </div>
                    </div>
                    <div className="text-right flex-shrink-0">
                      <p className="text-text-muted text-xs">last seen</p>
                      <p className="font-mono text-xs text-text-secondary">{fmtTs(user.last_seen)}</p>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
