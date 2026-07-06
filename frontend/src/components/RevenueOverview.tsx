import { DollarSign, TrendingUp, Calendar, Activity } from 'lucide-react'

interface RevenueData {
  total_min?: number
  total_max?: number
  avg_cpm_min?: number
  avg_cpm_max?: number
}

interface Props {
  revenue?: RevenueData
}

export default function RevenueOverview({ revenue }: Props) {
  if (!revenue) return null

  const cards = [
    {
      label: 'Revenue estimado total',
      value: revenue.total_min !== undefined && revenue.total_max !== undefined
        ? `$${revenue.total_min.toFixed(0)} — $${revenue.total_max.toFixed(0)}`
        : '$0',
      icon: DollarSign,
      color: 'text-green-400',
      bg: 'bg-green-500/5',
      border: 'border-green-500/10',
    },
    {
      label: 'Revenue por 1K vistas',
      value: revenue.avg_cpm_min !== undefined && revenue.avg_cpm_max !== undefined
        ? `$${revenue.avg_cpm_min.toFixed(1)} — $${revenue.avg_cpm_max.toFixed(1)}`
        : '—',
      icon: Activity,
      color: 'text-neon-cyan',
      bg: 'bg-neon-cyan/5',
      border: 'border-neon-cyan/10',
    },
    {
      label: 'CPM promedio (rango)',
      value: revenue.avg_cpm_min !== undefined && revenue.avg_cpm_max !== undefined
        ? `$${Math.round((revenue.avg_cpm_min + revenue.avg_cpm_max) / 2)}`
        : '—',
      icon: TrendingUp,
      color: 'text-neon-gold',
      bg: 'bg-neon-gold/5',
      border: 'border-neon-gold/10',
    },
    {
      label: 'Método de estimación',
      value: 'Basado en CPM vertical',
      icon: Calendar,
      color: 'text-neon-purple',
      bg: 'bg-neon-purple/5',
      border: 'border-neon-purple/10',
    },
  ]

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {cards.map((card, i) => (
        <div
          key={i}
          className={`${card.bg} ${card.border} border rounded-xl p-3 sm:p-4`}
        >
          <div className="flex items-center gap-2 mb-2">
            <card.icon size={16} className={card.color} />
            <span className="text-[11px] text-gray-500">{card.label}</span>
          </div>
          <div className={`text-base sm:text-lg font-mono tabular-nums font-semibold ${card.color}`}>
            {card.value}
          </div>
        </div>
      ))}
    </div>
  )
}
