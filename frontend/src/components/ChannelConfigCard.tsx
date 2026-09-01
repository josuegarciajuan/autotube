/** Per-channel planning configuration card.
 *  Allows setting videos_per_day, viral_per_day, and toggling planning_enabled.
 *  Public target, generation and upload capacity are deliberately separate.
 */

import { useState, useEffect } from 'react'
import { Plus, Minus, Video, Play, Clock, Zap, TrendingUp } from 'lucide-react'
import { api } from '../lib/api'
import { CHANNEL_DOT } from '../lib/channelConfig'

interface ChannelConfig {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos_per_day: number
  public_videos_per_day?: number
  longform_generation_per_day?: number
  upload_capacity_per_day?: number
  viral_per_day: number
  planning_enabled: boolean
  videos_day_boost_weight: number
  viral_day_boost_weight: number
}

export default function ChannelConfigCard({
  config,
  onUpdate,
}: {
  config: ChannelConfig
  onUpdate: (data: { videos_per_day?: number; planning_enabled?: boolean; viral_per_day?: number; public_videos_per_day?: number; longform_generation_per_day?: number; upload_capacity_per_day?: number;
                      videos_day_boost_weight?: number; viral_day_boost_weight?: number }) => void
}) {
  const [saving, setSaving] = useState(false)
  const [peakInfo, setPeakInfo] = useState<any>(null)
  const dotClass = CHANNEL_DOT[config.channel_slug] || 'bg-gray-400'

  useEffect(() => {
    api.getChannelPeakInfo(config.channel_id).then(setPeakInfo).catch(() => {})
  }, [config.channel_id])

  async function update(data: { videos_per_day?: number; planning_enabled?: boolean; viral_per_day?: number; public_videos_per_day?: number; longform_generation_per_day?: number; upload_capacity_per_day?: number;
                                 videos_day_boost_weight?: number; viral_day_boost_weight?: number }) {
    setSaving(true)
    try { onUpdate(data) } finally { setTimeout(() => setSaving(false), 500) }
  }

  const isScheduled = peakInfo?.publish_mode === 'scheduled'

  return (
    <div className={`bg-dark-700/50 rounded-xl p-4 space-y-3 border border-surface-border transition-opacity ${
      !config.planning_enabled ? 'opacity-50' : ''
    }`}>
      {/* Header: channel name + toggle */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
           <span className={`w-2.5 h-2.5 rounded-full ${dotClass}`} />
          <span className="text-sm font-medium text-white">{config.channel_name}</span>
        </div>
        <button
          onClick={() => update({ planning_enabled: !config.planning_enabled })}
          className={`relative w-9 h-5 rounded-full transition-colors ${
            config.planning_enabled ? 'bg-neon-gold' : 'bg-gray-600'
          } ${saving ? 'animate-pulse' : ''}`}
          title={config.planning_enabled ? 'Desactivar planificacion' : 'Activar planificacion'}
        >
          <span
            className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${
              config.planning_enabled ? 'translate-x-4' : ''
            }`}
          />
        </button>
      </div>

      <div className="grid grid-cols-3 gap-2 text-[10px] border-t border-surface-border/40 pt-2">
        {([
          ['public_videos_per_day', 'Públicos', config.public_videos_per_day ?? config.videos_per_day, 10],
          ['longform_generation_per_day', 'Generación', config.longform_generation_per_day ?? config.videos_per_day, 10],
          ['upload_capacity_per_day', 'Cap. subida', config.upload_capacity_per_day ?? config.videos_per_day, 20],
        ] as const).map(([key, label, value, max]) => (
          <label key={key} className="text-gray-500">
            {label}
            <input type="number" min={0} max={max} value={value}
              onChange={(e) => update({ [key]: Math.max(0, Math.min(max, Number(e.target.value))) } as any)}
              className="mt-1 w-full rounded bg-dark-600 border border-surface-border px-1.5 py-1 text-gray-200" />
          </label>
        ))}
      </div>

      {/* Videos/día slider */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400 flex items-center gap-1.5">
          <Video size={12} className="text-neon-gold" /> Videos/dia
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => update({ videos_per_day: Math.max(0, config.videos_per_day - 1) })}
            disabled={!config.planning_enabled}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Minus size={12} />
          </button>
          <span className="text-white font-mono w-4 text-center">{config.videos_per_day}</span>
          <button
            onClick={() => update({ videos_per_day: Math.min(6, config.videos_per_day + 1) })}
            disabled={!config.planning_enabled}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Preview: next videos this channel */}
      <div className="text-[10px] text-gray-500 flex items-center gap-1">
        <Play size={10} />
        {config.planning_enabled && config.videos_per_day > 0
          ? `${config.videos_per_day} video${config.videos_per_day !== 1 ? 's' : ''}/dia`
          : config.planning_enabled
          ? 'Planificacion activa pero con 0 videos/dia'
          : 'Planificacion desactivada'}
      </div>

      {/* Viral/día slider — how many of the N daily videos are viral */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400 flex items-center gap-1.5">
          <Zap size={12} className="text-purple-400" /> Virales/dia
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => update({ viral_per_day: Math.max(0, (config.viral_per_day || 0) - 1) })}
            disabled={!config.planning_enabled}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Minus size={12} />
          </button>
          <span className="text-white font-mono w-4 text-center">{config.viral_per_day || 0}</span>
          <button
            onClick={() => update({ viral_per_day: Math.min(config.videos_per_day, (config.viral_per_day || 0) + 1) })}
            disabled={!config.planning_enabled || config.videos_per_day <= 0}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Boost weight: probability of +1 video/day */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400 flex items-center gap-1.5">
          <TrendingUp size={12} className="text-blue-400" /> Prob. +1 video
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => update({ videos_day_boost_weight: Math.max(0, (config.videos_day_boost_weight || 0.7) - 0.05) })}
            disabled={!config.planning_enabled}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Minus size={12} />
          </button>
          <span className="text-white font-mono w-10 text-center">{Math.round((config.videos_day_boost_weight || 0.7) * 100)}%</span>
          <button
            onClick={() => update({ videos_day_boost_weight: Math.min(1.0, (config.videos_day_boost_weight || 0.7) + 0.05) })}
            disabled={!config.planning_enabled || (config.videos_day_boost_weight || 0.7) >= 1.0}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Boost weight: probability of 2nd viral/day */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400 flex items-center gap-1.5">
          <Zap size={12} className="text-purple-400" /> Prob. 2º viral
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => update({ viral_day_boost_weight: Math.max(0, (config.viral_day_boost_weight || 0.2) - 0.05) })}
            disabled={!config.planning_enabled}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Minus size={12} />
          </button>
          <span className="text-white font-mono w-10 text-center">{Math.round((config.viral_day_boost_weight || 0.2) * 100)}%</span>
          <button
            onClick={() => update({ viral_day_boost_weight: Math.min(1.0, (config.viral_day_boost_weight || 0.2) + 0.05) })}
            disabled={!config.planning_enabled || (config.viral_day_boost_weight || 0.2) >= 1.0}
            className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30"
          >
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Peak info for scheduled channels */}
      {isScheduled && peakInfo && (
        <div className="text-[10px] text-neon-gold/80 flex items-center gap-1">
          <Clock size={10} />
          <span>
            Franja pico: {peakInfo.peak_hour}:00
            {peakInfo.source === 'config' ? ' (config)' : ' (heuristica)'}
          </span>
        </div>
      )}
    </div>
  )
}
