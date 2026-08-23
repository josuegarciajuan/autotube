import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Trophy, ExternalLink, Eye, ThumbsUp, MessageCircle, DollarSign, Clock, Smartphone, Film } from 'lucide-react'
import { formatShortNumber, formatDate, apiUrl } from '../lib/api'

interface TopVideo {
  id: number
  titulo_final: string | null
  yt_video_id: string | null
  yt_url: string | null
  duracion_seg: number | null
  video_path: string | null
  created_at: string
  channel_name: string
  channel_slug: string
  views: number | null
  likes: number | null
  comments: number | null
  estimated_minutes_watched: number | null
  average_view_duration: number | null
  estimated_revenue_min?: number | null
  estimated_revenue_max?: number | null
  stats_updated: string | null
  kind?: 'video' | 'short'   // 'short' = YouTube Short
}

interface TopVideosProps {
  videos: TopVideo[]
}

const rankColors = ['text-neon-gold', 'text-gray-300', 'text-amber-600']

function VideoRow({ v, i }: { v: TopVideo; i: number }) {
  return (
    <Link
      key={v.id}
      to={`/videos/${v.id}/edit`}
      className="flex items-center gap-3 p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all group"
    >
      <span className={`font-bold text-lg w-6 shrink-0 text-center ${rankColors[i] || 'text-gray-600'}`}>
        {i + 1}
      </span>
      <div className="w-16 h-10 bg-dark-600 rounded overflow-hidden shrink-0 relative">
        {v.kind === 'short' ? (
          <div className="w-full h-full flex items-center justify-center bg-emerald-400/10">
            <Smartphone size={16} className="text-emerald-400" />
          </div>
        ) : v.video_path ? (
          <video
            src={apiUrl(`/video-file/${v.id}`)}
            className="w-full h-full object-cover opacity-50 group-hover:opacity-80 transition-opacity"
            muted
            preload="metadata"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <Eye size={14} className="text-gray-700" />
          </div>
        )}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-white truncate">
          {v.titulo_final || 'Sin título'}
        </p>
        <p className="text-xs text-gray-500">
          {v.channel_name} · {formatDate(v.created_at)}
        </p>
        {/* Stats bars */}
        <div className="flex items-center gap-3 mt-1.5 text-xs">
          <span className="flex items-center gap-1 text-neon-cyan" title="Vistas">
            <Eye size={11} />
            {v.views != null ? formatShortNumber(v.views) : '—'}
          </span>
          <span className="flex items-center gap-1 text-green-400" title="Horas de visualización">
            <Clock size={11} />
            {v.estimated_minutes_watched != null
              ? `${Math.round(v.estimated_minutes_watched / 6) / 10}h`
              : '—'}
          </span>
          <span className="flex items-center gap-1 text-neon-red" title="Likes">
            <ThumbsUp size={11} />
            {v.likes != null ? formatShortNumber(v.likes) : '—'}
          </span>
          <span className="flex items-center gap-1 text-neon-purple" title="Comentarios">
            <MessageCircle size={11} />
            {v.comments != null ? formatShortNumber(v.comments) : '—'}
          </span>
          {(v.estimated_revenue_min != null || v.estimated_revenue_max != null) && (
            <span className="flex items-center gap-1 text-neon-gold" title="Revenue estimado">
              <DollarSign size={11} />
              {v.estimated_revenue_min != null ? `$${v.estimated_revenue_min.toFixed(1)}` : '—'}
            </span>
          )}
        </div>
      </div>
      {v.yt_url && (
        <a
          href={v.yt_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-neon-red hover:underline shrink-0 flex items-center gap-1"
          onClick={e => e.stopPropagation()}
        >
          <ExternalLink size={12} />
        </a>
      )}
    </Link>
  )
}

function ColumnEmpty({ kind }: { kind: 'video' | 'short' }) {
  return (
    <div className="text-center py-8 px-3">
      <Smartphone size={30} className="mx-auto mb-2 text-gray-700" />
      <p className="text-gray-500 text-sm">
        Sin {kind === 'short' ? 'shorts' : 'videos'} con datos todavía
      </p>
    </div>
  )
}

export default function TopVideos({ videos }: TopVideosProps) {
  const longForm = useMemo(
    () => videos
      .filter(v => (v.kind ?? 'video') === 'video')
      .sort((a, b) => (b.views ?? 0) - (a.views ?? 0))
      .slice(0, 25),
    [videos],
  )

  const shorts = useMemo(
    () => videos
      .filter(v => v.kind === 'short')
      .sort((a, b) => (b.views ?? 0) - (a.views ?? 0))
      .slice(0, 25),
    [videos],
  )

  if (videos.length === 0) {
    return (
      <section className="glass rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Trophy size={20} className="text-neon-gold" />
          <h3 className="font-display text-lg font-semibold text-white">Top videos y shorts</h3>
        </div>
        <div className="text-center py-8">
          <Eye size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Aún no hay datos de rendimiento</p>
          <p className="text-gray-600 text-xs mt-1">
            Las estadísticas aparecerán cuando los videos acumulen visualizaciones
          </p>
        </div>
      </section>
    )
  }

  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center gap-2 mb-4">
        <Trophy size={20} className="text-neon-gold" />
        <h3 className="font-display text-lg font-semibold text-white">Top videos y shorts</h3>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
        {/* Columna: Videos long-form */}
        <div className="min-w-0">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-white mb-3 px-1">
            <Film size={16} className="text-neon-cyan" />
            Videos
            <span className="text-xs text-gray-500 font-normal">({longForm.length})</span>
          </h4>
          {longForm.length === 0 ? (
            <ColumnEmpty kind="video" />
          ) : (
            <div className="space-y-2">
              {longForm.map((v, i) => (
                <VideoRow key={v.id} v={v} i={i} />
              ))}
            </div>
          )}
        </div>

        {/* Columna: Shorts */}
        <div className="min-w-0">
          <h4 className="flex items-center gap-2 text-sm font-semibold text-white mb-3 px-1">
            <Smartphone size={16} className="text-emerald-400" />
            Shorts
            <span className="text-xs text-gray-500 font-normal">({shorts.length})</span>
          </h4>
          {shorts.length === 0 ? (
            <ColumnEmpty kind="short" />
          ) : (
            <div className="space-y-2">
              {shorts.map((v, i) => (
                <VideoRow key={v.id} v={v} i={i} />
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
