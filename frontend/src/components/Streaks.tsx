import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { Zap, Flame, TrendingUp } from 'lucide-react'
import { api } from '../lib/api'

interface Streak {
  id: number
  channel_id: number
  streak_type: string
  current_count: number
  longest: number
  last_date: string
}

const STREAK_META: Record<string, { icon: typeof Flame; label: string; color: string }> = {
  daily_views_above_average: { icon: Flame, label: 'Views sobre media', color: '#ff3355' },
  daily_publications: { icon: Zap, label: 'Publicaciones', color: '#ffb830' },
  daily_subs_growth: { icon: TrendingUp, label: 'Crecimiento subs', color: '#00e5ff' },
}

interface StreaksProps {
  channelId: number
}

export default function Streaks({ channelId }: StreaksProps) {
  const [streaks, setStreaks] = useState<Streak[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getStreaks(channelId)
      .then(setStreaks)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [channelId])

  if (loading) return null
  if (streaks.length === 0) return null

  return (
    <div className="glass rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Rachas Activas</h3>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {streaks.map(s => {
          const meta = STREAK_META[s.streak_type]
          if (!meta) return null
          const Icon = meta.icon
          const isMilestone = s.current_count >= 30
          return (
            <motion.div
              key={s.streak_type}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className={`rounded-lg p-3 border ${isMilestone ? 'border-neon-gold/40' : 'border-dark-500'} bg-dark-800/60`}
            >
              <div className="flex items-center gap-2 mb-1">
                <Icon className="w-4 h-4" style={{ color: meta.color }} />
                <span className="text-[10px] text-gray-500 uppercase">{meta.label}</span>
              </div>
              <div className="flex items-baseline gap-2">
                <motion.span
                  key={s.current_count}
                  initial={{ scale: 1.3, y: -8 }}
                  animate={{ scale: 1, y: 0 }}
                  className={`text-2xl font-bold font-mono ${isMilestone ? 'text-neon-gold' : 'text-white'}`}
                >
                  {s.current_count}
                </motion.span>
                <span className="text-[10px] text-gray-600">dias</span>
              </div>
              <div className="text-[9px] text-gray-700 mt-1">
                Record: {s.longest} dias
              </div>
              {isMilestone && (
                <motion.div
                  animate={{ scale: [1, 1.05, 1] }}
                  transition={{ repeat: Infinity, duration: 2 }}
                  className="text-[9px] text-neon-gold mt-1"
                >
                  🔥 Racha Legendaria
                </motion.div>
              )}
            </motion.div>
          )
        })}
      </div>
    </div>
  )
}
