import { useState, useMemo } from 'react'
import { api, formatCountdown, PlannedSlot, GeneratingVideo, AwaitingUploadVideo, WarmingVideo, ShortsPipelineSlot, PublishedItem } from '../lib/api'
import { usePipelineStatus } from '../hooks/useQueries'
import { Loader2, Clock, Play, Lock, AlertTriangle, CheckCircle2, ArrowRight, Smartphone, Scissors, Upload, HardDrive, Film, ExternalLink, Trophy, RefreshCw } from 'lucide-react'
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
 * Handles both ISO8601 UTC and naive local formats.
 * Uses the browser's Intl API for correct timezone conversion.
 */
function toLocalDate(ts: string): string {
  if (!ts) return ''
  try {
    let dt: Date
    const raw = ts.trim()

    // ISO8601 with explicit timezone → let JS handle conversion
    if (raw.match(/[+-]\d{2}:\d{2}$/) || raw.endsWith('Z')) {
      dt = new Date(raw)
    } else if (raw.includes('T') && raw.length >= 16) {
      // ISO without TZ → treat as UTC
      dt = new Date(raw + '+00:00')
    } else {
      // Naive local "YYYY-MM-DD HH:MM:SS" → parse date part
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

// ── Badge: content type (reusable) ──────────────────────────
function ContentTypeBadge({ type }: { type: 'video' | 'native' | 'clip' }) {
  if (type === 'video') {
    return (
      <span className="text-[10px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-purple-400/10 text-purple-400 border border-purple-400/30">
        <Film size={10} />
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
    <span className={`text-[10px] font-mono flex items-center gap-1 px-1.5 py-0.5 rounded-full ${typeBg} ${typeColor} border ${typeBorder}`}>
      <TypeIcon size={10} />
      {typeLabel}
    </span>
  )
}

// ── Card: Planned slot (3-phase) ─────────────────────────────
function PlannedCard({ slot }: { slot: PlannedSlot }) {
  const colors = getChannelStyles({ channel_id: slot.channel_id, channel_slug: slot.channel_slug })
  const hasUpload = slot.target_upload_at && slot.target_upload_at !== slot.target_public_at
  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: slot.channel_id, channel_slug: slot.channel_slug, channel_name: slot.channel_name })} #{slot.slot_position}
        </span>
        <ContentTypeBadge type="video" />
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
          {slot.scheduled_at && slot.scheduled_at.slice(0, 10) !== new Date().toISOString().slice(0, 10) && (
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
  const colors = getChannelStyles({ channel_id: video.channel_id, channel_slug: video.channel_slug })
  const pct = video.progress || video.job_progress || 0
  const phase = video.progress_phase || video.job_phase || 'inicio'

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.border} bg-dark-800/80 animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot} animate-pulse`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: video.channel_id, channel_slug: video.channel_slug, channel_name: video.channel_name })} #{video.video_id}
        </span>
        {video.is_marathon && (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold border border-amber-500/40 bg-amber-500/15 text-amber-400 flex items-center gap-1" title="Maratón ~1h">
            <Trophy size={10} />
            MARATÓN
          </span>
        )}
        <ContentTypeBadge type="video" />
        <span className="text-[10px] text-neon-cyan font-mono ml-auto flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-neon-cyan animate-pulse" />
          Generando
        </span>
      </div>

      {/* Generation start time */}
      {video.generation_started_at && (
        <div className="flex items-center gap-2 mb-2 text-[10px]">
          <Play size={11} className="text-purple-400" />
          <span className="text-gray-400">Inicio:</span>
          <span className="text-white font-mono">
            {toLocalTime(video.generation_started_at)} {toLocalDate(video.generation_started_at)}
          </span>
        </div>
      )}

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
  const colors = getChannelStyles({ channel_id: video.channel_id, channel_slug: video.channel_slug })
  const countdown = video.target_upload_at ? formatCountdown(video.target_upload_at) : (video.target_public_at ? formatCountdown(video.target_public_at) : '?')

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} border-l-2 border-l-blue-400/50 animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: video.channel_id, channel_slug: video.channel_slug, channel_name: video.channel_name })} #{video.video_id}
        </span>
        {video.is_marathon && (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold border border-amber-500/40 bg-amber-500/15 text-amber-400 flex items-center gap-1" title="Maratón ~1h">
            <Trophy size={10} />
            MARATÓN
          </span>
        )}
        <ContentTypeBadge type="video" />
        <span className="text-[10px] text-blue-400 font-mono ml-auto flex items-center gap-1">
          <HardDrive size={10} />
          Pendiente subida
        </span>
      </div>

      {/* Title */}
      {video.titulo_final && (
        <p className="text-[10px] text-gray-400 truncate mb-3">{video.titulo_final}</p>
      )}

      {/* Timeline: 3 lines */}
      <div className="space-y-1 mb-3">
        {video.generation_finished_at && (
          <div className="flex items-center gap-2 text-[10px]">
            <CheckCircle2 size={11} className="text-green-400" />
            <span className="text-gray-400">Fin gen:</span>
            <span className="text-white font-mono">
              {toLocalTime(video.generation_finished_at)} {toLocalDate(video.generation_finished_at)}
            </span>
          </div>
        )}
        {video.target_upload_at && (
          <div className="flex items-center gap-2 text-[10px]">
            <Upload size={11} className="text-blue-400" />
            <span className="text-gray-400">Subir:</span>
            <span className="text-white font-mono">
              {toLocalTime(video.target_upload_at)} {toLocalDate(video.target_upload_at)}
            </span>
          </div>
        )}
        {video.target_public_at && (
          <div className="flex items-center gap-2 text-[10px]">
            <ArrowRight size={11} className="text-green-400" />
            <span className="text-gray-400">Público:</span>
            <span className="text-white font-mono">
              {toLocalTime(video.target_public_at)} {toLocalDate(video.target_public_at)}
            </span>
          </div>
        )}
      </div>

      {/* Countdown */}
      <div className="flex items-center gap-1.5 mb-3 pt-2 border-t border-white/5">
        <Clock size={11} className={countdown === 'Ahora' ? 'text-neon-cyan' : 'text-amber-400'} />
        <span className={`text-[10px] font-mono ${countdown === 'Ahora' ? 'text-neon-cyan' : 'text-amber-400'}`}>
          {countdown === 'Ahora' ? 'Puede subir' : `${countdown}`}
        </span>
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

// ── Card: Warming (uploaded unlisted) ────────────────────────
function WarmingCard({ video }: { video: WarmingVideo }) {
  const colors = getChannelStyles({ channel_id: video.channel_id, channel_slug: video.channel_slug })

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
  const countdown = formatCountdown(video.target_public_at)
  const isDue = countdown === 'Ahora'

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} ${isDue ? 'animate-pulse border-neon-cyan/60' : ''} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: video.channel_id, channel_slug: video.channel_slug, channel_name: video.channel_name })} #{video.video_id}
        </span>
        <ContentTypeBadge type="video" />
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
          <span className="text-gray-300 font-mono">{toLocalTime(video.uploaded_at)} {toLocalDate(video.uploaded_at)}</span>
        </div>
        <div>
          <span className="text-gray-500">Publico:</span><br />
          <span className={`font-mono ${isDue ? 'text-neon-cyan' : 'text-gray-300'}`}>
            {toLocalTime(video.target_public_at)} {toLocalDate(video.target_public_at)}
          </span>
        </div>
      </div>

      {/* Countdown */}
      <div className="flex items-center gap-1.5 pt-2 border-t border-white/5">
        <Clock size={11} className={isDue ? 'text-neon-cyan' : 'text-amber-400'} />
        <span className={`text-[10px] font-mono ${isDue ? 'text-neon-cyan' : 'text-amber-400'}`}>
          {isDue ? 'Publicando...' : `En ${countdown}`}
        </span>
      </div>
    </div>
  )
}

// ── Card: Shorts planned (pending slot) ─────────────────────
function ShortsPlannedCard({ slot }: { slot: ShortsPipelineSlot }) {
  const colors = getChannelStyles({ channel_id: slot.channel_id, channel_slug: slot.channel_slug })
  const isNative = slot.short_type === 'native'
  const typeBorder = isNative ? 'border-emerald-400/30' : 'border-orange-400/30'
  const countdown = formatCountdown(slot.scheduled_at)

  return (
    <div className={`pipeline-card rounded-xl p-4 border ${colors.bg} ${colors.border} border-l-2 ${typeBorder} animate-fade-in`}>
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: slot.channel_id, channel_slug: slot.channel_slug, channel_name: slot.channel_name })}
        </span>
        <ContentTypeBadge type={isNative ? 'native' : 'clip'} />
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
  const colors = getChannelStyles({ channel_id: slot.channel_id, channel_slug: slot.channel_slug })
  const isNative = slot.short_type === 'native'
  const pct = slot.job_progress || 0
  const phase = slot.job_phase || 'inicio'

  return (
    <div className="pipeline-card rounded-xl p-4 border bg-dark-800/80 border-l-2 border-emerald-400/30 animate-fade-in">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot} animate-pulse`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: slot.channel_id, channel_slug: slot.channel_slug, channel_name: slot.channel_name })}
        </span>
        <ContentTypeBadge type={isNative ? 'native' : 'clip'} />
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

// ── Card: Shorts ready to upload (pre-rendered clip, v25) ───
function ShortsReadyUploadCard({ short }: { short: ShortsPipelineSlot }) {
  const colors = getChannelStyles({ channel_id: short.channel_id, channel_slug: short.channel_slug })
  const countdown = formatCountdown(short.target_upload_at || short.scheduled_at)

  return (
    <div className="pipeline-card rounded-xl p-4 border bg-dark-800/80 border-l-2 border-green-500/40 animate-fade-in">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: short.channel_id, channel_slug: short.channel_slug, channel_name: short.channel_name })}
        </span>
        <ContentTypeBadge type="clip" />
        <span className="text-[10px] text-green-400 font-mono ml-auto flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
          Listo para subir
        </span>
      </div>

      {/* Timeline */}
      {short.target_upload_at && (
        <div className="flex items-center gap-2 text-xs">
          <Upload size={11} className="text-gray-500" />
          <span className="text-gray-400">Subida:</span>
          <span className="text-white font-mono">{toLocalTime(short.target_upload_at)}</span>
        </div>
      )}
      <div className="flex items-center gap-2 text-xs mt-1">
        <Smartphone size={11} className="text-gray-500" />
        <span className="text-gray-400">Clip pre-renderizado</span>
      </div>

      {/* Countdown */}
      <div className="flex items-center gap-1.5 mt-3 pt-2 border-t border-white/5">
        <Clock size={11} className={countdown === 'Ahora' ? 'text-neon-cyan' : 'text-blue-400'} />
        <span className={`text-[10px] font-mono ${countdown === 'Ahora' ? 'text-neon-cyan' : 'text-blue-400'}`}>
          {countdown === 'Ahora' ? 'Inminente' : `Subida en ${countdown}`}
        </span>
      </div>
    </div>
  )
}

// ── Card: Published in last 24h ──────────────────────────────
function PublishedCard({ item }: { item: PublishedItem }) {
  const colors = getChannelStyles({ channel_id: item.channel_id, channel_slug: item.channel_slug })
  const ytUrl = item.youtube_id ? `https://youtube.com/watch?v=${item.youtube_id}` : null

  return (
    <div className="pipeline-card rounded-xl p-4 border bg-dark-800/40 border-l-2 border-l-green-500/30 hover:opacity-80 transition-opacity animate-fade-in">
      {/* Header */}
      <div className="flex items-center gap-2 mb-2">
        <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
        <span className={`text-xs font-semibold ${colors.text}`}>
          {getChannelShort({ channel_id: item.channel_id, channel_slug: item.channel_slug, channel_name: item.channel_name })}
        </span>
        <ContentTypeBadge type={item.content_type} />
        {ytUrl && (
          <a href={ytUrl} target="_blank" rel="noopener noreferrer"
             className="text-[10px] text-green-400 hover:text-green-300 font-mono ml-auto flex items-center gap-1">
            <ExternalLink size={10} />
            Ver
          </a>
        )}
      </div>

      {/* Title */}
      {item.title && (
        <p className="text-[10px] text-gray-400 truncate mb-2">{item.title}</p>
      )}

      {/* Published time */}
      <div className="flex items-center gap-2 text-[10px]">
        <CheckCircle2 size={11} className="text-green-400" />
        <span className="text-gray-400">Publicado:</span>
        <span className="text-white font-mono">
          {toLocalTime(item.published_at)} {toLocalDate(item.published_at)}
        </span>
      </div>
    </div>
  )
}

// ── Merged slot types for interleaved pipeline columns ────────
type PlannedItem =
  | { _type: 'video'; data: PlannedSlot }
  | { _type: 'shorts-pending'; data: ShortsPipelineSlot }

type GeneratingItem =
  | { _type: 'video'; data: GeneratingVideo }
  | { _type: 'shorts'; data: ShortsPipelineSlot }

type AwaitingUploadItem =
  | { _type: 'video'; data: AwaitingUploadVideo }
  | { _type: 'shorts-ready'; data: ShortsPipelineSlot }

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
  const { data, isLoading: loading, error: queryError, refetch } = usePipelineStatus()
  const error = queryError ? String(queryError) : null

  // Use memo to compute derived state from the cached query result
  const {
    planned, generating, awaitingUpload, warming, published24h,
    shortsPending, shortsGenerating, shortsCompleted, shortsReady,
    mergedPlanned, mergedGenerating, mergedAwaitingUpload,
  } = useMemo(() => {
    if (!data) {
      return {
        planned: [] as PlannedSlot[], generating: [] as GeneratingVideo[],
        awaitingUpload: [] as AwaitingUploadVideo[], warming: [] as WarmingVideo[],
        published24h: [] as PublishedItem[],
        shortsPending: [] as ShortsPipelineSlot[], shortsGenerating: [] as ShortsPipelineSlot[],
        shortsCompleted: [] as ShortsPipelineSlot[],
        shortsReady: [] as ShortsPipelineSlot[],
        mergedPlanned: [] as PlannedItem[],
        mergedGenerating: [] as GeneratingItem[],
        mergedAwaitingUpload: [] as AwaitingUploadItem[],
      }
    }
    const p: PlannedSlot[] = data.planned || []
    const g: GeneratingVideo[] = data.generating || []
    const a: AwaitingUploadVideo[] = data.awaiting_upload || []
    const w: WarmingVideo[] = data.warming || []
    const pub: PublishedItem[] = data.published_24h || []
    const sp: ShortsPipelineSlot[] = data.shorts?.pending || []
    const sg: ShortsPipelineSlot[] = data.shorts?.generating || []
    const sc: ShortsPipelineSlot[] = data.shorts?.completed || []
    const sr: ShortsPipelineSlot[] = data.shorts?.ready_to_upload || []

    // Merge and sort by scheduled_at / timestamp (most recent first)
    const mergedPlannedVal: PlannedItem[] = [
      ...p.map(s => ({ _type: 'video' as const, data: s })),
      ...sp.map(s => ({ _type: 'shorts-pending' as const, data: s })),
    ].sort((a, b) => new Date(a.data.scheduled_at).getTime() - new Date(b.data.scheduled_at).getTime())

    const mergedGeneratingVal: GeneratingItem[] = [
      ...g.map(v => ({ _type: 'video' as const, data: v })),
      ...sg.map(s => ({ _type: 'shorts' as const, data: s })),
    ].sort((a, b) => {
      const at = a._type === 'video' ? (a.data as GeneratingVideo).created_at : (a.data as ShortsPipelineSlot).scheduled_at
      const bt = b._type === 'video' ? (b.data as GeneratingVideo).created_at : (b.data as ShortsPipelineSlot).scheduled_at
      return new Date(at).getTime() - new Date(bt).getTime()
    })

    const mergedAwaitingUploadVal: AwaitingUploadItem[] = [
      ...a.map(v => ({ _type: 'video' as const, data: v })),
      ...sr.map(s => ({ _type: 'shorts-ready' as const, data: s })),
    ].sort((a, b) => {
      const at = a._type === 'video' ? (a.data as AwaitingUploadVideo).created_at : (a.data as ShortsPipelineSlot).scheduled_at
      const bt = b._type === 'video' ? (b.data as AwaitingUploadVideo).created_at : (b.data as ShortsPipelineSlot).scheduled_at
      return new Date(at).getTime() - new Date(bt).getTime()
    })

    return {
      planned: p, generating: g, awaitingUpload: a, warming: w, published24h: pub,
      shortsPending: sp, shortsGenerating: sg, shortsCompleted: sc, shortsReady: sr,
      mergedPlanned: mergedPlannedVal,
      mergedGenerating: mergedGeneratingVal,
      mergedAwaitingUpload: mergedAwaitingUploadVal,
    }
  }, [data])

  async function handleUploadNow(videoId: number) {
    try {
      await api.uploadVideo(videoId)
      refetch()
    } catch (e: any) {
      console.error('Upload now error:', e)
    }
  }

  const totalItems = planned.length + generating.length + awaitingUpload.length + warming.length
    + published24h.length + shortsPending.length + shortsGenerating.length + shortsCompleted.length
    + shortsReady.length

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
      {/* ── Column 1: Planned ─────────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Clock} title="Planificado" count={mergedPlanned.length} colorClass="text-amber-400" />
        {mergedPlanned.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay pendientes</p>
        ) : (
          <div className="space-y-3">
            {mergedPlanned.map((item) => {
              switch (item._type) {
                case 'video':
                  return <PlannedCard key={`vid-${item.data.slot_id}`} slot={item.data as PlannedSlot} />
                case 'shorts-pending':
                  return <ShortsPlannedCard key={`short-${item.data.slot_id}`} slot={item.data as ShortsPipelineSlot} />
              }
            })}
          </div>
        )}
      </div>

      {/* ── Column 2: Generating ──────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Loader2} title="Generando" count={mergedGenerating.length} colorClass="text-neon-cyan animate-spin" />
        {mergedGenerating.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay generaciones activas</p>
        ) : (
          <div className="space-y-3">
            {mergedGenerating.map((item) => {
              switch (item._type) {
                case 'video':
                  return <GeneratingCard key={`vid-${(item.data as GeneratingVideo).video_id}`} video={item.data as GeneratingVideo} />
                case 'shorts':
                  return <ShortsGeneratingCard key={`short-${item.data.slot_id}`} slot={item.data as ShortsPipelineSlot} />
              }
            })}
          </div>
        )}
      </div>

      {/* ── Column 3: Awaiting Upload ─────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={HardDrive} title="Pendiente subida" count={mergedAwaitingUpload.length} colorClass="text-blue-400" />
        {mergedAwaitingUpload.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos esperando subida</p>
        ) : (
          <div className="space-y-3">
            {mergedAwaitingUpload.map((item) => {
              switch (item._type) {
                case 'video':
                  return <AwaitingUploadCard key={`await-${(item.data as AwaitingUploadVideo).video_id}`} video={item.data as AwaitingUploadVideo} onUploadNow={handleUploadNow} />
                case 'shorts-ready':
                  return <ShortsReadyUploadCard key={`ready-${item.data.slot_id}`} short={item.data as ShortsPipelineSlot} />
              }
            })}
          </div>
        )}
      </div>

      {/* ── Column 4: Warming ─────────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={Lock} title="No listado (calentando)" count={warming.length} colorClass="text-amber-400" />
        {warming.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay videos en calentamiento</p>
        ) : (
          <div className="space-y-3">
            {warming.map((video) => (
              <WarmingCard key={video.video_id} video={video} />
            ))}
          </div>
        )}
      </div>

      {/* ── Column 5: Published 24h ──────────────────────────── */}
      <div className="pipeline-column">
        <ColumnHeader icon={CheckCircle2} title="Publicados (24h)" count={published24h.length} colorClass="text-green-400" />
        {published24h.length === 0 ? (
          <p className="text-[10px] text-gray-600 text-center py-4">No hay publicados recientes</p>
        ) : (
          <div className="space-y-3">
            {published24h.map((item, idx) => (
              <PublishedCard key={`pub-${item.content_type}-${item.id}-${idx}`} item={item} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
