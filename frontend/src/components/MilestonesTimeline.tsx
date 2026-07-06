import { Link } from 'react-router-dom'
import { Award, Clock } from 'lucide-react'

interface Milestone {
  channel_id?: number
  channel_name?: string
  channel_slug?: string
  metric_type?: string
  label?: string
  tier?: string
  percentage?: number
  predicted_days?: number | null
  status?: string
}

interface Props {
  milestones: Milestone[]
}

const TIER_COLORS: Record<string, string> = {
  bronze: '#cd7f32',
  silver: '#a8b4c0',
  gold: '#ffb830',
  diamond: '#a855f7',
}

const TIER_LABELS: Record<string, string> = {
  bronze: 'Bronce',
  silver: 'Plata',
  gold: 'Oro',
  diamond: 'Diamante',
}

export default function MilestonesTimeline({ milestones }: Props) {
  if (!milestones || milestones.length === 0) return null

  return (
    <div className="glass p-4 sm:p-5 rounded-xl border border-neon-gold/10">
      <div className="flex items-center gap-2 mb-3">
        <Award size={18} className="text-neon-gold" />
        <h3 className="text-sm font-semibold text-gray-300">Próximos Hitos</h3>
      </div>

      <div className="flex gap-2 overflow-x-auto pb-1 -mx-0.5 px-0.5 scrollbar-thin">
        {milestones.slice(0, 8).map((m, i) => {
          const pct = m.percentage ?? 0
          const tierColor = TIER_COLORS[m.tier || 'bronze'] || '#cd7f32'

          return (
            <div
              key={i}
              className="flex-shrink-0 w-[170px] bg-dark-800/50 rounded-lg p-3 border border-white/5"
            >
              <div className="flex items-center gap-1.5 mb-1.5">
                <div
                  className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                  style={{ backgroundColor: tierColor }}
                />
                <span className="text-[10px] text-gray-500">{TIER_LABELS[m.tier || 'bronze']}</span>
              </div>

              <p className="text-xs font-medium text-gray-200 mb-1.5 truncate">{m.label}</p>

              {m.channel_name && (
                <p className="text-[10px] text-gray-500 truncate mb-1.5">{m.channel_name}</p>
              )}

              <div className="h-1.5 bg-dark-600 rounded-full mb-1.5">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${Math.min(100, pct)}%`,
                    backgroundColor: tierColor,
                  }}
                />
              </div>

              <div className="flex items-center justify-between text-[10px]">
                <span className="font-mono tabular-nums text-gray-400">{Math.round(pct)}%</span>
                {m.predicted_days !== null && m.predicted_days !== undefined && m.predicted_days > 0 && (
                  <span className="flex items-center gap-0.5 text-gray-500">
                    <Clock size={10} />
                    ~{m.predicted_days} días
                  </span>
                )}
                {m.predicted_days === 0 && (
                  <span className="text-green-400 text-[10px]">✓</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
