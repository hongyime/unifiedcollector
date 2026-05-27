import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import StatusBadge from '../components/UI/StatusBadge'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import { useApiQuery } from '../hooks/useApi'

interface MediaFile {
  id: number
  raw_message_id: number | null
  message_id: string
  chat_jid: string
  mime_type: string | null
  file_size_bytes: number | null
  download_status: string
  downloaded_at: string | null
  expiry_at: string | null
  collected_at: string
}

interface ExpiringFile {
  id: number
  message_id: string
  chat_jid: string
  mime_type: string | null
  file_size_bytes: number | null
  download_status: string
  expiry_at: string | null
  by_id_path: string | null
}

function fmtBytes(b: number | null): string {
  if (b === null || b === 0) return '—'
  if (b < 1024) return `${b}B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)}KB`
  return `${(b / 1024 / 1024).toFixed(1)}MB`
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function timeUntil(iso: string | null): string {
  if (!iso) return '—'
  const diff = new Date(iso).getTime() - Date.now()
  if (diff < 0) return 'expired'
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)}GB`
}

const QUEUE_COLS: Column<MediaFile>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-16' },
  { key: 'message_id', header: 'Message ID', mono: true, truncate: true },
  { key: 'chat_jid', header: 'Chat', mono: true, truncate: true },
  { key: 'mime_type', header: 'MIME', mono: true },
  {
    key: 'file_size_bytes',
    header: 'Size',
    mono: true,
    render: (v) => fmtBytes(v as number | null),
  },
  {
    key: 'download_status',
    header: 'Status',
    render: (v) => <StatusBadge status={String(v || 'unknown')} />,
  },
  {
    key: 'collected_at',
    header: 'Collected',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

const EXPIRING_COLS: Column<ExpiringFile>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-16' },
  { key: 'message_id', header: 'Message ID', mono: true, truncate: true },
  { key: 'chat_jid', header: 'Chat', mono: true, truncate: true },
  { key: 'mime_type', header: 'MIME', mono: true },
  {
    key: 'file_size_bytes',
    header: 'Size',
    mono: true,
    render: (v) => fmtBytes(v as number | null),
  },
  {
    key: 'download_status',
    header: 'Status',
    render: (v) => <StatusBadge status={String(v || 'unknown')} />,
  },
  {
    key: 'expiry_at',
    header: 'Expires',
    render: (v) => {
      const str = timeUntil(v as string | null)
      const urgent = str !== '—' && str !== 'expired' && !str.includes('h')
      return (
        <span className={['font-mono text-xs', urgent ? 'text-status-down font-medium' : 'text-status-pending'].join(' ')}>
          {str}
        </span>
      )
    },
  },
]

export default function MediaArchival() {
  const qc = useQueryClient()

  const statsQ = useApiQuery<{
    total: number; downloaded: number; pending: number; failed: number;
    expiring_soon: number; total_size_bytes: number; queue_depth: number; error: string | null
  }>(['media', 'stats'], '/api/media/stats', { refetchInterval: 30_000 })

  const queueQ = useApiQuery<{ items: MediaFile[]; error: string | null }>(
    ['media', 'queue'], '/api/media/queue?limit=50'
  )
  const expiringQ = useApiQuery<{ items: ExpiringFile[]; error: string | null }>(
    ['media', 'expiring'], '/api/media/expiring?hours=2'
  )

  const stats = statsQ.data

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Media Archival"
        subtitle="Media download queue and expiry tracking"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['media'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">

        {/* Stats row */}
        <div className="col-span-12 sm:col-span-4 card p-4 flex flex-col gap-1">
          <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Queue Depth</span>
          <span className="text-2xl font-semibold font-mono text-white">{stats?.queue_depth ?? '—'}</span>
          <span className="text-text-muted text-xs">{stats?.pending ?? '—'} pending download</span>
        </div>
        <div className="col-span-12 sm:col-span-4 card p-4 flex flex-col gap-1">
          <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Storage Used</span>
          <span className="text-2xl font-semibold font-mono text-white">
            {stats ? fmtSize(stats.total_size_bytes) : '—'}
          </span>
          <span className="text-text-muted text-xs">{stats?.downloaded ?? '—'} downloaded / {stats?.total ?? '—'} total</span>
        </div>
        <div className="col-span-12 sm:col-span-4 card p-4 flex flex-col gap-1">
          <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Expiring Soon</span>
          <span className={['text-2xl font-semibold font-mono', stats?.expiring_soon ? 'text-status-down' : 'text-white'].join(' ')}>
            {stats?.expiring_soon ?? '—'}
          </span>
          <span className="text-text-muted text-xs">within 2 hours · {stats?.failed ?? '—'} failed</span>
        </div>

        {/* Pending queue — full width */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Pending Queue</h2>
          </div>
          {queueQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading queue…" />
          ) : (
            <DataTable
              columns={QUEUE_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(queueQ.data?.items ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No pending media items"
              maxHeight="400px"
            />
          )}
        </div>

        {/* Expiring media — full width */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Expiring Media</h2>
            {stats?.expiring_soon ? (
              <span className="px-1.5 py-0.5 bg-status-down/20 text-status-down text-xs rounded font-mono">
                {stats.expiring_soon} urgent
              </span>
            ) : null}
          </div>
          {expiringQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading expiring media…" />
          ) : (
            <DataTable
              columns={EXPIRING_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(expiringQ.data?.items ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No media expiring within 2 hours"
              maxHeight="400px"
            />
          )}
        </div>

      </div>
    </div>
  )
}
