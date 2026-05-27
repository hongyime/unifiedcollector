import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import StatusBadge from '../components/UI/StatusBadge'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import Button from '../components/UI/Button'
import { useApiQuery, useApiMutation, apiFetch } from '../hooks/useApi'
import { CheckSquare } from 'lucide-react'

interface QueueItem {
  id: number
  link: string
  session_name: string | null
  status: string
  source: string | null
  added_at: string
  processed_at: string | null
  error: string | null
}

interface Session {
  id: number
  session_name: string
  status: string
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export default function LinkDiscovery() {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkSession, setBulkSession] = useState('')
  const qc = useQueryClient()

  const statsQ = useApiQuery<{
    total_discovered: number; queued_joins: number; unassigned: number;
    processed: number; failed: number; error: string | null
  }>(['links', 'stats'], '/api/links/stats', { refetchInterval: 30_000 })

  const queueQ = useApiQuery<{ items: QueueItem[]; error: string | null }>(
    ['links', 'queue'], '/api/links/queue', { refetchInterval: 30_000 }
  )

  const sessionsQ = useApiQuery<{ sessions: Session[]; error: string | null }>(
    ['links', 'sessions'], '/api/links/sessions'
  )

  const approveMut = useApiMutation(
    ({ id, session_name }: { id: number; session_name: string }) =>
      apiFetch(`/api/links/queue/${id}/approve`, {
        method: 'POST',
        body: JSON.stringify({ session_name }),
      }),
    [['links', 'queue'], ['links', 'stats']]
  )

  const bulkMut = useApiMutation(
    ({ ids, session_name }: { ids: number[]; session_name: string }) =>
      apiFetch('/api/links/bulk-assign', {
        method: 'POST',
        body: JSON.stringify({ ids, session_name }),
      }),
    [['links', 'queue'], ['links', 'stats']]
  )

  const stats = statsQ.data
  const items = queueQ.data?.items ?? []
  const sessions = sessionsQ.data?.sessions ?? []

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAll = () => {
    if (selectedIds.size === items.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(items.map((i) => i.id)))
    }
  }

  const QUEUE_COLS: Column<QueueItem>[] = [
    {
      key: 'id',
      header: '',
      width: 'w-8',
      render: (_v, row) => (
        <input
          type="checkbox"
          checked={selectedIds.has(row.id)}
          onChange={() => toggleSelect(row.id)}
          className="accent-white cursor-pointer"
          onClick={(e) => e.stopPropagation()}
        />
      ),
    },
    { key: 'link', header: 'Link', mono: true, truncate: true },
    {
      key: 'status',
      header: 'Status',
      render: (v) => <StatusBadge status={String(v || 'unknown')} />,
    },
    { key: 'source', header: 'Source', mono: true },
    {
      key: 'session_name',
      header: 'Session',
      render: (v, row) => {
        if (row.status !== 'pending') {
          return <span className="font-mono text-xs text-text-secondary">{String(v || '—')}</span>
        }
        return (
          <div className="flex items-center gap-2">
            <select
              className="bg-bg-elevated border border-border text-xs text-white rounded px-2 py-1 font-mono"
              defaultValue={String(v || '')}
              onChange={(e) => {
                if (e.target.value) {
                  approveMut.mutate({ id: row.id, session_name: e.target.value })
                }
              }}
            >
              <option value="">— assign —</option>
              {sessions.map((s) => (
                <option key={s.session_name} value={s.session_name}>
                  {s.session_name}
                </option>
              ))}
            </select>
          </div>
        )
      },
    },
    {
      key: 'added_at',
      header: 'Added',
      render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
    },
    {
      key: 'error',
      header: 'Error',
      truncate: true,
      render: (v) => v ? (
        <span className="text-status-down text-xs font-mono">{String(v).slice(0, 50)}</span>
      ) : null,
    },
  ]

  return (
    <div className="flex flex-col h-full">
      <Header
        title="Link Discovery"
        subtitle="Group link queue management and join control"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['links'] })}
        isRefreshing={statsQ.isFetching}
      />
      <div className="flex-1 p-6 space-y-5 overflow-auto">
        {/* Metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <MetricCard label="Total Discovered" value={stats?.total_discovered ?? '—'} mono />
          <MetricCard label="Queued Joins" value={stats?.queued_joins ?? '—'} mono />
          <MetricCard
            label="Unassigned"
            value={stats?.unassigned ?? '—'}
            mono
            trend={stats?.unassigned ? 'down' : 'neutral'}
            sublabel="need session"
          />
          <MetricCard label="Processed" value={stats?.processed ?? '—'} mono trend="up" />
          <MetricCard label="Failed" value={stats?.failed ?? '—'} mono trend={stats?.failed ? 'down' : 'neutral'} />
        </div>

        {/* Bulk assign toolbar */}
        {selectedIds.size > 0 && (
          <div className="card p-3 flex items-center gap-3 border-white/20">
            <CheckSquare size={14} className="text-white" />
            <span className="text-sm text-white">{selectedIds.size} selected</span>
            <select
              value={bulkSession}
              onChange={(e) => setBulkSession(e.target.value)}
              className="bg-bg-elevated border border-border text-sm text-white rounded px-3 py-1.5 font-mono"
            >
              <option value="">— choose session —</option>
              {sessions.map((s) => (
                <option key={s.session_name} value={s.session_name}>
                  {s.session_name}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="primary"
              disabled={!bulkSession}
              loading={bulkMut.isPending}
              onClick={() => {
                if (bulkSession) {
                  bulkMut.mutate(
                    { ids: Array.from(selectedIds), session_name: bulkSession },
                    {
                      onSuccess: () => {
                        setSelectedIds(new Set())
                        setBulkSession('')
                      },
                    }
                  )
                }
              }}
            >
              Assign All
            </Button>
            <button
              className="text-text-muted text-xs hover:text-white transition-colors"
              onClick={() => setSelectedIds(new Set())}
            >
              Clear selection
            </button>
          </div>
        )}

        {/* Queue table */}
        <div className="card overflow-hidden">
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <h3 className="text-sm font-medium text-white">Join Queue</h3>
            <button
              onClick={selectAll}
              className="text-xs text-text-secondary hover:text-white transition-colors"
            >
              {selectedIds.size === items.length && items.length > 0 ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          {queueQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading queue…" />
          ) : (
            <DataTable
              columns={QUEUE_COLS as unknown as Column<Record<string, unknown>>[]}
              data={items as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No pending links in queue"
              maxHeight="500px"
            />
          )}
        </div>
      </div>
    </div>
  )
}
