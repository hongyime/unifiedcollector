import { useState, useEffect, useRef, useCallback } from 'react'

export interface ServiceHealth { service: string; status: 'up' | 'down' | 'unknown'; latency_ms: number | null }
interface UseHealthWSResult { services: ServiceHealth[]; connected: boolean; lastUpdated: Date | null }

const WS_URL = (() => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/health`
})()

export function useHealthWS(): UseHealthWSResult {
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [connected, setConnected] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    if (wsRef.current?.readyState === WebSocket.OPEN) return
    try {
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws
      ws.onopen = () => { if (mountedRef.current) setConnected(true) }
      ws.onmessage = e => {
        if (!mountedRef.current) return
        try {
          const d = JSON.parse(e.data)
          if (d.services) { setServices(d.services); setLastUpdated(new Date()) }
        } catch {}
      }
      ws.onclose = () => {
        if (!mountedRef.current) return
        setConnected(false); wsRef.current = null
        timerRef.current = setTimeout(() => { if (mountedRef.current) connect() }, 5000)
      }
      ws.onerror = () => ws.close()
    } catch {
      timerRef.current = setTimeout(() => { if (mountedRef.current) connect() }, 5000)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true; connect()
    return () => {
      mountedRef.current = false
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close(); wsRef.current = null
    }
  }, [connect])

  return { services, connected, lastUpdated }
}
