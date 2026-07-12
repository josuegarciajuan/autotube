import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../lib/api'
import { Users, Eye, Heart, Video, Wrench, Loader2, RefreshCw, X, CheckCircle2, AlertCircle, SkipForward } from 'lucide-react'
import KpiCard from '../components/KpiCard'
import ChannelTable from '../components/ChannelTable'
import PipelineSection from '../components/PipelineSection'
import TopVideos from '../components/TopVideos'
import RecentVideos from '../components/RecentVideos'
import RecentShorts from '../components/RecentShorts'
import YppProgressSection from '../components/YppProgressSection'
import RevenueOverview from '../components/RevenueOverview'
import MilestonesTimeline from '../components/MilestonesTimeline'
import PendingManualActions from '../components/PendingManualActions'
import UpcomingPublications from '../components/UpcomingPublications'
import type { StabilizeResult } from '../components/StabilizeProgress'

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  // Stabilization state
  const [stabilizing, setStabilizing] = useState(false)
  const [stabilizeResult, setStabilizeResult] = useState<StabilizeResult | null>(null)
  const [stabilizeError, setStabilizeError] = useState<string | null>(null)

  // Stats collection state
  const [collectingStats, setCollectingStats] = useState(false)
  const [collectStatsMsg, setCollectStatsMsg] = useState<string | null>(null)
  const [collectStatsError, setCollectStatsError] = useState(false)
  const [collectStatsState, setCollectStatsState] = useState<any>(null)
  const [collectStatsFinishedAt, setCollectStatsFinishedAt] = useState<string | null>(null)

  // Refrescar datos del Dashboard
  const loadDashboard = useCallback(async () => {
    try {
      const d = await api.getDashboard()
      setData(d)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [])

  // Load last/current stats-collection state on mount (survives reloads)
  useEffect(() => {
    let cancelled = false
    async function loadStatus() {
      try {
        const s = await api.getStatsCollectStatus()
        if (cancelled) return
        applyStatsStatus(s)
        if (s.status === 'running') {
          setCollectingStats(true)
          pollStatsStatus()
        }
      } catch {
        /* ignore */
      }
    }
    loadStatus()
    return () => { cancelled = true }
  }, [])

  function summarize(s: any): string {
    const chans = s.channels || []
    const ok = chans.filter((c: any) => c.ok)
    const failed = chans.filter((c: any) => !c.ok && !c.skipped)
    const totalVideos = ok.reduce((n: number, c: any) => n + (c.videos_updated || 0), 0)
    const totalShorts = ok.reduce((n: number, c: any) => n + (c.shorts_updated || 0), 0)
    let msg = `${ok.length} canal(es) OK · ${totalVideos} videos · ${totalShorts} shorts`
    if (failed.length) msg += ` · ${failed.length} con error`
    return msg
  }

  function applyStatsStatus(s: any) {
    setCollectStatsState(s)
    if (s.status === 'success') {
      setCollectStatsError(false)
      setCollectStatsMsg(`Recoleccion completada: ${summarize(s)}`)
      setCollectStatsFinishedAt(s.finished_at ? new Date(s.finished_at * 1000).toLocaleTimeString() : null)
    } else if (s.status === 'error') {
      setCollectStatsError(true)
      setCollectStatsMsg(`Error: ${s.error || 'fallo en la recoleccion'}`)
    } else if (s.status === 'running') {
      setCollectStatsError(false)
      setCollectStatsMsg('Recolectando stats de YouTube...')
    }
  }

  async function pollStatsStatus() {
    const poll = async () => {
      try {
        const s = await api.getStatsCollectStatus()
        applyStatsStatus(s)
        if (s.status === 'running') {
          setTimeout(poll, 2000)
        } else {
          setCollectingStats(false)
          // Refresh dashboard data immediately after stats collection completes
          loadDashboard()
        }
      } catch {
        setCollectingStats(false)
      }
    }
    setTimeout(poll, 2000)
  }

  useEffect(() => {
    loadDashboard()
    const interval = setInterval(loadDashboard, 15000)
    return () => clearInterval(interval)
  }, [loadDashboard])

  async function handleStabilize() {
    if (stabilizing) return
    setStabilizing(true)
    setStabilizeError(null)
    setStabilizeResult(null)
    try {
      const res = await api.stabilizeSystem()
      setStabilizeResult(res as StabilizeResult)
    } catch (e: any) {
      setStabilizeError(e.message || 'Error desconocido al estabilizar')
    } finally {
      setStabilizing(false)
    }
  }

  async function handleCollectStats() {
    if (collectingStats) return
    setCollectingStats(true)
    setCollectStatsError(false)
    setCollectStatsMsg('Recolectando stats de YouTube...')
    try {
      await api.collectStats()
      pollStatsStatus()
    } catch (e: any) {
      setCollectStatsError(true)
      setCollectStatsMsg(`Error: ${e.message || 'desconocido'}`)
      setCollectingStats(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  const kpis = data?.global_kpis
  const channels = data?.channels || []
  const pipeline = data?.pipeline || []
  const topVideos = data?.top_videos || []
  const recentVideos = data?.recent_videos || []
  const recentShorts = data?.recent_shorts || []

  return (
    <div className="max-w-7xl mx-auto space-y-4 sm:space-y-6 animate-fade-in">
      {/* Toolbar: Refresh Stats + Stabilize */}
      <div className="flex items-center justify-end gap-2">
        <button
          onClick={handleCollectStats}
          disabled={collectingStats}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-cyan-500/20 bg-cyan-500/5 text-cyan-400 hover:bg-cyan-500/10 hover:border-cyan-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title="Recolectar estadisticas de YouTube bajo demanda"
        >
          {collectingStats ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <RefreshCw size={13} />
          )}
          <span>{collectingStats ? 'Recolectando...' : 'Recolectar stats'}</span>
        </button>
        <button
          onClick={handleStabilize}
          disabled={stabilizing}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-amber-500/20 bg-amber-500/5 text-amber-400 hover:bg-amber-500/10 hover:border-amber-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title="Estabilizar: reinicia API, mata procesos zombie, libera espacio en disco"
        >
          {stabilizing ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Wrench size={13} />
          )}
          <span>{stabilizing ? 'Estabilizando...' : 'Estabilizar Herramienta'}</span>
        </button>
      </div>

      {/* Stats collection feedback */}
      {collectStatsMsg && (
        <div className={`rounded-lg border animate-fade-in ${
          collectStatsError
            ? 'bg-red-500/5 border-red-500/20'
            : collectingStats
            ? 'bg-cyan-500/5 border-cyan-500/20'
            : 'bg-green-500/5 border-green-500/20'
        }`}>
          <div className={`flex items-center justify-between gap-2 text-xs py-2 px-3 ${
            collectStatsError ? 'text-red-400' : collectingStats ? 'text-cyan-400' : 'text-green-400'
          }`}>
            <span className="flex items-center gap-1.5 font-medium">
              {collectingStats ? <Loader2 size={12} className="animate-spin" /> : collectStatsError ? <AlertCircle size={14} /> : <CheckCircle2 size={14} />}
              {collectStatsMsg}
              {collectStatsFinishedAt && <span className="text-[10px] opacity-60 ml-1">({collectStatsFinishedAt})</span>}
            </span>
            <button
              onClick={() => { setCollectStatsMsg(null); setCollectStatsState(null) }}
              className="opacity-60 hover:opacity-100 transition-opacity"
              title="Cerrar"
            >
              <X size={13} />
            </button>
          </div>
          {/* Per-channel breakdown on success */}
          {!collectingStats && !collectStatsError && collectStatsState?.channels?.length > 0 && (
            <div className="px-3 pb-2 pt-0">
              <div className="flex flex-wrap gap-1.5">
                {collectStatsState.channels.map((ch: any, i: number) => (
                  <div key={i} className={`flex items-center gap-1 px-2 py-1 rounded text-[11px] ${
                    ch.ok
                      ? 'bg-green-500/10 text-green-300 border border-green-500/15'
                      : ch.skipped
                      ? 'bg-gray-500/10 text-gray-400 border border-gray-500/15'
                      : 'bg-red-500/10 text-red-300 border border-red-500/15'
                  }`}>
                    {ch.ok ? <CheckCircle2 size={10} /> : ch.skipped ? <SkipForward size={10} /> : <AlertCircle size={10} />}
                    <span className="font-medium">{ch.slug}</span>
                    {ch.ok ? (
                      <span className="opacity-70">
                        {ch.channel_updated ? 'C+' : ''}{ch.videos_updated > 0 ? ` V${ch.videos_updated}` : ''}{ch.shorts_updated > 0 ? ` S${ch.shorts_updated}` : ''}
                        {(ch.channel_updated || ch.videos_updated > 0 || ch.shorts_updated > 0) ? '' : ' sin cambios'}
                      </span>
                    ) : ch.skipped ? (
                      <span className="opacity-60">{ch.reason || 'sin token'}</span>
                    ) : (
                      <span className="opacity-70">{ch.error || 'error'}</span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* KPIs Globales */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 sm:gap-4">
        <KpiCard
          label="Suscriptores"
          value={kpis?.subscribers?.value ?? 0}
          delta={kpis?.subscribers?.delta}
          icon={Users}
          color="text-neon-cyan"
          sparkline={kpis?.sparkline_subscribers}
        />
        <KpiCard
          label="Vistas totales"
          value={kpis?.total_views?.value ?? 0}
          delta={kpis?.total_views?.delta}
          icon={Eye}
          color="text-neon-red"
          sparkline={kpis?.sparkline_views}
          breakdown={kpis?.total_views?.breakdown}
        />
        <KpiCard
          label="Interacciones"
          value={kpis?.engagement?.value ?? 0}
          delta={kpis?.engagement?.delta}
          icon={Heart}
          color="text-neon-gold"
          sparkline={kpis?.sparkline_engagement}
          breakdown={kpis?.engagement?.breakdown}
        />
        <KpiCard
          label="En producción"
          value={kpis?.in_production?.value ?? 0}
          delta={null}
          icon={Video}
          color="text-neon-purple"
          format="pipeline"
          generating={kpis?.in_production?.generating}
          ready={kpis?.in_production?.ready}
        />
      </div>

      {/* YPP Progress — Camino a Monetización */}
      <YppProgressSection channels={channels} />

      {/* Revenue Overview */}
      <RevenueOverview revenue={data?.revenue_overview} />

      {/* Próximos Hitos */}
      <MilestonesTimeline milestones={data?.upcoming_milestones || []} />

      {/* Tabla comparativa de canales */}
      <ChannelTable channels={channels} />

      {/* Pipeline activo */}
      <PipelineSection pipeline={pipeline} />

      {/* ── Scheduled publishing: pending manual actions ── */}
      <PendingManualActions />

      {/* ── Scheduled publishing: upcoming publications ── */}
      <UpcomingPublications />

      {/* Top videos */}
      <TopVideos videos={topVideos} />

      {/* Recently published: videos + shorts side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
        <RecentVideos videos={recentVideos} />
        <RecentShorts shorts={recentShorts} />
      </div>

      {/* Stabilization result - embedded inline notification */}
      {stabilizeResult && (
        <div className="glass p-4 rounded-xl border border-green-500/20">
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-green-500/20 flex items-center justify-center shrink-0 mt-0.5">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="3">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-green-400">Estabilización completada</h3>
              <p className="text-xs text-gray-300 mt-1">{stabilizeResult.message}</p>
              <div className="mt-2 space-y-0.5">
                {stabilizeResult.steps.map((step: string, i: number) => (
                  <div key={i} className="flex items-center gap-1.5 text-[11px] text-gray-400">
                    <span className="w-1 h-1 rounded-full bg-green-400 shrink-0" />
                    {step}
                  </div>
                ))}
              </div>
              <p className="text-[10px] text-neon-gold mt-2 animate-pulse">
                La API se está reiniciando automáticamente...
              </p>
            </div>
          </div>
        </div>
      )}

      {stabilizeError && (
        <div className="glass p-4 rounded-xl border border-red-500/20">
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-red-500/20 flex items-center justify-center shrink-0 mt-0.5">
              <span className="text-red-400 text-sm font-bold">!</span>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-red-400">Error al estabilizar</h3>
              <p className="text-xs text-gray-400 mt-1">{stabilizeError}</p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
