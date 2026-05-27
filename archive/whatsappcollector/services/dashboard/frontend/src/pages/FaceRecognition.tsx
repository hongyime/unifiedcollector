import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import Button from '../components/UI/Button'
import { useApiQuery } from '../hooks/useApi'
import { User, ChevronLeft } from 'lucide-react'

interface Identity {
  id: string
  label: string
  occurrence_count: number
  first_seen: string
  last_seen: string
}

interface Embedding {
  id: number
  identity_id: string | null
  source_message_id: string
  source_chat_jid: string
  frame_index: number
  is_valid: boolean
  created_at: string
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

const EMBED_COLS: Column<Embedding>[] = [
  { key: 'id', header: 'ID', mono: true },
  { key: 'source_message_id', header: 'Message ID', mono: true, truncate: true },
  { key: 'source_chat_jid', header: 'Chat', mono: true, truncate: true },
  { key: 'frame_index', header: 'Frame', mono: true },
  {
    key: 'is_valid',
    header: 'Valid',
    render: (v) => (
      <span className={v ? 'text-status-up text-xs font-mono' : 'text-status-down text-xs font-mono'}>
        {v ? 'YES' : 'NO'}
      </span>
    ),
  },
  {
    key: 'created_at',
    header: 'Created',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

export default function FaceRecognition() {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [sortBy, setSortBy] = useState<'last_seen' | 'occurrence_count'>('last_seen')
  const qc = useQueryClient()

  const statsQ = useApiQuery<{
    identities: number; embeddings: number; processed_media: number;
    published_findings: number; unassigned_embeddings: number; error: string | null
  }>(['faces', 'stats'], '/api/faces/stats', { refetchInterval: 30_000 })

  const identitiesQ = useApiQuery<{ identities: Identity[]; error: string | null }>(
    ['faces', 'identities', sortBy], `/api/faces/identities?limit=50&sort=${sortBy}`
  )

  const embeddingsQ = useApiQuery<{ embeddings: Embedding[]; error: string | null }>(
    ['faces', 'embeddings', selectedId],
    selectedId ? `/api/faces/embeddings?identity_id=${selectedId}` : '/api/faces/embeddings',
    { enabled: true }
  )

  const stats = statsQ.data
  const identities = identitiesQ.data?.identities ?? []
  const selected = identities.find((id) => id.id === selectedId)

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Face Recognition"
        subtitle="Identity tracking and embedding management"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['faces'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">

        {/* Stats row */}
        <div className="col-span-12 sm:col-span-4">
          <MetricCard label="Identities" value={stats?.identities ?? '—'} mono />
        </div>
        <div className="col-span-6 sm:col-span-4">
          <MetricCard label="Embeddings" value={stats?.embeddings ?? '—'} mono />
        </div>
        <div className="col-span-6 sm:col-span-4">
          <MetricCard
            label="Processed Media"
            value={stats?.processed_media ?? '—'}
            mono
            sublabel={`${stats?.unassigned_embeddings ?? '—'} unassigned · ${stats?.published_findings ?? '—'} findings`}
          />
        </div>

        {/* Identity detail view — shown when a card is selected */}
        {selectedId && (
          <>
            <div className="col-span-12 flex items-center gap-3">
              <Button size="sm" variant="ghost" onClick={() => setSelectedId(null)}>
                <ChevronLeft size={14} />
                Back to identities
              </Button>
              {selected && (
                <span className="text-text-secondary text-sm">
                  {selected.label} — {selected.occurrence_count} occurrences
                </span>
              )}
            </div>
            <div className="col-span-12 card overflow-hidden">
              <div className="px-4 py-3 border-b border-border">
                <h2 className="text-text-muted text-xs uppercase tracking-wider">
                  Embeddings for {selected?.label ?? selectedId.slice(0, 8)}
                </h2>
              </div>
              {embeddingsQ.isLoading ? (
                <LoadingSpinner className="py-12" label="Loading embeddings…" />
              ) : (
                <DataTable
                  columns={EMBED_COLS as unknown as Column<Record<string, unknown>>[]}
                  data={(embeddingsQ.data?.embeddings ?? []) as unknown as Record<string, unknown>[]}
                  rowKey="id"
                  emptyMessage="No embeddings found"
                  maxHeight="400px"
                />
              )}
            </div>
          </>
        )}

        {/* Identity gallery — full width */}
        {!selectedId && (
          <div className="col-span-12 card p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-text-muted text-xs uppercase tracking-wider">Identity Gallery</h2>
              <div className="flex items-center gap-2">
                <span className="text-text-muted text-xs">Sort by:</span>
                <button
                  onClick={() => setSortBy('last_seen')}
                  className={[
                    'px-2.5 py-1 text-xs rounded font-mono transition-colors',
                    sortBy === 'last_seen'
                      ? 'bg-white text-black'
                      : 'bg-transparent text-text-secondary border border-border hover:text-white',
                  ].join(' ')}
                >
                  Last Seen
                </button>
                <button
                  onClick={() => setSortBy('occurrence_count')}
                  className={[
                    'px-2.5 py-1 text-xs rounded font-mono transition-colors',
                    sortBy === 'occurrence_count'
                      ? 'bg-white text-black'
                      : 'bg-transparent text-text-secondary border border-border hover:text-white',
                  ].join(' ')}
                >
                  Occurrences
                </button>
              </div>
            </div>
            {identitiesQ.isLoading ? (
              <LoadingSpinner className="py-12" label="Loading identities…" />
            ) : identities.length === 0 ? (
              <div className="p-12 text-center">
                <User size={32} className="mx-auto mb-3 text-text-muted" />
                <p className="text-text-muted text-sm">No identities found</p>
              </div>
            ) : (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
                {identities.map((identity) => (
                  <button
                    key={identity.id}
                    onClick={() => setSelectedId(identity.id)}
                    className="card p-4 text-left hover:border-border-strong hover:bg-accent-5 transition-all"
                  >
                    <div className="flex items-center gap-2 mb-3">
                      <div className="w-8 h-8 rounded-full bg-bg-elevated border border-border flex items-center justify-center flex-shrink-0">
                        <User size={14} className="text-text-muted" />
                      </div>
                      <span className="font-mono text-xs text-white font-medium truncate">
                        {identity.label}
                      </span>
                    </div>
                    <div className="space-y-1">
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-text-muted">occurrences</span>
                        <span className="font-mono text-white">{identity.occurrence_count}</span>
                      </div>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-text-muted">last seen</span>
                        <span className="font-mono text-text-secondary">
                          {new Date(identity.last_seen).toLocaleDateString([], { month: 'short', day: 'numeric' })}
                        </span>
                      </div>
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
