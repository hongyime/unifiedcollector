import Header from '../components/Layout/Header'
import StatusBadge from '../components/UI/StatusBadge'
import { useHealthWS, ServiceHealth } from '../hooks/useHealthWS'

function ServiceCard({ svc }: { svc: ServiceHealth }) {
  return (
    <div className="card p-4 flex flex-col gap-3">
      <div className="flex items-start justify-between">
        <span className="text-sm font-mono font-medium text-white">{svc.service}</span>
        <StatusBadge status={svc.status} pulse={svc.status === 'up'} />
      </div>
      <div className="flex items-center justify-between text-xs">
        <span className="text-text-muted">Latency</span>
        <span className={['font-mono', svc.latency_ms == null ? 'text-text-muted' : svc.latency_ms > 500 ? 'text-status-down' : svc.latency_ms > 200 ? 'text-status-pending' : 'text-status-up'].join(' ')}>
          {svc.latency_ms != null ? `${svc.latency_ms}ms` : '—'}
        </span>
      </div>
    </div>
  )
}

export default function SystemHealth() {
  const { services, connected, lastUpdated } = useHealthWS()
  const upCount = services.filter(s => s.status === 'up').length
  const downCount = services.filter(s => s.status === 'down').length
  const unknownCount = services.filter(s => s.status === 'unknown').length

  return (
    <div className="flex flex-col min-h-full">
      <Header title="System Health" subtitle="Live service status via WebSocket" lastUpdated={lastUpdated} />
      <div className="p-6 grid grid-cols-12 gap-3">
        <div className="col-span-12 card p-3 flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className={['w-2 h-2 rounded-full', connected ? 'bg-status-up animate-pulse' : 'bg-status-down'].join(' ')} />
            <span className="text-sm text-text-secondary">{connected ? 'Live — updates every 5s' : 'Disconnected — reconnecting…'}</span>
          </div>
          {services.length > 0 && (
            <div className="flex items-center gap-4 text-sm font-mono ml-2">
              <span className="text-status-up">{upCount} up</span>
              {downCount > 0 && <span className="text-status-down">{downCount} down</span>}
              {unknownCount > 0 && <span className="text-text-muted">{unknownCount} unknown</span>}
            </div>
          )}
        </div>

        {services.length === 0 ? (
          <div className="col-span-12 card p-16 flex items-center justify-center">
            <div className="text-center space-y-2">
              <div className="w-6 h-6 rounded-full border-2 border-white/20 border-t-white/80 animate-spin mx-auto" />
              <p className="text-text-muted text-sm">{connected ? 'Waiting for health data…' : 'Connecting to WebSocket…'}</p>
            </div>
          </div>
        ) : (
          <>
            <div className="col-span-12 md:col-span-4 card p-4 flex flex-col gap-1">
              <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Total Services</span>
              <span className="text-2xl font-semibold font-mono text-white">{services.length}</span>
            </div>
            <div className="col-span-6 md:col-span-4 card p-4 flex flex-col gap-1">
              <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Up</span>
              <span className="text-2xl font-semibold font-mono text-status-up">{upCount}</span>
            </div>
            <div className="col-span-6 md:col-span-4 card p-4 flex flex-col gap-1">
              <span className="text-text-muted text-xs uppercase tracking-wider font-medium">Down</span>
              <span className="text-2xl font-semibold font-mono text-status-down">{downCount}</span>
            </div>
            <div className="col-span-12 lg:col-span-8 card p-4">
              <h2 className="text-text-muted text-xs uppercase tracking-wider mb-3">Services</h2>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {services.map(svc => <ServiceCard key={svc.service} svc={svc} />)}
              </div>
            </div>
            <div className="col-span-12 lg:col-span-4 card p-4">
              <h2 className="text-text-muted text-xs uppercase tracking-wider mb-3">Latency</h2>
              <div className="space-y-2">
                {services.map(svc => {
                  const pct = Math.min(100, ((svc.latency_ms ?? 0) / 1000) * 100)
                  return (
                    <div key={svc.service} className="flex items-center gap-3">
                      <span className="font-mono text-xs text-text-secondary w-28 truncate">{svc.service}</span>
                      <div className="flex-1 h-1.5 bg-bg-elevated rounded-full overflow-hidden">
                        <div className={['h-full rounded-full transition-all duration-300', svc.status === 'down' ? 'bg-status-down' : svc.latency_ms && svc.latency_ms > 500 ? 'bg-status-pending' : 'bg-white/40'].join(' ')} style={{ width: `${pct}%` }} />
                      </div>
                      <span className="font-mono text-xs text-text-muted w-12 text-right">{svc.latency_ms != null ? `${svc.latency_ms}ms` : '—'}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
