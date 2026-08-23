import { useEffect, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { api } from '../lib/api'
import { SOCIAL_PLATFORMS } from '../types/channel'

interface PlatformRow {
  platform: string
  platform_video_id: string | null
  platform_video_url: string | null
  status: string
  views: number
  likes: number
  comments: number
  reposts: number
  uploaded_at: string | null
  history?: Array<{ views: number; likes: number; comments: number; reposts: number; fetched_at: string }>
}

function platformMeta(id: string) {
  return SOCIAL_PLATFORMS.find(p => p.id === id)
}

function fmt(n: number | undefined | null): string {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
}

function statusBadge(status: string): string {
  switch (status) {
    case 'published': return 'bg-green-900/40 text-green-300 border border-green-700/40'
    case 'processing': return 'bg-cyan-900/40 text-cyan-300 border border-cyan-700/40'
    case 'uploading': case 'pending': return 'bg-amber-900/40 text-amber-300 border border-amber-700/40'
    case 'failed': return 'bg-red-900/40 text-red-300 border border-red-700/40'
    default: return 'bg-gray-800 text-gray-400 border border-gray-700'
  }
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    published: 'Publicado', processing: 'Procesando', uploading: 'Subiendo',
    pending: 'Pendiente', failed: 'Fallido',
  }
  return map[status] || status
}

export default function VideoSocialStats({ videoId }: { videoId: number }) {
  const [rows, setRows] = useState<PlatformRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!videoId) return
    setLoading(true)
    api.getVideoSocialStats(videoId)
      .then(data => setRows(Array.isArray(data?.per_platform) ? data.per_platform : []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [videoId])

  return (
    <div className="glass rounded-xl p-5 space-y-3 animate-fade-in">
      <h3 className="font-display text-sm font-semibold text-white flex items-center gap-2">
        🌐 Distribución en otras redes
      </h3>

      {loading ? (
        <div className="text-xs text-gray-500">Cargando...</div>
      ) : rows.length === 0 ? (
        <p className="text-xs text-gray-500">
          Este vídeo no se ha distribuido a otras redes todavía.
          Gestiona el backfill en la página <b className="text-neon-cyan">Distribución</b>.
        </p>
      ) : (
        <div className="space-y-2">
          {rows.map(row => {
            const meta = platformMeta(row.platform)
            const hist = row.history || []
            const first = hist.length > 0 ? hist[hist.length - 1] : null
            const last = hist.length > 0 ? hist[0] : null
            const delta = (first && last) ? (last.views || 0) - (first.views || 0) : 0
            return (
              <div key={row.platform} className="bg-dark-700/50 rounded-lg p-3 border border-surface-border">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-base">{meta?.icon || '🌐'}</span>
                    <span className="text-xs font-medium text-white">{meta?.label || row.platform}</span>
                    <span className={`px-2 py-0.5 rounded text-[10px] ${statusBadge(row.status)}`}>
                      {statusLabel(row.status)}
                    </span>
                  </div>
                  {row.platform_video_url && (
                    <a href={row.platform_video_url} target="_blank" rel="noopener noreferrer"
                       className="flex items-center gap-1 text-[10px] text-neon-cyan hover:text-neon-cyan/70 shrink-0">
                      <ExternalLink size={11} /> Ver en {meta?.label || row.platform}
                    </a>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400">
                  <span>👁️ <b className="text-white">{fmt(row.views)}</b></span>
                  <span>❤️ <b className="text-white">{fmt(row.likes)}</b></span>
                  <span>💬 <b className="text-white">{fmt(row.comments)}</b></span>
                  <span>🔁 <b className="text-white">{fmt(row.reposts)}</b></span>
                  {delta !== 0 && (
                    <span className={delta > 0 ? 'text-green-400' : 'text-red-400'}>
                      {delta > 0 ? '▲' : '▼'} {fmt(Math.abs(delta))} vistas
                    </span>
                  )}
                  {hist.length > 0 && (
                    <span className="text-gray-600">· {hist.length} puntos de historial</span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
