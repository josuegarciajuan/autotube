import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { api } from '../lib/api'

const BADGE_META: Record<string, { emoji: string; label: string; desc: string }> = {
  first_blood: { emoji: '🩸', label: 'First Blood', desc: 'Primer video publicado' },
  centurion: { emoji: '🛡️', label: 'Centurion', desc: '100 videos publicados' },
  viral: { emoji: '🦠', label: 'Viral', desc: 'Video con +10K views' },
  night_owl: { emoji: '🦉', label: 'Night Owl', desc: 'Video de madrugada' },
  marathon: { emoji: '🏃', label: 'Marathon', desc: '5 videos en 24h' },
  alchemist: { emoji: '⚗️', label: 'Alchemist', desc: 'Todos los modulos activos' },
  ghost: { emoji: '👻', label: 'Ghost', desc: 'Sin actividad en 7 dias' },
}

interface BadgeGalleryProps {
  channelId: number
  compact?: boolean
}

export default function BadgeGallery({ channelId, compact = false }: BadgeGalleryProps) {
  const [badges, setBadges] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getBadges(channelId)
      .then((data: any[]) => {
        setBadges(data.map((b: any) => b.badge_key))
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [channelId])

  if (loading) return null
  if (badges.length === 0 && compact) return null

  const allBadges = Object.keys(BADGE_META)

  return (
    <div>
      <h4 className="text-xs font-semibold text-gray-400 mb-2">
        {compact ? 'Badges' : 'Badges Desbloqueados'} ({badges.length}/{allBadges.length})
      </h4>
      <div className="flex gap-2 flex-wrap">
        {allBadges.map(key => {
          const unlocked = badges.includes(key)
          const meta = BADGE_META[key]
          return (
            <motion.div
              key={key}
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{ delay: Math.random() * 0.3 }}
              className={`relative w-12 h-12 rounded-xl flex items-center justify-center text-xl border transition-all
                ${unlocked
                  ? 'bg-dark-700 border-neon-gold/40 shadow-[0_0_10px_rgba(255,184,48,0.2)]'
                  : 'bg-dark-700/30 border-dark-600 opacity-30 grayscale'
                }`}
              title={`${meta.label}: ${meta.desc}`}
            >
              <span className={unlocked ? '' : 'filter grayscale'}>{meta.emoji}</span>
              {unlocked && (
                <motion.div
                  initial={{ opacity: 0, scale: 2 }}
                  animate={{ opacity: 1, scale: 1 }}
                  className="absolute -top-1 -right-1 w-3 h-3 bg-neon-gold rounded-full"
                />
              )}
            </motion.div>
          )
        })}
      </div>
    </div>
  )
}
