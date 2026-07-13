import { useState, useRef, useEffect } from 'react'
import { Heart, Eye, Zap, Clock, Cog } from 'lucide-react'
import { useChannelFilter } from '../context/ChannelFilterContext'
import { useEasterEgg } from '../context/EasterEggContext'

interface ChannelFilterProps {
  channels: Array<{ id: number; name: string; slug: string }>
  pipelineCounts: Record<number, number>  // channel_id -> active pipeline count
}

export default function ChannelFilter({ channels, pipelineCounts }: ChannelFilterProps) {
  const { selectedChannelId, setSelectedChannelId, setChannels } = useChannelFilter()
  const { triggerGlitch } = useEasterEgg()

  // Sync channels to context
  useEffect(() => {
    setChannels(channels)
  }, [channels, setChannels])

  if (channels.length === 0) return null

  return (
    <div className="flex items-center gap-2 mb-6 overflow-x-auto pb-2 scrollbar-none"
      style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
      {/* ALL pill */}
      <button
        onClick={() => { setSelectedChannelId(null); triggerGlitch() }}
        className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all duration-300 shrink-0
          ${selectedChannelId === null
            ? 'bg-neon-red/20 border border-neon-red/50 text-neon-red shadow-[0_0_15px_rgba(255,51,85,0.2)]'
            : 'bg-dark-700 border border-dark-500 text-gray-400 hover:text-white hover:border-gray-400'
          }`}
      >
        <div className="w-7 h-7 rounded-full bg-gradient-to-br from-neon-cyan to-neon-purple flex items-center justify-center text-xs font-bold text-white">
          ∞
        </div>
        <span>TODOS</span>
      </button>

      {channels.map(ch => {
        const active = selectedChannelId === ch.id
        const pipeCount = pipelineCounts[ch.id] || 0
        return (
          <button
            key={ch.id}
            onClick={() => { setSelectedChannelId(ch.id); triggerGlitch() }}
            className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all duration-300 shrink-0 relative
              ${active
                ? 'bg-neon-red/20 border border-neon-red/50 text-neon-red shadow-[0_0_15px_rgba(255,51,85,0.2)]'
                : 'bg-dark-700 border border-dark-500 text-gray-400 hover:text-white hover:border-gray-400'
              }`}
          >
            {/* Avatar */}
            <div className={`w-7 h-7 rounded-full overflow-hidden border-2 flex-shrink-0 ${active ? 'border-neon-red' : 'border-dark-500'}`}>
              <img
                src={`api/static/output/thumbnails/${ch.slug}/avatar.jpg`}
                alt={ch.name}
                className="w-full h-full object-cover"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = 'none'
                }}
              />
            </div>
            <span className="truncate max-w-[100px]">{ch.name}</span>

            {/* Pipeline glow */}
            {pipeCount > 0 && (
              <span className={`absolute -top-1 -right-1 w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-bold
                ${active ? 'bg-neon-red text-white' : 'bg-neon-gold text-dark-900'}
                animate-pulse`}>
                {pipeCount}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
