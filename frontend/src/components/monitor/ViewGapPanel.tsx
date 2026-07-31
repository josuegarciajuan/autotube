import { useState, useEffect, useCallback } from 'react'
import { Radar, AlertTriangle, Search, ExternalLink, Download, Loader2 } from 'lucide-react'
import { api } from '../../lib/api'

interface GapChannel {
  channel_id: number
  slug: string
  name: string
  coverage_pct: number
  gap: number
  delta_24h: number
  yt_total: number
  db_total: number
  db_longform: number
  db_shorts: number
  last_checked: string
}

interface UnregisteredVideo {
  id: number
  yt_video_id: string
  titulo_final: string
  created_at: string
  thumbnail_path: string
  privacy_status: string
}

export default function ViewGapPanel() {
  const [channels, setChannels] = useState<GapChannel[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scanResult, setScanResult] = useState<string | null>(null)

  const fetchCoverage = useCallback(async () => {
    try {
      const res = await api.getViewGapCoverage()
      setChannels(res.channels || [])
      setError(null)
    } catch {
      setError('Failed to load coverage data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchCoverage() }, [fetchCoverage])

  async function handleScanAll() {
    setScanning(true)
    setScanResult(null)
    try {
      const res = await api.triggerViewGapScanAll()
      setScanResult(
        `${res.channels_checked} channels checked, ${res.gaps_detected} gaps, ${res.videos_registered} new videos registered`
      )
      fetchCoverage()
    } catch (e: any) {
      setScanResult(`Error: ${e?.message || e}`)
    } finally {
      setScanning(false)
    }
  }

  async function handleScanChannel(channelId: number) {
    setScanning(true)
    setScanResult(null)
    try {
      const res = await api.triggerViewGapScan(channelId)
      setScanResult(
        `Scanned: gap=${res.gap} delta=${res.delta} registered=${res.videos_registered}`
      )
      fetchCoverage()
    } catch (e: any) {
      setScanResult(`Error: ${e?.message || e}`)
    } finally {
      setScanning(false)
    }
  }

  function coverageColor(pct: number): string {
    if (pct >= 95) return 'text-emerald-400'
    if (pct >= 80) return 'text-amber-400'
    return 'text-red-400'
  }

  function coverageBarColor(pct: number): string {
    if (pct >= 95) return 'bg-emerald-500'
    if (pct >= 80) return 'bg-amber-500'
    return 'bg-red-500'
  }

  function formatNum(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
  }

  if (loading) {
    return (
      <div className="glass rounded-xl p-5 border border-surface-border">
        <div className="flex items-center gap-2 text-gray-500 text-sm">
          <Loader2 size={14} className="animate-spin" />
          Loading coverage data...
        </div>
      </div>
    )
  }

  return (
    <div className="glass rounded-xl p-5 border border-surface-border">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
          <Radar size={14} className="text-neon-cyan" />
          View Gap Monitor
        </h3>
        <div className="flex gap-2">
          <button
            onClick={handleScanAll}
            disabled={scanning}
            className="px-2.5 py-1 rounded text-[11px] font-medium bg-neon-cyan/10 text-neon-cyan border border-neon-cyan/20 hover:bg-neon-cyan/20 transition-colors disabled:opacity-50"
          >
            {scanning ? (
              <Loader2 size={12} className="animate-spin inline mr-1" />
            ) : (
              <Search size={12} className="inline mr-1" />
            )}
            Scan All
          </button>
        </div>
      </div>

      {/* Subtitle: qué es GAP y qué significa 0 */}
      <p className="text-[10px] text-gray-500 leading-relaxed mb-4">
        Gap ={' '}
        <span className="text-gray-400">vistas totales (YouTube)</span>
        {' − '}
        <span className="text-gray-400">vistas rastreadas (DB Autotube)</span>
        .{' '}
        <span className="text-emerald-400 font-medium">Gap = 0 → todo OK</span>
        <span className="text-gray-600"> — cada vista está contabilizada.</span>
      </p>

      {error && (
        <div className="text-red-400 text-xs mb-3 flex items-center gap-1">
          <AlertTriangle size={12} />
          {error}
        </div>
      )}

      {scanResult && (
        <div className="text-neon-cyan text-xs mb-3 bg-neon-cyan/5 rounded-lg px-3 py-2 border border-neon-cyan/10">
          {scanResult}
        </div>
      )}

      {channels.length === 0 ? (
        <div className="text-center py-6 text-gray-500 text-xs">
          No coverage data yet. The daily check runs every 24 hours.
        </div>
      ) : (
        <div className="space-y-3">
          {channels.map(ch => (
            <div
              key={ch.channel_id}
              className="border border-surface-border/30 rounded-lg p-3 hover:border-surface-border/60 transition-colors"
            >
              {/* Top row: name + coverage badge */}
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm font-medium text-gray-200">{ch.name}</span>
                <button
                  onClick={() => handleScanChannel(ch.channel_id)}
                  disabled={scanning}
                  className="text-[10px] text-gray-500 hover:text-neon-cyan transition-colors disabled:opacity-30"
                  title="Scan this channel"
                >
                  <Search size={11} className="inline" /> Scan
                </button>
              </div>

              {/* Coverage bar */}
              <div className="flex items-center gap-2 mb-2">
                <div
                  className="flex-1 h-2 bg-dark-700 rounded-full overflow-hidden cursor-help"
                  title={`Cobertura: ${ch.coverage_pct.toFixed(1)}%. ${ch.coverage_pct >= 100 ? 'GAP = 0 — todas las vistas están contabilizadas.' : `Falta rastrear ${formatNum(ch.gap)} vistas.`}`}
                >
                  <div
                    className={`h-full rounded-full transition-all duration-500 ${coverageBarColor(ch.coverage_pct)}`}
                    style={{ width: `${Math.min(100, ch.coverage_pct)}%` }}
                  />
                </div>
                <span className={`text-xs font-mono font-bold ${coverageColor(ch.coverage_pct)}`}>
                  {ch.coverage_pct.toFixed(1)}%
                </span>
              </div>

              {/* Details grid */}
              <div className="grid grid-cols-3 gap-x-3 gap-y-1 text-[10px]">
                <div>
                  <span className="text-gray-600 cursor-help" title="Total de visualizaciones del canal según YouTube (fuente oficial)">YT total</span>
                  <div className="text-gray-300 font-mono">{formatNum(ch.yt_total)}</div>
                </div>
                <div>
                  <span className="text-gray-600 cursor-help" title="Suma de visualizaciones de todos los videos registrados en la base de datos de Autotube">DB tracked</span>
                  <div className="text-gray-300 font-mono">{formatNum(ch.db_total)}</div>
                </div>
                <div>
                  <span className="text-gray-600 cursor-help" title="Diferencia entre YT total y DB tracked. GAP = 0 significa que todas las vistas están contabilizadas.">Gap</span>
                  <div className={`font-mono ${ch.gap > 0 ? 'text-amber-400' : 'text-emerald-400'}`}>
                    {formatNum(ch.gap)}
                  </div>
                </div>
                <div>
                  <span className="text-gray-600 cursor-help" title="Crecimiento del GAP en las últimas 24 horas. Si sube rápido, puede haber un video viral no registrado.">Δ24h</span>
                  <div className={`font-mono ${ch.delta_24h > 0 ? 'text-orange-400' : 'text-gray-400'}`}>
                    +{formatNum(ch.delta_24h)}
                  </div>
                </div>
                <div>
                  <span className="text-gray-600 cursor-help" title="Vistas de videos long-form (más de 60s) rastreadas en DB">Long-form</span>
                  <div className="text-gray-400 font-mono">{formatNum(ch.db_longform)}</div>
                </div>
                <div>
                  <span className="text-gray-600 cursor-help" title="Vistas de shorts rastreadas en DB">Shorts</span>
                  <div className="text-gray-400 font-mono">{formatNum(ch.db_shorts)}</div>
                </div>
              </div>

              {/* Alert: gap detected */}
              {ch.delta_24h > 0 && ch.gap > 500 && (
                <div className="mt-2 text-[10px] text-orange-400 bg-orange-500/5 rounded px-2 py-1 border border-orange-500/10 flex items-center gap-1">
                  <AlertTriangle size={10} />
                  &Delta;+{formatNum(ch.delta_24h)} untracked views — check YouTube Analytics for viral videos
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
