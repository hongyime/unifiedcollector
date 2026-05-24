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

interface DiscoveredLink { id: number; link_type: string | null; link_value: string; source_chat_id: string | null; resolved: boolean; discovered_at: string }
interface JoinQueueItem { id: number; peer_identifier: string; status: string; queued_at: string }
interface LinkStats { total_links: number; resolved: number; join_queue: number; error: string | null }

const LINK_COLS: Column<DiscoveredLink>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'link_type', header: 'Type', mono: true },
  { key: 'link_value', header: 'Value', mono: true, truncate: true },
  { key: 'source_chat_id', header: 'Source Chat', mono: true },
  { key: 'resolved', header: 'Resolved', render: v => <StatusBadge status={v ? 'resolved' : 'pending'} /> },
  { key: 'discovered_at', header: 'Discovered', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

const QUEUE_COLS: Column<JoinQueueItem>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'peer_identifier', header: 'Peer', mono: true },
  { key: 'status', header: 'Status', render: v => <StatusBadge status={String(v || 'unknown')} /> },
  { key: 'queued_at', header: 'Queued', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

export default function LinkDiscovery() {
  const qc = useQueryClient()
  const statsQ = useApiQuery<LinkStats>(['links', 'stats'], '/api/links/stats', { refetchInterval: 30_000 })
  const linksQ = useApiQuery<{ links: DiscoveredLink[]; error: string | null }>(['links', 'list'], '/api/links/list', { refetchInterval: 30_000 })
  const queueQ = useApiQuery<{ queue: JoinQueueItem[]; error: string | null }>(['links', 'queue'], '/api/links/queue', { refetchInterval: 15_000 })

  const stats = statsQ.data

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Link Discovery"
        subtitle="Telegram invite links and group join queue"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['links'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">
        <div className="col-span-6 sm:col-span-4"><MetricCard label="Total Links" value={stats?.total_links ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-4"><MetricCard label="Resolved" value={stats?.resolved ?? '—'} mono /></div>
        <div className="col-span-12 sm:col-span-4"><MetricCard label="Join Queue" value={stats?.join_queue ?? '—'} mono trend={stats?.join_queue && stats.join_queue > 0 ? 'up' : 'neutral'} /></div>

        <div className="col-span-12 lg:col-span-8 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Discovered Links</h2>
          </div>
          {linksQ.isLoading ? <LoadingSpinner className="py-12" label="Loading links…" /> : (
            <DataTable columns={LINK_COLS as unknown as Column<Record<string, unknown>>[]} data={(linksQ.data?.links ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No links discovered yet" maxHeight="480px" />
          )}
        </div>

        <div className="col-span-12 lg:col-span-4 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Join Queue</h2>
          </div>
          {queueQ.isLoading ? <LoadingSpinner className="py-12" label="Loading queue…" /> : (
            <DataTable columns={QUEUE_COLS as unknown as Column<Record<string, unknown>>[]} data={(queueQ.data?.queue ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="Queue empty" maxHeight="480px" />
          )}
        </div>
      </div>
    </div>
  )
}
