/**
 * SocialOverview — resumen de redes sociales en el Dashboard.
 *
 * Muestra, agregado por canal y por red (Rumble, Dailymotion, Facebook,
 * Bluesky, Mastodon), cuántas visitas trae cada red y el % sobre el total
 * de visitas sociales. Datos servidos por el dashboard (social_summary),
 * recogidos con APIs gratuitas (0 cuota de YouTube).
 */
import { Fragment } from 'react'
import { SOCIAL_PLATFORMS } from '../types/channel'

interface Props {
  channels: { id: number; name: string; slug: string }[]
  socialSummary: any
}

function fmt(n: number | undefined | null): string {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
}

function platformMeta(platform: string) {
  return SOCIAL_PLATFORMS.find(p => p.id === platform)
}

export default function SocialOverview({ channels, socialSummary }: Props) {
  if (!socialSummary) return null

  const byPlatform = socialSummary.by_platform || {}
  const totals = socialSummary.totals || {}
  const perChannel = socialSummary.per_channel || {}
  const viewsShare = socialSummary.views_share || {}
  const platformIds = Object.keys(byPlatform)

  if (platformIds.length === 0) {
    return (
      <div className="rounded-xl border border-dark-500 bg-dark-800/60 p-6 text-center">
        <p className="text-sm text-gray-500">
          Aún no hay vídeos publicados en redes sociales.
        </p>
        <p className="text-xs text-gray-600 mt-1 max-w-xl mx-auto">
          Configura cuentas en el panel del canal (pestaña Redes) y pulsa
          «Recolectar stats sociales» para medir las visitas que trae cada
          red (0 cuota de YouTube).
        </p>
      </div>
    )
  }

  const shareValues = Object.values(viewsShare) as number[]
  const maxShare = Math.max(...shareValues, 1)

  const statCards = [
    { label: 'Publicados', value: totals.published },
    { label: 'Vistas', value: totals.views },
    { label: 'Likes', value: totals.likes },
    { label: 'Comentarios', value: totals.comments },
    { label: 'Reposts', value: totals.reposts },
  ]

  return (
    <div className="space-y-5">
      {/* ── Totales globales ── */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        {statCards.map(s => (
          <div key={s.label} className="glass rounded-xl p-3 text-center">
            <div className="text-[10px] text-gray-500 uppercase tracking-wide">{s.label}</div>
            <div className="text-xl font-semibold text-white tabular-nums font-mono mt-1">
              {fmt(s.value)}
            </div>
          </div>
        ))}
      </div>

      {/* ── Visitas traídas por cada red (share bars) ── */}
      <div>
        <p className="text-xs text-gray-500 mb-2">Visitas traídas por cada red</p>
        <div className="space-y-1.5">
          {platformIds.map(platform => {
            const meta = platformMeta(platform)
            const bp = byPlatform[platform] || {}
            const share = viewsShare[platform] || 0
            return (
              <div key={platform} className="flex items-center gap-3">
                <span className="w-32 shrink-0 text-xs text-gray-300 truncate">
                  {meta?.icon} {meta?.label || platform}
                </span>
                <div className="flex-1 h-2 bg-dark-600 rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full transition-all"
                    style={{
                      width: `${(share / maxShare) * 100}%`,
                      background: meta?.color || '#a855f7',
                    }}
                  />
                </div>
                <span className="w-16 text-right text-xs text-white tabular-nums shrink-0">
                  {fmt(bp.views)}
                </span>
                <span className="w-12 text-right text-xs text-gray-500 tabular-nums shrink-0">
                  {share}%
                </span>
              </div>
            )
          })}
        </div>
        {totals.views > 0 && (
          <p className="text-[10px] text-gray-600 mt-2">
            {fmt(totals.views)} visitas sociales totales (no incluidas en las views de YouTube).
          </p>
        )}
      </div>

      {/* ── Por canal × red ── */}
      {channels.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 border-b border-surface-border/50">
                <th className="py-2 pr-3">Canal</th>
                <th className="py-2 pr-3 text-right">Red</th>
                <th className="py-2 pr-3 text-right">Publicados</th>
                <th className="py-2 pr-3 text-right">Vistas</th>
                <th className="py-2 pr-3 text-right">Likes</th>
                <th className="py-2 pr-3 text-right">Coment.</th>
                <th className="py-2 pr-3 text-right">Reposts</th>
              </tr>
            </thead>
            <tbody>
              {channels.map(ch => {
                const chData = perChannel[ch.id]
                if (!chData) return null
                const rows = Object.keys(chData).filter(k => k !== 'totals')
                if (rows.length === 0) return null
                return (
                  <Fragment key={ch.id}>
                    {rows.map(platform => {
                      const meta = platformMeta(platform)
                      const e = chData[platform] || {}
                      return (
                        <tr key={platform} className="border-b border-surface-border/30">
                          <td className="py-2 pr-3 text-white">{ch.name}</td>
                          <td className="py-2 pr-3 text-right text-gray-300">
                            {meta?.icon} {meta?.label || platform}
                          </td>
                          <td className="py-2 pr-3 text-right text-gray-400">{e.published}</td>
                          <td className="py-2 pr-3 text-right text-white">{fmt(e.views)}</td>
                          <td className="py-2 pr-3 text-right text-white">{fmt(e.likes)}</td>
                          <td className="py-2 pr-3 text-right text-white">{fmt(e.comments)}</td>
                          <td className="py-2 pr-3 text-right text-white">{fmt(e.reposts)}</td>
                        </tr>
                      )
                    })}
                    <tr className="border-b border-surface-border/50 bg-dark-800/40">
                      <td className="py-1.5 pr-3 text-xs font-semibold text-gray-400">
                        Total {ch.name}
                      </td>
                      <td />
                      <td className="py-1.5 pr-3 text-right text-gray-400">
                        {chData.totals?.published ?? 0}
                      </td>
                      <td className="py-1.5 pr-3 text-right text-gray-200">{fmt(chData.totals?.views)}</td>
                      <td className="py-1.5 pr-3 text-right text-gray-200">{fmt(chData.totals?.likes)}</td>
                      <td className="py-1.5 pr-3 text-right text-gray-200">{fmt(chData.totals?.comments)}</td>
                      <td className="py-1.5 pr-3 text-right text-gray-200">{fmt(chData.totals?.reposts)}</td>
                    </tr>
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
