import { useState, useEffect, useRef } from 'react'
import { Activity, CheckCircle, Clock, XCircle, AlertTriangle } from 'lucide-react'

interface MonitorEvent {
  type: string
  // health_update fields
  alerts_created?: number
  alerts_resolved?: number
  // snapshot fields
  status?: any
  system?: any
  // lifecycle event fields
  id?: number
  entity_type?: string
  entity_id?: number
  event?: string
  phase?: string
  event_status?: string
  message?: string
  created_at?: string
}

interface TimelineEntry {
  id: string
  time: string
  icon: 'check' | 'clock' | 'error' | 'warn' | 'info'
  color: string
  message: string
  detail?: string
  entity?: string
}

export default function EventTimeline() {
  const [entries, setEntries] = useState<TimelineEntry[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>()
  const mountedRef = useRef(true)
  const containerRef = useRef<HTMLDivElement>(null)

  const connect = () => {
    if (!mountedRef.current) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/api/ws/monitor`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      addEntry('info', 'Monitor conectado', 'WebSocket conectado en tiempo real')
    }

    ws.onclose = () => {
      setConnected(false)
      if (mountedRef.current) {
        reconnectRef.current = setTimeout(connect, 5000)
      }
    }

    ws.onmessage = (event) => {
      try {
        const data: MonitorEvent = JSON.parse(event.data)
        if (data.type === 'health_update') {
          addEntry(
            'warn',
            `Health check: +${data.alerts_created ?? 0} alertas, ${data.alerts_resolved ?? 0} resueltas`,
            undefined,
            'sistema'
          )
        } else if (data.type === 'snapshot') {
          // Silent — just heartbeat
        } else if (data.type === 'keepalive') {
          // Also silent
        } else if (data.type === 'pong') {
          // Ping response
        }
      } catch { /* ignore */ }
    }
  }

  function addEntry(icon: TimelineEntry['icon'], message: string, detail?: string, entity?: string) {
    const now = new Date().toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setEntries(prev => {
      const next = [{ id: Date.now().toString(), time: now, icon, color: iconColor(icon), message, detail, entity }, ...prev]
      return next.slice(0, 100) // keep last 100
    })
  }

  function iconColor(icon: TimelineEntry['icon']) {
    switch (icon) {
      case 'check': return 'text-emerald-400'
      case 'clock': return 'text-amber-400'
      case 'error': return 'text-red-400'
      case 'warn': return 'text-amber-400'
      case 'info': return 'text-neon-cyan'
    }
  }

  function IconComp({ icon }: { icon: TimelineEntry['icon'] }) {
    switch (icon) {
      case 'check': return <CheckCircle size={14} className="text-emerald-400" />
      case 'clock': return <Clock size={14} className="text-amber-400" />
      case 'error': return <XCircle size={14} className="text-red-400" />
      case 'warn': return <AlertTriangle size={14} className="text-amber-400" />
      case 'info': return <Activity size={14} className="text-neon-cyan" />
    }
  }

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(reconnectRef.current)
      wsRef.current?.close()
    }
  }, [])

  return (
    <div className="glass rounded-xl p-5 border border-surface-border">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
          <Activity size={14} className={connected ? 'text-emerald-400' : 'text-red-400'} />
          Eventos en Tiempo Real
        </h3>
        <span className={`text-[10px] flex items-center gap-1 ${connected ? 'text-emerald-400' : 'text-red-400'}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-400'}`} />
          {connected ? 'Conectado' : 'Reconectando...'}
        </span>
      </div>

      <div ref={containerRef} className="space-y-1 max-h-[300px] overflow-y-auto">
        {entries.length === 0 ? (
          <div className="text-center py-8 text-gray-500 text-xs">
            Esperando eventos del pipeline...
          </div>
        ) : (
          entries.map((entry, i) => (
            <div
              key={entry.id}
              className={`flex items-start gap-2 px-2 py-1.5 text-xs rounded hover:bg-dark-600/20 transition-colors ${
                entry.icon === 'error' ? 'bg-red-500/5' : ''
              }`}
            >
              <span className="font-mono text-gray-600 w-[70px] shrink-0">{entry.time}</span>
              <span className="shrink-0 mt-0.5"><IconComp icon={entry.icon} /></span>
              <div className="min-w-0">
                <span className={`font-medium ${entry.color}`}>{entry.message}</span>
                {entry.detail && (
                  <span className="text-gray-500 ml-1">— {entry.detail}</span>
                )}
                {entry.entity && (
                  <span className="text-gray-600 ml-1">[{entry.entity}]</span>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
