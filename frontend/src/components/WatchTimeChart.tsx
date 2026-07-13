/** Pure SVG area chart for watch time data — zero dependencies. */

interface DailyPoint {
  date: string
  watch_hours: number
  cumulative_hours: number
}

interface Props {
  data: DailyPoint[]
  totalWatchHours: number
  dailyAvgHours: number
  estimatedDays: number | null
  yppProgressPct: number
  remainingHours: number
}

function formatDateLabel(dateStr: string): string {
  // "2026-07-13" → "13 Jul"
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  const months = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
  const m = months[parseInt(parts[1]) - 1] || parts[1]
  return `${parseInt(parts[2])} ${m}`
}

export default function WatchTimeChart({
  data, totalWatchHours, dailyAvgHours, estimatedDays, yppProgressPct, remainingHours,
}: Props) {
  if (!data || data.length < 2) return null

  const W = 600
  const H = 160
  const pad = { top: 10, right: 10, bottom: 18, left: 40 }
  const innerW = W - pad.left - pad.right
  const innerH = H - pad.top - pad.bottom

  // Scale cumulative hours to chart height
  const maxCum = Math.max(...data.map(d => d.cumulative_hours), 1)
  const xStep = innerW / (data.length - 1)

  // Cumulative area path
  let cumPath = ''
  let dailyPath = ''
  const maxDaily = Math.max(...data.map(d => d.watch_hours), 1)

  data.forEach((d, i) => {
    const x = pad.left + i * xStep
    const yCum = pad.top + innerH - (d.cumulative_hours / maxCum) * innerH
    const prefix = i === 0 ? 'M' : 'L'
    cumPath += `${prefix}${x},${yCum} `
  })
  // Close area at bottom
  cumPath += `L${pad.left + (data.length - 1) * xStep},${pad.top + innerH} L${pad.left},${pad.top + innerH} Z`

  data.forEach((d, i) => {
    const x = pad.left + i * xStep
    const yDaily = pad.top + innerH - (d.watch_hours / maxDaily) * innerH
    dailyPath += `${i === 0 ? 'M' : 'L'}${x},${yDaily} `
  })

  // Y-axis ticks (5 ticks for cumulative)
  const yTicks = Array.from({ length: 5 }, (_, i) => {
    const val = Math.round(maxCum * i / 4)
    const y = pad.top + innerH - (val / maxCum) * innerH
    return { val, y }
  })

  // X-axis labels (every ~30 days, first and last)
  const xLabels = data
    .map((d, i) => ({ ...d, i }))
    .filter((_, i) => i === 0 || i === data.length - 1 || i % Math.max(1, Math.floor(data.length / 5)) === 0)

  return (
    <div className="glass p-4 rounded-xl border border-green-500/10">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-green-400 text-lg">⏱</span>
          <h3 className="text-sm font-semibold text-gray-300">Horas de Visualización</h3>
        </div>
        <div className="flex gap-3 text-xs">
          <span className="text-gray-400">
            Total: <span className="font-mono text-green-400 font-semibold">{totalWatchHours.toFixed(0)}h</span>
          </span>
          <span className="text-gray-400">
            Media diaria: <span className="font-mono text-green-400">{dailyAvgHours.toFixed(1)}h</span>
          </span>
          {estimatedDays != null && (
            <span className="text-neon-gold">
              ~{estimatedDays}d a 4,000h
            </span>
          )}
        </div>
      </div>

      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-44">
          {/* Grid lines */}
          {yTicks.map((t, i) => (
            <g key={i}>
              <line
                x1={pad.left} y1={t.y} x2={W - pad.right} y2={t.y}
                stroke="#1f2937" strokeWidth={0.5} strokeDasharray="3 3"
              />
              <text
                x={pad.left - 4} y={t.y + 3}
                textAnchor="end" fill="#6b7280" fontSize={8}
              >
                {t.val >= 1000 ? `${(t.val/1000).toFixed(0)}K` : t.val}h
              </text>
            </g>
          ))}

          {/* Cumulative area (filled) */}
          <path d={cumPath} fill="rgba(34,197,94,0.15)" stroke="none" />

          {/* Cumulative line */}
          <path
            d={cumPath.replace(/L\d+,\d+ Z.*/, '').trim()}
            fill="none" stroke="#22c55e" strokeWidth={2}
            strokeLinejoin="round" strokeLinecap="round"
          />

          {/* Daily line */}
          <path
            d={dailyPath}
            fill="none" stroke="#a3e635" strokeWidth={1}
            strokeLinejoin="round" strokeLinecap="round"
            opacity={0.6}
          />

          {/* X-axis labels */}
          {xLabels.map((d) => (
            <text
              key={d.i}
              x={pad.left + d.i * xStep}
              y={pad.top + innerH + 12}
              textAnchor="middle" fill="#6b7280" fontSize={8}
            >
              {formatDateLabel(d.date)}
            </text>
          ))}
        </svg>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 mt-1 text-[10px] text-gray-500">
        <span className="flex items-center gap-1">
          <span className="w-3 h-0.5 bg-green-500 rounded" /> Acumuladas
        </span>
        <span className="flex items-center gap-1">
          <span className="w-3 h-0.5 bg-lime-400 rounded opacity-60" /> Diarias
        </span>
      </div>

      {/* YPP progress bar */}
      <div className="mt-3">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[10px] text-gray-500">
            Progreso a 4,000h: {yppProgressPct.toFixed(0)}%
          </span>
          <span className="text-[10px] text-gray-500">
            Restan {remainingHours.toFixed(0)}h
          </span>
        </div>
        <div className="h-2 bg-dark-600 rounded-full overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-green-500 to-neon-gold rounded-full transition-all duration-700"
            style={{ width: `${Math.min(100, yppProgressPct)}%` }}
          />
        </div>
      </div>
    </div>
  )
}
