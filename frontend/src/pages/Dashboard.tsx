import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { Users, Eye, Heart, Clock, Cog, Wrench, Loader2, RefreshCw, X, CheckCircle2, AlertCircle, SkipForward } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { useChannelFilter } from '../context/ChannelFilterContext'
import { useEasterEgg } from '../context/EasterEggContext'
import ChannelFilter from '../components/ChannelFilter'
import VitalSignsBar from '../components/VitalSignsBar'
import ActivityHeatmap from '../components/ActivityHeatmap'
import PipelineSection from '../components/PipelineSection'
import TopVideos from '../components/TopVideos'
import RecentVideos from '../components/RecentVideos'
import RecentShorts from '../components/RecentShorts'
import YppProgressSection from '../components/YppProgressSection'
import RevenueOverview from '../components/RevenueOverview'
import BossFight from '../components/BossFight'
import Console from '../components/Console'
import DeepDivePanel from '../components/DeepDivePanel'
import PendingManualActions from '../components/PendingManualActions'
import UpcomingPublications from '../components/UpcomingPublications'
import type { StabilizeResult } from '../components/StabilizeProgress'

// Collapsible section wrapper
function CollapsibleSection({ title, icon, defaultOpen, children }: {
  title: string; icon?: string; defaultOpen?: boolean; children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen ?? true)
  return (
    <div className="mb-6">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between p-3 rounded-xl border border-dark-500 bg-dark-800/60 hover:border-gray-400 transition-all"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-gray-300">
          {icon && <span className="text-lg">{icon}</span>}
          {title}
        </span>
        <motion.span
          animate={{ rotate: open ? 180 : 0 }}
          transition={{ duration: 0.3 }}
          className="text-gray-500 text-xs"
        >
          ▼
        </motion.span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.4, ease: 'easeInOut' }}
            className="overflow-hidden"
          >
            <div className="pt-3">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [deepDiveChannel, setDeepDiveChannel] = useState<any>(null)
  const { selectedChannelId } = useChannelFilter()
  const { partyMode, matrixMode, glitchTick } = useEasterEgg()

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

  // Console events
  const [consoleEvents, setConsoleEvents] = useState<any[]>([])

  // Refrescar dashboard
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

  useEffect(() => {
    loadDashboard()
    const interval = setInterval(loadDashboard, 15000)
    return () => clearInterval(interval)
  }, [loadDashboard])

  // Load console events
  useEffect(() => {
    api.getRecentEvents(20, selectedChannelId ?? undefined)
      .then(setConsoleEvents)
      .catch(() => {})
  }, [selectedChannelId, glitchTick])

  function summarize(s: any): string {
    const chans = s.channels || []
    const ok = chans.filter((c: any) => c.ok)
    const failed = chans.filter((c: any) => !c.ok && !c.skipped)
    const totalVideos = ok.reduce((n: number, c: any) => n + (c.videos_updated || 0), 0)
    const totalShorts = ok.reduce((n: number, c: any) => n + (c.shorts_updated || 0), 0)
    const totalAnalytics = ok.reduce((n: number, c: any) => n + (c.analytics_updated || 0), 0)
    let msg = `${ok.length} canal(es) OK · ${totalVideos} videos · ${totalShorts} shorts`
    if (totalAnalytics > 0) msg += ` · ${totalAnalytics} analytics`
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
          loadDashboard()
        }
      } catch {
        setCollectingStats(false)
      }
    }
    setTimeout(poll, 2000)
  }

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
      } catch { /* ignore */ }
    }
    loadStatus()
    return () => { cancelled = true }
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
  const heatmapData = data?.heatmap_data || []
  const channelNames: Record<string, string> = {}
  const channelSlugs: Record<string, string> = {}
  const channelColors: Record<string, string> = {}
  const pipelineCounts: Record<number, number> = {}

  channels.forEach((ch: any, i: number) => {
    channelNames[ch.id] = ch.name
    channelSlugs[ch.id] = ch.slug
    const cols = ['#ff3355', '#a855f7', '#00e5ff', '#22c55e', '#ffb830', '#ec4899']
    channelColors[ch.id] = cols[i % cols.length]
  })

  pipeline.forEach((p: any) => {
    // Find channel_id from pipeline data - we need to look it up from channel slug
    const ch = channels.find((c: any) => c.slug === p.channel_slug || c.id === p.channel_id)
    if (ch) {
      pipelineCounts[ch.id] = (pipelineCounts[ch.id] || 0) + 1
    }
  })

  // Build KPI list for VitalSignsBar
  const kpiList = [
    {
      key: 'sparkline_subscribers',
      label: 'Subs',
      value: kpis?.subscribers?.value ?? 0,
      delta: kpis?.subscribers?.delta,
      icon: Users,
      color: '#00e5ff',
    },
    {
      key: 'sparkline_views',
      label: 'Views',
      value: kpis?.total_views?.value ?? 0,
      delta: kpis?.total_views?.delta,
      icon: Eye,
      color: '#ff3355',
      breakdown: kpis?.total_views?.breakdown,
    },
    {
      key: 'sparkline_engagement',
      label: 'Engage',
      value: kpis?.engagement?.value ?? 0,
      delta: kpis?.engagement?.delta,
      icon: Heart,
      color: '#ffb830',
      breakdown: kpis?.engagement?.breakdown,
    },
    {
      key: 'sparkline_watch_hours',
      label: 'Horas',
      value: kpis?.watch_hours?.value ?? 0,
      delta: kpis?.watch_hours?.delta,
      icon: Clock,
      color: '#22c55e',
    },
    {
      key: 'in_production',
      label: 'Pipeline',
      value: kpis?.in_production?.value ?? 0,
      delta: null,
      icon: Cog,
      color: '#a855f7',
    },
  ]

  // Sparkline mapping
  const sparklines: Record<string, number[]> = {
    sparkline_subscribers: kpis?.sparkline_subscribers || [],
    sparkline_views: kpis?.sparkline_views || [],
    sparkline_engagement: kpis?.sparkline_engagement || [],
    sparkline_watch_hours: kpis?.sparkline_watch_hours || [],
    in_production: Array(8).fill(kpis?.in_production?.value || 0),
  }

  // Channel breakdown for expanded KPI view (views per channel)
  const viewsBreakdown = channels.map((ch: any) => ({
    name: ch.name,
    slug: ch.slug,
    value: (ch.longform_views || 0) + (ch.shorts_views || 0),
  }))

  return (
    <div className="max-w-7xl mx-auto space-y-4 sm:space-y-6"
      style={partyMode ? { animation: 'party-bg 2s linear infinite' } : {}}
    >
      {/* Party mode overlay */}
      {partyMode && (
        <div className="fixed inset-0 pointer-events-none z-40"
          style={{
            background: 'linear-gradient(90deg, rgba(255,51,85,0.05), rgba(168,85,247,0.05), rgba(0,229,255,0.05), rgba(255,51,85,0.05))',
            backgroundSize: '400% 100%',
            animation: 'party-bg 2s linear infinite',
          }}
        />
      )}

      {/* Matrix mode global effect */}
      {matrixMode && (
        <div className="fixed inset-0 pointer-events-none z-39"
          style={{ background: 'rgba(0,15,0,0.3)', backdropFilter: 'hue-rotate(180deg)' }}
        />
      )}

      {/* Toolbar: Refresh Stats + Stabilize */}
      <div className="flex items-center justify-end gap-2">
        <button
          onClick={handleCollectStats}
          disabled={collectingStats}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-cyan-500/20 bg-cyan-500/5 text-cyan-400 hover:bg-cyan-500/10 hover:border-cyan-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title="Recolectar estadisticas de YouTube bajo demanda"
        >
          {collectingStats ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          <span>{collectingStats ? 'Recolectando...' : 'Recolectar stats'}</span>
        </button>
        <button
          onClick={handleStabilize}
          disabled={stabilizing}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-amber-500/20 bg-amber-500/5 text-amber-400 hover:bg-amber-500/10 hover:border-amber-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {stabilizing ? <Loader2 size={13} className="animate-spin" /> : <Wrench size={13} />}
          <span>{stabilizing ? 'Estabilizando...' : 'Estabilizar'}</span>
        </button>
      </div>

      {/* Stats collection feedback */}
      {collectStatsMsg && (
        <div className={`rounded-lg border animate-fade-in ${
          collectStatsError ? 'bg-red-500/5 border-red-500/20'
            : collectingStats ? 'bg-cyan-500/5 border-cyan-500/20'
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
            <button onClick={() => { setCollectStatsMsg(null); setCollectStatsState(null) }} className="opacity-60 hover:opacity-100 transition-opacity">
              <X size={13} />
            </button>
          </div>
        </div>
      )}

      {/* ═══════ NO SE TOCA: Stabilize result ═══════ */}
      {stabilizeResult && (
        <div className="glass p-4 rounded-xl border border-green-500/20">
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-green-500/20 flex items-center justify-center shrink-0 mt-0.5">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="3">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-green-400">Estabilizacion completada</h3>
              <p className="text-xs text-gray-300 mt-1">{stabilizeResult.message}</p>
              <div className="mt-2 space-y-0.5">
                {stabilizeResult.steps.map((step: string, i: number) => (
                  <div key={i} className="flex items-center gap-1.5 text-[11px] text-gray-400">
                    <span className="w-1 h-1 rounded-full bg-green-400 shrink-0" />
                    {step}
                  </div>
                ))}
              </div>
              <p className="text-[10px] text-neon-gold mt-2 animate-pulse">La API se esta reiniciando automaticamente...</p>
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

      {/* ═══════ NIVEL 1: Channel Filter Pills ═══════ */}
      <ChannelFilter channels={channels} pipelineCounts={pipelineCounts} />

      {/* ═══════ NIVEL 1: Vital Signs Bar (KPIs gamificados) ═══════ */}
      <VitalSignsBar
        kpis={kpiList}
        sparklines={sparklines}
        channelBreakdown={viewsBreakdown}
      />

      {/* ═══════ NIVEL 1: Activity Heatmap ═══════ */}
      <ActivityHeatmap
        data={heatmapData}
        channelSlugs={channelSlugs}
        channelNames={channelNames}
        channelColors={channelColors}
      />

      {/* ═══════ NIVEL 2: Pipeline Activo ═══════ */}
      {pipeline.length > 0 && (
        <CollapsibleSection title="Pipeline Activo" icon="⚙️">
          <PipelineSection pipeline={pipeline} />
        </CollapsibleSection>
      )}

      {/* ═══════ NIVEL 2: YPP + Revenue ═══════ */}
      <CollapsibleSection title="Monetizacion" icon="💰" defaultOpen={false}>
        <YppProgressSection channels={channels} />
        <div className="mt-3">
          <RevenueOverview revenue={data?.revenue_overview} />
        </div>
      </CollapsibleSection>

      {/* ═══════ NIVEL 2: Boss Fights (Milestones gamificados) ═══════ */}
      {(data?.upcoming_milestones || []).length > 0 && (
        <CollapsibleSection title="Jefes a Derrotar" icon="⚔️" defaultOpen={false}>
          {channels.slice(0, 3).map((ch: any) => (
            <BossFight key={ch.id} channelId={ch.id} channelName={ch.name} />
          ))}
        </CollapsibleSection>
      )}

      {/* ═══════ NIVEL 2: Scheduled Publishing ═══════ */}
      <CollapsibleSection title="Publicaciones Programadas" icon="📅" defaultOpen={false}>
        <PendingManualActions />
        <div className="mt-3">
          <UpcomingPublications />
        </div>
      </CollapsibleSection>

      {/* ═══════ NIVEL 2: Top Videos ═══════ */}
      {topVideos.length > 0 && (
        <CollapsibleSection title="Top Videos" icon="🏆">
          <TopVideos videos={topVideos} />
        </CollapsibleSection>
      )}

      {/* ═══════ NIVEL 2: Recents ═══════ */}
      <CollapsibleSection title="Publicado Recientemente" icon="🕐" defaultOpen={false}>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
          <RecentVideos videos={recentVideos} />
          <RecentShorts shorts={recentShorts} />
        </div>
      </CollapsibleSection>

      {/* ═══════ NIVEL 3: Deep Dive Panel (slide on channel click) ═══════ */}
      {selectedChannelId && (
        <div className="text-center">
          <button
            onClick={() => {
              const ch = channels.find((c: any) => c.id === selectedChannelId)
              if (ch) setDeepDiveChannel(ch)
            }}
            className="px-4 py-2 rounded-xl bg-neon-red/10 border border-neon-red/30 text-neon-red text-sm font-medium hover:bg-neon-red/20 transition-all"
          >
            🔬 Abrir Deep Dive: {channels.find((c: any) => c.id === selectedChannelId)?.name || 'Canal'}
          </button>
        </div>
      )}

      <DeepDivePanel
        channel={deepDiveChannel}
        open={!!deepDiveChannel}
        onClose={() => setDeepDiveChannel(null)}
        comparisonChannels={channels}
      />

      {/* ═══════ Console (fixed bottom-right) ═══════ */}
      <Console events={consoleEvents} />

      {/* Party/matrix background animations */}
      <style>{`
        @keyframes party-bg {
          0% { background-position: 0% 50%; }
          100% { background-position: 400% 50%; }
        }
      `}</style>
    </div>
  )
}
