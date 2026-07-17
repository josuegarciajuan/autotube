import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Calendar, Video, Smartphone, Scissors, Play, Clock, CheckCircle2, Loader2, XCircle, Settings, Plus, Minus } from 'lucide-react'
import PipelineView from '../components/PipelineView'
import ChannelConfigCard from '../components/ChannelConfigCard'
import { CHANNEL_SHORT, CHANNEL_STYLES, DEFAULT_STYLE } from '../lib/channelConfig'

interface ChannelSummary {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos: { pending: number; running: number; completed: number; cancelled: number }
  shorts: { pending: number; running: number; completed: number }
  next_time: string | null
  next_kind: string | null
}

interface ShortsPlanningConfig {
  channel_id: number; name: string; slug: string
  shorts_enabled: boolean
  shorts_native_per_day: number
  shorts_clips_per_long: number
}

interface PlanningConfig {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos_per_day: number
  viral_per_day: number
  planning_enabled: boolean
}

// ── Timezone ─────────────────────────────────────────────
// DB stores Europe/Madrid local time strings (e.g. "2026-07-13 21:00:00")
function toLocal(ts: string): string {
  const m = ts.match(/(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}` : ts.slice(0, 5)
}

// ── Shorts config card ───────────────────────────────────
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
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-400 flex items-center gap-1.5">
              <Scissors size={12} className="text-orange-400" /> Clips × vídeo largo
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => onUpdate({ shorts_clips_per_long: Math.max(0, (config.shorts_clips_per_long ?? 3) - 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Minus size={12} /></button>
              <span className="text-white font-mono w-4 text-center">{config.shorts_clips_per_long ?? 3}</span>
              <button onClick={() => onUpdate({ shorts_clips_per_long: Math.min(5, (config.shorts_clips_per_long ?? 3) + 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Plus size={12} /></button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// ── Today Status cards ───────────────────────────────────
function TodayStatus() {
  const [channels, setChannels] = useState<ChannelSummary[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [today, shortsToday] = await Promise.all([
        api.getTodaySlots(),
        api.getShortsSlotsToday(),
      ])

      const map = new Map<number, ChannelSummary>()
      const ensure = (id: number, name: string, slug: string) => {
        if (!map.has(id)) {
          map.set(id, {
            channel_id: id, channel_name: name, channel_slug: slug,
            videos: { pending: 0, running: 0, completed: 0, cancelled: 0 },
            shorts: { pending: 0, running: 0, completed: 0 },
            next_time: null, next_kind: null,
          })
        }
        return map.get(id)!
      }

      for (const s of (today.slots || [])) {
        const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
        if (s.status === 'completed') ch.videos.completed++
        else if (s.status === 'running') ch.videos.running++
        else if (s.status === 'pending') ch.videos.pending++
        else if (s.status === 'cancelled') ch.videos.cancelled++
        if (s.status === 'pending' && s.scheduled_at && (!ch.next_time || s.scheduled_at < ch.next_time)) {
          ch.next_time = s.scheduled_at; ch.next_kind = 'video'
        }
      }
      for (const s of (shortsToday.slots || [])) {
        const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
        if (s.status === 'completed') ch.shorts.completed++
        else if (s.status === 'running') ch.shorts.running++
        else if (s.status === 'pending') ch.shorts.pending++
        if (s.status === 'pending' && s.scheduled_at && (!ch.next_time || s.scheduled_at < ch.next_time)) {
          ch.next_time = s.scheduled_at; ch.next_kind = 'short'
        }
      }

      setChannels(Array.from(map.values()).sort((a, b) => a.channel_id - b.channel_id))
    } catch (e) { console.error(e) }
    setLoading(false)
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [load])

  if (loading) {
    return <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin text-gray-600" /></div>
  }
  if (channels.length === 0) {
    return <p className="text-xs text-gray-500 text-center py-4">Sin actividad hoy.</p>
  }

  const tzLabel = 'Europe/Madrid'

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
      {channels.map((ch) => {
        const colors = CHANNEL_STYLES[ch.channel_slug] || DEFAULT_STYLE
        const allDone = ch.videos.pending === 0 && ch.videos.running === 0 && ch.shorts.pending === 0 && ch.shorts.running === 0
        const hasRunning = ch.videos.running > 0 || ch.shorts.running > 0
        const hasPending = ch.videos.pending > 0 || ch.shorts.pending > 0
        const hasCancelled = ch.videos.cancelled > 0

        return (
          <div key={ch.channel_id} className={`rounded-xl p-4 border ${hasRunning ? 'bg-neon-cyan/5 border-neon-cyan/30' : 'bg-dark-700/50 border-surface-border'}`}>
            {/* Channel header */}
            <div className="flex items-center gap-2 mb-3">
              <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
              <span className="text-sm font-semibold text-white">{ch.channel_name}</span>
              <span className="text-[10px] text-gray-600 font-mono">({CHANNEL_SHORT[ch.channel_slug] || ch.channel_slug})</span>
              {hasRunning && <Loader2 size={12} className="text-neon-cyan animate-spin ml-auto" />}
              {allDone && !hasCancelled && <CheckCircle2 size={12} className="text-green-400 ml-auto" />}
              {!hasRunning && !allDone && !hasPending && hasCancelled && <XCircle size={12} className="text-red-400 ml-auto" />}
              {!hasRunning && hasPending && <Clock size={12} className="text-amber-400 ml-auto" />}
            </div>

            {/* Videos row */}
            <div className="flex items-center gap-2 text-xs mb-1.5">
              <Play size={11} className="text-neon-gold" />
              <span className="text-gray-400">Videos:</span>
              <div className="flex gap-1 ml-auto">
                {ch.videos.completed > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-green-400/15 text-green-400 text-[10px]">{ch.videos.completed}✓</span>
                )}
                {ch.videos.running > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-neon-cyan/15 text-neon-cyan text-[10px]">{ch.videos.running}▸</span>
                )}
                {ch.videos.pending > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-400 text-[10px]">{ch.videos.pending}⏳</span>
                )}
                {ch.videos.cancelled > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-red-400/15 text-red-400 text-[10px]">{ch.videos.cancelled}✕</span>
                )}
                {ch.videos.completed === 0 && ch.videos.running === 0 && ch.videos.pending === 0 && ch.videos.cancelled === 0 && (
                  <span className="text-gray-600 text-[10px]">—</span>
                )}
              </div>
            </div>

            {/* Shorts row */}
            <div className="flex items-center gap-2 text-xs mb-2">
              <Smartphone size={11} className="text-emerald-400" />
              <span className="text-gray-400">Shorts:</span>
              <div className="flex gap-1 ml-auto">
                {ch.shorts.completed > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-green-400/15 text-green-400 text-[10px]">{ch.shorts.completed}✓</span>
                )}
                {ch.shorts.running > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-neon-cyan/15 text-neon-cyan text-[10px]">{ch.shorts.running}▸</span>
                )}
                {ch.shorts.pending > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-purple-400/10 text-purple-400 text-[10px]">{ch.shorts.pending}⏳</span>
                )}
                {ch.shorts.completed === 0 && ch.shorts.running === 0 && ch.shorts.pending === 0 && (
                  <span className="text-gray-600 text-[10px]">—</span>
                )}
              </div>
            </div>

            {/* Next execution */}
            {ch.next_time && (
              <div className="text-[10px] text-gray-500 flex items-center gap-1 pt-1 border-t border-surface-border/50">
                <Clock size={9} />
                Proximo: {ch.next_kind === 'short' ? 'Short' : 'Video'} a las{' '}
                <span className="text-gray-300 font-mono">{toLocal(ch.next_time)} {tzLabel}</span>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Shorts section ───────────────────────────────────────
function ShortsSection() {
  const [configs, setConfigs] = useState<ShortsPlanningConfig[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [])
  async function load() { try { setConfigs(await api.getShortsPlanningConfig()) } catch {} setLoading(false) }
  async function update(channelId: number, data: any) { try { await api.updateShortsPlanningConfig(channelId, data); load() } catch (e: any) { alert(e.message) } }
  if (loading || !configs.length) return null
  const activeConfigs = configs.filter(c => c.slug !== 'test')
  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Smartphone size={14} className="text-emerald-400" /> Shorts por canal
      </h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {activeConfigs.map(ch => (<ShortsCard key={ch.channel_id} config={ch} onUpdate={(d) => update(ch.channel_id, d)} />))}
      </div>
    </div>
  )
}

// ── Planning config section ──────────────────────────────
function PlanningConfigSection() {
  const [configs, setConfigs] = useState<PlanningConfig[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const data = await api.getPlanningConfig()
      setConfigs(data.filter((c: PlanningConfig) => c.channel_slug !== 'test'))
    } catch (e) { console.error(e) }
    setLoading(false)
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [load])

  async function update(channelId: number, data: { videos_per_day?: number; planning_enabled?: boolean; viral_per_day?: number }) {
    try {
      await api.updatePlanningConfig(channelId, data)
      load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  if (loading) {
    return <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin text-gray-600" /></div>
  }

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Video size={14} className="text-neon-gold" /> Videos largos por canal
      </h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {configs.map(cfg => (
          <ChannelConfigCard
            key={cfg.channel_id}
            config={cfg}
            onUpdate={(data) => update(cfg.channel_id, data)}
          />
        ))}
      </div>
    </div>
  )
}

// ── Main scheduling page ─────────────────────────────────
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

      {/* ── Section 1: Pipeline Visual (3-columnas) ── */}
      <section className="glass rounded-xl p-5 space-y-3">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Play size={16} className="text-neon-gold" /> Pipeline de Publicacion
          <span className="text-xs text-gray-500 font-normal">(Europe/Madrid)</span>
        </h3>
        <PipelineView />
      </section>

      {/* ── Section 2: Estado de Hoy ─────────────────────── */}
      <section className="glass rounded-xl p-5 space-y-3">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Clock size={16} className="text-neon-cyan" /> Estado de Hoy
           <span className="text-xs text-gray-500 font-normal">(Europe/Madrid)</span>
        </h3>
        <TodayStatus />
      </section>

      {/* ── Section 3: Configuracion ─────────────────────── */}
      <section className="glass rounded-xl p-5 space-y-4">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Settings size={16} className="text-purple-400" /> Configuracion de Programacion
        </h3>
        <PlanningConfigSection />
        <ShortsSection />
      </section>
    </div>
  )
}
