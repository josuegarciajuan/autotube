import { useMemo } from 'react'
import { api, formatCountdown, PlannedSlot, GeneratingVideo, AwaitingUploadVideo, WarmingVideo, ShortsPipelineSlot, PublishedItem } from '../lib/api'
import { usePipelineStatus } from '../hooks/useQueries'
import { Loader2, Clock, Play, Lock, AlertTriangle, CheckCircle2, ArrowRight, Smartphone, Scissors, Upload, HardDrive, Film, ExternalLink, Trophy, Flame, RefreshCw, Check } from 'lucide-react'
import { getChannelStyles, getChannelShort } from '../lib/channelConfig'

// ── Helpers ──────────────────────────────────────────────────

/**
 * Parse a datetime string and return the local time as "HH:MM".
 * Handles both ISO8601 UTC ("2026-07-24T20:43:00+00:00") and naive local
 * ("2026-07-24 20:43:00") formats. Converts UTC to Europe/Madrid local.
 */
function toLocalTime(ts: string): string {
  if (!ts) return '--:--'
  const raw = ts.trim()

  // ISO8601 with explicit timezone offset or Z suffix → parse as full datetime
  if (raw.match(/[+-]\d{2}:\d{2}$/) || raw.endsWith('Z')) {
    const dt = new Date(raw)
    if (!isNaN(dt.getTime())) {
      return dt.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', hour12: false })
    }
  }

  // ISO8601 without timezone ("YYYY-MM-DDTHH:MM:SS") → treat as UTC
  if (raw.includes('T') && raw.length >= 16) {
    const dt = new Date(raw + '+00:00')
    if (!isNaN(dt.getTime())) {
      return dt.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', hour12: false })
    }
  }

  // Naive local "YYYY-MM-DD HH:MM:SS" → extract HH:MM directly
  const m = raw.match(/[\sT](\d{2}):(\d{2})/)
  if (m) {
    return `${m[1]}:${m[2]}`
  }

  return raw.length >= 5 ? raw.slice(0, 5) : '--:--'
}

/**
 * Parse a datetime string and return the local date in Spanish locale.
 */
function toLocalDate(ts: string): string {
  if (!ts) return ''
  try {
    let dt: Date
    const raw = ts.trim()

    if (raw.match(/[+-]\d{2}:\d{2}$/) || raw.endsWith('Z')) {
      dt = new Date(raw)
    } else if (raw.includes('T') && raw.length >= 16) {
      dt = new Date(raw + '+00:00')
    } else {
      const m = raw.match(/(\d{4})-(\d{2})-(\d{2})/)
      if (m) dt = new Date(parseInt(m[1]), parseInt(m[2]) - 1, parseInt(m[3]))
      else return ''
    }

    if (isNaN(dt.getTime())) return ''
    return dt.toLocaleDateString('es-ES', { weekday: 'short', day: 'numeric', month: 'short' })
  } catch {
    return ''
  }
}

/** Full date-time for tooltips: "lun 31 ago 02:13" */
function fmtFull(ts?: string | null): string {
  if (!ts) return ''
  const d = toLocalDate(ts)
  const t = toLocalTime(ts)
  return d && t !== '--:--' ? `${d} ${t}` : d || t || ts
}

/**
 * Compact "DD/MM HH:MM" formatter for both naive local and ISO8601 UTC.
 * Falls back to HH:MM when only time is available.
 */
function fmtCompact(ts?: string | null): string {
  if (!ts) return '—'
  const raw = ts.trim()
  // Naive local "YYYY-MM-DD HH:MM[:SS]"
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})[\sT](\d{2}):(\d{2})/)
  if (m) return `${m[3]}/${m[2]} ${m[4]}:${m[5]}`
  // ISO with timezone → convert to local
  try {
    const dt = new Date(raw)
    if (!isNaN(dt.getTime())) {
      const d = `${String(dt.getDate()).padStart(2, '0')}/${String(dt.getMonth() + 1).padStart(2, '0')}`
      const t = dt.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', hour12: false })
      return `${d} ${t}`
    }
  } catch { /* fallthrough */ }
  return raw.slice(0, 16)
}

/**
 * Numeric timestamp for sorting. Naive local strings are treated as UTC so all
 * items in a column share the same axis. Missing → -Infinity (sink to bottom).
 */
function tsNum(ts?: string | null): number {
  if (!ts) return -Infinity
  const raw = ts.trim()
  if (raw.includes('T') || raw.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(raw)) {
    const n = new Date(raw).getTime()
    return isNaN(n) ? -Infinity : n
  }
  const n = new Date(raw.replace(' ', 'T') + 'Z').getTime()
  return isNaN(n) ? -Infinity : n
}

function phaseLabel(phase: string): string {
  const map: Record<string, string> = {
    inicio: 'Iniciando',
    scrape: 'Scrapeando contenido',
    script: 'Generando guion',
    tts: 'Creando voz',
    media: 'Buscando imagenes',
    video: 'Renderizando video',
    metadata: 'Generando metadatos',
    upload: 'Subiendo a YouTube',
  }
  return map[phase] || phase || '...'
}

// ── Badges ───────────────────────────────────────────────────

function ContentTypeBadge({ type }: { type: 'video' | 'native' | 'clip' }) {
  if (type === 'video') {
    return (
      <span className="text-[9px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-purple-400/10 text-purple-400 border border-purple-400/30">
        <Film size={9} />
        Video
      </span>
    )
  }
  const isNative = type === 'native'
  const typeColor = isNative ? 'text-emerald-400' : 'text-orange-400'
  const typeBg = isNative ? 'bg-emerald-400/10' : 'bg-orange-400/10'
  const typeBorder = isNative ? 'border-emerald-400/30' : 'border-orange-400/30'
  const TypeIcon = isNative ? Smartphone : Scissors
  const typeLabel = isNative ? 'Short · Nativo' : 'Short · Clip'
  return (
    <span className={`text-[9px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded-full ${typeBg} ${typeColor} border ${typeBorder}`}>
      <TypeIcon size={9} />
      {typeLabel}
    </span>
  )
}

/** Marathon / Viral tag, shown in EVERY column. */
function TagBadge({ tone }: { tone: 'marathon' | 'viral' }) {
  if (tone === 'marathon') {
    return (
      <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border border-amber-500/40 bg-amber-500/15 text-amber-400 flex items-center gap-1" title="Maratón ~1h">
        <Trophy size={9} />
        MARATÓN
      </span>
    )
  }
  return (
    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold border border-pink-500/40 bg-pink-500/15 text-pink-400 flex items-center gap-1" title="Viral (contenido adaptado de fuente viral)">
        <Flame size={9} />
        VIRAL
    </span>
  )
}

// ── Unified timeline model ───────────────────────────────────

type StageKey = 'plan' | 'create' | 'upload' | 'publish'
type ItemState = 'planned' | 'generating' | 'awaiting' | 'ready' | 'warming' | 'published'

interface TimelineDates {
  plannedAt?: string    // fecha de lanzamiento de la programación
  planStart?: string    // inicio creación planificado
  realStart?: string    // inicio creación real
  realEnd?: string      // fin creación real
  planUpload?: string   // subida prevista
  realUpload?: string   // subida real
  planPublish?: string  // publicación prevista
  realPublish?: string  // publicación real
}

interface TaggedItem {
  key: string
  kind: 'video' | 'native' | 'clip'
  channelId: number
  channelSlug: string
  channelName: string
  title?: string
  isMarathon: boolean
  isViral: boolean
  dates: TimelineDates
  state: ItemState
  // render extras
  progress?: number
  phase?: string
  warmupPct?: number
  held?: boolean
  videoId?: number
  youtubeId?: string
  targetPublish?: string
  countdownText?: string
  countdownUrgent?: boolean
}

const isViral = (mode?: string) => mode === 'viral'

function calcWarmup(v: WarmingVideo): number {
  try {
    const uploaded = new Date(v.uploaded_at.replace(' ', 'T')).getTime()
    const target = new Date(v.target_public_at.replace(' ', 'T')).getTime()
    const now = Date.now()
    if (target <= uploaded) return 100
    return Math.min(100, Math.max(0, Math.round(((now - uploaded) / (target - uploaded)) * 100)))
  } catch { return 50 }
}

// ── Normalizers: every pipeline shape → TaggedItem ────────────

function normPlannedVideo(s: PlannedSlot): TaggedItem {
  return {
    key: `pv-${s.slot_id}`, kind: 'video', channelId: s.channel_id, channelSlug: s.channel_slug, channelName: s.channel_name,
    isMarathon: false, isViral: isViral(s.source_mode), state: 'planned',
    dates: { plannedAt: s.planned_at, planStart: s.scheduled_at, planUpload: s.target_upload_at, planPublish: s.target_public_at },
    countdownText: s.scheduled_at ? formatCountdown(s.scheduled_at) : undefined,
    countdownUrgent: !!s.scheduled_at && formatCountdown(s.scheduled_at) === 'Ahora',
  }
}

function normGeneratingVideo(v: GeneratingVideo): TaggedItem {
  return {
    key: `gv-${v.video_id}`, kind: 'video', channelId: v.channel_id, channelSlug: v.channel_slug, channelName: v.channel_name,
    isMarathon: !!v.is_marathon, isViral: isViral(v.source_mode), state: 'generating',
    dates: {
      plannedAt: v.planned_at || v.created_at, planStart: v.plan_start || undefined,
      realStart: v.generation_started_at || undefined, realEnd: v.generation_finished_at || undefined,
      planUpload: v.plan_upload || undefined, planPublish: v.target_public_at || undefined,
    },
    progress: v.progress || v.job_progress || 0, phase: v.progress_phase || v.job_phase || 'inicio',
    videoId: v.video_id, targetPublish: v.target_public_at || undefined,
  }
}

function normAwaitingVideo(v: AwaitingUploadVideo): TaggedItem {
  const cd = v.target_upload_at ? formatCountdown(v.target_upload_at) : (v.target_public_at ? formatCountdown(v.target_public_at) : '?')
  return {
    key: `av-${v.video_id}`, kind: 'video', channelId: v.channel_id, channelSlug: v.channel_slug, channelName: v.channel_name,
    isMarathon: !!v.is_marathon, isViral: isViral(v.source_mode), state: 'awaiting',
    dates: {
      plannedAt: v.planned_at || v.created_at, planStart: v.plan_start || undefined,
      realStart: v.generation_started_at || undefined, realEnd: v.generation_finished_at || undefined,
      planUpload: v.target_upload_at || undefined, planPublish: v.target_public_at || undefined,
    },
    title: v.titulo_final || undefined, videoId: v.video_id,
    countdownText: cd, countdownUrgent: cd === 'Ahora',
  }
}

function normWarmingVideo(v: WarmingVideo): TaggedItem {
  const held = !!v.held
  const cd = v.target_public_at ? formatCountdown(v.target_public_at) : '?'
  return {
    key: `wv-${v.video_id}`, kind: 'video', channelId: v.channel_id, channelSlug: v.channel_slug, channelName: v.channel_name,
    isMarathon: !!v.is_marathon, isViral: isViral(v.source_mode), state: 'warming',
    dates: {
      plannedAt: v.planned_at || v.created_at, planStart: v.plan_start || undefined,
      realStart: v.generation_started_at || undefined, realEnd: v.generation_finished_at || undefined,
      planUpload: v.plan_upload || undefined, realUpload: v.uploaded_at || undefined,
      planPublish: v.target_public_at || undefined,
    },
    title: v.titulo_final || undefined, videoId: v.video_id, warmupPct: held ? undefined : calcWarmup(v), held,
    countdownText: held ? undefined : cd, countdownUrgent: !held && cd === 'Ahora',
  }
}

function normShort(slot: ShortsPipelineSlot, sub: 'planned' | 'generating' | 'ready'): TaggedItem {
  const kind: 'native' | 'clip' = slot.short_type === 'native' ? 'native' : 'clip'
  const cd = (sub === 'planned' || sub === 'ready') ? formatCountdown(slot.target_upload_at || slot.scheduled_at) : undefined
  return {
    key: `sh-${slot.slot_id}-${sub}`, kind, channelId: slot.channel_id, channelSlug: slot.channel_slug, channelName: slot.channel_name,
    isMarathon: false, isViral: isViral(slot.source_mode), state: sub,
    dates: {
      plannedAt: slot.planned_at, planStart: slot.scheduled_at, realStart: slot.real_start || undefined,
      planUpload: slot.plan_upload || slot.target_upload_at || undefined,
      planPublish: slot.target_upload_at || slot.plan_upload || undefined,
      realPublish: slot.real_publish || slot.actual_completed_at || undefined,
    },
    title: slot.title || undefined, progress: sub === 'generating' ? (slot.job_progress || 0) : undefined,
    phase: sub === 'generating' ? (slot.job_phase || 'inicio') : undefined,
    countdownText: cd, countdownUrgent: cd === 'Ahora',
  }
}

function normPublished(item: PublishedItem): TaggedItem {
  return {
    key: `pub-${item.content_type}-${item.id}`, kind: item.content_type, channelId: item.channel_id,
    channelSlug: item.channel_slug, channelName: item.channel_name,
    isMarathon: !!item.is_marathon, isViral: isViral(item.source_mode), state: 'published',
    dates: {
      plannedAt: item.planned_at || undefined, planStart: item.plan_start || undefined,
      planUpload: item.plan_upload || undefined, planPublish: item.plan_publish || undefined,
      realStart: item.real_start || undefined, realUpload: item.real_upload || undefined,
      realPublish: item.published_at || undefined,
    },
    title: item.title || undefined, youtubeId: item.youtube_id || undefined,
  }
}

function stageForState(state: ItemState): StageKey {
  switch (state) {
    case 'planned': return 'plan'
    case 'generating': return 'create'
    case 'awaiting':
    case 'ready': return 'upload'
    case 'warming':
    case 'published': return 'publish'
  }
}

// ── Timeline strip (4-stage stepper) ──────────────────────────

const STAGE_ROWS = [
  { key: 'plan' as StageKey, label: 'Prog' },
  { key: 'create' as StageKey, label: 'Crea' },
  { key: 'upload' as StageKey, label: 'Subir' },
  { key: 'publish' as StageKey, label: 'Publ' },
]

function TimelineStrip({ dates, current }: { dates: TimelineDates; current: StageKey }) {
  const rows = [
    { key: 'plan' as StageKey, planned: dates.plannedAt, real: undefined, fin: undefined },
    { key: 'create' as StageKey, planned: dates.planStart, real: dates.realStart, fin: dates.realEnd },
    { key: 'upload' as StageKey, planned: dates.planUpload, real: dates.realUpload, fin: undefined },
    { key: 'publish' as StageKey, planned: dates.planPublish, real: dates.realPublish, fin: undefined },
  ]
  return (
    <div className="mt-1.5 space-y-0.5">
      {rows.map((r) => {
        const done = r.key === 'plan' ? !!r.planned : !!r.real
        const cur = r.key === current
        const hasPlan = !!r.planned
        const hasReal = !!r.real
        return (
          <div
            key={r.key}
            className={`flex items-center gap-1 text-[9px] leading-tight px-1 py-px rounded ${cur ? 'bg-white/5 ring-1 ring-white/10' : ''}`}
          >
            {done ? (
              <Check size={8} className="text-green-400 shrink-0" />
            ) : cur ? (
              <span className="w-1 h-1 rounded-full bg-neon-cyan animate-pulse shrink-0" />
            ) : (
              <span className="w-1 h-1 rounded-full bg-gray-600 shrink-0" />
            )}
            <span className={`w-7 shrink-0 font-semibold ${done ? 'text-gray-300' : cur ? 'text-neon-cyan' : 'text-gray-500'}`}>
              {STAGE_ROWS.find(s => s.key === r.key)!.label}
            </span>
            {hasPlan && (
              <span title={fmtFull(r.planned)} className={`font-mono ${done ? 'text-gray-500 line-through decoration-gray-700' : cur ? 'text-white' : 'text-gray-500'}`}>
                {fmtCompact(r.planned)}
              </span>
            )}
            {hasPlan && hasReal && <ArrowRight size={7} className="text-gray-600 shrink-0" />}
            {hasReal && (
              <span title={fmtFull(r.real)} className="font-mono text-green-300">
                {fmtCompact(r.real)}
              </span>
            )}
            {r.fin && (
              <span title={fmtFull(r.fin)} className="text-gray-500 font-mono shrink-0 ml-auto">
                fin {fmtCompact(r.fin)}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Unified card ─────────────────────────────────────────────

function PipelineCard({ item, onUploadNow }: { item: TaggedItem; onUploadNow?: (videoId: number) => void }) {
  const colors = getChannelStyles({ channel_id: item.channelId, channel_slug: item.channelSlug, channel_name: item.channelName })
  const short = getChannelShort({ channel_id: item.channelId, channel_slug: item.channelSlug, channel_name: item.channelName })
  const idLabel = (item.state === 'planned' || item.state === 'generating' || item.state === 'awaiting') && item.videoId != null
    ? `${short} #${item.videoId}` : short
  const current = stageForState(item.state)

  return (
    <div className={`pipeline-card rounded-xl p-3 border ${colors.border} ${item.state === 'warming' || item.state === 'planned' ? colors.bg : 'bg-dark-800/80'} ${item.state === 'awaiting' ? 'border-l-2 border-l-blue-400/50' : ''} ${item.state === 'ready' ? 'border-l-2 border-l-green-500/40' : ''} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
        <span className={`w-2 h-2 rounded-full ${colors.dot} ${item.state === 'generating' ? 'animate-pulse' : ''}`} />
        <span className={`text-[11px] font-semibold ${colors.text}`}>{idLabel}</span>
        <ContentTypeBadge type={item.kind} />
        {item.isMarathon && <TagBadge tone="marathon" />}
        {item.isViral && <TagBadge tone="viral" />}
        <span className="ml-auto flex items-center gap-1 text-[9px] text-gray-400 font-mono">
          {statusIndicator(item)}
        </span>
      </div>

      {item.title && <p className="text-[10px] text-gray-400 truncate mb-1.5" title={item.title}>{item.title}</p>}

      {/* Progress (generating) */}
      {item.state === 'generating' && item.progress != null && (
        <div className="mb-1.5">
          <div className="flex justify-between text-[9px] mb-0.5">
            <span className="text-gray-400">{phaseLabel(item.phase || '')}</span>
            <span className="text-white font-mono">{item.progress}%</span>
          </div>
          <div className="h-1 rounded-full bg-dark-600 overflow-hidden">
            <div className="h-full pipeline-progress-bar" style={{ width: `${Math.max(item.progress, 3)}%` }} />
          </div>
        </div>
      )}

      {/* Warmup (warming) */}
      {item.state === 'warming' && !item.held && item.warmupPct != null && (
        <div className="mb-1.5">
          <div className="flex justify-between text-[9px] mb-0.5">
            <span className="text-gray-400">Warmup</span>
            <span className="text-white font-mono">{item.warmupPct}%</span>
          </div>
          <div className="h-1 rounded-full bg-dark-600 overflow-hidden">
            <div className="h-full warming-progress-bar" style={{ width: `${item.warmupPct}%` }} />
          </div>
        </div>
      )}

      <TimelineStrip dates={item.dates} current={current} />

      {/* Footer */}
      <div className="flex items-center gap-1.5 mt-1.5 pt-1.5 border-t border-white/5">
        {item.state === 'planned' && (
          <>
            <Clock size={10} className={item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'} />
            <span className={`text-[9px] font-mono ${item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'}`}>
              Gen en {item.countdownText}
            </span>
          </>
        )}
        {item.state === 'generating' && item.targetPublish && (
          <>
            <Clock size={10} className="text-gray-500" />
            <span className="text-[9px] text-gray-500">Publ: <span className="text-gray-300 font-mono">{fmtCompact(item.targetPublish)}</span></span>
          </>
        )}
        {item.state === 'awaiting' && (
          <>
            <Clock size={10} className={item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'} />
            <span className={`text-[9px] font-mono ${item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'}`}>
              {item.countdownUrgent ? 'Puede subir' : item.countdownText}
            </span>
            {onUploadNow && item.videoId != null && (
              <button
                onClick={() => onUploadNow(item.videoId!)}
                className="ml-auto flex items-center gap-1 text-[9px] px-2 py-1 rounded-md bg-blue-400/10 text-blue-400 border border-blue-400/20 hover:bg-blue-400/20 transition-colors"
              >
                <Upload size={9} /> Subir
              </button>
            )}
          </>
        )}
        {item.state === 'ready' && (
          <>
            <Clock size={10} className={item.countdownUrgent ? 'text-neon-cyan' : 'text-blue-400'} />
            <span className={`text-[9px] font-mono ${item.countdownUrgent ? 'text-neon-cyan' : 'text-blue-400'}`}>
              {item.countdownUrgent ? 'Inminente' : `Subida en ${item.countdownText}`}
            </span>
          </>
        )}
        {item.state === 'warming' && (
          item.held ? (
            <>
              <Lock size={10} className="text-slate-400" />
              <span className="text-[9px] font-mono text-slate-400">Retenido — se reprograma al resetear cuota</span>
            </>
          ) : (
            <>
              <Clock size={10} className={item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'} />
              <span className={`text-[9px] font-mono ${item.countdownUrgent ? 'text-neon-cyan' : 'text-amber-400'}`}>
                {item.countdownUrgent ? 'Sin confirmar publicación' : `En ${item.countdownText}`}
              </span>
            </>
          )
        )}
        {item.state === 'published' && (
          <>
            <CheckCircle2 size={10} className="text-green-400" />
            <span className="text-[9px] text-gray-500">Publicado: <span className="text-white font-mono">{fmtCompact(item.dates.realPublish)}</span></span>
            {item.youtubeId && (
              <a href={`https://youtube.com/watch?v=${item.youtubeId}`} target="_blank" rel="noopener noreferrer"
                 className="ml-auto flex items-center gap-1 text-[9px] text-green-400 hover:text-green-300 font-mono">
                <ExternalLink size={9} /> Ver
              </a>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function statusIndicator(item: TaggedItem): React.ReactNode {
  switch (item.state) {
    case 'generating':
      return (<><span className="w-1.5 h-1.5 rounded-full bg-neon-cyan animate-pulse" />Generando</>)
    case 'awaiting':
      return (<><HardDrive size={9} className="text-blue-400" />Pend. subida</>)
    case 'ready':
      return (<><span className="w-1.5 h-1.5 rounded-full bg-green-400" />Listo</>)
    case 'warming':
      return item.held
        ? (<><Lock size={9} className="text-slate-400" />Privado</>)
        : (<><Clock size={9} className="text-amber-400" />Calentando</>)
    case 'published':
      return (<><CheckCircle2 size={9} className="text-green-400" />Publicado</>)
    default:
      return (<><Clock size={9} className="text-amber-400" />Planificado</>)
  }
}

// ── Main component ───────────────────────────────────────────

function ColumnHeader({ icon: Icon, title, count, colorClass }: {
  icon: any; title: string; count: number; colorClass: string
}) {
  return (
    <div className="flex items-center gap-2 mb-3 pb-2 border-b border-surface-border/50">
      <Icon size={16} className={colorClass} />
      <h4 className="text-sm font-semibold text-white">{title}</h4>
      <span className="text-xs text-gray-500 font-mono ml-auto">{count}</span>
    </div>
  )
}

export default function PipelineView() {
  const { data, isLoading: loading, error: queryError, refetch } = usePipelineStatus()
  const error = queryError ? String(queryError) : null

  const {
    mergedPlanned, mergedGenerating, mergedAwaiting, warming, published,
  } = useMemo(() => {
    if (!data) {
      return { mergedPlanned: [] as TaggedItem[], mergedGenerating: [] as TaggedItem[], mergedAwaiting: [] as TaggedItem[], warming: [] as TaggedItem[], published: [] as TaggedItem[] }
    }
    const plannedVideos: TaggedItem[] = (data.planned || []).map(normPlannedVideo)
    const shortsPending: TaggedItem[] = (data.shorts?.pending || []).map(s => normShort(s, 'planned'))
    const generatingVideos: TaggedItem[] = (data.generating || []).map(normGeneratingVideo)
    const shortsGenerating: TaggedItem[] = (data.shorts?.generating || []).map(s => normShort(s, 'generating'))
    const awaitingVideos: TaggedItem[] = (data.awaiting_upload || []).map(normAwaitingVideo)
    const shortsReady: TaggedItem[] = (data.shorts?.ready_to_upload || []).map(s => normShort(s, 'ready'))
    const warmingItems: TaggedItem[] = (data.warming || []).map(normWarmingVideo)
    const publishedItems: TaggedItem[] = (data.published_24h || []).map(normPublished)

    // Sort DESC (most recent first) by per-column key.
    const byKey = (a: TaggedItem, b: TaggedItem, keys: (k: TimelineDates) => (string | undefined)[]) => {
      const av = Math.max(...keys(a.dates).map(tsNum).filter(n => n !== -Infinity), -Infinity)
      const bv = Math.max(...keys(b.dates).map(tsNum).filter(n => n !== -Infinity), -Infinity)
      return bv - av
    }

    const mergedPlannedVal = [...plannedVideos, ...shortsPending].sort((a, b) =>
      byKey(a, b, d => [d.planStart]))
    const mergedGeneratingVal = [...generatingVideos, ...shortsGenerating].sort((a, b) =>
      byKey(a, b, d => [d.realStart, d.planStart, d.plannedAt]))
    const mergedAwaitingVal = [...awaitingVideos, ...shortsReady].sort((a, b) =>
      byKey(a, b, d => [d.planUpload, d.planPublish]))
    const warmingVal = [...warmingItems].sort((a, b) =>
      byKey(a, b, d => [d.planPublish]))
    const publishedVal = [...publishedItems].sort((a, b) =>
      byKey(a, b, d => [d.realPublish]))

    return { mergedPlanned: mergedPlannedVal, mergedGenerating: mergedGeneratingVal, mergedAwaiting: mergedAwaitingVal, warming: warmingVal, published: publishedVal }
  }, [data])

  async function handleUploadNow(videoId: number) {
    try {
      await api.uploadVideo(videoId)
      refetch()
    } catch (e: any) {
      console.error('Upload now error:', e)
    }
  }

  const totalItems = mergedPlanned.length + mergedGenerating.length + mergedAwaiting.length + warming.length + published.length

  if (loading) {
    return (
      <div className="flex justify-center py-10">
        <Loader2 size={20} className="animate-spin text-gray-600" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center gap-3 py-8 glass rounded-xl">
        <AlertTriangle size={20} className="text-amber-400" />
        <div className="text-center">
          <p className="text-xs text-red-400">{error}</p>
          <p className="text-[10px] text-gray-500 mt-1">El servidor puede estar reiniciandose.</p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white transition-colors"
        >
          <RefreshCw size={12} /> Reintentar
        </button>
      </div>
    )
  }

  if (totalItems === 0) {
    return (
      <div className="text-center py-10 glass rounded-xl">
        <Clock size={32} className="mx-auto mb-3 text-gray-700" />
        <p className="text-xs text-gray-500">No hay videos ni shorts en el pipeline ahora.</p>
        <p className="text-[10px] text-gray-600 mt-1">Apareceran aqui cuando esten planificados.</p>
      </div>
    )
  }

  return (
    <div className="pipeline-grid">
      <div className="pipeline-column">
        <ColumnHeader icon={Clock} title="Planificado" count={mergedPlanned.length} colorClass="text-amber-400" />
        {mergedPlanned.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay pendientes</p>
        ) : (
          <div className="space-y-3">{mergedPlanned.map(i => <PipelineCard key={i.key} item={i} />)}</div>
        )}
      </div>

      <div className="pipeline-column">
        <ColumnHeader icon={Loader2} title="Generando" count={mergedGenerating.length} colorClass="text-neon-cyan animate-spin" />
        {mergedGenerating.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay generaciones activas</p>
        ) : (
          <div className="space-y-3">{mergedGenerating.map(i => <PipelineCard key={i.key} item={i} />)}</div>
        )}
      </div>

      <div className="pipeline-column">
        <ColumnHeader icon={HardDrive} title="Pendiente subida" count={mergedAwaiting.length} colorClass="text-blue-400" />
        {mergedAwaiting.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos esperando subida</p>
        ) : (
          <div className="space-y-3">{mergedAwaiting.map(i => <PipelineCard key={i.key} item={i} onUploadNow={handleUploadNow} />)}</div>
        )}
      </div>

      <div className="pipeline-column">
        <ColumnHeader icon={Lock} title="No listado (calentando)" count={warming.length} colorClass="text-amber-400" />
        {warming.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos en calentamiento</p>
        ) : (
          <div className="space-y-3">{warming.map(i => <PipelineCard key={i.key} item={i} />)}</div>
        )}
      </div>

      <div className="pipeline-column">
        <ColumnHeader icon={CheckCircle2} title="Publicados (24h)" count={published.length} colorClass="text-green-400" />
        {published.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay publicados recientes</p>
        ) : (
          <div className="space-y-3">{published.map(i => <PipelineCard key={i.key} item={i} />)}</div>
        )}
      </div>
    </div>
  )
}
