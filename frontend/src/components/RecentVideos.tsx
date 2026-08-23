import { Link } from 'react-router-dom'
import { ExternalLink, Play, Film, AlertTriangle, Loader2, Zap, Upload, CheckCircle, Circle, Trophy } from 'lucide-react'
import { formatDate } from '../lib/api'
import { CHANNEL_PILL, DEFAULT_PILL } from '../lib/channelConfig'

interface ActionHistoryItem {
  action: string
  date: string
}

interface RecentVideo {
  id: number
  titulo_final: string | null
  yt_video_id: string | null
  yt_url: string | null
  duracion_seg: number | null
  uploaded_at: string | null
  status: string | null
  channel_name: string
  channel_slug: string
  action_history?: ActionHistoryItem[]
  is_marathon?: number | boolean
  marathon_config?: string | Record<string, any>
}

interface RecentVideosProps {
  videos: RecentVideo[]
}

function fmtTime(dateStr: string): string {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
}

function fmtDuration(sec: number | null): string {
  if (!sec) return ''
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function actionColor(action: string): { dot: string; text: string; iconBg: string } {
  switch (action) {
    case 'Generándose': return { dot: 'bg-purple-400', text: 'text-purple-400', iconBg: 'bg-purple-400/15' }
    case 'Generado':    return { dot: 'bg-amber-400',  text: 'text-amber-400',  iconBg: 'bg-amber-400/15' }
    case 'Subido':      return { dot: 'bg-blue-400',   text: 'text-blue-400',   iconBg: 'bg-blue-400/15' }
    case 'Publicado':   return { dot: 'bg-emerald-400',text: 'text-emerald-400',iconBg: 'bg-emerald-400/15' }
    default:            return { dot: 'bg-gray-400',   text: 'text-gray-400',   iconBg: 'bg-gray-400/15' }
  }
}

function actionIcon(action: string) {
  switch (action) {
    case 'Generándose': return <Loader2 size={9} className="animate-spin" />
    case 'Generado':    return <Zap size={9} />
    case 'Subido':      return <Upload size={9} />
    case 'Publicado':   return <CheckCircle size={9} />
    default:            return <Circle size={9} />
  }
}

export default function RecentVideos({ videos }: RecentVideosProps) {
  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Film size={20} className="text-neon-red" />
          Flujo de videos
        </h3>
      </div>

      {videos.length === 0 ? (
        <div className="text-center py-8">
          <Play size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Sin actividad reciente</p>
          <p className="text-gray-600 text-xs mt-1">
            Las acciones del pipeline aparecerán aquí
          </p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {videos.map((v) => {
            const history = v.action_history || []
            const lastAction = history.length > 0 ? history[history.length - 1] : null

            return (
              <div
                key={v.id}
                className="p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all group border border-surface-border/30 overflow-hidden"
              >
                {/* Top row: thumbnail + title + link */}
                <div className="flex items-center gap-3">
                  {/* Thumbnail placeholder */}
                  <div className="shrink-0 w-10 h-7 rounded bg-dark-600 flex items-center justify-center overflow-hidden">
                    <Play size={12} className="text-gray-500" />
                  </div>

                  {/* Title + channel */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium text-white truncate">
                        {v.titulo_final || 'Sin título'}
                      </p>
                      {v.duracion_seg && (
                        <span className="text-[10px] text-gray-500 font-mono shrink-0">
                          {fmtDuration(v.duracion_seg)}
                        </span>
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-0.5 min-w-0">
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium border shrink-0 ${
                          CHANNEL_PILL[v.channel_slug] || DEFAULT_PILL
                        }`}
                      >
                        {v.channel_name}
                      </span>
                      {v.is_marathon && (
                        <span
                          className="px-1.5 py-0.5 rounded text-[10px] font-bold border border-amber-500/40 bg-amber-500/15 text-amber-400 flex items-center gap-1 shrink-0"
                          title={
                            v.marathon_config
                              ? `Maratón ${typeof v.marathon_config === 'string' ? JSON.parse(v.marathon_config).duration_target : (v.marathon_config as any).duration_target}min`
                              : 'Video maratón ~1h'
                          }
                        >
                          <Trophy size={10} />
                          MARATÓN
                        </span>
                      )}
                      {!lastAction && (
                        <span className="text-[10px] text-gray-500 shrink-0">
                          Sin acciones registradas
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Detail link */}
                  <Link
                    to={`/videos/${v.id}/edit`}
                    className="text-xs text-neon-red hover:underline shrink-0 flex items-center gap-1"
                  >
                    <ExternalLink size={12} />
                  </Link>
                </div>

                {/* Action timeline — responsive: wraps on narrow screens, no horizontal scroll */}
                {history.length > 0 && (
                  <div className="mt-2 flex flex-wrap items-center gap-x-0.5 gap-y-1 min-w-0">
                    {history.map((a, i) => {
                      const colors = actionColor(a.action)
                      const isLast = i === history.length - 1
                      return (
                        <div key={i} className="flex items-center shrink-0">
                          {/* Pill: dot + icon + label + time */}
                          <div
                            className={`flex items-center gap-1 px-1.5 py-0.5 rounded-full border text-[10px] ${
                              isLast
                                ? `${colors.iconBg} border-current/30 ${colors.text}`
                                : 'border-transparent bg-transparent text-gray-400'
                            }`}
                            title={`${a.action}: ${formatDate(a.date)}`}
                          >
                            <span>{actionIcon(a.action)}</span>
                            <span className="font-medium">{a.action}</span>
                            <span className="text-[9px] text-gray-500 font-mono">
                              {fmtTime(a.date)}
                            </span>
                          </div>

                          {/* Connector arrow */}
                          {!isLast && (
                            <span className="text-gray-600 px-0.5 shrink-0 select-none" aria-hidden="true">
                              →
                            </span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* Error state */}
                {v.status === 'error' || v.status === 'failed' ? (
                  <div className="mt-1.5 flex items-center gap-1 text-[10px] text-red-400">
                    <AlertTriangle size={10} />
                    Error en pipeline
                  </div>
                ) : null}
                {v.status === 'held' ? (
                  <div className="mt-1.5 flex items-center gap-1 text-[10px] text-red-400">
                    <AlertTriangle size={10} />
                    Retenido — revisar
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
