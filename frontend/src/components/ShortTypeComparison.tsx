/** v2: Short Type Comparison — Native vs Clip analytics.
 *
 *  Shows aggregated performance metrics for native and clip shorts
 *  side-by-side, with ratio trend and comparison indicators.
 */
import { useEffect, useState, useCallback } from 'react'
import { api } from '../lib/api'
import {
  BarChart3, TrendingUp, Users, Eye, Clock, AlertCircle,
  Loader2, Film, Smartphone, Activity,
} from 'lucide-react'

// ── Types ──────────────────────────────────────────────────────────

interface ShortTypeStats {
  total_shorts: number
  avg_views: number
  avg_likes: number
  total_views: number
  total_likes: number
  avg_view_duration: number
  total_subs_gained: number
  subs_per_short: number
  subs_per_view_avg: number
}

interface TypeComparisonData {
  native: ShortTypeStats
  clip: ShortTypeStats
  native_pct: number
  total_shorts: number
  days: number
  comparison: {
    views_ratio: number | null
    subs_per_short_ratio: number | null
    retention_ratio: number | null
  }
}

// ── Props ───────────────────────────────────────────────────────────

interface Props {
  channelId: number
}

// ── Helpers ─────────────────────────────────────────────────────────

const DAY_OPTIONS = [7, 14, 30, 60, 90]

function fmtNum(n: number | null | undefined): string {
  if (n == null || n === 0) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return Math.round(n).toString()
}

function fmtSec(sec: number): string {
  if (!sec) return '0s'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return m > 0 ? `${m}m${s}s` : `${s}s`
}

function fmtRatio(ratio: number | null): string {
  if (ratio == null) return '—'
  if (ratio > 1) return `${ratio.toFixed(1)}x ↑`
  if (ratio < 1) return `${(1 / ratio).toFixed(1)}x ↓`
  return '1.0x'
}

function ratioColor(ratio: number | null, preferNative: boolean = true): string {
  if (ratio == null) return 'text-slate-400'
  const winner = preferNative ? (ratio > 1) : (ratio < 1)
  if (Math.abs((ratio || 1) - 1) < 1.1) return 'text-yellow-500'
  return winner ? 'text-emerald-400' : 'text-rose-400'
}

// ── Metric chip ─────────────────────────────────────────────────────

function MetricChip({ label, value, sub, icon: Icon, highlight }: {
  label: string; value: string; sub?: string; icon: any; highlight?: boolean
}) {
  return (
    <div className={`flex items-center gap-3 p-3 rounded-xl border
      ${highlight ? 'border-blue-500/30 bg-blue-500/5' : 'border-white/5 bg-white/[0.02]'}`}>
      <Icon className="w-5 h-5 text-slate-500 shrink-0" />
      <div className="min-w-0">
        <div className="text-xs text-slate-500">{label}</div>
        <div className="font-mono text-sm text-white tabular-nums">{value}</div>
        {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
      </div>
    </div>
  )
}

// ── Column card ─────────────────────────────────────────────────────

function TypeColumn({ label, data, icon: Icon, colorClass }: {
  label: string; data: ShortTypeStats; icon: any; colorClass: string
}) {
  return (
    <div className={`flex-1 rounded-2xl border border-white/5 bg-white/[0.02] p-4 ${colorClass}`}>
      <div className="flex items-center gap-2 mb-4">
        <Icon className="w-5 h-5 text-slate-400" />
        <h3 className="text-sm font-semibold text-white">{label}</h3>
        <span className="ml-auto text-xs text-slate-500 font-mono">
          {data.total_shorts} shorts
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <MetricChip label="Vistas avg" value={fmtNum(data.avg_views)} icon={Eye} />
        <MetricChip label="Likes avg" value={fmtNum(data.avg_likes)} icon={Activity} />
        <MetricChip label="Retención" value={fmtSec(data.avg_view_duration)} icon={Clock} />
        <MetricChip
          label="Subs/short"
          value={data.subs_per_short.toFixed(1)}
          sub={data.total_subs_gained > 0 ? `${data.total_subs_gained} total` : undefined}
          icon={Users}
          highlight={(data.subs_per_short || 0) > 0}
        />
      </div>
    </div>
  )
}

// ── Main component ──────────────────────────────────────────────────

export default function ShortTypeComparison({ channelId }: Props) {
  const [data, setData] = useState<TypeComparisonData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState(30)
  const [refreshing, setRefreshing] = useState(false)

  const fetchData = useCallback(async (d: number = days) => {
    setRefreshing(true)
    setError(null)
    try {
      const res = await api.getShortTypeComparison(channelId, d)
      setData(res as TypeComparisonData)
    } catch (e: any) {
      setError(e?.message || 'Failed to load comparison data')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [channelId, days])

  useEffect(() => { fetchData() }, [fetchData])

  // ── Loading state ──────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="rounded-2xl border border-white/5 bg-white/[0.02] p-6">
        <div className="flex items-center gap-3 text-slate-400">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="text-sm">Cargando comparativa de shorts...</span>
        </div>
      </div>
    )
  }

  // ── Error state ────────────────────────────────────────────────────
  if (error || !data) {
    return (
      <div className="rounded-2xl border border-rose-500/20 bg-rose-500/5 p-6">
        <div className="flex items-center gap-3 text-rose-400">
          <AlertCircle className="w-5 h-5" />
          <span className="text-sm">{error || 'No data available'}</span>
        </div>
      </div>
    )
  }

  // ── No data state ──────────────────────────────────────────────────
  if (data.total_shorts === 0) {
    return (
      <div className="rounded-2xl border border-white/5 bg-white/[0.02] p-6">
        <div className="flex items-center gap-3 text-slate-500">
          <BarChart3 className="w-5 h-5" />
          <span className="text-sm">No hay shorts publicados en los últimos {days} días</span>
        </div>
      </div>
    )
  }

  const { native, clip, comparison } = data

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-blue-400" />
          <h2 className="text-sm font-semibold text-white">
            Native vs Clip ({data.total_shorts} shorts, {days}d)
          </h2>
        </div>

        {/* Day selector */}
        <div className="flex gap-1">
          {DAY_OPTIONS.map(d => (
            <button
              key={d}
              onClick={() => { setDays(d); fetchData(d) }}
              disabled={refreshing}
              className={`px-2 py-1 text-xs rounded-lg font-mono transition-colors
                ${d === days
                  ? 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
                  : 'text-slate-500 hover:text-slate-300 border border-transparent'
                }`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {/* Ratio bar */}
      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-4">
        <div className="flex items-center gap-2 mb-2">
          <Smartphone className="w-4 h-4 text-indigo-400" />
          <span className="text-xs text-slate-400">Distribución</span>
          <span className="ml-auto text-xs text-slate-500">
            {data.native_pct}% native · {(100 - data.native_pct)}% clip
          </span>
        </div>
        <div className="h-2 rounded-full bg-white/5 overflow-hidden">
          <div
            className="h-full rounded-full bg-indigo-500/60 transition-all duration-500"
            style={{ width: `${data.native_pct}%` }}
          />
        </div>
        <div className="flex justify-between mt-1 text-[10px] text-slate-500">
          <span>Native: {native.total_shorts}</span>
          <span>Clip: {clip.total_shorts}</span>
        </div>
      </div>

      {/* Side-by-side columns */}
      <div className="flex gap-3">
        <TypeColumn
          label="Native"
          data={native}
          icon={Smartphone}
          colorClass="border-l-indigo-500/30"
        />
        <TypeColumn
          label="Clip"
          data={clip}
          icon={Film}
          colorClass="border-l-amber-500/30"
        />
      </div>

      {/* Comparison footer */}
      <div className="grid grid-cols-3 gap-3">
        <div className="flex items-center gap-2 rounded-xl border border-white/5 bg-white/[0.02] p-3">
          <TrendingUp className={`w-4 h-4 ${ratioColor(comparison.views_ratio)}`} />
          <div>
            <div className="text-[10px] text-slate-500">Vistas (N/C)</div>
            <div className={`text-xs font-mono font-semibold ${ratioColor(comparison.views_ratio)}`}>
              {fmtRatio(comparison.views_ratio)}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 rounded-xl border border-white/5 bg-white/[0.02] p-3">
          <Users className={`w-4 h-4 ${ratioColor(comparison.subs_per_short_ratio)}`} />
          <div>
            <div className="text-[10px] text-slate-500">Subs/short (N/C)</div>
            <div className={`text-xs font-mono font-semibold ${ratioColor(comparison.subs_per_short_ratio)}`}>
              {fmtRatio(comparison.subs_per_short_ratio)}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 rounded-xl border border-white/5 bg-white/[0.02] p-3">
          <Clock className={`w-4 h-4 ${ratioColor(comparison.retention_ratio)}`} />
          <div>
            <div className="text-[10px] text-slate-500">Retención (N/C)</div>
            <div className={`text-xs font-mono font-semibold ${ratioColor(comparison.retention_ratio)}`}>
              {fmtRatio(comparison.retention_ratio)}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
