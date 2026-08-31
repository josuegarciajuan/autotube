import { Link } from 'react-router-dom'
import { Clock, ExternalLink, Smartphone, Scissors, AlertTriangle, CheckCircle, Loader2 } from 'lucide-react'
import { formatDate, formatDateTime, parseApiDate } from '../lib/api'
import { CHANNEL_PILL, DEFAULT_PILL } from '../lib/channelConfig'

interface RecentShort {
  id: number
  title: string | null
  youtube_id: string | null
  youtube_url: string | null
  duration: number | null
  published_at: string | null
  status: string | null
  publish_at: string | null
  yt_visibility: string | null
  channel_name: string
  channel_slug: string
}

interface RecentShortsProps {
  shorts: RecentShort[]
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return ''
  const now = Date.now()
  const then = parseApiDate(dateStr)?.getTime()
  if (then === undefined) return ''
  const diffMs = now - then
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return 'Ahora'
  if (diffMin < 60) return `hace ${diffMin}min`
  const diffH = Math.floor(diffMin / 60)
  if (diffH < 24) return `hace ${diffH}h`
  const diffD = Math.floor(diffH / 24)
  if (diffD === 1) return 'Ayer'
  if (diffD < 30) return `hace ${diffD}d`
  return formatDate(dateStr)
}

function fmtDurationSec(sec: number | null): string {
  if (!sec) return ''
  if (sec < 60) return `${Math.round(sec)}s`
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function statusBadge(status: string | null) {
  const st = (status || '').toLowerCase()
  if (st === 'error' || st === 'failed') {
    return { icon: <AlertTriangle size={11} />, label: 'Error', cls: 'bg-red-500/15 text-red-400 border-red-500/30' }
  }
  if (st === 'generating' || st === 'rendering' || st === 'extracted' || st === 'uploading') {
    return { icon: <Loader2 size={11} className="animate-spin" />, label: st.charAt(0).toUpperCase() + st.slice(1), cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30' }
  }
  if (st === 'published') {
    return { icon: <CheckCircle size={11} />, label: 'Publicado', cls: 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30' }
  }
  if (st === 'scheduled') {
    return { icon: <Clock size={11} />, label: 'Programado', cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30' }
  }
  if (st === 'ready') {
    return { icon: <CheckCircle size={11} />, label: 'Ready', cls: 'bg-blue-400/15 text-blue-400 border-blue-400/30' }
  }
  if (st === 'pending') {
    return { icon: <Clock size={11} />, label: 'Pendiente', cls: 'bg-gray-400/15 text-gray-400 border-gray-400/30' }
  }
  return null
}

export default function RecentShorts({ shorts }: RecentShortsProps) {
  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Smartphone size={20} className="text-emerald-400" />
          Últimos Shorts publicados
        </h3>
      </div>

      {shorts.length === 0 ? (
        <div className="text-center py-8">
          <Scissors size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Aún no hay Shorts publicados</p>
          <p className="text-gray-600 text-xs mt-1">
            Los Shorts aparecerán aquí tras ser publicados en YouTube
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {shorts.map((s) => (
            <div
              key={s.id}
              className="flex items-center gap-3 p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all group border border-surface-border/30 overflow-hidden"
            >
              {/* Shorts icon */}
              <div className="shrink-0 w-8 h-8 rounded-lg bg-emerald-400/10 flex items-center justify-center">
                <Smartphone size={14} className="text-emerald-400" />
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-white truncate">
                    {s.title || 'Short sin título'}
                  </p>
                  {s.duration && (
                    <span className="text-[10px] text-gray-500 font-mono shrink-0">
                      {fmtDurationSec(s.duration)}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-0.5 min-w-0">
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium border shrink-0 ${
                      CHANNEL_PILL[s.channel_slug] || DEFAULT_PILL
                    }`}
                  >
                    {s.channel_name}
                  </span>
                  <span className="text-[10px] text-gray-500 flex items-center gap-1 shrink-0">
                    <Clock size={10} />
                    {s.status === 'scheduled'
                      ? (s.publish_at ? `pub. ${formatDateTime(s.publish_at)}` : 'programado')
                      : timeAgo(s.published_at)}
                    {s.status !== 'scheduled' && s.published_at && (
                      <>
                        <span aria-hidden="true">·</span>
                        <time dateTime={s.published_at}>{formatDateTime(s.published_at)}</time>
                      </>
                    )}
                  </span>
                  {statusBadge(s.status) && (
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border flex items-center gap-1 shrink-0 ${statusBadge(s.status)!.cls}`}>
                      {statusBadge(s.status)!.icon}
                      {statusBadge(s.status)!.label}
                    </span>
                  )}
                </div>
              </div>

              {/* Video detail link */}
              <Link
                to={`/videos/${s.id}/edit`}
                className="text-xs text-emerald-400 hover:underline shrink-0 flex items-center gap-1"
              >
                <ExternalLink size={12} />
              </Link>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
