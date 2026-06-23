import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api, formatDate, formatDateTime, statusBadge, statusLabel, apiUrl } from '../lib/api'
import { Radio, Video, Upload, FileText, Clock, AlertCircle, TrendingUp } from 'lucide-react'

export default function Dashboard() {
  const [stats, setStats] = useState<any>(null)
  const [recentVideos, setRecentVideos] = useState<any[]>([])
  const [logs, setLogs] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    async function load() {
      try {
        const [s, v, l] = await Promise.all([
          api.getStats(),
          api.getVideos(undefined, undefined, 8),
          api.getLogs(),
        ])
        setStats(s)
        setRecentVideos(v)
        setLogs(l)
      } catch (e) {
        console.error(e)
      } finally {
        setLoading(false)
      }
    }
    load()
    // Refresh every 15 seconds
    const interval = setInterval(load, 15000)
    return () => clearInterval(interval)
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="max-w-7xl mx-auto space-y-6 animate-fade-in">
      {/* Stats Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard icon={Radio} label="Canales" value={stats?.channels ?? 0} color="text-neon-cyan" />
        <StatCard icon={Video} label="Videos Totales" value={stats?.total_videos ?? 0} color="text-neon-red" />
        <StatCard icon={Upload} label="Subidos" value={stats?.uploaded_videos ?? 0} color="text-purple-400" />
        <StatCard icon={FileText} label="Contenido Pendiente" value={stats?.unused_content ?? 0} color="text-neon-gold" />
      </div>

      {/* Recent Videos */}
      <section className="glass rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
            <TrendingUp size={20} className="text-neon-red" />
            Últimos Videos
          </h3>
          <Link to="/channels" className="text-sm text-neon-red hover:underline">
            Ver canales →
          </Link>
        </div>

        {recentVideos.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            <Video size={40} className="mx-auto mb-3 opacity-30" />
            <p>No hay videos todavía</p>
            <p className="text-xs mt-1">Crea un canal y genera tu primer video</p>
          </div>
        ) : (
          <div className="space-y-2">
            {recentVideos.map((v: any) => (
              <Link
                key={v.id}
                to={`/videos/${v.id}/edit`}
                className="flex items-center gap-4 p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all duration-200 group"
              >
                {/* Thumbnail placeholder */}
                <div className="w-28 h-16 bg-dark-600 rounded overflow-hidden shrink-0 relative">
                  {v.video_path ? (
                    <video
                      src={apiUrl(`/video-file/${v.id}`)}
                      className="w-full h-full object-cover opacity-60 group-hover:opacity-100 transition-opacity"
                      muted
                      preload="metadata"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-dark-600">
                      <Video size={20} className="text-gray-600" />
                    </div>
                  )}
                  <span className={`badge absolute top-1.5 right-1.5 ${statusBadge(v.status || 'draft')}`}>
                    {statusLabel(v.status || 'draft')}
                  </span>
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-white truncate">
                    {v.titulo_final || 'Video sin título'}
                  </p>
                  <p className="text-xs text-gray-500">
                    {v.channel_name || v.canal} · {formatDate(v.created_at)}
                    {v.duracion_seg ? ` · ${Math.floor(v.duracion_seg / 60)}:${String(v.duracion_seg % 60).padStart(2, '0')}` : ''}
                  </p>
                </div>
                {v.yt_url && (
                  <a href={v.yt_url} target="_blank" rel="noopener noreferrer" className="text-xs text-neon-red hover:underline shrink-0"
                    onClick={e => e.stopPropagation()}>
                    Ver en YT ↗
                  </a>
                )}
              </Link>
            ))}
          </div>
        )}
      </section>

      {/* Pipeline Log */}
      <section className="glass rounded-xl p-5">
        <h3 className="font-display text-lg font-semibold text-white mb-4 flex items-center gap-2">
          <Clock size={20} className="text-neon-cyan" />
          Log del Pipeline
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 border-b border-surface-border">
                <th className="pb-2 font-medium">Hora</th>
                <th className="pb-2 font-medium">Fase</th>
                <th className="pb-2 font-medium">Estado</th>
                <th className="pb-2 font-medium">Mensaje</th>
              </tr>
            </thead>
            <tbody>
              {logs.slice(0, 15).map((row: any, i: number) => (
                <tr key={i} className="border-b border-surface-border/30 hover:bg-dark-700/30">
                  <td className="py-1.5 text-gray-500 font-mono text-xs">
                    {formatDateTime(row.created_at)}
                  </td>
                  <td className="py-1.5">
                    <span className="px-1.5 py-0.5 rounded bg-dark-600 text-xs text-gray-400">
                      {row.phase}
                    </span>
                  </td>
                  <td className="py-1.5">
                    <span className={`text-xs ${row.status === 'success' ? 'text-green-400' : row.status === 'error' ? 'text-red-400' : 'text-yellow-400'}`}>
                      {row.status === 'success' ? '✓' : row.status === 'error' ? '✗' : '○'} {row.status}
                    </span>
                  </td>
                  <td className="py-1.5 text-gray-400 text-xs max-w-md truncate">
                    {row.message || '-'}
                  </td>
                </tr>
              ))}
              {logs.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-6 text-center text-gray-600">
                    <AlertCircle size={20} className="mx-auto mb-2 opacity-30" />
                    Sin actividad reciente
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, color }: { icon: any; label: string; value: number; color: string }) {
  return (
    <div className="glass rounded-xl p-4 flex items-center gap-3">
      <Icon size={24} className={`${color} opacity-80`} />
      <div>
        <p className={`text-2xl font-bold ${color} font-mono`}>{value}</p>
        <p className="text-xs text-gray-500">{label}</p>
      </div>
    </div>
  )
}
