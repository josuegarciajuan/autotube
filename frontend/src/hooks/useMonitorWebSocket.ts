/** WebSocket hook for real-time monitor updates (system snapshots + health alerts). */
import { useEffect, useRef, useState, useCallback } from 'react'

export interface SystemSnapshot {
  cpu_percent?: number
  ram_available_mb?: number
  ram_percent?: number
  disk_output_free_gb?: number
  uptime_seconds?: number
}

export interface MonitorUpdate {
  type: string
  // health_update
  alerts_created?: number
  alerts_resolved?: number
  // snapshot
  status?: {
    workers: number
    long_running: number
    shorts_running: number
    ram_available_mb: number | null
    critical_alerts: number
  }
  system?: SystemSnapshot
}

const MAX_RECONNECT_DELAY = 30000
const INITIAL_RECONNECT_DELAY = 2000

export function useMonitorWebSocket() {
  const [connected, setConnected] = useState(false)
  const [lastUpdate, setLastUpdate] = useState<MonitorUpdate | null>(null)
  const [status, setStatus] = useState<MonitorUpdate['status'] | null>(null)
  const [system, setSystem] = useState<SystemSnapshot | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>()
  const attemptRef = useRef(0)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/api/ws/monitor`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return }
      setConnected(true)
      attemptRef.current = 0
    }

    ws.onclose = () => {
      if (!mountedRef.current) return
      setConnected(false)
      if (mountedRef.current) {
        const delay = Math.min(
          INITIAL_RECONNECT_DELAY * Math.pow(2, attemptRef.current),
          MAX_RECONNECT_DELAY
        )
        attemptRef.current++
        reconnectRef.current = setTimeout(connect, delay)
      }
    }

    ws.onmessage = (event) => {
      try {
        const data: MonitorUpdate = JSON.parse(event.data)
        setLastUpdate(data)
        if (data.status) setStatus(data.status)
        if (data.system) setSystem(data.system)
      } catch { /* ignore */ }
    }

    ws.onerror = () => {
      // onclose will handle reconnection
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(reconnectRef.current)
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
  }, [connect])

  return { connected, lastUpdate, status, system }
}
