import { useQueryClient } from '@tanstack/react-query'
import { useHealthWS } from '../hooks/useHealthWS'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import StatusBadge from '../components/UI/StatusBadge'
import DataTable, { Column } from '../components/UI/DataTable'
import Button from '../components/UI/Button'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import { useApiQuery, useApiMutation, apiFetch } from '../hooks/useApi'

interface QrData { status: string; qr: string | null; error: string | null }

function SessionQrCard({ sessionName }: { sessionName: string }) {
  const qrQ = useApiQuery<QrData>(
    ['qr', sessionName], `/api/collector/sessions/${encodeURIComponent(sessionName)}/qr`,
    { refetchInterval: 8_000 }
  )
  const d = qrQ.data
  const isConnected = d?.status === 'connected'
  const isScanned = d?.status === 'scanned'

  return (
    <div className="card p-4 flex flex-col items-center gap-3 min-h-[280px] justify-center">
      <div className="flex items-center justify-between w-full">
        <span className="font-mono text-sm text-white">{sessionName}</span>
        <StatusBadge status={isConnected ? 'active' : isScanned ? 'pending' : 'down'} pulse={isConnected} />
      </div>
      {isConnected ? (
        <div className="flex flex-col items-center gap-2 py-4">
          <div className="w-12 h-12 rounded-full bg-status-up/10 flex items-center justify-center">
            <span className="text-status-up text-2xl">✓</span>
          </div>
          <span className="text-status-up text-sm font-mono">Connected</span>
        </div>
      ) : isScanned ? (
        <div className="flex flex-col items-center gap-2 py-4">
          <div className="w-5 h-5 rounded-full border-2 border-status-pending border-t-transparent animate-spin" />
          <span className="text-status-pending text-xs font-mono">QR scanned — logging in…</span>
        </div>
      ) : d?.qr ? (
        <>
          <img
            src={`data:image/png;base64,${d.qr}`}
            alt={`QR for ${sessionName}`}
            className="w-48 h-48 rounded border border-border bg-white p-1"
          />
          <p className="text-text-muted text-xs text-center">Scan with WhatsApp<br />Settings → Linked Devices → Link a Device</p>
        </>
      ) : (
        <div className="flex flex-col items-center gap-2 py-4">
          <div className="w-4 h-4 rounded-full border-2 border-white/20 border-t-white/60 animate-spin" />
          <span className="text-text-muted text-xs font-mono">Waiting for QR…</span>
        </div>
      )}
    </div>
  )
}

interface Session {
  id: number
  session_name: string
  phone_jid: string | null
  display_name: string | null
  status: string
  last_connected: string | null
  cooldown_until: string | null
  created_at: string
}

interface BackfillJob {
  id: number
  session_name: string
  chat_jid: string
  status: string
  oldest_msg_ts: number | null
  messages_done: number
  cutoff_date: string | null
  created_at: string
  updated_at: string
}

interface Cursor {
  service_name: string
  last_message_id: number
  updated_at: string
}

interface RawMessage {
  id: number
  message_id: string
  chat_jid: string
  chat_type: string | null
  sender_jid: string | null
  session_name: string
  message_type: string | null
  body: string | null
  has_media: boolean
  is_forwarded: boolean
  is_deleted: boolean
  collected_at: string
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

const SESSION_COLS: Column<Session>[] = [
  { key: 'session_name', header: 'Session', mono: true },
  { key: 'phone_jid', header: 'Phone JID', mono: true, truncate: true },
  { key: 'display_name', header: 'Display Name' },
  {
    key: 'status',
    header: 'Status',
    render: (v) => <StatusBadge status={String(v || 'unknown')} />,
  },
  {
    key: 'last_connected',
    header: 'Last Connected',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
  {
    key: 'cooldown_until',
    header: 'Cooldown Until',
    render: (v) => v ? (
      <span className="font-mono text-xs text-status-pending">{fmtTs(v as string)}</span>
    ) : <span className="text-text-muted">—</span>,
  },
]

const BACKFILL_COLS: Column<BackfillJob>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'session_name', header: 'Session', mono: true },
  { key: 'chat_jid', header: 'Chat JID', mono: true, truncate: true },
  { key: 'status', header: 'Status', render: (v) => <StatusBadge status={String(v || 'unknown')} /> },
  { key: 'messages_done', header: 'Done', mono: true },
  {
    key: 'created_at',
    header: 'Created',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

const CURSOR_COLS: Column<Cursor>[] = [
  { key: 'service_name', header: 'Service', mono: true },
  { key: 'last_message_id', header: 'Last Msg ID', mono: true, sortable: true },
  {
    key: 'updated_at',
    header: 'Updated',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

const MESSAGE_COLS: Column<RawMessage>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-16' },
  { key: 'session_name', header: 'Session', mono: true },
  { key: 'chat_jid', header: 'Chat', mono: true, truncate: true },
  { key: 'message_type', header: 'Type', mono: true },
  {
    key: 'body',
    header: 'Body',
    truncate: true,
    render: (v) => v ? <span className="text-text-secondary">{String(v).slice(0, 60)}</span> : <span className="text-text-muted">—</span>,
  },
  {
    key: 'has_media',
    header: 'Media',
    render: (v) => v ? <span className="text-status-running text-xs font-mono">YES</span> : <span className="text-text-muted text-xs font-mono">—</span>,
  },
  {
    key: 'collected_at',
    header: 'Collected',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

export default function Collector() {
  const qc = useQueryClient()
  const { services: healthServices } = useHealthWS()
  // Derive session names from wa-client health probes: "wa-client-session_1" → "session_1"
  const waClientNames = healthServices
    .filter(s => s.service.startsWith('wa-client-'))
    .map(s => s.service.replace('wa-client-', ''))
  const sessionNames = waClientNames.length > 0 ? waClientNames : ['session_1', 'session_2']

  const statsQ = useApiQuery<{ raw_messages: number; users: number; chats: number; media_messages: number; messages_last_24h: number; error: string | null }>(
    ['collector', 'stats'], '/api/collector/stats', { refetchInterval: 30_000 }
  )
  const sessionsQ = useApiQuery<{ sessions: Session[]; error: string | null }>(
    ['collector', 'sessions'], '/api/collector/sessions', { refetchInterval: 30_000 }
  )
  const backfillQ = useApiQuery<{ jobs: BackfillJob[]; error: string | null }>(
    ['collector', 'backfill'], '/api/collector/backfill'
  )
  const dlqQ = useApiQuery<{ depth: number; queues?: number; error: string | null }>(
    ['collector', 'dlq'], '/api/collector/dlq-depth', { refetchInterval: 60_000 }
  )
  const cursorsQ = useApiQuery<{ cursors: Cursor[]; error: string | null }>(
    ['collector', 'cursors'], '/api/collector/cursors', { refetchInterval: 30_000 }
  )
  const messagesQ = useApiQuery<{ messages: RawMessage[]; error: string | null }>(
    ['collector', 'messages'], '/api/collector/recent-messages', { refetchInterval: 15_000 }
  )

  const backfillMut = useApiMutation(
    (name: string) => apiFetch(`/api/collector/sessions/${encodeURIComponent(name)}/request-backfill`, { method: 'POST' }),
    [['collector', 'backfill']]
  )

  const stats = statsQ.data
  const isRefreshing = statsQ.isFetching || sessionsQ.isFetching

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Collector"
        subtitle="WhatsApp message collection and session management"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['collector'] })}
        isRefreshing={isRefreshing}
      />
      <div className="p-6 grid grid-cols-12 gap-3">

        {/* Stats row */}
        <div className="col-span-6 sm:col-span-3">
          <MetricCard label="Messages" value={stats?.raw_messages ?? '—'} mono />
        </div>
        <div className="col-span-6 sm:col-span-3">
          <MetricCard label="Users" value={stats?.users ?? '—'} mono />
        </div>
        <div className="col-span-6 sm:col-span-3">
          <MetricCard label="Chats" value={stats?.chats ?? '—'} mono />
        </div>
        <div className="col-span-6 sm:col-span-3">
          <MetricCard
            label="DLQ"
            value={dlqQ.data?.depth ?? '—'}
            mono
            trend={dlqQ.data?.depth && dlqQ.data.depth > 0 ? 'down' : 'neutral'}
          />
        </div>

        {/* QR pairing cards — one per WA client session */}
        {sessionNames.map(name => (
          <div key={name} className="col-span-12 sm:col-span-6 lg:col-span-4 xl:col-span-3">
            <SessionQrCard sessionName={name} />
          </div>
        ))}

        {/* Sessions table — full width */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Sessions</h2>
          </div>
          {sessionsQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading sessions…" />
          ) : (
            <DataTable
              columns={[
                ...SESSION_COLS,
                {
                  key: 'session_name' as keyof Session,
                  header: 'Actions',
                  render: (_v, row) => (
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={backfillMut.isPending}
                      onClick={() => backfillMut.mutate(row.session_name)}
                    >
                      Request Backfill
                    </Button>
                  ),
                },
              ]}
              data={(sessionsQ.data?.sessions ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No sessions configured"
            />
          )}
        </div>

        {/* Backfill jobs — left 6 */}
        <div className="col-span-12 md:col-span-6 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Backfill Jobs</h2>
          </div>
          {backfillQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading backfill jobs…" />
          ) : (
            <DataTable
              columns={BACKFILL_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(backfillQ.data?.jobs ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No backfill jobs"
              maxHeight="320px"
            />
          )}
        </div>

        {/* Service cursors — right 6 */}
        <div className="col-span-12 md:col-span-6 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Service Cursors</h2>
          </div>
          {cursorsQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading cursors…" />
          ) : (
            <DataTable
              columns={CURSOR_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(cursorsQ.data?.cursors ?? []) as unknown as Record<string, unknown>[]}
              rowKey="service_name"
              emptyMessage="No service cursors registered"
              maxHeight="320px"
            />
          )}
        </div>

        {/* Recent messages — full width */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Recent Messages</h2>
            <div className="flex items-center gap-3 text-xs font-mono text-text-muted">
              <span>Last 24h: {stats?.messages_last_24h ?? '—'}</span>
              <span>Media: {stats?.media_messages ?? '—'}</span>
            </div>
          </div>
          {messagesQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading messages…" />
          ) : (
            <DataTable
              columns={MESSAGE_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(messagesQ.data?.messages ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No recent messages"
              maxHeight="400px"
            />
          )}
        </div>

        {/* DLQ error notice if present */}
        {dlqQ.data?.error && (
          <div className="col-span-12 card p-4 border-status-down/30">
            <p className="text-status-down text-sm font-mono">{dlqQ.data.error}</p>
            <p className="text-text-muted text-xs mt-1">
              RabbitMQ management API may be unavailable or credentials incorrect
            </p>
          </div>
        )}

      </div>
    </div>
  )
}
