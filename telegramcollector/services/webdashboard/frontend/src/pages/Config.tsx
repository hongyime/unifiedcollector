import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import { useApiQuery } from '../hooks/useApi'

interface ConfigRow { config_key: string; group_name: string; value_plain: string | null; is_sensitive: boolean; updated_at: string }
interface EnvVar { key: string; value: string }

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function Config() {
  const qc = useQueryClient()
  const configQ = useApiQuery<{ settings: ConfigRow[]; error: string | null }>(['config', 'settings'], '/api/config/settings')
  const envQ = useApiQuery<{ env: EnvVar[]; error: string | null }>(['config', 'env'], '/api/config/env')

  const grouped: Record<string, ConfigRow[]> = {}
  for (const row of configQ.data?.settings ?? []) {
    const g = row.group_name || 'general'
    if (!grouped[g]) grouped[g] = []
    grouped[g].push(row)
  }

  return (
    <div className="flex flex-col min-h-full">
      <Header
        title="Live Config"
        subtitle="Runtime configuration settings and environment"
        lastUpdated={configQ.dataUpdatedAt ? new Date(configQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['config'] })}
        isRefreshing={configQ.isFetching}
      />
      <div className="p-6 grid grid-cols-12 gap-3">
        {configQ.isLoading ? (
          <div className="col-span-12"><LoadingSpinner className="py-12" label="Loading config…" /></div>
        ) : (
          Object.entries(grouped).map(([group, rows]) => (
            <div key={group} className="col-span-12 md:col-span-6 card overflow-hidden">
              <div className="px-4 py-3 border-b border-border">
                <h2 className="text-text-muted text-xs uppercase tracking-wider">{group}</h2>
              </div>
              <div className="divide-y divide-border/50">
                {rows.map(row => (
                  <div key={row.config_key} className="px-4 py-2.5 flex items-center justify-between hover:bg-accent-5">
                    <span className="font-mono text-xs text-text-secondary">{row.config_key}</span>
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-xs text-white">
                        {row.is_sensitive ? '•••••' : (row.value_plain ?? '—')}
                      </span>
                      <span className="text-text-muted text-xs">{fmtTs(row.updated_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
        )}

        {/* Env vars */}
        <div className="col-span-12 card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <h2 className="text-text-muted text-xs uppercase tracking-wider">Environment</h2>
          </div>
          {envQ.isLoading ? <LoadingSpinner className="py-8" /> : (
            <div className="divide-y divide-border/50 max-h-64 overflow-auto">
              {(envQ.data?.env ?? []).map(e => (
                <div key={e.key} className="px-4 py-2 flex items-center justify-between hover:bg-accent-5">
                  <span className="font-mono text-xs text-text-muted">{e.key}</span>
                  <span className="font-mono text-xs text-white">{e.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
