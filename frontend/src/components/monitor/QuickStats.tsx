import { useState, useEffect } from 'react'
import { TrendingUp, Clock, Target, Zap } from 'lucide-react'
import { api } from '../../lib/api'

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
  const [data, setData] = useState<QuickStatsData | null>(null)

  useEffect(() => {
    const fetch = async () => {
      try {
        const d = await api.getMonitorDashboard()
        setData(d.quick_stats || null)
      } catch { /* silent */ }
    }
    fetch()
    const iv = setInterval(fetch, 30000)
    return () => clearInterval(iv)
  }, [])

  function fmtDate(iso: string): string {
    try {
      const d = new Date(iso + 'Z')
      return d.toLocaleString('es-ES', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short' })
    } catch { return iso }
  }

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
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
