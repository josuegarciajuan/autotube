import { TrendingUp, Clock, Target, Zap, ShieldCheck, Radar } from 'lucide-react'
import { useMonitorDashboard } from '../../hooks/useQueries'

interface QuickStatsData {
  generated_today: number
  success_rate_7d: number
  total_7d: number
  avg_generation_minutes: number | null
  next_slot: {
    channel: string
    slug: string
    at: string
  } | null
}

export default function QuickStats() {
  const { data: monitorData } = useMonitorDashboard()
  const data: QuickStatsData | null = monitorData?.quick_stats || null

  function fmtDate(iso: string): string {
    try {
      const d = new Date(iso + 'Z')
      return d.toLocaleString('es-ES', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' })
    } catch { return iso }
  }

  const sh = (monitorData as any)?.script_health
  const scriptHealthPct = sh ? `${(100 - (sh.error_rate_7d || 0)).toFixed(0)}%` : '--'
  const scriptHealthSuffix = sh?.failures_24h !== undefined
    ? `${sh.failures_24h} fallos 24h${sh.emergency_24h ? ` / ${sh.emergency_24h} emerg.` : ''}`
    : ''

  // View gap coverage — average across all channels
  const viewGap = (monitorData as any)?.view_gap as Record<string, any> | undefined
  let avgCoverage = 100
  let totalGap = 0
  if (viewGap && Object.keys(viewGap).length > 0) {
    const values = Object.values(viewGap)
    avgCoverage = Math.round(values.reduce((s, v) => s + (v.coverage_pct ?? 100), 0) / values.length)
    totalGap = values.reduce((s, v) => s + (v.gap ?? 0), 0)
  }
  const coverageSuffix = totalGap > 0
    ? `${totalGap.toLocaleString()} untracked views`
    : 'all tracked'

  return (
    <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
      <StatCard
        icon={<TrendingUp size={14} className="text-emerald-400" />}
        label="Generados hoy"
        value={data?.generated_today ?? '--'}
        suffix="videos"
      />
      <StatCard
        icon={<Target size={14} className="text-neon-cyan" />}
        label="Tasa de éxito 7d"
        value={data ? `${data.success_rate_7d}%` : '--'}
        suffix={data ? `${data.total_7d} total` : ''}
      />
      <StatCard
        icon={<Clock size={14} className="text-amber-400" />}
        label="Tiempo medio gen."
        value={data?.avg_generation_minutes ? `${data.avg_generation_minutes}m` : '--'}
        suffix="por video"
      />
      <StatCard
        icon={<Zap size={14} className="text-neon-purple" />}
        label="Próximo slot"
        value={data?.next_slot ? data.next_slot.channel : '--'}
        suffix={data?.next_slot?.at ? fmtDate(data.next_slot.at) : 'sin programar'}
      />
      <StatCard
        icon={<ShieldCheck size={14} className="text-emerald-400" />}
        label="Script Gen 7d"
        value={scriptHealthPct}
        suffix={scriptHealthSuffix}
      />
      <StatCard
        icon={<Radar size={14} className={avgCoverage >= 95 ? 'text-emerald-400' : avgCoverage >= 80 ? 'text-amber-400' : 'text-red-400'} />}
        label="Tracking Coverage"
        value={`${avgCoverage}%`}
        suffix={coverageSuffix}
      />
    </div>
  )
}

function StatCard({ icon, label, value, suffix }: {
  icon: React.ReactNode
  label: string
  value: string | number
  suffix: string
}) {
  return (
    <div className="glass rounded-xl p-3 border border-surface-border">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-[10px] text-gray-500">{label}</span>
      </div>
      <div className="text-xl font-bold font-mono text-white">{value}</div>
      <div className="text-[10px] text-gray-600">{suffix}</div>
    </div>
  )
}
