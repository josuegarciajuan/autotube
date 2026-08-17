import { TrendingUp, Users, Clock, ExternalLink } from 'lucide-react'
import { formatShortNumber } from '../lib/api'

interface Channel {
  id: number
  name: string
  slug: string
  subscribers?: number
  ypp_subs_pct?: number
  ypp_hours_pct?: number
  watch_hours?: number
}

interface Props {
  channels: Channel[]
}

const YPP_TARGET_SUBS = 1000
const YPP_TARGET_HOURS = 4000

function hoursToText(h: number): string {
  return Math.round(h).toLocaleString('es-ES')
}

export default function YppProgressSection({ channels }: Props) {
  if (!channels || channels.length === 0) return null

  return (
    <div className="glass p-4 sm:p-5 rounded-xl border border-neon-gold/10">
      <div className="flex items-center gap-2 mb-3">
        <TrendingUp size={18} className="text-neon-gold" />
        <h3 className="text-sm font-semibold text-neon-gold">Camino a Monetización (YPP)</h3>
        <span className="text-[10px] text-gray-500 ml-auto">{YPP_TARGET_SUBS.toLocaleString()} subs · {YPP_TARGET_HOURS.toLocaleString()}h</span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {channels.map((ch) => {
          const subs = ch.subscribers || 0
          const hours = ch.watch_hours || 0
          const subsPct = ch.ypp_subs_pct ?? Math.min(100, Math.round(subs / YPP_TARGET_SUBS * 100))
          const hoursPct = ch.ypp_hours_pct ?? Math.min(100, Math.round(hours / YPP_TARGET_HOURS * 100))
          const subsRemaining = Math.max(0, YPP_TARGET_SUBS - subs)
          const hoursRemaining = Math.max(0, YPP_TARGET_HOURS - hours)
          const eligible = subsPct >= 100 && hoursPct >= 100

          return (
            <div key={ch.id} className={`bg-dark-800/50 rounded-lg p-3 border ${eligible ? 'border-green-500/30' : 'border-white/5'}`}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-gray-300 truncate max-w-[140px]">{ch.name}</span>
                <div className="flex items-center gap-2">
                  {eligible && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-500/15 text-green-400 font-medium">Elegible</span>
                  )}
                  <a
                    href={`/autotube/channels/${ch.id}`}
                    className="text-neon-cyan/60 hover:text-neon-cyan text-[10px] flex items-center gap-0.5"
                  >
                    Detalle <ExternalLink size={10} />
                  </a>
                </div>
              </div>

              {/* Subscribers bar */}
              <div className="mb-2">
                <div className="flex items-center justify-between mb-0.5">
                  <div className="flex items-center gap-1 text-[10px] text-gray-400">
                    <Users size={10} />
                    <span>Subs: {formatShortNumber(subs)}/{formatShortNumber(YPP_TARGET_SUBS)}</span>
                    {subsRemaining > 0 && (
                      <span className="text-neon-cyan/60">(-{formatShortNumber(subsRemaining)})</span>
                    )}
                  </div>
                  <span className={`text-[10px] font-mono tabular-nums ${
                    subsPct >= 100 ? 'text-green-400' : subsPct >= 80 ? 'text-neon-gold' : 'text-gray-500'
                  }`}>{subsPct}%</span>
                </div>
                <div className="h-2 bg-dark-600 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-neon-cyan to-neon-gold rounded-full transition-all duration-700"
                    style={{ width: `${Math.min(100, subsPct)}%` }}
                  />
                </div>
              </div>

              {/* Watch hours bar */}
              <div>
                <div className="flex items-center justify-between mb-0.5">
                  <div className="flex items-center gap-1 text-[10px] text-gray-400">
                    <Clock size={10} />
                    <span>Horas (12m): {hoursToText(hours)}/{formatShortNumber(YPP_TARGET_HOURS)}</span>
                    {hoursRemaining > 0 && (
                      <span className="text-green-400/60">(-{formatShortNumber(hoursRemaining)})</span>
                    )}
                  </div>
                  <span className={`text-[10px] font-mono tabular-nums ${
                    hoursPct >= 100 ? 'text-green-400' : hoursPct >= 50 ? 'text-neon-gold' : 'text-gray-500'
                  }`}>{hoursPct}%</span>
                </div>
                <div className="h-2 bg-dark-600 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-green-500 to-neon-gold rounded-full transition-all duration-700"
                    style={{ width: `${Math.min(100, hoursPct)}%` }}
                  />
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
