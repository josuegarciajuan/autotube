import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, API_TIME_ZONE } from '../lib/api'
import { useDashboard, useRecentEvents, useQuotaStatus } from '../hooks/useQueries'
import { Users, Eye, Heart, Clock, Cog, Wrench, Loader2, RefreshCw, X, CheckCircle2, AlertCircle, SkipForward, Zap, Share2 } from 'lucide-react'
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
import RecentActions from '../components/RecentActions'
import YppProgressSection from '../components/YppProgressSection'
import RevenueOverview from '../components/RevenueOverview'
import BossFight from '../components/BossFight'
import Console from '../components/Console'
import DeepDivePanel from '../components/DeepDivePanel'

import UpcomingPublications from '../components/UpcomingPublications'
import ViewGapPanel from '../components/monitor/ViewGapPanel'
import SocialOverview from '../components/SocialOverview'
import type { StabilizeResult } from '../components/StabilizeProgress'
import QuotaWidget from '../components/QuotaWidget'

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
  const { selectedChannelId } = useChannelFilter()
  const { partyMode, matrixMode, glitchTick } = useEasterEgg()
  const queryClient = useQueryClient()

  // React Query: auto-fetches and caches dashboard data
  const { data, isLoading: loading, dataUpdatedAt, refetch: refetchDashboard } =
    useDashboard(selectedChannelId ?? undefined)

  // YouTube API quota status (for disabling stats collection button)
  const { data: quotaStatus } = useQuotaStatus()
  const quotaExhausted = quotaStatus?.exhausted ?? false

  const [deepDiveChannel, setDeepDiveChannel] = useState<any>(null)

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
  // Social network stats collection (0 cuota YouTube)
  const [collectingSocialStats, setCollectingSocialStats] = useState(false)
  const [socialStatsMsg, setSocialStatsMsg] = useState<string | null>(null)
  const [socialStatsError, setSocialStatsError] = useState(false)

  // Previous KPI snapshot — captured before stats collection, cleared on manual refresh
  const [previousKpis, setPreviousKpis] = useState<Record<string, number> | null>(null)

  // Optimal slots recalculation state
  const [recalculatingSlots, setRecalculatingSlots] = useState(false)
  const [recalcSlotsResult, setRecalcSlotsResult] = useState<any>(null)
  const [recalcSlotsError, setRecalcSlotsError] = useState<string | null>(null)

  // SEO scores — now included in the dashboard response (seo_summary)
  // Eliminates N×2 per-channel HTTP requests (was: api.getChannelSEOScore + api.getChannelCTR)
  const seoScores = data?.seo_summary || {}
  const seoLoading = false  // already included in dashboard data

  // Console events (via React Query)
  const { data: consoleEvents = [] } = useRecentEvents(20, selectedChannelId ?? undefined)

  function summarize(s: any): string {
    const chans = s.channels || []
    const ok = chans.filter((c: any) => c.ok)
    const failed = chans.filter((c: any) => !c.ok && !c.skipped)
    const totalVideos = ok.reduce((n: number, c: any) => n + (c.videos_updated || 0), 0)
    const totalShorts = ok.reduce((n: number, c: any) => n + (c.shorts_updated || 0), 0)
    const totalAnalytics = ok.reduce((n: number, c: any) => n + (c.analytics_updated || 0), 0)
    const totalFallback = ok.reduce((n: number, c: any) => n + (c.analytics_fallback_videos || 0), 0)
    const totalScraped = ok.reduce((n: number, c: any) => n + (c.scrape_fallback_videos || 0) + (c.scrape_fallback_shorts || 0), 0)
    const quotaExhausted = ok.some((c: any) => c.quota_exhausted)
    const scrapeMode = s.scrape_mode || ok.some((c: any) => c.scrape_mode)
    let msg = `${ok.length} canal(es)`
    if (scrapeMode) {
      msg += ` · 🕸️ modo scraping`
    } else if (quotaExhausted) {
      msg += ` · ⚠️ Cuota Data API agotada`
    }
    if (totalVideos > 0) msg += ` · ${totalVideos} videos`
    else if (quotaExhausted && totalScraped === 0) msg += ` · 0 videos (sin quota)`
    if (totalShorts > 0) msg += ` · ${totalShorts} shorts`
    if (totalAnalytics > 0) msg += ` · ${totalAnalytics} analytics`
    if (totalFallback > 0) msg += ` · ${totalFallback} via analytics`
    if (totalScraped > 0) msg += ` · ${totalScraped} via scraping`
    if (failed.length) msg += ` · ${failed.length} con error`
    return msg
  }

  function hasQuotaWarning(s: any): boolean {
    const chans = s.channels || []
    const ok = chans.filter((c: any) => c.ok)
    // Warn only on a GENUINE quota failure: quota exhausted AND scraping did
    // not recover the data. A successful scrape-mode collection is a success.
    return ok.some((c: any) => c.quota_exhausted && (c.scrape_fallback_videos || 0) + (c.scrape_fallback_shorts || 0) === 0)
  }

  function applyStatsStatus(s: any, showBanner = true) {
    setCollectStatsState(s)
    if (s.status === 'success') {
      const quota = hasQuotaWarning(s)
      setCollectStatsError(quota)
      if (showBanner) {
        setCollectStatsMsg(`Recoleccion completada: ${summarize(s)}`)
      }
      setCollectStatsFinishedAt(s.finished_at ? new Date(s.finished_at * 1000).toLocaleTimeString('es-ES', { timeZone: API_TIME_ZONE }) : null)
    } else if (s.status === 'error') {
      setCollectStatsError(true)
      if (showBanner) {
        setCollectStatsMsg(`Error: ${s.error || 'fallo en la recoleccion'}`)
      }
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
          // Force-invalidate React Query cache so refetch fetches fresh data from the API
          queryClient.invalidateQueries({ queryKey: ['dashboard'] })
          refetchDashboard()
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
        applyStatsStatus(s, false) // no banner for historical results on mount
        if (s.status === 'running') {
          setCollectingStats(true)
          pollStatsStatus()
        } else if (s.status === 'success' && !cancelled) {
          // Dashboard was served stale cache — force-refresh when last collection succeeded
          queryClient.invalidateQueries({ queryKey: ['dashboard'] })
        }
      } catch { /* ignore */ }
    }
    loadStatus()
    return () => { cancelled = true }
  }, [])

  function formatTimeAgo(ts: number): string {
    const secs = Math.floor((Date.now() - ts) / 1000)
    if (secs < 5) return 'ahora'
    if (secs < 60) return `hace ${secs}s`
    const mins = Math.floor(secs / 60)
    if (mins < 60) return `hace ${mins}min`
    const hrs = Math.floor(mins / 60)
    return `hace ${hrs}h`
  }

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
    // Capture current KPI values as snapshot so we can show change after collection
    const k = data?.global_kpis
    setPreviousKpis({
      sparkline_subscribers: k?.subscribers?.value ?? 0,
      sparkline_views: k?.total_views?.value ?? 0,
      sparkline_engagement: k?.engagement?.value ?? 0,
      sparkline_watch_hours: k?.watch_hours?.value ?? 0,
      in_production: k?.in_production?.value ?? 0,
    })
    try {
      await api.collectStats(true)  // deep=true: includes CTR, traffic, demographics
      pollStatsStatus()
    } catch (e: any) {
      setCollectStatsError(true)
      setCollectStatsMsg(`Error: ${e.message || 'desconocido'}`)
      setCollectingStats(false)
    }
  }

  async function handleCollectSocialStats() {
    if (collectingSocialStats) return
    setCollectingSocialStats(true)
    setSocialStatsError(false)
    setSocialStatsMsg('Recolectando stats de redes sociales...')
    try {
      const res = await api.collectSocialStats()  // global: todos los canales, 0 cuota
      const results = res?.results || {}
      const keys = Object.keys(results)
      const parts = keys.length
        ? keys.map(p => `${p}: ${results[p].updated}/${results[p].checked}`).join(' · ')
        : 'sin vídeos publicados en redes'
      setSocialStatsMsg(`Stats sociales listas — ${parts}`)
    } catch (e: any) {
      setSocialStatsError(true)
      setSocialStatsMsg(`Error: ${e.message || 'desconocido'}`)
    }
    setCollectingSocialStats(false)
  }

  async function handleRecalculateSlots() {
    if (recalculatingSlots) return
    setRecalculatingSlots(true)
    setRecalcSlotsError(null)
    setRecalcSlotsResult(null)
    try {
      const res = await api.recalculateOptimalSlotsAll()
      setRecalcSlotsResult(res)
    } catch (e: any) {
      setRecalcSlotsError(e.message || 'Error al recalcular franjas')
    } finally {
      setRecalculatingSlots(false)
    }
  }

  if (!data) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  const kpis = data?.global_kpis
  const channels = data?.channels || []
  const pipeline = data?.pipeline || []
  const shortsPipeline = data?.shorts_pipeline || {}
  const topVideos = data?.top_videos || []
  const recentVideos = data?.recent_videos || []
  const recentShorts = data?.recent_shorts || []
  const publishedShorts = recentShorts.filter((s: any) => (s.status || '').toLowerCase() === 'published')
  const scheduledShorts = recentShorts.filter((s: any) => (s.status || '').toLowerCase() === 'scheduled')
  const todayVideos = data?.today_videos || []
  const heatmapData = data?.heatmap_data || []
  const todayActions = data?.today_actions || []
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
      label: 'Horas (12m)',
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

      {/* Toolbar: Refresh Dashboard + Refresh Stats + Stabilize */}
      <div className="flex items-center justify-end gap-2">
        <span className="text-[10px] text-gray-600 tabular-nums">
          {dataUpdatedAt ? formatTimeAgo(dataUpdatedAt) : ''}
        </span>
        <button
          onClick={() => { setPreviousKpis(null); refetchDashboard() }}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-500/20 bg-gray-500/5 text-gray-400 hover:bg-gray-500/10 hover:border-gray-500/40 transition-all text-xs font-medium"
          title="Refrescar dashboard"
        >
          <RefreshCw size={13} />
          <span>Refrescar</span>
        </button>
        <button
          onClick={handleCollectStats}
          disabled={collectingStats}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-cyan-500/20 bg-cyan-500/5 text-cyan-400 hover:bg-cyan-500/10 hover:border-cyan-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title={quotaExhausted 
            ? 'Recolectar stats en modo scraping (0 cuota Data API)' 
            : 'Recolectar estadisticas de YouTube bajo demanda'}
        >
          {collectingStats ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          <span>{collectingStats ? 'Recolectando...' : 'Recolectar stats'}</span>
        </button>
        <button
          onClick={handleCollectSocialStats}
          disabled={collectingSocialStats}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-green-500/20 bg-green-500/5 text-green-400 hover:bg-green-500/10 hover:border-green-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title="Recolectar stats de redes sociales (Rumble, Dailymotion, Facebook, Bluesky, Mastodon) — 0 cuota de YouTube"
        >
          {collectingSocialStats ? <Loader2 size={13} className="animate-spin" /> : <Share2 size={13} />}
          <span>{collectingSocialStats ? 'Recolectando...' : 'Recolectar stats sociales'}</span>
        </button>
        <button
          onClick={handleStabilize}
          disabled={stabilizing}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-amber-500/20 bg-amber-500/5 text-amber-400 hover:bg-amber-500/10 hover:border-amber-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {stabilizing ? <Loader2 size={13} className="animate-spin" /> : <Wrench size={13} />}
          <span>{stabilizing ? 'Estabilizando...' : 'Estabilizar'}</span>
        </button>
        <button
          onClick={handleRecalculateSlots}
          disabled={recalculatingSlots}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-purple-500/20 bg-purple-500/5 text-purple-400 hover:bg-purple-500/10 hover:border-purple-500/40 transition-all text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
          title="Recalcular franjas óptimas de publicación con datos de YouTube Analytics"
        >
          {recalculatingSlots ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
          <span>{recalculatingSlots ? 'Calculando...' : 'Optimizar franjas'}</span>
        </button>
      </div>

      {/* Stats collection feedback */}
      {collectStatsMsg && (
        <div className={`rounded-lg border animate-fade-in ${
          collectingStats ? 'bg-cyan-500/5 border-cyan-500/20'
            : collectStatsError ? 'bg-amber-500/5 border-amber-500/20'
            : 'bg-green-500/5 border-green-500/20'
        }`}>
          <div className={`flex items-center justify-between gap-2 text-xs py-2 px-3 ${
            collectingStats ? 'text-cyan-400' : collectStatsError ? 'text-amber-400' : 'text-green-400'
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

      {/* Social stats collection feedback */}
      {socialStatsMsg && (
        <div className={`rounded-lg border animate-fade-in ${
          collectingSocialStats ? 'bg-green-500/5 border-green-500/20'
            : socialStatsError ? 'bg-amber-500/5 border-amber-500/20'
            : 'bg-green-500/5 border-green-500/20'
        }`}>
          <div className={`flex items-center justify-between gap-2 text-xs py-2 px-3 ${
            collectingSocialStats ? 'text-green-400' : socialStatsError ? 'text-amber-400' : 'text-green-400'
          }`}>
            <span className="flex items-center gap-1.5 font-medium">
              {collectingSocialStats ? <Loader2 size={12} className="animate-spin" /> : socialStatsError ? <AlertCircle size={14} /> : <CheckCircle2 size={14} />}
              {socialStatsMsg}
            </span>
            <button onClick={() => setSocialStatsMsg(null)} className="opacity-60 hover:opacity-100 transition-opacity">
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

      {/* Optimal slots recalculation feedback */}
      {recalcSlotsResult && !recalcSlotsError && (
        <div className="glass p-4 rounded-xl border border-purple-500/20 space-y-2">
          <div className="text-purple-400 text-sm font-medium flex items-center gap-2">
            <Zap size={14} /> Franjas óptimas recalculadas
          </div>
          <div className="text-gray-400 text-xs space-y-1">
            <div>{recalcSlotsResult.channels_processed} canales · {recalcSlotsResult.slots_calculated} franjas calculadas</div>
            {recalcSlotsResult.channels_replanned > 0 && (
              <div className="text-amber-400">
                {recalcSlotsResult.channels_replanned} canales replanificados{' '}
                ({recalcSlotsResult.long_replanned} largos, {recalcSlotsResult.shorts_replanned} shorts)
              </div>
            )}
          </div>
        </div>
      )}
      {recalcSlotsError && (
        <div className="glass p-4 rounded-xl border border-red-500/20">
          <div className="text-red-400 text-sm">{recalcSlotsError}</div>
        </div>
      )}

      {/* ═══════ NIVEL 1: Channel Filter Pills ═══════ */}
      <ChannelFilter channels={channels} pipelineCounts={pipelineCounts} />

      {/* ═══════ NIVEL 1: Vital Signs Bar (KPIs gamificados) ═══════ */}
      <VitalSignsBar
        kpis={kpiList}
        sparklines={sparklines}
        channelBreakdown={viewsBreakdown}
        previousKpis={previousKpis}
      />

      {/* ═══════ NIVEL 1: Activity Heatmap ═══════ */}
      <ActivityHeatmap
        data={heatmapData}
        channelSlugs={channelSlugs}
        channelNames={channelNames}
        channelColors={channelColors}
      />

      {/* ═══════ YouTube API Quota Widget ═══════ */}
      <div className="mb-6">
        <QuotaWidget />
      </div>
      
      {/* ═══════ NIVEL 1: SEO Overview (CTR + Score) ═══════ */}
      <CollapsibleSection title="SEO Overview" icon="🔎" defaultOpen={false}>
        {seoLoading ? (
          <div className="flex items-center justify-center py-6">
            <div className="animate-spin rounded-full h-6 w-6 border-2 border-neon-red border-t-transparent" />
            <span className="ml-3 text-xs text-gray-400">Cargando metricas SEO...</span>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {channels.map((ch: any) => {
              const seoData = seoScores[ch.id]
              const ctr = seoData?.avg_ctr_30d
              const retention = seoData?.avg_retention_30d
              const impressions = seoData?.total_impressions_30d
              const ctrCount = seoData?.ctr_video_count
              return (
                <div
                  key={ch.id}
                  className="rounded-xl border border-dark-500 bg-dark-800/60 p-4 hover:border-gray-400 transition-all"
                >
                  <div className="flex items-center justify-between mb-2">
                    <span
                      className="text-sm font-semibold"
                      style={{ color: channelColors[ch.id] || '#ff3355' }}
                    >
                      {ch.name}
                    </span>
                    {ctr != null && (
                      <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
                        ctr >= 5 ? 'bg-green-500/20 text-green-400' :
                        ctr >= 2 ? 'bg-amber-500/20 text-amber-400' :
                        'bg-red-500/20 text-red-400'
                      }`}>
                        CTR {ctr}%
                      </span>
                    )}
                  </div>
                  <div className="space-y-1.5 text-xs text-gray-400">
                    {ctr != null && (
                      <div className="flex justify-between">
                        <span>CTR 30d</span>
                        <span className={ctr >= 5 ? 'text-green-400' : ctr >= 2 ? 'text-amber-400' : 'text-red-400'}>
                          {ctr}%
                        </span>
                      </div>
                    )}
                    {retention != null && (
                      <div className="flex justify-between">
                        <span>Retencion</span>
                        <span className={retention >= 40 ? 'text-green-400' : retention >= 25 ? 'text-amber-400' : 'text-red-400'}>
                          {retention}%
                        </span>
                      </div>
                    )}
                    {impressions != null && impressions > 0 && (
                      <div className="flex justify-between">
                        <span>Impresiones 30d</span>
                        <span className="text-gray-500">{impressions.toLocaleString()}</span>
                      </div>
                    )}
                    {ctrCount != null && ctrCount > 0 && (
                      <div className="flex justify-between">
                        <span>Videos con datos</span>
                        <span className="text-gray-500">{ctrCount}</span>
                      </div>
                    )}
                    {!ctr && !retention && (
                      <div className="text-center text-gray-600 py-2">
                        Sin datos de analytics
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </CollapsibleSection>

      {/* ═══════ NIVEL 1: View Gap Monitor ═══════ */}
      <CollapsibleSection title="View Gap Monitor" icon="🔍" defaultOpen={false}>
        <ViewGapPanel />
      </CollapsibleSection>

      {/* ═══════ Redes Sociales: visitas traídas por cada red ═══════ */}
      <CollapsibleSection title="Redes Sociales" icon="🌐" defaultOpen={false}>
        <SocialOverview channels={channels} socialSummary={data?.social_summary} />
      </CollapsibleSection>
 
      {/* ═══════ NIVEL 2: Pipeline Activo ═══════ */}
      {(pipeline.length > 0 || Object.keys(shortsPipeline).length > 0) && (
        <CollapsibleSection title="Pipeline Activo" icon="⚙️">
          <PipelineSection pipeline={pipeline} shortsPipeline={shortsPipeline} />
        </CollapsibleSection>
      )}

      {/* ═══════ NIVEL 1: Publicado Hoy ═══════ */}
      <CollapsibleSection title={`Publicado Hoy (${todayVideos.length} videos · ${publishedShorts.length} shorts${scheduledShorts.length ? ` · ${scheduledShorts.length} programados` : ''})`} icon="📺" defaultOpen={true}>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 sm:gap-6">
          <RecentVideos videos={todayVideos.slice(0, 10)} />
          <RecentShorts shorts={recentShorts.slice(0, 10)} />
        </div>
      </CollapsibleSection>

      {/* ═══════ NIVEL 1: Acciones de Hoy ═══════ */}
      <CollapsibleSection title={`Acciones de Hoy (${todayActions.length})`} icon="📋" defaultOpen={true}>
        <RecentActions actions={todayActions} />
      </CollapsibleSection>

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
        <div className="mt-0">
          <UpcomingPublications />
        </div>
      </CollapsibleSection>

      {/* ═══════ NIVEL 2: Top Videos ═══════ */}
      {topVideos.length > 0 && (
        <CollapsibleSection title="Top Videos y Shorts" icon="🏆">
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
