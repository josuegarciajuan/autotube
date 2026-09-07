import { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, ApiRequestError, type FullReplanApplyResult, type FullReplanPreflight } from '../lib/api'
import { useTodaySlots, useShortsSlotsToday, useShortsPlanningConfig, usePlanningConfig } from '../hooks/useQueries'
import { Calendar, Smartphone, Play, Clock, CheckCircle2, Loader2, XCircle, Settings, Plus, Minus, RefreshCw, AlertTriangle, RotateCcw, ShieldCheck, Scissors } from 'lucide-react'
import PipelineView from '../components/PipelineView'
import PacingProfileCard from '../components/PacingProfileCard'
import { getChannelStyles, getChannelShort } from '../lib/channelConfig'

interface ChannelSummary {
  channel_id: number
  channel_name: string
  channel_slug: string
  videos: { pending: number; running: number; completed: number; cancelled: number }
  shorts: { pending: number; running: number; completed: number; generated: number }
  next_time: string | null
  next_kind: string | null
}

// ── Timezone ─────────────────────────────────────────────
// DB stores Europe/Madrid local time strings (e.g. "2026-07-13 21:00:00")
function toLocal(ts: string): string {
  const m = ts.match(/(\d{2}):(\d{2})/)
  return m ? `${m[1]}:${m[2]}` : ts.slice(0, 5)
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
          shorts: { pending: 0, running: 0, completed: 0, generated: 0 },
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
      else if (s.status === 'generated') ch.shorts.generated++
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
        const allDone = ch.videos.pending === 0 && ch.videos.running === 0 && ch.shorts.pending === 0 && ch.shorts.running === 0 && ch.shorts.generated === 0
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
                {ch.shorts.generated > 0 && (
                  <span title={`${ch.shorts.generated} shorts generados en cola, pendientes de subir`} className="px-1.5 py-0.5 rounded bg-sky-400/15 text-sky-400 text-[10px]">{ch.shorts.generated}⏸</span>
                )}
                {ch.shorts.completed === 0 && ch.shorts.running === 0 && ch.shorts.pending === 0 && ch.shorts.generated === 0 && (
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

// ── Cola de shorts generados (status='generated') ─────────
// (fix ago 2026) Shorts nativos renderizados pero SIN subir (p. ej. durante
// bloqueos de spam o cuota) antes eran invisibles en Programación.
function QueuedShortsSection() {
  const { data: shortsToday, refetch } = useShortsSlotsToday()
  const [uploading, setUploading] = useState<number | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const queued = shortsToday?.queued || []

  if (!queued.length) return null

  const handleUpload = async (shortId: number) => {
    setUploading(shortId); setMsg(null)
    try {
      const res = await api.uploadQueuedShort(shortId)
      setMsg(res?.ok ? `Short #${shortId} subido.` : `No se pudo subir el short #${shortId}.`)
    } catch (e: any) {
      setMsg(e?.message || `Error al subir el short #${shortId}.`)
    } finally {
      setUploading(null)
      refetch()
    }
  }

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Smartphone size={14} className="text-sky-400" /> Cola de shorts generados ({queued.length})
        <span className="text-[10px] text-gray-500 font-normal">renderizados, pendientes de subir</span>
      </h4>
      <div className="bg-dark-700/40 rounded-xl border border-sky-400/20 overflow-hidden">
        {queued.map((q: any) => (
          <div key={q.short_id} className="flex items-center gap-3 px-3 py-2 border-b border-surface-border/40 last:border-0 text-xs">
            <span className="text-gray-500 font-mono">#{q.short_id}</span>
            <span className="text-gray-200 truncate flex-1">{q.title}</span>
            <span className="text-gray-500 shrink-0">{q.channel_name || q.channel_slug}</span>
            <span className="text-gray-600 font-mono shrink-0">{q.created_at?.slice(0, 16)}</span>
            {q.file_exists ? (
              <span className="text-emerald-400 text-[10px] shrink-0">✓ archivo</span>
            ) : (
              <span className="text-red-400 text-[10px] shrink-0" title="El archivo ya no existe en disco">✗ sin archivo</span>
            )}
            <button
              onClick={() => handleUpload(q.short_id)}
              disabled={uploading === q.short_id || !q.file_exists}
              className="px-2 py-1 rounded bg-sky-500/15 text-sky-300 hover:bg-sky-500/25 text-[10px] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            >
              {uploading === q.short_id ? 'Subiendo...' : 'Subir ahora'}
            </button>
          </div>
        ))}
      </div>
      {msg && <p className="text-xs text-gray-400">{msg}</p>}
    </div>
  )
}

// ── Planning config section (real, editable values) ──────
interface PacingChan {
  id: number; slug: string; name: string; delivery_state?: string; manual_override?: boolean
  longform_publish_cap?: number; native_shorts_per_day?: number
  profile_longform_cap?: number; profile_short_cap?: number
}

interface PacingSummary { active_profile?: string; active_channels?: PacingChan[] }

function Stepper({ value, onCommit, min = 0, max = 10, disabled, title }: {
  value: number; onCommit: (v: number) => void; min?: number; max?: number; disabled?: boolean; title?: string
}) {
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <button
        onClick={() => onCommit(Math.max(min, value - 1))}
        disabled={disabled}
        className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed"
      ><Minus size={12} /></button>
      <span className={`text-white font-mono w-4 text-center ${disabled ? 'text-gray-500' : ''}`}>{value}</span>
      <button
        onClick={() => onCommit(Math.min(max, value + 1))}
        disabled={disabled}
        className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed"
      ><Plus size={12} /></button>
    </div>
  )
}

// Percent stepper (weight almacenado como 0..1, mostrado como %)
function BoostRow({ label, value, onChange, disabled, title }: {
  label: string; value: number; onChange: (v: number) => void; disabled?: boolean; title?: string
}) {
  const pct = Math.round(Math.max(0, Math.min(1, value || 0)) * 100)
  const set = (p: number) => onChange(Math.max(0, Math.min(100, p)) / 100)
  return (
    <div className="flex items-center justify-between text-[10px]" title={title}>
      <span className="text-gray-400">{label}</span>
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => set(pct - 5)}
          disabled={disabled}
          className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed"
        ><Minus size={12} /></button>
        <span className={`text-white font-mono w-9 text-center ${disabled ? 'text-gray-500' : ''}`}>{pct}%</span>
        <button
          onClick={() => set(pct + 5)}
          disabled={disabled}
          className="w-6 h-6 rounded bg-dark-500 text-gray-300 hover:bg-dark-400 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed"
        ><Plus size={12} /></button>
      </div>
    </div>
  )
}

function PlanningSection() {
  const queryClient = useQueryClient()
  const { data: rawConfigs = [], isLoading: loadingCfg, isError: errCfg, refetch: refetchCfg } = usePlanningConfig()
  const { data: rawShorts = [], isLoading: loadingSh, isError: errSh, refetch: refetchSh } = useShortsPlanningConfig()
  const [pacing, setPacing] = useState<PacingSummary | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadPacing = useCallback(async () => {
    try { const p = await api.getPacingProfile(); setPacing(p) } catch { /* non-fatal */ }
  }, [])

  const refreshAll = useCallback(() => {
    refetchCfg(); refetchSh(); loadPacing()
    // Refresca solo lo relevante (no invalidar todo el panel a cada clic).
    queryClient.invalidateQueries({ queryKey: ['today-slots'] })
    queryClient.invalidateQueries({ queryKey: ['shorts-slots-today'] })
    queryClient.invalidateQueries({ queryKey: ['planned-slots'] })
    queryClient.invalidateQueries({ queryKey: ['week-slots'] })
  }, [refetchCfg, refetchSh, loadPacing, queryClient])

  useEffect(() => { loadPacing() }, [loadPacing])

  // Actualiza la query cache de forma OPTIMISTA para que el +/- responda al
  // instante; luego el PUT guarda y refetch reconcilia (server = verdad).
  const saveLong = useCallback(async (channelId: number, data: Record<string, unknown>) => {
    setBusyId(channelId); setError(null)
    queryClient.setQueryData<any[]>(['planning-config'], (old: any[] | undefined) =>
      old ? old.map((c: any) => (c.channel_id === channelId ? { ...c, ...data } : c)) : old)
    try {
      await api.updatePlanningConfig(channelId, data as any)
      refreshAll()
    } catch (e: any) {
      setError(e?.message || 'No se pudo guardar.')
      refreshAll() // revertir el optimista si falló el servidor
    } finally {
      setBusyId(null)
    }
  }, [refreshAll, queryClient])

  const saveShort = useCallback(async (channelId: number, data: Record<string, unknown>) => {
    setBusyId(channelId); setError(null)
    queryClient.setQueryData<any[]>(['shorts-planning-config'], (old: any[] | undefined) =>
      old ? old.map((c: any) => (c.channel_id === channelId ? { ...c, ...data } : c)) : old)
    try {
      await api.updateShortsPlanningConfig(channelId, data as any)
      refreshAll()
    } catch (e: any) {
      setError(e?.message || 'No se pudo guardar.')
      refreshAll() // revertir el optimista si falló el servidor
    } finally {
      setBusyId(null)
    }
  }, [refreshAll, queryClient])

  const resetOverride = useCallback(async (channelId: number) => {
    setBusyId(channelId); setError(null)
    try { await api.clearChannelDeliveryOverride(channelId); refreshAll() }
    catch (e: any) { setError(e?.message || 'No se pudo quitar el override.') }
    finally { setBusyId(null) }
  }, [refreshAll])

  const loading = loadingCfg || loadingSh
  const hasError = errCfg || errSh

  // Combinar config long + shorts + pacing por canal
  const rows = useMemo(() => {
    const cfg = rawConfigs.filter((c: any) => c.channel_slug !== 'test')
    const sh = rawShorts.filter((c: any) => c.slug !== 'test')
    const pcmap = new Map<number, PacingChan>((pacing?.active_channels || []).map(c => [c.id, c]))
    const shortsByCid = new Map<number, any>(sh.map(c => [c.channel_id, c]))
    const union = new Map<number, any>()
    for (const c of cfg) union.set(c.channel_id, c)
    for (const c of sh) if (!union.has(c.channel_id)) union.set(c.channel_id, c)
    return Array.from(union.values())
      .map((c: any) => {
        const cid = c.channel_id
        const sc = shortsByCid.get(cid) || {}
        const p: PacingChan = pcmap.get(cid) || { id: cid, slug: c.slug, name: '' }
        const publicTarget = Number(c.public_videos_per_day ?? c.videos_per_day ?? 0)
        const shortTarget = Number(sc.shorts_native_per_day ?? 3)
        const genDay = Number(c.longform_generation_per_day ?? c.videos_per_day ?? 0)
        const effPub = Number(p.longform_publish_cap ?? 0)
        // Vídeos long-form/día que materializa el canal (objetivo de generación
        // acotado por el techo/override efectivo). Es el máximo real de virales.
        let dayLongs = genDay > 0 ? genDay : (publicTarget > 0 ? publicTarget : (effPub || 0))
        if (effPub > 0 && effPub < dayLongs) dayLongs = effPub
        if (dayLongs < 0) dayLongs = 0
        return {
          channel_id: cid,
          channel_name: c.channel_name || c.name || c.slug,
          slug: c.slug,
          planning_enabled: c.planning_enabled ?? true,
          public_videos_per_day: publicTarget,
          longform_generation_per_day: genDay,
          upload_capacity_per_day: Number(c.upload_capacity_per_day ?? c.videos_per_day ?? 0),
          viral_per_day: Math.max(0, Math.min(dayLongs, Number(c.viral_per_day ?? 0))),
          videos_day_boost_weight: Number(c.videos_day_boost_weight ?? 0),
          viral_day_boost_weight: Number(c.viral_day_boost_weight ?? 0),
          dayLongs,
          shorts_enabled: sc.shorts_enabled ?? false,
          shorts_native_per_day: shortTarget,
          delivery_state: p.delivery_state || pacing?.active_profile || 'strike',
          manual_override: !!p.manual_override,
          pub_effective: effPub,
          short_effective: Number(p.native_shorts_per_day ?? 0),
          profile_long_cap: Number(p.profile_longform_cap ?? 0),
          profile_short_cap: Number(p.profile_short_cap ?? 0),
        }
      })
      .sort((a, b) => a.channel_id - b.channel_id)
  }, [rawConfigs, rawShorts, pacing])

  if (loading) return <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin text-gray-600" /></div>
  if (hasError) {
    return (
      <div className="flex flex-col items-center gap-3 py-6 glass rounded-xl">
        <AlertTriangle size={20} className="text-amber-400" />
        <div className="text-center">
          <p className="text-sm text-gray-300">Error al cargar la configuracion</p>
          <p className="text-xs text-gray-500 mt-1">El servidor puede estar reiniciandose.</p>
        </div>
        <button onClick={() => { refetchCfg(); refetchSh() }} className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white transition-colors">
          <RefreshCw size={12} /> Reintentar
        </button>
      </div>
    )
  }
  if (!rows.length) return null

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-gray-500 leading-relaxed">
        Valores <strong className="text-gray-300">reales</strong> que aplica el motor de programación.
        Fijar Publicaciones/Shorts por encima del techo del perfil (<strong>{pacing?.active_profile || 'strike'}</strong>) activa
        un <strong className="text-amber-300">override autoritativo</strong> para ese canal. Cada cambio aplica ya un replan del horizonte.
      </p>
      {error && <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg p-2">{error}</p>}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {rows.map(r => {
          const isOverride = r.manual_override
          const pubShown = isOverride ? r.public_videos_per_day : Math.min(r.public_videos_per_day, r.profile_long_cap || r.public_videos_per_day)
          const shortShown = isOverride ? r.shorts_native_per_day : Math.min(r.shorts_native_per_day, r.profile_short_cap || r.shorts_native_per_day)
          const disabled = !r.planning_enabled || busyId === r.channel_id
          return (
            <div key={r.channel_id} className={`bg-dark-700/50 rounded-xl p-4 space-y-3 border border-surface-border transition-opacity ${!r.planning_enabled ? 'opacity-60' : ''}`}>
              {/* Header */}
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${getChannelStyles({ channel_id: r.channel_id, channel_slug: r.slug }).dot}`} />
                  <span className="text-sm font-medium text-white truncate">{r.channel_name}</span>
                  <span className="text-[10px] text-gray-500 font-mono">{getChannelShort({ channel_id: r.channel_id, channel_slug: r.slug, channel_name: r.channel_name })}</span>
                  {busyId === r.channel_id && <Loader2 size={12} className="animate-spin text-neon-cyan shrink-0" />}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className={`text-[9px] px-1.5 py-0.5 rounded uppercase tracking-wide ${r.delivery_state === 'normal' ? 'text-emerald-300 bg-emerald-500/10' : r.delivery_state === 'recovery' ? 'text-amber-300 bg-amber-500/10' : 'text-red-300 bg-red-500/10'}`}>
                    {r.delivery_state}
                  </span>
                  {isOverride && (
                    <span className="text-[9px] px-1.5 py-0.5 rounded uppercase tracking-wide text-amber-200 bg-amber-500/20 border border-amber-500/30" title="Override manual activo: la cifra fijada manda por encima del perfil">
                      override
                    </span>
                  )}
                </div>
              </div>

              {/* planning_enabled toggle */}
              <button onClick={() => saveLong(r.channel_id, { planning_enabled: !r.planning_enabled })} disabled={busyId === r.channel_id}
                className={`relative w-9 h-5 rounded-full transition-colors ${r.planning_enabled ? 'bg-neon-gold' : 'bg-gray-600'}`} title="Activar/desactivar planificacion">
                <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform ${r.planning_enabled ? 'translate-x-4' : ''}`} />
              </button>

              {/* Long-form: Publicaciones */}
              <div className="border-t border-surface-border/40 pt-2 space-y-2">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 flex items-center gap-1.5"><Play size={12} className="text-neon-gold" /> Publicaciones long-form/día</span>
                  <Stepper value={r.public_videos_per_day} disabled={disabled} onCommit={v => saveLong(r.channel_id, { public_videos_per_day: v })} max={10}
                    title="Publicaciones/día objetivo (si supera el techo, activa override)" />
                </div>
                <p className="text-[10px] text-gray-500">
                  {isOverride
                    ? <>Override: se publican <strong className="text-amber-300">{r.pub_effective}/día</strong> (objetivo autoritativo).</>
                    : <>Techo del perfil <strong>{r.delivery_state}</strong>: <strong className="text-gray-300">{r.profile_long_cap}/día</strong>. Actual: <strong className="text-gray-300">{r.pub_effective}/día</strong></>}
                </p>
                {/* Generación y cap subida */}
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-400">Generación</span>
                    <Stepper value={r.longform_generation_per_day} disabled={disabled} onCommit={v => saveLong(r.channel_id, { longform_generation_per_day: v })} max={10} />
                  </div>
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-gray-400">Cap. subida</span>
                    <Stepper value={r.upload_capacity_per_day} disabled={disabled} onCommit={v => saveLong(r.channel_id, { upload_capacity_per_day: v })} max={20} />
                  </div>
                </div>
              </div>

              {/* Virales (mezcla viral/original del long-form) */}
              <div className="border-t border-surface-border/40 pt-2 space-y-1.5">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 flex items-center gap-1.5" title="Vídeos/día producidos adaptando vídeos ya virales del nicho; el resto son originales del canal.">
                    <Play size={12} className="text-purple-400" /> Virales/día
                  </span>
                  <Stepper value={r.viral_per_day} disabled={disabled} min={0} max={Math.max(0, r.dayLongs)} onCommit={v => saveLong(r.channel_id, { viral_per_day: v })}
                    title="Vídeos/día en modo viral (reescritura de vídeos virales del nicho)" />
                </div>
                <p className="text-[10px] text-gray-500">
                  {r.dayLongs > 0
                    ? <>De {r.dayLongs} vídeos/día: <strong className="text-purple-300">{r.viral_per_day} viral</strong> · {Math.max(0, r.dayLongs - r.viral_per_day)} original.</>
                    : <>Sin vídeos/día planificados.</>}
                  {r.viral_per_day > 0 && <> Sin candidatos virales disponibles → cae a original.</>}
                </p>
                <BoostRow label="Prob. +1 video" value={r.videos_day_boost_weight} disabled={disabled} onChange={v => saveLong(r.channel_id, { videos_day_boost_weight: v })}
                  title="Probabilidad de +1 vídeo extra a generar algunos días" />
                <BoostRow label="Prob. 2º viral" value={r.viral_day_boost_weight} disabled={disabled} onChange={v => saveLong(r.channel_id, { viral_day_boost_weight: v })}
                  title="Probabilidad de sumar un vídeo viral extra dentro del total del día" />
              </div>

              {/* Shorts */}
              <div className="border-t border-surface-border/40 pt-2 space-y-1">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 flex items-center gap-1.5"><Smartphone size={12} className="text-emerald-400" /> Shorts nativos/día</span>
                  <div className="flex items-center gap-2">
                    <button onClick={() => saveShort(r.channel_id, { shorts_enabled: !r.shorts_enabled })} disabled={busyId === r.channel_id}
                      className={`relative w-8 h-4 rounded-full transition-colors ${r.shorts_enabled ? 'bg-emerald-500' : 'bg-gray-600'}`}>
                      <span className={`absolute top-0.5 left-0.5 w-3 h-3 bg-white rounded-full transition-transform ${r.shorts_enabled ? 'translate-x-4' : ''}`} />
                    </button>
                    <Stepper value={r.shorts_native_per_day} disabled={disabled || !r.shorts_enabled} onCommit={v => saveShort(r.channel_id, { shorts_native_per_day: v })} max={10} />
                  </div>
                </div>
                <p className="text-[10px] text-gray-500">
                  {isOverride
                    ? <>Override: se suben <strong className="text-amber-300">{r.short_effective}/día</strong>.</>
                    : <>Techo del perfil: <strong className="text-gray-300">{r.profile_short_cap}/día</strong>. Actual: <strong className="text-gray-300">{r.short_effective}/día</strong></>}
                </p>
              </div>

              {/* Clips deshabilitado con explicación */}
              <div className="border-t border-surface-border/40 pt-2">
                <div className="flex items-center justify-between text-xs opacity-50">
                  <span className="text-gray-400 flex items-center gap-1.5"><Scissors size={12} className="text-orange-400" /> Clips × vídeo largo</span>
                  <span className="text-gray-500 font-mono text-[10px]">desactivado</span>
                </div>
                <p className="text-[9px] text-gray-600 mt-0.5" title="Los clips están desactivados en el pipeline (CLIP_SHORTS_ENABLED=false). Este control no tiene efecto.">
                  Los clips están desactivados en el pipeline (CLIP_SHORTS_ENABLED=false) → este control no tiene efecto.
                </p>
              </div>

              {isOverride && (
                <button onClick={() => resetOverride(r.channel_id)} disabled={busyId === r.channel_id}
                  className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg bg-dark-600 text-gray-300 hover:bg-dark-500 hover:text-white text-[11px] transition-colors disabled:opacity-50">
                  <ShieldCheck size={12} className="text-emerald-400" /> Usar perfil (quitar override)
                </button>
              )}
            </div>
          )
        })}
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
      setReplanError((e instanceof ApiRequestError && (e.status === 503 || e.code === 'SERVER_BUSY'))
        ? 'El servidor está ocupado con otra operación. Espera unos segundos y vuelve a intentarlo.'
        : isExpiredError(message)
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
        <QueuedShortsSection />
      </section>

      {/* ── Section 3: Configuracion ─────────────────────── */}
      <section className="glass rounded-xl p-5 space-y-4">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Settings size={16} className="text-purple-400" /> Configuracion de Programacion
        </h3>
        <PlanningSection />
      </section>
    </div>
  )
}
