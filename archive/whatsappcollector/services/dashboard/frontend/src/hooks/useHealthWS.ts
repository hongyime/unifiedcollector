import { useState, useEffect, useRef, useCallback } from 'react'
import { wsHealthUrl } from './useApi'

export interface ServiceHealth {
  service: string
  status: 'up' | 'down' | 'unknown'
  latency_ms: number | null
}

interface UseHealthWSResult {
  services: ServiceHealth[]
  connected: boolean
  lastUpdated: Date | null
}

// URL computed at connect-time so it picks up the current token


export function useHealthWS(): UseHealthWSResult {
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [connected, setConnected] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return

    try {
      const ws = new WebSocket(wsHealthUrl())
      wsRef.current = ws

      ws.onopen = () => {
        if (!mountedRef.current) return
        setConnected(true)
      }

      ws.onmessage = (event) => {
        if (!mountedRef.current) return
        try {
          const data = JSON.parse(event.data)
          if (data.services) {
            setServices(data.services)
            setLastUpdated(new Date())
          }
        } catch {
          // ignore parse errors
        }
      }

      ws.onclose = () => {
        if (!mountedRef.current) return
        setConnected(false)
        wsRef.current = null
        // Reconnect after 5 seconds
        reconnectTimer.current = setTimeout(() => {
          if (mountedRef.current) connect()
        }, 5000)
      }

      ws.onerror = () => {
        ws.close()
      }
    } catch {
      // If WebSocket construction fails (e.g. in dev without server), retry
      reconnectTimer.current = setTimeout(() => {
        if (mountedRef.current) connect()
      }, 5000)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [connect])

  return { services, connected, lastUpdated }
}
