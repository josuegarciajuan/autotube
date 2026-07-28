import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import {
  AlertTriangle, CheckCircle, Clock, Film, Smartphone,
  Loader2, RefreshCw, Bell, Eye, EyeOff, XCircle, ChevronDown, ChevronRight,
  Activity, ShieldAlert, Radio
} from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { CHANNEL_PILL, DEFAULT_PILL } from '../lib/channelConfig'

interface DashboardData {
  health_score: number
  videos: {
    generating: number
    awaiting_upload: number
    uploaded_private: number
    error: number
    active: any[]
  }
  shorts: {
    rendering: number
    uploading: number
    failed: number
    active: any[]
  }
  alerts: {
    total: number
    critical: number
    warning: number
    info: number
    unacknowledged: number
  }
}

interface Alert {
  id: number
  entity_type: string
  entity_id: number | null
  channel_id: number | null
  alert_type: string
  severity: string
  title: string
  message: string
  acknowledged: boolean
  resolved: boolean
  created_at: string
  entity_title: string | null
  entity_slug: string | null
}

interface LifecycleEvent {
  id: number
  entity_type: string
  entity_id: number
  event: string
  phase: string | null
  status: string
  message: string | null
  created_at: string
}

function fmtTime(isoStr: string): string {
  try {
    const d = new Date(isoStr + 'Z')
    return d.toLocaleString('es-ES', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })
  } catch { return isoStr }
}

function severityColor(s: string) {
  switch (s) {
    case 'critical': return 'bg-red-500/20 text-red-400 border-red-500/30'
    case 'warning': return 'bg-amber-500/20 text-amber-400 border-amber-500/30'
    case 'info': return 'bg-blue-500/20 text-blue-400 border-blue-500/30'
    default: return 'bg-gray-500/20 text-gray-400 border-gray-500/30'
  }
}

function statusColor(status: string) {
  switch (status) {
    case 'completed': return 'text-emerald-400'
    case 'started': return 'text-amber-400'
    case 'failed': return 'text-red-400'
    default: return 'text-gray-400'
  }
}

function statusIcon(status: string) {
  switch (status) {
    case 'completed': return <CheckCircle size={12} className="text-emerald-400" />
    case 'started': return <Clock size={12} className="text-amber-400 animate-pulse" />
    case 'failed': return <XCircle size={12} className="text-red-400" />
    default: return <Activity size={12} className="text-gray-400" />
  }
}

function eventLabel(event: string): string {
  const map: Record<string, string> = {
    generation_started: 'Gen. iniciada',
    generation_completed: 'Gen. completada',
    generation_failed: 'Gen. fallida',
    upload_started: 'Subida iniciada',
    upload_completed: 'Subida completada',
    upload_failed: 'Subida fallida',
    publish_scheduled: 'Pub. programada',
    publish_completed: 'Publicación completada',
    scrape_started: 'Scrape iniciado',
    script_started: 'Script iniciado',
    tts_started: 'TTS iniciado',
    media_started: 'Media iniciado',
    video_started: 'Video iniciado',
    metadata_started: 'Metadata iniciado',
  }
  return map[event] || event
}

export default function Monitor() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [loading, setLoading] = useState(true)
  const [alertFilter, setAlertFilter] = useState<string>('active')
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; id: number } | null>(null)
  const [entityEvents, setEntityEvents] = useState<LifecycleEvent[]>([])
  const [eventsLoading, setEventsLoading] = useState(false)

  const refreshDashboard = useCallback(async () => {
    try {
      const d = await api.getMonitorDashboard()
      setData(d)
    } catch (e) { console.error(e) }
    setLoading(false)
  }, [])

  const refreshAlerts = useCallback(async () => {
    try {
      const status = alertFilter === 'active' ? 'active' : undefined
      const res = await api.getMonitorAlerts(status)
      setAlerts(res.alerts || [])
    } catch (e) { console.error(e) }
  }, [alertFilter])

  const refreshEntityEvents = useCallback(async () => {
    if (!selectedEntity) return
    setEventsLoading(true)
    try {
      const res = await api.getMonitorEvents(selectedEntity.type, selectedEntity.id)
      setEntityEvents(res.events || [])
    } catch (e) { console.error(e) }
    setEventsLoading(false)
  }, [selectedEntity])

  useEffect(() => { refreshDashboard() }, [refreshDashboard])
  useEffect(() => { refreshAlerts() }, [refreshAlerts])
  useEffect(() => { refreshEntityEvents() }, [refreshEntityEvents])

  const autoRefresh = useCallback(() => {
    refreshDashboard()
    refreshAlerts()
  }, [refreshDashboard, refreshAlerts])

  useEffect(() => {
    const iv = setInterval(autoRefresh, 30000)
    return () => clearInterval(iv)
  }, [autoRefresh])

  async function handleAcknowledge(alertId: number) {
    await api.acknowledgeMonitorAlert(alertId)
    refreshAlerts()
  }

  async function handleResolve(alertId: number) {
    await api.resolveMonitorAlert(alertId)
    refreshAlerts()
  }

  if (!data) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  const criticalAlerts = alerts.filter(a => a.severity === 'critical' && !a.resolved).length

  return (
    <div className="max-w-7xl mx-auto space-y-4 sm:space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-xl font-bold text-white flex items-center gap-2">
          <ShieldAlert size={20} className="text-neon-red" />
          Monitor de Pipeline
        </h2>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-600">Auto-refresh 30s</span>
          <button
            onClick={autoRefresh}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-500/20 bg-gray-500/5 text-gray-400 hover:bg-gray-500/10 transition-all text-xs"
          >
            <RefreshCw size={13} />
            Refrescar
          </button>
        </div>
      </div>

      {/* Alert banner for critical issues */}
      {criticalAlerts > 0 && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-3 flex items-center gap-3">
          <Bell size={18} className="text-red-400 animate-pulse" />
          <span className="text-red-300 font-medium">
            {criticalAlerts} alerta{criticalAlerts > 1 ? 's' : ''} crítica{criticalAlerts > 1 ? 's' : ''} activa{criticalAlerts > 1 ? 's' : ''}
          </span>
          <span className="text-red-400/60 text-xs ml-auto">Revisa la lista de alertas abajo</span>
        </div>
      )}

      {/* Health Score */}
      <div className="glass rounded-xl p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-300">Estado del Sistema</h3>
          <span className={`text-lg font-bold font-mono ${
            data.health_score >= 80 ? 'text-emerald-400' :
            data.health_score >= 50 ? 'text-amber-400' : 'text-red-400'
          }`}>
            {data.health_score}/100
          </span>
        </div>
        <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-1000 ${
              data.health_score >= 80 ? 'bg-emerald-500' :
              data.health_score >= 50 ? 'bg-amber-500' : 'bg-red-500'
            }`}
            style={{ width: `${data.health_score}%` }}
          />
        </div>
      </div>

      {/* Status Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatusCard
          label="Videos generándose"
          count={data.videos.generating}
          color="amber"
          icon={<Film size={16} />}
          active={data.videos.active}
          onEntityClick={(id) => setSelectedEntity({ type: 'video', id })}
        />
        <StatusCard
          label="Shorts en proceso"
          count={data.shorts.rendering + data.shorts.uploading}
          color="emerald"
          icon={<Smartphone size={16} />}
          active={data.shorts.active}
          onEntityClick={(id) => setSelectedEntity({ type: 'short', id })}
        />
        <StatusCard
          label="Pendientes de publicar"
          count={data.videos.uploaded_private}
          color="blue"
          icon={<Clock size={16} />}
          active={[]}
        />
        <StatusCard
          label="Errores activos"
          count={data.videos.error + data.shorts.failed}
          color="red"
          icon={<AlertTriangle size={16} />}
          active={[]}
        />
      </div>

      {/* Alerts Section */}
      <div className="glass rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
            <Bell size={16} className="text-neon-gold" />
            Alertas
            {data.alerts.total > 0 && (
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                data.alerts.unacknowledged > 0
                  ? 'bg-red-500/20 text-red-400'
                  : 'bg-gray-500/20 text-gray-400'
              }`}>
                {data.alerts.unacknowledged} sin leer
              </span>
            )}
          </h3>
          <div className="flex gap-1">
            {(['active', 'acknowledged', 'resolved'] as const).map(f => (
              <button
                key={f}
                onClick={() => setAlertFilter(f)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  alertFilter === f
                    ? 'bg-neon-red/10 text-neon-red border border-neon-red/20'
                    : 'text-gray-400 hover:text-white border border-transparent'
                }`}
              >
                {f === 'active' ? 'Activas' : f === 'acknowledged' ? 'Vistas' : 'Resueltas'}
              </button>
            ))}
          </div>
        </div>

        {alerts.length === 0 ? (
          <div className="text-center py-8">
            <CheckCircle size={36} className="mx-auto mb-3 text-emerald-600" />
            <p className="text-gray-500 text-sm">No hay alertas {alertFilter === 'active' ? 'activas' : alertFilter}</p>
          </div>
        ) : (
          <div className="space-y-2 max-h-[500px] overflow-y-auto">
            {alerts.map(a => (
              <AlertRow
                key={a.id}
                alert={a}
                onAcknowledge={() => handleAcknowledge(a.id)}
                onResolve={() => handleResolve(a.id)}
                onEntityClick={() => {
                  if (a.entity_id) setSelectedEntity({ type: a.entity_type, id: a.entity_id })
                }}
              />
            ))}
          </div>
        )}
      </div>

      {/* Entity Timeline Drill-down */}
      <AnimatePresence>
        {selectedEntity && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="glass rounded-xl p-5 overflow-hidden"
          >
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
                <Radio size={14} className="text-neon-cyan" />
                Timeline: {selectedEntity.type} #{selectedEntity.id}
              </h3>
              <button
                onClick={() => setSelectedEntity(null)}
                className="text-gray-400 hover:text-white text-xs"
              >
                Cerrar ✕
              </button>
            </div>
            {eventsLoading ? (
              <div className="flex justify-center py-4"><Loader2 size={18} className="animate-spin text-gray-500" /></div>
            ) : entityEvents.length === 0 ? (
              <p className="text-gray-500 text-xs py-4 text-center">Sin eventos registrados</p>
            ) : (
              <div className="space-y-1 max-h-[400px] overflow-y-auto">
                {entityEvents.map((ev, i) => (
                  <div
                    key={ev.id || i}
                    className={`flex items-center gap-3 px-3 py-2 text-xs border-b border-surface-border/20 last:border-0 hover:bg-dark-600/30 rounded ${
                      ev.status === 'failed' ? 'bg-red-900/10' : ''
                    }`}
                  >
                    <span className="font-mono text-gray-500 w-[70px] shrink-0">
                      {fmtTime(ev.created_at)}
                    </span>
                    <span className="shrink-0">{statusIcon(ev.status)}</span>
                    <span className={`text-gray-300 min-w-0`}>
                      <span className={`font-medium ${statusColor(ev.status)}`}>
                        {eventLabel(ev.event)}
                      </span>
                      {ev.phase && (
                        <span className="text-gray-600 ml-1">[{ev.phase}]</span>
                      )}
                      {ev.message && (
                        <span className="text-gray-500 ml-2 truncate">— {ev.message}</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Active Videos */}
      {data.videos.active.length > 0 && (
        <div className="glass rounded-xl p-5">
          <h3 className="text-sm font-semibold text-gray-300 mb-3 flex items-center gap-2">
            <Film size={14} className="text-amber-400" />
            Videos en generación ({data.videos.active.length})
          </h3>
          <div className="space-y-1">
            {data.videos.active.map((v: any) => (
              <button
                key={v.id}
                onClick={() => setSelectedEntity({ type: 'video', id: v.id })}
                className="w-full flex items-center gap-3 px-3 py-2 text-xs border-b border-surface-border/20 last:border-0 hover:bg-dark-600/30 rounded text-left"
              >
                <span className="text-gray-300">#{v.id}</span>
                <span className="text-gray-500">{v.canal}</span>
                <span className="text-amber-400 ml-auto">{v.progress_phase} ({v.progress}%)</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function StatusCard({ label, count, color, icon, active, onEntityClick }: {
  label: string
  count: number
  color: string
  icon: React.ReactNode
  active: any[]
  onEntityClick?: (id: number) => void
}) {
  const colorMap: Record<string, string> = {
    amber: 'border-amber-500/20 bg-amber-500/5 text-amber-400',
    emerald: 'border-emerald-500/20 bg-emerald-500/5 text-emerald-400',
    blue: 'border-blue-500/20 bg-blue-500/5 text-blue-400',
    red: 'border-red-500/20 bg-red-500/5 text-red-400',
  }

  return (
    <div className={`glass rounded-xl p-4 border ${colorMap[color]?.split(' ')[0] || ''}`}>
      <div className="flex items-center gap-2 mb-2">
        <span className={colorMap[color]?.split(' ')?.slice(2).join(' ') || ''}>{icon}</span>
        <span className="text-xs text-gray-400">{label}</span>
      </div>
      <div className={`text-2xl font-bold font-mono ${colorMap[color]?.split(' ')[2] || ''}`}>
        {count}
      </div>
      {active.length > 0 && onEntityClick && (
        <div className="mt-2 space-y-1">
          {active.slice(0, 3).map((a: any) => (
            <button
              key={a.id}
              onClick={() => onEntityClick(a.id)}
              className="block w-full text-[10px] text-gray-500 hover:text-gray-300 text-left truncate"
            >
              #{a.id} — {a.status || a.progress_phase || 'en proceso'}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function AlertRow({ alert: a, onAcknowledge, onResolve, onEntityClick }: {
  alert: Alert
  onAcknowledge: () => void
  onResolve: () => void
  onEntityClick: () => void
}) {
  return (
    <div className={`flex flex-col sm:flex-row sm:items-center gap-2 px-3 py-2.5 border-b border-surface-border/20 last:border-0 rounded text-xs ${
      a.resolved ? 'opacity-50' : ''
    }`}>
      <div className="flex items-center gap-2 flex-1 min-w-0">
        <span className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium border ${severityColor(a.severity)}`}>
          {a.severity}
        </span>
        <span className="text-gray-400 shrink-0">{a.alert_type}</span>
        <span className="text-gray-300 font-medium truncate">{a.title}</span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {a.entity_title && (
          <button
            onClick={onEntityClick}
            className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${
              a.entity_type === 'video'
                ? 'bg-neon-red/10 text-neon-red border-neon-red/20'
                : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
            } hover:underline`}
          >
            {a.entity_type} #{a.entity_id}
          </button>
        )}
        <span className="text-gray-600">{fmtTime(a.created_at)}</span>
        {!a.resolved && !a.acknowledged && (
          <button
            onClick={onAcknowledge}
            className="flex items-center gap-1 px-1.5 py-0.5 text-[10px] text-amber-400 hover:bg-amber-500/10 rounded"
            title="Marcar como visto"
          >
            <Eye size={11} />
          </button>
        )}
        {!a.resolved && (
          <button
            onClick={onResolve}
            className="flex items-center gap-1 px-1.5 py-0.5 text-[10px] text-emerald-400 hover:bg-emerald-500/10 rounded"
            title="Marcar como resuelto"
          >
            <CheckCircle size={11} />
          </button>
        )}
      </div>
    </div>
  )
}
