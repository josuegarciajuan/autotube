/** Insights AI Tab — AI self-optimization analysis viewer.
 *
 *  Features:
 *    - Immersive loading screen with live 3-phase progress
 *    - AI summary + key metric chips with sparklines
 *    - Recommendation cards grouped by category
 *    - 3-button model: Expandir / Descartar / Aplicar | Prompt OpenCode
 *    - Expandable detail view with config changes table
 *    - Discarded state (collapsed gray bar, restorable)
 */

import { useEffect, useState, useRef, useCallback } from 'react'
import { api } from '../lib/api'
import type { ChannelInsight, InsightRecommendation, InsightCategory, KeyMetric, RefinedVersion, ValidationResult } from '../types/channel'
import { getCategoryMeta } from '../types/channel'
import {
  Brain, Clock, Loader2, AlertCircle, ChevronDown, ChevronUp,
  Check, X, Copy, ExternalLink, TrendingUp, Zap, BarChart3,
  Eye, EyeOff, Sparkles, Send, MessageSquare, RotateCcw, ThumbsUp,
  ThumbsDown, AlertTriangle, Search,
} from 'lucide-react'

// ── Props ──────────────────────────────────────────────────────────

interface Props {
  channelId: number
  insights: ChannelInsight | null
  setInsights: (i: ChannelInsight | null) => void
  analyzing: boolean
  setAnalyzing: (v: boolean) => void
  channel: any
  setChannel: (ch: any) => void
}

// ── Helpers ────────────────────────────────────────────────────────

const PHASE_LABELS: Record<string, string> = {
  exploration: 'Explorando datos del canal...',
  hypothesis: 'Formulando hipotesis...',
  recommendations: 'Generando recomendaciones...',
  done: 'Analisis completado',
}

const PHASE_ORDER = ['exploration', 'hypothesis', 'recommendations', 'done']

function getPhaseIndex(phase: string | null): number {
  if (!phase) return 0
  return PHASE_ORDER.indexOf(phase) >= 0 ? PHASE_ORDER.indexOf(phase) : 0
}

function confidenceColor(confidence: number): string {
  if (confidence >= 80) return 'bg-neon-cyan'
  if (confidence >= 60) return 'bg-neon-gold'
  if (confidence >= 40) return 'bg-yellow-500'
  return 'bg-neon-red'
}

function impactBadge(impact: string) {
  const map: Record<string, { label: string; cls: string }> = {
    alta: { label: 'ALTA', cls: 'bg-neon-red/20 text-neon-red border-neon-red/30' },
    media: { label: 'MEDIA', cls: 'bg-neon-gold/20 text-neon-gold border-neon-gold/30' },
    baja: { label: 'BAJA', cls: 'bg-gray-500/20 text-gray-400 border-gray-500/30' },
  }
  return map[impact] || map.baja
}

/** Reconstruct chat history from refined_versions array. */
function restoreHistoryFromVersions(versions: { triggered_by: string; explanation: string }[]): { role: string; content: string }[] {
  if (!versions || !versions.length) return []
  const history: { role: string; content: string }[] = []
  for (const v of versions) {
    if (v.triggered_by) history.push({ role: 'user', content: v.triggered_by })
    if (v.explanation) history.push({ role: 'assistant', content: v.explanation })
  }
  return history
}

// ── Sparkline mini component ───────────────────────────────────────

function MiniSparkline({ data, positive }: { data: number[]; positive: boolean }) {
  if (!data.length) return null
  const max = Math.max(...data, 1)
  const min = Math.min(...data)
  const range = max - min || 1
  const color = positive ? '#10b981' : '#ff3355'
  const h = 24
  const w = 60
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w
    const y = h - ((v - min) / range) * (h - 4) - 2
    return `${x.toFixed(0)},${y.toFixed(0)}`
  })
  return (
    <svg width={w} height={h} className="inline-block">
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// ── Sub-components ─────────────────────────────────────────────────

/** The 3-phase loading screen shown during analysis. */
function AnalysisLoadingScreen({
  phase, rawPatterns,
}: {
  phase: string | null
  rawPatterns: any | null
}) {
  const idx = getPhaseIndex(phase)
  const dots = PHASE_ORDER.slice(0, 4).map((p, i) => ({
    label: PHASE_LABELS[p],
    done: i < idx,
    active: i === idx,
  }))

  // Extract live discoveries from raw_patterns if available
  let discoveries: string[] = []
  if (rawPatterns) {
    const patterns = rawPatterns?.patterns || (Array.isArray(rawPatterns) ? rawPatterns : [])
    discoveries = patterns.slice(0, 5).map((p: any) => p.finding || p.name || '')
  }

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Phase stepper */}
      <div className="flex items-center justify-center gap-2 sm:gap-4">
        {dots.map((d, i) => (
          <div key={i} className="flex items-center gap-2">
            <div
              className={`w-3 h-3 rounded-full transition-all duration-500 ${
                d.done
                  ? 'bg-neon-cyan shadow-[0_0_10px_rgba(0,229,255,0.4)]'
                  : d.active
                  ? 'bg-neon-cyan animate-pulse'
                  : 'bg-dark-600'
              }`}
            />
            <span
              className={`text-xs hidden sm:inline ${
                d.active ? 'text-white' : d.done ? 'text-neon-cyan/70' : 'text-gray-600'
              }`}
            >
              {d.label}
            </span>
            {i < dots.length - 1 && (
              <div
                className={`h-px w-4 sm:w-8 transition-colors duration-500 ${
                  i < idx ? 'bg-neon-cyan/40' : 'bg-dark-600'
                }`}
              />
            )}
          </div>
        ))}
      </div>

      {/* Current phase label */}
      <p className="text-center text-gray-400 text-sm">
        {phase ? PHASE_LABELS[phase] || phase : 'Iniciando analisis...'}
      </p>

      {/* Progress bar */}
      <div className="w-full max-w-md mx-auto h-1 bg-dark-600 rounded-full overflow-hidden">
        <div
          className="h-full bg-gradient-to-r from-neon-cyan to-neon-purple rounded-full transition-all duration-700"
          style={{ width: `${((idx + 1) / 4) * 100}%` }}
        />
      </div>

      {/* Live discoveries */}
      {discoveries.length > 0 && (
        <div className="space-y-3 max-w-2xl mx-auto">
          <p className="text-xs text-gray-500 text-center">Hallazgos descubiertos:</p>
          {discoveries.map((d, i) => (
            <div
              key={i}
              className="glass rounded-lg p-3 border border-neon-cyan/10 animate-slide-up text-sm text-gray-300"
              style={{ animationDelay: `${i * 0.15}s` }}
            >
              <Sparkles size={12} className="inline text-neon-cyan mr-2" />
              {d}
            </div>
          ))}
        </div>
      )}

      {/* Spinner at bottom */}
      <div className="flex justify-center">
        <Loader2 size={20} className="animate-spin text-neon-cyan/60" />
      </div>
    </div>
  )
}

/** Metric chip with sparkline. */
function MetricChip({ metric }: { metric: KeyMetric }) {
  return (
    <div className="glass rounded-xl p-4 flex flex-col gap-2 min-w-[140px]">
      <span className="text-xs text-gray-500">{metric.label}</span>
      <span className="text-2xl font-semibold text-white tabular-nums font-mono">
        {metric.value}
      </span>
      <div className="flex items-center gap-2">
        <MiniSparkline data={metric.sparkline} positive={metric.delta_positive} />
        <span
          className={`text-xs font-medium ${
            metric.delta_positive ? 'text-green-400' : 'text-neon-red'
          }`}
        >
          {metric.delta}
        </span>
      </div>
    </div>
  )
}

/** A single recommendation card (collapsed or expanded). */
function RecommendationCard({
  rec,
  insightId,
  channelId,
  onApply,
  onDiscard,
  onRestore,
  applying,
  onValidate,
  validating,
  onRefine,
  refining,
  onApplyRefined,
}: {
  rec: InsightRecommendation
  insightId: number
  channelId: number
  onApply: (recId: string) => void
  onDiscard: (recId: string) => void
  onRestore: (recId: string) => void
  applying: boolean
  onValidate: (recId: string) => void
  validating: boolean
  onRefine: (recId: string, feedback: string, history: {role: string; content: string}[]) => void
  refining: boolean
  onApplyRefined: (recId: string, versionIndex: number) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const [copiedPrompt, setCopiedPrompt] = useState(false)
  const [refineMode, setRefineMode] = useState(false)
  const [refineInput, setRefineInput] = useState('')
  const [refineHistory, setRefineHistory] = useState<{role: string; content: string}[]>([])
  const [activeRefinedIndex, setActiveRefinedIndex] = useState<number | null>(null)
  const [applyingRefined, setApplyingRefined] = useState(false)
  const [resumeChat, setResumeChat] = useState(false) // re-enter chat from applied/discarded state
  const meta = getCategoryMeta(rec.category)

  const refinedVersions: (RefinedVersion & { _index: number })[] =
    (rec.refined_versions || []).map((v, i) => ({ ...v, _index: i }))

  // Validation state display
  const validation: ValidationResult | undefined = rec.validation
  const showValidationBadge = !!validation

  // Resume chat mode: skip discard/applied returns, show full card with chat open
  // (refineMode + resumeChat are set together in the onClick handlers below)

  // Discarded state
  if (!resumeChat && rec.discarded) {
    return (
      <div className="flex items-center gap-3 px-4 py-2 bg-dark-800/30 rounded-lg border border-dashed border-gray-700/50">
        <span className="text-xs text-gray-600">
          ✕ Descartada: <span className="text-gray-500">{rec.title}</span>
        </span>
        <button
          onClick={() => { setResumeChat(true); setRefineMode(true); setExpanded(true); setRefineHistory(restoreHistoryFromVersions(refinedVersions)) }}
          className="text-xs text-purple-400 hover:text-purple-300 transition-colors flex items-center gap-1"
          title="Comentar esta sugerencia con la IA"
        >
          <MessageSquare size={12} /> Comentar
        </button>
        <button
          onClick={() => onRestore(rec.id)}
          className="ml-auto text-xs text-gray-500 hover:text-gray-300 transition-colors"
        >
          Restaurar
        </button>
      </div>
    )
  }

  // Applied state
  if (!resumeChat && (rec.applied || applying)) {
    return (
      <div className="glass rounded-xl p-4 border border-green-500/20">
        <div className="flex items-center gap-3">
          <Check size={18} className="text-green-400 flex-shrink-0" />
          <div className="flex-1">
            <p className="text-sm font-medium text-green-300">{rec.title}</p>
            <p className="text-xs text-green-400/60">
              {applying ? 'Aplicando...' : 'Cambio aplicado al canal'}
            </p>
          </div>
          <button
            onClick={() => { setResumeChat(true); setRefineMode(true); setExpanded(true); setRefineHistory(restoreHistoryFromVersions(refinedVersions)) }}
            className="text-xs text-purple-400 hover:text-purple-300 transition-colors flex items-center gap-1"
            title="Comenta esta sugerencia con la IA aunque ya este aplicada"
          >
            <MessageSquare size={12} /> Comentar
          </button>
        </div>
      </div>
    )
  }

  const impact = impactBadge(rec.expected_impact)

  return (
    <div className="glass rounded-xl border border-surface-border overflow-hidden transition-all">
      {/* ── Collapsed row ── */}
      <div className="p-4">
        <div className="flex items-start gap-3">
          {/* Confidence bar */}
          <div className="flex-shrink-0 flex flex-col items-center gap-1">
            <div className="w-1.5 h-12 bg-dark-600 rounded-full overflow-hidden relative">
              <div
                className={`absolute bottom-0 w-full rounded-full transition-all ${confidenceColor(rec.confidence)}`}
                style={{ height: `${rec.confidence}%` }}
              />
            </div>
            <span className="text-[10px] text-gray-500 tabular-nums">{rec.confidence}%</span>
          </div>

          {/* Content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${meta.border} ${meta.bg} ${meta.color}`}>
                {meta.icon} {meta.label}
              </span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${impact.cls}`}>
                {impact.label}
              </span>
              {rec.requires_code && (
                <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-400">
                  Requiere codigo
                </span>
              )}
            </div>
            <p className="text-sm font-medium text-white leading-snug">{rec.title}</p>
            {!expanded && (
              <p className="text-xs text-gray-400 mt-1 line-clamp-2">{rec.detail}</p>
            )}
          </div>

          {/* Action buttons */}
          <div className="flex-shrink-0 flex items-center gap-1">
            <button
              onClick={() => setExpanded(!expanded)}
              className="p-1.5 rounded-lg text-gray-500 hover:text-white hover:bg-dark-600 transition-colors"
              title={expanded ? 'Colapsar' : 'Expandir'}
            >
              {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
            </button>
            <button
              onClick={() => onDiscard(rec.id)}
              className="p-1.5 rounded-lg text-gray-500 hover:text-neon-red hover:bg-neon-red/10 transition-colors"
              title="Descartar"
            >
              <X size={16} />
            </button>
            {/* Comentar button (available for all recommendation types) */}
            <button
              onClick={() => {
                const newMode = !refineMode
                setRefineMode(newMode)
                if (newMode) {
                  setExpanded(true)
                  // Restore chat history from existing refined_versions
                  const restored = restoreHistoryFromVersions(refinedVersions)
                  if (restored.length > 0) setRefineHistory(restored)
                }
              }}
              className={`px-2.5 py-1.5 text-xs rounded-lg transition-colors flex items-center gap-1 ${
                refineMode
                  ? 'bg-purple-500/20 text-purple-400 border border-purple-500/30'
                  : 'bg-purple-500/10 text-purple-400 border border-purple-500/20 hover:bg-purple-500/20'
              }`}
              title="Comenta esta sugerencia con la IA para discutirla o refinarla"
            >
              <MessageSquare size={12} />
              <span className="hidden sm:inline">Comentar</span>
            </button>
            {rec.requires_code ? (
              <>
                <button
                  onClick={() => {
                    if (rec.opencode_prompt) {
                      navigator.clipboard.writeText(rec.opencode_prompt)
                      setCopiedPrompt(true)
                      setTimeout(() => setCopiedPrompt(false), 2000)
                    }
                  }}
                  className={`px-2.5 py-1.5 text-xs rounded-lg transition-colors flex items-center gap-1 ${
                    copiedPrompt
                      ? 'bg-green-600/20 text-green-400 border border-green-500/30'
                      : 'bg-amber-500/10 text-amber-400 border border-amber-500/30 hover:bg-amber-500/20'
                  }`}
                >
                  {copiedPrompt ? (
                    <>
                      <Check size={12} /> Copiado
                    </>
                  ) : (
                    <>
                      <Copy size={12} /> Prompt
                    </>
                  )}
                </button>
                <button
                  onClick={() => onValidate(rec.id)}
                  disabled={validating}
                  className={`px-2.5 py-1.5 text-xs rounded-lg transition-colors flex items-center gap-1 ${
                    validating
                      ? 'bg-dark-600 text-gray-400 border border-dark-500 cursor-wait'
                      : validation
                      ? validation.status === 'resolved'
                        ? 'bg-green-600/20 text-green-400 border border-green-500/30 hover:bg-green-600/30'
                        : validation.status === 'partial'
                        ? 'bg-neon-gold/10 text-neon-gold border border-neon-gold/30 hover:bg-neon-gold/20'
                        : 'bg-neon-red/10 text-neon-red border border-neon-red/30 hover:bg-neon-red/20'
                      : 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/20'
                  }`}
                  title={validation ? `Validado: ${validation.summary}` : 'Validar si el cambio resolvio el problema'}
                >
                  {validating ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : validation?.status === 'resolved' ? (
                    <Check size={12} />
                  ) : validation?.status === 'partial' ? (
                    <AlertTriangle size={12} />
                  ) : validation?.status === 'not_resolved' ? (
                    <X size={12} />
                  ) : (
                    <Search size={12} />
                  )}
                  <span className="hidden sm:inline">
                    {validating ? 'Validando...' : validation ? 'Validado' : 'Validar'}
                  </span>
                </button>
              </>
            ) : (
              <button
                onClick={() => onApply(rec.id)}
                className="px-2.5 py-1.5 text-xs bg-neon-red/10 text-neon-red border border-neon-red/30 rounded-lg hover:bg-neon-red/20 transition-colors flex items-center gap-1"
              >
                <Check size={12} /> Aplicar
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ── Expanded detail ── */}
      {(expanded || refineMode) && (
        <div className="border-t border-surface-border px-4 py-4 space-y-4 bg-dark-800/30">
          {/* Validation result (if validated) */}
          {showValidationBadge && validation && (
            <div className={`rounded-lg p-3 border ${
              validation.status === 'resolved'
                ? 'bg-green-500/5 border-green-500/20'
                : validation.status === 'partial'
                ? 'bg-neon-gold/5 border-neon-gold/20'
                : 'bg-neon-red/5 border-neon-red/20'
            }`}>
              <div className="flex items-start gap-2">
                {validation.status === 'resolved' ? (
                  <Check size={16} className="text-green-400 mt-0.5 flex-shrink-0" />
                ) : validation.status === 'partial' ? (
                  <AlertTriangle size={16} className="text-neon-gold mt-0.5 flex-shrink-0" />
                ) : (
                  <X size={16} className="text-neon-red mt-0.5 flex-shrink-0" />
                )}
                <div className="flex-1 min-w-0">
                  <p className={`text-xs font-semibold ${
                    validation.status === 'resolved' ? 'text-green-400' :
                    validation.status === 'partial' ? 'text-neon-gold' : 'text-neon-red'
                  }`}>
                    {validation.status === 'resolved' ? '✅ Cambio validado — problema resuelto' :
                     validation.status === 'partial' ? '⚠️ Mejoria parcial detectada' :
                     '❌ Problema no resuelto'}
                  </p>
                  <p className="text-xs text-gray-400 mt-1">{validation.summary}</p>
                  {validation.evidence && validation.evidence.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {validation.evidence.map((e: string, i: number) => (
                        <p key={i} className="text-[11px] text-gray-500">• {e}</p>
                      ))}
                    </div>
                  )}
                  <p className="text-[10px] text-gray-600 mt-1">
                    Confianza: {validation.confidence}% · {new Date(validation.validated_at).toLocaleString('es-ES')}
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* ── Refine chat mode ── */}
          {refineMode && activeRefinedIndex !== null && onApplyRefined ? (
            <div className="space-y-3">
              {/* Already refined — show the refined version */}
              {refinedVersions.length > 0 && refinedVersions.filter(v => v._index === activeRefinedIndex).map(rv => {
                const hasConfigChanges = rv.revised_config_changes && Object.keys(rv.revised_config_changes).length > 0
                return (
                <div key={rv._index} className="space-y-3">
                  <div className={`flex items-start gap-2 rounded-lg p-3 border ${
                    rv.explanation?.startsWith('⚠️')
                      ? 'bg-amber-500/5 border-amber-500/20'
                      : 'bg-purple-500/5 border-purple-500/20'
                  }`}>
                    <MessageSquare size={14} className={rv.explanation?.startsWith('⚠️') ? 'text-amber-400 mt-0.5 flex-shrink-0' : 'text-purple-400 mt-0.5 flex-shrink-0'} />
                    <div className="flex-1 min-w-0">
                      <p className={`text-xs font-medium mb-1 ${rv.explanation?.startsWith('⚠️') ? 'text-amber-400' : 'text-purple-400'}`}>
                        {hasConfigChanges ? 'Version refinada' : 'Respuesta'}
                      </p>
                      <p className="text-xs text-gray-300">{rv.explanation}</p>
                      {hasConfigChanges && (
                        <div className="mt-2">
                          <p className="text-xs font-semibold text-green-400 mb-1">Cambios propuestos</p>
                          <div className="overflow-x-auto">
                            <table className="text-xs w-full">
                              <thead>
                                <tr className="text-gray-500 border-b border-surface-border">
                                  <th className="text-left py-1 pr-3">Config key</th>
                                  <th className="text-left py-1">Nuevo valor</th>
                                </tr>
                              </thead>
                              <tbody>
                                {Object.entries(rv.revised_config_changes).map(([k, v]) => (
                                  <tr key={k} className="border-b border-surface-border/30">
                                    <td className="py-1 pr-3 text-gray-400 font-mono">{k}</td>
                                    <td className="py-1 text-gray-200 font-mono">
                                      {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                      <div className="flex items-center gap-2 mt-3">
                        {hasConfigChanges && !rec.requires_code && (
                          <button
                            onClick={async () => {
                              setApplyingRefined(true)
                              try {
                                await onApplyRefined(rec.id, rv._index)
                              } finally {
                                setApplyingRefined(false)
                              }
                            }}
                            disabled={applyingRefined}
                            className="px-3 py-1.5 text-xs bg-green-600/20 text-green-400 border border-green-500/30 rounded-lg hover:bg-green-600/30 transition-colors flex items-center gap-1"
                          >
                            {applyingRefined ? (
                              <Loader2 size={12} className="animate-spin" />
                            ) : (
                              <Check size={12} />
                            )}
                            Aplicar esta version
                          </button>
                        )}
                        <button
                          onClick={() => setActiveRefinedIndex(null)}
                          className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-300 transition-colors flex items-center gap-1"
                        >
                          <RotateCcw size={12} /> Seguir comentando
                        </button>
                        <button
                          onClick={() => { setRefineMode(false); setRefineInput(''); setRefineHistory([]); setActiveRefinedIndex(null); setResumeChat(false) }}
                          className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-400 transition-colors"
                        >
                          Cancelar
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              )})}
            </div>
          ) : refineMode ? (
            <div className="space-y-4">
              {/* Original suggestion as first bubble */}
              <div className="flex items-start gap-2">
                <div className="w-6 h-6 rounded-full bg-neon-cyan/10 border border-neon-cyan/20 flex items-center justify-center flex-shrink-0">
                  <Sparkles size={10} className="text-neon-cyan" />
                </div>
                <div className="bg-dark-900 rounded-lg p-3 border border-dark-600 flex-1">
                  <p className="text-[11px] text-gray-500 mb-1">Sugerencia original</p>
                  <p className="text-xs text-gray-300">{rec.detail.substring(0, 300)}...</p>
                  {rec.config_changes && Object.keys(rec.config_changes).length > 0 && (
                    <div className="mt-2">
                      <p className="text-[10px] text-gray-500 mb-1">Cambios propuestos:</p>
                      {Object.entries(rec.config_changes).map(([k, v]) => (
                        <span key={k} className="text-[10px] bg-dark-700 px-1.5 py-0.5 rounded mr-1">
                          {k}: <span className="text-gray-300">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Refinement history */}
              {refineHistory.map((msg, i) => (
                <div key={i} className={`flex items-start gap-2 ${msg.role === 'assistant' ? '' : 'justify-end'}`}>
                  {msg.role === 'assistant' && (
                    <div className="w-6 h-6 rounded-full bg-purple-500/10 border border-purple-500/20 flex items-center justify-center flex-shrink-0">
                      <MessageSquare size={10} className="text-purple-400" />
                    </div>
                  )}
                  <div className={`rounded-lg p-3 border max-w-[85%] ${
                    msg.role === 'assistant'
                      ? 'bg-purple-500/5 border-purple-500/20'
                      : 'bg-dark-900 border-dark-600'
                  }`}>
                    <p className="text-[11px] text-gray-500 mb-1">
                      {msg.role === 'assistant' ? 'Respuesta' : 'Tu'}
                    </p>
                    <p className="text-xs text-gray-300 whitespace-pre-wrap">{msg.content}</p>
                  </div>
                  {msg.role === 'user' && (
                    <div className="w-6 h-6 rounded-full bg-dark-600 border border-dark-500 flex items-center justify-center flex-shrink-0">
                      <span className="text-[10px] text-gray-400">Tu</span>
                    </div>
                  )}
                </div>
              ))}

              {/* Latest refine response (if we just got one) */}
              {refinedVersions.length > 0 && !activeRefinedIndex && (() => {
                const latest = refinedVersions[refinedVersions.length - 1]
                const hasChanges = latest.revised_config_changes && Object.keys(latest.revised_config_changes).length > 0
                return (
                <div className="flex items-start gap-2">
                  <div className="w-6 h-6 rounded-full bg-purple-500/10 border border-purple-500/20 flex items-center justify-center flex-shrink-0">
                    <MessageSquare size={10} className="text-purple-400" />
                  </div>
                  <div className="bg-purple-500/5 rounded-lg p-3 border border-purple-500/20 flex-1">
                    <p className="text-[11px] text-gray-500 mb-1">Respuesta</p>
                    <p className="text-xs text-gray-300">{latest.explanation}</p>
                    {hasChanges && (
                      <div className="mt-2">
                        <p className="text-[10px] text-gray-500 mb-1">Cambios revisados:</p>
                        {Object.entries(latest.revised_config_changes).map(([k, v]) => (
                          <span key={k} className="text-[10px] bg-dark-700 px-1.5 py-0.5 rounded mr-1">
                            {k}: <span className="text-gray-300">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )})()}

              {/* Input for refinement */}
              <div className="flex items-start gap-2">
                <textarea
                  value={refineInput}
                  onChange={e => setRefineInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      if (refineInput.trim() && !refining) {
                        const msg = refineInput.trim()
                        setRefineHistory(prev => [...prev, { role: 'user', content: msg }])
                        setRefineInput('')
                        onRefine(rec.id, msg, refineHistory)
                      }
                    }
                  }}
                  placeholder="Escribe tu feedback para refinar el cambio... (Enter para enviar)"
                  className="flex-1 bg-dark-900 border border-dark-600 rounded-lg p-2 text-xs text-gray-300 placeholder-gray-600 resize-none focus:border-purple-500/50 focus:outline-none"
                  rows={2}
                  disabled={refining}
                />
                <button
                  onClick={() => {
                    if (refineInput.trim() && !refining) {
                      const msg = refineInput.trim()
                      setRefineHistory(prev => [...prev, { role: 'user', content: msg }])
                      setRefineInput('')
                      onRefine(rec.id, msg, refineHistory)
                    }
                  }}
                  disabled={!refineInput.trim() || refining}
                  className="p-2 bg-purple-500/20 text-purple-400 border border-purple-500/30 rounded-lg hover:bg-purple-500/30 transition-colors disabled:opacity-40"
                >
                  {refining ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Send size={14} />
                  )}
                </button>
              </div>

              {/* Cancel refine */}
              <button
                onClick={() => { setRefineMode(false); setRefineInput(''); setRefineHistory([]); setActiveRefinedIndex(null); setResumeChat(false) }}
                className="text-xs text-gray-500 hover:text-gray-400 transition-colors flex items-center gap-1"
              >
                <X size={12} /> Salir del modo refinar
              </button>
            </div>
          ) : (
            <>
              {/* ── Original expanded content (not in refine mode) ── */}
          {/* Full analysis */}
          <div>
            <p className="text-xs font-semibold text-gray-400 mb-1">Analisis completo</p>
            <p className="text-sm text-gray-300 leading-relaxed">{rec.detail}</p>
            {rec.rationale_brief && (
              <p className="text-xs text-gray-500 mt-1 italic">{rec.rationale_brief}</p>
            )}
          </div>

          {/* Data cited */}
          {rec.data_cited && Object.keys(rec.data_cited).length > 0 && (
            <div>
              <p className="text-xs font-semibold text-gray-400 mb-1">Datos citados</p>
              <div className="space-y-1">
                {Object.entries(rec.data_cited).map(([k, v]) => (
                  <div key={k} className="flex items-center gap-2 text-xs">
                    <span className="text-gray-500">{k}:</span>
                    <span className="text-gray-300 font-mono">{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Config changes (if not code only) */}
          {!rec.requires_code && rec.config_changes && Object.keys(rec.config_changes).length > 0 && (
            <div>
              <p className="text-xs font-semibold text-green-400 mb-1">Cambios que se aplicaran</p>
              <div className="overflow-x-auto">
                <table className="text-xs w-full">
                  <thead>
                    <tr className="text-gray-500 border-b border-surface-border">
                      <th className="text-left py-1 pr-3">Config key</th>
                      <th className="text-left py-1 pr-3">Valor actual</th>
                      <th className="text-left py-1">Nuevo valor</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(rec.config_changes).map(([k, v]) => (
                      <tr key={k} className="border-b border-surface-border/30">
                        <td className="py-1 pr-3 text-gray-400 font-mono">{k}</td>
                        <td className="py-1 pr-3 text-gray-600">(se sobreescribe)</td>
                        <td className="py-1 text-gray-200 font-mono">
                          {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* OpenCode prompt (if requires code) */}
          {rec.requires_code && rec.opencode_prompt && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <p className="text-xs font-semibold text-amber-400">Prompt para OpenCode</p>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(rec.opencode_prompt!)
                    setCopiedPrompt(true)
                    setTimeout(() => setCopiedPrompt(false), 2000)
                  }}
                  className={`text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1 transition-colors ${
                    copiedPrompt
                      ? 'bg-green-600/20 text-green-400'
                      : 'bg-amber-500/10 text-amber-400 hover:bg-amber-500/20'
                  }`}
                >
                  {copiedPrompt ? (
                    <>
                      <Check size={10} /> Copiado
                    </>
                  ) : (
                    <>
                      <Copy size={10} /> Copiar
                    </>
                  )}
                </button>
              </div>
              <div className="bg-dark-900 rounded-lg p-3 border border-amber-500/20 overflow-auto max-h-40">
                <pre className="text-xs text-gray-300 whitespace-pre-wrap font-mono">
                  {rec.opencode_prompt}
                </pre>
              </div>
            </div>
          )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────

export default function InsightsTab({
  channelId, insights, setInsights, analyzing, setAnalyzing,
  channel, setChannel,
}: Props) {
  const [loading, setLoading] = useState(!insights)
  const [error, setError] = useState<string | null>(null)
  const [applyingIds, setApplyingIds] = useState<Set<string>>(new Set())
  const [validatingIds, setValidatingIds] = useState<Set<string>>(new Set())
  const [refiningIds, setRefiningIds] = useState<Set<string>>(new Set())
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const pollCountRef = useRef(0)
  // Ref-backed phase for the stepper — read directly in render, updated
  // unconditionally on every poll.  Using renderTick as a force-update
  // ensures the component always re-renders after each poll regardless
  // of whether React detects the ref change.
  const phaseRef = useRef<string>('exploration')
  const [renderTick, setRenderTick] = useState(0)

  // ── Load / poll ──────────────────────────────────────────────
  const loadInsight = useCallback(async (opts?: { signal?: AbortSignal }) => {
    try {
      const data = await api.getLatestInsight(channelId, opts?.signal)
      if (opts?.signal?.aborted) return
      setInsights(data)
      // Unconditionally update phase and force re-render so the
      // stepper advances immediately on every poll.
      phaseRef.current = data.current_phase || 'exploration'
      setRenderTick(t => t + 1)
      setError(null)
      if (data.status === 'completed' || data.status === 'failed') {
        setAnalyzing(false)
      }
      return data
    } catch (e: any) {
      if (e.name === 'AbortError') return
      if (e.message?.includes('404') || e.message?.includes('No analysis')) {
        setError(null) // not an error, just no analysis yet
      } else {
        setError(e.message)
      }
      return null
    } finally {
      if (!opts?.signal?.aborted) setLoading(false)
    }
  }, [channelId, setInsights, setAnalyzing])

  // Initial load
  useEffect(() => {
    setLoading(true)
    // Abort any in-flight request from previous mount
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller
    pollCountRef.current = 0
    loadInsight({ signal: controller.signal })
    return () => {
      controller.abort()
    }
  }, [channelId])

  // Poll while processing — with timeout guard
  useEffect(() => {
    if (insights?.status === 'processing' || analyzing) {
      pollRef.current = setInterval(() => {
        pollCountRef.current++
        // After 200 polls (10 min), give up and show timeout
        // Analysis typically takes 3-6 min for 3 LLM passes; 10 min is a safe ceiling.
        if (pollCountRef.current > 200) {
          if (pollRef.current) clearInterval(pollRef.current)
          pollRef.current = null
          setAnalyzing(false)
          setError('El analisis esta tardando mas de lo esperado. El servidor de IA puede estar ocupado — puedes cerrar esta ventana, el analisis seguira ejecutandose en segundo plano.')
          return
        }
        // Use a fresh AbortController per poll so we don't cancel the wrong one
        if (abortRef.current) abortRef.current.abort()
        const controller = new AbortController()
        abortRef.current = controller
        loadInsight({ signal: controller.signal })
      }, 3000)
      return () => {
        if (pollRef.current) clearInterval(pollRef.current)
        if (abortRef.current) abortRef.current.abort()
      }
    } else {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      pollCountRef.current = 0
    }
  }, [insights?.status, analyzing, loadInsight])

  // ── Actions ──────────────────────────────────────────────────
  async function handleGenerate() {
    setAnalyzing(true)
    phaseRef.current = 'exploration'
    setRenderTick(0)
    // Clear previous recommendations so the loading screen renders
    // (hasExistingData would otherwise hide it) — new data replaces on completion.
    if (!insights) {
      setInsights(null)
    } else {
      setInsights({
        ...insights,
        status: 'processing',
        current_phase: 'exploration',
        insights_json: { ...insights.insights_json, recommendations: [] },
      })
    }
    try {
      const { insight_id } = await api.analyzeChannel(channelId)
      // Start polling immediately; use a placeholder if no existing insights
      if (!insights) {
        setInsights({
          id: insight_id, channel_id: channelId,
          status: 'processing', current_phase: 'exploration',
          insights_json: { analysis_summary: '', recommendations: [] },
          raw_patterns: null, raw_hypotheses: null,
          error_msg: null, model_used: null,
          tokens_input: 0, tokens_output: 0, generation_time_ms: 0,
          generated_at: null, applied_at: null, applied_by: null,
        })
      }
    } catch (e: any) {
      setError(e.message)
      setAnalyzing(false)
    }
  }

  async function handleApply(recId: string) {
    if (!insights) return
    setApplyingIds(prev => new Set(prev).add(recId))
    try {
      await api.applyInsight(channelId, insights.id, recId)
      // Reload channel to get updated config
      const ch = await api.getChannel(channelId)
      setChannel(ch)
      // Mark as applied in local state
      const recs = insights.insights_json.recommendations.map(r =>
        r.id === recId ? { ...r, applied: true } : r
      )
      setInsights({
        ...insights,
        insights_json: { ...insights.insights_json, recommendations: recs },
      })
    } catch (e: any) {
      console.error('Apply failed:', e)
    }
    setApplyingIds(prev => {
      const next = new Set(prev)
      next.delete(recId)
      return next
    })
  }

  async function handleDiscard(recId: string) {
    if (!insights) return
    // Optimistic update
    const prevInsights = insights
    const recs = insights.insights_json.recommendations.map(r =>
      r.id === recId ? { ...r, discarded: true } : r
    )
    setInsights({
      ...insights,
      insights_json: { ...insights.insights_json, recommendations: recs },
    })
    try {
      await api.discardInsight(channelId, insights.id, recId, true)
    } catch (e: any) {
      console.error('Discard failed:', e)
      // Revert on failure
      setInsights(prevInsights)
    }
  }

  async function handleRestore(recId: string) {
    if (!insights) return
    // Optimistic update
    const prevInsights = insights
    const recs = insights.insights_json.recommendations.map(r =>
      r.id === recId ? { ...r, discarded: false } : r
    )
    setInsights({
      ...insights,
      insights_json: { ...insights.insights_json, recommendations: recs },
    })
    try {
      await api.discardInsight(channelId, insights.id, recId, false)
    } catch (e: any) {
      console.error('Restore failed:', e)
      // Revert on failure
      setInsights(prevInsights)
    }
  }

  // ── Validate (code-change recommendations) ─────────────────────
  async function handleValidate(recId: string) {
    if (!insights) return
    setValidatingIds(prev => new Set(prev).add(recId))
    try {
      const { validation } = await api.validateInsight(channelId, insights.id, recId)
      const recs = insights.insights_json.recommendations.map(r =>
        r.id === recId ? { ...r, validation: validation as any } : r
      )
      setInsights({
        ...insights,
        insights_json: { ...insights.insights_json, recommendations: recs },
      })
    } catch (e: any) {
      console.error('Validate failed:', e)
    }
    setValidatingIds(prev => {
      const next = new Set(prev)
      next.delete(recId)
      return next
    })
  }

  // ── Refine (config-change recommendations) ─────────────────────
  async function handleRefine(recId: string, feedback: string, history: {role: string; content: string}[]) {
    if (!insights) return
    setRefiningIds(prev => new Set(prev).add(recId))
    try {
      const result = await api.refineInsight(channelId, insights.id, recId, feedback, history)
      if (result.cannot_fulfill) {
        // Show the cannot-fulfill reason as a message
        const recs = insights.insights_json.recommendations.map(r => {
          if (r.id !== recId) return r
          const existing = r.refined_versions || []
          return {
            ...r,
            refined_versions: [...existing, {
              revised_config_changes: {},
              explanation: `⚠️ ${result.cannot_fulfill_reason}`,
              triggered_by: feedback,
              refined_at: new Date().toISOString(),
            }],
          }
        })
        setInsights({
          ...insights,
          insights_json: { ...insights.insights_json, recommendations: recs },
        })
      } else {
        const recs = insights.insights_json.recommendations.map(r => {
          if (r.id !== recId) return r
          const existing = r.refined_versions || []
          return {
            ...r,
            refined_versions: [...existing, {
              revised_config_changes: result.revised_config_changes,
              explanation: result.explanation,
              triggered_by: feedback,
              refined_at: result.refined_at || new Date().toISOString(),
            }],
          }
        })
        setInsights({
          ...insights,
          insights_json: { ...insights.insights_json, recommendations: recs },
        })
      }
    } catch (e: any) {
      console.error('Refine failed:', e)
    }
    setRefiningIds(prev => {
      const next = new Set(prev)
      next.delete(recId)
      return next
    })
  }

  // ── Apply refined version ─────────────────────────────────────
  async function handleApplyRefined(recId: string, versionIndex: number) {
    if (!insights) return
    setApplyingIds(prev => new Set(prev).add(recId))
    try {
      await api.applyInsight(channelId, insights.id, recId, versionIndex)
      const ch = await api.getChannel(channelId)
      setChannel(ch)
      const recs = insights.insights_json.recommendations.map(r =>
        r.id === recId ? { ...r, applied: true } : r
      )
      setInsights({
        ...insights,
        insights_json: { ...insights.insights_json, recommendations: recs },
      })
    } catch (e: any) {
      console.error('Apply refined failed:', e)
    }
    setApplyingIds(prev => {
      const next = new Set(prev)
      next.delete(recId)
      return next
    })
  }

  // ── Render states ────────────────────────────────────────────

  const hasExistingData = (insights?.insights_json?.recommendations?.length ?? 0) > 0

  // Immersive loading screen during analysis (only when no existing data)
  if ((insights?.status === 'processing' || analyzing) && !hasExistingData) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Brain size={22} className="text-neon-cyan" />
            <div>
              <h2 className="text-lg font-semibold text-white">Insights AI</h2>
              <p className="text-xs text-gray-500">Analizando datos del canal...</p>
            </div>
          </div>
        </div>
        <AnalysisLoadingScreen
          phase={phaseRef.current}
          rawPatterns={insights?.raw_patterns}
        />
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 size={24} className="animate-spin text-neon-cyan/60" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-3">
        <AlertCircle size={32} className="text-neon-red/60" />
        <p className="text-sm text-gray-400">{error}</p>
        <button
          onClick={() => { setError(null); loadInsight() }}
          className="text-xs text-neon-cyan hover:underline"
        >
          Reintentar
        </button>
      </div>
    )
  }

  if (!insights || insights.status === 'failed') {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-4">
        <Brain size={40} className="text-gray-700" />
        <p className="text-sm text-gray-500">
          {insights?.status === 'failed'
            ? `Error: ${insights.error_msg || 'Analisis fallido'}`
            : 'No hay analisis disponible para este canal'}
        </p>
        <button
          onClick={handleGenerate}
          className="px-4 py-2 bg-neon-red text-white rounded-lg text-sm hover:bg-neon-red/80 transition-colors flex items-center gap-2"
        >
          <Sparkles size={14} />
          Generar analisis
        </button>
      </div>
    )
  }

  // ── Completed state ──────────────────────────────────────────
  const data = insights.insights_json
  const summary = data?.analysis_summary || ''
  const metrics = data?.key_metrics || []
  const recs = data?.recommendations || []

  // Group recommendations by category
  const grouped: Record<string, InsightRecommendation[]> = {}
  for (const r of recs) {
    (grouped[r.category] ||= []).push(r)
  }

  // Sort categories by priority: errores first (fixes), then duracion, hora, keywords, contenido
  const categoryOrder: InsightCategory[] = [
    'errores', 'duracion', 'hora_publicacion', 'keywords', 'contenido',
  ]

  // Count discarded and applied
  const discardedIds = new Set(recs.filter(r => r.discarded).map(r => r.id))
  const appliedIds = new Set(recs.filter(r => r.applied).map(r => r.id))

  return (
    <div className="space-y-6">
      {/* ── Enrichment banner (when re-analyzing with existing data) ── */}
      {analyzing && (
        <div className="glass rounded-xl p-4 border border-neon-cyan/30 bg-neon-cyan/5 animate-pulse">
          <div className="flex items-center gap-3">
            <Loader2 size={18} className="animate-spin text-neon-cyan" />
            <div>
              <p className="text-sm font-medium text-neon-cyan">Enriqueciendo analisis...</p>
              <p className="text-xs text-gray-400 mt-0.5">
                El nuevo analisis se esta generando con los datos actualizados.
                Las recomendaciones activas se mantienen visibles.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* ── Header ── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Brain size={22} className="text-neon-cyan" />
          <div>
            <h2 className="text-lg font-semibold text-white">Insights AI</h2>
            {insights.generated_at && (
              <p className="text-xs text-gray-500">
                Analisis del{' '}
                {new Date(insights.generated_at).toLocaleDateString('es-ES', {
                  day: 'numeric', month: 'short', year: 'numeric',
                  hour: '2-digit', minute: '2-digit',
                })}
                {insights.model_used && (
                  <span className="ml-2 text-gray-600">
                    · {insights.model_used} · {(insights.tokens_input + insights.tokens_output).toLocaleString()} tokens
                  </span>
                )}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* Health score */}
          {data?.health_score != null && (
            <div className="flex items-center gap-2 glass rounded-lg px-3 py-1.5">
              <span className="text-xs text-gray-500">Salud del canal</span>
              <span
                className={`text-sm font-semibold tabular-nums ${
                  (data.health_score || 0) >= 70
                    ? 'text-green-400'
                    : (data.health_score || 0) >= 40
                    ? 'text-neon-gold'
                    : 'text-neon-red'
                }`}
              >
                {data.health_score}/100
              </span>
            </div>
          )}
          <button
            onClick={handleGenerate}
            className="px-3 py-1.5 bg-neon-red text-white rounded-lg text-xs hover:bg-neon-red/80 transition-colors flex items-center gap-1.5"
          >
            <Sparkles size={12} /> Enriquecer analisis
          </button>
        </div>
      </div>

      {/* ── Summary (AI conversational) ── */}
      <div className="glass rounded-xl p-5 border border-neon-cyan/10">
        <div className="flex items-start gap-3">
          <div className="flex-shrink-0 w-8 h-8 rounded-full bg-neon-cyan/10 border border-neon-cyan/20 flex items-center justify-center">
            <Sparkles size={14} className="text-neon-cyan" />
          </div>
          <div>
            <p className="text-xs text-neon-cyan/60 mb-2">Asistente de IA</p>
            <p className="text-sm text-gray-300 leading-relaxed whitespace-pre-line">{summary}</p>
          </div>
        </div>
      </div>

      {/* ── Key metrics ── */}
      {metrics.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {metrics.map((m, i) => (
            <MetricChip key={i} metric={m} />
          ))}
        </div>
      )}

      {/* ── Recommendations by category ── */}
      <div className="space-y-6">
        {categoryOrder.map(cat => {
          const catRecs = grouped[cat]
          if (!catRecs?.length) return null
          const meta = getCategoryMeta(cat)
          const activeRecs = catRecs.filter(r => !r.discarded)
          const discardedRecs = catRecs.filter(r => r.discarded)
          return (
            <div key={cat}>
              {/* Category header */}
              <div className="flex items-center gap-3 mb-3">
                <div className={`w-1 h-5 rounded-full ${meta.color.replace('text-', 'bg-')}`} />
                <h3 className="text-sm font-semibold text-gray-300">
                  {meta.icon} {meta.label}
                </h3>
                <span className="text-xs text-gray-500">
                  {activeRecs.length} recomendacion{activeRecs.length !== 1 ? 'es' : ''}
                </span>
              </div>

              {/* Active cards */}
              {activeRecs.length > 0 && (
                <div className="space-y-2">
                  {activeRecs.map(r => (
                    <RecommendationCard
                      key={r.id}
                      rec={r}
                      insightId={insights.id}
                      channelId={channelId}
                      onApply={handleApply}
                      onDiscard={handleDiscard}
                      onRestore={handleRestore}
                      applying={applyingIds.has(r.id)}
                      onValidate={handleValidate}
                      validating={validatingIds.has(r.id)}
                      onRefine={handleRefine}
                      refining={refiningIds.has(r.id)}
                      onApplyRefined={handleApplyRefined}
                    />
                  ))}
                </div>
              )}

              {/* Discarded cards (collapsed) */}
              {discardedRecs.length > 0 && (
                <div className="space-y-1 mt-2">
                  {discardedRecs.map(r => (
                    <RecommendationCard
                      key={r.id}
                      rec={r}
                      insightId={insights.id}
                      channelId={channelId}
                      onApply={handleApply}
                      onDiscard={handleDiscard}
                      onRestore={handleRestore}
                      applying={false}
                      onValidate={handleValidate}
                      validating={false}
                      onRefine={handleRefine}
                      refining={false}
                      onApplyRefined={handleApplyRefined}
                    />
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
