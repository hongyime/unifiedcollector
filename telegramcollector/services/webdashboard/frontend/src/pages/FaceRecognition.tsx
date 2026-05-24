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

interface Identity { id: number; topic_id: number; label: string | null; face_count: number; message_count: number; created_at: string; updated_at: string }
interface Correction { id: number; from_topic_id: number; to_topic_id: number; corrected_at: string; from_label: string | null; to_label: string | null }
interface FaceStats { identities: number; embeddings: number; processed_24h: number; processed_total: number; error: string | null }

const IDENTITY_COLS: Column<Identity>[] = [
  { key: 'topic_id', header: 'Topic ID', mono: true },
  { key: 'label', header: 'Label', render: v => v ? <span className="text-white font-medium">{String(v)}</span> : <span className="text-text-muted italic">unlabelled</span> },
  { key: 'face_count', header: 'Faces', mono: true, sortable: true },
  { key: 'message_count', header: 'Messages', mono: true, sortable: true },
  { key: 'updated_at', header: 'Updated', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

const CORRECTION_COLS: Column<Correction>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'from_label', header: 'From', render: (v, r) => <span className="font-mono text-xs text-status-down">{String(v ?? (r as unknown as Correction).from_topic_id)}</span> },
  { key: 'to_label', header: 'To', render: (v, r) => <span className="font-mono text-xs text-status-up">{String(v ?? (r as unknown as Correction).to_topic_id)}</span> },
  { key: 'corrected_at', header: 'At', render: v => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span> },
]

export default function FaceRecognition() {
  const qc = useQueryClient()
  const statsQ = useApiQuery<FaceStats>(['faces', 'stats'], '/api/faces/stats', { refetchInterval: 30_000 })
  const identitiesQ = useApiQuery<{ identities: Identity[]; error: string | null }>(['faces', 'identities'], '/api/faces/identities', { refetchInterval: 60_000 })
  const correctionsQ = useApiQuery<{ corrections: Correction[]; error: string | null }>(['faces', 'corrections'], '/api/faces/corrections')

  const stats = statsQ.data

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Face Recognition"
        subtitle="Identity tracking via Telegram hub topics"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['faces'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Identities" value={stats?.identities ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Embeddings" value={stats?.embeddings ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Processed (24h)" value={stats?.processed_24h ?? '—'} mono /></div>
        <div className="col-span-6 sm:col-span-3"><MetricCard label="Processed Total" value={stats?.processed_total ?? '—'} mono /></div>

        <div className="col-span-12 lg:col-span-8 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Identities</h2>
          </div>
          {identitiesQ.isLoading ? <LoadingSpinner className="py-12" label="Loading identities…" /> : (
            <DataTable columns={IDENTITY_COLS as unknown as Column<Record<string, unknown>>[]} data={(identitiesQ.data?.identities ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No identities yet" maxHeight="480px" />
          )}
        </div>

        <div className="col-span-12 lg:col-span-4 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Corrections</h2>
          </div>
          {correctionsQ.isLoading ? <LoadingSpinner className="py-12" label="Loading corrections…" /> : (
            <DataTable columns={CORRECTION_COLS as unknown as Column<Record<string, unknown>>[]} data={(correctionsQ.data?.corrections ?? []) as unknown as Record<string, unknown>[]} rowKey="id" emptyMessage="No corrections" maxHeight="480px" />
          )}
        </div>
      </div>
    </div>
  )
}
