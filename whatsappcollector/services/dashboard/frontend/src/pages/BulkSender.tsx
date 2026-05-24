import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import MetricCard from '../components/UI/MetricCard'
import StatusBadge from '../components/UI/StatusBadge'
import DataTable, { Column } from '../components/UI/DataTable'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import Button from '../components/UI/Button'
import { useApiQuery, useApiMutation, apiFetch } from '../hooks/useApi'
import { Plus, X } from 'lucide-react'

interface SendJob {
  id: number
  session_name: string
  mode: string
  source_type: string
  source_path: string | null
  status: string
  operator_confirmed: boolean
  total_files: number
  sent_count: number
  created_at: string
  updated_at: string
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

const JOB_COLS: Column<SendJob>[] = [
  { key: 'id', header: 'ID', mono: true, width: 'w-12' },
  { key: 'session_name', header: 'Session', mono: true },
  { key: 'mode', header: 'Mode', mono: true },
  {
    key: 'source_path',
    header: 'Source',
    mono: true,
    truncate: true,
    render: (v) => v ? (
      <span className="font-mono text-xs text-text-secondary">{String(v)}</span>
    ) : <span className="text-text-muted">—</span>,
  },
  {
    key: 'status',
    header: 'Status',
    render: (v) => <StatusBadge status={String(v || 'unknown')} />,
  },
  {
    key: 'sent_count',
    header: 'Progress',
    render: (v, row) => (
      <span className="font-mono text-xs">
        {String(v)} / {row.total_files || '?'}
      </span>
    ),
  },
  {
    key: 'created_at',
    header: 'Created',
    render: (v) => <span className="font-mono text-xs text-text-secondary">{fmtTs(v as string)}</span>,
  },
]

interface CreateJobForm {
  session_name: string
  mode: string
  source_type: string
  source_path: string
  targets: string[]
  targetInput: string
}

export default function BulkSender() {
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<CreateJobForm>({
    session_name: '',
    mode: 'broadcast',
    source_type: 'path',
    source_path: '',
    targets: [],
    targetInput: '',
  })
  const qc = useQueryClient()

  const statsQ = useApiQuery<{
    pending: number; running: number; completed: number; failed: number;
    total_sent: number; error: string | null
  }>(['bulk', 'stats'], '/api/bulk/stats', { refetchInterval: 30_000 })

  const jobsQ = useApiQuery<{ jobs: SendJob[]; error: string | null }>(
    ['bulk', 'jobs'], '/api/bulk/jobs?limit=25', { refetchInterval: 30_000 }
  )

  const sessionsQ = useApiQuery<{ sessions: { session_name: string }[]; error: string | null }>(
    ['links', 'sessions'], '/api/links/sessions'
  )

  const createMut = useApiMutation(
    (body: Omit<CreateJobForm, 'targetInput'>) =>
      apiFetch('/api/bulk/jobs', {
        method: 'POST',
        body: JSON.stringify({
          session_name: body.session_name,
          mode: body.mode,
          source_type: body.source_type,
          source_path: body.source_path || null,
          targets: body.targets,
        }),
      }),
    [['bulk', 'jobs'], ['bulk', 'stats']]
  )

  const stats = statsQ.data
  const sessions = sessionsQ.data?.sessions ?? []

  const addTarget = () => {
    const t = form.targetInput.trim()
    if (t && !form.targets.includes(t)) {
      setForm((f) => ({ ...f, targets: [...f.targets, t], targetInput: '' }))
    }
  }

  const removeTarget = (t: string) => {
    setForm((f) => ({ ...f, targets: f.targets.filter((x) => x !== t) }))
  }

  const submitJob = () => {
    if (!form.session_name) return
    createMut.mutate(form, {
      onSuccess: () => {
        setShowForm(false)
        setForm({
          session_name: '',
          mode: 'broadcast',
          source_type: 'path',
          source_path: '',
          targets: [],
          targetInput: '',
        })
      },
    })
  }

  return (
    <div className="flex flex-col h-full">
      <Header
        title="Bulk Sender"
        subtitle="Batch message sending job management"
        lastUpdated={statsQ.dataUpdatedAt ? new Date(statsQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['bulk'] })}
        isRefreshing={statsQ.isFetching}
        actions={
          <Button size="sm" variant="primary" onClick={() => setShowForm((v) => !v)}>
            <Plus size={13} />
            New Job
          </Button>
        }
      />
      <div className="flex-1 p-6 space-y-5 overflow-auto">
        {/* Metrics */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <MetricCard label="Pending" value={stats?.pending ?? '—'} mono />
          <MetricCard label="Running" value={stats?.running ?? '—'} mono trend={stats?.running ? 'up' : 'neutral'} />
          <MetricCard label="Completed" value={stats?.completed ?? '—'} mono trend="up" />
          <MetricCard label="Failed" value={stats?.failed ?? '—'} mono trend={stats?.failed ? 'down' : 'neutral'} />
          <MetricCard label="Total Sent" value={stats?.total_sent ?? '—'} mono />
        </div>

        {/* Create job form */}
        {showForm && (
          <div className="card p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-white">Create Send Job</h3>
              <button onClick={() => setShowForm(false)}>
                <X size={14} className="text-text-muted hover:text-white" />
              </button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs text-text-muted mb-1.5">Session</label>
                <select
                  value={form.session_name}
                  onChange={(e) => setForm((f) => ({ ...f, session_name: e.target.value }))}
                  className="w-full bg-bg-elevated border border-border text-sm text-white rounded px-3 py-2 font-mono"
                >
                  <option value="">— select session —</option>
                  {sessions.map((s) => (
                    <option key={s.session_name} value={s.session_name}>
                      {s.session_name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs text-text-muted mb-1.5">Mode</label>
                <select
                  value={form.mode}
                  onChange={(e) => setForm((f) => ({ ...f, mode: e.target.value }))}
                  className="w-full bg-bg-elevated border border-border text-sm text-white rounded px-3 py-2 font-mono"
                >
                  <option value="broadcast">broadcast</option>
                  <option value="sequential">sequential</option>
                </select>
              </div>

              <div>
                <label className="block text-xs text-text-muted mb-1.5">Source Type</label>
                <select
                  value={form.source_type}
                  onChange={(e) => setForm((f) => ({ ...f, source_type: e.target.value }))}
                  className="w-full bg-bg-elevated border border-border text-sm text-white rounded px-3 py-2 font-mono"
                >
                  <option value="path">path</option>
                  <option value="query">query</option>
                </select>
              </div>

              <div>
                <label className="block text-xs text-text-muted mb-1.5">Source Path</label>
                <input
                  type="text"
                  value={form.source_path}
                  onChange={(e) => setForm((f) => ({ ...f, source_path: e.target.value }))}
                  placeholder="/data/media/..."
                  className="w-full bg-bg-elevated border border-border text-sm text-white rounded px-3 py-2 font-mono placeholder-text-muted focus:outline-none focus:border-border-strong"
                />
              </div>
            </div>

            {/* Targets */}
            <div>
              <label className="block text-xs text-text-muted mb-1.5">
                Targets ({form.targets.length})
              </label>
              <div className="flex gap-2 mb-2">
                <input
                  type="text"
                  value={form.targetInput}
                  onChange={(e) => setForm((f) => ({ ...f, targetInput: e.target.value }))}
                  onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTarget())}
                  placeholder="1234567890@s.whatsapp.net"
                  className="flex-1 bg-bg-elevated border border-border text-sm text-white rounded px-3 py-2 font-mono placeholder-text-muted focus:outline-none focus:border-border-strong"
                />
                <Button size="sm" variant="ghost" onClick={addTarget}>
                  Add
                </Button>
              </div>
              {form.targets.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {form.targets.map((t) => (
                    <span
                      key={t}
                      className="inline-flex items-center gap-1 px-2 py-1 bg-bg-elevated border border-border rounded text-xs font-mono text-text-secondary"
                    >
                      {t}
                      <button onClick={() => removeTarget(t)}>
                        <X size={10} className="text-text-muted hover:text-white" />
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div className="flex gap-2 pt-2">
              <Button
                variant="primary"
                loading={createMut.isPending}
                disabled={!form.session_name}
                onClick={submitJob}
              >
                Create Job
              </Button>
              <Button variant="ghost" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
            </div>
            {createMut.isError && (
              <p className="text-status-down text-xs font-mono">{createMut.error?.message}</p>
            )}
          </div>
        )}

        {/* Jobs table */}
        <div className="card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h3 className="text-sm font-medium text-white">Recent Jobs</h3>
          </div>
          {jobsQ.isLoading ? (
            <LoadingSpinner className="py-12" label="Loading jobs…" />
          ) : (
            <DataTable
              columns={JOB_COLS as unknown as Column<Record<string, unknown>>[]}
              data={(jobsQ.data?.jobs ?? []) as unknown as Record<string, unknown>[]}
              rowKey="id"
              emptyMessage="No send jobs yet"
              maxHeight="500px"
            />
          )}
        </div>
      </div>
    </div>
  )
}
