import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { Users, Eye, Heart, Video, Wrench, Loader2 } from 'lucide-react'
import KpiCard from '../components/KpiCard'
import ChannelTable from '../components/ChannelTable'
import PipelineSection from '../components/PipelineSection'
import TopVideos from '../components/TopVideos'
import YppProgressSection from '../components/YppProgressSection'
import RevenueOverview from '../components/RevenueOverview'
import MilestonesTimeline from '../components/MilestonesTimeline'
import type { StabilizeResult } from '../components/StabilizeProgress'

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  // Stabilization state
  const [stabilizing, setStabilizing] = useState(false)
  const [stabilizeResult, setStabilizeResult] = useState<StabilizeResult | null>(null)
  const [stabilizeError, setStabilizeError] = useState<string | null>(null)

  useEffect(() => {
    async function load() {
      try {
        const d = await api.getDashboard()
        setData(d)
      } catch (e) {
        console.error(e)
      } finally {
        setLoading(false)
      }
    }
    load()
    const interval = setInterval(load, 15000)
    return () => clearInterval(interval)
  }, [])

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

  return (
    <div className="max-w-7xl mx-auto space-y-4 sm:space-y-6 animate-fade-in">
      {/* Toolbar: Stabilize button + KPIs */}
      <div className="flex items-center justify-end">
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

      {/* Top videos */}
      <TopVideos videos={topVideos} />

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
