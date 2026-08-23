import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type FullReplanApplyResult, type FullReplanPreflight } from '../lib/api'
import { useTodaySlots, useShortsSlotsToday, useShortsPlanningConfig, usePlanningConfig } from '../hooks/useQueries'
import { Calendar, Video, Smartphone, Scissors, Play, Clock, CheckCircle2, Loader2, XCircle, Settings, Plus, Minus, RefreshCw, AlertTriangle, RotateCcw } from 'lucide-react'
import PipelineView from '../components/PipelineView'
import ChannelConfigCard from '../components/ChannelConfigCard'
import PacingProfileCard from '../components/PacingProfileCard'
import { getChannelStyles, getChannelShort } from '../lib/channelConfig'

interface ChannelSummary {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos: { pending: number; running: number; completed: number; cancelled: number }
  shorts: { pending: number; running: number; completed: number }
  next_time: string | null
  next_kind: string | null
}

interface ShortsPlanningConfig {
  channel_id: number; name: string; slug: string
  shorts_enabled: boolean
  shorts_native_per_day: number
  shorts_clips_per_long: number
}

interface PlanningConfig {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos_per_day: number
  viral_per_day: number
  planning_enabled: boolean
  videos_day_boost_weight: number
  viral_day_boost_weight: number
}

// ── Timezone ─────────────────────────────────────────────
// DB stores Europe/Madrid local time strings (e.g. "2026-07-13 21:00:00")
function toLocal(ts: string): string {
  const m = ts.match(/(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}` : ts.slice(0, 5)
}

// ── Shorts config card ───────────────────────────────────
function ShortsCard({ config, onUpdate }: { config: ShortsPlanningConfig; onUpdate: (d: any) => void }) {
  return (
    <div className="bg-dark-700/50 rounded-xl p-4 space-y-3 border border-surface-border">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-white">{config.name}</span>
        <button onClick={() => onUpdate({ shorts_enabled: !config.shorts_enabled })}
          className={`relative w-9 h-5 rounded-full transition-colors ${config.shorts_enabled ? 'bg-neon-red' : 'bg-gray-600'}`}>
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${config.shorts_enabled ? 'translate-x-4' : ''}`} />
        </button>
      </div>
      {config.shorts_enabled && (
        <>
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-400 flex items-center gap-1.5">
              <Smartphone size={12} className="text-emerald-400" /> Nativos/dia
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => onUpdate({ shorts_native_per_day: Math.max(0, config.shorts_native_per_day - 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Minus size={12} /></button>
              <span className="text-white font-mono w-4 text-center">{config.shorts_native_per_day}</span>
              <button onClick={() => onUpdate({ shorts_native_per_day: Math.min(5, config.shorts_native_per_day + 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Plus size={12} /></button>
            </div>
          </div>
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-400 flex items-center gap-1.5">
              <Scissors size={12} className="text-orange-400" /> Clips × vídeo largo
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => onUpdate({ shorts_clips_per_long: Math.max(0, (config.shorts_clips_per_long ?? 3) - 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Minus size={12} /></button>
              <span className="text-white font-mono w-4 text-center">{config.shorts_clips_per_long ?? 3}</span>
              <button onClick={() => onUpdate({ shorts_clips_per_long: Math.min(5, (config.shorts_clips_per_long ?? 3) + 1) })}
                className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center"><Plus size={12} /></button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// ── Today Status cards ───────────────────────────────────
function TodayStatus() {
  const { data: today, isLoading: loadingToday, isError: errorToday, refetch: refetchToday } = useTodaySlots()
  const { data: shortsToday, isLoading: loadingShorts, isError: errorShorts, refetch: refetchShorts } = useShortsSlotsToday()
  const loading = loadingToday || loadingShorts
  const hasError = errorToday || errorShorts

  const channels = useMemo(() => {
    if (!today && !shortsToday) return [] as ChannelSummary[]
    const map = new Map<number, ChannelSummary>()
    const ensure = (id: number, name: string, slug: string) => {
      if (!map.has(id)) {
        map.set(id, {
          channel_id: id, channel_name: name, channel_slug: slug,
          videos: { pending: 0, running: 0, completed: 0, cancelled: 0 },
          shorts: { pending: 0, running: 0, completed: 0 },
          next_time: null, next_kind: null,
        })
      }
      return map.get(id)!
    }
    for (const s of (today?.slots || [])) {
      const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
      if (s.status === 'completed') ch.videos.completed++
      else if (s.status === 'running') ch.videos.running++
      else if (s.status === 'pending') ch.videos.pending++
      else if (s.status === 'cancelled') ch.videos.cancelled++
      if (s.status === 'pending' && s.scheduled_at && (!ch.next_time || s.scheduled_at < ch.next_time)) {
        ch.next_time = s.scheduled_at; ch.next_kind = 'video'
      }
    }
    for (const s of (shortsToday?.slots || [])) {
      const ch = ensure(s.channel_id, s.channel_name, s.channel_slug)
      if (s.status === 'completed') ch.shorts.completed++
      else if (s.status === 'running') ch.shorts.running++
      else if (s.status === 'pending') ch.shorts.pending++
      if (s.status === 'pending' && s.scheduled_at && (!ch.next_time || s.scheduled_at < ch.next_time)) {
        ch.next_time = s.scheduled_at; ch.next_kind = 'short'
      }
    }
    return Array.from(map.values()).sort((a, b) => a.channel_id - b.channel_id)
  }, [today, shortsToday])

  if (loading) {
    return <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin text-gray-600" /></div>
  }
  if (hasError) {
    return (
      <div className="flex flex-col items-center gap-3 py-6 glass rounded-xl">
        <AlertTriangle size={20} className="text-amber-400" />
        <div className="text-center">
          <p className="text-sm text-gray-300">Error al cargar el estado de hoy</p>
          <p className="text-xs text-gray-500 mt-1">El servidor puede estar reiniciandose.</p>
        </div>
        <button
          onClick={() => { if (errorToday) refetchToday(); if (errorShorts) refetchShorts(); }}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white transition-colors"
        >
          <RefreshCw size={12} /> Reintentar
        </button>
      </div>
    )
  }
  if (channels.length === 0) {
    return <p className="text-xs text-gray-500 text-center py-4">Sin actividad hoy.</p>
  }

  const tzLabel = 'Europe/Madrid'

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
      {channels.map((ch) => {
        const colors = getChannelStyles({ channel_id: ch.channel_id, channel_slug: ch.channel_slug })
        const allDone = ch.videos.pending === 0 && ch.videos.running === 0 && ch.shorts.pending === 0 && ch.shorts.running === 0
        const hasRunning = ch.videos.running > 0 || ch.shorts.running > 0
        const hasPending = ch.videos.pending > 0 || ch.shorts.pending > 0
        const hasCancelled = ch.videos.cancelled > 0

        return (
          <div key={ch.channel_id} className={`rounded-xl p-4 border ${hasRunning ? 'bg-neon-cyan/5 border-neon-cyan/30' : 'bg-dark-700/50 border-surface-border'}`}>
            {/* Channel header */}
            <div className="flex items-center gap-2 mb-3">
              <span className={`w-2.5 h-2.5 rounded-full ${colors.dot}`} />
              <span className="text-sm font-semibold text-white">{ch.channel_name}</span>
              <span className="text-[10px] text-gray-600 font-mono">({getChannelShort({ channel_id: ch.channel_id, channel_slug: ch.channel_slug, channel_name: ch.channel_name })})</span>
              {hasRunning && <Loader2 size={12} className="text-neon-cyan animate-spin ml-auto" />}
              {allDone && !hasCancelled && <CheckCircle2 size={12} className="text-green-400 ml-auto" />}
              {!hasRunning && !allDone && !hasPending && hasCancelled && <XCircle size={12} className="text-red-400 ml-auto" />}
              {!hasRunning && hasPending && <Clock size={12} className="text-amber-400 ml-auto" />}
            </div>

            {/* Videos row */}
            <div className="flex items-center gap-2 text-xs mb-1.5">
              <Play size={11} className="text-neon-gold" />
              <span className="text-gray-400">Videos:</span>
              <div className="flex gap-1 ml-auto">
                {ch.videos.completed > 0 && (
                  <span title={`${ch.videos.completed} videos completados hoy`} className="px-1.5 py-0.5 rounded bg-green-400/15 text-green-400 text-[10px]">{ch.videos.completed}✓</span>
                )}
                {ch.videos.running > 0 && (
                  <span title={`${ch.videos.running} videos generándose ahora`} className="px-1.5 py-0.5 rounded bg-neon-cyan/15 text-neon-cyan text-[10px]">{ch.videos.running}▸</span>
                )}
                {ch.videos.pending > 0 && (
                  <span title={`${ch.videos.pending} videos pendientes de generar`} className="px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-400 text-[10px]">{ch.videos.pending}⏳</span>
                )}
                {ch.videos.cancelled > 0 && (
                  <span title={`${ch.videos.cancelled} videos cancelados`} className="px-1.5 py-0.5 rounded bg-red-400/15 text-red-400 text-[10px]">{ch.videos.cancelled}✕</span>
                )}
                {ch.videos.completed === 0 && ch.videos.running === 0 && ch.videos.pending === 0 && ch.videos.cancelled === 0 && (
                  <span className="text-gray-600 text-[10px]">—</span>
                )}
              </div>
            </div>

            {/* Shorts row */}
            <div className="flex items-center gap-2 text-xs mb-2">
              <Smartphone size={11} className="text-emerald-400" />
              <span className="text-gray-400">Shorts:</span>
              <div className="flex gap-1 ml-auto">
                {ch.shorts.completed > 0 && (
                  <span title={`${ch.shorts.completed} shorts completados hoy`} className="px-1.5 py-0.5 rounded bg-green-400/15 text-green-400 text-[10px]">{ch.shorts.completed}✓</span>
                )}
                {ch.shorts.running > 0 && (
                  <span title={`${ch.shorts.running} shorts generándose ahora`} className="px-1.5 py-0.5 rounded bg-neon-cyan/15 text-neon-cyan text-[10px]">{ch.shorts.running}▸</span>
                )}
                {ch.shorts.pending > 0 && (
                  <span title={`${ch.shorts.pending} shorts pendientes de generar`} className="px-1.5 py-0.5 rounded bg-purple-400/10 text-purple-400 text-[10px]">{ch.shorts.pending}⏳</span>
                )}
                {ch.shorts.completed === 0 && ch.shorts.running === 0 && ch.shorts.pending === 0 && (
                  <span className="text-gray-600 text-[10px]">—</span>
                )}
              </div>
            </div>

            {/* Next execution */}
            {ch.next_time && (
              <div className="text-[10px] text-gray-500 flex items-center gap-1 pt-1 border-t border-surface-border/50">
                <Clock size={9} />
                Proximo: {ch.next_kind === 'short' ? 'Short' : 'Video'} a las{' '}
                <span className="text-gray-300 font-mono">{toLocal(ch.next_time)} {tzLabel}</span>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Shorts section ───────────────────────────────────────
function ShortsSection() {
  const { data: configs = [], isLoading: loading, isError, refetch } = useShortsPlanningConfig()
  const update = useCallback(async (channelId: number, data: any) => {
    try { await api.updateShortsPlanningConfig(channelId, data); refetch() } catch (e: any) { alert(e.message) }
  }, [refetch])
  const activeConfigs = configs.filter((c: any) => c.slug !== 'test')
  if (loading) return null
  if (isError) {
    return (
      <div className="flex flex-col items-center gap-3 py-6 glass rounded-xl">
        <AlertTriangle size={20} className="text-amber-400" />
        <div className="text-center">
          <p className="text-sm text-gray-300">Error al cargar config de shorts</p>
          <p className="text-xs text-gray-500 mt-1">El servidor puede estar reiniciandose.</p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white transition-colors"
        >
          <RefreshCw size={12} /> Reintentar
        </button>
      </div>
    )
  }
  if (!activeConfigs.length) return null
  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Smartphone size={14} className="text-emerald-400" /> Shorts por canal
      </h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {activeConfigs.map(ch => (<ShortsCard key={ch.channel_id} config={ch} onUpdate={(d) => update(ch.channel_id, d)} />))}
      </div>
    </div>
  )
}

// ── Planning config section ──────────────────────────────
function PlanningConfigSection() {
  const { data: rawConfigs = [], isLoading: loading, isError, refetch } = usePlanningConfig()
  const configs = rawConfigs.filter((c: any) => c.channel_slug !== 'test')

  const update = useCallback(async (channelId: number, data: { videos_per_day?: number; planning_enabled?: boolean; viral_per_day?: number }) => {
    try {
      await api.updatePlanningConfig(channelId, data)
      refetch()
    } catch (e: any) {
      alert(e.message)
    }
  }, [refetch])

  if (loading) {
    return <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin text-gray-600" /></div>
  }

  if (isError) {
    return (
      <div className="flex flex-col items-center gap-3 py-6 glass rounded-xl">
        <AlertTriangle size={20} className="text-amber-400" />
        <div className="text-center">
          <p className="text-sm text-gray-300">Error al cargar la configuracion</p>
          <p className="text-xs text-gray-500 mt-1">El servidor puede estar reiniciandose.</p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white transition-colors"
        >
          <RefreshCw size={12} /> Reintentar
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Video size={14} className="text-neon-gold" /> Videos largos por canal
      </h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {configs.map(cfg => (
          <ChannelConfigCard
            key={cfg.channel_id}
            config={cfg}
            onUpdate={(data) => update(cfg.channel_id, data)}
          />
        ))}
      </div>
    </div>
  )
}

// ── Main scheduling page ─────────────────────────────────
export default function Scheduling() {
  const [showReplanModal, setShowReplanModal] = useState(false)
  const [replanState, setReplanState] = useState<'idle' | 'loading-review' | 'review' | 'applying' | 'result' | 'error'>('idle')
  const [preflight, setPreflight] = useState<FullReplanPreflight | null>(null)
  const [replanResult, setReplanResult] = useState<FullReplanApplyResult | null>(null)
  const [replanError, setReplanError] = useState<string | null>(null)
  const reviewButtonRef = useRef<HTMLButtonElement>(null)
  const replanDialogRef = useRef<HTMLDivElement>(null)
  const replanTriggerRef = useRef<HTMLButtonElement>(null)
  const queryClient = useQueryClient()

  const isExpiredError = (message: string) => /expir|caduc|stale|invalid|no longer/i.test(message)

  const openReplanReview = useCallback(async () => {
    setShowReplanModal(true)
    setReplanState('loading-review')
    setPreflight(null)
    setReplanResult(null)
    setReplanError(null)
    try {
      const result = await api.fullReplanPreflight()
      setPreflight(result)
      setReplanState('review')
    } catch (e: any) {
      setReplanError(e?.message || 'No se pudo preparar la revisión.')
      setReplanState('error')
    }
  }, [])

  const closeReplanModal = useCallback(() => {
    if (replanState !== 'applying') {
      setShowReplanModal(false)
      requestAnimationFrame(() => replanTriggerRef.current?.focus())
    }
  }, [replanState])

  useEffect(() => {
    if (!showReplanModal) return
    const focusable = replanDialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )
    if (focusable?.length) reviewButtonRef.current?.focus() ?? focusable[0].focus()
    else replanDialogRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeReplanModal()
      if (event.key !== 'Tab' || !focusable?.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [showReplanModal, replanState, closeReplanModal])

  const handleReplan = useCallback(async () => {
    if (!preflight?.confirmation_token) return
    setReplanState('applying')
    setReplanResult(null)
    setReplanError(null)
    try {
      const result = await api.fullReplanApply(preflight.confirmation_token)
      setReplanResult(result)
      setReplanState('result')
      if (result.ok) {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ['today-slots'] }),
          queryClient.invalidateQueries({ queryKey: ['shorts-slots-today'] }),
          queryClient.invalidateQueries({ queryKey: ['planned-slots'] }),
          queryClient.invalidateQueries({ queryKey: ['week-slots'] }),
          queryClient.invalidateQueries({ queryKey: ['planning-config'] }),
          queryClient.invalidateQueries({ queryKey: ['shorts-planning-config'] }),
          queryClient.invalidateQueries({ queryKey: ['active-jobs'] }),
        ])
      }
    } catch (e: any) {
      const message = e?.message || 'No se pudo aplicar la reprogramación.'
      setReplanError(isExpiredError(message)
        ? 'La revisión ha caducado o ya no coincide con la planificación actual. Revísala de nuevo antes de confirmar.'
        : message)
      setReplanState('error')
    }
  }, [preflight, queryClient])

  return (
    <div className="max-w-6xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-2xl font-bold text-white flex items-center gap-3">
          <Calendar size={24} className="text-neon-gold" />
          Programacion
        </h2>
        <button
          ref={replanTriggerRef}
          onClick={openReplanReview}
          disabled={replanState === 'loading-review' || replanState === 'applying'}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 hover:bg-amber-500/20 hover:text-amber-200 text-sm font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {replanState === 'loading-review' || replanState === 'applying' ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <RotateCcw size={14} />
          )}
          {replanState === 'loading-review' || replanState === 'applying' ? 'Reprogramando...' : 'Reprogramar Ahora'}
        </button>
      </div>

      {/* ── Replan confirmation modal ── */}
      {showReplanModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onMouseDown={closeReplanModal}>
          <div ref={replanDialogRef} role="dialog" aria-modal="true" aria-labelledby="replan-dialog-title" tabIndex={-1} className="bg-dark-800 border border-surface-border rounded-2xl p-6 w-full max-w-md shadow-2xl" onMouseDown={(event) => event.stopPropagation()}>
            {replanState === 'loading-review' || replanState === 'applying' ? (
              /* ── Loading state ── */
              <div className="flex flex-col items-center gap-4 py-6">
                <Loader2 size={32} className="animate-spin text-amber-400" />
                <p className="text-white text-sm font-medium">{replanState === 'loading-review' ? 'Preparando revisión segura...' : 'Aplicando la planificación revisada...'}</p>
                <p className="text-gray-400 text-xs text-center">
                  {replanState === 'loading-review' ? 'Calculando el impacto antes de cambiar ningún slot.' : 'Actualizando slots, cupos y colisiones sin recargar la página.'}
                </p>
              </div>
            ) : replanState === 'result' && replanResult ? (
              /* ── Result state ── */
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  {replanResult.ok ? (
                    <CheckCircle2 size={20} className="text-green-400" />
                  ) : (
                    <AlertTriangle size={20} className="text-amber-400" />
                  )}
                    <h3 id="replan-dialog-title" className="text-white font-semibold text-base">
                    {replanResult.ok ? 'Reprogramación aplicada' : 'Reprogramación no aplicada'}
                  </h3>
                </div>

                {replanResult.ok && (
                  <div className="bg-dark-700/50 rounded-lg p-3 space-y-1 text-xs">
                    <p className="text-gray-400">
                      <span className="text-neon-cyan">{replanResult.updated}</span> slots reprogramados
                    </p>
                    <p className="text-gray-400"><span className="text-green-400">+{replanResult.created}</span> slots nuevos</p>
                    <p className="text-gray-500"><span className="text-gray-300">{replanResult.preserved}</span> slots preservados</p>
                  </div>
                )}

                <div className="flex gap-2">
                  <button
                    ref={reviewButtonRef}
                    onClick={closeReplanModal}
                    className="px-4 py-2 rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 text-sm transition-colors ml-auto"
                  >
                    Cerrar
                  </button>
                </div>
              </div>
            ) : replanState === 'error' ? (
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  <AlertTriangle size={20} className="text-amber-400" />
                  <h3 id="replan-dialog-title" className="text-white font-semibold text-base">Revisión no disponible</h3>
                </div>
                <p className="text-amber-200 text-sm bg-amber-500/10 rounded-lg p-3" role="alert">{replanError}</p>
                <div className="flex gap-2 pt-1">
                  <button onClick={closeReplanModal} className="flex-1 px-4 py-2 rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 text-sm transition-colors">Cerrar</button>
                  <button ref={reviewButtonRef} onClick={openReplanReview} className="flex-1 px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 hover:bg-amber-500/30 text-sm font-medium transition-colors">Revisar de nuevo</button>
                </div>
              </div>
            ) : (
              /* ── Confirmation state ── */
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  <RotateCcw size={20} className="text-amber-400" />
                  <h3 id="replan-dialog-title" className="text-white font-semibold text-base">Revisar reprogramación completa</h3>
                </div>

                <div className="text-sm text-gray-300 space-y-2">
                  <p>Esta revisión calcula los cambios antes de aplicarlos:</p>
                  <ul className="list-disc list-inside text-gray-400 space-y-1 text-xs ml-2">
                    <li>Preservar slots pendientes que no necesiten cambios</li>
                    <li>Reprogramar solo los horarios necesarios, sin tocar vídeos ni jobs</li>
                    <li>Añadir slots nuevos cuando el plan lo requiera, respetando:</li>
                  </ul>
                  <ul className="list-disc list-inside text-gray-500 space-y-0.5 text-xs ml-6">
                    <li>Cupo diario por canal (videos + shorts)</li>
                    <li>Franjas horarias de upload y publicacion</li>
                    <li>Colisiones entre canales y mismo canal</li>
                    <li>Videos/shorts ya en vuelo (generando, pendiente subida, calentando)</li>
                  </ul>
                  <div className="bg-amber-500/10 border border-amber-500/20 rounded-lg p-3 mt-3">
                    <p className="text-amber-300 text-xs flex items-start gap-2">
                      <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                      <span>Los slots en <strong>generacion activa</strong> (running) se preservan. Esta acción solo ajusta horarios pendientes y añade los nuevos necesarios.</span>
                    </p>
                  </div>
                  {preflight && (
                    <div className="bg-dark-700/50 rounded-lg p-3 space-y-1 text-xs text-gray-400">
                      <p><span className="text-neon-cyan">{preflight.summary.proposed}</span> slots propuestos para los próximos {preflight.summary.horizon_days} días</p>
                      <p>{preflight.proposed_slots.length} horarios revisados, conservando slots, vídeos y jobs existentes.</p>
                      {preflight.expires_at ? <p className="text-gray-500">Esta revisión caduca: {preflight.expires_at}</p> : null}
                    </div>
                  )}
                </div>

                <div className="flex gap-2 pt-1">
                  <button
                    onClick={closeReplanModal}
                    className="flex-1 px-4 py-2 rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 text-sm transition-colors"
                  >
                    Cancelar
                  </button>
                  <button
                    ref={reviewButtonRef}
                    onClick={handleReplan}
                    disabled={!preflight?.confirmation_token}
                    className="flex-1 px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 hover:bg-amber-500/30 text-sm font-medium transition-colors"
                  >
                    Confirmar y aplicar
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Section 0: Perfil de cadencia (strike mode) ── */}
      <PacingProfileCard />

      {/* ── Section 1: Pipeline Visual (3-columnas) ── */}
      <section className="glass rounded-xl p-5 space-y-3">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Play size={16} className="text-neon-gold" /> Pipeline de Publicacion
          <span className="text-xs text-gray-500 font-normal">(Europe/Madrid)</span>
        </h3>
        <PipelineView />
      </section>

      {/* ── Section 2: Estado de Hoy ─────────────────────── */}
      <section className="glass rounded-xl p-5 space-y-3">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Clock size={16} className="text-neon-cyan" /> Estado de Hoy
           <span className="text-xs text-gray-500 font-normal">(Europe/Madrid)</span>
        </h3>
        <TodayStatus />
      </section>

      {/* ── Section 3: Configuracion ─────────────────────── */}
      <section className="glass rounded-xl p-5 space-y-4">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Settings size={16} className="text-purple-400" /> Configuracion de Programacion
        </h3>
        <PlanningConfigSection />
        <ShortsSection />
      </section>
    </div>
  )
}
