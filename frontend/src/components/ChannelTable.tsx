import { Link } from 'react-router-dom'
import { Radio } from 'lucide-react'
import { formatShortNumber } from '../lib/api'

interface ChannelRow {
  id: number
  name: string
  slug: string
  active: boolean
  subscribers: number | null
  total_views: number | null
  video_count: number | null
  engagement: number | null
  stats_updated: string | null
  uploaded_videos: number | null
  shorts_published: number | null
  total_likes: number | null
  longform_views: number | null
  shorts_views: number | null
  shorts_likes: number | null
  estimated_revenue_min?: number | null
  estimated_revenue_max?: number | null
  watch_hours?: number | null
}

interface ChannelTableProps {
  channels: ChannelRow[]
}

export default function ChannelTable({ channels }: ChannelTableProps) {
  const maxSubs = Math.max(...channels.map(c => c.subscribers || 0), 1)
  const maxViews = Math.max(...channels.map(c => c.total_views || 0), 1)
  const maxEngagement = Math.max(...channels.map(c => c.engagement || 0), 1)

  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Radio size={20} className="text-neon-cyan" />
          Canales
        </h3>
        <Link to="/channels" className="text-sm text-neon-red hover:underline">
          Gestionar →
        </Link>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 border-b border-surface-border">
              <th className="pb-3 font-medium pl-1">Canal</th>
              <th className="pb-3 font-medium">Suscriptores</th>
              <th className="pb-3 font-medium">Vistas totales</th>
              <th className="pb-3 font-medium hidden xl:table-cell">V. vídeos</th>
              <th className="pb-3 font-medium hidden xl:table-cell">V. shorts</th>
              <th className="pb-3 font-medium hidden sm:table-cell">Interacciones</th>
              <th className="pb-3 font-medium hidden sm:table-cell">Likes</th>
              <th className="pb-3 font-medium hidden md:table-cell">Videos</th>
              <th className="pb-3 font-medium hidden md:table-cell">Shorts</th>
              <th className="pb-3 font-medium hidden lg:table-cell">Revenue est.</th>
              <th className="pb-3 font-medium hidden lg:table-cell">Watch h</th>
            </tr>
          </thead>
          <tbody>
            {channels.length === 0 ? (
              <tr>
                  <td colSpan={11} className="py-8 text-center text-gray-600">
                  No hay canales activos
                </td>
              </tr>
            ) : (
              channels.map(ch => (
                <tr key={ch.id} className="border-b border-surface-border/30 hover:bg-dark-700/30 transition-colors">
                  <td className="py-3 pl-1">
                    <Link to={`/channels/${ch.id}`} className="flex items-center gap-2 hover:text-neon-cyan transition-colors">
                      <span className="w-2 h-2 rounded-full bg-green-500 shrink-0" />
                      <span className="font-medium text-white">{ch.name}</span>
                    </Link>
                  </td>
                  <td className="py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-white text-sm w-14 shrink-0">
                        {ch.subscribers != null ? formatShortNumber(ch.subscribers) : '—'}
                      </span>
                      <div className="w-16 h-1.5 bg-dark-600 rounded-full overflow-hidden hidden sm:block">
                        <div
                          className="h-full bg-neon-cyan rounded-full transition-all"
                          style={{ width: `${Math.min((ch.subscribers || 0) / maxSubs * 100, 100)}%` }}
                        />
                      </div>
                    </div>
                  </td>
                  <td className="py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-white text-sm w-14 shrink-0">
                        {ch.total_views != null ? formatShortNumber(ch.total_views) : '—'}
                      </span>
                      <div className="w-16 h-1.5 bg-dark-600 rounded-full overflow-hidden hidden sm:block">
                        <div
                          className="h-full bg-neon-red rounded-full transition-all"
                          style={{ width: `${Math.min((ch.total_views || 0) / maxViews * 100, 100)}%` }}
                        />
                      </div>
                    </div>
                  </td>
                  <td className="py-3 hidden xl:table-cell">
                    <span className="font-mono text-neon-cyan text-sm">
                      {ch.longform_views != null ? formatShortNumber(ch.longform_views) : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden xl:table-cell">
                    <span className="font-mono text-neon-purple text-sm">
                      {ch.shorts_views != null ? formatShortNumber(ch.shorts_views) : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden sm:table-cell">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-gray-400 text-sm w-12 shrink-0">
                        {ch.engagement != null ? formatShortNumber(ch.engagement) : '—'}
                      </span>
                      <div className="w-16 h-1.5 bg-dark-600 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-neon-gold rounded-full transition-all"
                          style={{ width: `${Math.min((ch.engagement || 0) / maxEngagement * 100, 100)}%` }}
                        />
                      </div>
                    </div>
                  </td>
                  <td className="py-3 hidden sm:table-cell">
                    <span className="font-mono text-gray-400 text-sm">
                      {ch.total_likes != null ? formatShortNumber(ch.total_likes) : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden md:table-cell">
                    <span className="font-mono text-gray-400 text-sm">
                      {ch.uploaded_videos != null ? ch.uploaded_videos : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden md:table-cell">
                    <span className="font-mono text-neon-purple text-sm">
                      {ch.shorts_published != null ? ch.shorts_published : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden lg:table-cell">
                    <span className="font-mono text-green-400 text-sm">
                      {ch.estimated_revenue_min != null || ch.estimated_revenue_max != null
                        ? `$${Math.round(ch.estimated_revenue_min || 0)}–$${Math.round(ch.estimated_revenue_max || 0)}`
                        : '—'}
                    </span>
                  </td>
                  <td className="py-3 hidden lg:table-cell">
                    <span className="font-mono text-gray-400 text-sm">
                      {ch.watch_hours != null ? formatShortNumber(ch.watch_hours) : '—'}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
