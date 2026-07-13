/** ChannelScheduleTable — per-channel breakdown of today's planned slots.
 *  Shows videos + shorts counts, next execution time, and overlap warnings.
 */

import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Clock, Play, Smartphone, AlertTriangle, CheckCircle2, Loader2, XCircle } from 'lucide-react'

interface ChannelSummary {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos_total: number
  videos_completed: number
  videos_running: number
  videos_pending: number
  videos_cancelled: number
  shorts_total: number
  shorts_completed: number
  shorts_running: number
  shorts_pending: number
  next_slot_time: string | null
  next_slot_type: string | null
  next_slot_status: string | null
  has_overlap: boolean
}

const CHANNEL_COLORS: Record<string, { bg: string; text: string; border: string; dot: string }> = {
  canal2: { bg: 'bg-neon-cyan/15', text: 'text-neon-cyan', border: 'border-neon-cyan/30', dot: 'bg-neon-cyan' },
  canal3: { bg: 'bg-amber-400/15', text: 'text-amber-400', border: 'border-amber-400/30', dot: 'bg-amber-400' },
  canal4: { bg: 'bg-purple-400/15', text: 'text-purple-400', border: 'border-purple-400/30', dot: 'bg-purple-400' },
}

const CHANNEL_SHORT: Record<string, string> = {
  canal2: 'SIN', canal3: 'CIV', canal4: 'EXP',
}

function fmtTime(ts: string | null): string {
  if (!ts) return '—'
  const match = ts.match(/T?(\d{2}):(\d{2})/)
  return match ? `${match[1]}:${match[2]}` : ts.slice(11, 16) || ts.slice(0, 5)
}

function statusIcon(status: string | null) {
  if (!status) return <Clock size={10} className="text-gray-500" />
  switch (status) {
    case 'completed': return <CheckCircle2 size={10} className="text-green-400" />
    case 'running': return <Loader2 size={10} className="text-neon-cyan animate-spin" />
    case 'cancelled': return <XCircle size={10} className="text-red-400" />
    default: return <Clock size={10} className="text-amber-400" />
  }
}

export default function ChannelScheduleTable() {
  const [channels, setChannels] = useState<ChannelSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [nextTick, setNextTick] = useState(Date.now())

  const load = useCallback(async () => {
    try {
      const [today, shortsToday] = await Promise.all([
        api.getTodaySlots(),
        api.getShortsSlotsToday(),
      ])

      // Build a map of channel summaries
      const map = new Map<number, ChannelSummary>()
      const ensure = (id: number, name: string, slug: string) => {
        if (!map.has(id)) {
          map.set(id, {
            channel_id: id, channel_name: name, channel_slug: slug,
            videos_total: 0, videos_completed: 0, videos_running: 0, videos_pending: 0, videos_cancelled: 0,
            shorts_total: 0, shorts_completed: 0, shorts_running: 0, shorts_pending: 0,
            next_slot_time: null, next_slot_type: null, next_slot_status: null, has_overlap: false,
          })
        }
        return map.get(id)!
      }

      // Process video slots
      for (const s of (today.slots || [])) {
        const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
        ch.videos_total++
        if (s.status === 'completed') ch.videos_completed++
        else if (s.status === 'running') ch.videos_running++
        else if (s.status === 'cancelled') ch.videos_cancelled++
        else ch.videos_pending++

        // Track next pending slot (earliest scheduled_at that's pending)
        if (s.status === 'pending' && s.scheduled_at) {
          if (!ch.next_slot_time || s.scheduled_at < ch.next_slot_time) {
            ch.next_slot_time = s.scheduled_at
            ch.next_slot_type = 'video'
            ch.next_slot_status = 'pending'
          }
        }
      }

      // Process shorts slots
      for (const s of (shortsToday.slots || [])) {
        const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
        ch.shorts_total++
        if (s.status === 'completed') ch.shorts_completed++
        else if (s.status === 'running') ch.shorts_running++
        else ch.shorts_pending++

        if (s.status === 'pending' && s.scheduled_at) {
          if (!ch.next_slot_time || s.scheduled_at < ch.next_slot_time) {
            ch.next_slot_time = s.scheduled_at
            ch.next_slot_type = 'short'
            ch.next_slot_status = 'pending'
          }
        }
      }

      // Detect overlap: any two ACTIVE slots (not cancelled) for same channel
      // with target_upload_at within 5 minutes of each other.
      for (const [id, ch] of map.entries()) {
        const allSlots = [
          ...(today.slots || []).filter((s: any) => s.channel_id === id),
          ...(shortsToday.slots || []).filter((s: any) => s.channel_id === id),
        ]
        // Only compare active slots (pending or running), exclude cancelled and completed
        const activeSlots = allSlots.filter((s: any) =>
          s.status === 'pending' || s.status === 'running'
        )
        for (let i = 0; i < activeSlots.length; i++) {
          for (let j = i + 1; j < activeSlots.length; j++) {
            const a = activeSlots[i].target_upload_at
            const b = activeSlots[j].target_upload_at
            if (a && b) {
              const diff = Math.abs(new Date(a).getTime() - new Date(b).getTime())
              if (diff < 5 * 60 * 1000) { ch.has_overlap = true; break }
            }
          }
        }
      }

      setChannels(Array.from(map.values()).sort((a, b) => a.channel_id - b.channel_id))
    } catch (e) {
      console.error('ChannelScheduleTable load error:', e)
    }
    setLoading(false)
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])
  useEffect(() => { const t = setInterval(() => setNextTick(Date.now()), 15000); return () => clearInterval(t) }, [])

  if (loading) return null

  const now = new Date()
  const overallNext = channels
    .filter(c => c.next_slot_time)
    .sort((a, b) => (a.next_slot_time || '').localeCompare(b.next_slot_time || ''))
  const firstNext = overallNext[0]

  // Countdown to next execution
  const countdown = firstNext?.next_slot_time
    ? (() => {
        const target = new Date(firstNext.next_slot_time!)
        const diff = target.getTime() - now.getTime()
        if (diff <= 0) return 'AHORA'
        const mins = Math.floor(diff / 60000)
        const hrs = Math.floor(mins / 60)
        if (hrs > 0) return `${hrs}h ${mins % 60}m`
        return `${mins}m`
      })()
    : null

  return (
    <section className="space-y-4">
      {/* ── Next Execution Banner ────────────────────────── */}
      {firstNext && (
        <div className={`rounded-xl p-4 border flex items-center justify-between flex-wrap gap-3 ${
          firstNext.next_slot_status === 'running'
            ? 'bg-neon-cyan/10 border-neon-cyan/40'
            : 'bg-neon-gold/10 border-neon-gold/40'
        }`}>
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-full flex items-center justify-center ${
              firstNext.next_slot_status === 'running' ? 'bg-neon-cyan/20' : 'bg-neon-gold/20'
            }`}>
              {firstNext.next_slot_status === 'running'
                ? <Loader2 size={20} className="text-neon-cyan animate-spin" />
                : <Clock size={20} className="text-neon-gold" />
              }
            </div>
            <div>
              <p className="text-xs text-gray-400 uppercase tracking-wide">Proxima Ejecucion</p>
              <p className="text-white font-semibold text-sm">
                {firstNext.next_slot_type === 'short' ? 'Short' : 'Video'}
                {' · '}
                <span className={CHANNEL_COLORS[firstNext.channel_slug]?.text}>
                  {firstNext.channel_name}
                </span>
                {' · '}
                                {fmtTime(firstNext.next_slot_time)} Europe/Madrid
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`px-3 py-1.5 rounded-lg text-xs font-bold font-mono ${
              countdown === 'AHORA'
                ? 'bg-neon-red/20 text-neon-red border border-neon-red/40'
                : 'bg-dark-600 text-neon-gold border border-neon-gold/30'
            }`}>
              {countdown === 'AHORA' ? '⏳ AHORA' : `⏱ ${countdown}`}
            </span>
          </div>
        </div>
      )}

      {/* ── Channel Summary Table ─────────────────────────── */}
      <div className="glass rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-surface-border bg-dark-700/50">
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Canal</th>
                <th className="text-center px-3 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">
                  <div className="flex items-center justify-center gap-1"><Play size={11} /> Videos</div>
                </th>
                <th className="text-center px-3 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">
                  <div className="flex items-center justify-center gap-1"><Smartphone size={11} /> Shorts</div>
                </th>
                <th className="text-center px-3 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Proximo</th>
                <th className="text-center px-3 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Estado</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-border/50">
              {channels.map((ch) => {
                const colors = CHANNEL_COLORS[ch.channel_slug] || { bg: 'bg-gray-500/15', text: 'text-gray-400', border: 'border-gray-500/30', dot: 'bg-gray-400' }
                const videoDone = ch.videos_completed
                const videoTotal = ch.videos_completed + ch.videos_pending + ch.videos_running
                const shortDone = ch.shorts_completed
                const shortTotal = ch.shorts_completed + ch.shorts_pending + ch.shorts_running
                const hasActive = ch.videos_running > 0 || ch.shorts_running > 0

                return (
                  <tr key={ch.channel_id} className={`hover:bg-dark-700/30 transition-colors ${hasActive ? 'bg-neon-cyan/5' : ''}`}>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
                        <span className="text-white font-medium text-xs">{ch.channel_name}</span>
                        <span className="text-[10px] text-gray-600 font-mono">({CHANNEL_SHORT[ch.channel_slug]})</span>
                      </div>
                    </td>
                    <td className="px-3 py-3 text-center">
                      {videoTotal > 0 ? (
                        <div className="flex items-center justify-center gap-1.5">
                          <span className={`font-mono font-bold text-xs ${videoDone >= videoTotal ? 'text-green-400' : 'text-white'}`}>
                            {videoDone}/{videoTotal}
                          </span>
                          {ch.videos_running > 0 && <Loader2 size={10} className="text-neon-cyan animate-spin" />}
                          {ch.videos_cancelled > 0 && (
                            <span className="text-[9px] text-red-400/70">({ch.videos_cancelled} canc.)</span>
                          )}
                        </div>
                      ) : (
                        <span className="text-gray-600 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-center">
                      {shortTotal > 0 ? (
                        <div className="flex items-center justify-center gap-1.5">
                          <span className={`font-mono font-bold text-xs ${shortDone >= shortTotal ? 'text-green-400' : shortDone > 0 ? 'text-emerald-400' : 'text-gray-400'}`}>
                            {shortDone}/{shortTotal}
                          </span>
                          {ch.shorts_running > 0 && <Loader2 size={10} className="text-neon-cyan animate-spin" />}
                        </div>
                      ) : (
                        <span className="text-gray-600 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-center">
                      {ch.next_slot_time ? (
                        <div className="flex items-center justify-center gap-1.5">
                          <span className="font-mono text-xs text-neon-gold font-medium">
                            {fmtTime(ch.next_slot_time)}
                          </span>
                          <span className={`text-[9px] px-1.5 py-0.5 rounded ${ch.next_slot_type === 'short' ? 'bg-emerald-400/15 text-emerald-400' : 'bg-neon-gold/15 text-neon-gold'}`}>
                            {ch.next_slot_type === 'short' ? 'Short' : 'Video'}
                          </span>
                        </div>
                      ) : (
                        <span className="text-gray-600 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-center">
                      {hasActive ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-neon-cyan/15 text-neon-cyan text-[10px] font-medium border border-neon-cyan/20">
                          <Loader2 size={9} className="animate-spin" /> ejecutando
                        </span>
                      ) : ch.videos_completed + ch.shorts_completed >= ch.videos_total + ch.shorts_total - ch.videos_cancelled ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-green-400/15 text-green-400 text-[10px] font-medium border border-green-400/20">
                          <CheckCircle2 size={9} /> completado
                        </span>
                      ) : ch.videos_total + ch.shorts_total > 0 ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-400/15 text-amber-400 text-[10px] font-medium border border-amber-400/20">
                          <Clock size={9} /> pendiente
                        </span>
                      ) : (
                        <span className="text-gray-600 text-[10px]">sin plan</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* Overlap warnings */}
        {channels.some(c => c.has_overlap) && (
          <div className="px-4 py-2 border-t border-red-400/20 bg-red-400/5 flex items-center gap-2 text-xs text-red-400">
            <AlertTriangle size={12} />
            Se detecto solapamiento en algunos canales. Revisa la planificacion.
          </div>
        )}

        {/* No channels planned */}
        {channels.length === 0 && (
          <div className="px-4 py-8 text-center text-gray-500 text-xs">
            <Clock size={24} className="mx-auto mb-2 opacity-30" />
            No hay planificacion para hoy. El sistema generara slots automaticamente.
          </div>
        )}
      </div>
    </section>
  )
}
