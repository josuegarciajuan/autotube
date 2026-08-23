import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  AlertTriangle,
  ShieldAlert,
  Clock,
  FileText,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  ExternalLink,
  Wrench,
  CheckCircle2,
  TrendingUp,
  ListOrdered,
} from 'lucide-react'
import { api } from '../../lib/api'
import { useQuotaStatus, useResumeStatus } from '../../hooks/useQueries'

// Persistencia del plegado: guardamos la FIRMA del último conjunto de avisos
// que el usuario colapsó. La firma solo incluye identidad/severidad (nunca las
// cuentas atrás), así que la tira se re-expande ÚNICAMENTE ante avisos nuevos
// (strike/bloqueo/cuota/sesión); si solo cambian los contadores de tiempo,
// respeta el estado manual del usuario (plegado o desplegado).
const LS_KEY = 'alertcenter_collapsed_sig_v2'
// Estado manual plegado/desplegado (persistido para sobrevivir a refrescos).
const EXPANDED_KEY = 'alertcenter_expanded_v1'

interface SpamBlock {
  channel_id: number
  slug: string
  name: string
  strikes: number
  blocked: boolean
  blocked_until: number | null
  restan_h: number
  freq_reduced: boolean
  scope: string
  why: string
  last_removal: { video_id?: string; reason?: string; detected_at?: string; strike_count?: number } | null
  current_freq?: { videos_per_day?: number; shorts_native_per_day?: number; shorts_clips_per_long?: number }
  original_freq?: { videos_per_day?: number; shorts_native_per_day?: number; shorts_clips_per_long?: number }
  pending_publish?: { total?: number; within_block?: any[] }
}

// Estado de reanudación post-strike (endpoint /system/resume-status)
interface ResumeEntry {
  channel_id: number
  slug: string
  name: string
  source: string
  sibling_of?: string
  start_iso: string
  phase_today: number
  phase_label: string
  days_elapsed?: number
  days_remaining_in_phase?: number | null
  next_transition_iso?: string | null
  freq?: {
    videos_per_day?: number
    alternate_pattern?: number[] | null
    shorts_native_per_day?: number
    shorts_clips_per_long?: number
  }
  strikes?: number
  blocked?: boolean
  restan_h?: number
  freq_reduced?: boolean
  pending_publish?: { total?: number; upcoming?: { video_id?: string; target_public_at?: string }[] }
}

interface QuotaProject {
  project_id: string
  account: string
  channels: string[]
  exhausted: boolean
  exhausted_at: string | null
  reset_at_utc: string | null
  remaining_hours: number | null
}

interface AlertCenterProps {
  onOpenReport: (channel: SpamBlock) => void
}

function sigOf(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return String(h)
}

function fmtRestan(restan_h: number): string {
  if (restan_h <= 0) return 'expirado'
  if (restan_h >= 24) {
    const d = Math.floor(restan_h / 24)
    const h = Math.round(restan_h % 24)
    return `~${d}d ${h}h`
  }
  return `~${restan_h.toFixed(1)}h`
}

function fmtFreq(block: SpamBlock): string {
  const cur = block.current_freq || {}
  const orig = block.original_freq || {}
  const parts: string[] = []
  if (orig.videos_per_day != null && cur.videos_per_day != null) {
    parts.push(`long ${orig.videos_per_day}→${cur.videos_per_day}`)
  } else if (cur.videos_per_day != null) {
    parts.push(`long ${cur.videos_per_day}/d`)
  }
  if (orig.shorts_native_per_day != null && cur.shorts_native_per_day != null) {
    parts.push(`shorts ${orig.shorts_native_per_day}→${cur.shorts_native_per_day}`)
  } else if (cur.shorts_native_per_day != null) {
    parts.push(`shorts ${cur.shorts_native_per_day}/d`)
  }
  if (orig.shorts_clips_per_long != null && cur.shorts_clips_per_long != null) {
    parts.push(`clips ${orig.shorts_clips_per_long}→${cur.shorts_clips_per_long}`)
  } else if (cur.shorts_clips_per_long != null) {
    parts.push(`clips ${cur.shorts_clips_per_long}`)
  }
  return parts.join(' · ') || '—'
}

export default function AlertCenter({ onOpenReport }: AlertCenterProps) {
  const [spamBlocks, setSpamBlocks] = useState<SpamBlock[]>([])
  const [sessionWarnings, setSessionWarnings] = useState<{ account: string; channels: string[] }[]>([])
  const [expanded, setExpanded] = useState<boolean>(() => localStorage.getItem(EXPANDED_KEY) !== '0')
  const [collapsedSig, setCollapsedSig] = useState<string>(() => localStorage.getItem(LS_KEY) || '')
  const [busy, setBusy] = useState<number | null>(null)
  const [applyingAll, setApplyingAll] = useState(false)
  const { data: quotaStatus } = useQuotaStatus()
  const { data: resumeData } = useResumeStatus()

  // Índice channel_id → estado de reanudación (para enriquecer las tarjetas)
  const resumeMap: Record<number, ResumeEntry> = useMemo(() => {
    const m: Record<number, ResumeEntry> = {}
    if (resumeData?.ok && Array.isArray(resumeData.channels)) {
      for (const c of resumeData.channels as ResumeEntry[]) {
        m[Number(c.channel_id)] = c
      }
    }
    return m
  }, [resumeData])

  const resumeChannels = useMemo(
    () => Object.values(resumeMap).filter(c => c.phase_today > 0 || c.blocked),
    [resumeMap],
  )

  const loadSpamBlocks = useCallback(async () => {
    try {
      const res = await api.getSpamBlocks()
      if (res?.ok && Array.isArray(res.channels)) {
        setSpamBlocks(res.channels.filter((c: any) => c.blocked || c.freq_reduced))
      } else {
        setSpamBlocks([])
      }
    } catch { /* banner opcional, no crítico */ }
  }, [])

  useEffect(() => {
    loadSpamBlocks()
    const iv = setInterval(loadSpamBlocks, 5 * 60 * 1000)
    return () => clearInterval(iv)
  }, [loadSpamBlocks])

  useEffect(() => {
    const check = async () => {
      try {
        const data = await api.getBrowserSessionStatus()
        const expired = (data.accounts || [])
          .filter((a: any) => a.status === 'expired')
          .map((a: any) => ({ account: a.account, channels: a.channels }))
        setSessionWarnings(expired)
      } catch { /* servidor reiniciándose */ }
    }
    check()
    const iv = setInterval(check, 5 * 60 * 1000)
    return () => clearInterval(iv)
  }, [])

  const quotaProjects: QuotaProject[] = useMemo(() => {
    const list: QuotaProject[] = Array.isArray((quotaStatus as any)?.projects)
      ? (quotaStatus as any).projects.filter((p: QuotaProject) => p.exhausted)
      : []
    if (list.length > 0) return list
    if ((quotaStatus as any)?.exhausted) {
      return [{
        project_id: '', account: '', channels: [], exhausted: true,
        exhausted_at: (quotaStatus as any).exhausted_at ?? null,
        reset_at_utc: (quotaStatus as any).reset_at_utc ?? null,
        remaining_hours: (quotaStatus as any).remaining_hours ?? null,
      }]
    }
    return []
  }, [quotaStatus])

  const totalAvisos = spamBlocks.length + quotaProjects.length + sessionWarnings.length
  const hasBlocked = spamBlocks.some(c => c.blocked)

  const currentSig = useMemo(() => {
    // Firma ESTABLE: solo identidad/severidad de los avisos. Excluimos
    // deliberadamente los contadores temporales (restan_h, remaining_hours),
    // que cambian en cada poll y provocaban que la tira se re-expandiera sola
    // aunque no hubiera ningún aviso nuevo.
    return sigOf(JSON.stringify({
      spam: spamBlocks.map(c => [c.channel_id, c.strikes, c.blocked, c.freq_reduced, c.scope]),
      quota: quotaProjects.map(p => [p.project_id, p.account, p.channels]),
      sessions: sessionWarnings.map(s => [s.account, s.channels]),
    }))
  }, [spamBlocks, quotaProjects, sessionWarnings])

  // Auto-expansión SOLO ante avisos realmente nuevos: si el usuario colapsó
  // (collapsedSig guardado) y la firma actual difiere, se re-abre. Si solo
  // cambiaron los contadores de tiempo, la firma no cambia y se respeta el
  // estado manual (plegado o desplegado). Nunca fuerza colapso.
  useEffect(() => {
    if (!currentSig || !totalAvisos) return
    if (collapsedSig && currentSig !== collapsedSig) {
      setExpanded(true)
      localStorage.setItem(EXPANDED_KEY, '1')
    }
  }, [currentSig, collapsedSig, totalAvisos])

  function handleToggle() {
    setExpanded(prev => {
      const next = !prev
      localStorage.setItem(EXPANDED_KEY, next ? '1' : '0')
      if (!next && currentSig) {
        setCollapsedSig(currentSig)
        localStorage.setItem(LS_KEY, currentSig)
      } else {
        setCollapsedSig('')
        localStorage.removeItem(LS_KEY)
      }
      return next
    })
  }

  async function handleRestore(c: SpamBlock) {
    setBusy(c.channel_id)
    try {
      await api.restoreSpamFrequency(c.channel_id)
      await loadSpamBlocks()
    } catch { /* noop */ }
    setBusy(null)
  }

  async function handleUnblock(c: SpamBlock) {
    if (!window.confirm(`¿Desbloquear ${c.name || c.slug}? Solo si verificaste en YouTube Studio que la penalización cesó.`)) return
    setBusy(c.channel_id)
    try {
      await api.unblockSpamChannel(c.channel_id)
      await loadSpamBlocks()
    } catch { /* noop */ }
    setBusy(null)
  }

  async function handleApplyAll() {
    if (applyingAll) return
    setApplyingAll(true)
    try {
      await api.applyResumePhases()
      await loadSpamBlocks()
    } catch { /* noop */ }
    setApplyingAll(false)
  }

  function phaseBadge(phase: number): { label: string; cls: string } {
    if (phase === 1) return { label: 'Fase 1 · 1 long/2 días', cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30' }
    if (phase === 2) return { label: 'Fase 2 · 1 long/día', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' }
    return { label: 'No iniciado', cls: 'bg-gray-500/15 text-gray-400 border-gray-500/30' }
  }

  function phaseCountdown(r: ResumeEntry): string {
    if (r.phase_today === 1 && typeof r.days_remaining_in_phase === 'number') {
      const n = r.days_remaining_in_phase
      return n <= 0 ? 'pasa a Fase 2 hoy' : `quedan ${n} día${n !== 1 ? 's' : ''} · pasa a Fase 2`
    }
    if (r.phase_today === 0 && r.blocked && typeof r.restan_h === 'number' && r.restan_h > 0) {
      return `bloqueado ~${r.restan_h.toFixed(1)}h`
    }
    if (r.next_transition_iso) {
      return `desde ${new Date(r.start_iso).toLocaleDateString('es-ES', { day: '2-digit', month: 'short' })}`
    }
    return ''
  }

  if (totalAvisos === 0) return null

  const barTone = hasBlocked
    ? 'from-red-500/15 via-red-500/5 to-transparent border-red-500/25'
    : 'from-amber-500/15 via-amber-500/5 to-transparent border-amber-500/25'

  return (
    <div className={`flex-shrink-0 border-b bg-gradient-to-r ${barTone} animate-fade-in`}>
      {/* ── Fila de resumen (siempre visible) ── */}
      <button
        onClick={handleToggle}
        className="w-full flex items-center gap-2.5 px-4 py-1.5 text-xs hover:bg-white/[0.03] transition-colors text-left"
        aria-expanded={expanded}
      >
        <ShieldAlert size={15} className={hasBlocked ? 'text-neon-red flex-shrink-0' : 'text-amber-400 flex-shrink-0'} />
        <span className="font-semibold text-gray-200 whitespace-nowrap">
          {totalAvisos} aviso{totalAvisos !== 1 ? 's' : ''} de restricción
        </span>

        <span className="flex items-center gap-1.5 flex-wrap min-w-0">
          {spamBlocks.filter(c => c.blocked).length > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-red-500/15 text-red-300 border border-red-500/30 font-medium whitespace-nowrap">
              {spamBlocks.filter(c => c.blocked).length} bloqueado{spamBlocks.filter(c => c.blocked).length > 1 ? 's' : ''}
            </span>
          )}
          {spamBlocks.filter(c => !c.blocked && c.freq_reduced).length > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30 font-medium whitespace-nowrap">
              {spamBlocks.filter(c => !c.blocked && c.freq_reduced).length} freq rebajada
            </span>
          )}
          {quotaProjects.length > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-300 border border-blue-500/30 font-medium whitespace-nowrap">
              {quotaProjects.length} sin cuota
            </span>
          )}
          {sessionWarnings.length > 0 && (
            <span className="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30 font-medium whitespace-nowrap">
              {sessionWarnings.length} sesión{sessionWarnings.length > 1 ? 'es' : ''} caducada{sessionWarnings.length > 1 ? 's' : ''}
            </span>
          )}
        </span>

        <span className="ml-auto flex items-center gap-1 text-gray-400 whitespace-nowrap flex-shrink-0">
          {expanded ? 'Ver menos' : 'Ver detalle'}
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </span>
      </button>

      {/* ── Panel expandido ── */}
      {expanded && (
        <div className="px-4 pb-3 pt-1 space-y-2.5 max-h-[55vh] overflow-y-auto animate-fade-in">
          {/* ── Reanudación post-strike (fases) ── */}
          {resumeChannels.length > 0 && (
            <div className="rounded-lg border border-cyan-500/25 px-3 py-2 bg-dark-800/60">
              <div className="flex items-center gap-2 flex-wrap">
                <TrendingUp size={14} className="text-cyan-300 flex-shrink-0" />
                <span className="font-semibold text-sm text-cyan-200">Reanudación post-strike</span>
                <span className="text-[10px] text-gray-500">
                  {resumeChannels.filter(c => c.phase_today === 1).length} en Fase 1 · {resumeChannels.filter(c => c.phase_today === 2).length} en Fase 2
                </span>
                <button
                  onClick={handleApplyAll}
                  disabled={applyingAll}
                  className="ml-auto flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-cyan-500/15 text-cyan-200 border border-cyan-500/30 rounded-md hover:bg-cyan-500 hover:text-dark-900 transition-all disabled:opacity-50"
                >
                  {applyingAll ? <RefreshCw size={12} className="animate-spin" /> : <TrendingUp size={12} />}
                  {applyingAll ? 'Aplicando...' : 'Re-aplicar fases'}
                </button>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {resumeChannels.map(r => {
                  const b = phaseBadge(r.phase_today)
                  const cd = phaseCountdown(r)
                  return (
                    <span key={r.channel_id} className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${b.cls}`}>
                      {r.name || r.slug} · {b.label}
                      {cd && <span className="opacity-80"> · {cd}</span>}
                    </span>
                  )
                })}
              </div>
            </div>
          )}

          {/* Strikes / spam */}
          {spamBlocks.map(c => {
            const isBlocked = c.blocked
            const res = resumeMap[c.channel_id]
            const durTotal = (c.strikes >= 2 ? 168 : 72) + 6
            const pct = isBlocked && c.restan_h > 0
              ? Math.max(0, Math.min(100, 100 * (1 - c.restan_h / durTotal)))
              : 100
            const removal = c.last_removal
            const scopeLabel = c.scope === 'todo' ? 'todo (shorts + vídeos)' : c.scope
            return (
              <div
                key={c.channel_id}
                className={`rounded-lg border px-3 py-2.5 bg-dark-800/60 ${
                  isBlocked ? 'border-red-500/30' : 'border-amber-500/30'
                }`}
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`font-semibold text-sm ${isBlocked ? 'text-red-300' : 'text-amber-300'}`}>
                    {c.name || c.slug}
                  </span>
                  <code className="text-[10px] text-gray-500 bg-dark-700 px-1 py-0.5 rounded">{c.slug}</code>
                  {c.strikes > 0 && (
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                      c.strikes >= 2 ? 'bg-neon-red/20 text-neon-red' : 'bg-red-500/15 text-red-300'
                    } border border-red-500/30`}>
                      {c.strikes} strike{c.strikes > 1 ? 's' : ''}
                    </span>
                  )}
                  {isBlocked && (
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-neon-red/20 text-neon-red border border-neon-red/40">
                      BLOQUEADO
                    </span>
                  )}
                  {c.freq_reduced && (
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">
                      frecuencia rebajada
                    </span>
                  )}
                  <span className="text-[10px] text-gray-500">{scopeLabel}</span>
                  {res && res.phase_today > 0 && (
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${phaseBadge(res.phase_today).cls}`}>
                      {phaseBadge(res.phase_today).label}
                    </span>
                  )}
                  {res && phaseCountdown(res) && (
                    <span className="text-[10px] text-cyan-300/80 flex items-center gap-1">
                      <Clock size={10} /> {phaseCountdown(res)}
                    </span>
                  )}
                </div>

                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                  {isBlocked ? (
                    <span className="flex items-center gap-1.5 text-red-300">
                      <Clock size={12} />
                      <b>Restan {fmtRestan(c.restan_h)}</b>
                      {c.blocked_until && (
                        <span className="text-gray-500">
                          (hasta {new Date(c.blocked_until * 1000).toLocaleString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })})
                        </span>
                      )}
                    </span>
                  ) : c.strikes > 0 ? (
                    <span className="text-emerald-400 flex items-center gap-1.5">
                      <CheckCircle2 size={12} /> Bloqueo expirado
                    </span>
                  ) : null}
                  <span className="text-gray-400">Frecuencia: <b className="text-gray-200">{fmtFreq(c)}</b></span>
                </div>

                {/* Barra de progreso del bloqueo */}
                {isBlocked && c.restan_h > 0 && (
                  <div className="mt-2 h-1 rounded-full bg-dark-600 overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-neon-red to-red-400 transition-all"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                )}

                {c.why && <p className="mt-2 text-xs text-gray-400 leading-relaxed">⚠️ {c.why}</p>}
                {removal && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
                    <span>Vídeo: <code className="text-gray-300 bg-dark-700 px-1 py-0.5 rounded">{removal.video_id || 'desconocido'}</code></span>
                    {removal.detected_at && (
                      <span>Detectado: {new Date(removal.detected_at).toLocaleString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</span>
                    )}
                    {removal.reason && <span className="italic">“{removal.reason}”</span>}
                  </div>
                )}

                {/* Próximas publicaciones (esparcido Fase 1) */}
                {res && (res.pending_publish?.upcoming?.length ?? 0) > 0 && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-gray-500">
                    <span className="flex items-center gap-1 text-gray-400">
                      <ListOrdered size={11} />
                      Publicaciones ({res.pending_publish?.total}):
                    </span>
                    {(res.pending_publish?.upcoming ?? []).slice(0, 6).map((p, i) => (
                      <span key={i} className="bg-dark-700 px-1.5 py-0.5 rounded text-gray-300">
                        {p.target_public_at
                          ? new Date(p.target_public_at).toLocaleString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
                          : p.video_id || '?'}
                      </span>
                    ))}
                  </div>
                )}

                <div className="mt-2 flex items-center gap-2 flex-wrap">
                  <button
                    onClick={() => onOpenReport(c)}
                    className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-red-500/15 text-red-200 border border-red-500/30 rounded-md hover:bg-red-500 hover:text-white transition-all"
                  >
                    <FileText size={12} /> Informe
                  </button>
                  {c.freq_reduced && (
                    <button
                      onClick={() => handleRestore(c)}
                      disabled={busy === c.channel_id}
                      className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-amber-500/15 text-amber-200 border border-amber-500/30 rounded-md hover:bg-amber-500 hover:text-dark-900 transition-all disabled:opacity-50"
                    >
                      {busy === c.channel_id ? <RefreshCw size={12} className="animate-spin" /> : <Wrench size={12} />}
                      Restaurar frecuencia
                    </button>
                  )}
                  {isBlocked && (
                    <button
                      onClick={() => handleUnblock(c)}
                      disabled={busy === c.channel_id}
                      className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-dark-600 text-gray-300 border border-surface-border rounded-md hover:bg-dark-500 transition-all disabled:opacity-50"
                    >
                      Desbloquear (verificado)
                    </button>
                  )}
                </div>
              </div>
            )
          })}

          {/* Cuota agotada */}
          {quotaProjects.map(p => {
            const label = p.account || p.project_id || 'cuenta'
            const pReset = p.reset_at_utc
              ? new Date(p.reset_at_utc).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
              : null
            return (
              <div key={p.project_id || label} className="rounded-lg border border-blue-500/30 px-3 py-2.5 bg-dark-800/60">
                <div className="flex items-center gap-2 flex-wrap">
                  <AlertTriangle size={14} className="text-blue-300 flex-shrink-0" />
                  <span className="font-semibold text-sm text-blue-300">Cuota YouTube API agotada</span>
                  <code className="text-[10px] text-gray-400 bg-dark-700 px-1 py-0.5 rounded">{label}</code>
                  {p.channels.length > 0 && (
                    <span className="text-[11px] text-gray-500">({p.channels.join(', ')})</span>
                  )}
                  <span className="ml-auto flex items-center gap-1 text-xs text-blue-300">
                    <Clock size={12} />
                    {pReset ? `Recarga a las ${pReset}` : 'Recarga a medianoche PT'}
                    {p.remaining_hours != null && p.remaining_hours > 0 && (
                      <span className="text-blue-200/70">(~{p.remaining_hours.toFixed(1)}h)</span>
                    )}
                  </span>
                </div>
              </div>
            )
          })}

          {/* Sesiones caducadas */}
          {sessionWarnings.map(w => (
            <div key={w.account} className="rounded-lg border border-amber-500/30 px-3 py-2.5 bg-dark-800/60">
              <div className="flex items-center gap-2 flex-wrap">
                <AlertTriangle size={14} className="text-amber-400 flex-shrink-0" />
                <span className="font-semibold text-sm text-amber-300">Sesión de navegador caducada</span>
                <code className="text-[10px] text-gray-400 bg-dark-700 px-1 py-0.5 rounded">{w.account}</code>
                {w.channels.length > 0 && <span className="text-[11px] text-gray-500">({w.channels.join(', ')})</span>}
                <span className="ml-auto text-[11px] text-gray-400 flex items-center gap-1.5">
                  <ExternalLink size={11} />
                  <code className="bg-dark-700 px-1.5 py-0.5 rounded text-amber-200">
                    python3 scripts/yt_browser_login.py --account {w.account}
                  </code>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
