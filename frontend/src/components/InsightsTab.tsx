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
import type { ChannelInsight, InsightRecommendation, InsightCategory, KeyMetric } from '../types/channel'
import { getCategoryMeta } from '../types/channel'
import {
  Brain, Clock, Loader2, AlertCircle, ChevronDown, ChevronUp,
  Check, X, Copy, ExternalLink, TrendingUp, Zap, BarChart3,
  Eye, EyeOff, Sparkles,
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
}: {
  rec: InsightRecommendation
  insightId: number
  channelId: number
  onApply: (recId: string) => void
  onDiscard: (recId: string) => void
  onRestore: (recId: string) => void
  applying: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const [copiedPrompt, setCopiedPrompt] = useState(false)
  const meta = getCategoryMeta(rec.category)

  // Discarded state
  if (rec.discarded) {
    return (
      <div className="flex items-center gap-3 px-4 py-2 bg-dark-800/30 rounded-lg border border-dashed border-gray-700/50">
        <span className="text-xs text-gray-600">
          ✕ Descartada: <span className="text-gray-500">{rec.title}</span>
        </span>
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
  if (rec.applied || applying) {
    return (
      <div className="glass rounded-xl p-4 border border-green-500/20">
        <div className="flex items-center gap-3">
          <Check size={18} className="text-green-400 flex-shrink-0" />
          <div>
            <p className="text-sm font-medium text-green-300">{rec.title}</p>
            <p className="text-xs text-green-400/60">
              {applying ? 'Aplicando...' : 'Cambio aplicado al canal'}
            </p>
          </div>
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
            {rec.requires_code ? (
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
      {expanded && (
        <div className="border-t border-surface-border px-4 py-4 space-y-4 bg-dark-800/30">
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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Load / poll ──────────────────────────────────────────────
  const loadInsight = useCallback(async () => {
    try {
      const data = await api.getLatestInsight(channelId)
      setInsights(data)
      setError(null)
      if (data.status === 'completed' || data.status === 'failed') {
        setAnalyzing(false)
      }
    } catch (e: any) {
      if (e.message?.includes('404') || e.message?.includes('No analysis')) {
        setError(null) // not an error, just no analysis yet
      } else {
        setError(e.message)
      }
    }
    setLoading(false)
  }, [channelId, setInsights, setAnalyzing])

  // Initial load
  useEffect(() => {
    setLoading(true)
    loadInsight()
  }, [channelId])

  // Poll while processing
  useEffect(() => {
    if (insights?.status === 'processing' || analyzing) {
      pollRef.current = setInterval(loadInsight, 3000)
      return () => {
        if (pollRef.current) clearInterval(pollRef.current)
      }
    } else {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [insights?.status, analyzing, loadInsight])

  // ── Actions ──────────────────────────────────────────────────
  async function handleGenerate() {
    setAnalyzing(true)
    setInsights(null)
    try {
      const { insight_id } = await api.analyzeChannel(channelId)
      // Start polling immediately
      setInsights({
        id: insight_id, channel_id: channelId,
        status: 'processing', current_phase: 'exploration',
        insights_json: { analysis_summary: '', recommendations: [] },
        raw_patterns: null, raw_hypotheses: null,
        error_msg: null, model_used: null,
        tokens_input: 0, tokens_output: 0, generation_time_ms: 0,
        generated_at: null, applied_at: null, applied_by: null,
      })
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

  function handleDiscard(recId: string) {
    if (!insights) return
    const recs = insights.insights_json.recommendations.map(r =>
      r.id === recId ? { ...r, discarded: true } : r
    )
    setInsights({
      ...insights,
      insights_json: { ...insights.insights_json, recommendations: recs },
    })
  }

  function handleRestore(recId: string) {
    if (!insights) return
    const recs = insights.insights_json.recommendations.map(r =>
      r.id === recId ? { ...r, discarded: false } : r
    )
    setInsights({
      ...insights,
      insights_json: { ...insights.insights_json, recommendations: recs },
    })
  }

  // ── Render states ────────────────────────────────────────────

  // Immersive loading screen during analysis
  if (insights?.status === 'processing' || analyzing) {
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
          phase={insights?.current_phase || 'exploration'}
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
            <Sparkles size={12} /> Generar nuevo analisis
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
