import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Calendar, Plus, Minus, Video, Smartphone, Scissors } from 'lucide-react'
import DailySchedule from '../components/DailySchedule'
import ExecutionTimeline from '../components/ExecutionTimeline'

interface ShortsPlanningConfig {
  channel_id: number; name: string; slug: string
  shorts_enabled: boolean
  shorts_native_per_day: number
  shorts_clip_per_day: number
}

function ShortsCard({ config, onUpdate }: { config: ShortsPlanningConfig; onUpdate: (d: any) => void }) {
  return (
    <div className="bg-dark-700/50 rounded-xl p-4 space-y-3 border border-surface-border">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-white">{config.name}</span>
        <button onClick={() => onUpdate({ shorts_enabled: !config.shorts_enabled })}
          className={`relative w-9 h-5 rounded-full transition-colors ${config.shorts_enabled ? 'bg-neon-red' : 'bg-gray-600'}`}>
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${config.shorts_enabled ? 'translate-x-4' : ''}`} />
        </button>
      </div>
      {config.shorts_enabled && (
        <>
          {/* Nativos por dia */}
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-400 flex items-center gap-1.5">
              <Smartphone size={12} className="text-emerald-400" /> Nativos/dia
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => onUpdate({ shorts_native_per_day: Math.max(0, config.shorts_native_per_day - 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Minus size={12} /></button>
              <span className="text-white font-mono w-4 text-center">{config.shorts_native_per_day}</span>
              <button onClick={() => onUpdate({ shorts_native_per_day: Math.min(5, config.shorts_native_per_day + 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Plus size={12} /></button>
            </div>
          </div>
          {/* Clips por dia */}
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-400 flex items-center gap-1.5">
              <Scissors size={12} className="text-orange-400" /> Clips/dia
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => onUpdate({ shorts_clip_per_day: Math.max(0, config.shorts_clip_per_day - 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Minus size={12} /></button>
              <span className="text-white font-mono w-4 text-center">{config.shorts_clip_per_day}</span>
              <button onClick={() => onUpdate({ shorts_clip_per_day: Math.min(3, config.shorts_clip_per_day + 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Plus size={12} /></button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function ShortsSection() {
  const [configs, setConfigs] = useState<ShortsPlanningConfig[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [])
  async function load() { try { setConfigs(await api.getShortsPlanningConfig()) } catch {} setLoading(false) }
  async function update(channelId: number, data: any) { try { await api.updateShortsPlanningConfig(channelId, data); load() } catch (e: any) { alert(e.message) } }
  if (loading || !configs.length) return null
  return (
    <section className="glass rounded-xl p-5">
      <h3 className="font-display text-base font-semibold text-white mb-4 flex items-center gap-2"><Video size={16} className="text-purple-400" /> Planificacion de Shorts</h3>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {configs.map(ch => (<ShortsCard key={ch.channel_id} config={ch} onUpdate={(d) => update(ch.channel_id, d)} />))}
      </div>
    </section>
  )
}

export default function Scheduling() {
  return (
    <div className="max-w-6xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-2xl font-bold text-white flex items-center gap-3">
          <Calendar size={24} className="text-neon-gold" />
          Programacion
        </h2>
      </div>

      {/* Smart daily schedule (planned_slots — replaced old content_schedules) */}
      <DailySchedule />

      {/* Shorts planning */}
      <ShortsSection />

      {/* Execution history */}
      <ExecutionTimeline />
    </div>
  )
}
