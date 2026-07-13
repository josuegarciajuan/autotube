import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { api } from '../lib/api'
import confetti from 'canvas-confetti'

interface Milestone {
  channel_id: number
  channel_name: string
  metric_type: string
  label: string
  tier: string
  percentage: number
  predicted_days: number | null
  status: string
}

interface BossFightProps {
  channelId: number
  channelName: string
}

const BOSS_NAMES: Record<string, string> = {
  subs_100: 'Guardian de los 100',
  subs_500: 'Senor de los 500',
  subs_1000: 'Rey del Milenio',
  subs_5000: 'Emperador de las Sombras',
  subs_10000: 'Dragon de Plata',
  watch_hours_100: 'Vigilante del Reloj',
  watch_hours_1000: 'Cronomante Supremo',
  watch_hours_4000: 'Archimago del Tiempo',
}

const TIER_COLORS: Record<string, string> = {
  bronze: '#cd7f32',
  silver: '#c0c0c0',
  gold: '#ffb830',
  diamond: '#00e5ff',
}

export default function BossFight({ channelId, channelName }: BossFightProps) {
  const [milestones, setMilestones] = useState<Milestone[]>([])
  const [defeated, setDefeated] = useState<string | null>(null)

  useEffect(() => {
    api.getChannelMilestones(channelId)
      .then((data: any) => {
        setMilestones(data.milestones || data || [])
        // Check for newly completed milestones
        const completed = (data.milestones || data || []).filter((m: Milestone) => m.percentage >= 100 && m.status === 'in_progress')
        if (completed.length > 0 && !defeated) {
          setDefeated(completed[0].label)
          confetti({
            particleCount: 150,
            spread: 80,
            origin: { y: 0.6 },
            colors: ['#ff3355', '#ffb830', '#00e5ff', '#a855f7'],
          })
          setTimeout(() => setDefeated(null), 5000)
        }
      })
      .catch(() => {})
  }, [channelId])

  if (milestones.length === 0) return null

  return (
    <div className="glass rounded-xl p-4 relative overflow-hidden">
      {/* Boss defeated overlay */}
      {defeated && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="absolute inset-0 z-10 flex items-center justify-center"
          style={{ background: 'radial-gradient(circle, rgba(255,184,48,0.3), transparent 70%)' } as React.CSSProperties}
        >
          <motion.div
            initial={{ scale: 0, rotate: -10 }}
            animate={{ scale: 1, rotate: 0 }}
            transition={{ type: 'spring', stiffness: 200 }}
            className="text-center"
          >
            <div className="text-4xl mb-2">⚔️</div>
            <p className="text-lg font-bold text-neon-gold">BOSS DERROTADO</p>
            <p className="text-sm text-gray-300">{defeated}</p>
            <p className="text-[10px] text-gray-500 mt-1">Canal: {channelName}</p>
          </motion.div>
        </motion.div>
      )}

      <h3 className="text-sm font-semibold text-gray-300 mb-3">Jefes a Derrotar</h3>
      <div className="space-y-3">
        {milestones.slice(0, 4).map((m, i) => {
          const tierColor = TIER_COLORS[m.tier] || '#666'
          const bossName = BOSS_NAMES[m.label] || m.label
          const isComplete = m.percentage >= 100

          return (
            <div key={i} className={`rounded-lg p-3 border transition-all ${isComplete ? 'border-neon-gold/50 bg-neon-gold/5' : 'border-dark-500 bg-dark-800/60'}`}>
              <div className="flex items-center justify-between mb-2">
                <div>
                  <span className="text-xs font-medium text-gray-300">{bossName}</span>
                  <span className="text-[9px] ml-2 px-2 py-0.5 rounded-full uppercase"
                    style={{ backgroundColor: `${tierColor}20`, color: tierColor, border: `1px solid ${tierColor}40` }}>
                    {m.tier}
                  </span>
                </div>
                <span className={`text-xs font-mono font-bold ${isComplete ? 'text-neon-gold' : 'text-gray-500'}`}>
                  {Math.round(m.percentage)}%
                </span>
              </div>
              {/* Boss health bar */}
              <div className="relative h-3 bg-dark-600 rounded-full overflow-hidden">
                <motion.div
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.min(m.percentage, 100)}%` }}
                  transition={{ duration: 1.2, delay: i * 0.1, ease: 'easeOut' }}
                  className={`h-full rounded-full ${isComplete ? 'boss-bar-defeated' : 'boss-bar-active'}`}
                  style={{
                    background: isComplete
                      ? 'linear-gradient(90deg, #ffb830, #ff3355)'
                      : `linear-gradient(90deg, ${tierColor}, ${tierColor}80)`,
                  } as React.CSSProperties}
                />
                {/* Damage sparks */}
                {!isComplete && m.percentage > 0 && (
                  <div className="absolute inset-0 overflow-hidden pointer-events-none">
                    <div className="boss-sparkle" />
                  </div>
                )}
              </div>
              {m.predicted_days !== null && !isComplete && (
                <p className="text-[9px] text-gray-600 mt-1">
                  ~{m.predicted_days} dias restantes
                </p>
              )}
              {isComplete && (
                <p className="text-[9px] text-neon-gold mt-1 animate-pulse">¡Derrotado!</p>
              )}
            </div>
          )
        })}
      </div>

      <style>{`
        @keyframes boss-sparkle {
          0% { transform: translateX(-100%); opacity: 0; }
          30% { opacity: 1; }
          70% { opacity: 1; }
          100% { transform: translateX(400%); opacity: 0; }
        }
        .boss-sparkle {
          width: 40px;
          height: 100%;
          background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent);
          animation: boss-sparkle 2s ease-in-out infinite;
        }
        @keyframes boss-defeated {
          0%, 100% { filter: brightness(1); }
          50% { filter: brightness(1.5); }
        }
        .boss-bar-defeated {
          animation: boss-defeated 1s ease-in-out infinite;
        }
      `}</style>
    </div>
  )
}
