import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Play, Pause, ListPlus, Database } from 'lucide-react'
import { api } from '../lib/api'
import { SOCIAL_PLATFORMS } from '../types/channel'

interface PlatformStatus {
  platform: string
  type: 'espejo' | 'embudo'
  has_account: boolean
  pending_count: number
  daily_cap: number
  warmup_until: string | null
  backoff_until: string | null
  last_publish_at: string | null
}

interface Status {
  enabled: boolean
  paused: boolean
  espejo: string[]
  embudo: string[]
  warmup_days: number
  warmup_daily_cap: number
  backlog_direction: string
  total_pending: number
  platforms: PlatformStatus[]
}

interface PlatformStat {
  platform: string
  total_published: number
  total_views: number
  total_likes: number
  total_comments: number
  total_reposts: number
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

export default function Distribution() {
  const [channels, setChannels] = useState<any[]>([])
  const [channelId, setChannelId] = useState<number | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [stats, setStats] = useState<PlatformStat[]>([])
  const [backlog, setBacklog] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    api.getChannels().then(data => {
      setChannels(Array.isArray(data) ? data : [])
      if (Array.isArray(data) && data.length > 0) setChannelId(data[0].id)
    }).catch(e => setResult({ ok: false, message: e.message }))
  }, [])

  useEffect(() => {
    if (!channelId) return
    setLoading(true)
    Promise.all([
      api.getRedistributionStatus(channelId),
      api.getChannelSocialStats(channelId),
    ]).then(([st, sts]) => {
      setStatus(st)
      setStats(sts?.per_platform || [])
    }).catch(e => setResult({ ok: false, message: e.message }))
      .finally(() => setLoading(false))
  }, [channelId])

  useEffect(() => {
    if (!channelId) return
    api.getRedistributionBacklog(channelId).then(setBacklog).catch(() => {})
  }, [channelId])

  async function run(fn: () => Promise<any>, okMsg: string) {
    setBusy(true)
    setResult(null)
    try {
      const res = await fn()
      setResult({ ok: true, message: `${okMsg} (${res?.enqueued ?? ''})` })
      // Reload
      const [st, sts] = await Promise.all([
        api.getRedistributionStatus(channelId!),
        api.getChannelSocialStats(channelId!),
      ])
      setStatus(st)
      setStats(sts?.per_platform || [])
      const bl = await api.getRedistributionBacklog(channelId!)
      setBacklog(bl)
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
    setBusy(false)
  }

  const chartData = useMemo(() => {
    return stats.map(s => ({
      name: platformMeta(s.platform)?.label || s.platform,
      vistas: s.total_views,
      likes: s.total_likes,
    }))
  }, [stats])

  if (loading && !status) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-cyan border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="max-w-7xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-lg font-bold text-white flex items-center gap-2">
          <Database size={18} className="text-neon-cyan" />
          Distribución Social
        </h2>
        <div className="flex items-center gap-2">
          <select
            value={channelId ?? ''}
            onChange={e => setChannelId(Number(e.target.value))}
            className="bg-dark-800 border border-surface-border text-white text-xs rounded-lg px-3 py-2"
          >
            {channels.map(ch => (
              <option key={ch.id} value={ch.id}>{ch.name || ch.slug}</option>
            ))}
          </select>
          <button
            onClick={() => channelId && run(() => api.collectSocialStats(channelId), 'Stats recolectados')}
            disabled={busy}
            className="flex items-center gap-1 px-3 py-2 rounded-lg bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan text-xs hover:bg-neon-cyan/20 disabled:opacity-50"
          >
            <RefreshCw size={12} className={busy ? 'animate-spin' : ''} />
            Recolectar stats
          </button>
        </div>
      </div>

      {result && (
        <div className={`px-3 py-2 rounded-lg text-xs ${result.ok ? 'bg-green-900/40 text-green-300 border border-green-700/40' : 'bg-red-900/40 text-red-300 border border-red-700/40'}`}>
          {result.message}
        </div>
      )}

      {/* Controls */}
      {status && (
        <div className="glass rounded-xl p-4 border border-surface-border flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-400 mr-1">
            Backfill: <b className="text-white">{status.enabled ? (status.paused ? 'PAUSADO' : 'activo') : 'no configurado'}</b>
            {' · '}Cola: <b className="text-neon-cyan">{status.total_pending}</b> pendientes
            {' · '}Ritmo: {status.warmup_daily_cap}/día (warmup {status.warmup_days}d) → régimen
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button onClick={() => channelId && run(() => api.startRedistribution(channelId), 'Backfill iniciado')}
              disabled={busy} className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-neon-gold/10 border border-neon-gold/30 text-neon-gold text-xs hover:bg-neon-gold/20 disabled:opacity-50">
              <Play size={12} /> Iniciar backfill
            </button>
            <button onClick={() => channelId && run(() => api.enqueueRedistribution(channelId), 'Catálogo encolado')}
              disabled={busy} className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-dark-600 text-gray-300 text-xs hover:bg-dark-500 disabled:opacity-50">
              <ListPlus size={12} /> Re-encolar catálogo
            </button>
            {status.paused ? (
              <button onClick={() => channelId && run(() => api.resumeRedistribution(channelId), 'Reanudado')}
                disabled={busy} className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-900/30 text-green-400 text-xs hover:bg-green-900/50 disabled:opacity-50">
                <Play size={12} /> Reanudar
              </button>
            ) : (
              <button onClick={() => channelId && run(() => api.pauseRedistribution(channelId), 'Pausado')}
                disabled={busy} className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-red-900/30 text-red-400 text-xs hover:bg-red-900/50 disabled:opacity-50">
                <Pause size={12} /> Pausar
              </button>
            )}
          </div>
        </div>
      )}

      {/* Per-platform status */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
        {(status?.platforms || []).map(p => {
          const meta = platformMeta(p.platform)
          return (
            <div key={p.platform} className="glass rounded-xl p-4 border border-surface-border">
              <div className="flex items-center justify-between">
                <span className="text-lg">{meta?.icon || '🌐'}</span>
                <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${p.type === 'espejo' ? 'bg-neon-cyan/10 text-neon-cyan' : 'bg-purple-500/10 text-purple-400'}`}>
                  {p.type === 'espejo' ? 'ESPEJO' : 'EMBUDO'}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="text-sm font-semibold text-white">{meta?.label || p.platform}</span>
                <span className={`w-1.5 h-1.5 rounded-full ${p.has_account ? 'bg-green-400' : 'bg-gray-700'}`}
                      title={p.has_account ? 'Cuenta conectada' : 'Sin cuenta'} />
              </div>
              <div className="mt-2 space-y-1 text-xs text-gray-400">
                <div className="flex justify-between"><span>En cola</span><b className="text-white">{p.pending_count}</b></div>
                <div className="flex justify-between"><span>Cap diario</span><b className="text-white">{p.daily_cap}</b></div>
                <div className="flex justify-between"><span>Última subida</span>
                  <b className="text-white">{p.last_publish_at ? new Date(p.last_publish_at).toLocaleString() : '—'}</b></div>
                {p.backoff_until && (
                  <div className="text-red-400">⏳ Backoff hasta {new Date(p.backoff_until).toLocaleString()}</div>
                )}
                {!p.has_account && <div className="text-amber-400/80">⚠️ Conecta la cuenta en Canales → Redes Sociales</div>}
              </div>
            </div>
          )
        })}
        {(status?.platforms || []).length === 0 && (
          <div className="col-span-full text-center text-xs text-gray-500 py-8">
            No hay plataformas habilitadas para este canal. Activa la redistribución en la config del canal.
          </div>
        )}
      </div>

      {/* Stats per platform */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">📊 Rendimiento por red (público)</h3>
        {stats.length === 0 ? (
          <p className="text-xs text-gray-500">Aún sin datos. Pulsa "Recolectar stats" cuando haya vídeos publicados.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-surface-border">
                  <th className="py-2 pr-3">Red</th>
                  <th className="py-2 pr-3">Publicados</th>
                  <th className="py-2 pr-3">Vistas</th>
                  <th className="py-2 pr-3">Likes</th>
                  <th className="py-2 pr-3">Comentarios</th>
                  <th className="py-2">Reposts</th>
                </tr>
              </thead>
              <tbody>
                {stats.map(s => (
                  <tr key={s.platform} className="border-b border-surface-border/50 last:border-0">
                    <td className="py-2 pr-3 text-white">{platformMeta(s.platform)?.icon} {platformMeta(s.platform)?.label || s.platform}</td>
                    <td className="py-2 pr-3">{s.total_published}</td>
                    <td className="py-2 pr-3 text-neon-cyan font-semibold">{fmt(s.total_views)}</td>
                    <td className="py-2 pr-3">{fmt(s.total_likes)}</td>
                    <td className="py-2 pr-3">{fmt(s.total_comments)}</td>
                    <td className="py-2">{fmt(s.total_reposts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Backlog queue */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">🗂️ Cola de backfill (pendientes)</h3>
        {backlog.length === 0 ? (
          <p className="text-xs text-gray-500">No hay vídeos pendientes. Pulsa "Iniciar backfill" para encolar el catálogo publicado.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-surface-border">
                  <th className="py-2 pr-3">Plataforma</th>
                  <th className="py-2 pr-3">Vídeo</th>
                  <th className="py-2 pr-3">Orden</th>
                  <th className="py-2">Estado</th>
                </tr>
              </thead>
              <tbody>
                {backlog.slice(0, 20).map(row => (
                  <tr key={`${row.platform}-${row.video_id}`} className="border-b border-surface-border/50 last:border-0">
                    <td className="py-2 pr-3 text-white">{platformMeta(row.platform)?.icon} {platformMeta(row.platform)?.label || row.platform}</td>
                    <td className="py-2 pr-3 text-gray-300 max-w-[280px] truncate" title={row.video_title}>
                      {row.video_title || `#${row.video_id}`}
                    </td>
                    <td className="py-2 pr-3 text-gray-500">{row.queue_order}</td>
                    <td className="py-2 text-amber-400">pendiente</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {backlog.length > 20 && <p className="text-[10px] text-gray-600 mt-2">Mostrando 20 de {backlog.length} pendientes…</p>}
          </div>
        )}
      </div>
    </div>
  )
}
