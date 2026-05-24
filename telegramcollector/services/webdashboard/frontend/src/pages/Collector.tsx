import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import StatusBadge from '../components/UI/StatusBadge'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import { useApiQuery } from '../hooks/useApi'

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

interface Account { id: number; phone_number: string; display_name: string | null; status: string; last_active: string | null; created_at: string }
interface BackfillJob { id: number; account_id: number; chat_id: string; status: string; messages_done: number; error: string | null; created_at: string; updated_at: string }
interface Cursor { service_name: string; last_message_id: number; updated_at: string }
interface RawMessage { id: number; chat_id: string; message_id: string; sender_id: string | null; message_type: string | null; has_media: boolean; collected_at: string }
interface CollectorStats { messages_total: number; messages_5m: number; messages_1h: number; accounts: number; media_24h: number; chats: number; error: string | null }

const ACCOUNT_COLS: Column<Account>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'phone_number', header: 'Phone', mono: true },
  { key: 'display_name', header: 'Name' },
  { key: 'status', header: 'Status', render: v => <StatusBadge status={String(v || 'unknown')} pulse={v === 'active'} /> },
  { key: 'last_active', header: 'Last Active', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
  { key: 'created_at', header: 'Added', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

const BACKFILL_COLS: Column<BackfillJob>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'chat_id', header: 'Chat ID', mono: true, truncate: true },
  { key: 'status', header: 'Status', render: v => <StatusBadge status={String(v || 'unknown')} /> },
  { key: 'messages_done', header: 'Done', mono: true },
  { key: 'error', header: 'Error', truncate: true, render: v => v ? <span className="text-status-down text-xs font-mono">{String(v).slice(0, 40)}</span> : <span className="text-text-muted">—</span> },
  { key: 'updated_at', header: 'Updated', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

const CURSOR_COLS: Column<Cursor>[] = [
  { key: 'service_name', header: 'Service', mono: true },
  { key: 'last_message_id', header: 'Last Msg ID', mono: true, sortable: true },
  { key: 'updated_at', header: 'Updated', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

const MESSAGE_COLS: Column<RawMessage>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-16' },
  { key: 'chat_id', header: 'Chat', mono: true, truncate: true },
  { key: 'message_id', header: 'Msg ID', mono: true },
  { key: 'message_type', header: 'Type', mono: true },
  { key: 'has_media', header: 'Media', render: v => v ? <span className="text-status-running text-xs font-mono">YES</span> : <span className="text-text-muted text-xs font-mono">—</span> },
  { key: 'collected_at', header: 'Collected', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

export default function Collector() {
  const qc = useQueryClient()
  const statsQ = useApiQuery<CollectorStats>(['collector', 'stats'], '/api/collector/stats', { refetchInterval: 15_000 })
  const accountsQ = useApiQuery<{ accounts: Account[]; error: string | null }>(['collector', 'accounts'], '/api/collector/accounts', { refetchInterval: 30_000 })
  const backfillQ = useApiQuery<{ jobs: BackfillJob[]; error: string | null }>(['collector', 'backfill'], '/api/collector/backfill')
  const cursorsQ = useApiQuery<{ cursors: Cursor[]; error: string | null }>(['collector', 'cursors'], '/api/collector/cursors', { refetchInterval: 15_000 })
  const messagesQ = useApiQuery<{ messages: RawMessage[]; error: string | null }>(['collector', 'messages'], '/api/collector/recent-messages', { refetchInterval: 10_000 })

  const stats = statsQ.data

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Collector"
        subtitle="Telegram message collection — accounts, messages, backfill"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['collector'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Total Messages" value={stats?.messages_total ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Last 5 Min" value={stats?.messages_5m ?? '—'} mono trend={stats?.messages_5m && stats.messages_5m > 0 ? 'up' : 'neutral'} /></div>
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Last Hour" value={stats?.messages_1h ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Active Accounts" value={stats?.accounts ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Media (24h)" value={stats?.media_24h ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-4 lg:col-span-2"><MetricCard label="Chats" value={stats?.chats ?? '—'} mono /></div>

        {/* Accounts */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Telegram Accounts</h2>
          </div>
          {accountsQ.isLoading ? <LoadingSpinner className="py-12" label="Loading accounts…" /> : (
            <DataTable columns={ACCOUNT_COLS as unknown as Column<Record<string, unknown>>[]} data={(accountsQ.data?.accounts ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No accounts registered" />
          )}
        </div>

        {/* Backfill + Cursors */}
        <div className="col-span-12 md:col-span-6 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Backfill Jobs</h2>
          </div>
          {backfillQ.isLoading ? <LoadingSpinner className="py-12" label="Loading jobs…" /> : (
            <DataTable columns={BACKFILL_COLS as unknown as Column<Record<string, unknown>>[]} data={(backfillQ.data?.jobs ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No backfill jobs" maxHeight="320px" />
          )}
        </div>
        <div className="col-span-12 md:col-span-6 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Service Cursors</h2>
          </div>
          {cursorsQ.isLoading ? <LoadingSpinner className="py-12" label="Loading cursors…" /> : (
            <DataTable columns={CURSOR_COLS as unknown as Column<Record<string, unknown>>[]} data={(cursorsQ.data?.cursors ?? []) as unknown as Record<string, unknown>[]} rowKey="service_name" emptyMessage="No cursors" maxHeight="320px" />
          )}
        </div>

        {/* Recent messages */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Recent Messages</h2>
          </div>
          {messagesQ.isLoading ? <LoadingSpinner className="py-12" label="Loading messages…" /> : (
            <DataTable columns={MESSAGE_COLS as unknown as Column<Record<string, unknown>>[]} data={(messagesQ.data?.messages ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No messages yet" maxHeight="400px" />
          )}
        </div>
      </div>
    </div>
  )
}
