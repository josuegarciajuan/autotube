import { useState, useEffect, useMemo } from 'react'
import {
  ShieldAlert,
  Shield,
  Clock,
  FileText,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  ExternalLink,
  Wrench,
  AlertTriangle,
  CheckCircle2,
  TrendingUp,
  Ban,
  Radio,
} from 'lucide-react'
import { api, parseApiDate, formatApiDate, formatTime } from '../../lib/api'
import { useQuotaStatus, useChannelRestrictions } from '../../hooks/useQueries'

// Persistencia del plegado: guardamos el CONJUNTO de identidades de los avisos
// presentes cuando el usuario colapsó la tira. La auto-expansión solo se dispara
// si aparece una identidad que NO estaba (aviso realmente nuevo).
const LS_KEY = 'alertcenter_collapsed_ids_v5'
const EXPANDED_KEY = 'alertcenter_expanded_v3'

type Sev = 'critical' | 'blocked' | 'warning' | 'defensive' | 'ok'

interface ChannelRestriction {
  channel_id: number
  slug: string
  name: string
  verdict: { severity: Sev; label: string; detail: string }
  studio_scan: {
    status?: string
    channel?: string
    findings?: string[]
    scanned_at?: string | null
  } | null
  internal: {
    blocked: boolean
    blocked_until: number | null
    restan_h: number
    strikes: number
    scope: string
    why: string
    freq_reduced: boolean
    phase: number
    phase_label: string
    phase_days_remaining: number | null
    next_transition_iso: string | null
    pending_publish_total: number
  }
  youtube: {
    checked_at: string | null
    shorts: { youtube_id: string; title: string; visibility: string; published_at: string | null; publish_at: string | null }[]
    videos: any[]
    age_restricted: any[]
    removed: any[]
    discrepancies: { type: string; youtube_id: string; title: string; publish_at?: string }[]
  }
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
  onOpenReport: (channel: any) => void
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

function fmtDate(iso: string | number | null | undefined): string {
  if (!iso) return ''
  try {
    const d = typeof iso === 'number' ? new Date(iso * 1000) : parseApiDate(iso)
    return d ? formatApiDate(typeof iso === 'number' ? new Date(iso * 1000).toISOString() : iso, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''
  } catch {
    return ''
  }
}

const VIS_LABEL: Record<string, { text: string; cls: string }> = {
  public: { text: 'Público', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  scheduled: { text: 'Programado', cls: 'bg-sky-500/15 text-sky-300 border-sky-500/30' },
  private: { text: 'Privado', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  age_restricted: { text: 'Restricción edad', cls: 'bg-red-500/15 text-red-300 border-red-500/30' },
  removed: { text: 'Eliminado', cls: 'bg-red-500/20 text-red-200 border-red-500/40' },
  unavailable: { text: 'No disponible', cls: 'bg-red-500/15 text-red-300 border-red-500/30' },
}

// Paleta por severidad del veredicto único.
const SEV: Record<Sev, { chip: string; card: string }> = {
  critical: { chip: 'bg-red-500/20 text-red-200 border-red-500/40', card: 'border-red-500/40' },
  blocked: { chip: 'bg-red-500/15 text-red-300 border-red-500/30', card: 'border-red-500/30' },
  warning: { chip: 'bg-amber-500/15 text-amber-300 border-amber-500/30', card: 'border-amber-500/30' },
  defensive: { chip: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30', card: 'border-cyan-500/30' },
  ok: { chip: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30', card: 'border-emerald-500/30' },
}

const ACTIONABLE: Sev[] = ['critical', 'blocked', 'warning']

export default function AlertCenter({ onOpenReport }: AlertCenterProps) {
  const [expanded, setExpanded] = useState<boolean>(() => localStorage.getItem(EXPANDED_KEY) !== '0')
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(LS_KEY) || '[]')
      return new Set(Array.isArray(raw) ? raw.filter((x: unknown) => typeof x === 'string') : [])
    } catch { return new Set() }
  })
  const [busy, setBusy] = useState<number | null>(null)
  const [applyingAll, setApplyingAll] = useState(false)
  const [sessionWarnings, setSessionWarnings] = useState<{ account: string; channels: string[] }[]>([])
  const { data: restrictions } = useChannelRestrictions()
  const { data: quotaStatus } = useQuotaStatus()

  const channels: ChannelRestriction[] = useMemo(
    () => (restrictions?.channels as ChannelRestriction[]) || [],
    [restrictions],
  )

  // Solo los canales con un problema REAL (crítico/bloqueo/advertencia) son alerta.
  const actionable = useMemo(
    () => channels.filter(c => ACTIONABLE.includes(c?.verdict?.severity)),
    [channels],
  )
  // Canales en modo defensivo de política (informativos, NO alerta).
  const defensive = useMemo(
    () => channels.filter(c => c?.verdict?.severity === 'defensive'),
    [channels],
  )

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

  const totalAvisos = actionable.length + quotaProjects.length + sessionWarnings.length

  // Identidades de los avisos actuales (para auto-expansión solo ante algo nuevo).
  const currentIds = useMemo(() => {
    const ids = new Set<string>()
    for (const c of actionable) ids.add(`ch:${c.channel_id}:${c.verdict.severity}`)
    for (const p of quotaProjects) ids.add(`quota:${p.project_id || 'x'}:${p.account || 'x'}`)
    for (const s of sessionWarnings) ids.add(`session:${s.account}`)
    return [...ids].sort()
  }, [actionable, quotaProjects, sessionWarnings])

  useEffect(() => {
    if (collapsedIds.size === 0) return
    if (currentIds.some(id => !collapsedIds.has(id))) {
      setExpanded(true)
      localStorage.setItem(EXPANDED_KEY, '1')
    }
  }, [currentIds, collapsedIds])

  function handleToggle() {
    setExpanded(prev => {
      const next = !prev
      localStorage.setItem(EXPANDED_KEY, next ? '1' : '0')
      if (!next) {
        setCollapsedIds(new Set(currentIds))
        localStorage.setItem(LS_KEY, JSON.stringify(currentIds))
      } else {
        setCollapsedIds(new Set())
        localStorage.removeItem(LS_KEY)
      }
      return next
    })
  }

  async function handleRefresh() {
    setBusy(0)
    try { await api.getChannelRestrictions() } catch { /* noop */ }
    setBusy(null)
  }

  async function handleRestore(c: ChannelRestriction) {
    setBusy(c.channel_id)
    try { await api.restoreSpamFrequency(c.channel_id); await handleRefresh() } catch { /* noop */ }
    setBusy(null)
  }

  async function handleUnblock(c: ChannelRestriction) {
    if (!window.confirm(`¿Desbloquear ${c.name || c.slug}? Solo si verificaste en YouTube Studio que la penalización cesó.`)) return
    setBusy(c.channel_id)
    try { await api.unblockSpamChannel(c.channel_id); await handleRefresh() } catch { /* noop */ }
    setBusy(null)
  }

  async function handleApplyAll() {
    if (applyingAll) return
    setApplyingAll(true)
    try { await api.applyResumePhases(); await handleRefresh() } catch { /* noop */ }
    setApplyingAll(false)
  }

  async function handleStudioScan(c: ChannelRestriction) {
    if (!window.confirm(`¿Escanear YouTube Studio de ${c.name || c.slug}? Abrirá Studio con el perfil de la cuenta para leer restricciones reales.`)) return
    setBusy(c.channel_id)
    try { await api.studioScan(c.channel_id); await handleRefresh() } catch { /* noop */ }
    setBusy(null)
  }

  // Si no hay ningún problema real, la barra no debe ocupar espacio.
  if (totalAvisos === 0) return null

  const sevRank: Sev[] = ['critical', 'blocked', 'warning', 'defensive', 'ok']
  const maxSev = (actionable[0]?.verdict?.severity || 'defensive')
  const barTone = maxSev === 'critical' || maxSev === 'blocked'
    ? 'from-red-500/15 via-red-500/5 to-transparent border-red-500/25'
    : maxSev === 'warning'
      ? 'from-amber-500/15 via-amber-500/5 to-transparent border-amber-500/25'
      : 'from-sky-500/15 via-sky-500/5 to-transparent border-sky-500/25'

  return (
    <div className={`flex-shrink-0 border-b bg-gradient-to-r ${barTone} animate-fade-in`}>
      {/* ── Fila de resumen ── */}
      <button
        onClick={handleToggle}
        className="w-full flex items-center gap-2.5 px-4 py-1.5 text-xs hover:bg-white/[0.03] transition-colors text-left"
        aria-expanded={expanded}
      >
        <ShieldAlert size={15} className={`${maxSev === 'critical' || maxSev === 'blocked' ? 'text-neon-red' : 'text-amber-400'} flex-shrink-0`} />
        <span className="font-semibold text-gray-200 whitespace-nowrap">
          {totalAvisos} aviso{totalAvisos !== 1 ? 's' : ''} de estado
        </span>

        {/* Un chip por canal con su veredicto único */}
        <span className="flex items-center gap-1.5 flex-wrap min-w-0">
          {actionable.map(c => {
            const sev = SEV[c.verdict.severity] || SEV.warning
            return (
              <span key={c.channel_id} className={`px-1.5 py-0.5 rounded border font-medium whitespace-nowrap ${sev.chip}`}>
                {c.name || c.slug} · {c.verdict.label}
              </span>
            )
          })}
          {defensive.length > 0 && (
            <span className="px-1.5 py-0.5 rounded border border-cyan-500/30 bg-cyan-500/10 text-cyan-300 font-medium whitespace-nowrap">
              {defensive.length} en modo defensivo
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

        <span className="ml-auto flex items-center gap-1.5 text-gray-400 whitespace-nowrap flex-shrink-0">
          {restrictions?.generated_at && (
            <span className="hidden md:inline text-[10px] text-gray-500">
              verific. {fmtDate(restrictions.generated_at)}
            </span>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); handleRefresh() }}
            title="Refrescar estado real (0 cuota)"
            className="p-1 rounded hover:bg-white/10 text-gray-400"
          >
            <RefreshCw size={13} className={busy === 0 ? 'animate-spin' : ''} />
          </button>
          {expanded ? 'Ver menos' : 'Ver detalle'}
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </span>
      </button>

      {/* ── Panel expandido ── */}
      {expanded && (
        <div className="px-4 pb-3 pt-1 space-y-3 max-h-[60vh] overflow-y-auto animate-fade-in">

          {/* 1. Alertas activas (canales con problema real) */}
          {actionable.length > 0 && (
            <div className="rounded-lg border border-red-500/25 px-3 py-2 bg-dark-800/60">
              <div className="flex items-center gap-2 flex-wrap">
                <ShieldAlert size={14} className="text-red-400 flex-shrink-0" />
                <span className="font-semibold text-sm text-red-200">Alertas activas</span>
                <span className="text-[10px] text-gray-500">{actionable.length} canal(es) requieren atención</span>
              </div>

              <div className="mt-2 space-y-2">
                {actionable.map(c => (
                  <ChannelCard
                    key={c.channel_id}
                    c={c}
                    onOpenReport={onOpenReport}
                    busy={busy === c.channel_id}
                    onRestore={() => handleRestore(c)}
                    onUnblock={() => handleUnblock(c)}
                    onStudioScan={() => handleStudioScan(c)}
                  />
                ))}
              </div>
            </div>
          )}

          {/* 2. Modo defensivo (política, informativo, no es alerta) */}
          {defensive.length > 0 && (
            <div className="rounded-lg border border-cyan-500/20 px-3 py-2 bg-dark-800/50">
              <div className="flex items-center gap-2 flex-wrap">
                <Shield size={14} className="text-cyan-300 flex-shrink-0" />
                <span className="font-semibold text-sm text-cyan-200">Modo defensivo (política vigente)</span>
                <span className="text-[10px] text-gray-500">no es una sanción real; es la cadencia anti-spam actual</span>
                {defensive.some(c => c.internal.freq_reduced) && (
                  <button
                    onClick={handleApplyAll}
                    disabled={applyingAll}
                    className="ml-auto flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-cyan-500/15 text-cyan-200 border border-cyan-500/30 rounded-md hover:bg-cyan-500 hover:text-dark-900 transition-all disabled:opacity-50"
                  >
                    {applyingAll ? <RefreshCw size={12} className="animate-spin" /> : <TrendingUp size={12} />}
                    Re-aplicar fases
                  </button>
                )}
              </div>

              <div className="mt-2 space-y-1.5">
                {defensive.map(c => {
                  const sev = SEV[c.verdict.severity]
                  return (
                    <div key={c.channel_id} className={`rounded-md border ${sev.card} px-3 py-1.5 bg-dark-900/30`}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-sm text-gray-100">{c.name || c.slug}</span>
                        <code className="text-[10px] text-gray-500 bg-dark-700 px-1 py-0.5 rounded">{c.slug}</code>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${sev.chip}`}>{c.verdict.label}</span>
                        {c.internal.strikes > 0 && (
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-red-500/10 text-red-300 border border-red-500/20">
                            {c.internal.strikes} strike{c.internal.strikes > 1 ? 's' : ''}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-xs text-gray-400">{c.verdict.detail}</div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* 3. Cuota y sesiones */}
          {(quotaProjects.length > 0 || sessionWarnings.length > 0) && (
            <div className="rounded-lg border border-blue-500/25 px-3 py-2 bg-dark-800/60">
              <div className="flex items-center gap-2 flex-wrap">
                <Shield size={14} className="text-blue-300 flex-shrink-0" />
                <span className="font-semibold text-sm text-blue-200">Cuota y sesiones (no es restricción de contenido)</span>
              </div>
              <div className="mt-2 space-y-1.5">
                {quotaProjects.map(p => {
                  const label = p.account || p.project_id || 'cuenta'
                  const pReset = p.reset_at_utc ? (formatTime(p.reset_at_utc) || '—') : null
                  return (
                    <div key={p.project_id || label} className="flex items-center gap-2 flex-wrap text-xs">
                      <AlertTriangle size={12} className="text-blue-300" />
                      <span className="text-blue-200 font-medium">Cuota YouTube API agotada</span>
                      <code className="text-[10px] text-gray-400 bg-dark-700 px-1 py-0.5 rounded">{label}</code>
                      {p.channels.length > 0 && <span className="text-[11px] text-gray-500">({p.channels.join(', ')})</span>}
                      <span className="ml-auto flex items-center gap-1 text-blue-300">
                        <Clock size={12} />
                        {pReset ? `Recarga a las ${pReset}` : 'Recarga a medianoche PT'}
                        {p.remaining_hours != null && p.remaining_hours > 0 && (
                          <span className="text-blue-200/70">(~{p.remaining_hours.toFixed(1)}h)</span>
                        )}
                      </span>
                    </div>
                  )
                })}
                {sessionWarnings.map(w => (
                  <div key={w.account} className="flex items-center gap-2 flex-wrap text-xs">
                    <AlertTriangle size={12} className="text-amber-400" />
                    <span className="text-amber-200 font-medium">Sesión de navegador caducada</span>
                    <code className="text-[10px] text-gray-400 bg-dark-700 px-1 py-0.5 rounded">{w.account}</code>
                    {w.channels.length > 0 && <span className="text-[11px] text-gray-500">({w.channels.join(', ')})</span>}
                    <span className="ml-auto flex items-center gap-1.5 text-[11px] text-gray-400">
                      <ExternalLink size={11} />
                      <code className="bg-dark-700 px-1.5 py-0.5 rounded text-amber-200">
                        python3 scripts/yt_browser_login.py --account {w.account}
                      </code>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Leyenda (visibilidad de shorts, dentro del detalle) */}
          <div className="text-[10px] text-gray-500 flex flex-wrap gap-x-3 gap-y-1 px-1">
            <span className="flex items-center gap-1"><CheckCircle2 size={11} className="text-emerald-400" /> Público</span>
            <span className="flex items-center gap-1"><Clock size={11} className="text-sky-300" /> Programado</span>
            <span className="flex items-center gap-1"><AlertTriangle size={11} className="text-amber-300" /> Privado/desconocido</span>
            <span className="flex items-center gap-1"><Ban size={11} className="text-red-300" /> Eliminado / restricción edad</span>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Tarjeta de un canal con problema real: un veredicto + detalle colapsable ──
interface ChannelCardProps {
  c: ChannelRestriction
  busy: boolean
  onOpenReport: (channel: any) => void
  onRestore: () => void
  onUnblock: () => void
  onStudioScan: () => void
}

function ChannelCard({ c, busy, onOpenReport, onRestore, onUnblock, onStudioScan }: ChannelCardProps) {
  const sev = SEV[c.verdict.severity] || SEV.warning
  const findings = c.studio_scan?.findings || []
  const lastScan = c.studio_scan?.scanned_at

  return (
    <div className={`rounded-md border ${sev.card} px-3 py-2 bg-dark-900/40`}>
      {/* Cabecera: canal + UN veredicto */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-semibold text-sm text-gray-100">{c.name || c.slug}</span>
        <code className="text-[10px] text-gray-500 bg-dark-700 px-1 py-0.5 rounded">{c.slug}</code>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${sev.chip}`}>{c.verdict.label}</span>
        {c.internal.strikes > 0 && (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-red-500/15 text-red-300 border border-red-500/30">
            {c.internal.strikes} strike{c.internal.strikes > 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Una línea de resumen del estado real */}
      <div className="mt-1.5 text-xs text-gray-300">{c.verdict.detail}</div>

      {/* Detalle interno colapsable (oculto por defecto) */}
      <details className="mt-1.5">
        <summary className="cursor-pointer text-[11px] text-gray-400 hover:text-gray-200 select-none">
          Detalle interno
        </summary>
        <div className="mt-1.5 space-y-1.5 text-[11px] text-gray-400">
          {c.internal.blocked && (
            <div className="flex items-center gap-1.5">
              <Clock size={12} className="text-red-300" />
              Bloqueo interno hasta {fmtDate(c.internal.blocked_until)} (~{fmtRestan(c.internal.restan_h)})
            </div>
          )}
          {c.internal.freq_reduced && (
            <div className="flex items-center gap-1.5 text-amber-200">
              <AlertTriangle size={12} />
              Frecuencia de publicación rebajada (restaurar cuando verifiques en Studio que la penalización cesó).
            </div>
          )}
          {c.internal.phase > 0 && (
            <div className="flex items-center gap-1.5 text-cyan-200">
              <TrendingUp size={12} />
              Reanudación gradual: {c.internal.phase_label}
              {c.internal.phase_days_remaining != null ? ` · quedan ${c.internal.phase_days_remaining} día(s)` : ''}
            </div>
          )}
          {c.internal.why && <div className="text-gray-500">⚠️ {c.internal.why}</div>}

          {/* Hallazgos reales de Studio */}
          {findings.length > 0 && (
            <div>
              <span className="font-medium text-amber-300/80">Hallazgos de Studio{lastScan ? ` (${fmtDate(lastScan)})` : ''}:</span>
              <ul className="mt-1 space-y-0.5">
                {findings.map((f, i) => (
                  <li key={i} className="bg-dark-700/50 px-1.5 py-0.5 rounded text-gray-300">{f}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Discrepancias BD vs YouTube */}
          {c.youtube.discrepancies.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              <span className="font-medium text-amber-300/80">Discrepancias BD↔YouTube:</span>
              {c.youtube.discrepancies.map((d, i) => (
                <span key={i} className="bg-dark-700 px-1.5 py-0.5 rounded text-gray-300">
                  {d.type === 'bd_published_yt_removed' ? '⚠ BD dice publicado pero está ELIMINADO' : 'BD dice publicado pero YouTube lo tiene programado/privado'}
                  {d.publish_at ? ` · ${fmtDate(d.publish_at)}` : ''} · {d.title}
                </span>
              ))}
            </div>
          )}

          {/* Estado real de los shorts recientes */}
          {c.youtube.shorts.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {c.youtube.shorts.slice(0, 8).map(s => {
                const vl = VIS_LABEL[s.visibility] || { text: s.visibility || '?', cls: 'bg-gray-500/15 text-gray-400 border-gray-500/30' }
                return (
                  <span key={s.youtube_id} className={`px-1 py-0.5 rounded border ${vl.cls}`} title={s.title}>
                    {vl.text}
                  </span>
                )
              })}
              {c.youtube.checked_at && (
                <span className="text-gray-500 px-1">verific. {fmtDate(c.youtube.checked_at)}</span>
              )}
            </div>
          )}
        </div>
      </details>

      {/* Acciones contextuales */}
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        <button
          onClick={() => onOpenReport(c)}
          className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-red-500/15 text-red-200 border border-red-500/30 rounded-md hover:bg-red-500 hover:text-white transition-all"
        >
          <FileText size={12} /> Informe
        </button>
        <button
          onClick={onStudioScan}
          disabled={busy}
          className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-dark-600 text-gray-300 border border-surface-border rounded-md hover:bg-dark-500 transition-all disabled:opacity-50"
        >
          <Radio size={12} /> Escanear Studio
        </button>
        {c.internal.freq_reduced && (
          <button
            onClick={onRestore}
            disabled={busy}
            className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-amber-500/15 text-amber-200 border border-amber-500/30 rounded-md hover:bg-amber-500 hover:text-dark-900 transition-all disabled:opacity-50"
          >
            {busy ? <RefreshCw size={12} className="animate-spin" /> : <Wrench size={12} />}
            Restaurar frecuencia
          </button>
        )}
        {c.internal.blocked && (
          <button
            onClick={onUnblock}
            disabled={busy}
            className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-dark-600 text-gray-300 border border-surface-border rounded-md hover:bg-dark-500 transition-all disabled:opacity-50"
          >
            Desbloquear (verificado)
          </button>
        )}
      </div>
    </div>
  )
}
