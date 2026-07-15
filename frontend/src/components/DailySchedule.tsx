/** Daily schedule visualization for planned_slots + shorts slots.
 *  Shows KPIs, today's timeline, and week overview.
 *  Shorts slots are interleaved with video slots and visually distinguished.
 */

import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Clock, CheckCircle2, Loader2, Calendar, Play, XCircle, BarChart3, Smartphone, Scissors, Filter, X } from 'lucide-react'
import { CHANNEL_SHORT, CHANNEL_DOT, CHANNEL_PILL, DEFAULT_PILL } from '../lib/channelConfig'

interface Slot {
  id: number
  channel_id: number
  channel_name: string
  channel_slug: string
  scheduled_at: string
  target_upload_at: string
  status: string
  slot_position: number
  kind?: string      // "video" or "short"
  short_type?: string // "native" or "clip" (only for shorts)
  source_mode?: string // "original" or "viral"
}

interface WeekDay {
  date: string
  weekday: string
  total: number
  slots: Slot[]
  shorts_total?: number
  shorts_slots?: Slot[]
}

// ── Shorts type colors ─────────────────────────────────
const SHORTS_TYPE_COLORS: Record<string, string> = {
  native: 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30',
  clip: 'bg-orange-400/15 text-orange-400 border-orange-400/30',
}
const SHORTS_TYPE_DOT: Record<string, string> = {
  native: 'bg-emerald-400',
  clip: 'bg-orange-400',
}
const SHORTS_TYPE_LABEL: Record<string, string> = {
  native: 'Nativo',
  clip: 'Clip',
}

// ── Helpers ────────────────────────────────────────────
// DB stores Europe/Madrid local time strings (e.g. "2026-07-13 21:00:00")

function toLocalTime(ts: string | null | undefined): string {
  if (!ts) return '??:??'
  // Parse as local time — supported formats: "YYYY-MM-DD HH:MM:SS", "YYYY-MM-DDTHH:MM:SS", "HH:MM:SS"
  const m = ts.match(/(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}` : ts.slice(0, 5)
}

function statusBadge(status: string, progress?: number) {
  switch (status) {
    case 'completed':
      return (
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-400/20 text-green-400 border border-green-400/30 flex items-center gap-1">
          <CheckCircle2 size={12} /> completado
        </span>
      )
    case 'running':
      return (
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-neon-cyan/20 text-neon-cyan border border-neon-cyan/30 flex items-center gap-1">
          <Loader2 size={12} className="animate-spin" />
          ejecutando
          {progress !== undefined && (
            <span className="ml-1 opacity-70">{progress}%</span>
          )}
        </span>
      )
    case 'cancelled':
      return (
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-400/10 text-red-400 border border-red-400/20 flex items-center gap-1">
          <XCircle size={12} /> cancelado
        </span>
      )
    default: // pending
      return (
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-amber-400/15 text-amber-400 border border-amber-400/20 flex items-center gap-1">
          <Clock size={12} /> pendiente
        </span>
      )
  }
}

export default function DailySchedule() {
  const [slots, setSlots] = useState<Slot[]>([])
  const [weekDays, setWeekDays] = useState<WeekDay[]>([])
  const [stats, setStats] = useState({ pending: 0, running: 0, completed: 0, cancelled: 0 })
  const [shortsStats, setShortsStats] = useState({ pending: 0, running: 0, completed: 0 })
  const [loading, setLoading] = useState(true)
  const [view, setView] = useState<'today' | 'week'>('today')
  const [filterKind, setFilterKind] = useState<'all' | 'videos' | 'shorts'>('all')
  const [hideCancelled, setHideCancelled] = useState(false)

  const load = useCallback(async () => {
    try {
      const [today, week, shortsToday, shortsWeek] = await Promise.all([
        api.getTodaySlots(),
        api.getWeekSlots(),
        api.getShortsSlotsToday(),
        api.getShortsSlotsWeek(),
      ])

      // ── Merge today's video + shorts slots ──────────
      const videoSlots: Slot[] = (today.slots || []).map((s: Slot) => ({ ...s, kind: 'video' }))
      const shortSlots: Slot[] = (shortsToday.slots || []).map((s: Slot) => ({ ...s, kind: 'short' }))
      const allSlots = [...videoSlots, ...shortSlots].sort(
        (a, b) => new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime()
      )
      setSlots(allSlots)
      setStats({
        pending: today.pending || 0,
        running: today.running || 0,
        completed: today.completed || 0,
        cancelled: today.cancelled || 0,
      })
      setShortsStats({
        pending: shortsToday.pending || 0,
        running: shortsToday.running || 0,
        completed: shortsToday.completed || 0,
      })

      // ── Merge week data ────────────────────────────
      const shortsDayMap: Record<string, { total: number; slots: Slot[] }> = {}
      for (const d of (shortsWeek.days || [])) {
        shortsDayMap[d.date] = {
          total: d.total || 0,
          slots: (d.slots || []).map((s: Slot) => ({ ...s, kind: 'short' })),
        }
      }
      const mergedWeekDays: WeekDay[] = (week.days || []).map((day: WeekDay) => {
        const sd = shortsDayMap[day.date]
        const videoSlots: Slot[] = (day.slots || []).map((s: Slot) => ({ ...s, kind: 'video' }))
        const shortSlots: Slot[] = sd?.slots || []
        return {
          ...day,
          total: (day.total || 0) + (sd?.total || 0),
          slots: [...videoSlots, ...shortSlots],
          shorts_total: sd?.total || 0,
          shorts_slots: shortSlots,
        }
      })
      setWeekDays(mergedWeekDays)
    } catch (e) {
      console.error('DailySchedule load error:', e)
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  // ── Filtered slots ──────────────────────────────────
  const baseSlots = hideCancelled ? slots.filter(s => s.status !== 'cancelled') : slots
  const filteredSlots = baseSlots.filter((s) => {
    if (filterKind === 'videos') return s.kind === 'video' || !s.kind
    if (filterKind === 'shorts') return s.kind === 'short'
    return true
  })

  const noFilteredSlots = filteredSlots.length === 0
  const hasShorts = slots.some((s) => s.kind === 'short')
  
  // Recompute display stats when hiding cancelled
  const displayStats = hideCancelled
    ? {
        pending: slots.filter(s => s.kind !== 'short' && s.status !== 'cancelled' && s.status === 'pending').length,
        running: slots.filter(s => s.kind !== 'short' && s.status !== 'cancelled' && s.status === 'running').length,
        completed: slots.filter(s => s.kind !== 'short' && s.status !== 'cancelled' && s.status === 'completed').length,
        cancelled: 0, // hidden
      }
    : stats
  const displayShortsStats = hideCancelled
    ? {
        pending: shortsStats.pending, // shorts don't have 'cancelled' in their slot data from API
        running: shortsStats.running,
        completed: shortsStats.completed,
      }
    : shortsStats

  if (loading) {
    return (
      <section className="glass rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Calendar size={18} className="text-neon-gold" />
          <h3 className="font-display text-base font-semibold text-white">Planificacion Diaria</h3>
        </div>
        <div className="flex items-center justify-center h-32">
          <Loader2 size={24} className="animate-spin text-neon-cyan" />
        </div>
      </section>
    )
  }

  return (
    <section className="glass rounded-xl p-5 space-y-4">
      {/* ── Header + view toggle ────────────────────────── */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Calendar size={18} className="text-neon-gold" />
          Planificacion Diaria
          <span className="text-xs text-gray-500 font-normal">
             (Europe/Madrid)
          </span>
        </h3>
        <div className="flex flex-wrap gap-1.5">
          {/* Kind filter toggle */}
          <div className="flex gap-0.5 bg-dark-700 rounded-lg p-0.5 mr-1">
            <button
              onClick={() => setFilterKind('videos')}
              className={`px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                filterKind === 'videos'
                  ? 'bg-neon-gold/20 text-neon-gold'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              <Play size={12} className="inline mr-1" /> Videos
            </button>
            <button
              onClick={() => setFilterKind('all')}
              className={`px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                filterKind === 'all'
                  ? 'bg-neon-gold/20 text-neon-gold'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              <Filter size={12} className="inline mr-1" /> Todos
            </button>
            <button
              onClick={() => setFilterKind('shorts')}
              className={`px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                filterKind === 'shorts'
                  ? 'bg-neon-gold/20 text-neon-gold'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              <Smartphone size={12} className="inline mr-1" /> Shorts
            </button>
          </div>
          {/* View toggle */}
          <div className="flex gap-0.5 bg-dark-700 rounded-lg p-0.5">
            <button
              onClick={() => setView('today')}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                view === 'today'
                  ? 'bg-neon-gold/20 text-neon-gold'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              <Play size={12} className="inline mr-1" /> Hoy
            </button>
            <button
              onClick={() => setView('week')}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                view === 'week'
                  ? 'bg-neon-gold/20 text-neon-gold'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              <BarChart3 size={12} className="inline mr-1" /> Semana
            </button>
          </div>
        </div>
      </div>

      {/* ── KPIs ─────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-3 items-center">
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-400/15 text-amber-400 border border-amber-400/20 flex items-center gap-1.5">
          <Clock size={14} />
          Pendientes <strong>{displayStats.pending}</strong>
        </span>
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-neon-cyan/15 text-neon-cyan border border-neon-cyan/20 flex items-center gap-1.5">
          <Loader2 size={14} className={displayStats.running > 0 ? 'animate-spin' : ''} />
          Ejecutando <strong>{displayStats.running}</strong>
        </span>
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-green-400/15 text-green-400 border border-green-400/20 flex items-center gap-1.5">
          <CheckCircle2 size={14} />
          Completados <strong>{displayStats.completed}</strong>
        </span>
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-400/10 text-red-400 border border-red-400/20 flex items-center gap-1.5">
          <XCircle size={14} />
          Cancelados <strong>{displayStats.cancelled}</strong>
        </span>
        {/* Shorts KPIs */}
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-400/15 text-purple-400 border border-purple-400/20 flex items-center gap-1.5">
          <Smartphone size={14} />
          Shorts pend. <strong>{displayShortsStats.pending}</strong>
        </span>
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-400/15 text-purple-400 border border-purple-400/20 flex items-center gap-1.5">
          <Loader2 size={14} className={displayShortsStats.running > 0 ? 'animate-spin' : ''} />
          Shorts ejec. <strong>{displayShortsStats.running}</strong>
        </span>
        <span className="px-3 py-1.5 rounded-lg text-xs font-medium bg-purple-400/15 text-purple-400 border border-purple-400/20 flex items-center gap-1.5">
          <CheckCircle2 size={14} />
          Shorts compl. <strong>{displayShortsStats.completed}</strong>
        </span>
        {/* Hide cancelled toggle */}
        <button
          onClick={() => setHideCancelled(!hideCancelled)}
          className={`px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors border flex items-center gap-1 ${
            hideCancelled
              ? 'bg-neon-gold/15 text-neon-gold border-neon-gold/30'
              : 'text-gray-500 border-transparent hover:border-gray-600 hover:text-gray-400'
          }`}
          title={hideCancelled ? 'Mostrar cancelados' : 'Ocultar cancelados'}
        >
          <X size={12} />
          {hideCancelled ? 'Cancelados ocultos' : 'Cancelados'}
        </button>
      </div>

      {/* ── Empty state ──────────────────────────────────── */}
      {view === 'today' && noFilteredSlots && (
        <div className="text-center py-8 text-gray-500 space-y-2">
          <Calendar size={32} className="mx-auto opacity-30" />
          <p className="text-sm">
            {filterKind === 'shorts'
              ? 'No hay Shorts programados para hoy.'
              : filterKind === 'videos'
              ? 'No hay videos programados para hoy.'
              : 'No hay horarios programados para hoy.'}
          </p>
          <p className="text-xs opacity-60">
            {filterKind === 'all' || filterKind === 'videos'
              ? 'El sistema genera automaticamente 2 videos/dia por canal en las franjas de ~16:00 y ~21:30 (CEST).'
              : 'Activa la planificacion de Shorts desde la seccion inferior para comenzar.'}
          </p>
        </div>
      )}

      {/* ── Today Timeline ──────────────────────────────── */}
      {view === 'today' && !noFilteredSlots && (
        <div className="space-y-2">
          {filteredSlots.map((s, i) => {
            const isShort = s.kind === 'short'
            const isPast = s.scheduled_at && new Date(s.scheduled_at) < new Date() && s.status === 'pending'
            const shortColor = isShort ? SHORTS_TYPE_COLORS[s.short_type || 'native'] : null
            return (
              <div
                key={`${s.kind || 'v'}-${s.id}`}
                className={`flex items-center gap-3 p-3 rounded-lg border transition-opacity ${
                  s.status === 'running'
                    ? 'bg-neon-cyan/5 border-neon-cyan/30'
                    : isPast
                    ? 'bg-dark-700/30 border-surface-border/50 opacity-60'
                    : isShort
                    ? 'bg-dark-700/50 border-surface-border'
                    : 'bg-dark-700/50 border-surface-border'
                }`}
              >
                {/* Kind badge */}
                <span className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium border flex items-center gap-1 ${
                  isShort
                    ? (SHORTS_TYPE_COLORS[s.short_type || 'native'] || 'bg-gray-500/20 text-gray-400 border-gray-500/30')
                    : 'bg-neon-gold/15 text-neon-gold border-neon-gold/20'
                }`}>
                  {isShort ? (
                    s.short_type === 'clip'
                      ? <Scissors size={10} />
                      : <Smartphone size={10} />
                  ) : (
                    <Play size={10} />
                  )}
                  {isShort ? SHORTS_TYPE_LABEL[s.short_type || 'native'] : 'Video'}
                </span>

                {/* Position */}
                <span className="text-xs text-gray-500 w-5 text-center font-mono">
                  #{s.slot_position}
                </span>

                {/* Time arrow */}
                <div className="flex items-center gap-1.5 text-sm font-mono min-w-[110px]">
                  <span className={isShort ? 'text-gray-500' : 'text-gray-400'}>
                    {toLocalTime(s.scheduled_at)}
                  </span>
                  <span className="text-gray-600">{'→'}</span>
                  <span
                    className={
                      s.status === 'completed'
                        ? 'text-green-400'
                        : s.status === 'running'
                        ? 'text-white'
                        : 'text-gray-300'
                    }
                  >
                    {toLocalTime(s.target_upload_at)}
                  </span>
                </div>

                {/* Channel pill */}
                <span
                  className={`px-2 py-0.5 rounded text-xs font-medium border ${
                    CHANNEL_PILL[s.channel_slug] || DEFAULT_PILL
                  }`}
                >
                  <span className={`inline-block w-2 h-2 rounded-full mr-1 ${
                    CHANNEL_DOT[s.channel_slug] || 'bg-gray-400'
                  }`} />
                  {CHANNEL_SHORT[s.channel_slug] || s.channel_name}
                </span>

                {/* Source Mode */}
                {s.kind !== 'short' && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border cursor-pointer transition-all ${
                    (s.source_mode || 'original') === 'viral'
                      ? 'bg-purple-500/15 text-purple-400 border-purple-500/30'
                      : 'bg-gray-500/15 text-gray-500 border-gray-500/30'
                  }`}
                  title="Click para cambiar metodo"
                  onClick={async () => {
                    const newMode = (s.source_mode || 'original') === 'original' ? 'viral' : 'original'
                    try { await api.updateSlotSourceMode(s.id, newMode); s.source_mode = newMode } catch (_) {}
                  }}>
                    {(s.source_mode || 'original') === 'viral' ? 'Viral' : 'Orig'}
                  </span>
                )}

                {/* Spacer */}
                <div className="flex-1" />

                {/* Progress bar for running */}
                {s.status === 'running' && (
                  <div className="w-20 h-1.5 bg-dark-600 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-neon-cyan rounded-full animate-pulse"
                      style={{ width: '60%' }}
                    />
                  </div>
                )}
                {statusBadge(s.status)}
              </div>
            )
          })}
        </div>
      )}

      {/* ── Week Grid ───────────────────────────────────── */}
      {view === 'week' && weekDays.length > 0 && (
        <div className="overflow-x-auto -mx-1 pb-2">
          <div
            className="grid gap-2"
            style={{
              gridTemplateColumns: `repeat(${weekDays.length}, minmax(100px, 1fr))`,
              minWidth: `${weekDays.length * 110}px`,
            }}
          >
            {weekDays.map((day) => {
              const today = new Date().toISOString().slice(0, 10)
              const isToday = day.date === today

              // Group video slots by channel
              const videoSlots = (day.slots || []).filter((s) => s.kind !== 'short')
              const shortSlots = day.shorts_slots || []
              const byChannel: Record<string, Slot[]> = {}
              for (const s of videoSlots) {
                if (!byChannel[s.channel_slug]) byChannel[s.channel_slug] = []
                byChannel[s.channel_slug].push(s)
              }

              return (
                <div
                  key={day.date}
                  className={`rounded-lg p-2.5 border ${
                    isToday
                      ? 'border-neon-gold/40 bg-neon-gold/5'
                      : 'border-surface-border/50 bg-dark-700/30'
                  }`}
                >
                  {/* Day header */}
                  <div className="text-center mb-2">
                    <div className={`text-xs font-medium uppercase tracking-wide ${
                      isToday ? 'text-neon-gold' : 'text-gray-400'
                    }`}>
                      {day.weekday}
                    </div>
                    <div className={`text-lg font-bold ${
                      isToday ? 'text-neon-gold' : 'text-white'
                    }`}>
                      {day.date.slice(8)}
                    </div>
                  </div>

                  {/* Video slots */}
                  <div className="space-y-1.5">
                    {Object.entries(byChannel).length === 0 && shortSlots.length === 0 && (
                      <div className="text-xs text-gray-600 text-center py-2">-</div>
                    )}
                    {Object.entries(byChannel).map(([slug, chSlots]) =>
                      chSlots.map((s) => (
                        <div
                          key={s.id}
                          className={`text-xs px-2 py-1 rounded border flex items-center justify-between ${
                            CHANNEL_PILL[slug] || DEFAULT_PILL
                          }`}
                        >
                          <span className="truncate">
                            {CHANNEL_SHORT[slug] || slug}
                          </span>
                          <span className="font-mono opacity-80 tabular-nums">
                            {toLocalTime(s.target_upload_at)}
                          </span>
                        </div>
                      ))
                    )}
                  </div>

                  {/* Shorts count badge */}
                  {shortSlots.length > 0 && (
                    <div className="mt-2 pt-1.5 border-t border-surface-border/40">
                      <div className="flex items-center gap-1.5 justify-center">
                        <Smartphone size={11} className="text-emerald-400" />
                        <span className="text-[10px] text-emerald-400 font-medium">
                          {shortSlots.length} shorts
                        </span>
                      </div>
                      {/* Type breakdown */}
                      {(() => {
                        const nativeCount = shortSlots.filter((s) => s.short_type === 'native').length
                        const clipCount = shortSlots.filter((s) => s.short_type === 'clip').length
                        return (
                          <div className="flex items-center justify-center gap-2 mt-0.5">
                            {nativeCount > 0 && (
                              <span className="text-[9px] text-emerald-500/70">
                                {nativeCount} nat.
                              </span>
                            )}
                            {clipCount > 0 && (
                              <span className="text-[9px] text-orange-400/70">
                                {clipCount} clip
                              </span>
                            )}
                          </div>
                        )
                      })()}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </section>
  )
}
