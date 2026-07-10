/** UpcomingExecutions — Show all FUTURE pending slots (videos + shorts) for next 7 days.
 *  Clear table sorted by time, with countdown to the next execution.
 *  Filters: by channel, by type (videos/shorts/all).
 */

import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Clock, Play, Smartphone, Loader2, Filter } from 'lucide-react'

interface Slot {
  id: number
  channel_id: number
  channel_name: string
  channel_slug: string
  scheduled_at: string
  target_upload_at: string
  status: string
  slot_position: number
  kind: string
  short_type?: string
}

// ── Timezone helper ──────────────────────────────────────
const TZ_OFFSET = 1  // GMT+1 (CET) — fixed, no DST

function toLocal(utcStr: string): string {
  const match = utcStr.match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/)
  if (!match) return utcStr.slice(0, 5)
  const [, year, month, day, hour, minute] = match.map(Number)
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute))
  date.setHours(date.getHours() + TZ_OFFSET)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function toLocalFull(utcStr: string): string {
  const match = utcStr.match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/)
  if (!match) return utcStr
  const [, year, month, day, hour, minute] = match.map(Number)
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute))
  date.setHours(date.getHours() + TZ_OFFSET)
  return date.toLocaleString('es-ES', {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function fmtTime(ts: string): string {
  if (!ts) return '??:??'
  return toLocal(ts)
}

// ── Channel colors ───────────────────────────────────────
const CHANNEL_COLORS: Record<string, string> = {
  canal2: 'bg-neon-cyan/15 text-neon-cyan border-neon-cyan/30',
  canal3: 'bg-amber-400/15 text-amber-400 border-amber-400/30',
  canal4: 'bg-purple-400/15 text-purple-400 border-purple-400/30',
}
const CHANNEL_DOT: Record<string, string> = {
  canal2: 'bg-neon-cyan', canal3: 'bg-amber-400', canal4: 'bg-purple-400',
}
const CHANNEL_SHORT: Record<string, string> = {
  canal2: 'SIN', canal3: 'CIV', canal4: 'EXP',
}

export default function UpcomingExecutions() {
  const [slots, setSlots] = useState<Slot[]>([])
  const [loading, setLoading] = useState(true)
  const [filterChannel, setFilterChannel] = useState<number>(0)
  const [filterKind, setFilterKind] = useState<'all' | 'videos' | 'shorts'>('all')
  const [now, setNow] = useState(Date.now())

  const load = useCallback(async () => {
    try {
      const [week, shortsWeek] = await Promise.all([
        api.getWeekSlots(),
        api.getShortsSlotsWeek(),
      ])

      // Collect all future pending slots
      const allSlots: Slot[] = []
      const todayUtc = new Date().toISOString().slice(0, 10)

      for (const day of (week.days || [])) {
        for (const s of (day.slots || [])) {
          if (s.status === 'pending' && s.scheduled_at > new Date().toISOString()) {
            allSlots.push({ ...s, kind: 'video' })
          }
        }
      }
      for (const day of (shortsWeek.days || [])) {
        for (const s of (day.slots || [])) {
          if (s.status === 'pending' && s.scheduled_at > new Date().toISOString()) {
            allSlots.push({ ...s, kind: 'short' })
          }
        }
      }

      // Sort by scheduled_at
      allSlots.sort((a, b) => (a.scheduled_at || '').localeCompare(b.scheduled_at || ''))
      setSlots(allSlots)
    } catch (e) {
      console.error('UpcomingExecutions load error:', e)
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 60000) // refresh every minute
    return () => clearInterval(t)
  }, [load])

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15000)
    return () => clearInterval(t)
  }, [])

  // ── Filtering ────────────────────────────────────────────
  const filtered = slots.filter((s) => {
    if (filterChannel && s.channel_id !== filterChannel) return false
    if (filterKind === 'videos' && s.kind !== 'video') return false
    if (filterKind === 'shorts' && s.kind !== 'short') return false
    return true
  })

  // ── Group by date ────────────────────────────────────────
  const groups = new Map<string, Slot[]>()
  for (const s of filtered) {
    const dateKey = (s.scheduled_at || '').slice(0, 10)
    if (!groups.has(dateKey)) groups.set(dateKey, [])
    groups.get(dateKey)!.push(s)
  }

  const tzLabel = 'GMT+1'

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 size={20} className="animate-spin text-gray-600" />
      </div>
    )
  }

  return (
    <section className="glass rounded-xl p-5 space-y-4">
      {/* ── Header + filters ────────────────────────────── */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Clock size={18} className="text-neon-gold" />
          Proximas Ejecuciones
          <span className="text-xs text-gray-500 font-normal">
            ({tzLabel})
          </span>
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          {/* Kind filter */}
          <div className="flex gap-0.5 bg-dark-700 rounded-lg p-0.5">
            {(['videos', 'all', 'shorts'] as const).map((k) => (
              <button
                key={k}
                onClick={() => setFilterKind(k)}
                className={`px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  filterKind === k
                    ? 'bg-neon-gold/20 text-neon-gold'
                    : 'text-gray-400 hover:text-white'
                }`}
              >
                {k === 'videos' ? <Play size={12} className="inline mr-1" /> : null}
                {k === 'shorts' ? <Smartphone size={12} className="inline mr-1" /> : null}
                {k === 'all' ? <Filter size={12} className="inline mr-1" /> : null}
                {k === 'videos' ? 'Videos' : k === 'shorts' ? 'Shorts' : 'Todos'}
              </button>
            ))}
          </div>
          {/* Channel filter */}
          <select
            value={filterChannel}
            onChange={(e) => setFilterChannel(Number(e.target.value))}
            className="px-2 py-1.5 bg-dark-700 rounded-lg text-xs text-gray-400 border border-surface-border"
          >
            <option value={0}>Todos los canales</option>
            <option value={3}>Sincronias</option>
            <option value={4}>Civilizaciones</option>
            <option value={5}>Expediciones</option>
          </select>
        </div>
      </div>

      {/* ── No slots message ──────────────────────────────── */}
      {filtered.length === 0 && (
        <div className="text-center py-8 text-gray-500">
          <Clock size={32} className="mx-auto mb-2 opacity-30" />
          <p className="text-sm">No hay ejecuciones futuras programadas.</p>
          <p className="text-xs opacity-60 mt-1">
            El sistema generara slots automaticamente. Asegurate de que la planificacion este activada.
          </p>
        </div>
      )}

      {/* ── Table grouped by day ──────────────────────────── */}
      {Array.from(groups.entries()).map(([dateKey, daySlots]) => {
        const first = daySlots[0]
        const countdown = (() => {
          const target = new Date(first.scheduled_at)
          const diff = target.getTime() - now
          if (diff <= 0) return 'AHORA'
          const mins = Math.floor(diff / 60000)
          const hrs = Math.floor(mins / 60)
          if (hrs > 0) return `${hrs}h ${mins % 60}m`
          return `${mins}m`
        })()

        return (
          <div key={dateKey} className="space-y-2">
            {/* Day header */}
            <div className="flex items-center justify-between px-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-white">
                  {toLocalFull(dateKey + ' 00:00:00').split(',')[0]}
                </span>
                <span className="text-xs text-gray-500">
                  {daySlots.length} ejecucion{daySlots.length !== 1 ? 'es' : ''}
                </span>
              </div>
              <span className={`px-2.5 py-1 rounded-lg text-xs font-bold font-mono ${
                countdown === 'AHORA'
                  ? 'bg-neon-red/20 text-neon-red border border-neon-red/40'
                  : 'bg-dark-600 text-neon-gold border border-neon-gold/30'
              }`}>
                {countdown === 'AHORA' ? 'AHORA' : `en ${countdown}`}
              </span>
            </div>

            {/* Slots table */}
            <div className="overflow-x-auto rounded-lg border border-surface-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-dark-700/50 border-b border-surface-border">
                    <th className="text-left px-3 py-2 text-gray-400 font-medium uppercase tracking-wider">
                      <div className="flex items-center gap-1"><Play size={10} /> Tipo</div>
                    </th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium uppercase tracking-wider">
                      Hora
                    </th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium uppercase tracking-wider">
                      Subida est.
                    </th>
                    <th className="text-left px-3 py-2 text-gray-400 font-medium uppercase tracking-wider">
                      Canal
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-border/30">
                  {daySlots.map((s) => {
                    const isShort = s.kind === 'short'
                    return (
                      <tr key={`${s.kind}-${s.id}`} className="hover:bg-dark-700/30 transition-colors">
                        <td className="px-3 py-2">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${
                            isShort
                              ? 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30'
                              : 'bg-neon-gold/15 text-neon-gold border-neon-gold/30'
                          }`}>
                            {isShort ? (
                              s.short_type === 'clip' ? 'Clip' : 'Short'
                            ) : (
                              'Video'
                            )}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono text-white tabular-nums">
                          {fmtTime(s.scheduled_at)}
                        </td>
                        <td className="px-3 py-2 font-mono text-gray-400 tabular-nums">
                          {fmtTime(s.target_upload_at)}
                        </td>
                        <td className="px-3 py-2">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-medium border ${
                            CHANNEL_COLORS[s.channel_slug] || 'bg-gray-500/15 text-gray-400 border-gray-500/30'
                          }`}>
                            <span className={`inline-block w-1.5 h-1.5 rounded-full mr-1 ${
                              CHANNEL_DOT[s.channel_slug] || 'bg-gray-400'
                            }`} />
                            {CHANNEL_SHORT[s.channel_slug] || s.channel_name}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
    </section>
  )
}
