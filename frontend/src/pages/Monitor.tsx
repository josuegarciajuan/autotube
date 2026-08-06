import { RefreshCw, ShieldAlert } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMonitorDashboard } from '../hooks/useQueries'
import SystemMetrics from '../components/monitor/SystemMetrics'
import ActiveWorkers from '../components/monitor/ActiveWorkers'
import EventTimeline from '../components/monitor/EventTimeline'
import AlertsPanel from '../components/monitor/AlertsPanel'
import QuickStats from '../components/monitor/QuickStats'
import LLMCreditPanel from '../components/monitor/LLMCreditPanel'
import { useMonitorWebSocket } from '../hooks/useMonitorWebSocket'

interface DashboardData {
  health_score: number
  alerts: {
    total: number
    critical: number
    warning: number
    info: number
    unacknowledged: number
  }
}

export default function Monitor() {
  const { data, isLoading: loading, refetch: refreshDashboard } = useMonitorDashboard()
  const { connected } = useMonitorWebSocket()

  const dashboardData = data as DashboardData | undefined

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  const criticalAlerts = dashboardData?.alerts?.critical || 0

  return (
    <div className="max-w-7xl mx-auto space-y-3 sm:space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-lg font-bold text-white flex items-center gap-2">
          <ShieldAlert size={18} className="text-neon-red" />
          Monitor de Pipeline
          <span className={`w-1.5 h-1.5 rounded-full ml-1 ${connected ? 'bg-emerald-400' : 'bg-red-400'}`}
                title={connected ? 'WebSocket conectado' : 'WebSocket desconectado'} />
        </h2>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-600">
            {connected ? '🟢 Live' : '🔴 Polling'}
          </span>
          <button
            onClick={() => refreshDashboard()}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-500/20 bg-gray-500/5 text-gray-400 hover:bg-gray-500/10 transition-all text-xs"
          >
            <RefreshCw size={12} />
            Refrescar
          </button>
        </div>
      </div>

      {/* Critical alert banner */}
      {criticalAlerts > 0 && (
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-red-500/10 border border-red-500/30 rounded-xl p-3 flex items-center gap-3"
        >
          <ShieldAlert size={18} className="text-red-400 animate-pulse" />
          <span className="text-red-300 font-medium text-sm">
            {criticalAlerts} alerta{criticalAlerts > 1 ? 's' : ''} crítica{criticalAlerts > 1 ? 's' : ''} activa{criticalAlerts > 1 ? 's' : ''}
          </span>
          <span className="text-red-400/60 text-xs ml-auto">Revisa el panel de alertas</span>
        </motion.div>
      )}

      {/* Health Score */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-300">Estado del Sistema</h3>
          <span className={`text-lg font-bold font-mono ${
            (dashboardData?.health_score ?? 0) >= 80 ? 'text-emerald-400' :
            (dashboardData?.health_score ?? 0) >= 50 ? 'text-amber-400' : 'text-red-400'
          }`}>
            {dashboardData?.health_score ?? 0}/100
          </span>
        </div>
        <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-1000 ${
              (dashboardData?.health_score ?? 0) >= 80 ? 'bg-emerald-500' :
              (dashboardData?.health_score ?? 0) >= 50 ? 'bg-amber-500' : 'bg-red-500'
            }`}
            style={{ width: `${dashboardData?.health_score ?? 0}%` }}
          />
        </div>
      </div>

      {/* ═══ Section 1: LLM Credits / Quota Status ═══ */}
      <LLMCreditPanel />

      {/* ═══ Section 2: System metrics ═══ */}
      <SystemMetrics />

      {/* ═══ Section 3: Quick stats ═══ */}
      <QuickStats />

      {/* ═══ Section 4: Main content: Workers + Alerts side by side ═══ */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3 sm:gap-4">
        {/* Workers (2/3 width) */}
        <div className="xl:col-span-2">
          <ActiveWorkers />
        </div>
        {/* Alerts (1/3 width) */}
        <div className="xl:col-span-1">
          <AlertsPanel />
        </div>
      </div>

      {/* ═══ Section 5: Event timeline (full width, shares parent WS) ═══ */}
      <EventTimeline wsConnected={connected} />
    </div>
  )
}
