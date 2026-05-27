import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Header from '../components/Layout/Header'
import LoadingSpinner from '../components/UI/LoadingSpinner'
import Button from '../components/UI/Button'
import { useApiQuery, useApiMutation, apiFetch } from '../hooks/useApi'
import { RotateCcw, Save, AlertTriangle } from 'lucide-react'

interface ParamEntry {
  key: string
  service: string
  type: string
  default: string | number | boolean | null
  description: string
  min_value: number | null
  max_value: number | null
  options: string[] | null
  requires_restart: boolean
  multi_select: boolean
  known_values: string[] | null
  current_value: string | null
  has_override: boolean
}

interface ConfigResp {
  config: Record<string, ParamEntry[]>
  error: string | null
}

// Editable input for a single config param
function ParamRow({ param, service }: { param: ParamEntry; service: string }) {
  const qc = useQueryClient()
  const [editValue, setEditValue] = useState(
    param.current_value ?? String(param.default ?? '')
  )
  const [dirty, setDirty] = useState(false)

  const saveMut = useApiMutation(
    ({ value }: { value: string }) =>
      apiFetch(`/api/config/${service}/${param.key}`, {
        method: 'POST',
        body: JSON.stringify({ value }),
      }),
    [['config']]
  )

  const resetMut = useApiMutation(
    () => apiFetch(`/api/config/${service}/${param.key}`, { method: 'DELETE' }),
    [['config']]
  )

  const handleChange = (v: string) => {
    setEditValue(v)
    setDirty(v !== (param.current_value ?? String(param.default ?? '')))
  }

  const handleSave = () => {
    saveMut.mutate({ value: editValue }, {
      onSuccess: () => {
        setDirty(false)
        qc.invalidateQueries({ queryKey: ['config'] })
      },
    })
  }

  const handleReset = () => {
    resetMut.mutate(undefined, {
      onSuccess: () => {
        setEditValue(String(param.default ?? ''))
        setDirty(false)
        qc.invalidateQueries({ queryKey: ['config'] })
      },
    })
  }

  const renderInput = () => {
    if (param.options) {
      return (
        <select
          value={editValue}
          onChange={(e) => handleChange(e.target.value)}
          className="bg-bg-base border border-border text-sm text-white rounded px-2 py-1 font-mono min-w-[120px]"
        >
          {param.options.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      )
    }
    if (param.type === 'bool') {
      return (
        <select
          value={editValue}
          onChange={(e) => handleChange(e.target.value)}
          className="bg-bg-base border border-border text-sm text-white rounded px-2 py-1 font-mono"
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      )
    }
    return (
      <input
        type={param.type === 'int' || param.type === 'float' ? 'number' : 'text'}
        value={editValue}
        onChange={(e) => handleChange(e.target.value)}
        min={param.min_value ?? undefined}
        max={param.max_value ?? undefined}
        step={param.type === 'float' ? 0.1 : 1}
        className="w-32 bg-bg-base border border-border text-sm text-white rounded px-2 py-1 font-mono focus:outline-none focus:border-border-strong"
      />
    )
  }

  return (
    <div className="flex items-start gap-4 py-3 border-b border-border/50 last:border-0">
      {/* Key + description */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-sm text-white">{param.key}</span>
          <span className="text-text-muted text-xs font-mono bg-bg-elevated px-1.5 py-0.5 rounded">
            {param.type}
          </span>
          {param.requires_restart && (
            <span className="inline-flex items-center gap-1 text-status-pending text-xs font-mono bg-status-pending/10 px-1.5 py-0.5 rounded">
              <AlertTriangle size={10} />
              restart
            </span>
          )}
          {param.has_override && (
            <span className="text-status-running text-xs font-mono bg-status-running/10 px-1.5 py-0.5 rounded">
              overridden
            </span>
          )}
        </div>
        <p className="text-text-muted text-xs mt-0.5">{param.description}</p>
        {(param.min_value !== null || param.max_value !== null) && (
          <p className="text-text-muted text-xs mt-0.5 font-mono">
            range: {param.min_value ?? '—'} – {param.max_value ?? '—'}
          </p>
        )}
      </div>

      {/* Default */}
      <div className="text-right flex-shrink-0 min-w-[80px]">
        <p className="text-text-muted text-xs">default</p>
        <p className="font-mono text-xs text-text-secondary">{String(param.default ?? '—')}</p>
      </div>

      {/* Input + actions */}
      <div className="flex items-center gap-2 flex-shrink-0">
        {renderInput()}
        <Button
          size="sm"
          variant={dirty ? 'primary' : 'ghost'}
          loading={saveMut.isPending}
          disabled={!dirty}
          onClick={handleSave}
        >
          <Save size={12} />
        </Button>
        {param.has_override && (
          <Button
            size="sm"
            variant="danger"
            loading={resetMut.isPending}
            onClick={handleReset}
          >
            <RotateCcw size={12} />
          </Button>
        )}
      </div>
    </div>
  )
}

export default function LiveConfig() {
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['collector']))
  const qc = useQueryClient()

  const configQ = useApiQuery<ConfigResp>(
    ['config'], '/api/config', { refetchInterval: 60_000 }
  )

  const toggleService = (svc: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(svc)) next.delete(svc)
      else next.add(svc)
      return next
    })
  }

  const config = configQ.data?.config ?? {}
  const services = Object.keys(config)

  const totalOverrides = services.reduce((acc, svc) => {
    return acc + (config[svc]?.filter((p) => p.has_override).length ?? 0)
  }, 0)

  return (
    <div className="flex flex-col h-full">
      <Header
        title="Live Config"
        subtitle="Runtime parameter overrides stored in Redis"
        lastUpdated={configQ.dataUpdatedAt ? new Date(configQ.dataUpdatedAt) : null}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['config'] })}
        isRefreshing={configQ.isFetching}
      />
      <div className="flex-1 p-6 space-y-4 overflow-auto">
        {configQ.isLoading ? (
          <LoadingSpinner className="py-16" label="Loading configuration…" />
        ) : (
          <>
            {totalOverrides > 0 && (
              <div className="card p-3 flex items-center gap-2 border-status-running/30">
                <div className="w-1.5 h-1.5 rounded-full bg-status-running" />
                <span className="text-status-running text-sm">
                  {totalOverrides} active Redis override{totalOverrides > 1 ? 's' : ''} across {services.length} services
                </span>
              </div>
            )}

            {services.length === 0 ? (
              <div className="card p-12 text-center">
                <p className="text-text-muted text-sm">No configuration registry found</p>
                <p className="text-text-muted text-xs mt-1">
                  Ensure shared/live_config.py is mounted at /app/shared
                </p>
              </div>
            ) : (
              services.map((svc) => {
                const params = config[svc] ?? []
                const overrideCount = params.filter((p) => p.has_override).length
                const isOpen = expanded.has(svc)

                return (
                  <div key={svc} className="card overflow-hidden">
                    <button
                      onClick={() => toggleService(svc)}
                      className="w-full flex items-center justify-between px-4 py-3 hover:bg-accent-5 transition-colors"
                    >
                      <div className="flex items-center gap-3">
                        <span className="font-mono text-sm font-medium text-white">{svc}</span>
                        <span className="text-text-muted text-xs">{params.length} params</span>
                        {overrideCount > 0 && (
                          <span className="text-status-running text-xs font-mono bg-status-running/10 px-1.5 py-0.5 rounded">
                            {overrideCount} overridden
                          </span>
                        )}
                      </div>
                      <span className="text-text-muted text-xs">{isOpen ? '▲' : '▼'}</span>
                    </button>

                    {isOpen && (
                      <div className="border-t border-border px-4">
                        {params.map((param) => (
                          <ParamRow key={param.key} param={param} service={svc} />
                        ))}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </>
        )}
      </div>
    </div>
  )
}
