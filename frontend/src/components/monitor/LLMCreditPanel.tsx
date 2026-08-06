import { Brain, Bot, Youtube, RefreshCw, AlertTriangle, CheckCircle, XCircle } from 'lucide-react'
import { motion } from 'framer-motion'
import { api } from '../../lib/api'
import { useLLMCredits } from '../../hooks/useQueries'
import { useState } from 'react'

export default function LLMCreditPanel() {
  const { data, isLoading, refetch } = useLLMCredits()
  const [checking, setChecking] = useState(false)

  async function handleCheck() {
    setChecking(true)
    try {
      await api.triggerLLMCreditCheck()
      refetch()
    } finally {
      setTimeout(() => setChecking(false), 2000)
    }
  }

  function fmtTime(iso: string | null): string {
    if (!iso) return '--'
    try {
      const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'))
      const now = Date.now()
      const diffMin = Math.round((now - d.getTime()) / 60000)
      if (diffMin < 1) return 'ahora'
      if (diffMin < 60) return `hace ${diffMin}m`
      const diffH = Math.round(diffMin / 60)
      if (diffH < 24) return `hace ${diffH}h`
      return `hace ${Math.round(diffH / 24)}d`
    } catch { return iso }
  }

  if (isLoading && !data) {
    return (
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="animate-pulse flex items-center gap-2">
          <div className="h-4 w-4 bg-gray-600 rounded" />
          <div className="h-4 w-40 bg-gray-600 rounded" />
        </div>
      </div>
    )
  }

  const deepseek = (data as any)?.deepseek
  const openai = (data as any)?.openai
  const youtube = (data as any)?.youtube

  return (
    <div className="glass rounded-xl border border-surface-border overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-surface-border bg-dark-800/50">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-2">
          <AlertTriangle size={13} className="text-neon-red" />
          Créditos y Cuotas
        </h3>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-gray-600">
            {data?.checked_at ? fmtTime(data.checked_at) : 'sin datos'}
          </span>
          <button
            onClick={handleCheck}
            disabled={checking}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] border border-gray-500/20 bg-gray-500/5 text-gray-400 hover:bg-gray-500/10 transition-all disabled:opacity-50"
          >
            <RefreshCw size={10} className={checking ? 'animate-spin' : ''} />
            Verificar
          </button>
        </div>
      </div>

      {/* Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-surface-border">
        {/* DeepSeek */}
        <CreditCard
          icon={<Brain size={16} className="text-neon-purple" />}
          provider="DeepSeek"
          status={deepseek?.status || 'unknown'}
          main={
            deepseek?.balance_usd !== undefined
              ? `$${Number(deepseek.balance_usd).toFixed(2)}`
              : '--'
          }
          subtitle={
            deepseek?.metadata?.currency
              ? `${Number(deepseek.metadata.topped_up_balance || 0).toFixed(2)} recargado`
              : undefined
          }
          tooltip={deepseek?.status === 'exhausted'
            ? 'DeepSeek sin créditos. Los scripts no se generarán.'
            : deepseek?.status === 'low'
            ? 'Créditos bajos — recarga pronto.'
            : undefined}
        />

        {/* OpenAI */}
        <CreditCard
          icon={<Bot size={16} className="text-neon-cyan" />}
          provider="OpenAI"
          status={openai?.status || 'unknown'}
          main={openai?.status === 'healthy' ? 'OK' : openai?.status === 'exhausted' ? 'Agotado' : '--'}
          subtitle={
            openai?.error_count_7d !== undefined
              ? `${openai.error_count_7d} errores 7d`
              : undefined
          }
          tooltip={openai?.last_error
            ? `Último error: ${openai.last_error.slice(0, 120)}`
            : undefined}
        />

        {/* YouTube */}
        <CreditCard
          icon={<Youtube size={16} className="text-neon-red" />}
          provider="YouTube API"
          status={youtube?.exhausted ? 'exhausted' : 'healthy'}
          main={youtube?.exhausted ? 'Agotada' : 'OK'}
          subtitle={
            youtube?.exhausted
              ? `${youtube.elapsed_hours}h / reset en ${youtube.estimated_reset_hours}h`
              : 'Cuota disponible'
          }
          tooltip={youtube?.exhausted
            ? `Quota agotada hace ${youtube.elapsed_hours}h. Auto-reset en ~${youtube.estimated_reset_hours}h.`
            : undefined}
          progress={youtube?.exhausted ? Math.min(100, ((youtube.elapsed_hours || 0) / 6) * 100) : undefined}
        />
      </div>
    </div>
  )
}

function CreditCard({
  icon, provider, status, main, subtitle, tooltip, progress,
}: {
  icon: React.ReactNode
  provider: string
  status: string
  main: string
  subtitle?: string
  tooltip?: string
  progress?: number
}) {
  const statusConfig = {
    healthy: { icon: CheckCircle, color: 'text-emerald-400', bg: 'bg-emerald-400/10', label: 'Activo' },
    low: { icon: AlertTriangle, color: 'text-amber-400', bg: 'bg-amber-400/10', label: 'Bajo' },
    exhausted: { icon: XCircle, color: 'text-red-400', bg: 'bg-red-400/10', label: 'Agotado' },
    error: { icon: XCircle, color: 'text-red-400', bg: 'bg-red-400/10', label: 'Error' },
    unknown: { icon: AlertTriangle, color: 'text-gray-400', bg: 'bg-gray-400/10', label: '--' },
  }
  const cfg = statusConfig[status as keyof typeof statusConfig] || statusConfig.unknown
  const StatusIcon = cfg.icon

  return (
    <div className="p-3.5 relative" title={tooltip}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-1.5">
          {icon}
          <span className="text-xs font-medium text-gray-300">{provider}</span>
        </div>
        <span className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full ${cfg.bg} ${cfg.color}`}>
          <StatusIcon size={10} />
          {cfg.label}
        </span>
      </div>

      <div className="text-xl font-bold font-mono text-white mb-0.5">{main}</div>
      {subtitle && (
        <div className="text-[10px] text-gray-500">{subtitle}</div>
      )}

      {/* Quota recovery progress bar (YouTube only) */}
      {progress !== undefined && (
        <div className="mt-2">
          <div className="h-1 bg-dark-700 rounded-full overflow-hidden">
            <motion.div
              className="h-full bg-gradient-to-r from-red-500 to-amber-400 rounded-full"
              initial={{ width: 0 }}
              animate={{ width: `${progress}%` }}
              transition={{ duration: 0.5 }}
            />
          </div>
          <div className="text-[9px] text-gray-600 mt-0.5 text-right">
            {progress >= 100 ? 'Listo' : `~${Math.max(0.1, 6 - (progress * 6 / 100)).toFixed(1)}h para reset`}
          </div>
        </div>
      )}
    </div>
  )
}
