import { useState, useEffect, useCallback } from 'react'
import { Bell, BellOff, CheckCircle, Eye, XCircle } from 'lucide-react'
import { api } from '../../lib/api'
import { useMonitorWebSocket } from '../../hooks/useMonitorWebSocket'

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

type SevFilter = 'todas' | 'critical' | 'warning' | 'info'

export default function AlertsPanel() {
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const [sevFilter, setSevFilter] = useState<SevFilter>('todas')
  const [silenced, setSilenced] = useState<string[]>([])
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const fetchAlerts = useCallback(async () => {
    try {
      const status = filter === 'active' ? 'active' : undefined
      const res = await api.getMonitorAlerts(status)
      setAlerts(res.alerts || [])
    } catch { /* silent */ }
  }, [filter])

  const fetchSilenced = useCallback(async () => {
    try {
      const res = await api.getSilencedAlertTypes()
      setSilenced(res.types || [])
    } catch { /* silent */ }
  }, [])

  useEffect(() => { fetchAlerts() }, [fetchAlerts])
  useEffect(() => { fetchSilenced() }, [fetchSilenced])

  // v50: refresca la lista cuando llega una alerta nueva por WebSocket (el
  // panel ya abierto no esperaba a recargar la página para mostrar alertas
  // de reminders/cobertura de stats).
  const { lastUpdate } = useMonitorWebSocket()
  useEffect(() => {
    if (lastUpdate?.type === 'health_update' && (lastUpdate.alerts_created || 0) > 0) {
      fetchAlerts()
    }
  }, [lastUpdate, fetchAlerts])

  const visible = alerts.filter(a =>
    sevFilter === 'todas' || a.severity === sevFilter
  )

  const critical = alerts.filter(a => a.severity === 'critical' && !a.resolved).length
  const warning = alerts.filter(a => a.severity === 'warning' && !a.resolved).length
  const info = alerts.filter(a => a.severity === 'info' && !a.resolved).length
  const unacked = alerts.filter(a => !a.acknowledged && !a.resolved).length

  async function handleAck(id: number) {
    await api.acknowledgeMonitorAlert(id)
    fetchAlerts()
  }

  async function handleResolve(id: number) {
    await api.resolveMonitorAlert(id)
    fetchAlerts()
  }

  async function handleResolveAll() {
    if (!confirm('¿Resolver TODAS las alertas activas?')) return
    await api.resolveAllMonitorAlerts()
    fetchAlerts()
  }

  async function handleSilenceType(alertType: string, isSilenced: boolean) {
    await api.silenceAlertType(alertType, !isSilenced)
    await Promise.all([fetchSilenced(), fetchAlerts()])
  }

  function severityBadge(s: string) {
    const map: Record<string, string> = {
      critical: 'bg-red-500/20 text-red-400 border-red-500/30',
      warning: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
      info: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
    }
    return map[s] || 'bg-gray-500/20 text-gray-400 border-gray-500/30'
  }

  /** Highlight special alert types with distinctive colors */
  function alertTypeBadge(a: Alert): string {
    if (a.alert_type === 'view_gap_detected') {
      return 'bg-orange-500/20 text-orange-400 border-orange-500/30'
    }
    if (a.alert_type.startsWith('platform_token_expired') || a.alert_type === 'llm_credit_exhausted') {
      return 'bg-red-600/20 text-red-400 border-red-500/40'
    }
    if (a.alert_type === 'llm_credit_low') {
      return 'bg-amber-500/20 text-amber-400 border-amber-500/30'
    }
    if (a.alert_type === 'task_stalled') {
      return 'bg-red-600/20 text-red-300 border-red-500/40'
    }
    if (a.alert_type === 'yt_token_invalid' || a.alert_type === 'yt_upload_error') {
      return 'bg-rose-600/20 text-rose-300 border-rose-500/40'
    }
    if (a.alert_type === 'phase_nonfatal') {
      return 'bg-sky-500/20 text-sky-300 border-sky-500/30'
    }
    return severityBadge(a.severity)
  }

  function alertTypeIcon(a: Alert): string {
    if (a.alert_type === 'view_gap_detected') return 'view_gap'
    if (a.alert_type.startsWith('platform_token_expired')) return 'token_expired'
    if (a.alert_type === 'llm_credit_exhausted') return 'credit_exhausted'
    if (a.alert_type === 'llm_credit_low') return 'credit_low'
    return a.alert_type
  }

  function fmtTime(iso: string) {
    try {
      const d = new Date(iso + 'Z')
      return d.toLocaleString('es-ES', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })
    } catch { return iso }
  }

  const sevTabs: { key: SevFilter; label: string; count: number; cls: string }[] = [
    { key: 'todas', label: 'Todas', count: alerts.length, cls: 'text-gray-400' },
    { key: 'critical', label: 'Críticas', count: critical, cls: 'text-red-400' },
    { key: 'warning', label: 'Avisos', count: warning, cls: 'text-amber-400' },
    { key: 'info', label: 'Info', count: info, cls: 'text-blue-400' },
  ]

  return (
    <div className="glass rounded-xl p-5 border border-surface-border">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
          <Bell size={14} className={critical > 0 ? 'text-red-400 animate-pulse' : 'text-gray-500'} />
          Alertas
          {alerts.length > 0 && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${
              unacked > 0 ? 'bg-red-500/20 text-red-400' : 'bg-gray-500/20 text-gray-500'
            }`}>
              {unacked > 0 ? `${unacked} nuevas` : alerts.length}
            </span>
          )}
        </h3>
        <div className="flex gap-1">
          {(['active', 'all'] as const).map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                filter === f
                  ? 'bg-neon-red/10 text-neon-red border border-neon-red/20'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {f === 'active' ? 'Activas' : 'Todas'}
            </button>
          ))}
          {unacked > 0 && (
            <button
              onClick={handleResolveAll}
              className="px-2 py-0.5 rounded text-[10px] font-medium text-emerald-400 hover:bg-emerald-500/10 border border-emerald-500/20 transition-colors"
              title="Resolver todas las alertas activas"
            >
              ✓ Todas
            </button>
          )}
        </div>
      </div>

      {/* Severity tabs */}
      <div className="flex gap-1.5 mb-3">
        {sevTabs.map(t => (
          <button
            key={t.key}
            onClick={() => setSevFilter(t.key)}
            className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors border ${
              sevFilter === t.key
                ? 'bg-gray-600/30 border-gray-500/40 text-gray-200'
                : 'border-transparent hover:bg-gray-600/20 text-gray-500'
            }`}
          >
            <span className={t.cls}>{t.label}</span>
            <span className="ml-1 text-gray-500">{t.count}</span>
          </button>
        ))}
      </div>

      {/* Silenced types chips */}
      {silenced.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-3">
          {silenced.map(st => (
            <span
              key={st}
              className="inline-flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-gray-500/10 text-gray-500 border border-gray-500/20"
              title="Tipo silenciado — clic para reactivar"
            >
              <BellOff size={9} />
              {st}
              <button
                onClick={() => handleSilenceType(st, true)}
                className="text-gray-400 hover:text-gray-200"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Alert list */}
      {visible.length === 0 ? (
        <div className="text-center py-8">
          <CheckCircle size={28} className="mx-auto mb-2 text-emerald-600" />
          <p className="text-gray-500 text-xs">Sin alertas {filter === 'active' ? 'activas' : ''}</p>
        </div>
      ) : (
        <div className="space-y-1 max-h-[500px] overflow-y-auto">
          {visible.slice(0, 50).map(a => (
            <div
              key={a.id}
              className={`text-xs rounded-lg border transition-colors ${
                a.resolved ? 'opacity-50' : ''
              } hover:bg-dark-600/30`}
            >
              {/* Compact row */}
              <div className="flex items-center gap-2 px-2.5 py-1.5">
                <span className={`shrink-0 w-1 h-1 rounded-full ${
                  a.severity === 'critical' ? 'bg-red-400' : a.severity === 'warning' ? 'bg-amber-400' : 'bg-blue-400'
                }`} />
                <span className={`shrink-0 text-[10px] px-1 rounded font-medium border ${alertTypeBadge(a)}`}>
                  {alertTypeIcon(a)}
                </span>
                <button
                  className="text-gray-300 font-medium truncate text-left flex-1 hover:underline"
                  onClick={() => setExpandedId(expandedId === a.id ? null : a.id)}
                >
                  {a.title}
                </button>
                <span className="text-gray-600 shrink-0">{fmtTime(a.created_at)}</span>
                <button
                  onClick={(e) => { e.stopPropagation(); handleSilenceType(a.alert_type, false) }}
                  className="shrink-0 p-0.5 text-gray-600 hover:text-gray-300 rounded"
                  title={`Silenciar tipo '${a.alert_type}' (ocultar del panel)`}
                >
                  <BellOff size={11} />
                </button>
                {!a.resolved && !a.acknowledged && (
                  <button
                    onClick={(e) => { e.stopPropagation(); handleAck(a.id) }}
                    className="shrink-0 p-0.5 text-amber-400 hover:bg-amber-500/10 rounded"
                    title="Visto"
                  >
                    <Eye size={11} />
                  </button>
                )}
                {!a.resolved && (
                  <button
                    onClick={(e) => { e.stopPropagation(); handleResolve(a.id) }}
                    className="shrink-0 p-0.5 text-emerald-400 hover:bg-emerald-500/10 rounded"
                    title="Resolver"
                  >
                    <CheckCircle size={11} />
                  </button>
                )}
              </div>
              {/* Expanded detail */}
              {expandedId === a.id && a.message && (
                <div className="px-3 pb-2 text-[10px] text-gray-500 border-t border-surface-border/20 pt-1.5">
                  {a.message}
                  {a.entity_title && (
                    <span className="ml-2 text-gray-600">
                      [{a.entity_type}: {a.entity_title}]
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
