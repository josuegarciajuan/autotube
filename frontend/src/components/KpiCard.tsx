import { clsx } from 'clsx'
import { TrendingUp, TrendingDown, Minus, Film, Phone } from 'lucide-react'

interface Breakdown {
  longform: number
  shorts: number
}

interface KpiCardProps {
  label: string
  value: number
  delta: number | null
  icon: React.ElementType
  color: string
  sparkline?: number[]
  format?: 'number' | 'minutes' | 'pipeline'
  generating?: number
  ready?: number
  breakdown?: Breakdown
}

function formatBigNumber(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace('.0', '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace('.0', '') + 'K'
  return n.toLocaleString('es-ES')
}

function formatMinutes(n: number): string {
  if (n >= 60 * 24) {
    const days = Math.floor(n / (60 * 24))
    return `${days}d ${Math.floor((n % (60 * 24)) / 60)}h`
  }
  if (n >= 60) return `${Math.floor(n / 60)}h ${Math.floor(n % 60)}m`
  return `${Math.floor(n)}m`
}

function MiniSparkline({ data, color }: { data: number[]; color: string }) {
  if (!data || data.length < 2) return null
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const h = 28
  const w = 80
  const step = w / (data.length - 1)
  const points = data.map((v, i) => {
    const x = i * step
    const y = h - ((v - min) / range) * (h - 4) - 2
    return `${x},${y}`
  })
  return (
    <svg width={w} height={h} className="opacity-60 shrink-0">
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function KpiCard({ label, value, delta, icon: Icon, color, sparkline, format = 'number', generating, ready, breakdown }: KpiCardProps) {
  const trendIcon =
    delta === null ? null :
    delta > 0 ? <TrendingUp size={14} className="text-green-400" /> :
    delta < 0 ? <TrendingDown size={14} className="text-red-400" /> :
    <Minus size={14} className="text-gray-500" />

  const trendClass = clsx(
    'text-xs font-medium',
    delta === null && 'text-gray-600',
    delta !== null && delta > 0 && 'text-green-400',
    delta !== null && delta < 0 && 'text-red-400',
    delta === 0 && 'text-gray-500'
  )

  let displayValue: string
  if (format === 'minutes') {
    displayValue = formatMinutes(value)
  } else if (format === 'pipeline') {
    displayValue = `${value} activos`
  } else {
    displayValue = formatBigNumber(value)
  }

  return (
    <div className="glass rounded-xl p-3 sm:p-4 flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon size={18} className={`${color} opacity-80 shrink-0`} />
          <span className="text-xs text-gray-500 font-medium truncate">{label}</span>
        </div>
        {sparkline && sparkline.length > 1 && (
          <MiniSparkline data={sparkline} color={color} />
        )}
      </div>
      <div className="flex items-baseline gap-2">
        <span className={`text-2xl sm:text-3xl font-bold font-mono ${color}`}>
          {displayValue}
        </span>
        {trendIcon && (
          <span className={`flex items-center gap-0.5 ${trendClass}`}>
            {trendIcon}
            {delta !== null ? `${Math.abs(delta)}%` : ''}
          </span>
        )}
      </div>
      {breakdown && (
        <div className="flex gap-3 text-[11px] mt-0.5">
          <span className="flex items-center gap-1 text-neon-cyan/70">
            <Film size={11} />
            {formatBigNumber(breakdown.longform)} vídeos
          </span>
          <span className="flex items-center gap-1 text-neon-purple/70">
            <Phone size={11} />
            {formatBigNumber(breakdown.shorts)} shorts
          </span>
        </div>
      )}
      {format === 'pipeline' && (
        <div className="flex gap-2 text-xs">
          {generating != null && generating > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300">
              {generating} generando
            </span>
          )}
          {ready != null && ready > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-green-900/40 text-green-300">
              {ready} listos
            </span>
          )}
        </div>
      )}
    </div>
  )
}
