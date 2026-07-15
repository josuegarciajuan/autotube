import { Link } from 'react-router-dom'
import { Clock, ExternalLink, Play, Film, AlertTriangle, CheckCircle, Loader2 } from 'lucide-react'
import { formatDate } from '../lib/api'

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
}

interface RecentVideosProps {
  videos: RecentVideo[]
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return ''
  const now = Date.now()
  const then = new Date(dateStr).getTime()
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

function fmtDuration(sec: number | null): string {
  if (!sec) return ''
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function statusBadge(status: string | null) {
  const st = (status || '').toLowerCase()
  if (st === 'error' || st === 'failed') {
    return { icon: <AlertTriangle size={11} />, label: 'Error', cls: 'bg-red-500/15 text-red-400 border-red-500/30' }
  }
  if (st === 'generating' || st === 'rendering' || st === 'assembled' || st === 'reassembling') {
    return { icon: <Loader2 size={11} className="animate-spin" />, label: st.charAt(0).toUpperCase() + st.slice(1), cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30' }
  }
  if (st === 'uploaded' || st === 'published' || st === 'uploaded_private') {
    return { icon: <CheckCircle size={11} />, label: st === 'uploaded_private' ? 'Scheduled' : (st.charAt(0).toUpperCase() + st.slice(1)), cls: 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30' }
  }
  if (st === 'ready') {
    return { icon: <CheckCircle size={11} />, label: 'Ready', cls: 'bg-blue-400/15 text-blue-400 border-blue-400/30' }
  }
  // draft, queued, or unknown
  if (st === 'draft' || st === 'queued') {
    return { icon: <Clock size={11} />, label: st === 'draft' ? 'Borrador' : 'En cola', cls: 'bg-gray-400/15 text-gray-400 border-gray-400/30' }
  }
  return null
}

const CHANNEL_COLORS: Record<string, string> = {
  canal2: 'bg-neon-cyan/20 text-neon-cyan border-neon-cyan/30',
  canal3: 'bg-amber-400/20 text-amber-400 border-amber-400/30',
  canal4: 'bg-purple-400/20 text-purple-400 border-purple-400/30',
}

export default function RecentVideos({ videos }: RecentVideosProps) {
  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Film size={20} className="text-neon-red" />
          Últimos videos publicados
        </h3>
      </div>

      {videos.length === 0 ? (
        <div className="text-center py-8">
          <Play size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Aún no hay videos publicados</p>
          <p className="text-gray-600 text-xs mt-1">
            Los videos aparecerán aquí tras ser subidos a YouTube
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {videos.map((v) => (
            <div
              key={v.id}
              className="flex items-center gap-3 p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all group border border-surface-border/30"
            >
              {/* Thumbnail placeholder */}
              <div className="shrink-0 w-12 h-8 rounded bg-dark-600 flex items-center justify-center overflow-hidden">
                <Play size={14} className="text-gray-500" />
              </div>

              {/* Info */}
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
                <div className="flex items-center gap-2 mt-0.5">
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${
                      CHANNEL_COLORS[v.channel_slug] || 'bg-gray-500/20 text-gray-400 border-gray-500/30'
                    }`}
                  >
                    {v.channel_name}
                  </span>
                  <span className="text-[10px] text-gray-500 flex items-center gap-1">
                    <Clock size={10} />
                    {timeAgo(v.uploaded_at)}
                  </span>
                  {statusBadge(v.status) && (
                    <span className={`ml-auto px-1.5 py-0.5 rounded text-[10px] font-medium border flex items-center gap-1 ${statusBadge(v.status)!.cls}`}>
                      {statusBadge(v.status)!.icon}
                      {statusBadge(v.status)!.label}
                    </span>
                  )}
                </div>
              </div>

              {/* YouTube link */}
              {v.yt_url && (
                <a
                  href={v.yt_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-neon-red hover:underline shrink-0 flex items-center gap-1"
                >
                  <ExternalLink size={12} />
                </a>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
