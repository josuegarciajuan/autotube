/** HorariosTab — full timing dashboard for a channel.
 *  Shows optimal slots, upload/publication windows,
 *  execution history scatter chart, and performance stats.
 *  Pure SVG charts — zero dependencies beyond lucide-react.
 */

import { useEffect, useState } from 'react'
import {
  Clock, TrendingUp, Zap, Calendar, Upload, Globe, Loader2,
  Target, BarChart3, ShieldCheck, Timer, AlertTriangle, ArrowDown,
} from 'lucide-react'
import {
  api,
  TimingDashboardResponse,
  OptimalSlot,
  TimingConfig,
  ExecutionEvent,
  TimingStats,
  parseApiDate,
  API_TIME_ZONE,
  formatApiDate,
} from '../lib/api'

// ── helpers ──────────────────────────────────────────────────

function pad(n: number) { return String(n).padStart(2, '0') }

function fmtHourMin(h: number, m: number) {
  return `${pad(h)}:${pad(m)}`
}

function fmtHourRange(h1: number, h2: number) {
  return `${pad(h1)}:00 – ${pad(h2)}:00`
}

function fmtJitterRange(hour: number, min: number, jitter: number) {
  const base = hour * 60 + min
  const lo = base - jitter
  const hi = base + jitter
  const loStr = `${pad(Math.floor(((lo % 1440) + 1440) % 1440 / 60))}:${pad(((lo % 1440) + 1440) % 1440 % 60)}`
  const hiStr = `${pad(Math.floor(((hi % 1440) + 1440) % 1440 / 60))}:${pad(((hi % 1440) + 1440) % 1440 % 60)}`
  return `~${loStr} – ${hiStr} (±${jitter}min)`
}

function getEventHour(dtStr: string | null): number | null {
  if (!dtStr) return null
  try {
    const d = parseApiDate(dtStr)
    if (!d) return null
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: API_TIME_ZONE, hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(d)
    const hour = Number(parts.find(p => p.type === 'hour')?.value)
    const minute = Number(parts.find(p => p.type === 'minute')?.value)
    return Number.isFinite(hour) && Number.isFinite(minute) ? hour + minute / 60 : null
  } catch { return null }
}

function getEventDate(dtStr: string | null): string | null {
  if (!dtStr) return null
  try {
    const d = parseApiDate(dtStr)
    if (!d) return null
    const parts = new Intl.DateTimeFormat('es-ES', {
      timeZone: API_TIME_ZONE, day: 'numeric', month: 'short',
    }).formatToParts(d)
    const day = parts.find(p => p.type === 'day')?.value
    const month = parts.find(p => p.type === 'month')?.value
    return day && month ? `${day} ${month}` : null
  } catch { return null }
}

function closestUploadWindow(slotHour: number, windows: {start:number,end:number}[], warmupMin: number): {start:number,end:number} | null {
  const uploadBy = slotHour - warmupMin / 60
  // Find window that ends before uploadBy (on same day), or wraps to previous day
  const same = windows.filter(w => w.end <= uploadBy)
  if (same.length > 0) return same[same.length - 1]
  // Wrap: pick last window (previous day equivalent)
  if (windows.length > 0) return windows[windows.length - 1]
  return null
}

function formatSlotTime(utcStr: string | null): string {
  if (!utcStr) return '—'
  try {
    const d = parseApiDate(utcStr)
    if (!d) return '—'
    return formatApiDate(utcStr, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  } catch { return '—' }
}

// ── colors ───────────────────────────────────────────────────

const COLORS = {
  uploadBand: 'rgba(245,158,11,0.12)',
  uploadBorder: 'rgba(245,158,11,0.35)',
  pubLine: '#a855f7',
  pubLineDash: 'rgba(168,85,247,0.5)',
  pubJitter: 'rgba(168,85,247,0.08)',
  uploadDot: '#60a5fa',
  uploadDotShorts: '#f472b6',
  publishDot: '#34d399',
  publishDotShorts: '#fbbf24',
  gridLine: 'rgba(75,85,99,0.15)',
  text: '#9ca3af',
  textBright: '#d1d5db',
  cardBg: 'rgba(15,15,22,0.7)',
}

// ═══════════════════════════════════════════════════════════════
// Main component
// ═══════════════════════════════════════════════════════════════

interface Props {
  channelId: number
}

export default function HorariosTab({ channelId }: Props) {
  const [data, setData] = useState<TimingDashboardResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(90)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getTimingDashboard(channelId, days)
      setData(res)
    } catch (e: any) {
      setError(e.message || 'Error al cargar datos de horarios')
    }
    setLoading(false)
  }

  useEffect(() => { load() }, [channelId, days])

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 size={24} className="text-purple-400 animate-spin mr-3" />
        <span className="text-gray-400 text-sm">Cargando horarios...</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="glass rounded-xl p-6 text-center">
        <AlertTriangle size={20} className="text-amber-400 mx-auto mb-2" />
        <p className="text-gray-400 text-sm">{error}</p>
        <button onClick={load} className="mt-3 text-purple-400 text-xs hover:underline">Reintentar</button>
      </div>
    )
  }

  if (!data) return null

  const { config, optimal_slots, execution_history, stats } = data

  return (
    <div className="space-y-6">
      {/* ── Config summary bar ── */}
      <ConfigBar config={config} channelId={channelId} days={days} onDaysChange={setDays} />

      {config.publish_mode === 'scheduled' ? (
        <>
          {/* ── Long-form section ── */}
          <SlotSection
            title="🎬 Vídeos largos"
            icon={<Upload size={16} className="text-blue-400" />}
            slots={optimal_slots.long}
            config={config}
            accentColor="#60a5fa"
          />

          {/* ── Shorts section ── */}
          <SlotSection
            title="📱 Shorts"
            icon={<Zap size={16} className="text-pink-400" />}
            slots={optimal_slots.shorts}
            config={config}
            accentColor="#f472b6"
          />

          {/* ── Execution scatter ── */}
          <ExecutionScatter
            events={execution_history}
            config={config}
            slots={optimal_slots}
          />

          {/* ── KPI stats ── */}
          <StatsCards stats={stats} />
        </>
      ) : (
        <div className="glass rounded-xl p-8 text-center">
          <Globe size={28} className="text-gray-500 mx-auto mb-3" />
          <h3 className="text-sm font-semibold text-gray-400 mb-1">Modo inmediato activo</h3>
          <p className="text-xs text-gray-500 max-w-md mx-auto">
            Las franjas óptimas y el dashboard de horarios solo aplican en modo <strong>programado</strong>.
            Activa el modo programado desde la configuración del canal para usar publicación en hora pico
            y ver el análisis de rendimiento por franja horaria.
          </p>
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// ConfigBar — summary of current timing config
// ═══════════════════════════════════════════════════════════════

function ConfigBar({ config, channelId, days, onDaysChange }: {
  config: TimingConfig
  channelId: number
  days: number
  onDaysChange: (d: number) => void
}) {
  const items = [
    {
      icon: <Clock size={13} />,
      label: config.publish_mode === 'scheduled' ? 'Programado' : 'Inmediato',
      color: config.publish_mode === 'scheduled' ? 'text-green-400' : 'text-amber-400',
    },
    {
      icon: <Globe size={13} />,
      label: config.publish_timezone?.replace('Europe/', '').replace('America/', '') || '—',
      color: 'text-gray-400',
    },
    {
      icon: <ShieldCheck size={13} />,
      label: `Warmup ${config.publish_warmup_min}min`,
      color: 'text-gray-400',
    },
    {
      icon: <Target size={13} />,
      label: config.publish_target_hour != null
        ? `Pico ${pad(config.publish_target_hour)}:00 (±${config.publish_jitter_min}min)`
        : 'Sin pico configurado',
      color: config.publish_target_hour != null ? 'text-purple-400' : 'text-gray-500',
    },
    {
      icon: <BarChart3 size={13} />,
      label: config.upload_windows?.map(w => fmtHourRange(w.start, w.end)).join(' · ') || '—',
      color: 'text-amber-400/70',
    },
  ]

  return (
    <div className="glass rounded-xl p-4">
      <div className="flex items-center gap-4 flex-wrap">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-1.5 text-[11px]">
            <span className={it.color}>{it.icon}</span>
            <span className="text-gray-400">{it.label}</span>
          </div>
        ))}
        {/* Days selector */}
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-[10px] text-gray-600">historial</span>
          {[30, 60, 90, 180].map(d => (
            <button
              key={d}
              onClick={() => onDaysChange(d)}
              className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                days === d ? 'bg-purple-500/20 text-purple-400' : 'text-gray-600 hover:text-gray-400'
              }`}
            >{d}d</button>
          ))}
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// SlotSection — timeline + slot cards for one content type
// ═══════════════════════════════════════════════════════════════

function SlotSection({ title, icon, slots, config, accentColor }: {
  title: string
  icon: React.ReactNode
  slots: OptimalSlot[]
  config: TimingConfig
  accentColor: string
}) {
  if (!slots || slots.length === 0) {
    return (
      <div className="glass rounded-xl p-5 text-center">
        <p className="text-xs text-gray-600">Sin franjas calculadas aún para {title?.split(' ')[1] || ''}.</p>
      </div>
    )
  }

  return (
    <div className="glass rounded-xl p-5 space-y-4">
      {/* Section header */}
      <div className="flex items-center gap-2">
        {icon}
        <h3 className="text-sm font-semibold text-gray-200">{title}</h3>
        <span className="text-[10px] text-gray-600">{slots.length} franjas</span>
      </div>

      {/* ── 24h timeline bar ── */}
      <TimelineBar slots={slots} config={config} accentColor={accentColor} />

      {/* ── Slot cards grid ── */}
      <div className={`grid gap-3 ${slots.length <= 3 ? 'grid-cols-3' : 'grid-cols-4'}`}>
        {slots.map(s => (
          <SlotCard key={s.rank} slot={s} config={config} accentColor={accentColor} />
        ))}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// TimelineBar — 24-hour horizontal bar SVG
// ═══════════════════════════════════════════════════════════════

function TimelineBar({ slots, config, accentColor }: {
  slots: OptimalSlot[]
  config: TimingConfig
  accentColor: string
}) {
  const H = 36
  const padX = 28
  const barH = 14
  const barY = (H - barH) / 2
  const innerW = 100 // percentage-based

  const uploadWindows = config.upload_windows || [{ start: 9, end: 11 }]

  return (
    <div className="w-full overflow-x-auto">
      <svg viewBox={`0 0 700 ${H}`} className="w-full" style={{ minWidth: 500 }}>
        {/* Hour grid & labels */}
        {[0, 3, 6, 9, 12, 15, 18, 21].map(h => {
          const x = padX + (h / 24) * (700 - padX * 2)
          return (
            <g key={h}>
              <line x1={x} y1={0} x2={x} y2={H} stroke={COLORS.gridLine} strokeWidth={0.5} />
              <text x={x} y={H - 4} textAnchor="middle" fill={COLORS.text} fontSize={8} fontFamily="monospace">
                {pad(h)}h
              </text>
            </g>
          )
        })}

        {/* Upload window bands */}
        {uploadWindows.map((w, i) => {
          const x1 = padX + (w.start / 24) * (700 - padX * 2)
          const w24 = w.end <= w.start ? 24 - w.start + w.end : w.end - w.start
          const width = (w24 / 24) * (700 - padX * 2)
          return (
            <rect key={`uw-${i}`} x={x1} y={barY} width={width} height={barH}
              rx={3} fill={COLORS.uploadBand} stroke={COLORS.uploadBorder} strokeWidth={0.5} />
          )
        })}

        {/* Slot markers */}
        {slots.map((s, i) => {
          const targetMin = s.target_hour + s.target_minute / 60
          const x = padX + (targetMin / 24) * (700 - padX * 2)
          const jitter = config.publish_jitter_min
          const jLo = targetMin - jitter / 60
          const jHi = targetMin + jitter / 60
          const jx1 = padX + (jLo / 24) * (700 - padX * 2)
          const jWidth = ((jHi - jLo) / 24) * (700 - padX * 2)

          return (
            <g key={s.rank}>
              {/* Jitter band */}
              <rect x={jx1} y={barY + 1} width={jWidth} height={barH - 2}
                rx={2} fill={COLORS.pubJitter} />
              {/* Diamond marker */}
              <polygon
                points={`${x},${barY - 4} ${x + 5},${barY + barH / 2} ${x},${barY + barH + 4} ${x - 5},${barY + barH / 2}`}
                fill={accentColor} opacity={0.9}
              />
              {/* Hour label below diamond */}
              <text x={x} y={barY - 6} textAnchor="middle" fill={accentColor} fontSize={7} fontWeight={600}>
                {fmtHourMin(s.target_hour, s.target_minute)}
              </text>
            </g>
          )
        })}
      </svg>

      {/* Legend */}
      <div className="flex items-center gap-3 text-[9px] text-gray-600 mt-1 px-1">
        <span className="flex items-center gap-1">
          <span className="w-2.5 h-2.5 rounded-sm bg-amber-500/20 border border-amber-500/35" />
          Ventana subida
        </span>
        <span className="flex items-center gap-1">
          <span className="w-2.5 h-2.5 rounded-sm bg-purple-500/10" />
          Franja publicación (±jitter)
        </span>
        <span className="flex items-center gap-1">
          <span style={{ color: accentColor }}>◆</span>
          Hora óptima
        </span>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// SlotCard — enhanced individual slot card
// ═══════════════════════════════════════════════════════════════

function SlotCard({ slot, config, accentColor }: {
  slot: OptimalSlot
  config: TimingConfig
  accentColor: string
}) {
  const rankEmoji: Record<number, string> = { 1: '🥇', 2: '🥈', 3: '🥉', 4: '4️⃣' }
  const focusLabel: Record<string, string> = { spain: '🇪🇸 Spain', latam: '🌎 LATAM', blend: '🌍 Blend' }

  const jitter = config.publish_jitter_min
  const warmupMin = config.publish_warmup_min
  const upWin = closestUploadWindow(slot.target_hour, config.upload_windows, warmupMin)

  const confPct = Math.round((slot.confidence || 0) * 100)
  const confColor = confPct >= 80 ? 'text-green-400' : confPct >= 50 ? 'text-amber-400' : 'text-red-400'

  return (
    <div className="rounded-lg border border-white/5 bg-dark-800/60 p-3 space-y-2 hover:border-white/10 transition-colors">
      {/* Rank + time header */}
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-bold text-gray-500">{rankEmoji[slot.rank]} #{slot.rank}</span>
        <span className="text-lg font-mono font-bold tracking-tight text-gray-200">
          {fmtHourMin(slot.target_hour, slot.target_minute)}
        </span>
      </div>

      {/* Upload window */}
      <div className="space-y-1">
        <div className="flex items-center gap-1.5 text-[9px] text-gray-500">
          <Upload size={9} className="text-amber-400/60" />
          <span>Subir</span>
        </div>
        <div className="text-[10px] font-mono text-amber-300/80 pl-4">
          {upWin ? fmtHourRange(upWin.start, upWin.end) : '—'}
        </div>
      </div>

      {/* Publication window */}
      <div className="space-y-1">
        <div className="flex items-center gap-1.5 text-[9px] text-gray-500">
          <Target size={9} className="text-purple-400/60" />
          <span>Publicar</span>
        </div>
        <div className="text-[10px] font-mono text-purple-300/80 pl-4">
          {fmtJitterRange(slot.target_hour, slot.target_minute, jitter)}
        </div>
      </div>

      {/* Warmup indicator */}
      <div className="flex items-center gap-1.5 text-[9px] text-gray-500">
        <ShieldCheck size={9} className="text-gray-500" />
        <span>Privado {warmupMin}min</span>
      </div>

      {/* Confidence bar */}
      <div>
        <div className="flex items-center justify-between text-[9px] mb-0.5">
          <span className="text-gray-500">Confianza</span>
          <span className={confColor}>{confPct}%</span>
        </div>
        <div className="h-1 rounded-full bg-white/5 overflow-hidden">
          <div className="h-full rounded-full transition-all"
            style={{
              width: `${confPct}%`,
              backgroundColor: confPct >= 80 ? '#22c55e' : confPct >= 50 ? '#f59e0b' : '#ef4444',
              opacity: 0.6,
            }}
          />
        </div>
      </div>

      {/* Stats row */}
      <div className="flex items-center justify-between text-[9px] text-gray-600 pt-1 border-t border-white/5">
        <span>{slot.used_count || 0} usos</span>
        <span className="flex items-center gap-1">
          <TrendingUp size={8} />
          {slot.avg_views_result ? `${(slot.avg_views_result / 1000).toFixed(1)}K avg` : '—'}
        </span>
        <span>{focusLabel[slot.audience_focus] || slot.audience_focus}</span>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// ExecutionScatter — historical execution vs optimal times
// ═══════════════════════════════════════════════════════════════

function ExecutionScatter({ events, config, slots }: {
  events: ExecutionEvent[]
  config: TimingConfig
  slots: { long: OptimalSlot[]; shorts: OptimalSlot[] }
}) {
  if (!events || events.length === 0) {
    return (
      <div className="glass rounded-xl p-5 text-center">
        <Calendar size={18} className="text-gray-600 mx-auto mb-2" />
        <p className="text-xs text-gray-600">Sin historial de ejecución aún.</p>
      </div>
    )
  }

  // Filter events with at least one valid time
  const validEvents = events.filter(e => getEventHour(e.uploaded_at) !== null || getEventHour(e.published_at) !== null)
  if (validEvents.length === 0) return null

  // Build Y-axis dates (unique, latest first)
  const dates = new Map<string, number>() // dateKey → index
  validEvents.forEach(e => {
    const d = getEventDate(e.published_at) || getEventDate(e.uploaded_at) || '—'
    if (!dates.has(d)) dates.set(d, dates.size)
  })
  const dateEntries = Array.from(dates.entries()) // [label, index]

  const W = 700
  const padding = { top: 12, right: 16, bottom: 22, left: 54 }
  const innerW = W - padding.left - padding.right
  const rowH = Math.min(24, Math.max(14, 180 / dateEntries.length))
  const chartH = padding.top + dateEntries.length * rowH + padding.bottom

  const uploadWindows = config.upload_windows || [{ start: 9, end: 11 }]
  const jitterMin = config.publish_jitter_min

  function xForHour(h: number) { return padding.left + (h / 24) * innerW }

  // Gather all optimal hours
  const longHours = slots.long.map(s => s.target_hour + s.target_minute / 60)
  const shortHours = slots.shorts.map(s => s.target_hour + s.target_minute / 60)

  return (
    <div className="glass rounded-xl p-5 space-y-3">
      <div className="flex items-center gap-2">
        <BarChart3 size={16} className="text-purple-400" />
        <h3 className="text-sm font-semibold text-gray-200">Historial de ejecución</h3>
        <span className="text-[10px] text-gray-600">{validEvents.length} eventos</span>
        {/* Legend */}
        <div className="ml-auto flex items-center gap-2 text-[9px]">
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-400/70" />Subida</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-emerald-400/70" />Publicación</span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-pink-400/70" />Short</span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${W} ${chartH}`} className="w-full" style={{ minWidth: 500 }}>
          <defs>
            <pattern id="dashPattern" patternUnits="userSpaceOnUse" width="4" height="4">
              <rect width="2" height="4" fill={COLORS.pubLine} />
            </pattern>
          </defs>

          {/* Upload window bands */}
          {uploadWindows.map((w, i) => {
            const x1 = xForHour(w.start)
            const x2 = xForHour(w.end <= w.start ? 24 : w.end)
            return (
              <rect key={`ub-${i}`} x={x1} y={0} width={x2 - x1} height={chartH}
                fill={COLORS.uploadBand} />
            )
          })}

          {/* Optimal publish hour zones */}
          {longHours.map((h, i) => {
            const jLo = h - jitterMin / 60
            const jHi = h + jitterMin / 60
            const x1 = xForHour(jLo)
            const x2 = xForHour(jHi)
            return (
              <rect key={`jz-l-${i}`} x={x1} y={0} width={x2 - x1} height={chartH}
                fill={COLORS.pubJitter} />
            )
          })}
          {shortHours.map((h, i) => {
            const jLo = h - jitterMin / 60
            const jHi = h + jitterMin / 60
            const x1 = xForHour(jLo)
            const x2 = xForHour(jHi)
            return (
              <rect key={`jz-s-${i}`} x={x1} y={0} width={x2 - x1} height={chartH}
                fill="rgba(244,114,182,0.05)" />
            )
          })}

          {/* Grid lines */}
          {[0, 4, 8, 12, 16, 20].map(h => {
            const x = xForHour(h)
            return (
              <g key={`gl-${h}`}>
                <line x1={x} y1={0} x2={x} y2={chartH} stroke={COLORS.gridLine} strokeWidth={0.5} />
                <text x={x} y={chartH - 6} textAnchor="middle" fill={COLORS.text} fontSize={7.5} fontFamily="monospace">
                  {pad(h)}h
                </text>
              </g>
            )
          })}

          {/* Optimal publish lines (vertical dashed) */}
          {longHours.map((h, i) => {
            const x = xForHour(h)
            return (
              <line key={`opl-${i}`} x1={x} y1={2} x2={x} y2={chartH - padding.bottom + 2}
                stroke={COLORS.pubLine} strokeWidth={1} strokeDasharray="3,3" opacity={0.4} />
            )
          })}
          {shortHours.map((h, i) => {
            const x = xForHour(h)
            return (
              <line key={`ops-${i}`} x1={x} y1={2} x2={x} y2={chartH - padding.bottom + 2}
                stroke="#f472b6" strokeWidth={0.8} strokeDasharray="2,4" opacity={0.3} />
            )
          })}

          {/* Event dots */}
          {validEvents.map((e, ei) => {
            const dateKey = getEventDate(e.published_at) || getEventDate(e.uploaded_at) || '—'
            const yi = dates.get(dateKey) ?? 0
            const y = padding.top + yi * rowH + rowH / 2

            const dots: { h: number; type: 'upload' | 'publish' }[] = []
            const uh = getEventHour(e.uploaded_at)
            const ph = getEventHour(e.published_at)
            if (uh !== null) dots.push({ h: uh, type: 'upload' })
            if (ph !== null && (uh === null || Math.abs(ph - (uh ?? 0)) > 0.1)) {
              dots.push({ h: ph, type: 'publish' })
            }

            return (
              <g key={e.video_id}>
                {/* Date label */}
                {ei % Math.max(1, Math.floor(validEvents.length / 12)) === 0 && (
                  <text x={padding.left - 4} y={y + 3} textAnchor="end" fill={COLORS.text} fontSize={7.5}>
                    {dateKey}
                  </text>
                )}
                {/* Row stripe */}
                <rect x={padding.left} y={padding.top + yi * rowH} width={innerW} height={rowH}
                  fill={ei % 2 === 0 ? 'rgba(255,255,255,0.01)' : 'transparent'} />

                {/* Dots */}
                {dots.map((d, di) => {
                  const x = xForHour(d.h)
                  const isShort = e.is_short
                  const fill = d.type === 'upload'
                    ? (isShort ? COLORS.uploadDotShorts : COLORS.uploadDot)
                    : (isShort ? COLORS.publishDotShorts : COLORS.publishDot)
                  return (
                    <g key={di}>
                      <circle cx={x} cy={y} r={3.5} fill={fill} opacity={0.85}>
                        <title>
                          {e.titulo_final || `Video #${e.video_id}`}{'\n'}
                          {d.type === 'upload' ? 'Subida: ' : 'Publicación: '}
                          {fmtHourMin(Math.floor(d.h), Math.round((d.h % 1) * 60))}{'\n'}
                          {isShort ? 'Short' : 'Largo'}
                        </title>
                      </circle>
                    </g>
                  )
                })}
              </g>
            )
          })}
        </svg>
      </div>

      {/* Bottom legend */}
      <div className="flex items-center gap-2 text-[9px] text-gray-600 flex-wrap">
        <span className="flex items-center gap-1">
          <span className="w-3 h-2.5 rounded-sm bg-amber-500/10 border border-amber-500/30" />
          Ventana subida
        </span>
        <span className="flex items-center gap-1">
          <span className="w-3 h-2.5 rounded-sm bg-purple-500/8 border border-purple-500/20" />
          Franja ±jitter (largos)
        </span>
        <span className="flex items-center gap-1">
          <span className="w-px h-3 bg-purple-500/40" />
          <span className="text-purple-400/60">Óptima largos</span>
        </span>
        <span className="flex items-center gap-1">
          <span className="w-px h-3 bg-pink-400/30" />
          <span className="text-pink-400/60">Óptima shorts</span>
        </span>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════
// StatsCards — summary KPIs
// ═══════════════════════════════════════════════════════════════

function StatsCards({ stats }: { stats: TimingStats }) {
  const cards = [
    {
      label: 'vídeos publicados',
      value: stats.total_published,
      icon: <Upload size={14} />,
      color: 'text-blue-400',
    },
    {
      label: 'dentro de ventana',
      value: `${stats.pct_within_window}%`,
      icon: <Target size={14} />,
      color: stats.pct_within_window >= 80 ? 'text-green-400' : stats.pct_within_window >= 50 ? 'text-amber-400' : 'text-red-400',
    },
    {
      label: 'warmup medio real',
      value: stats.avg_warmup_actual_min != null ? `${stats.avg_warmup_actual_min}min` : '—',
      icon: <Timer size={14} />,
      color: 'text-purple-400',
    },
    {
      label: 'programados',
      value: stats.total_scheduled,
      icon: <Calendar size={14} />,
      color: 'text-amber-400',
    },
  ]

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {cards.map((c, i) => (
        <div key={i} className="glass rounded-xl p-3 text-center">
          <div className={`flex justify-center mb-1 ${c.color}`}>{c.icon}</div>
          <div className="text-lg font-bold text-white tabular-nums">{c.value}</div>
          <div className="text-[10px] text-gray-500">{c.label}</div>
        </div>
      ))}
    </div>
  )
}
