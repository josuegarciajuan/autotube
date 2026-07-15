import { useState, useEffect, useCallback } from 'react'
import { api, formatCountdown, PlannedSlot, GeneratingVideo, AwaitingUploadVideo, WarmingVideo, ShortsPipelineSlot } from '../lib/api'
import { Loader2, Clock, Play, Lock, AlertTriangle, CheckCircle2, ArrowRight, Smartphone, Scissors, Upload, HardDrive } from 'lucide-react'
import { CHANNEL_SHORT, CHANNEL_STYLES, DEFAULT_STYLE } from '../lib/channelConfig'

// ── Helpers ──────────────────────────────────────────────────
function toLocalTime(ts: string): string {
  try {
    // Handle "YYYY-MM-DD HH:MM:SS" format (Europe/Madrid local from DB)
    const m = ts.match(/(\d{2}):(\d{2})/)
    return m ? `${m[1]}:${m[2]}` : ts.slice(0, 5)
  } catch {
    return ts?.slice(0, 5) || '--:--'
  }
}

function toLocalDate(ts: string): string {
  try {
    const m = ts.match(/(\d{4}-\d{2}-\d{2})/)
    if (m) {
      const d = new Date(m[1] + 'T00:00:00')
      return d.toLocaleDateString('es-ES', { weekday: 'short', day: 'numeric', month: 'short' })
    }
  } catch {}
  return ''
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

// ── Card: Planned slot (3-phase) ─────────────────────────────
function PlannedCard({ slot }: { slot: PlannedSlot }) {
  const colors = CHANNEL_STYLES[slot.channel_slug] || DEFAULT_STYLE
  const hasUpload = slot.target_upload_at && slot.target_upload_at !== slot.target_public_at
  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[slot.channel_slug] || slot.channel_name} #{slot.slot_position}
        </span>
        <span className="text-[10px] text-gray-500 font-mono ml-auto">
          {slot.source_mode === 'viral' ? ' Viral' : 'Original'}
        </span>
      </div>

      {/* 3-Phase timeline */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-xs">
          <Play size={11} className="text-purple-400" />
          <span className="text-gray-400">Gen:</span>
          <span className="text-white font-mono">{toLocalTime(slot.scheduled_at)}</span>
          {slot.date_key && slot.date_key !== slot.scheduled_at?.slice(0, 10) && (
            <span className="text-[9px] text-purple-400/70 ml-1">{toLocalDate(slot.scheduled_at)}</span>
          )}
        </div>
        {hasUpload && (
          <div className="flex items-center gap-2 text-xs">
            <Upload size={11} className="text-blue-400" />
            <span className="text-gray-400">Subir:</span>
            <span className="text-white font-mono">{toLocalTime(slot.target_upload_at!)}</span>
          </div>
        )}
        {slot.target_public_at && (
          <div className="flex items-center gap-2 text-xs">
            <ArrowRight size={11} className="text-green-400" />
            <span className="text-gray-400">Publico:</span>
            <span className="text-white font-mono">{toLocalTime(slot.target_public_at)}</span>
          </div>
        )}
      </div>

      {/* Countdown */}
      <div className="flex items-center gap-1.5 mt-3 pt-2 border-t border-white/5">
        <Clock size={11} className="text-amber-400" />
        <span className="text-[10px] text-amber-400 font-mono">
          Gen en {formatCountdown(slot.scheduled_at)}
        </span>
      </div>
    </div>
  )
}

// ── Card: Generating ─────────────────────────────────────────
function GeneratingCard({ video }: { video: GeneratingVideo }) {
  const colors = CHANNEL_STYLES[video.channel_slug] || DEFAULT_STYLE
  const pct = video.progress || video.job_progress || 0
  const phase = video.progress_phase || video.job_phase || 'inicio'

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.border} bg-dark-800/80 animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot} animate-pulse`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[video.channel_slug] || video.channel_name} #{video.video_id}
        </span>
        <span className="text-[10px] text-neon-cyan font-mono ml-auto flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-neon-cyan animate-pulse" />
          Generando
        </span>
      </div>

      {/* Progress bar */}
      <div className="mb-2">
        <div className="flex justify-between text-[10px] mb-1">
          <span className="text-gray-400">{phaseLabel(phase)}</span>
          <span className="text-white font-mono">{pct}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-dark-600 overflow-hidden">
          <div
            className="h-full rounded-full pipeline-progress-bar"
            style={{ width: `${Math.max(pct, 3)}%` }}
          />
        </div>
      </div>

      {/* Target publication */}
      {video.target_public_at && (
        <div className="flex items-center gap-1.5 mt-2 pt-2 border-t border-white/5">
          <Clock size={11} className="text-gray-500" />
          <span className="text-[10px] text-gray-500">
            Publicacion estimada: <span className="text-gray-300 font-mono">{toLocalTime(video.target_public_at)}</span>
          </span>
        </div>
      )}
    </div>
  )
}

// ── Card: Awaiting Upload (F1 done, waiting for F2 upload window) ──
function AwaitingUploadCard({ video, onUploadNow }: { video: AwaitingUploadVideo; onUploadNow: (videoId: number) => void }) {
  const colors = CHANNEL_STYLES[video.channel_slug] || DEFAULT_STYLE
  const countdown = video.target_public_at ? formatCountdown(video.target_public_at) : '?'

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} border-l-2 border-l-blue-400/50 animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[video.channel_slug] || video.channel_name} #{video.video_id}
        </span>
        <span className="text-[10px] text-blue-400 font-mono ml-auto flex items-center gap-1">
          <HardDrive size={10} />
          Pendiente subida
        </span>
      </div>

      {/* Info */}
      <div className="space-y-1 mb-3">
        {video.titulo_final && (
          <p className="text-[10px] text-gray-400 truncate">{video.titulo_final}</p>
        )}
        <div className="flex items-center gap-2 text-[10px]">
          <span className="text-gray-500">Publicacion:</span>
          <span className="text-gray-300 font-mono">
            {video.target_public_at ? toLocalTime(video.target_public_at) : '--:--'}
          </span>
          <span className={`font-mono ml-auto ${countdown === 'Ahora' ? 'text-neon-cyan' : 'text-amber-400'}`}>
            {countdown === 'Ahora' ? 'Puede subir' : `${countdown}`}
          </span>
        </div>
      </div>

      {/* Upload Now button */}
      <button
        onClick={() => onUploadNow(video.video_id)}
        className="w-full flex items-center justify-center gap-1.5 text-[10px] px-2 py-1.5 rounded-md
                   bg-blue-400/10 text-blue-400 border border-blue-400/20
                   hover:bg-blue-400/20 transition-colors"
      >
        <Upload size={10} />
        Subir ahora
      </button>
    </div>
  )
}

// ── Card: Warming (uploaded private) ─────────────────────────
function WarmingCard({ video, onManualToggle }: { video: WarmingVideo; onManualToggle: (videoId: number, item: string, done: boolean) => void }) {
  const colors = CHANNEL_STYLES[video.channel_slug] || DEFAULT_STYLE

  // Calculate warmup progress
  const warmupPct = (() => {
    try {
      const uploaded = new Date(video.uploaded_at.replace(' ', 'T')).getTime()
      const target = new Date(video.target_public_at.replace(' ', 'T')).getTime()
      const now = Date.now()
      if (target <= uploaded) return 100
      const pct = ((now - uploaded) / (target - uploaded)) * 100
      return Math.min(100, Math.max(0, Math.round(pct)))
    } catch { return 50 }
  })()

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} ${isDue ? 'animate-pulse border-neon-cyan/60' : ''} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[video.channel_slug] || video.channel_name} #{video.video_id}
        </span>
        <span className="text-[10px] text-amber-400 font-mono ml-auto flex items-center gap-1">
          <Lock size={10} />
          Calentando
        </span>
      </div>

      {/* Warmup progress bar */}
      <div className="mb-3">
        <div className="flex justify-between text-[10px] mb-1">
          <span className="text-gray-400">Warmup</span>
          <span className="text-white font-mono">{warmupPct}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-dark-600 overflow-hidden">
          <div
            className="h-full rounded-full warming-progress-bar"
            style={{ width: `${warmupPct}%` }}
          />
        </div>
      </div>

      {/* Timeline */}
      <div className="grid grid-cols-2 gap-2 text-[10px] mb-3">
        <div>
          <span className="text-gray-500">Subido:</span><br />
          <span className="text-gray-300 font-mono">{toLocalTime(video.uploaded_at)}</span>
        </div>
        <div>
          <span className="text-gray-500">Publico:</span><br />
          <span className={`font-mono ${isDue ? 'text-neon-cyan' : 'text-gray-300'}`}>{toLocalTime(video.target_public_at)}</span>
        </div>
      </div>

      {/* Countdown */}
      <div className="flex items-center gap-1.5 mb-3 pt-2 border-t border-white/5">
        <Clock size={11} className={isDue ? 'text-neon-cyan' : 'text-amber-400'} />
        <span className={`text-[10px] font-mono ${isDue ? 'text-neon-cyan' : 'text-amber-400'}`}>
          {isDue ? 'Publicando...' : `En ${countdown}`}
        </span>
      </div>

      {/* Manual checks */}
      <div className="space-y-1.5 pt-2 border-t border-white/5">
        <button
          onClick={() => onManualToggle(video.video_id, 'altered_content', !video.manual_altered_content_done)}
          className={`w-full flex items-center gap-2 text-[10px] px-2 py-1.5 rounded-md transition-colors ${
            video.manual_altered_content_done
              ? 'bg-green-400/10 text-green-400 border border-green-400/20'
              : 'bg-red-400/10 text-red-400 border border-red-400/20 hover:bg-green-400/10 hover:text-green-400'
          }`}
        >
          {video.manual_altered_content_done
            ? <CheckCircle2 size={11} />
            : <AlertTriangle size={11} />}
          {video.manual_altered_content_done ? 'Contenido IA marcado' : 'Marcar contenido IA'}
        </button>
        <button
          onClick={() => onManualToggle(video.video_id, 'end_screens', !video.manual_end_screens_done)}
          className={`w-full flex items-center gap-2 text-[10px] px-2 py-1.5 rounded-md transition-colors ${
            video.manual_end_screens_done
              ? 'bg-green-400/10 text-green-400 border border-green-400/20'
              : 'bg-red-400/10 text-red-400 border border-red-400/20 hover:bg-green-400/10 hover:text-green-400'
          }`}
        >
          {video.manual_end_screens_done
            ? <CheckCircle2 size={11} />
            : <AlertTriangle size={11} />}
          {video.manual_end_screens_done ? 'Pantallas finales OK' : 'Configurar pantallas finales'}
        </button>
      </div>
    </div>
  )
}

// ── Card: Shorts planned (pending slot) ─────────────────────
function ShortsPlannedCard({ slot }: { slot: ShortsPipelineSlot }) {
  const colors = CHANNEL_STYLES[slot.channel_slug] || DEFAULT_STYLE
  const isNative = slot.short_type === 'native'
  const typeColor = isNative ? 'text-emerald-400' : 'text-orange-400'
  const typeBg = isNative ? 'bg-emerald-400/10' : 'bg-orange-400/10'
  const typeBorder = isNative ? 'border-emerald-400/30' : 'border-orange-400/30'
  const TypeIcon = isNative ? Smartphone : Scissors
  const typeLabel = isNative ? 'Short \u00B7 Nativo' : 'Short \u00B7 Clip'
  const countdown = formatCountdown(slot.scheduled_at)

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} border-l-2 ${typeBorder} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[slot.channel_slug] || slot.channel_name}
        </span>
        <span className={`text-[10px] font-mono ml-auto flex items-center gap-1 px-1.5 py-0.5 rounded-full ${typeBg} ${typeColor}`}>
          <TypeIcon size={10} />
          {typeLabel}
        </span>
      </div>

      {/* Timeline */}
      <div className="flex items-center gap-2 text-xs">
        <Smartphone size={11} className="text-gray-500" />
        <span className="text-gray-400">Inicio:</span>
        <span className="text-white font-mono">{toLocalTime(slot.scheduled_at)}</span>
      </div>
      {slot.target_upload_at && (
        <div className="flex items-center gap-2 text-xs mt-1">
          <ArrowRight size={11} className="text-gray-500" />
          <span className="text-gray-400">Publicacion:</span>
          <span className="text-white font-mono">{toLocalTime(slot.target_upload_at)}</span>
        </div>
      )}

      {/* Countdown */}
      <div className="flex items-center gap-1.5 mt-3 pt-2 border-t border-white/5">
        <Clock size={11} className={countdown === 'Ahora' ? 'text-neon-cyan' : 'text-amber-400'} />
        <span className={`text-[10px] font-mono ${countdown === 'Ahora' ? 'text-neon-cyan' : 'text-amber-400'}`}>
          {countdown === 'Ahora' ? 'Inminente' : `En ${countdown}`}
        </span>
      </div>
    </div>
  )
}

// ── Card: Shorts generating (running slot with progress) ────
function ShortsGeneratingCard({ slot }: { slot: ShortsPipelineSlot }) {
  const colors = CHANNEL_STYLES[slot.channel_slug] || DEFAULT_STYLE
  const isNative = slot.short_type === 'native'
  const typeColor = isNative ? 'text-emerald-400' : 'text-orange-400'
  const typeBg = isNative ? 'bg-emerald-400/10' : 'bg-orange-400/10'
  const TypeIcon = isNative ? Smartphone : Scissors
  const typeLabel = isNative ? 'Short \u00B7 Nativo' : 'Short \u00B7 Clip'
  const pct = slot.job_progress || 0
  const phase = slot.job_phase || 'inicio'

  return (
    <div className="pipeline-card rounded-xl p-4 border bg-dark-800/80 border-l-2 border-emerald-400/30 animate-fade-in">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot} animate-pulse`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {CHANNEL_SHORT[slot.channel_slug] || slot.channel_name}
        </span>
        <span className={`text-[10px] font-mono ml-auto flex items-center gap-1 px-1.5 py-0.5 rounded-full ${typeBg} ${typeColor}`}>
          <TypeIcon size={10} />
          {typeLabel}
        </span>
        <span className="text-[10px] text-neon-cyan font-mono flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-neon-cyan animate-pulse" />
          Generando
        </span>
      </div>

      {/* Progress bar */}
      <div className="mb-2">
        <div className="flex justify-between text-[10px] mb-1">
          <span className="text-gray-400">{phaseLabel(phase)}</span>
          <span className="text-white font-mono">{pct}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-dark-600 overflow-hidden">
          <div
            className="h-full rounded-full pipeline-progress-bar"
            style={{ width: `${Math.max(pct, 3)}%` }}
          />
        </div>
      </div>

      {/* Target upload */}
      {slot.target_upload_at && (
        <div className="flex items-center gap-1.5 mt-2 pt-2 border-t border-white/5">
          <Clock size={11} className="text-gray-500" />
          <span className="text-[10px] text-gray-500">
            Publicacion estimada: <span className="text-gray-300 font-mono">{toLocalTime(slot.target_upload_at)}</span>
          </span>
        </div>
      )}
    </div>
  )
}

// ── Card: Shorts completed (done today) ──────────────────────
function ShortsCompletedCard({ slot }: { slot: ShortsPipelineSlot }) {
  const colors = CHANNEL_STYLES[slot.channel_slug] || DEFAULT_STYLE
  const isNative = slot.short_type === 'native'
  const typeColor = isNative ? 'text-emerald-400/60' : 'text-orange-400/60'
  const typeBg = isNative ? 'bg-emerald-400/5' : 'bg-orange-400/5'
  const TypeIcon = isNative ? Smartphone : Scissors
  const typeLabel = isNative ? 'Short \u00B7 Nativo' : 'Short \u00B7 Clip'

  return (
    <div className="pipeline-card rounded-xl p-3 border bg-dark-800/40 border-l-2 border-l-green-500/30 opacity-60 hover:opacity-80 transition-opacity">
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${colors.dot} opacity-50`} />
        <span className={`text-xs font-semibold ${colors.text} opacity-70`}>
          {CHANNEL_SHORT[slot.channel_slug] || slot.channel_name}
        </span>
        <span className={`text-[10px] font-mono ml-auto flex items-center gap-1 px-1.5 py-0.5 rounded-full ${typeBg} ${typeColor}`}>
          <TypeIcon size={9} />
          {typeLabel}
        </span>
        <span className="text-[10px] text-green-400 font-mono flex items-center gap-1">
          <CheckCircle2 size={10} />
          Completado
        </span>
      </div>
      <div className="flex items-center gap-2 text-[10px] mt-1.5">
        <Smartphone size={10} className="text-gray-500" />
        <span className="text-gray-500">Inicio:</span>
        <span className="text-gray-400 font-mono">{toLocalTime(slot.scheduled_at)}</span>
      </div>
    </div>
  )
}

// ── Column header ────────────────────────────────────────────
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

// ── Main component ───────────────────────────────────────────
export default function PipelineView() {
  const [planned, setPlanned] = useState<PlannedSlot[]>([])
  const [generating, setGenerating] = useState<GeneratingVideo[]>([])
  const [awaitingUpload, setAwaitingUpload] = useState<AwaitingUploadVideo[]>([])
  const [warming, setWarming] = useState<WarmingVideo[]>([])
  const [shortsPending, setShortsPending] = useState<ShortsPipelineSlot[]>([])
  const [shortsGenerating, setShortsGenerating] = useState<ShortsPipelineSlot[]>([])
  const [shortsCompleted, setShortsCompleted] = useState<ShortsPipelineSlot[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.getPipelineStatus()
      setPlanned(data.planned || [])
      setGenerating(data.generating || [])
      setAwaitingUpload(data.awaiting_upload || [])
      setWarming(data.warming || [])
      setShortsPending(data.shorts?.pending || [])
      setShortsGenerating(data.shorts?.generating || [])
      setShortsCompleted(data.shorts?.completed || [])
      setError(null)
    } catch (e: any) {
      console.error('PipelineView load error:', e)
      setError(e.message)
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [load])

  async function handleManualToggle(videoId: number, item: string, done: boolean) {
    try {
      await api.updateManualChecklist(videoId, item, done)
      load()
    } catch (e: any) {
      console.error('Manual toggle error:', e)
    }
  }

  async function handleUploadNow(videoId: number) {
    try {
      await api.uploadVideo(videoId)
      load()
    } catch (e: any) {
      console.error('Upload now error:', e)
    }
  }

  const totalItems = planned.length + generating.length + awaitingUpload.length + warming.length
    + shortsPending.length + shortsGenerating.length + shortsCompleted.length

  if (loading) {
    return (
      <div className="flex justify-center py-10">
        <Loader2 size={20} className="animate-spin text-gray-600" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center py-6">
        <p className="text-xs text-red-400">{error}</p>
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
      {/* ── Column 1: Planned ─────────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Clock} title="Planificado" count={planned.length + shortsPending.length + shortsCompleted.length} colorClass="text-amber-400" />
        {planned.length === 0 && shortsPending.length === 0 && shortsCompleted.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay pendientes</p>
        ) : (
          <div className="space-y-3">
            {planned.map((slot) => (
              <PlannedCard key={`vid-${slot.slot_id}`} slot={slot} />
            ))}
            {shortsPending.length > 0 && planned.length > 0 && (
              <div className="flex items-center gap-2 py-1">
                <div className="flex-1 h-px bg-surface-border/30" />
                <Smartphone size={11} className="text-gray-600" />
                <span className="text-[9px] text-gray-600 uppercase tracking-wider">Shorts pendientes</span>
                <div className="flex-1 h-px bg-surface-border/30" />
              </div>
            )}
            {shortsPending.map((slot) => (
              <ShortsPlannedCard key={`short-${slot.slot_id}`} slot={slot} />
            ))}
            {shortsCompleted.length > 0 && (
              <div className="flex items-center gap-2 py-1">
                <div className="flex-1 h-px bg-green-500/15" />
                <CheckCircle2 size={11} className="text-green-500/60" />
                <span className="text-[9px] text-green-500/80 uppercase tracking-wider">
                  Shorts completados ({shortsCompleted.length})
                </span>
                <div className="flex-1 h-px bg-green-500/15" />
              </div>
            )}
            {shortsCompleted.map((slot) => (
              <ShortsCompletedCard key={`short-done-${slot.slot_id}`} slot={slot} />
            ))}
          </div>
        )}
      </div>

      {/* ── Column 2: Generating ──────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Loader2} title="Generando" count={generating.length + shortsGenerating.length} colorClass="text-neon-cyan animate-spin" />
        {generating.length === 0 && shortsGenerating.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay generaciones activas</p>
        ) : (
          <div className="space-y-3">
            {generating.map((video) => (
              <GeneratingCard key={`vid-${video.video_id}`} video={video} />
            ))}
            {shortsGenerating.length > 0 && generating.length > 0 && (
              <div className="flex items-center gap-2 py-1">
                <div className="flex-1 h-px bg-surface-border/30" />
                <Smartphone size={11} className="text-gray-600" />
                <span className="text-[9px] text-gray-600 uppercase tracking-wider">Shorts</span>
                <div className="flex-1 h-px bg-surface-border/30" />
              </div>
            )}
            {shortsGenerating.map((slot) => (
              <ShortsGeneratingCard key={`short-${slot.slot_id}`} slot={slot} />
            ))}
          </div>
        )}
      </div>

      {/* ── Column 3: Awaiting Upload ─────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={HardDrive} title="Pendiente subida" count={awaitingUpload.length} colorClass="text-blue-400" />
        {awaitingUpload.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos esperando subida</p>
        ) : (
          <div className="space-y-3">
            {awaitingUpload.map((video) => (
              <AwaitingUploadCard key={`await-${video.video_id}`} video={video} onUploadNow={handleUploadNow} />
            ))}
          </div>
        )}
      </div>

      {/* ── Column 4: Warming ─────────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Lock} title="En privado (calentando)" count={warming.length} colorClass="text-amber-400" />
        {warming.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos en calentamiento</p>
        ) : (
          <div className="space-y-3">
            {warming.map((video) => (
              <WarmingCard key={video.video_id} video={video} onManualToggle={handleManualToggle} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
